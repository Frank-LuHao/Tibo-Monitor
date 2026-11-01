import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import monitor as m
from test_monitor import post, analysis


class ResetTimeTests(unittest.TestCase):
    def setUp(self):
        notifier = patch.object(m, "FEISHU_NOTIFY", "/test-notifier")
        notifier.start()
        self.addCleanup(notifier.stop)

    def test_time_point_range_and_absence_reach_the_real_sender(self):
        for timing in ['预计北京时间 11/03 06:00 生效。', '原帖称接下来两天会有额度重置。', '']:
            with self.subTest(timing=timing), tempfile.TemporaryDirectory() as folder:
                store = m.Store(Path(folder))
                p = post()
                a = analysis()
                a.update(reset_class='announced', evidence_strength='explicit', evidence_post_ids=[p['id']], reset_time_zh=timing)
                store.ingest([p]); store.finish(p, a)
                with store.connect() as db:
                    db.execute('UPDATE reset_notification SET queued_at=?', (time.time()-301,))
                with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
                    self.assertTrue(m.process_reset_notifications(store))
                message = run.call_args.args[0][1]
                if timing:
                    self.assertIn(timing, message)
                else:
                    self.assertEqual(message, '【Tibo Reset】\nTibo 宣布额度将 reset。\n原帖：'+p['url'])
                self.assertEqual(store.notification_stats(), {'sent': 1})

    def test_legacy_analysis_and_confirmation(self):
        for kind in ['announced', 'confirmed']:
            a=analysis(); a['reset_class']=kind
            row={'created':1000, 'post_id':'100', 'reset_class':kind, 'analysis':json.dumps(a), 'url':'https://example.com/100'}
            with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
                m.send_reset_notification([row])
            self.assertNotIn('未说明', run.call_args.args[0][1])

    def test_merge_keeps_timing_when_later_post_has_no_time(self):
        timing='原帖称接下来两天会有额度重置。'
        rows=[{'created':1000+i, 'post_id':str(100+i), 'reset_class':'announced', 'analysis':json.dumps(dict(analysis(str(100+i)), reset_time_zh=timing if i==0 else '')), 'url':'https://example.com/'+str(100+i)} for i in range(2)]
        with patch.object(m.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
            m.send_reset_notification(rows)
        self.assertIn(timing, run.call_args.args[0][1])
        self.assertIn(rows[0]['url'], run.call_args.args[0][1])

    def test_timing_does_not_change_classification_validation(self):
        a=analysis();a['reset_time_zh']=''
        self.assertEqual(m.validate_analyses({'analyses':[a]},[post()],[]), [a])
        a['reset_time_zh']=None
        with self.assertRaises(ValueError):m.validate_analyses({'analyses':[a]},[post()],[])

if __name__=='__main__':unittest.main()
