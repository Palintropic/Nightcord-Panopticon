import os
import tempfile
import unittest

import yaml

import app
from config_editor import appearance_of, clean_appearance, merge_submitted


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

    def test_old_settings_address_lands_on_the_settings_pivot(self):
        for path in ("/settings", "/setup"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r.headers["Location"].endswith("/#p=5"))


if __name__ == "__main__":
    unittest.main()
