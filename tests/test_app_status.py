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


class StatusPushTests(AppTestCase):
    SECRET = "s3cret"

    def setUp(self):
        super().setUp()
        self.write_config({
            "metrics_agent": {"enabled": True, "shared_secret": self.SECRET},
            "nightcord_status": {"enabled": True, "name": "Nightcord-Status", "source": "push"},
        })

    def push(self, targets, secret=SECRET):
        return self.client.post(
            app.STATUS_REPORT_PATH,
            json={"last_updated": "2026-10-19T00:00:00+08:00", "targets": targets},
            headers={"X-Metrics-Secret": secret},
        )

    def card(self):
        app._refresh_remote_async = lambda: None
        body = self.client.get("/api/status").get_json()
        return next(p for p in body["panels"] if p["kind"] == "nightcord-status"), body

    def test_wrong_secret_is_rejected(self):
        self.assertEqual(self.push([], secret="nope").status_code, 401)
        self.assertIsNone(metrics_store.get_status_push(app.STATUS_PUSH_SOURCE))

    def test_disabled_when_not_in_push_mode(self):
        self.write_config({"metrics_agent": {"shared_secret": self.SECRET},
                           "nightcord_status": {"enabled": True, "url": "http://10.10.0.4:8702"}})
        self.assertEqual(self.push([]).status_code, 404)

    def test_pushed_targets_show_on_the_card_without_a_remote_fetch(self):
        resp = self.push([
            {"id": "esxi-1", "display_name": "Nightcord-ESXi-1", "type": "esxi", "status": "down",
             "last_error": "timed out", "detail": {"host": "192.168.3.230"}},
            {"id": "x", "status": "weird", "detail": "not a dict"},
            "garbage",
        ])
        self.assertEqual(resp.get_json(), {"ok": True, "targets": 2})
        card, body = self.card()
        self.assertTrue(card["online"])
        self.assertFalse(body["remote_pending"])
        self.assertEqual(card["targets"][0]["status"], "down")
        self.assertEqual(card["targets"][1], {"id": "x", "display_name": "x", "type": "",
                                              "status": "unknown", "last_error": None, "detail": {}})

    def test_card_goes_offline_when_pushes_stop(self):
        metrics_store.save_status_push(app.STATUS_PUSH_SOURCE, 1000, {"targets": []})
        card = app._nightcord_push_card(app.load_config(), 1000 + app.STATUS_PUSH_STALE_SECONDS + 1)
        self.assertFalse(card["online"])
        self.assertIn("没有推送", card["error"])

    def test_card_offline_before_first_push(self):
        card, _ = self.card()
        self.assertFalse(card["online"])


class StatusPushAlertRoundTests(StatusPushTests):
    """End-to-end through _alert_round: what actually reaches the webhook."""

    def setUp(self):
        super().setUp()
        from test_agent_alerts import RecordingNotifier

        cfg = app.load_config()
        cfg["notifications"] = {"cooldown_seconds": 0}
        self.write_config(cfg)
        self.notifier = RecordingNotifier()
        self.old_notifier, app._notifier = app._notifier, self.notifier

    def tearDown(self):
        app._notifier = self.old_notifier
        super().tearDown()

    def esxi(self, status):
        return [{"id": "esxi-1", "display_name": "ESXi-1", "type": "esxi", "status": status,
                 "last_error": "timed out"}]

    def test_never_pushed_stays_quiet(self):
        app._alert_round()
        self.assertEqual(self.notifier.sent, [])

    def test_down_then_up_sends_alert_and_recovery(self):
        self.push(self.esxi("down"))
        app._alert_round()
        self.push(self.esxi("ok"))
        app._alert_round()
        self.assertEqual(self.notifier.sent, [("2702 检测故障告警", "red"), ("2702 检测故障告警已恢复", "green")])

    def test_simulated_fault_alerts_and_recovers_when_it_leaves_the_list(self):
        self.push([{"id": "simulated-fault", "display_name": "Nightcord-VoIP 模拟故障",
                    "type": "simulated", "status": "down", "last_error": "simulation"}])
        app._alert_round()
        self.push([])
        app._alert_round()
        self.assertEqual(self.notifier.sent, [("2702 检测故障告警", "red"), ("2702 检测故障告警已恢复", "green")])

    def test_silent_2702_does_not_fake_a_recovery(self):
        self.push(self.esxi("down"))
        app._alert_round()
        received_at, body = metrics_store.get_status_push(app.STATUS_PUSH_SOURCE)
        metrics_store.save_status_push(app.STATUS_PUSH_SOURCE, received_at - app.STATUS_PUSH_STALE_SECONDS - 5, body)
        app._alert_round()
        self.assertEqual(self.notifier.sent, [("2702 检测故障告警", "red"), ("2702 失联告警", "red")])
        # 2702 comes back with ESXi still down: only the silence recovers.
        self.push(self.esxi("down"))
        app._alert_round()
        self.assertEqual(self.notifier.sent[-1], ("2702 失联告警已恢复", "green"))
        self.assertEqual(len(self.notifier.sent), 3)


if __name__ == "__main__":
    unittest.main()
