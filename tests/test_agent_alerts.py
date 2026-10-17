import unittest

from notifier import ALL_OFFLINE_SERVER, Notifier, merged_offline_key, plan_agent_alerts

NOW = 10_000
STALE = 180


def state(panel, age, cpu=10.0, generation=0):
    return {"panel": panel, "seen_at": NOW - age, "generation": generation,
            "sample": {"cpu": cpu, "mem": 10.0, "disk": 10.0},
            "disk_detail": None, "ip_internal": None, "ip_external": None}


def no_active(_server):
    return []


class PlanAgentAlertsTests(unittest.TestCase):
    def plan(self, states, active_keys_for=no_active):
        alerts, keep, _drop = plan_agent_alerts(states, {"cpu": 90}, NOW, STALE, active_keys_for)
        return alerts, keep

    def test_three_missed_reports_is_offline(self):
        alerts, _ = self.plan([state("tokyo", 181), state("shanghai", 30)])
        self.assertEqual([a["key"] for a in alerts], ["tokyo-agent-offline"])

    def test_within_threshold_is_online(self):
        alerts, _ = self.plan([state("tokyo", 180), state("shanghai", 30)])
        self.assertEqual(alerts, [])

    def test_all_offline_collapses_into_one_alert(self):
        alerts, _ = self.plan([state("tokyo", 600), state("shanghai", 600), state("FreePBX", 600)])
        self.assertEqual([a["key"] for a in alerts], [merged_offline_key(["FreePBX", "shanghai", "tokyo"])])

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
        # watched：模拟持久化名单 {panel: 代次}（别的进程移除机器时只改它，不调用本对象的 forget）
        self.watched = watched
        self.sent = []
        self._next_generation = {}
        super().__init__({"cooldown_seconds": 0},
                         watch_generations=None if watched is None else self._lookup)

    def _lookup(self, panels):
        if not all(p in self.watched for p in panels):
            return None
        return {p: self.watched[p] for p in panels}

    def retire(self, panel):
        self._next_generation[panel] = self.watched.pop(panel) + 1

    def come_back(self, panel):
        self.watched[panel] = self._next_generation[panel]

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

    def test_cooldown_suppressed_alert_has_no_recovery(self):
        n = RecordingNotifier()
        n.cooldown = 600
        n.notify(alert("tokyo-agent-offline"))
        n.resolve(set())
        n.notify(alert("tokyo-agent-offline"))  # 冷却期内复发，不发
        n.resolve(set())
        self.assertEqual(n.sent, [("机器离线告警", "red"), ("机器离线告警已恢复", "green")])

    def test_retired_elsewhere_before_recovery_is_silent(self):
        n = RecordingNotifier(watched={"tokyo": 0, "shanghai": 0})
        n.notify(alert("tokyo-agent-cpu", alert_type="CPU高占用告警"))
        n.retire("tokyo")               # 另一个进程移除了 tokyo，没调用本对象的 forget()
        n.resolve(set())                # 本轮 CPU 正常 / 机器不在名单
        self.assertEqual(n.sent, [("CPU高占用告警", "red")])

    def test_retired_elsewhere_before_notify_is_not_sent(self):
        n = RecordingNotifier(watched={"shanghai": 0})
        n.notify(alert("tokyo-agent-offline"))
        self.assertEqual(n.sent, [])
        self.assertFalse(n.is_active("tokyo-agent-offline"))

    def test_bt_alerts_without_panels_ignore_watch_list(self):
        n = RecordingNotifier(watched={})
        n.notify({"key": "上海-offline", "server": "上海", "alert_type": "面板离线告警", "content": "x"})
        n.resolve(set())
        self.assertEqual(n.sent, [("面板离线告警", "red"), ("面板离线告警已恢复", "green")])

    def test_alert_from_before_retire_does_not_recover_after_comeback(self):
        n = RecordingNotifier(watched={"a": 0})
        n.notify(alert("a-agent-cpu", server="a", alert_type="CPU高占用告警"))
        n.retire("a")
        n.come_back("a")               # 又上报了，CPU 仍然很高，但这轮巡检没看到它
        n.resolve(set())
        self.assertEqual(n.sent, [("CPU高占用告警", "red")])

    def run_rounds(self, n, rounds):
        """rounds: 每轮的 states；照 app 巡检的顺序调用 plan / forget_keys / notify / resolve。"""
        for states in rounds:
            alerts, keep, drop = plan_agent_alerts(states, {"cpu": 90}, NOW, STALE, n.active_keys_for, n.active_merged)
            n.forget_keys(drop)
            for a in alerts:
                n.notify(a)
            n.resolve({a["key"] for a in alerts} | keep)

    def test_merged_alert_with_fewer_members_is_a_new_alert(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0, "c": 0})
        n.cooldown = 600
        abc = [state("a", 600), state("b", 600), state("c", 600)]
        self.run_rounds(n, [abc])
        n.retire("a")
        self.run_rounds(n, [abc[1:]])
        self.assertEqual(n.sent, [("全部机器同时失联", "red"), ("全部机器同时失联", "red")])
        self.assertEqual([k for k, _ in n.active_merged()], [merged_offline_key(["b", "c"])])

    def test_merged_alert_gaining_a_member_is_superseded_not_recovered(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0, "c": 0})
        self.run_rounds(n, [[state("a", 600), state("b", 600)],
                            [state("a", 600), state("b", 600), state("c", 600)]])
        self.assertEqual(n.sent, [("全部机器同时失联", "red"), ("全部机器同时失联", "red")])

    def test_merged_alert_recovers_when_a_member_returns(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0})
        self.run_rounds(n, [[state("a", 600), state("b", 600)],
                            [state("a", 10), state("b", 600)]])
        self.assertEqual(n.sent, [("全部机器同时失联", "red"), ("机器离线告警", "red"), ("全部机器同时失联已恢复", "green")])

    def test_new_generation_alert_is_not_swallowed_by_old_one(self):
        n = RecordingNotifier(watched={"a": 0})
        n.cooldown = 600
        self.run_rounds(n, [[state("a", 10, cpu=95)]])
        n.retire("a"); n.come_back("a")          # 另一个进程移除，随后又上报回来，CPU 仍高
        self.run_rounds(n, [[state("a", 10, cpu=95, generation=1)]])
        self.assertEqual(n.sent, [("CPU高占用告警", "red"), ("CPU高占用告警", "red")])

    def test_plan_from_before_retire_is_dropped(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0})
        alerts, _, _ = plan_agent_alerts([state("a", 600), state("b", 10)], {}, NOW, STALE, n.active_keys_for, n.active_merged)
        n.retire("a"); n.come_back("a")          # 计划算完后 a 被移除又回来
        for a in alerts:
            n.notify(a)
        self.run_rounds(n, [[state("a", 10, generation=1), state("b", 10)]])
        self.assertEqual(n.sent, [])

    def test_machine_named_like_merged_alert_does_not_break_round(self):
        n = RecordingNotifier(watched={ALL_OFFLINE_SERVER: 0, "b": 0})
        rounds = [[state(ALL_OFFLINE_SERVER, 10, cpu=95), state("b", 10)]] * 2 + [[state(ALL_OFFLINE_SERVER, 10), state("b", 10)]]
        self.run_rounds(n, rounds)
        self.assertEqual(n.sent, [("CPU高占用告警", "red"), ("CPU高占用告警已恢复", "green")])

    def test_old_generation_cooldown_does_not_suppress_new_generation(self):
        n = RecordingNotifier(watched={"a": 0})
        n.cooldown = 600
        self.run_rounds(n, [[state("a", 10, cpu=95)]])
        n.retire("a")
        self.run_rounds(n, [[]])                 # 移除后巡检一轮：旧告警静默摘掉，冷却记录还在
        n.come_back("a")
        self.run_rounds(n, [[state("a", 10, cpu=95, generation=1)]])
        self.assertEqual(n.sent, [("CPU高占用告警", "red"), ("CPU高占用告警", "red")])

    def test_merged_keys_do_not_collide(self):
        self.assertNotEqual(merged_offline_key(["a", "b|c"]), merged_offline_key(["a|b", "c"]))

    def test_planned_alerts_name_their_panels(self):
        alerts, _, _ = plan_agent_alerts([state("tokyo", 600), state("shanghai", 600)], {}, NOW, STALE, no_active)
        self.assertEqual(alerts[0]["panels"], ["tokyo", "shanghai"])


if __name__ == "__main__":
    unittest.main()
