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


    def test_removed_settings_pages_land_on_the_settings_list(self):
        routes = self.steps["routes"]
        self.assertEqual(routes["#s=notify"], {"view": "settings", "page": "notify"})
        self.assertEqual(routes["#s=appearance"], {"view": "settings", "page": "theme"})
        self.assertEqual(routes["#s=panels"], {"view": "list", "pivot": 5})
        self.assertEqual(routes["#s=bogus"], {"view": "list", "pivot": 5})

    def test_signed_out_sees_tiles_only(self):
        self.assertEqual(self.steps["publicRoute"], {"view": "list", "pivot": 0})
        html = self.steps["publicOverview"]
        self.assertIn('ov-wall', html)
        self.assertNotIn('href=', html)            # nothing to open
        self.assertNotIn('ov-switch', html)        # no metric curves
        self.assertIn('state-offline', html)       # gone is grey
        self.assertIn('state-caution', html)
        self.assertLess(html.index('gone'), html.index('tokyo'))   # problems first
        self.assertIn("1 / 2 在线", self.steps["publicSummary"])
        self.assertIn("1 接近阈值", self.steps["publicSummary"])
        self.assertEqual(self.steps["publicButton"], "登录")
        self.assertEqual(self.steps["publicUpdated"], "")           # no times signed out
        self.assertIn("PUBLIC-NEAR", html)                          # faces come with the cards

    def test_a_failing_me_shows_the_public_page_instead_of_bouncing(self):
        self.assertEqual(self.steps["meFails_network"], {"view": "list", "pivot": 0})
        self.assertEqual(self.steps["meFails_broken"], {"view": "list", "pivot": 0})
        self.assertEqual(self.steps["publicRedirect"], self.steps["afterExpiry"])   # no new redirect

    def test_after_a_failed_me_the_next_refresh_restores_the_signed_in_view(self):
        self.assertEqual(self.steps["meRecovered"], {"view": "settings", "page": "notify"})

    def test_expired_session_goes_back_to_sign_in_at_the_same_place(self):
        self.assertEqual(self.steps["afterExpiry"], "/login#p=1")

    def test_general_admin_never_opens_a_settings_page(self):
        self.assertEqual(self.steps["adminRoute"], {"view": "list", "pivot": 5})

    def _row(self, name):
        rows = self.steps["machines"].split('class="machine-row"')
        return next(r for r in rows if f'<span class="name">{name}</span>' in r)

    def test_table_shows_swap_and_load(self):
        self.assertIn('<span class="num">Swap</span><span class="num">负载</span>', self.steps["machines"])
        row = self._row("new")
        self.assertIn('<span class="num">12.5%</span>', row)
        self.assertIn('<span class="num">0.42</span>', row)

    def test_machine_without_swap_says_so(self):
        self.assertIn('<span class="num">无</span>', self._row("noswap"))
        self.assertIn("未启用", self.steps["overviewNoSwap"])

    def test_old_agent_shows_dashes_and_no_system_group(self):
        row = self._row("old")
        self.assertEqual(row.count('<span class="num">-</span>'), 2)
        self.assertNotIn('<div class="group-header">系统</div>', self.steps["overviewOld"])
        self.assertNotIn("进程数", self.steps["overviewOld"])

    def test_overview_lists_the_new_system_fields(self):
        html = self.steps["overviewNew"]
        for text in ("0.42 / 0.30 / 0.25（4 核）", "187", "23", "1 天 1 小时", "TCP 连接（已建立）"):
            self.assertIn(text, html)


if __name__ == "__main__":
    unittest.main()
