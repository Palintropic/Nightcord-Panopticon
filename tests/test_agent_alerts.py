import unittest

from notifier import ALL_OFFLINE_KEY, Notifier, plan_agent_alerts

NOW = 10_000
STALE = 180


def state(panel, age, cpu=10.0):
    return {"panel": panel, "seen_at": NOW - age, "sample": {"cpu": cpu, "mem": 10.0, "disk": 10.0},
            "disk_detail": None, "ip_internal": None, "ip_external": None}


def no_active(_server):
    return []


class PlanAgentAlertsTests(unittest.TestCase):
    def plan(self, states, active_keys_for=no_active):
        return plan_agent_alerts(states, {"cpu": 90}, NOW, STALE, active_keys_for)

    def test_three_missed_reports_is_offline(self):
        alerts, _ = self.plan([state("tokyo", 181), state("shanghai", 30)])
        self.assertEqual([a["key"] for a in alerts], ["tokyo-agent-offline"])

    def test_within_threshold_is_online(self):
        alerts, _ = self.plan([state("tokyo", 180), state("shanghai", 30)])
        self.assertEqual(alerts, [])

    def test_all_offline_collapses_into_one_alert(self):
        alerts, _ = self.plan([state("tokyo", 600), state("shanghai", 600), state("FreePBX", 600)])
        self.assertEqual([a["key"] for a in alerts], [ALL_OFFLINE_KEY])

    def test_single_machine_offline_is_not_treated_as_all_offline(self):
        alerts, _ = self.plan([state("tokyo", 600)])
        self.assertEqual([a["key"] for a in alerts], ["tokyo-agent-offline"])

    def test_offline_machine_keeps_its_open_alerts(self):
        active = {"tokyo": ["tokyo-agent-cpu", "tokyo-agent-offline"]}
        _, keep = self.plan([state("tokyo", 600), state("shanghai", 30)], lambda s: active.get(s, []))
        self.assertEqual(keep, {"tokyo-agent-cpu", "tokyo-agent-offline"})

    def test_online_machine_still_gets_threshold_alerts(self):
        alerts, _ = self.plan([state("tokyo", 30, cpu=95.0)])
        self.assertEqual([a["key"] for a in alerts], ["tokyo-agent-cpu"])


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__({"cooldown_seconds": 0})
        self.sent = []

    def _send_feishu(self, alert, template="red", mention_all=True):
        self.sent.append((alert["alert_type"], template))

    def _send_email(self, alert):
        pass


def alert(key, server="tokyo", alert_type="机器离线告警", panels=None):
    return {"key": key, "server": server, "panels": panels if panels is not None else [server],
            "alert_type": alert_type, "content": "x", "ip_external": None, "ip_internal": None}


class RecoveryTests(unittest.TestCase):
    def test_resolved_alert_sends_recovery(self):
        n = RecordingNotifier()
        n.notify(alert("tokyo-agent-offline"))
        n.resolve(set())
        self.assertEqual(n.sent, [("机器离线告警", "red"), ("机器离线告警已恢复", "green")])

    def test_still_active_alert_is_not_recovered(self):
        n = RecordingNotifier()
        n.notify(alert("tokyo-agent-offline"))
        n.resolve({"tokyo-agent-offline"})
        self.assertEqual(len(n.sent), 1)

    def test_forget_drops_alerts_without_recovery(self):
        n = RecordingNotifier()
        n.notify(alert("tokyo-agent-offline"))
        n.notify(alert("shanghai-agent-cpu", server="shanghai", alert_type="CPU高占用告警"))
        n.forget("tokyo")
        n.resolve({"shanghai-agent-cpu"})
        self.assertEqual(len(n.sent), 2)
        self.assertEqual(n.active_keys_for("shanghai"), ["shanghai-agent-cpu"])

    def test_cooldown_suppressed_alert_has_no_recovery(self):
        n = RecordingNotifier()
        n.cooldown = 600
        n.notify(alert("tokyo-agent-offline"))
        n.resolve(set())
        n.notify(alert("tokyo-agent-offline"))  # 冷却期内复发，不发
        n.resolve(set())
        self.assertEqual(n.sent, [("机器离线告警", "red"), ("机器离线告警已恢复", "green")])

    def test_forget_drops_merged_alert_when_a_member_is_retired(self):
        n = RecordingNotifier()
        n.notify(alert(ALL_OFFLINE_KEY, server="全部青源机器", alert_type="全部机器同时失联",
                       panels=["tokyo", "shanghai"]))
        n.forget("tokyo")
        n.forget("shanghai")
        n.resolve(set())
        self.assertEqual(n.sent, [("全部机器同时失联", "red")])

    def test_forget_retired_uses_persisted_watch_list(self):
        # 模拟移除请求跑在别的进程：这个 Notifier 没被调用 forget()，只靠巡检时查到的名单
        n = RecordingNotifier()
        n.notify(alert("tokyo-agent-offline"))
        n.notify(alert("shanghai-agent-cpu", server="shanghai", alert_type="CPU高占用告警"))
        watched = {"shanghai"}
        n.forget_retired(lambda panels: all(p in watched for p in panels))
        n.resolve({"shanghai-agent-cpu"})
        self.assertEqual(len(n.sent), 2)

    def test_bt_alerts_without_panels_are_untouched_by_forget_retired(self):
        n = RecordingNotifier()
        n.notify({"key": "上海-offline", "server": "上海", "alert_type": "面板离线告警", "content": "x"})
        n.forget_retired(lambda panels: False)
        self.assertTrue(n.is_active("上海-offline"))

    def test_planned_alerts_name_their_panels(self):
        alerts, _ = plan_agent_alerts([state("tokyo", 600), state("shanghai", 600)], {}, NOW, STALE, no_active)
        self.assertEqual(alerts[0]["panels"], ["tokyo", "shanghai"])


if __name__ == "__main__":
    unittest.main()
