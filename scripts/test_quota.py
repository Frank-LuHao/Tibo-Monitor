import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor as m
from test_monitor import post, analysis


class QuotaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = m.Store(Path(self.temp.name))
        self.notify = patch.object(m, "FEISHU_NOTIFY", "/mock/notify")
        self.notify.start()
        self.addCleanup(self.notify.stop)

    def finish(self, ident, kind):
        p = post(ident)
        a = analysis(ident)
        a.update(reset_class=kind, evidence_strength="explicit",
                 evidence_post_ids=[ident], summary_zh="团队周限额由400提高到800，周五生效。")
        self.store.ingest([p])
        self.store.finish(p, a)
        return p, a

    def test_quota_is_immediate_deduplicated_and_uses_policy_summary(self):
        p, a = self.finish("100", "quota_change")
        self.store.finish(p, a)
        with patch.object(m.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")) as run:
            self.assertTrue(m.process_reset_notifications(self.store))
        message = run.call_args.args[0][1]
        self.assertIn(a["summary_zh"], message)
        self.assertTrue(message.startswith("【Tibo 额度公告】\n"))
        self.assertIn("原帖：" + p["url"], message)
        self.assertNotIn("reset", message)
        self.store.recover()
        self.assertFalse(m.process_reset_notifications(self.store, lambda _: self.fail("duplicate")))
        self.assertEqual(self.store.notification_stats(), {"sent": 1})

    def test_quota_does_not_merge_or_wait_for_reset_announcement(self):
        self.finish("100", "announced")
        self.finish("101", "quota_change")
        sent = []
        self.assertTrue(m.process_reset_notifications(self.store, lambda rows: sent.append([r["post_id"] for r in rows])))
        self.assertEqual(sent, [["101"]])
        self.assertFalse(m.process_reset_notifications(self.store, lambda _: self.fail("announcement too early")))

    def test_unconfigured_quota_notification_stays_pending(self):
        self.finish("100", "quota_change")
        with patch.object(m, "FEISHU_NOTIFY", ""):
            with patch.object(m.subprocess, "run", side_effect=AssertionError("unexpected send")):
                self.assertFalse(m.process_reset_notifications(self.store))
        self.assertEqual(self.store.notification_stats(), {"pending": 1})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT attempts FROM reset_notification").fetchone()[0], 0)

    def test_reset_confirmation_remains_immediate(self):
        self.finish("100", "confirmed")
        sent = []
        self.assertTrue(m.process_reset_notifications(self.store, lambda rows: sent.append(rows[0]["post_id"])))
        self.assertEqual(sent, ["100"])

    def test_nonannouncements_never_enter_notification_queue(self):
        for ident, kind in [("100", "unrelated"), ("101", "discussion"), ("102", "uncertain")]:
            self.finish(ident, kind)
        self.assertEqual(self.store.notification_stats(), {})

    def test_quota_requires_explicit_available_evidence(self):
        p, a = self.finish("100", "quota_change")
        self.assertEqual(m.validate_analyses({"analyses": [a]}, [p], []), [a])
        a["evidence_strength"] = "contextual"
        with self.assertRaises(ValueError):
            m.validate_analyses({"analyses": [a]}, [p], [])
        a.update(evidence_strength="explicit", evidence_post_ids=[])
        with self.assertRaises(ValueError):
            m.validate_analyses({"analyses": [a]}, [p], [])

    def test_uncertain_quota_delivery_is_not_resent_after_restart(self):
        self.finish("100", "quota_change")
        def timeout(_):
            raise m.MailDeliveryError("delivery timeout", uncertain=True)
        with self.assertLogs(level="ERROR"):
            m.process_reset_notifications(self.store, timeout)
        self.store.recover()
        self.assertEqual(self.store.notification_stats(), {"unknown": 1})
        self.assertFalse(m.process_reset_notifications(self.store, lambda _: self.fail("unsafe retry")))


if __name__ == "__main__":
    unittest.main()
