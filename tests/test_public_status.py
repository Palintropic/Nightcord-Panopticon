import os
import tempfile
import time
import unittest
from unittest import mock

from werkzeug.security import generate_password_hash

import metrics_store

_TMP = tempfile.TemporaryDirectory()
metrics_store.DB_PATH = os.path.join(_TMP.name, "metrics.db")

import yaml  # noqa: E402

import app  # noqa: E402

SECRET = "s3cret"
PUBLIC_KEYS = {"name", "kind", "online", "state", "nodata"}


class PublicStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_config = app.CONFIG_PATH
        app.CONFIG_PATH = os.path.join(self.tmp.name, "config.yaml")
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")
        metrics_store.init_db()
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "dashboard_auth": {"enabled": True, "username": "mizuki", "password_hash": generate_password_hash("pw")},
                "metrics_agent": {"enabled": True, "shared_secret": SECRET},
                "nightcord_status": {"enabled": True, "name": "Nightcord-Status", "source": "push"},
            }, f)
        self.client = app.app.test_client()
        p = mock.patch.object(app, "_refresh_remote_async", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        app.CONFIG_PATH = self.old_config
        self.tmp.cleanup()

    def report(self, panel, cpu=5, mem=5, disk=5, **extra):
        body = {"panel": panel, "ts": int(time.time()), "cpu": cpu, "mem": mem, "disk": disk,
                "net_in_kbps": 0, "net_out_kbps": 0, "ip_internal": "10.10.0.3", "ip_external": "64.83.38.7",
                "disk_detail": [{"path": "/srv/secret-mount", "total": 100, "used": 5, "percent": disk}], **extra}
        resp = self.client.post(app.METRICS_REPORT_PATH, json=body, headers={"X-Metrics-Secret": SECRET})
        self.assertEqual(resp.status_code, 200)

    def public(self):
        resp = self.client.get("/api/public/status")
        self.assertEqual(resp.status_code, 200)
        return resp

    def card(self, name):
        return next(p for p in self.public().get_json()["panels"] if p["name"] == name)

    def test_signed_out_gets_the_page_and_names_with_states_only(self):
        self.report("tokyo", cpu=95.5)
        self.assertEqual(self.client.get("/").status_code, 200)
        resp = self.public()
        text = resp.get_data(as_text=True)
        for leak in ("10.10.0.3", "64.83.38.7", "secret-mount", "95.5", "cpu", "qingyuan\":{"):
            self.assertNotIn(leak, text)
        tokyo = self.card("tokyo")
        self.assertEqual(set(tokyo), PUBLIC_KEYS)
        self.assertEqual(tokyo, {"name": "tokyo", "kind": "qingyuan", "online": True, "state": "warn", "nodata": False})

    def test_states_follow_the_dashboard_thresholds(self):
        self.report("calm")
        self.report("near", mem=75)
        self.report("full", disk=90)
        self.assertEqual(self.card("calm")["state"], "ok")
        self.assertEqual(self.card("near")["state"], "caution")
        self.assertEqual(self.card("full")["state"], "warn")

    def test_offline_machine_is_grey_and_needs_attention(self):
        self.report("gone")
        later = time.time() + app.AGENT_STALE_SECONDS + 5
        with mock.patch.object(app.time, "time", lambda: later):
            card = self.card("gone")
        self.assertEqual(card, {"name": "gone", "kind": "qingyuan", "online": False, "state": "warn", "nodata": True})

    def test_nightcord_status_shows_its_state_but_not_its_targets(self):
        self.client.post(app.STATUS_REPORT_PATH, headers={"X-Metrics-Secret": SECRET}, json={"targets": [
            {"id": "esxi-1", "display_name": "Nightcord-ESXi-1", "type": "esxi", "status": "down",
             "last_error": "timed out", "detail": {"host": "192.168.3.230"}}]})
        text = self.public().get_data(as_text=True)
        for leak in ("ESXi", "192.168.3.230", "timed out", "esxi-1"):
            self.assertNotIn(leak, text)
        self.assertEqual(self.card("Nightcord-Status"),
                         {"name": "Nightcord-Status", "kind": "nightcord-status", "online": True, "state": "warn",
                          "nodata": False})

    def test_everything_else_still_needs_signing_in(self):
        self.report("tokyo")
        for path in ("/api/status", "/api/metrics/history?panel=tokyo", "/api/agents", "/api/config", "/api/me"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    def test_with_sign_in_off_nothing_changes(self):
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump({"metrics_agent": {"enabled": True, "shared_secret": SECRET}}, f)
        self.report("tokyo")
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertEqual(self.client.get("/api/me").get_json()["role"], "superadmin")


if __name__ == "__main__":
    unittest.main()
