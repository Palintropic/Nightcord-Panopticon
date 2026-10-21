import unittest

from notifier import ALL_OFFLINE_KEY, ALL_OFFLINE_SERVER, Notifier, alert_key, evaluate_status_alerts, plan_agent_alerts

NOW = 10_000
STALE = 180


def state(panel, age, cpu=10.0, generation=0):
    return {"panel": panel, "seen_at": NOW - age, "generation": generation,
            "sample": {"cpu": cpu, "mem": 10.0, "disk": 10.0},
            "disk_detail": None, "ip_internal": None, "ip_external": None}


def qk(panel, suffix):
    return alert_key("qingyuan", panel, suffix)


def no_active(_server):
    return []


class PlanAgentAlertsTests(unittest.TestCase):
    def plan(self, states, active_keys_for=no_active):
        return plan_agent_alerts(states, {"cpu": 90}, NOW, STALE, active_keys_for)

    def test_three_missed_reports_is_offline(self):
        alerts, _ = self.plan([state("tokyo", 181), state("shanghai", 30)])
        self.assertEqual([a["key"] for a in alerts], [qk("tokyo", "agent-offline")])

    def test_within_threshold_is_online(self):
        alerts, _ = self.plan([state("tokyo", 180), state("shanghai", 30)])
        self.assertEqual(alerts, [])

    def test_all_offline_collapses_into_one_alert(self):
        alerts, _ = self.plan([state("tokyo", 600), state("shanghai", 600), state("FreePBX", 600)])
        self.assertEqual([a["key"] for a in alerts], [ALL_OFFLINE_KEY])

    def test_single_machine_offline_is_not_treated_as_all_offline(self):
        alerts, _ = self.plan([state("tokyo", 600)])
        self.assertEqual([a["key"] for a in alerts], [qk("tokyo", "agent-offline")])

    def test_offline_machine_keeps_its_open_alerts(self):
        active = {"tokyo": [qk("tokyo", "agent-cpu"), qk("tokyo", "agent-offline")]}
        _, keep = self.plan([state("tokyo", 600), state("shanghai", 30)], lambda s: active.get(s, []))
        self.assertEqual(keep, {qk("tokyo", "agent-cpu"), qk("tokyo", "agent-offline")})

    def test_online_machine_still_gets_threshold_alerts(self):
        alerts, _ = self.plan([state("tokyo", 30, cpu=95.0)])
        self.assertEqual([a["key"] for a in alerts], [qk("tokyo", "agent-cpu")])


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

    def test_status_alerts_without_panels_ignore_watch_list(self):
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
        """rounds: 每轮的 states；照 app 巡检的顺序调用 plan / notify / resolve。"""
        for states in rounds:
            alerts, keep = plan_agent_alerts(states, {"cpu": 90}, NOW, STALE, n.active_keys_for)
            for a in alerts:
                n.notify(a)
            n.resolve({a["key"] for a in alerts} | keep)

    def test_merged_alert_with_fewer_members_is_replaced(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0, "c": 0})
        n.cooldown = 600
        abc = [state("a", 600), state("b", 600), state("c", 600)]
        self.run_rounds(n, [abc])
        n.retire("a")
        self.run_rounds(n, [abc[1:]])
        self.assertEqual(n.sent, [("全部机器同时失联", "red"), ("全部机器同时失联", "red")])
        self.assertEqual(n._active[ALL_OFFLINE_KEY]["panels"], ["b", "c"])

    def test_status_card_name_cannot_collide_with_merged_key(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0})
        card = {"name": "qingyuan-all", "online": False, "url": "http://x", "error": "down", "kind": "nightcord-status"}
        for _ in range(2):
            status_alerts = evaluate_status_alerts(card)
            alerts, keep = plan_agent_alerts([state("a", 600), state("b", 600)], {}, NOW, STALE, n.active_keys_for)
            for a in status_alerts + alerts:
                n.notify(a)
            n.resolve({a["key"] for a in status_alerts + alerts} | keep)
        self.assertEqual(n.sent, [("面板离线告警", "red"), ("全部机器同时失联", "red")])
        n.resolve({ALL_OFFLINE_KEY})            # 2702 恢复了
        self.assertEqual(n.sent[-1], ("面板离线告警已恢复", "green"))

    def test_same_named_status_card_recovers_while_agent_is_offline(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0})
        ns_down = {"name": "a", "online": False, "url": "http://x", "error": "down", "kind": "nightcord-status"}
        ns_up = {"name": "a", "online": True, "kind": "nightcord-status", "targets": []}
        for card in (ns_down, ns_up):
            status_alerts = evaluate_status_alerts(card)
            alerts, keep = plan_agent_alerts([state("a", 600), state("b", 10)], {}, NOW, STALE, n.active_keys_for)
            for a in status_alerts + alerts:
                n.notify(a)
            n.resolve({a["key"] for a in status_alerts + alerts} | keep)
        self.assertEqual(n.sent, [("面板离线告警", "red"), ("机器离线告警", "red"), ("面板离线告警已恢复", "green")])

    def test_merged_alert_gaining_a_member_is_not_recovered(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0, "c": 0})
        self.run_rounds(n, [[state("a", 600), state("b", 600)],
                            [state("a", 600), state("b", 600), state("c", 600)]])
        self.assertNotIn(("全部机器同时失联已恢复", "green"), n.sent)
        self.assertEqual(n._active[ALL_OFFLINE_KEY]["panels"], ["a", "b", "c"])

    def test_cancelled_replacement_keeps_old_merged_alert(self):
        # ena 第六轮：AB 已报；计划 ABC 后、发送前 C 被移除 → 取消 ABC，AB 仍要能恢复
        n = RecordingNotifier(watched={"a": 0, "b": 0, "c": 0})
        self.run_rounds(n, [[state("a", 600), state("b", 600)]])
        alerts, keep = plan_agent_alerts([state("a", 600), state("b", 600), state("c", 600)], {}, NOW, STALE, n.active_keys_for)
        n.retire("c")
        for a in alerts:
            n.notify(a)
        n.resolve({a["key"] for a in alerts} | keep)
        self.run_rounds(n, [[state("a", 10), state("b", 600)]])
        self.assertEqual(n.sent[-1], ("全部机器同时失联已恢复", "green"))

    def test_merged_alert_survives_add_then_retire_within_cooldown(self):
        n = RecordingNotifier(watched={"a": 0, "b": 0, "c": 0})
        n.cooldown = 600
        ab = [state("a", 600), state("b", 600)]
        self.run_rounds(n, [ab, ab + [state("c", 600)]])
        n.retire("c")
        self.run_rounds(n, [ab, [state("a", 10), state("b", 600)]])
        self.assertEqual(n.sent[-1], ("全部机器同时失联已恢复", "green"))

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
        alerts, _ = plan_agent_alerts([state("a", 600), state("b", 10)], {}, NOW, STALE, n.active_keys_for)
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

    def test_planned_alerts_name_their_panels(self):
        alerts, _ = plan_agent_alerts([state("tokyo", 600), state("shanghai", 600)], {}, NOW, STALE, no_active)
        self.assertEqual(alerts[0]["panels"], ["tokyo", "shanghai"])


