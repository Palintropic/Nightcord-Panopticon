import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

import app
from config_editor import appearance_of, clean_appearance, clean_kaomoji, kaomoji_of, merge_submitted


ROOT = pathlib.Path(__file__).resolve().parents[1]


class AppearanceConfigTests(unittest.TestCase):
    def test_defaults_to_the_accent(self):
        self.assertEqual(appearance_of({}), {"curve": "#884499", "tile": "#884499", "bar": "#884499"})

    def test_bad_stored_values_fall_back_per_part(self):
        cfg = {"appearance": {"tile": "#4455DD", "curve": "url(x)", "bar": None}}
        self.assertEqual(appearance_of(cfg), {"curve": "#884499", "tile": "#4455dd", "bar": "#884499"})

    def test_submitted_colours_are_normalised_or_refused(self):
        colours, errors = clean_appearance({"tile": "4455DD", "curve": " #ddaacc ", "bar": "#8888cc"})
        self.assertEqual(errors, [])
        self.assertEqual(colours, {"curve": "#ddaacc", "tile": "#4455dd", "bar": "#8888cc"})
        _, errors = clean_appearance({"tile": "#fff", "curve": "red;}", "bar": "#8888cc"})
        self.assertEqual(len(errors), 2)

    def test_saving_the_settings_form_keeps_the_colours(self):
        old = {"appearance": {"tile": "#4455dd", "curve": "#ddaacc", "bar": "#8888cc"}}
        merged, _ = merge_submitted(old, {})
        self.assertEqual(merged["appearance"], old["appearance"])


class KaomojiConfigTests(unittest.TestCase):
    def test_empty_by_default(self):
        self.assertEqual(kaomoji_of({}), {"assign": {}, "faces": {}})

    def test_blank_triples_are_dropped_and_text_trimmed(self):
        k, errors = clean_kaomoji({"assign": {"上海": " 瑞希 "},
                                   "faces": {"瑞希": [" (・ε・) ", "", ""], "奏": ["", "", ""]}})
        self.assertEqual(errors, [])
        self.assertEqual(k, {"assign": {"上海": "瑞希"}, "faces": {"瑞希": ["(・ε・)", "", ""]}})

    def test_bad_entries_are_refused(self):
        _, errors = clean_kaomoji({"faces": {"瑞希": ["a", "b"]}})
        self.assertEqual(len(errors), 1)
        _, errors = clean_kaomoji({"faces": {"瑞希": ["x" * 25, "", ""]}})
        self.assertEqual(len(errors), 1)
        _, errors = clean_kaomoji({"faces": {"瑞希": ["a\nb", "", ""]}})
        self.assertEqual(len(errors), 1)
        _, errors = clean_kaomoji({"assign": {"上海": 3}})
        self.assertEqual(len(errors), 1)
        _, errors = clean_kaomoji({"assign": ["上海"]})
        self.assertEqual(errors, ["格式不对"])

    def test_saving_the_settings_form_keeps_the_kaomoji(self):
        old = {"kaomoji": {"assign": {"上海": "瑞希"}, "faces": {}}}
        merged, _ = merge_submitted(old, {})
        self.assertEqual(merged["kaomoji"], old["kaomoji"])


class AppearanceApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "config.yaml")
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"panels": [], "metrics_agent": {"enabled": True}}, f)
        self.old_path = app.CONFIG_PATH
        app.CONFIG_PATH = self.path
        self.client = app.app.test_client()

    def tearDown(self):
        app.CONFIG_PATH = self.old_path
        self.dir.cleanup()

    def test_save_then_read_back(self):
        r = self.client.post("/api/appearance", json={"tile": "#4455DD", "curve": "#bbdd22", "bar": "#33aaee"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/appearance").get_json(),
                         {"curve": "#bbdd22", "tile": "#4455dd", "bar": "#33aaee"})
        with open(self.path, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        self.assertEqual(saved["metrics_agent"], {"enabled": True})   # nothing else touched

    def test_invalid_colour_is_refused_and_not_saved(self):
        r = self.client.post("/api/appearance", json={"tile": "javascript:x", "curve": "#bbdd22", "bar": "#33aaee"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/api/appearance").get_json()["tile"], "#884499")

    def test_kaomoji_save_then_read_back(self):
        body = {"assign": {"东京": "爱莉"}, "faces": {"爱莉": ["", "", "(｀皿´)"]}}
        r = self.client.post("/api/kaomoji", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/kaomoji").get_json(), body)
        r = self.client.post("/api/kaomoji", json={"faces": {"爱莉": ["x" * 30, "", ""]}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get("/api/kaomoji").get_json(), body)

    def test_old_settings_addresses_forward_in_the_browser(self):
        for path in ("/settings", "/setup"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200)
            self.assertIn("location.replace(", r.get_data(as_text=True))

    def test_forward_page_opens_without_signing_in(self):
        # With login on, the forward still has to run so the #s=... survives into the login page.
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"panels": [], "dashboard_auth": {"enabled": True, "password_hash": "x"}}, f)
        self.assertEqual(self.client.get("/settings").status_code, 200)
        self.assertEqual(self.client.get("/api/appearance").status_code, 401)
        self.assertEqual(self.client.get("/").status_code, 302)

    @unittest.skipUnless(shutil.which("node"), "needs node to run the login page script")
    def test_login_returns_to_dashboard_addresses_only(self):
        page = (ROOT / "static" / "login.html").read_text(encoding="utf-8")
        fn = re.search(r"function returnHash\(\)\{.*?\n\}", page, re.S).group(0)
        runner = ("const fn = require('fs').readFileSync(0, 'utf8'); const out = {};"
                  "for(const hash of ['', '#s=notify', '#p=5', '#m=%E4%B8%8A%E6%B5%B7', '#x', '#s=a b', '#p=5#m=x']){"
                  "  out[hash] = new Function('location', fn + '; return returnHash();')({hash});"
                  "} console.log(JSON.stringify(out));")
        result = subprocess.run(["node", "-e", runner], input=fn, capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "": "", "#s=notify": "#s=notify", "#p=5": "#p=5", "#m=%E4%B8%8A%E6%B5%B7": "#m=%E4%B8%8A%E6%B5%B7",
            "#x": "", "#s=a b": "", "#p=5#m=x": "",
        })

    @unittest.skipUnless(shutil.which("node"), "needs node to run the save queue")
    def test_saves_go_out_one_at_a_time_with_the_latest_state(self):
        # The first reply is slow; the second save must wait for it and carry the newest state,
        # so the server ends with the last pick whatever order the network would have used.
        src = (ROOT / "static" / "settings.js").read_text(encoding="utf-8")
        fn = re.search(r"function makeSaveQueue\(\)\{.*?\n\}", src, re.S).group(0)
        runner = fn + """
        const queue = makeSaveQueue(); let state = 'a'; const sent = []; const done = [];
        const send = delay => () => { sent.push(state); return new Promise(r => setTimeout(() => r({ok: true}), delay)); };
        queue(send(50), (r, last) => done.push(['first', last]));
        state = 'b';
        const second = queue(send(0), (r, last) => done.push(['second', last]));
        second.then(ok => console.log(JSON.stringify({sent, done, ok})));
        """
        result = subprocess.run(["node", "-e", runner], capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout),
                         {"sent": ["b", "b"], "done": [["first", False], ["second", True]], "ok": True})

    @unittest.skipUnless(shutil.which("node"), "needs node to run the forwarding script")
    def test_old_bookmarks_keep_their_page(self):
        # The page name after # never reaches the server, so the forward happens in the browser.
        html = self.client.get("/settings").get_data(as_text=True)
        script = html.split("<script>")[1].split("</script>")[0]
        runner = ("const script = require('fs').readFileSync(0, 'utf8'); const out = {};"
                  "for(const hash of ['', '#s=notify', '#s=appearance', '#s=panels', '#s=constructor', '#s=bogus', '#x']){"
                  "  const location = {hash, replace(u){ out[hash] = u; }};"
                  "  new Function('location', script)(location);"
                  "} console.log(JSON.stringify(out));")
        result = subprocess.run(["node", "-e", runner], input=script, capture_output=True, text=True, timeout=30, check=True)
        self.assertEqual(json.loads(result.stdout), {
            "": "/#p=5", "#s=notify": "/#s=notify", "#s=appearance": "/#s=theme", "#s=panels": "/#s=panels",
            "#s=constructor": "/#p=5", "#s=bogus": "/#p=5", "#x": "/#p=5",
        })


if __name__ == "__main__":
    unittest.main()
