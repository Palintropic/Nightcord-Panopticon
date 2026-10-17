import json
import pathlib
import shutil
import subprocess
import unittest

HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "dashboard_harness.js"


@unittest.skipUnless(shutil.which("node"), "needs node to run the dashboard script")
class DashboardStatusBarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=30, check=True)
        cls.steps = json.loads(out.stdout)

    def test_fresh_data_reads_normally(self):
        self.assertEqual(self.steps["fresh"], "1 / 1 在线")

    def test_failed_refresh_is_shown(self):
        self.assertIn("刷新失败", self.steps["afterFailure"])

    def test_failure_survives_switching_pivots(self):
        # ena: success → network down → switch pivot used to redraw "1 / 1 在线" from stale data.
        self.assertIn("刷新失败", self.steps["afterSwitchingPivot"])

    def test_old_data_is_flagged_by_its_age(self):
        self.assertIn("数据 10 分钟前", self.steps["oldData"])
        self.assertNotIn("刷新失败", self.steps["oldData"])


if __name__ == "__main__":
    unittest.main()
