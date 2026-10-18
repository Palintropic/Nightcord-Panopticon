import os
import tempfile
import unittest

import yaml

import app
from config_editor import appearance_of, clean_appearance, clean_kaomoji, kaomoji_of, merge_submitted


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

    def test_old_settings_address_lands_on_the_settings_pivot(self):
        for path in ("/settings", "/setup"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/#p=5"))


if __name__ == "__main__":
    unittest.main()
