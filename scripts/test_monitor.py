import json
from pathlib import Path
import tempfile
import threading
import subprocess
import unittest
from unittest.mock import patch

import monitor as m


def post(post_id="100", created=1000, text="hello", author=m.HANDLE):
    return {"type": "status", "id": post_id, "created_timestamp": created, "text": text,
            "url": f"https://x.com/{author}/status/{post_id}",
            "author": {"id": m.AUTHOR_ID if author == m.HANDLE else "123", "screen_name": author}}


def analysis(post_id="100"):
    return {"post_id": post_id, "summary_zh": "测试摘要", "reset_class": "unrelated",
            "evidence_strength": "none", "evidence_post_ids": [], "scope_zh": "未说明", "reason_zh": "无关"}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = m.Store(self.data)

    def test_initial_history_is_context_only_then_deduplicates(self):
        response = {"code": 200, "results": [post()], "cursor": {}}
        with patch.object(m.time, "time", return_value=1100):
            m.poll_once(self.store, lambda *_: response)
        self.assertEqual(self.store.stats(), {"baseline": 1})
        response["results"] += [post("101", 1101), post("102", 1102, author="other")]
        self.assertEqual(m.poll_once(self.store, lambda *_: response), 1)
        self.assertEqual(m.poll_once(self.store, lambda *_: response), 0)
        self.assertEqual(self.store.stats(), {"baseline": 1, "pending": 1})

    def test_forged_author_is_not_tibo(self):
        value = post()
        value["author"]["id"] = "999"
        self.assertFalse(m.own_post(value))

    def test_pagination_filters_context_and_reaches_overlap(self):
        calls = []
        def fetch(path, params):
            calls.append(dict(params))
            if "cursor" not in params:
                return {"results": [post("110", 1100), post("115", 1150, author="other")], "cursor": {"bottom": "next"}}
            return {"results": [post("090", 900)], "cursor": {"bottom": "older"}}
        posts, covered = m.fetch_timeline(1000, fetch)
        self.assertTrue(covered)
        self.assertEqual([p["id"] for p in posts], ["110", "090"])
        self.assertEqual(calls[1]["cursor"], "next")

    def test_coverage_failure_keeps_checkpoint_and_queues_discovered_posts(self):
        self.store.put_meta(baseline_timestamp=1000, highwater_timestamp=1000)
        def fetch(*_):
            return {"results": [post("101", 1200)], "cursor": {"bottom": "same"}}
        with self.assertRaisesRegex(RuntimeError, "coverage gap"):
            m.poll_once(self.store, fetch)
        self.assertEqual(self.store.meta()["highwater_timestamp"], 1000)
        self.assertEqual(self.store.stats(), {"pending": 1})

    def test_204_does_not_create_jobs(self):
        self.store.put_meta(baseline_timestamp=1000, highwater_timestamp=1000)
        self.assertEqual(m.poll_once(self.store, lambda *_: None), 0)
        self.assertEqual(self.store.stats(), {})

    def test_network_error_keeps_state(self):
        self.store.put_meta(baseline_timestamp=1000, highwater_timestamp=1000)
        before = self.store.meta()
        def fail(*_):
            raise OSError("offline")
        with self.assertRaises(OSError):
            m.poll_once(self.store, fail)
        self.assertEqual(before, self.store.meta())

    def test_restart_recovers_inflight_without_losing_attempt(self):
        self.store.ingest([post()])
        self.store.claim()
        self.assertEqual(self.store.stats(), {"analyzing": 1})
        m.Store(self.data).recover()
        self.assertEqual(self.store.stats(), {"pending": 1})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM posts").fetchone()[0], 0)

    def test_three_failures_are_retained_and_backoff_is_enforced(self):
        self.store.ingest([post()])
        for attempt in range(3):
            self.store.claim()
            self.store.fail([post()], "offline")
            self.assertEqual(self.store.claim(), [])
            if attempt < 2:
                with self.store.connect() as db:
                    db.execute("UPDATE posts SET retry_at=0")
        self.assertEqual(self.store.stats(), {"failed": 1})

    def test_no_new_posts_does_not_invoke_model(self):
        def forbidden(*_):
            self.fail("Model must not run with an empty queue")
        self.assertFalse(m.process_batch(self.store, self.data / "reports", threading.Event(), forbidden))

    def test_success_writes_report_before_completion(self):
        self.store.ingest([post()])
        m.process_batch(self.store, self.data / "reports", threading.Event(),
                        lambda *_: [analysis()], lambda _, context: context)
        self.assertEqual(self.store.stats(), {"done": 1})
        self.assertEqual(json.loads((self.data / "reports/100.json").read_text(encoding="utf-8"))["analysis"], analysis())

    def test_output_failure_does_not_mark_done(self):
        self.store.ingest([post()])
        with patch.object(m, "save_report", side_effect=OSError("disk full")):
            with self.assertLogs(level="ERROR"):
                m.process_batch(self.store, self.data / "reports", threading.Event(),
                                lambda *_: [analysis()], lambda _, context: context)
        self.assertEqual(self.store.stats(), {"pending": 1})

    def test_rejects_missing_duplicate_or_hallucinated_evidence(self):
        with self.assertRaises(ValueError):
            m.validate_analyses({"analyses": []}, [post()], [])
        with self.assertRaises(ValueError):
            m.validate_analyses({"analyses": [analysis(), analysis()]}, [post()], [])
        item = analysis()
        item["evidence_post_ids"] = ["999"]
        with self.assertRaises(ValueError):
            m.validate_analyses({"analyses": [item]}, [post()], [])

    def test_context_contains_actual_quote_chain(self):
        p = post()
        p["quote"] = post("099", 900, "Day 10/ Reset")
        item = analysis()
        item.update(reset_class="confirmed", evidence_strength="explicit", evidence_post_ids=["100", "099"])
        self.assertEqual(m.validate_analyses({"analyses": [item]}, [p], []), [item])

    def test_instance_lock_prevents_duplicate_processes(self):
        self.assertFalse(m.is_running(self.data))
        with m.InstanceLock(self.data / "instance.lock"):
            self.assertTrue(m.is_running(self.data))
        self.assertFalse(m.is_running(self.data))

    def exhaust_analysis_retries(self, posts=None):
        posts = posts or [post()]
        self.store.ingest(posts)
        for _ in range(3):
            claimed = self.store.claim()
            self.store.fail(claimed, "model unavailable")
            with self.store.connect() as db:
                db.execute("UPDATE posts SET retry_at=0")

    def test_failure_mail_only_after_third_failure_and_no_duplicates(self):
        self.store.ingest([post()])
        for attempt in range(3):
            self.store.fail(self.store.claim(), "offline")
            self.assertEqual(self.store.mail_stats(), {"pending": 1} if attempt == 2 else {})
            with self.store.connect() as db:
                db.execute("UPDATE posts SET retry_at=0")
        self.store.fail([post()], "same failure")
        sent = []
        self.assertTrue(m.process_failure_mail(self.store, lambda ids, _: sent.append(ids)))
        self.store.recover()
        self.assertFalse(m.process_failure_mail(self.store, lambda *_: self.fail("duplicate email")))
        self.assertEqual(sent, [["100"]])
        self.assertEqual(self.store.mail_stats(), {"sent": 1})

    def test_one_mail_groups_failed_batch_and_survives_restart(self):
        self.exhaust_analysis_retries([post(), post("101")])
        reopened = m.Store(self.data)
        reopened.recover()
        sent = []
        m.process_failure_mail(reopened, lambda ids, _: sent.append(ids))
        self.assertEqual(sent, [["100", "101"]])

    def test_user_cancellation_never_exhausts_retries_or_emails(self):
        self.store.ingest([post()])
        def cancel(*_):
            raise m.AnalysisCancelled()
        for _ in range(4):
            m.process_batch(self.store, self.data / 'reports', threading.Event(), cancel, lambda p, c: c)
        self.assertEqual(self.store.stats(), {"pending": 1})
        self.assertEqual(self.store.mail_stats(), {})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM posts").fetchone()[0], 0)

    def test_mail_retries_are_bounded_and_keep_analysis_failed(self):
        self.exhaust_analysis_retries()
        def offline(*_):
            raise m.MailDeliveryError('network unavailable', retryable=True)
        for attempt in range(3):
            with self.assertLogs(level='ERROR'):
                m.process_failure_mail(self.store, offline)
            self.assertEqual(self.store.mail_stats(), {"pending": 1} if attempt < 2 else {"error": 1})
            self.assertFalse(m.process_failure_mail(self.store, lambda *_: self.fail('retry too early')))
            with self.store.connect() as db:
                db.execute("UPDATE failure_mail SET retry_at=0")
        self.assertEqual(self.store.stats(), {"failed": 1})

    def test_delivery_timeout_and_crash_do_not_automatically_resend(self):
        self.exhaust_analysis_retries()
        def timeout(*_):
            raise m.MailDeliveryError('timeout', uncertain=True)
        with self.assertLogs(level='ERROR'):
            m.process_failure_mail(self.store, timeout)
        self.assertEqual(self.store.mail_stats(), {"unknown": 1})
        self.assertFalse(m.process_failure_mail(self.store, lambda *_: self.fail('duplicate risk')))
        with self.store.connect() as db:
            db.execute("UPDATE failure_mail SET state='sending'")
        self.store.recover()
        self.assertEqual(self.store.mail_stats(), {"unknown": 1})

    def test_mail_command_uses_authorized_recipient_and_workspace(self):
        with patch.dict('os.environ', {'TIBO_MONITOR_FAILURE_EMAIL': 'alerts@example.invalid'}):
            with patch.object(m, 'FAILURE_EMAIL', 'alerts@example.invalid'):
                with patch.object(m.shutil, 'which', return_value='/mock/agently-cli'):
                    with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
                        m.send_failure_email(['100'], self.data)
        args, kwargs = run.call_args
        command = args[0]
        self.assertEqual(command[command.index('--to') + 1], 'alerts@example.invalid')
        self.assertIn('--confirmed', command)
        self.assertEqual(kwargs['env']['AGENTLY_WORKSPACE'], 'codex')
        self.assertIn('https://x.com/thsottiaux/status/100', command[command.index('--body') + 1])

    def test_notifications_wait_locally_until_explicitly_configured(self):
        self.exhaust_analysis_retries()
        self.assertFalse(m.process_failure_mail(self.store))
        self.assertEqual(self.store.mail_stats(), {"pending": 1})
        with self.store.connect() as db:
            db.execute("INSERT INTO reset_notification(post_id,reset_class,created,url,queued_at) VALUES(?,?,?,?,?)",
                       ("100", "confirmed", 100, "https://x.com/thsottiaux/status/100", 100))
        self.assertFalse(m.process_reset_notifications(self.store))
        self.assertEqual(self.store.notification_stats(), {"pending": 1})

    def test_cli_rate_limit_and_timeout_preserve_delivery_semantics(self):
        with patch.object(m, 'FAILURE_EMAIL', 'alerts@example.invalid'):
            with patch.object(m.shutil, 'which', return_value='/mock/agently-cli'):
                response = subprocess.CompletedProcess([], 7, b'{"error":{"message":"Retry-After: 900"}}', b'')
                with patch.object(m.subprocess, 'run', return_value=response):
                    with self.assertRaises(m.MailDeliveryError) as rate:
                        m.send_failure_email(['100'], self.data)
                self.assertTrue(rate.exception.retryable)
                self.assertEqual(rate.exception.retry_after, 900)
                with patch.object(m.subprocess, 'run', side_effect=subprocess.TimeoutExpired('mock', 45)):
                    with self.assertRaises(m.MailDeliveryError) as timeout:
                        m.send_failure_email(['100'], self.data)
                self.assertTrue(timeout.exception.uncertain)


if __name__ == "__main__":
    unittest.main()
