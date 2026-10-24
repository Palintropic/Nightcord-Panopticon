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
        self.refreshes = []
        p = mock.patch.object(app, "_refresh_remote_async", lambda: self.refreshes.append(1))
        p.start()
        self.addCleanup(p.stop)
        app._public_cache.update(at=0.0, body=None)
        with app._remote_lock:
            app._remote.update(data=None, ts=0, fp=None, refreshing=False, seq=0, stored_seq=0)

    def tearDown(self):
        app.CONFIG_PATH = self.old_config
        self.tmp.cleanup()

    def report(self, panel, cpu=5, mem=5, disk=5, **extra):
        body = {"panel": panel, "ts": int(time.time()), "cpu": cpu, "mem": mem, "disk": disk,
                "net_in_kbps": 0, "net_out_kbps": 0, "ip_internal": "10.10.0.3", "ip_external": "64.83.38.7",
                "disk_detail": [{"path": "/srv/secret-mount", "total": 100, "used": 5, "percent": disk}], **extra}
        resp = self.client.post(app.METRICS_REPORT_PATH, json=body, headers={"X-Metrics-Secret": SECRET})
        self.assertEqual(resp.status_code, 200)

    def write(self, **extra):
        with open(app.CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        cfg.update(extra)
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f)

    def public(self):
        app._public_cache.update(at=0.0, body=None)
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

    def test_threshold_edges_match_the_dashboard(self):
        # index.html levelOf: >= threshold is warn, >= 70 is caution.
        self.report("at-threshold", cpu=90)
        self.report("at-seventy", cpu=70)
        self.report("just-under", cpu=69.9)
        self.assertEqual(self.card("at-threshold")["state"], "warn")
        self.assertEqual(self.card("at-seventy")["state"], "caution")
        self.assertEqual(self.card("just-under")["state"], "ok")

    def test_the_worst_disk_counts(self):
        self.report("disks", disk=10, disk_detail=[{"path": "/", "total": 1, "used": 0, "percent": 10},
                                                   {"path": "/data", "total": 1, "used": 1, "percent": 86}])
        self.assertEqual(self.card("disks")["state"], "warn")

    def test_only_cards_and_their_faces_go_out(self):
        self.report("tokyo")
        self.write(kaomoji={"assign": {"tokyo": "爱莉", "retired-box": "绘名"},
                            "faces": {"爱莉": ["a", "b", "c"], "绘名": ["d", "e", "f"], "secret-role": ["g", "h", "i"]}})
        body = self.public().get_json()
        self.assertEqual(set(body), {"panels", "kaomoji", "pending"})
        self.assertEqual(body["kaomoji"], {"assign": {"tokyo": "爱莉"}, "faces": {"爱莉": ["a", "b", "c"]}})
        self.assertEqual(self.client.get("/api/kaomoji").status_code, 401)

    def pull_mode_with_remote_cache(self, age):
        self.write(nightcord_status={"enabled": True, "name": "NS", "source": "pull", "url": "http://10.10.0.4:8702"})
        ticket = app._begin_remote_fetch()
        app._store_remote([{"name": "NS", "kind": "nightcord-status", "online": True, "targets": []}], ticket)
        with app._remote_lock:
            app._remote["ts"] = time.time() - age

    def test_anonymous_requests_leave_a_recent_remote_cache_alone(self):
        self.pull_mode_with_remote_cache(age=30)   # older than the dashboard's 10 s, younger than a minute
        for _ in range(3):
            self.public()
        self.assertEqual(self.refreshes, [])
        self.assertEqual(self.card("NS")["state"], "ok")

    def test_a_remote_cache_older_than_a_minute_is_refreshed_once_per_answer(self):
        # Multi-worker deployments: the alert worker's fetch lives in its own process, so a web worker
        # that only serves anonymous visitors has to fetch for itself.
        self.pull_mode_with_remote_cache(age=61)
        self.client.get("/api/public/status")
        self.client.get("/api/public/status")   # within the five seconds: the cached answer, no new fetch
        self.assertEqual(self.refreshes, [1])

    def test_no_remote_data_yet_says_pending_instead_of_no_machines(self):
        self.write(nightcord_status={"enabled": True, "source": "pull", "url": "http://10.10.0.4:8702"})
        body = self.public().get_json()
        self.assertEqual(body["panels"], [])
        self.assertTrue(body["pending"])

    def test_signed_out_status_still_refuses(self):
        self.client.get("/api/status")
        self.assertEqual(self.refreshes, [])

    def test_answers_are_reused_for_a_few_seconds(self):
        self.report("tokyo")
        self.public()
        with mock.patch.object(app, "_qingyuan_cards", side_effect=AssertionError("database read")):
            self.assertEqual(self.client.get("/api/public/status").status_code, 200)

    def test_nightcord_status_without_pushes_reads_as_unreachable(self):
        self.assertEqual(self.card("Nightcord-Status"),
                         {"name": "Nightcord-Status", "kind": "nightcord-status", "online": False, "state": "warn",
                          "nodata": False})

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
