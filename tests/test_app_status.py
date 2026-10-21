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


PULL = {"nightcord_status": {"enabled": True, "source": "pull", "url": "http://x"}}
PULL_OTHER = {"nightcord_status": {"enabled": True, "source": "pull", "url": "http://y"}}
OFF = {"nightcord_status": {"enabled": False}}


class RemoteCacheTests(AppTestCase):
    def test_cache_from_an_older_config_is_ignored_even_if_another_process_changed_it(self):
        self.write_config(PULL)
        ticket = app._begin_remote_fetch()
        app._store_remote([{"name": "ns-old", "url": "http://x", "online": True, "kind": "nightcord-status"}], ticket)
        # Another worker saves the config; this process never hears about it.
        self.write_config(OFF)
        app._refresh_remote_async = lambda: None
        body = self.client.get("/api/status").get_json()
        self.assertEqual([p["name"] for p in body["panels"]], [])
        self.assertFalse(body["remote_pending"])

    def test_fetch_uses_one_config_snapshot_for_fingerprint_and_sources(self):
        # ena: config A → B (source added) → back to A while a fetch runs. The result fetched under B
        # must not land with A's fingerprint.
        self.write_config(OFF)
        ticket = app._begin_remote_fetch()
        self.write_config(PULL)
        result = app.fetch_remote(ticket["cfg"])
        self.write_config(OFF)
        app._store_remote(result, ticket)
        app._refresh_remote_async = lambda: None
        body = self.client.get("/api/status").get_json()
        self.assertEqual([p["name"] for p in body["panels"]], [])

    def test_an_earlier_fetch_cannot_overwrite_a_later_one(self):
        self.write_config(PULL)
        first = app._begin_remote_fetch()
        second = app._begin_remote_fetch()
        app._store_remote([{"name": "ns", "online": True}], second)
        app._store_remote([{"name": "ns", "online": False}], first)
        self.assertTrue(app._remote["data"][0]["online"])

    def test_status_answers_without_waiting_for_remote_sources(self):
        self.write_config(PULL)
        started = []
        app._refresh_remote_async = lambda: started.append(True)
        body = self.client.get("/api/status").get_json()
        self.assertTrue(body["remote_pending"])
        self.assertEqual(started, [True])

    def test_changing_the_status_url_invalidates_the_cache(self):
        self.write_config(PULL)
        ticket = app._begin_remote_fetch()
        app._store_remote([{"name": "ns", "url": "http://x", "online": True, "kind": "nightcord-status"}], ticket)
        self.write_config(PULL_OTHER)
        app._refresh_remote_async = lambda: None
        self.assertTrue(self.client.get("/api/status").get_json()["remote_pending"])


class LeftoverPanelsTests(AppTestCase):
    """Old configs still carry a `panels:` list of BT panels; it is ignored, and dropped on save."""

    OLD = {"panels": [{"name": "上海", "url": "http://10.10.0.1:18101", "api_key": "k"}]}

    def test_old_panels_produce_no_cards_and_no_fetch(self):
        self.write_config(self.OLD)
        started = []
        app._refresh_remote_async = lambda: started.append(True)
        body = self.client.get("/api/status").get_json()
        self.assertEqual(body["panels"], [])
        self.assertFalse(body["remote_pending"])
        self.assertEqual(app.fetch_remote(app.load_config()), [])

    def test_saving_settings_drops_old_panels(self):
        from config_editor import merge_submitted, redact_for_display
        self.assertNotIn("panels", redact_for_display(self.OLD))
        merged, errors = merge_submitted(self.OLD, {"panels": [{"name": "x", "url": "http://x"}]})
        self.assertEqual(errors, [])
        self.assertNotIn("panels", merged)


class ProxyLoginTests(AppTestCase):
    """Through a reverse proxy on another machine, lockout must follow the real client, not the proxy."""

    def setUp(self):
        super().setUp()
        app._brute_force_guard._fails.clear()
        app._brute_force_guard._lockouts.clear()
        from werkzeug.security import generate_password_hash
        self.write_config({"dashboard_auth": {"enabled": True, "username": "mizuki", "max_attempts": 2,
                                              "password_hash": generate_password_hash("right"),
                                              "trusted_proxies": ["10.10.0.3"]}})

    def login(self, client_ip, password):
        return self.client.post("/login", json={"username": "mizuki", "password": password},
                                environ_base={"REMOTE_ADDR": "10.10.0.3"},
                                headers={"X-Forwarded-For": client_ip, "X-Forwarded-Proto": "https"})

    def test_one_client_locking_itself_out_does_not_lock_others(self):
        for _ in range(2):
            self.login("203.0.113.9", "wrong")
        self.assertEqual(self.login("203.0.113.9", "right").status_code, 429)
        self.assertEqual(self.login("198.51.100.20", "right").status_code, 200)

    def test_direct_client_cannot_pick_its_lockout_key(self):
        for i in range(2):
            self.client.post("/login", json={"username": "mizuki", "password": "wrong"},
                             environ_base={"REMOTE_ADDR": "198.51.100.77"},
                             headers={"X-Forwarded-For": f"192.0.2.{i}"})
        res = self.client.post("/login", json={"username": "mizuki", "password": "right"},
                               environ_base={"REMOTE_ADDR": "198.51.100.77"},
                               headers={"X-Forwarded-For": "192.0.2.200"})
        self.assertEqual(res.status_code, 429)

    def test_broken_config_falls_back_to_loopback_in_the_middleware(self):
        # The middleware runs before every route; a YAML error there must not turn into a 500 by itself.
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write("dashboard_auth: [unclosed\n")
        self.assertEqual(app._current_trusted_proxies(), {"127.0.0.1", "::1"})

    def test_saving_settings_keeps_an_empty_trusted_proxies(self):
        from config_editor import merge_submitted
        merged, _ = merge_submitted({"dashboard_auth": {"trusted_proxies": []}}, {"dashboard_auth": {}})
        self.assertIn("trusted_proxies", merged["dashboard_auth"])

    def test_saving_settings_keeps_trusted_proxies(self):
        from config_editor import merge_submitted
        merged, errors = merge_submitted(app.load_config(), {"dashboard_auth": {"enabled": True}})
        self.assertEqual(errors, [])
        self.assertEqual(merged["dashboard_auth"]["trusted_proxies"], ["10.10.0.3"])


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
