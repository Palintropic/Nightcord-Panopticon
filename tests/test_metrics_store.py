import os
import tempfile
import time
import unittest

import metrics_store


class KnownPanelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = metrics_store.DB_PATH
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")
        metrics_store.init_db()

    def tearDown(self):
        metrics_store.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_panel_stays_known_after_it_stops_reporting(self):
        metrics_store.mark_seen("tokyo", 1000)
        self.assertEqual(metrics_store.list_known_panels(), [{"panel": "tokyo", "seen_at": 1000}])

    def test_mark_seen_overwrites_with_latest_receive_time(self):
        metrics_store.mark_seen("tokyo", 99999999999)  # 曾经写进去的错误大值不能把它钉死
        metrics_store.mark_seen("tokyo", 1500)
        self.assertEqual(metrics_store.list_known_panels()[0]["seen_at"], 1500)

    def test_report_in_same_second_as_retire_brings_panel_back(self):
        metrics_store.mark_seen("tokyo", 1000)
        metrics_store.retire_panel("tokyo", 1001)
        metrics_store.mark_seen("tokyo", 1001)
        self.assertEqual([p["panel"] for p in metrics_store.list_known_panels()], ["tokyo"])

    def test_backfill_caps_future_agent_timestamps(self):
        metrics_store.insert_sample("fast-clock", 99999999999, 1, 2, 3, 0, 0)
        metrics_store.init_db()
        seen_at = metrics_store.list_known_panels()[0]["seen_at"]
        self.assertLessEqual(seen_at, time.time())

    def test_retired_panel_is_hidden_until_it_reports_again(self):
        metrics_store.mark_seen("old-host", 1000)
        self.assertTrue(metrics_store.retire_panel("old-host", 1100))
        self.assertEqual(metrics_store.list_known_panels(), [])
        metrics_store.mark_seen("old-host", 1200)
        self.assertEqual(metrics_store.list_known_panels(), [{"panel": "old-host", "seen_at": 1200}])

    def test_retiring_unknown_panel_is_refused(self):
        self.assertFalse(metrics_store.retire_panel("nobody", 1000))

    def test_init_backfills_last_seen_from_existing_history(self):
        metrics_store.insert_sample("shanghai", 500, 1, 2, 3, 0, 0)
        metrics_store.insert_sample("shanghai", 900, 1, 2, 3, 0, 0)
        metrics_store.init_db()
        self.assertEqual(metrics_store.list_known_panels(), [{"panel": "shanghai", "seen_at": 900}])

    def test_prune_keeps_last_seen(self):
        metrics_store.insert_sample("tokyo", 100, 1, 2, 3, 0, 0)
        metrics_store.mark_seen("tokyo", 100)
        metrics_store.prune_older_than(1000)
        self.assertEqual(metrics_store.list_known_panels(), [{"panel": "tokyo", "seen_at": 100}])


if __name__ == "__main__":
    unittest.main()
