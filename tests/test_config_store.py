import os
import tempfile
import unittest

try:
    from config_store import load_yaml, save_yaml, update_yaml
except ModuleNotFoundError as error:
    if error.name != "yaml":
        raise
    load_yaml = save_yaml = update_yaml = None


@unittest.skipUnless(load_yaml, "PyYAML is required; install requirements.txt to run config-store tests")
class ConfigStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "config.yaml")

    def tearDown(self):
        self.directory.cleanup()

    def test_save_replaces_complete_yaml_document(self):
        save_yaml(self.path, {"dashboard_auth": {"enabled": True}, "panels": []})
        self.assertEqual(load_yaml(self.path)["dashboard_auth"]["enabled"], True)

    def test_update_preserves_unrelated_configuration(self):
        save_yaml(self.path, {"panels": [{"name": "A"}], "dashboard_auth": {}})

        def add_secret(config):
            config["dashboard_auth"]["session_secret"] = "test-secret"

        update_yaml(self.path, add_secret)
        updated = load_yaml(self.path)
        self.assertEqual(updated["panels"], [{"name": "A"}])
        self.assertEqual(updated["dashboard_auth"]["session_secret"], "test-secret")


if __name__ == "__main__":
    unittest.main()
