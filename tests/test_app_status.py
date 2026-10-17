import json
import os
import tempfile
import unittest

import metrics_store

# Point the store at a temporary database before app.py imports it (app calls init_db at import).
_TMP = tempfile.TemporaryDirectory()
metrics_store.DB_PATH = os.path.join(_TMP.name, "metrics.db")

import yaml  # noqa: E402

import app  # noqa: E402


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_config = app.CONFIG_PATH
        app.CONFIG_PATH = os.path.join(self.tmp.name, "config.yaml")
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")
        metrics_store.init_db()
        with app._remote_lock:
            app._remote.update(data=None, ts=0, fp=None, refreshing=False, seq=0, stored_seq=0)
        self.client = app.app.test_client()
        self.old_refresh = app._refresh_remote_async

    def tearDown(self):
        app._refresh_remote_async = self.old_refresh
        app.CONFIG_PATH = self.old_config
        self.tmp.cleanup()

    def write_config(self, cfg):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True)


class RemoteCacheTests(AppTestCase):
    def test_cache_from_an_older_config_is_ignored_even_if_another_process_changed_it(self):
        self.write_config({"panels": [{"name": "bt-old", "url": "http://x", "api_key": "k"}]})
        ticket = app._begin_remote_fetch()
        app._store_remote([{"name": "bt-old", "url": "http://x", "online": True}], ticket)
        # Another worker saves the config; this process never hears about it.
        self.write_config({"panels": []})
        app._refresh_remote_async = lambda: None
        body = self.client.get("/api/status").get_json()
        self.assertEqual([p["name"] for p in body["panels"]], [])
        self.assertFalse(body["remote_pending"])

    def test_fetch_uses_one_config_snapshot_for_fingerprint_and_sources(self):
        # ena: config A → B (panel added) → back to A while a fetch runs. The result fetched under B
        # must not land with A's fingerprint.
        self.write_config({"panels": []})
        ticket = app._begin_remote_fetch()
        self.write_config({"panels": [{"name": "bt-temp", "url": "http://x", "api_key": "k"}]})
        result = app.fetch_remote(ticket["cfg"])
        self.write_config({"panels": []})
        app._store_remote(result, ticket)
        app._refresh_remote_async = lambda: None
        body = self.client.get("/api/status").get_json()
        self.assertEqual([p["name"] for p in body["panels"]], [])

    def test_an_earlier_fetch_cannot_overwrite_a_later_one(self):
        self.write_config({"panels": [{"name": "bt", "url": "http://x", "api_key": "k"}]})
        first = app._begin_remote_fetch()
        second = app._begin_remote_fetch()
        app._store_remote([{"name": "bt", "online": True}], second)
        app._store_remote([{"name": "bt", "online": False}], first)
        self.assertTrue(app._remote["data"][0]["online"])

    def test_status_answers_without_waiting_for_remote_sources(self):
        self.write_config({"panels": [{"name": "bt", "url": "http://x", "api_key": "k"}]})
        started = []
        app._refresh_remote_async = lambda: started.append(True)
        body = self.client.get("/api/status").get_json()
        self.assertTrue(body["remote_pending"])
        self.assertEqual(started, [True])


class DeployTests(AppTestCase):
    def test_one_click_deploy_reaches_the_installer(self):
        self.write_config({"metrics_agent": {"enabled": True, "shared_secret": "s3cret",
                                             "report_url": "http://10.0.0.1:1810"}})
        calls = []

        def fake_deploy(ip, port, ssh_user, password, report_url, shared_secret, log=None, panel_name_override=None):
            calls.append((ip, report_url, shared_secret))
            return {"ok": True, "panel_name": "new-host"}

        original = app.deploy_agent
        app.deploy_agent = fake_deploy
        try:
            res = self.client.post("/api/agent/deploy", json={"ip": "10.0.0.9", "password": "pw"})
            events = [json.loads(line) for line in res.get_data(as_text=True).splitlines() if line.strip()]
        finally:
            app.deploy_agent = original
        self.assertEqual(calls, [("10.0.0.9", "http://10.0.0.1:1810/api/metrics/report", "s3cret")])
        self.assertTrue(events[-1]["ok"])


if __name__ == "__main__":
    unittest.main()
