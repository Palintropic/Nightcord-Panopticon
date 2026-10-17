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
        self.assertEqual([a["key"] for a in alerts], [f"{ALL_OFFLINE_KEY}:FreePBX|shanghai|tokyo"])

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
    def __init__(self, watched=None):
        # watched：模拟持久化名单（别的进程移除机器时只改它，不调用本对象的 forget）
        self.watched = watched
        super().__init__({"cooldown_seconds": 0},
                         is_watched=None if watched is None else (lambda panels: all(p in self.watched for p in panels)))
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

    def test_retired_elsewhere_before_recovery_is_silent(self):
        n = RecordingNotifier(watched={"tokyo", "shanghai"})
        n.notify(alert("tokyo-agent-cpu", alert_type="CPU高占用告警"))
        n.watched.discard("tokyo")      # 另一个进程移除了 tokyo，没调用本对象的 forget()
        n.resolve(set())                # 本轮 CPU 正常 / 机器不在名单
        self.assertEqual(n.sent, [("CPU高占用告警", "red")])

    def test_retired_elsewhere_before_notify_is_not_sent(self):
        n = RecordingNotifier(watched={"shanghai"})
        n.notify(alert("tokyo-agent-offline"))
        self.assertEqual(n.sent, [])
        self.assertFalse(n.is_active("tokyo-agent-offline"))

    def test_bt_alerts_without_panels_ignore_watch_list(self):
        n = RecordingNotifier(watched=set())
        n.notify({"key": "上海-offline", "server": "上海", "alert_type": "面板离线告警", "content": "x"})
        n.resolve(set())
        self.assertEqual(n.sent, [("面板离线告警", "red"), ("面板离线告警已恢复", "green")])

    def test_merged_alert_with_new_members_is_a_new_alert(self):
        n = RecordingNotifier(watched={"a", "b", "c"})
        n.cooldown = 600
        first, _ = plan_agent_alerts([state("a", 600), state("b", 600), state("c", 600)], {}, NOW, STALE, no_active)
        n.notify(first[0])
        n.watched.discard("a")
        second, _ = plan_agent_alerts([state("b", 600), state("c", 600)], {}, NOW, STALE, no_active)
        n.notify(second[0])
        n.resolve({second[0]["key"]})
        self.assertEqual(n.sent, [("全部机器同时失联", "red"), ("全部机器同时失联", "red")])
        self.assertTrue(n.is_active(second[0]["key"]))

    def test_planned_alerts_name_their_panels(self):
        alerts, _ = plan_agent_alerts([state("tokyo", 600), state("shanghai", 600)], {}, NOW, STALE, no_active)
        self.assertEqual(alerts[0]["panels"], ["tokyo", "shanghai"])


if __name__ == "__main__":
    unittest.main()