class NightcordStatusAlertTests(unittest.TestCase):
    TH = {"cpu": 90, "mem": 90, "disk": 85}

    def card(self, targets, **overrides):
        card = {"name": "Nightcord-Status", "url": None, "online": True, "error": None,
                "kind": "nightcord-status", "targets": targets}
        card.update(overrides)
        return card

    def test_each_down_target_alerts_once(self):
        alerts = evaluate_status_alerts(self.card([
            {"id": "esxi-1", "display_name": "ESXi-1", "type": "esxi", "status": "down",
             "last_error": "timed out"},
            {"id": "claude", "display_name": "Claude", "type": "statuspage", "status": "degraded"},
            {"id": "freepbx-self", "display_name": "FreePBX", "type": "tcp", "status": "ok"},
        ]))
        self.assertEqual([a["key"] for a in alerts], [alert_key("status", "Nightcord-Status", "target-esxi-1")])
        self.assertIn("timed out", alerts[0]["content"])

    def test_qingyuan_and_panopticon_targets_are_left_to_panopticon(self):
        alerts = evaluate_status_alerts(self.card([
            {"id": "cloud-japan", "type": "qingyuan", "status": "down"},
            {"id": "panopticon", "type": "panopticon", "status": "down"},
        ]))
        self.assertEqual(alerts, [])

    def test_silent_2702_is_its_own_alert(self):
        alerts = evaluate_status_alerts(self.card([], online=False, seen_at=1000,
                                           error="2702 超过 3 分钟没有推送"))
        self.assertEqual(alerts[0]["alert_type"], "2702 失联告警")
        self.assertNotIn("None", alerts[0]["content"])

    def test_never_pushed_is_not_an_alert(self):
        card = self.card([], online=False, seen_at=None, error="还没收到过 2702 的推送")
        self.assertEqual(evaluate_status_alerts(card), [])


if __name__ == "__main__":
    unittest.main()
