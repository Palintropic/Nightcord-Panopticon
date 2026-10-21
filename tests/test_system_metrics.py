import os
import sqlite3
import sys
import tempfile
import time
import unittest

import metrics_store

# Point the store at a temporary database before app.py imports it (app calls init_db at import).
_TMP = tempfile.TemporaryDirectory()
metrics_store.DB_PATH = os.path.join(_TMP.name, "metrics.db")

import yaml  # noqa: E402

import app  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_init_adds_new_columns_to_an_old_metrics_table(self):
        with sqlite3.connect(metrics_store.DB_PATH) as conn:
            conn.execute("""CREATE TABLE metrics (panel TEXT NOT NULL, ts INTEGER NOT NULL, cpu REAL, mem REAL,
                            disk REAL, net_in_kbps REAL, net_out_kbps REAL, PRIMARY KEY (panel, ts))""")
            conn.execute("INSERT INTO metrics VALUES ('old', 100, 1, 2, 3, 0, 0)")
        metrics_store.init_db()
        latest = metrics_store.get_latest("old")
        self.assertEqual(latest["cpu"], 1)
        self.assertIsNone(latest["swap"])
        self.assertIsNone(latest["load1"])

    def test_sample_without_extra_fields_stores_nulls(self):
        metrics_store.init_db()
        metrics_store.insert_sample("esxi", 100, 1, 2, 3, 0, 0)
        metrics_store.insert_sample("agent", 100, 1, 2, 3, 0, 0, {"swap": 5.0, "load1": 0.5, "procs": 90})
        self.assertIsNone(metrics_store.get_latest("esxi")["procs"])
        latest = metrics_store.get_latest("agent")
        self.assertEqual((latest["swap"], latest["load1"], latest["procs"]), (5.0, 0.5, 90))
        self.assertIsNone(latest["tcp_established"])
        self.assertEqual(metrics_store.query_history("agent", 0)[0]["load1"], 0.5)

    def test_update_system_returns_the_previous_boot_time_and_keeps_missing_values(self):
        metrics_store.init_db()
        self.assertIsNone(metrics_store.update_system("a", 1000, 4, 0))
        self.assertEqual(metrics_store.update_system("a", None, None, None), 1000)
        self.assertEqual(metrics_store.get_system("a"), {"boot_time": 1000, "cores": 4, "swap_total": 0})
        self.assertEqual(metrics_store.update_system("a", 5000, None, None), 1000)
        self.assertEqual(metrics_store.get_system("a")["boot_time"], 5000)
        self.assertEqual(metrics_store.get_system("nobody"), {"boot_time": None, "cores": None, "swap_total": None})


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_config = app.CONFIG_PATH
        app.CONFIG_PATH = os.path.join(self.tmp.name, "config.yaml")
        metrics_store.DB_PATH = os.path.join(self.tmp.name, "metrics.db")
        metrics_store.init_db()
        with open(app.CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump({"metrics_agent": {"enabled": True, "shared_secret": "s3cret"}}, f)
        self.client = app.app.test_client()
        self.reboots = []
        self.old_notify = app._notify_reboot
        app._notify_reboot = lambda panel, prev, boot, *_: self.reboots.append((panel, prev, boot))
        self.old_refresh = app._refresh_remote_async
        app._refresh_remote_async = lambda: None

    def tearDown(self):
        app._notify_reboot = self.old_notify
        app._refresh_remote_async = self.old_refresh
        app.CONFIG_PATH = self.old_config
        self.tmp.cleanup()

    def report(self, **extra):
        body = {"panel": "tokyo", "ts": int(time.time()), "cpu": 1, "mem": 2, "disk": 3,
                "net_in_kbps": 0, "net_out_kbps": 0, **extra}
        return self.client.post("/api/metrics/report", json=body, headers={"X-Metrics-Secret": "s3cret"})

    def qingyuan(self):
        panels = self.client.get("/api/status").get_json()["panels"]
        return next(p for p in panels if p["name"] == "tokyo")["qingyuan"]

    def test_old_agent_report_still_works(self):
        self.assertEqual(self.report().status_code, 200)
        q = self.qingyuan()
        self.assertIsNone(q["swap"])
        self.assertIsNone(q["boot_time"])

    def test_new_fields_reach_the_status_api(self):
        boot = int(time.time()) - 3600
        self.report(swap=12.5, swap_total=1024, load1=0.42, load5=0.3, load15=0.2, cores=2,
                    procs=150, tcp_established=9, boot_time=boot)
        q = self.qingyuan()
        self.assertEqual((q["swap"], q["swap_total"], q["load1"], q["cores"]), (12.5, 1024, 0.42, 2))
        self.assertEqual((q["procs"], q["tcp_established"], q["boot_time"]), (150, 9, boot))

    def test_junk_values_are_stored_as_null_without_failing_the_report(self):
        resp = self.report(swap=250, load1="lots", procs=-3, tcp_established=True, cores=0,
                           boot_time=int(time.time()) + 10 * 86400, swap_total=float("nan"))
        self.assertEqual(resp.status_code, 200)
        q = self.qingyuan()
        for key in ("swap", "load1", "procs", "tcp_established", "cores", "boot_time", "swap_total"):
            self.assertIsNone(q[key], key)

    def test_first_report_is_not_a_reboot(self):
        self.report(boot_time=int(time.time()) - 600)
        self.assertEqual(self.reboots, [])

    def test_boot_time_moving_forward_is_a_reboot(self):
        old_boot = int(time.time()) - 86400
        new_boot = int(time.time()) - 60
        self.report(boot_time=old_boot)
        self.report(boot_time=old_boot)
        self.report(boot_time=new_boot)
        self.report(boot_time=new_boot)
        self.assertEqual(self.reboots, [("tokyo", old_boot, new_boot)])

    def test_clock_jitter_is_not_a_reboot(self):
        boot = int(time.time()) - 86400
        self.report(boot_time=boot)
        self.report(boot_time=boot + 3)
        self.report(boot_time=boot - 5)
        self.assertEqual(self.reboots, [])

    def test_a_report_without_boot_time_does_not_reset_the_baseline(self):
        boot = int(time.time()) - 86400
        self.report(boot_time=boot)
        self.report()
        self.report(boot_time=boot)
        self.assertEqual(self.reboots, [])


def _import_agent():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
    try:
        import metrics_agent
    finally:
        sys.path.pop(0)
    return metrics_agent


class AgentCollectTests(unittest.TestCase):
    def test_established_connections_are_counted_from_proc(self):
        agent = _import_agent()
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        row = "   0: 3600007F:0035 00000000:0000 {} 00000000:00000000 00:00000000 00000000   992        0 63845 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            tcp, tcp6 = os.path.join(tmp, "tcp"), os.path.join(tmp, "tcp6")
            with open(tcp, "w") as f:
                f.write(header + row.format("0A") + row.format("01") + row.format("06") + row.format("01"))
            with open(tcp6, "w") as f:
                f.write(header + row.format("01"))
            old = agent._PROC_TCP_TABLES
            agent._PROC_TCP_TABLES = (tcp, tcp6)
            try:
                self.assertEqual(agent.count_established_tcp(), 3)
                agent._PROC_TCP_TABLES = (tcp, os.path.join(tmp, "missing"))  # no IPv6
                self.assertEqual(agent.count_established_tcp(), 2)
            finally:
                agent._PROC_TCP_TABLES = old

    def test_collect_system_reports_the_new_fields(self):
        out = _import_agent().collect_system()
        for key in ("swap_total", "load1", "load5", "load15", "cores", "procs", "boot_time"):
            self.assertIn(key, out)
        self.assertIn("tcp_established", out)  # None is allowed when the OS refuses to list sockets
        self.assertLessEqual(out["boot_time"], time.time())
        if out["swap_total"] == 0:
            self.assertIsNone(out["swap"])


if __name__ == "__main__":
    unittest.main()
