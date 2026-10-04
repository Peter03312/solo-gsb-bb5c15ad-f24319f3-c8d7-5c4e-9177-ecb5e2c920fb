"""故障隔离核心逻辑的单元测试（仅依赖标准库）。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.planning import (  # noqa: E402
    Node,
    Pipe,
    DrillStep,
    diff_states,
    allocate_delivery,
    evaluate_bypass,
    evaluate_delivery,
    evaluate_plan,
    evaluate_restore,
    evaluate_source_switch,
    freeze_state,
    reachable_sink_ids,
    recommend_plan,
    simulate_drill,
    validate_bypass_request,
    validate_delivery_request,
    validate_reconcile_request,
    reconcile_meters,
    validate_import,
    _undirected_max_flow,
    DRILL_STEP_ISOLATION,
    DRILL_STEP_RESTORE,
    DRILL_STEP_SOURCE_SWITCH,
    PLAN_INVALID,
    NOT_ISOLATABLE,
    EVENT_NOT_FOUND,
    EVENT_ALREADY_RESTORED,
    RESTORE_UNSAFE,
    SOURCE_SWITCH_INVALID,
    SOURCE_SWITCH_UNSAFE,
    DUPLICATE_NODE_ID,
    DUPLICATE_PIPE_ID,
    PIPE_SELF_LOOP,
    PIPE_ENDPOINT_UNKNOWN,
    NON_VALVE_PIPE_CLOSED,
    FAULT_PIPE_NOT_FOUND,
    CLOSE_PIPE_NOT_FOUND,
    CLOSE_PIPE_NO_VALVE,
    CLOSE_PIPE_DUPLICATE,
    CLOSE_PIPE_IS_FAULT,
    FAULT_REWATER_RISK,
    NO_FEASIBLE_PLAN,
    SOURCE_NOT_FOUND,
    SOURCE_NOT_SWITCHABLE,
    SOURCE_TARGET_DUPLICATE,
    DELIVERY_SOURCE_NOT_FOUND,
    DELIVERY_SOURCE_MISSING,
    DELIVERY_SOURCE_DUPLICATE,
    DELIVERY_PIPE_NOT_FOUND,
    DELIVERY_PIPE_MISSING,
    DELIVERY_PIPE_DUPLICATE,
    DELIVERY_SINK_NOT_FOUND,
    DELIVERY_SINK_NOT_SINK,
    DELIVERY_SINK_DUPLICATE,
    DELIVERY_DEMANDS_EMPTY,
    BYPASS_ENDPOINT_NOT_FOUND,
    BYPASS_ENDPOINT_SAME,
)


def build_demo():
    """演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v)-- D --P6(无阀)-- U3
                 |           |
               P3(v)       P5(v)
                 |           |
                 U1          U2
    故障管段 P6（B? 不：D-U3）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
        "U3": Node("U3", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True),
        "P5": Pipe("P5", "B", "U2", has_valve=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
    }
    return nodes, pipes


class ImportValidationTests(unittest.TestCase):
    def test_valid_import(self):
        nodes, pipes = build_demo()
        self.assertEqual(validate_import(list(nodes.values()), list(pipes.values())), [])

    def test_dangling_endpoint(self):
        nodes = [Node("S", "source")]
        pipes = [Pipe("P1", "S", "X")]
        issues = validate_import(nodes, pipes)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, PIPE_ENDPOINT_UNKNOWN)

    def test_self_loop(self):
        nodes = [Node("A", "junction")]
        pipes = [Pipe("P1", "A", "A")]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == PIPE_SELF_LOOP for i in issues))

    def test_duplicate_ids(self):
        nodes = [Node("A", "junction"), Node("A", "sink")]
        pipes = [
            Pipe("P1", "A", "A"),  # 自环也会报
            Pipe("P1", "A", "A"),
        ]
        issues = validate_import(nodes, pipes)
        codes = [i.code for i in issues]
        self.assertIn(DUPLICATE_NODE_ID, codes)
        self.assertIn(DUPLICATE_PIPE_ID, codes)

    def test_non_valve_closed_rejected(self):
        nodes = [Node("S", "source"), Node("U", "sink")]
        pipes = [Pipe("P1", "S", "U", has_valve=False, valve_closed=True)]
        issues = validate_import(nodes, pipes)
        self.assertTrue(any(i.code == NON_VALVE_PIPE_CLOSED for i in issues))


class IsolationPlanTests(unittest.TestCase):
    def test_isolatable_plan_with_one_closed_valve(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4"])
        self.assertTrue(result.isolatable)
        self.assertEqual(result.affected_sink_ids, ["U3"])
        self.assertEqual(result.issues, [])

    def test_non_isolatable_plan(self):
        nodes, pipes = build_demo()
        # 只关 P5（B-U2）：D 仍可经 P4 从水源到达，故障管段隔离失败
        result = evaluate_plan(nodes, pipes, "P6", ["P5"])
        self.assertFalse(result.isolatable)
        # U2 因关阀失供，U3 因故障管段移除而失供，均按“前可达后不可达”统计
        self.assertEqual(result.affected_sink_ids, ["U2", "U3"])

    def test_already_closed_valve_keeps_effect_preview(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        # 预演时无需重复关闭，已关阀门继续生效
        result = evaluate_plan(nodes, pipes, "P6", [])
        self.assertTrue(result.isolatable)
        # P4 此前已关闭，U3 在方案前就已失供，不计入本次新增失供用水点
        self.assertEqual(result.affected_sink_ids, [])

    def test_submit_rejects_closing_already_closed_valve(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = evaluate_plan(nodes, pipes, "P6", ["P4"], for_submit=True)
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_DUPLICATE for i in result.issues))

    def test_fault_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "PX", ["P4"])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.issues[0].code, FAULT_PIPE_NOT_FOUND)

    def test_close_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["PX"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NOT_FOUND for i in result.issues))

    def test_close_pipe_without_valve(self):
        nodes, pipes = build_demo()
        # P6 是故障管段，单独构造一个无阀且非故障的管段来测试
        nodes["E"] = Node("E", "junction")
        pipes["P7"] = Pipe("P7", "B", "E", has_valve=False)
        result = evaluate_plan(nodes, pipes, "P6", ["P7"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_NO_VALVE for i in result.issues))

    def test_close_fault_pipe_itself_rejected(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P6"])
        self.assertFalse(result.isolatable)
        self.assertTrue(any(i.code == CLOSE_PIPE_IS_FAULT for i in result.issues))

    def test_duplicate_close_entries(self):
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P6", ["P4", "P4"])
        self.assertFalse(result.isolatable)
        dupes = [i for i in result.issues if i.code == CLOSE_PIPE_DUPLICATE]
        self.assertEqual(len(dupes), 1)

    def test_source_endpoint_always_reachable(self):
        # 故障管段直连水源且无额外阀门：水源端恒可达，不能隔离
        nodes, pipes = build_demo()
        result = evaluate_plan(nodes, pipes, "P1", [])
        self.assertFalse(result.isolatable)

    def test_affected_sinks_sorted(self):
        nodes, pipes = build_demo()
        # 关 P3 后移除 P6：U1、U3 失供（U1 字典序在前）
        result = evaluate_plan(nodes, pipes, "P6", ["P3"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U3"])
        self.assertFalse(result.isolatable)

    def test_no_affected_sink_when_fault_is_on_dead_branch_without_sink(self):
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U1": Node("U1", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True),
            "P2": Pipe("P2", "A", "B", has_valve=True),
            "P3": Pipe("P3", "A", "U1", has_valve=True),
        }
        # 故障 P2（A-B，B 为盲端连接点），关 P2 自身不可（是故障）；
        # 改为故障 P2、无阀门可关 -> 不可隔离；失供用水点为空
        result = evaluate_plan(nodes, pipes, "P2", [])
        self.assertFalse(result.isolatable)
        self.assertEqual(result.affected_sink_ids, [])

    def test_pending_fault_pipes_are_blocked_in_plan(self):
        # 待修复事件的故障管段在方案前后均视为不可通行：
        # 其下游用水点方案前已失供，不计入本次 affected
        nodes, pipes = build_demo()
        # P6（D-U3）已是另一条待修复事件的故障管段
        result = evaluate_plan(
            nodes,
            pipes,
            "P5",                      # 本次故障 B-U2
            ["P5"],
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertFalse(result.isolatable)  # B 仍从 P2 进水
        self.assertNotIn("U3", result.affected_sink_ids)


def build_restore_demo():
    """复供演示管网：

    S --P1(v)-- A --P2(v)-- B --P4(v,关)-- D --P6(无阀)-- U3
                 |                    |
               P3(v)                P7(无阀)
                 |                    |
                 U1            P8(v,开) -- E -- U4
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "E": Node("E", "junction"),
        "U1": Node("U1", "sink"),
        "U3": Node("U3", "sink"),
        "U4": Node("U4", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True, valve_closed=True),
        "P6": Pipe("P6", "D", "U3", has_valve=False),
        "P7": Pipe("P7", "D", "E", has_valve=False),
        "P8": Pipe("P8", "E", "U4", has_valve=True),
    }
    return nodes, pipes


class RestorePlanningTests(unittest.TestCase):
    def test_safe_restore_when_other_faults_stay_isolated(self):
        nodes, pipes = build_restore_demo()
        # 片区内事件（未关阀）复供：重开集合为空，仍关的 P4 保护故障 P6
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset(),
            other_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, [])
        self.assertEqual(result.blocks, [])

    def test_unsafe_restore_when_pending_fault_end_gets_water(self):
        nodes, pipes = build_restore_demo()
        # 外层事件（关 P4）复供，而片区内 P7 仍待修复：D 端重新进水
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset({"P7"}),
        )
        self.assertFalse(result.safe)
        self.assertEqual(len(result.blocks), 1)
        block = result.blocks[0]
        self.assertEqual(block.fault_pipe_id, "P7")
        self.assertEqual(block.reachable_endpoints, ["D"])
        self.assertEqual(block.as_dict()["code"], FAULT_REWATER_RISK)
        # U3 恢复；U4 因 P7 自身阻断仍不可达
        self.assertEqual(result.restored_sink_ids, ["U3"])

    def test_restore_becomes_safe_after_inner_event_restored(self):
        nodes, pipes = build_restore_demo()
        # 没有其他待修复故障时，重开 P4，U3/U4 同时恢复
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset(),
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, ["U3", "U4"])

    def test_other_closed_valves_keep_effect(self):
        # 复供只重开本事件阀门；另一事件关闭的 P2 继续生效
        nodes, pipes = build_restore_demo()
        pipes["P2"] = Pipe("P2", "A", "B", has_valve=True, valve_closed=True)
        result = evaluate_restore(
            nodes, pipes, reopen_pipe_ids=frozenset({"P4"}),
            other_fault_pipe_ids=frozenset(),
        )
        # P2 关闭时 B/D 侧整体仍无水，U3/U4 不会恢复，但也无风险
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, [])


def build_series_demo():
    """串联阀门管网：任一单阀都能隔离故障，用于检验字典序 tie-break。

    S --PB(v)-- J1 --PA(v)-- J2 --PX(无阀,故障)-- U1
    """
    nodes = {
        "S": Node("S", "source"),
        "J1": Node("J1", "junction"),
        "J2": Node("J2", "junction"),
        "U1": Node("U1", "sink"),
    }
    pipes = {
        "PB": Pipe("PB", "S", "J1", has_valve=True),
        "PA": Pipe("PA", "J1", "J2", has_valve=True),
        "PX": Pipe("PX", "J2", "U1", has_valve=False),
    }
    return nodes, pipes


def build_ring_demo(with_branch_sinks=True):
    """环形管网：B 经 P1（A-B）与 P3+P8（A-C-B）双路进水。

    S --P9(v)-- A --P1(v)-- B --PF(无阀,故障)-- D --P5(v)-- U1
                 |          |
                 P3(v)      P2(v)-- U2
                 |
                 C --P8(v)--（回到 B）
    with_branch_sinks=True 时增加 A/C 侧用水点（P7->U3、P4->U4）。
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "C": Node("C", "junction"),
        "D": Node("D", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "P9": Pipe("P9", "S", "A", has_valve=True),
        "P1": Pipe("P1", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "C", has_valve=True),
        "P8": Pipe("P8", "C", "B", has_valve=True),
        "PF": Pipe("PF", "B", "D", has_valve=False),
        "P5": Pipe("P5", "D", "U1", has_valve=True),
        "P2": Pipe("P2", "B", "U2", has_valve=True),
    }
    if with_branch_sinks:
        nodes["U3"] = Node("U3", "sink")
        nodes["U4"] = Node("U4", "sink")
        pipes["P7"] = Pipe("P7", "A", "U3", has_valve=True)
        pipes["P4"] = Pipe("P4", "C", "U4", has_valve=True)
    return nodes, pipes


class RecommendPlanTests(unittest.TestCase):
    def test_basic_recommendation(self):
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P6")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P4"])
        self.assertEqual(result.affected_sink_ids, ["U3"])
        self.assertIsNone(result.reason)

    def test_fault_pipe_not_found(self):
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "PX")
        self.assertFalse(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, [])
        self.assertEqual(result.affected_sink_ids, [])
        self.assertEqual(result.reason.code, FAULT_PIPE_NOT_FOUND)

    def test_no_feasible_plan_when_fault_touches_source(self):
        # 故障管段 P1 直连水源：S 端恒可达，关任何阀都无法隔离
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P1")
        self.assertFalse(result.feasible)
        self.assertEqual(result.reason.code, NO_FEASIBLE_PLAN)

    def test_empty_plan_when_fault_already_isolated(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        # P4 已关闭，故障 P6 两端本就不通水：建议空方案（关阀数 0 最优）
        result = recommend_plan(nodes, pipes, "P6")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, [])
        self.assertEqual(result.affected_sink_ids, [])

    def test_closed_valves_and_pending_faults_are_respected(self):
        nodes, pipes = build_demo()
        # P4 已关闭、P6 为其他待修复事件的故障管段
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        result = recommend_plan(
            nodes, pipes, "P5", pending_fault_pipe_ids=frozenset({"P6"})
        )
        # 建议不得包含已关闭的 P4；关 P2 即可隔离 B 侧，仅 U2 新增失供
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P2"])
        self.assertEqual(result.affected_sink_ids, ["U2"])

    def test_fault_pipe_itself_never_recommended(self):
        # 故障管段 P5 自带阀门：建议仍须绕开它（提交接口拒绝关故障管段本身）
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P5")
        self.assertTrue(result.feasible)
        self.assertNotIn("P5", result.recommended_close_pipe_ids)
        self.assertEqual(result.recommended_close_pipe_ids, ["P2"])

    def test_lexicographic_tie_break(self):
        # 关 PB 或 PA 都能隔离且失供相同（U1）：取编号字典序最小者 PA
        nodes, pipes = build_series_demo()
        result = recommend_plan(nodes, pipes, "PX")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["PA"])
        self.assertEqual(result.affected_sink_ids, ["U1"])

    def test_fewer_affected_sinks_beats_fewer_valves(self):
        # 环形：关干管 P9 只动 1 个阀但 U1-U4 全部失供；
        # 关 P1+P8 两个阀只失供 U1/U2 -> 失供最少优先
        nodes, pipes = build_ring_demo(with_branch_sinks=True)
        result = recommend_plan(nodes, pipes, "PF")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P1", "P8"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U2"])

    def test_fewer_valves_beats_lexicographically_smaller_set(self):
        # 无支线用水点：{P9}（1 阀）与 {P1,P3}/{P1,P8}（2 阀）失供相同，
        # 关阀数最少优先于编号字典序 -> 选 P9
        nodes, pipes = build_ring_demo(with_branch_sinks=False)
        result = recommend_plan(nodes, pipes, "PF")
        self.assertTrue(result.feasible)
        self.assertEqual(result.recommended_close_pipe_ids, ["P9"])
        self.assertEqual(result.affected_sink_ids, ["U1", "U2"])

    def test_recommendation_is_submittable(self):
        # 建议结果直接交给 evaluate_plan(for_submit=True) 复核：可隔离且无阻断问题
        nodes, pipes = build_demo()
        result = recommend_plan(nodes, pipes, "P6")
        check = evaluate_plan(
            nodes, pipes, "P6", result.recommended_close_pipe_ids, for_submit=True
        )
        self.assertTrue(check.isolatable)
        self.assertEqual(check.issues, [])
        self.assertEqual(check.affected_sink_ids, result.affected_sink_ids)


def build_two_source_demo():
    """双水源管网：

    S1 --Q1(v)-- C --Q2(v,故障)-- U1
    S2 --Q3(v)-- C --Q4(v)------ U2
    两座水源初始均运行；C 为公共连接点。
    """
    nodes = {
        "S1": Node("S1", "source"),
        "S2": Node("S2", "source"),
        "C": Node("C", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "Q1": Pipe("Q1", "S1", "C", has_valve=True),
        "Q3": Pipe("Q3", "S2", "C", has_valve=True),
        "Q2": Pipe("Q2", "C", "U1", has_valve=True),
        "Q4": Pipe("Q4", "C", "U2", has_valve=True),
    }
    return nodes, pipes


class SourceSwitchPlanningTests(unittest.TestCase):
    def test_stop_one_source_redundant_supply(self):
        # 双源顶供：停运 S1，S2 仍供 C，无用水点失供
        nodes, pipes = build_two_source_demo()
        result = evaluate_source_switch(nodes, pipes, [("S1", False)])
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.affected_sink_ids, [])
        self.assertEqual(result.restored_sink_ids, [])

    def test_stop_all_sources_cuts_every_sink(self):
        # 允许全网无运行水源：全部 sink 新增失供
        nodes, pipes = build_two_source_demo()
        result = evaluate_source_switch(
            nodes, pipes, [("S1", False), ("S2", False)]
        )
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.affected_sink_ids, ["U1", "U2"])

    def test_restore_source_restores_sinks(self):
        # 两源全停后恢复 S1：U1/U2 恢复
        nodes, pipes = build_two_source_demo()
        for sid in ("S1", "S2"):
            nodes[sid] = Node(sid, "source", running=False)
        result = evaluate_source_switch(nodes, pipes, [("S1", True)])
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.restored_sink_ids, ["U1", "U2"])
        self.assertEqual(result.affected_sink_ids, [])

    def test_noop_when_target_states_already_satisfied(self):
        nodes, pipes = build_two_source_demo()
        # 已运行 -> 运行；已停运 -> 停运：changed=False
        nodes["S2"] = Node("S2", "source", running=False)
        result = evaluate_source_switch(
            nodes, pipes, [("S1", True), ("S2", False)]
        )
        self.assertFalse(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.affected_sink_ids, [])
        self.assertEqual(result.restored_sink_ids, [])

    def test_invalid_targets_not_found_non_source_duplicate(self):
        nodes, pipes = build_two_source_demo()
        result = evaluate_source_switch(
            nodes,
            pipes,
            [("SX", False), ("U1", False), ("S1", False), ("S1", True)],
        )
        codes = [i.code for i in result.issues]
        self.assertEqual(
            codes,
            [
                SOURCE_NOT_FOUND,
                SOURCE_NOT_SWITCHABLE,
                SOURCE_TARGET_DUPLICATE,
            ],
        )
        self.assertFalse(result.safe)
        self.assertFalse(result.changed)

    def test_restore_source_rewaters_pending_fault(self):
        # S2 停运、Q1 关闭隔离待修复故障 Q2（C/U1 均不可达）；
        # 恢复 S2 -> Q2 的 C 端重新进水，不安全；U2 恢复（Q2 仍阻断 U1）
        nodes, pipes = build_two_source_demo()
        nodes["S2"] = Node("S2", "source", running=False)
        pipes["Q1"] = Pipe("Q1", "S1", "C", has_valve=True, valve_closed=True)
        result = evaluate_source_switch(
            nodes,
            pipes,
            [("S2", True)],
            pending_fault_pipe_ids=frozenset({"Q2"}),
        )
        self.assertTrue(result.changed)
        self.assertFalse(result.safe)
        self.assertEqual(len(result.blocks), 1)
        block = result.blocks[0]
        self.assertEqual(block.fault_pipe_id, "Q2")
        self.assertEqual(block.reachable_endpoints, ["C"])
        self.assertEqual(block.as_dict()["code"], FAULT_REWATER_RISK)
        self.assertEqual(result.restored_sink_ids, ["U2"])

    def test_stop_source_never_introduces_risk(self):
        # 同一状态下停运 S1：可达范围只收缩，不产生重新进水风险
        nodes, pipes = build_two_source_demo()
        nodes["S2"] = Node("S2", "source", running=False)
        pipes["Q1"] = Pipe("Q1", "S1", "C", has_valve=True, valve_closed=True)
        result = evaluate_source_switch(
            nodes,
            pipes,
            [("S1", False)],
            pending_fault_pipe_ids=frozenset({"Q2"}),
        )
        self.assertTrue(result.changed)
        self.assertTrue(result.safe)
        self.assertEqual(result.blocks, [])

    def test_stopped_source_excluded_from_all_reachability(self):
        # 停运 S1 后：隔离/建议只需切断 S2 一路（关 Q3 即可隔离 C），
        # 且 Q1（停运源侧）不在建议中
        nodes, pipes = build_two_source_demo()
        nodes["S1"] = Node("S1", "source", running=False)
        plan = evaluate_plan(nodes, pipes, "Q2", ["Q3"])
        self.assertTrue(plan.isolatable)
        self.assertEqual(plan.affected_sink_ids, ["U1", "U2"])
        rec = recommend_plan(nodes, pipes, "Q2")
        self.assertTrue(rec.feasible)
        self.assertEqual(rec.recommended_close_pipe_ids, ["Q3"])
        # 两座水源都停运：故障端本就不可达，空方案即可“隔离”，无可达水源
        nodes["S2"] = Node("S2", "source", running=False)
        plan = evaluate_plan(nodes, pipes, "Q2", [])
        self.assertTrue(plan.isolatable)
        self.assertEqual(plan.affected_sink_ids, [])


class SnapshotStateTests(unittest.TestCase):
    def test_reachable_sink_ids_only_running_sources(self):
        nodes, pipes = build_demo()
        self.assertEqual(reachable_sink_ids(nodes, pipes), ["U1", "U2", "U3"])
        # 关闭 P4 与故障 P6 阻断后 U3 不在供水点
        blocked = frozenset({"P4", "P6"})
        self.assertEqual(reachable_sink_ids(nodes, pipes, blocked), ["U1", "U2"])

    def test_reachable_sink_ids_empty_when_all_sources_stopped(self):
        nodes, pipes = build_two_source_demo()
        nodes["S1"] = Node("S1", "source", running=False)
        nodes["S2"] = Node("S2", "source", running=False)
        self.assertEqual(reachable_sink_ids(nodes, pipes), [])

    def test_freeze_state_contains_all_parts(self):
        nodes, pipes = build_demo()
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        state = freeze_state(nodes, pipes, [(7, "P6")])
        self.assertEqual([n["id"] for n in state["nodes"]],
                         ["A", "B", "D", "S", "U1", "U2", "U3"])
        self.assertEqual([p["id"] for p in state["pipes"]],
                         ["P1", "P2", "P3", "P4", "P5", "P6"])
        p4 = next(p for p in state["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])
        self.assertEqual(
            state["open_faults"], [{"event_id": 7, "fault_pipe_id": "P6"}]
        )
        # P4 关、P6 故障：U3 不供水
        self.assertEqual(state["supplied_sink_ids"], ["U1", "U2"])

    def test_diff_none_base_treats_all_added(self):
        nodes, pipes = build_demo()
        target = freeze_state(nodes, pipes)
        diff = diff_states(None, target)
        self.assertEqual(len(diff["nodes"]["added"]), 7)
        self.assertEqual(diff["nodes"]["removed"], [])
        self.assertEqual(len(diff["pipes"]["added"]), 6)
        self.assertEqual(
            diff["supplied_sinks"]["added"], ["U1", "U2", "U3"]
        )
        self.assertEqual(diff["supplied_sinks"]["removed"], [])
        self.assertEqual(diff["valve_positions"]["changed"], [])
        self.assertEqual(diff["source_states"]["changed"], [])
        self.assertEqual(diff["open_faults"]["added"], [])
        self.assertEqual(diff["open_faults"]["removed"], [])

    def test_diff_valve_fault_and_sink_changes(self):
        nodes, pipes = build_demo()
        before = freeze_state(nodes, pipes, [])
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        after = freeze_state(nodes, pipes, [(1, "P6")])
        diff = diff_states(before, after)
        self.assertEqual(diff["nodes"]["added"], [])
        self.assertEqual(diff["pipes"]["added"], [])
        self.assertEqual(
            diff["valve_positions"]["changed"],
            [{"pipe_id": "P4", "valve_closed_before": False,
              "valve_closed_after": True}],
        )
        self.assertEqual(
            diff["open_faults"]["added"],
            [{"event_id": 1, "fault_pipe_id": "P6"}],
        )
        self.assertEqual(diff["open_faults"]["removed"], [])
        self.assertEqual(diff["supplied_sinks"]["added"], [])
        self.assertEqual(diff["supplied_sinks"]["removed"], ["U3"])

    def test_diff_source_state_changes(self):
        nodes, pipes = build_two_source_demo()
        before = freeze_state(nodes, pipes, [])
        nodes["S1"] = Node("S1", "source", running=False)
        after = freeze_state(nodes, pipes, [])
        diff = diff_states(before, after)
        self.assertEqual(
            diff["source_states"]["changed"],
            [{"source_id": "S1", "running_before": True,
              "running_after": False}],
        )
        # S2 仍经 C 顶供：用水点无净变化
        self.assertEqual(diff["supplied_sinks"]["added"], [])
        self.assertEqual(diff["supplied_sinks"]["removed"], [])

    def test_diff_network_replacement(self):
        nodes, pipes = build_demo()
        before = freeze_state(nodes, pipes, [])
        nodes2, pipes2 = build_two_source_demo()
        after = freeze_state(nodes2, pipes2, [])
        diff = diff_states(before, after)
        self.assertEqual(
            {n["id"] for n in diff["nodes"]["added"]}, {"S1", "S2", "C"}
        )
        self.assertEqual(
            {n["id"] for n in diff["nodes"]["removed"]},
            {"S", "A", "B", "D", "U3"},
        )
        self.assertEqual(
            {p["id"] for p in diff["pipes"]["added"]}, {"Q1", "Q2", "Q3", "Q4"}
        )
        self.assertEqual(
            {p["id"] for p in diff["pipes"]["removed"]},
            {"P1", "P2", "P3", "P4", "P5", "P6"},
        )

    def test_diff_is_symmetric_for_no_change(self):
        nodes, pipes = build_demo()
        state = freeze_state(nodes, pipes, [(1, "P1")])
        diff = diff_states(state, state)
        for section in ("nodes", "pipes", "open_faults", "supplied_sinks"):
            self.assertEqual(diff[section]["added"], [])
            self.assertEqual(diff[section]["removed"], [])
        self.assertEqual(diff["valve_positions"]["changed"], [])
        self.assertEqual(diff["source_states"]["changed"], [])


# ---------------------------------------------------------------- 流量核算


def build_delivery_demo():
    """双水源联网（便于核算水源/管段容量约束）：

    S1 --Q1-- C --Q2-- U1
    S2 --Q3-- C --Q4-- U2
    """
    nodes, pipes = build_two_source_demo()
    source_caps = [("S1", 100), ("S2", 100)]
    pipe_caps = [("Q1", 5), ("Q3", 5), ("Q2", 3), ("Q4", 100)]
    return nodes, pipes, source_caps, pipe_caps


class DeliveryValidationTests(unittest.TestCase):
    def _full_caps(self, nodes, pipes):
        sources = [(nid, 10) for nid, n in nodes.items() if n.type == "source"]
        plist = [(pid, 10) for pid in pipes]
        return sources, plist

    def test_valid_request_no_issues(self):
        nodes, pipes, source_caps, pipe_caps = build_delivery_demo()
        issues = validate_delivery_request(
            nodes, pipes, source_caps, pipe_caps, [("U1", 3), ("U2", 6)]
        )
        self.assertEqual(issues, [])

    def test_zero_capacity_and_demand_allowed(self):
        nodes, pipes, source_caps, pipe_caps = build_delivery_demo()
        source_caps = [("S1", 0), ("S2", 0)]
        pipe_caps = [(pid, 0) for pid, _ in pipe_caps]
        issues = validate_delivery_request(
            nodes, pipes, source_caps, pipe_caps, [("U1", 0)]
        )
        self.assertEqual(issues, [])

    def test_missing_source_and_pipe(self):
        nodes, pipes, _sc, _pc = build_delivery_demo()
        issues = validate_delivery_request(
            nodes, pipes,
            [("S1", 10)],                       # 缺 S2
            [("Q1", 10), ("Q3", 10), ("Q2", 10)],  # 缺 Q4
            [("U1", 1)],
        )
        codes = {i.code for i in issues}
        self.assertEqual(codes, {DELIVERY_SOURCE_MISSING, DELIVERY_PIPE_MISSING})

    def test_unknown_duplicate_source_pipe(self):
        nodes, pipes, _sc, _pc = build_delivery_demo()
        issues = validate_delivery_request(
            nodes, pipes,
            [("S1", 1), ("S1", 1), ("S2", 1), ("SX", 1)],
            [("Q1", 1), ("Q1", 1), ("Q3", 1), ("Q2", 1), ("Q4", 1), ("QX", 1)],
            [("U1", 1)],
        )
        codes = {i.code for i in issues}
        self.assertEqual(
            codes,
            {
                DELIVERY_SOURCE_DUPLICATE,
                DELIVERY_SOURCE_NOT_FOUND,
                DELIVERY_PIPE_DUPLICATE,
                DELIVERY_PIPE_NOT_FOUND,
            },
        )

    def test_unknown_duplicate_non_sink_demand(self):
        nodes, pipes, source_caps, pipe_caps = build_delivery_demo()
        issues = validate_delivery_request(
            nodes, pipes, source_caps, pipe_caps,
            [("U1", 1), ("U1", 1), ("S1", 1), ("C", 1), ("UX", 1)],
        )
        codes = {i.code for i in issues}
        self.assertEqual(
            codes,
            {
                DELIVERY_SINK_DUPLICATE,
                DELIVERY_SINK_NOT_SINK,   # S1、C 都不是 sink，报两条
                DELIVERY_SINK_NOT_FOUND,
            },
        )
        # S1 与 C 各产生一条 NOT_SINK
        self.assertEqual(
            sum(1 for i in issues if i.code == DELIVERY_SINK_NOT_SINK), 2
        )

    def test_empty_demands_rejected(self):
        nodes, pipes, source_caps, pipe_caps = build_delivery_demo()
        issues = validate_delivery_request(
            nodes, pipes, source_caps, pipe_caps, []
        )
        self.assertEqual([i.code for i in issues], [DELIVERY_DEMANDS_EMPTY])

    def test_empty_network_coverage_uses_empty_lists(self):
        # 空网：水源/管段容量均为空列表即视为完整覆盖（用水点仍须存在，故必失败）
        nodes = {"U1": Node("U1", "sink")}
        issues = validate_delivery_request(
            nodes, {}, [], [], [("U1", 1)]
        )
        self.assertEqual(issues, [])
        # 空网上申报不存在的用水点
        issues = validate_delivery_request(nodes, {}, [], [], [("UX", 1)])
        self.assertEqual([i.code for i in issues], [DELIVERY_SINK_NOT_FOUND])


class DeliveryMaxFlowTests(unittest.TestCase):
    def test_pipe_bottleneck(self):
        # S --P1(5)-- A --P2(3)-- U：需求 4 只能交付 3
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "U": Node("U", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "A"), "P2": Pipe("P2", "A", "U")}
        result = evaluate_delivery(
            nodes, pipes, [("S", 10)], [("P1", 5), ("P2", 3)], [("U", 4)]
        )
        self.assertEqual(result.total_demand, 4)
        self.assertEqual(result.max_delivered_total, 3)
        self.assertFalse(result.feasible)

    def test_demand_caps_result(self):
        # 需求小于管段能力：最大可交付对总需求封顶
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "U": Node("U", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "A"), "P2": Pipe("P2", "A", "U")}
        result = evaluate_delivery(
            nodes, pipes, [("S", 10)], [("P1", 5), ("P2", 3)], [("U", 2)]
        )
        self.assertEqual(result.max_delivered_total, 2)
        self.assertTrue(result.feasible)

    def test_source_capacity_bounds_total(self):
        nodes, pipes, _sc, _pc = build_delivery_demo()
        # 两座水源各只能供 2：合计最多 4，即便管段富余
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 2), ("S2", 2)],
            [("Q1", 100), ("Q3", 100), ("Q2", 100), ("Q4", 100)],
            [("U1", 10), ("U2", 10)],
        )
        self.assertEqual(result.max_delivered_total, 4)
        self.assertFalse(result.feasible)

    def test_two_source_redundancy_and_shared_pipes(self):
        nodes, pipes, _sc, pipe_caps = build_delivery_demo()
        # Q1/Q3 各 5，Q2 只有 3（U1），Q4 富余（U2）
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 100), ("S2", 100)],
            pipe_caps,
            [("U1", 3), ("U2", 6)],
        )
        self.assertEqual(result.max_delivered_total, 9)
        self.assertTrue(result.feasible)
        # 总需求 12 超过两进水管合计 10
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 100), ("S2", 100)],
            pipe_caps,
            [("U1", 3), ("U2", 9)],
        )
        self.assertEqual(result.max_delivered_total, 10)
        self.assertFalse(result.feasible)

    def test_stopped_source_supplies_nothing(self):
        nodes, pipes, _sc, pipe_caps = build_delivery_demo()
        nodes["S1"] = Node("S1", "source", running=False)
        # S2 经 Q3（cap 5）顶供：最多 5
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 100), ("S2", 100)],  # 停运源给容量也不供水
            pipe_caps,
            [("U1", 3), ("U2", 6)],
        )
        self.assertEqual(result.max_delivered_total, 5)
        self.assertFalse(result.feasible)

    def test_all_sources_stopped_delivers_zero(self):
        nodes, pipes, _sc, pipe_caps = build_delivery_demo()
        nodes["S1"] = Node("S1", "source", running=False)
        nodes["S2"] = Node("S2", "source", running=False)
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 100), ("S2", 100)],
            pipe_caps,
            [("U1", 3), ("U2", 6)],
        )
        self.assertEqual(result.max_delivered_total, 0)
        self.assertFalse(result.feasible)

    def test_closed_valve_and_pending_fault_block(self):
        # S --P1[阀,关]-- A --P2(故障)-- U1；A --P3-- U2
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "U1": Node("U1", "sink"),
            "U2": Node("U2", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A", has_valve=True, valve_closed=True),
            "P2": Pipe("P2", "A", "U1"),
            "P3": Pipe("P3", "A", "U2"),
        }
        result = evaluate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 5), ("U2", 5)],
            blocked_pipe_ids=frozenset({"P1", "P2"}),
        )
        # P1 关闭后 A 完全断源：交付 0
        self.assertEqual(result.max_delivered_total, 0)

        # 仅故障 P2 阻断、P1 通水：U2 可满足、U1 不行
        result = evaluate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 5), ("U2", 5)],
            blocked_pipe_ids=frozenset({"P2"}),
        )
        self.assertEqual(result.max_delivered_total, 5)
        self.assertFalse(result.feasible)

    def test_unlisted_sink_does_not_consume(self):
        # 只申报 U1：U2 不走汇边、不占用量
        nodes, pipes, _sc, pipe_caps = build_delivery_demo()
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 100), ("S2", 100)],
            pipe_caps,
            [("U1", 3)],
        )
        self.assertEqual(result.total_demand, 3)
        self.assertEqual(result.max_delivered_total, 3)
        self.assertTrue(result.feasible)

    def test_zero_total_demand_is_feasible(self):
        nodes, pipes, _sc, pipe_caps = build_delivery_demo()
        result = evaluate_delivery(
            nodes, pipes,
            [("S1", 100), ("S2", 100)],
            pipe_caps,
            [("U1", 0), ("U2", 0)],
        )
        self.assertEqual(result.total_demand, 0)
        self.assertEqual(result.max_delivered_total, 0)
        self.assertTrue(result.feasible)

    def test_undirected_shared_capacity_reroute(self):
        # 无向共用容量：菱形管网，验证两侧路径可分流且单管双向合计受限
        #           --P1(6)-- A --P2(6)--
        # S(cap100)                        U(demand 10)
        #           --P3(4)-- B --P4(4)--
        nodes = {
            "S": Node("S", "source"),
            "A": Node("A", "junction"),
            "B": Node("B", "junction"),
            "U": Node("U", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "A"),
            "P2": Pipe("P2", "A", "U"),
            "P3": Pipe("P3", "S", "B"),
            "P4": Pipe("P4", "B", "U"),
        }
        result = evaluate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 6), ("P2", 6), ("P3", 4), ("P4", 4)],
            [("U", 10)],
        )
        self.assertEqual(result.max_delivered_total, 10)
        self.assertTrue(result.feasible)
        # P3 收紧到 2：总能力 6+2=8
        result = evaluate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 6), ("P2", 6), ("P3", 2), ("P4", 4)],
            [("U", 10)],
        )
        self.assertEqual(result.max_delivered_total, 8)

    def test_direct_undirected_max_flow_helper(self):
        # 直接校验无向边：S 与两个汇点经同一条无向边不可能超过共用容量
        nodes = ["S", "A", "U1", "U2"]
        # S-A 共用 5；A-U1 100、A-U2 100
        flow = _undirected_max_flow(
            nodes,
            [("S", "A", 5), ("A", "U1", 100), ("A", "U2", 100)],
            [("S", 100)],
            [("U1", 100), ("U2", 100)],
        )
        self.assertEqual(flow, 5)


# ------------------------------------------------------------ 保障等级分档交付


def build_allocation_demo():
    """S --P1-- A --P2-- U1；A --P3-- U2（便于按等级/逐点核对字典序）。"""
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A"),
        "P2": Pipe("P2", "A", "U1"),
        "P3": Pipe("P3", "A", "U2"),
    }
    return nodes, pipes


class DeliveryAllocationTests(unittest.TestCase):
    def _entries(self, result):
        return [(e.sink_id, e.level, e.delivered, e.demand) for e in result.entries]

    def test_higher_level_starves_first_under_shared_bottleneck(self):
        nodes, pipes = build_allocation_demo()
        # P1 双向共用只有 10：等级 1 的 U1 先拿满 10，等级 2 的 U2 得 0
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 10), ("P2", 100), ("P3", 100)],
            [("U1", 10, 1), ("U2", 10, 2)],
        )
        self.assertEqual(
            self._entries(result),
            [("U1", 1, 10, 10), ("U2", 2, 0, 10)],
        )
        self.assertEqual(result.total_delivered, 10)
        self.assertFalse(result.all_satisfied)
        self.assertEqual(
            [(l.level, l.delivered, l.demand, l.shortfall) for l in result.levels],
            [(1, 10, 10, 0), (2, 0, 10, 10)],
        )

    def test_level_order_comes_before_sink_id_order(self):
        nodes, pipes = build_allocation_demo()
        # U2 等级 1、U1 等级 2：编号靠后的 U2 反而先拿满
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 10), ("P2", 100), ("P3", 100)],
            [("U1", 10, 2), ("U2", 10, 1)],
        )
        self.assertEqual(
            self._entries(result),
            [("U2", 1, 10, 10), ("U1", 2, 0, 10)],
        )

    def test_within_level_sink_id_lexicographic_tie_break(self):
        nodes, pipes = build_allocation_demo()
        # 同等级、共享瓶颈 10、各要 10：编号小的 U1 先满
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 10), ("P2", 100), ("P3", 100)],
            [("U1", 10, 1), ("U2", 10, 1)],
        )
        self.assertEqual(
            self._entries(result),
            [("U1", 1, 10, 10), ("U2", 1, 0, 10)],
        )
        self.assertEqual(result.levels[0].delivered, 10)

    def test_within_level_total_kept_while_sharing_scarce_pipe(self):
        nodes, pipes = build_allocation_demo()
        # P2（A->U1）只能 6：等级总量受 P1=10 上限为 10；逐点阶段先保 U1=6，
        # 再在“等级总量 10 不变”下把剩余 4 给 U2
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 10), ("P2", 6), ("P3", 100)],
            [("U1", 10, 1), ("U2", 10, 1)],
        )
        self.assertEqual(
            self._entries(result),
            [("U1", 1, 6, 10), ("U2", 1, 4, 10)],
        )
        self.assertTrue(result.levels[0].all_satisfied is False)
        self.assertEqual(result.levels[0].delivered, 10)

    def test_source_capacity_bounds_priority_levels(self):
        nodes, pipes = build_allocation_demo()
        # 水源只能供 7：等级 1 的 U1 先满足 5，余量 2 给等级 2 的 U2
        result = allocate_delivery(
            nodes, pipes,
            [("S", 7)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 5, 1), ("U2", 5, 2)],
        )
        self.assertEqual(
            self._entries(result),
            [("U1", 1, 5, 5), ("U2", 2, 2, 5)],
        )

    def test_all_satisfied_when_capacity_abundant(self):
        nodes, pipes = build_allocation_demo()
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 3, 1), ("U2", 4, 2)],
        )
        self.assertEqual(
            self._entries(result),
            [("U1", 1, 3, 3), ("U2", 2, 4, 4)],
        )
        self.assertTrue(result.all_satisfied)
        self.assertTrue(all(l.all_satisfied for l in result.levels))

    def test_zero_demand_points_get_zero_and_skipped_levels(self):
        nodes, pipes = build_allocation_demo()
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 0, 1), ("U2", 0, 2)],
        )
        self.assertEqual(result.total_demand, 0)
        self.assertEqual(result.total_delivered, 0)
        self.assertTrue(result.all_satisfied)
        self.assertEqual(
            self._entries(result),
            [("U1", 1, 0, 0), ("U2", 2, 0, 0)],
        )
        # 等级允许跳跃（1 与 3）
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 10), ("P2", 100), ("P3", 100)],
            [("U1", 10, 3), ("U2", 10, 1)],
        )
        self.assertEqual([l.level for l in result.levels], [1, 3])
        self.assertEqual(
            self._entries(result),
            [("U2", 1, 10, 10), ("U1", 3, 0, 10)],
        )

    def test_blocked_pipes_and_stopped_source_deliver_zero(self):
        nodes, pipes = build_allocation_demo()
        # P1 阻断：整个下游断供
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 5, 1), ("U2", 5, 1)],
            blocked_pipe_ids=frozenset({"P1"}),
        )
        self.assertEqual(result.total_delivered, 0)
        self.assertFalse(result.all_satisfied)

        nodes["S"] = Node("S", "source", running=False)
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U1", 5, 1), ("U2", 5, 1)],
        )
        self.assertEqual(result.total_delivered, 0)

    def test_entries_sorted_by_level_then_sink_id(self):
        nodes, pipes = build_allocation_demo()
        # 请求顺序故意打乱；响应恒按（等级, 编号）排序
        result = allocate_delivery(
            nodes, pipes,
            [("S", 100)],
            [("P1", 100), ("P2", 100), ("P3", 100)],
            [("U2", 4, 2), ("U1", 3, 1)],
        )
        self.assertEqual(
            [(e.sink_id, e.level) for e in result.entries],
            [("U1", 1), ("U2", 2)],
        )


# ---------------------------------------------------------------- 读数核对


def build_reconcile_demo():
    """单水源分支管网：

    S --P1-- A --P2-- B --P4-- U2
              |
             P3
              |
             U1
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A"),
        "P2": Pipe("P2", "A", "B"),
        "P3": Pipe("P3", "A", "U1"),
        "P4": Pipe("P4", "B", "U2"),
    }
    return nodes, pipes


class ReconcileValidationTests(unittest.TestCase):
    def _full(self, nodes, pipes):
        injections = [(nid, 1) for nid, n in nodes.items() if n.type == "source"]
        withdrawals = [(nid, 1) for nid, n in nodes.items() if n.type == "sink"]
        readings = [(pid, 10, None) for pid in pipes]
        return injections, withdrawals, readings

    def test_valid_request_no_issues(self):
        nodes, pipes = build_reconcile_demo()
        injections, withdrawals, readings = self._full(nodes, pipes)
        issues = validate_reconcile_request(
            nodes, pipes, injections, withdrawals, readings
        )
        self.assertEqual(issues, [])

    def test_coverage_and_duplicates(self):
        nodes, pipes = build_reconcile_demo()
        # 缺水源 S / 缺水点 U2 / 缺管段 P3 + 各自编号重复 / 不存在 / 类型错误
        issues = validate_reconcile_request(
            nodes,
            pipes,
            [("SX", 1), ("SX", 1)],
            [("U1", 1), ("U1", 1), ("A", 1)],
            [("P1", 10, None), ("P1", 10, None), ("PX", 10, None)],
        )
        codes = {i.code for i in issues}
        self.assertIn("RECONCILE_SOURCE_NOT_FOUND", codes)
        self.assertIn("RECONCILE_SOURCE_DUPLICATE", codes)
        self.assertIn("RECONCILE_SOURCE_MISSING", codes)
        self.assertIn("RECONCILE_SINK_DUPLICATE", codes)
        self.assertIn("RECONCILE_SINK_NOT_SINK", codes)
        self.assertIn("RECONCILE_SINK_MISSING", codes)
        self.assertIn("RECONCILE_PIPE_DUPLICATE", codes)
        self.assertIn("RECONCILE_PIPE_NOT_FOUND", codes)
        self.assertIn("RECONCILE_PIPE_MISSING", codes)

    def test_sink_not_found(self):
        nodes, pipes = build_reconcile_demo()
        injections = [("S", 1)]
        withdrawals = [("U1", 1), ("U2", 0), ("UX", 1)]
        readings = [(pid, 10, None) for pid in pipes]
        issues = validate_reconcile_request(
            nodes, pipes, injections, withdrawals, readings
        )
        self.assertIn("RECONCILE_SINK_NOT_FOUND", {i.code for i in issues})


class ReconcileMetersTests(unittest.TestCase):
    def test_all_reported_consistent(self):
        nodes, pipes = build_reconcile_demo()
        # S 注入 3：U1 取 1、U2 取 2
        readings = [
            ("P1", 10, 3),
            ("P2", 10, 2),
            ("P3", 10, 1),
            ("P4", 10, 2),
        ]
        result = reconcile_meters(
            nodes, pipes,
            [("S", 3)], [("U1", 1), ("U2", 2)], readings,
        )
        self.assertTrue(result.consistent, result.reasons)
        # 无缺报管段
        self.assertEqual(result.readings, [])
        self.assertEqual(result.reasons, [])

    def test_missing_pipe_ranges_branch(self):
        nodes, pipes = build_reconcile_demo()
        # 注入=取用=0：停运态全网零流，缺报全部可确定为 0
        result = reconcile_meters(
            nodes, pipes,
            [("S", 0)], [("U1", 0), ("U2", 0)],
            [(pid, 10, None) for pid in pipes],
        )
        self.assertTrue(result.consistent)
        self.assertEqual({r.pipe_id for r in result.readings}, set(pipes))
        for r in result.readings:
            self.assertEqual((r.min_flow, r.max_flow), (0, 0))
            self.assertTrue(r.determinable)

    def test_missing_downstream_determined_by_demand(self):
        nodes, pipes = build_reconcile_demo()
        # S 注入 3、U1 取 1、U2 取 2：P1/P3/P4 由平衡唯一确定，P2 同为 2
        result = reconcile_meters(
            nodes, pipes,
            [("S", 3)], [("U1", 1), ("U2", 2)],
            [(pid, 10, None) for pid in pipes],
        )
        self.assertTrue(result.consistent, result.reasons)
        got = {r.pipe_id: (r.min_flow, r.max_flow) for r in result.readings}
        self.assertEqual(got, {"P1": (3, 3), "P2": (2, 2), "P3": (1, 1), "P4": (2, 2)})
        self.assertTrue(all(r.determinable for r in result.readings))

    def test_indeterminate_range_in_loop(self):
        # 双路径环网：S 注入 2，U 取 2，两并联管段容量均为 10
        nodes = {"S": Node("S", "source"), "U": Node("U", "sink")}
        pipes = {
            "Q1": Pipe("Q1", "S", "U"),
            "Q2": Pipe("Q2", "S", "U"),
        }
        result = reconcile_meters(
            nodes, pipes,
            [("S", 2)], [("U", 2)],
            [("Q1", 10, None), ("Q2", 10, None)],
        )
        self.assertTrue(result.consistent)
        got = {r.pipe_id: (r.min_flow, r.max_flow) for r in result.readings}
        # f1+f2=2，各可在 [-8,10] 内分担
        self.assertEqual(got, {"Q1": (-8, 10), "Q2": (-8, 10)})
        self.assertFalse(any(r.determinable for r in result.readings))

    def test_partial_reading_plus_missing_range(self):
        nodes = {"S": Node("S", "source"), "U": Node("U", "sink")}
        pipes = {"Q1": Pipe("Q1", "S", "U"), "Q2": Pipe("Q2", "S", "U")}
        # Q1 已报 3，则 Q2 缺报被唯一确定为 1
        result = reconcile_meters(
            nodes, pipes,
            [("S", 4)], [("U", 4)],
            [("Q1", 10, 3), ("Q2", 10, None)],
        )
        self.assertTrue(result.consistent)
        self.assertEqual(len(result.readings), 1)
        self.assertEqual(result.readings[0].pipe_id, "Q2")
        self.assertEqual((result.readings[0].min_flow, result.readings[0].max_flow), (1, 1))
        self.assertTrue(result.readings[0].determinable)

    def test_report_violates_balance_inconsistent(self):
        nodes, pipes = build_reconcile_demo()
        # P1 报 0，但 S 注入 3：无可行解，且不输出补值
        readings = [
            ("P1", 10, 0), ("P2", 10, None), ("P3", 10, None), ("P4", 10, None),
        ]
        result = reconcile_meters(
            nodes, pipes,
            [("S", 3)], [("U1", 1), ("U2", 2)], readings,
        )
        self.assertFalse(result.consistent)
        self.assertEqual(result.readings, [])
        self.assertEqual(result.reasons[0].code, "READINGS_INCONSISTENT")

    def test_total_mismatch_inconsistent(self):
        nodes, pipes = build_reconcile_demo()
        result = reconcile_meters(
            nodes, pipes,
            [("S", 3)], [("U1", 1), ("U2", 1)],
            [(pid, 10, None) for pid in pipes],
        )
        self.assertFalse(result.consistent)
        self.assertEqual(result.reasons[0].code, "READINGS_INCONSISTENT")

    def test_stopped_source_injection_must_be_zero(self):
        nodes = {
            "S": Node("S", "source", running=False),
            "U": Node("U", "sink"),
        }
        pipes = {"P1": Pipe("P1", "S", "U")}
        result = reconcile_meters(
            nodes, pipes, [("S", 1)], [("U", 1)], [("P1", 10, 1)]
        )
        self.assertFalse(result.consistent)
        self.assertEqual(result.reasons[0].code, "STOPPED_SOURCE_INJECTION")
        # 停运且注入 0、取用 0：一致，缺报 P1 确定为 0
        result = reconcile_meters(
            nodes, pipes, [("S", 0)], [("U", 0)], [("P1", 10, None)]
        )
        self.assertTrue(result.consistent)
        self.assertEqual((result.readings[0].min_flow, result.readings[0].max_flow), (0, 0))

    def test_blocked_pipe_flow_must_be_zero(self):
        nodes = {"S": Node("S", "source"), "U": Node("U", "sink")}
        pipes = {"P1": Pipe("P1", "S", "U", has_valve=True, valve_closed=True)}
        # 阻断管段有非零读数
        result = reconcile_meters(
            nodes, pipes, [("S", 0)], [("U", 0)], [("P1", 10, 1)],
            blocked_pipe_ids=frozenset(["P1"]),
        )
        self.assertFalse(result.consistent)
        self.assertEqual(result.reasons[0].code, "BLOCKED_PIPE_FLOW")
        # 阻断管段缺报：可确定为 0
        result = reconcile_meters(
            nodes, pipes, [("S", 0)], [("U", 0)], [("P1", 10, None)],
            blocked_pipe_ids=frozenset(["P1"]),
        )
        self.assertTrue(result.consistent)
        self.assertEqual(result.readings[0].pipe_id, "P1")
        self.assertTrue(result.readings[0].determinable)
        self.assertEqual(result.readings[0].min_flow, 0)

    def test_reading_over_capacity(self):
        nodes = {"S": Node("S", "source"), "U": Node("U", "sink")}
        pipes = {"P1": Pipe("P1", "S", "U")}
        result = reconcile_meters(
            nodes, pipes, [("S", 5)], [("U", 5)], [("P1", 4, -5)]
        )
        self.assertFalse(result.consistent)
        self.assertEqual(result.reasons[0].code, "READING_OVER_CAPACITY")

    def test_zero_capacity_missing_is_zero(self):
        nodes = {"S": Node("S", "source"), "U": Node("U", "sink")}
        pipes = {"P1": Pipe("P1", "S", "U")}
        result = reconcile_meters(
            nodes, pipes, [("S", 0)], [("U", 0)], [("P1", 0, None)]
        )
        self.assertTrue(result.consistent)
        self.assertEqual((result.readings[0].min_flow, result.readings[0].max_flow), (0, 0))

    def test_empty_network(self):
        result = reconcile_meters({}, {}, [], [], [])
        self.assertTrue(result.consistent)
        self.assertEqual(result.readings, [])


def build_drill_network():
    """演练测试管网（同 examples/restore_network.json）：

    S --P1(v)-- A --P2(v)-- B --P4(v)-- D --P6(无阀)-- U3
                 |                     |
               P3(v)                 P7(无阀)-- E --P8(v)-- U4
                 |
                 U1
    """
    nodes = {
        "S": Node("S", "source"),
        "A": Node("A", "junction"),
        "B": Node("B", "junction"),
        "D": Node("D", "junction"),
        "E": Node("E", "junction"),
        "U1": Node("U1", "sink"),
        "U3": Node("U3", "sink"),
        "U4": Node("U4", "sink"),
    }
    pipes = {
        "P1": Pipe("P1", "S", "A", has_valve=True),
        "P2": Pipe("P2", "A", "B", has_valve=True),
        "P3": Pipe("P3", "A", "U1", has_valve=True),
        "P4": Pipe("P4", "B", "D", has_valve=True),
        "P6": Pipe("P6", "D", "U3"),
        "P7": Pipe("P7", "D", "E"),
        "P8": Pipe("P8", "E", "U4", has_valve=True),
    }
    return nodes, pipes


def build_drill_two_source_network():
    """双水源演练测试管网：

    S1 --Q1(v)-- C --Q2(v)-- U1
    S2 --Q3(v)-- C --Q4(v)-- U2
    """
    nodes = {
        "S1": Node("S1", "source"),
        "S2": Node("S2", "source"),
        "C": Node("C", "junction"),
        "U1": Node("U1", "sink"),
        "U2": Node("U2", "sink"),
    }
    pipes = {
        "Q1": Pipe("Q1", "S1", "C", has_valve=True),
        "Q3": Pipe("Q3", "S2", "C", has_valve=True),
        "Q2": Pipe("Q2", "C", "U1", has_valve=True),
        "Q4": Pipe("Q4", "C", "U2", has_valve=True),
    }
    return nodes, pipes


class DrillTests(unittest.TestCase):
    """调度演练（simulate_drill）纯逻辑测试：只读、逐步校验、首败即停。"""

    def test_empty_steps_returns_current_state(self):
        nodes, pipes = build_drill_network()
        result = simulate_drill(nodes, pipes, {}, [])
        self.assertTrue(result.ok)
        self.assertIsNone(result.failed_step_index)
        self.assertEqual(result.steps, [])
        final = result.final
        self.assertEqual(final.sources, [("S", True)])
        self.assertEqual(
            final.valves,
            [("P1", False), ("P2", False), ("P3", False), ("P4", False), ("P8", False)],
        )
        self.assertEqual(final.open_faults, [])
        self.assertEqual(final.supplied_sink_ids, ["U1", "U3", "U4"])

    def test_isolation_then_restore_temp_event(self):
        nodes, pipes = build_drill_network()
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T1"),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertTrue(result.ok)
        s1, s2 = result.steps
        # 第 1 步：隔离 P6 关 P4 -> 临时事件 T1，U3/U4 新增失供
        self.assertTrue(s1.ok)
        self.assertEqual(s1.temp_event_ref, "T1")
        self.assertEqual(s1.affected_sink_ids, ["U3", "U4"])
        self.assertEqual(s1.restored_sink_ids, [])
        # 第 2 步：复供 T1 -> 重开 P4，U3/U4 恢复（相对上一步）
        self.assertTrue(s2.ok)
        self.assertEqual(s2.reopened_pipe_ids, ["P4"])
        self.assertEqual(s2.restored_sink_ids, ["U3", "U4"])
        self.assertEqual(s2.affected_sink_ids, [])
        # 最终：无待修复故障、阀门全开、全部通水
        self.assertEqual(result.final.open_faults, [])
        self.assertTrue(all(not closed for _pid, closed in result.final.valves))
        self.assertEqual(result.final.supplied_sink_ids, ["U1", "U3", "U4"])

    def test_step_deltas_are_relative_to_previous_step(self):
        nodes, pipes = build_drill_network()
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
            # P7 已被 P4 隔离在上一步之后的状态里：空关阀即可隔离，且无新增失供
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=[]),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertTrue(result.ok)
        self.assertEqual(result.steps[0].affected_sink_ids, ["U3", "U4"])
        self.assertEqual(result.steps[1].affected_sink_ids, [])
        self.assertEqual(result.steps[1].temp_event_ref, "T2")
        # 最终两条待修复故障（T1/T2 按产生步骤升序）
        self.assertEqual(
            result.final.open_faults,
            [(None, "T1", "P6"), (None, "T2", "P7")],
        )
        self.assertEqual(result.final.supplied_sink_ids, ["U1"])

    def test_full_sequence_with_source_switch_and_final_state(self):
        nodes, pipes = build_drill_network()
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T1"),
            DrillStep(
                type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=["P4", "P8"]
            ),
            DrillStep(type=DRILL_STEP_SOURCE_SWITCH, targets=[("S", False)]),
            DrillStep(type=DRILL_STEP_SOURCE_SWITCH, targets=[("S", True)]),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertTrue(result.ok)
        # 停运：仅剩的 U1 新增失供；恢复：U1 恢复（P4/P8 仍关，U3/U4 不恢复）
        self.assertEqual(result.steps[3].affected_sink_ids, ["U1"])
        self.assertTrue(result.steps[3].changed)
        self.assertEqual(result.steps[4].restored_sink_ids, ["U1"])
        final = result.final
        self.assertEqual(final.sources, [("S", True)])
        self.assertEqual(
            final.valves,
            [("P1", False), ("P2", False), ("P3", False), ("P4", True), ("P8", True)],
        )
        self.assertEqual(final.open_faults, [(None, "T3", "P7")])
        self.assertEqual(final.supplied_sink_ids, ["U1"])

    def test_isolation_not_isolatable_stops_at_first_failure(self):
        nodes, pipes = build_drill_network()
        steps = [
            # 空关阀：D 端仍经 P4 进水 -> 不可隔离
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=[]),
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=[]),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step_index, 1)
        self.assertEqual(len(result.steps), 1)  # 后续步骤不再演练
        self.assertEqual(result.steps[0].error.code, NOT_ISOLATABLE)
        self.assertIsNone(result.final)

    def test_isolation_plan_invalid_commit_semantics(self):
        nodes, pipes = build_drill_network()
        # 关闭无阀管段 P6（以 P7 为故障管段）-> PLAN_INVALID / CLOSE_PIPE_NO_VALVE
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=["P6"]),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.steps[0].error.code, PLAN_INVALID)
        codes = {i.code for i in result.steps[0].error.issues}
        self.assertIn(CLOSE_PIPE_NO_VALVE, codes)

        # 提交口径：上一步已关的阀门不得重复关闭
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=["P4"]),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step_index, 2)
        self.assertEqual(result.steps[1].error.code, PLAN_INVALID)
        codes = {i.code for i in result.steps[1].error.issues}
        self.assertIn(CLOSE_PIPE_DUPLICATE, codes)

    def test_restore_rewater_risk_stops_drill(self):
        nodes, pipes = build_drill_network()
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=[]),
            # 复供 T1 重开 P4 -> 待修复故障 P7 的 D 端重新进水
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T1"),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step_index, 3)
        error = result.steps[2].error
        self.assertEqual(error.code, RESTORE_UNSAFE)
        self.assertEqual(error.blocks[0].fault_pipe_id, "P7")
        self.assertEqual(error.blocks[0].reachable_endpoints, ["D"])
        self.assertIsNone(result.final)

    def test_restore_real_event_and_reference_errors(self):
        nodes, pipes = build_drill_network()
        # 现存待修复事件 1（故障 P6，已关 P4）与已复供事件 2
        pipes["P4"] = Pipe("P4", "B", "D", has_valve=True, valve_closed=True)
        real_events = {
            1: {"fault_pipe_id": "P6", "closed_pipe_ids": ["P4"], "status": "open"},
            2: {"fault_pipe_id": "P7", "closed_pipe_ids": [], "status": "restored"},
        }
        # 复供现存事件 1：重开 P4，U3/U4 恢复
        result = simulate_drill(
            nodes, pipes, real_events, [DrillStep(type=DRILL_STEP_RESTORE, event_id=1)]
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.steps[0].restored_sink_ids, ["U3", "U4"])
        self.assertEqual(result.steps[0].reopened_pipe_ids, ["P4"])
        self.assertEqual(result.final.open_faults, [])
        self.assertEqual(result.final.supplied_sink_ids, ["U1", "U3", "U4"])

        # 已复供事件再次复供 -> EVENT_ALREADY_RESTORED
        result = simulate_drill(
            nodes, pipes, real_events, [DrillStep(type=DRILL_STEP_RESTORE, event_id=2)]
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.steps[0].error.code, EVENT_ALREADY_RESTORED)

        # 不存在的事件编号 / 未产生的临时引用 -> EVENT_NOT_FOUND
        for step in (
            DrillStep(type=DRILL_STEP_RESTORE, event_id=99),
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T9"),
        ):
            result = simulate_drill(nodes, pipes, real_events, [step])
            self.assertFalse(result.ok)
            self.assertEqual(result.steps[0].error.code, EVENT_NOT_FOUND)

    def test_restore_temp_event_twice_in_one_drill(self):
        nodes, pipes = build_drill_network()
        steps = [
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T1"),
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T1"),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step_index, 3)
        self.assertEqual(result.steps[2].error.code, EVENT_ALREADY_RESTORED)

    def test_restore_forward_temp_reference_not_found(self):
        nodes, pipes = build_drill_network()
        # 第 1 步引用第 2 步才会产生的临时事件 T2 -> 不存在
        steps = [
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T2"),
            DrillStep(type=DRILL_STEP_ISOLATION, fault_pipe_id="P6", close_pipe_ids=["P4"]),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        self.assertEqual(result.failed_step_index, 1)
        self.assertEqual(result.steps[0].error.code, EVENT_NOT_FOUND)

    def test_source_switch_invalid_and_unsafe(self):
        nodes, pipes = build_drill_two_source_network()
        # 结构非法：编号不存在 / 非水源 / 同批重复 -> SOURCE_SWITCH_INVALID
        steps = [
            DrillStep(
                type=DRILL_STEP_SOURCE_SWITCH,
                targets=[("SX", False), ("U1", False), ("S1", False), ("S1", True)],
            ),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertFalse(result.ok)
        error = result.steps[0].error
        self.assertEqual(error.code, SOURCE_SWITCH_INVALID)
        codes = {i.code for i in error.issues}
        self.assertEqual(
            codes, {SOURCE_NOT_FOUND, SOURCE_NOT_SWITCHABLE, SOURCE_TARGET_DUPLICATE}
        )

        # S2 停运、故障 Q2 待修复（关 Q1）状态下恢复 S2 -> Q2 的 C 端重新进水
        nodes["S2"] = Node("S2", "source", running=False)
        pipes["Q1"] = Pipe("Q1", "S1", "C", has_valve=True, valve_closed=True)
        real_events = {
            1: {"fault_pipe_id": "Q2", "closed_pipe_ids": ["Q1"], "status": "open"},
        }
        steps = [DrillStep(type=DRILL_STEP_SOURCE_SWITCH, targets=[("S2", True)])]
        result = simulate_drill(nodes, pipes, real_events, steps)
        self.assertFalse(result.ok)
        error = result.steps[0].error
        self.assertEqual(error.code, SOURCE_SWITCH_UNSAFE)
        self.assertEqual(error.blocks[0].fault_pipe_id, "Q2")
        self.assertEqual(error.blocks[0].reachable_endpoints, ["C"])

    def test_source_switch_noop_step_is_ok(self):
        nodes, pipes = build_drill_two_source_network()
        steps = [DrillStep(type=DRILL_STEP_SOURCE_SWITCH, targets=[("S1", True)])]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertTrue(result.ok)
        self.assertFalse(result.steps[0].changed)
        self.assertEqual(result.steps[0].affected_sink_ids, [])
        self.assertEqual(result.steps[0].restored_sink_ids, [])

    def test_drill_does_not_mutate_inputs(self):
        nodes, pipes = build_drill_network()
        steps = [
            DrillStep(
                type=DRILL_STEP_ISOLATION, fault_pipe_id="P7", close_pipe_ids=["P4", "P8"]
            ),
            DrillStep(type=DRILL_STEP_SOURCE_SWITCH, targets=[("S", False)]),
            DrillStep(type=DRILL_STEP_RESTORE, temp_event_ref="T1"),
        ]
        result = simulate_drill(nodes, pipes, {}, steps)
        self.assertTrue(result.ok)
        # 输入的节点/管段对象均未被修改（全部状态演进发生在内部副本上）
        self.assertTrue(all(not p.valve_closed for p in pipes.values()))
        self.assertTrue(nodes["S"].running)


# ---------------------------------------------------- 临时旁通管评估


def build_bypass_fault_state():
    """故障态演示管网：P6 为待修复故障，P4 已关阀隔离（D 片区断供）。

    S --P1(v)-- A --P2(v)-- B --P4(v,关)-- D --P6(无阀,待修复)-- U3
                 |           |
               P3(v)       P5(v)
                 |           |
                 U1          U2
    """
    nodes, pipes = build_demo()
    p = pipes["P4"]
    pipes["P4"] = Pipe(p.id, p.from_node, p.to_node, p.has_valve, True, p.name)
    return nodes, pipes


class BypassEvaluationTests(unittest.TestCase):
    def _caps(self, pipes, overrides=None):
        overrides = overrides or {}
        return [(pid, overrides.get(pid, 100)) for pid in pipes]

    def test_baseline_and_safe_bypass_delivery(self):
        nodes, pipes = build_demo()
        # P3 瓶颈 6：基线 U1=6；旁通 S->U1 容量 4 后 U1=10
        result = evaluate_bypass(
            nodes, pipes, "S", "U1", 4,
            [("S", 1000)],
            self._caps(pipes, {"P3": 6}),
            [("U1", 10, 1), ("U2", 10, 2), ("U3", 10, 2)],
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.blocks, [])
        self.assertIsNotNone(result.bypass)
        self.assertEqual(
            [(e.sink_id, e.delivered) for e in result.baseline.entries],
            [("U1", 6), ("U2", 10), ("U3", 10)],
        )
        self.assertEqual(result.baseline.total_delivered, 26)
        self.assertEqual(
            [(e.sink_id, e.delivered) for e in result.bypass.entries],
            [("U1", 10), ("U2", 10), ("U3", 10)],
        )
        self.assertEqual(result.bypass.total_delivered, 30)
        self.assertTrue(result.bypass.all_satisfied)

    def test_bypass_parallel_to_existing_pipe(self):
        nodes, pipes = build_demo()
        # 旁通 S->A 与现有管段 P1 并联：P1 只能过 5，并联后再多 5
        result = evaluate_bypass(
            nodes, pipes, "S", "A", 5,
            [("S", 1000)],
            self._caps(pipes, {"P1": 5}),
            [("U1", 10, 1)],
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.baseline.total_delivered, 5)
        self.assertEqual(result.bypass.total_delivered, 10)

    def test_unsafe_when_bypass_rewaters_pending_fault(self):
        nodes, pipes = build_bypass_fault_state()
        blocked = frozenset({"P4", "P6"})
        # 旁通 A->D 使待修复故障 P6 的 D 端重新进水：判不可用，不核算旁通交付
        result = evaluate_bypass(
            nodes, pipes, "A", "D", 50,
            [("S", 1000)],
            self._caps(pipes, {"P3": 6}),
            [("U1", 10, 1), ("U2", 10, 2), ("U3", 10, 2)],
            blocked_pipe_ids=blocked,
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertFalse(result.safe)
        self.assertIsNone(result.bypass)
        self.assertEqual(len(result.blocks), 1)
        self.assertEqual(result.blocks[0].fault_pipe_id, "P6")
        self.assertEqual(result.blocks[0].reachable_endpoints, ["D"])
        self.assertEqual(result.blocks[0].as_dict()["code"], FAULT_REWATER_RISK)
        # 基线仍按当前状态给出（U3 断供）
        self.assertEqual(
            [(e.sink_id, e.delivered) for e in result.baseline.entries],
            [("U1", 6), ("U2", 10), ("U3", 0)],
        )

    def test_unsafe_reports_both_endpoints(self):
        # S --P1-- M --P2(v,关)-- X --P5-- Z --P6-- Y；故障 PF：X—Y
        # 旁通 M->X 接入后 X 经旁通进水，Y 经 P5/P6 进水：两端均危险
        nodes = {
            "S": Node("S", "source"),
            "M": Node("M", "junction"),
            "X": Node("X", "junction"),
            "Z": Node("Z", "junction"),
            "Y": Node("Y", "junction"),
            "U": Node("U", "sink"),
        }
        pipes = {
            "P1": Pipe("P1", "S", "M", has_valve=True),
            "P2": Pipe("P2", "M", "X", has_valve=True, valve_closed=True),
            "P5": Pipe("P5", "X", "Z"),
            "P6": Pipe("P6", "Z", "Y"),
            "P9": Pipe("P9", "M", "U"),
            "PF": Pipe("PF", "X", "Y"),
        }
        result = evaluate_bypass(
            nodes, pipes, "M", "X", 10,
            [("S", 100)],
            self._caps(pipes),
            [("U", 5, 1)],
            blocked_pipe_ids=frozenset({"P2", "PF"}),
            pending_fault_pipe_ids=frozenset({"PF"}),
        )
        self.assertFalse(result.safe)
        self.assertEqual(result.blocks[0].fault_pipe_id, "PF")
        self.assertEqual(result.blocks[0].reachable_endpoints, ["X", "Y"])

    def test_safe_bypass_in_fault_state_away_from_fault(self):
        nodes, pipes = build_bypass_fault_state()
        blocked = frozenset({"P4", "P6"})
        # 旁通 S->U1 不触碰故障区：安全；U1 提升，U3 仍断供
        result = evaluate_bypass(
            nodes, pipes, "S", "U1", 4,
            [("S", 1000)],
            self._caps(pipes, {"P3": 6}),
            [("U1", 10, 1), ("U2", 10, 2), ("U3", 10, 2)],
            blocked_pipe_ids=blocked,
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertTrue(result.safe)
        self.assertEqual(
            [(e.sink_id, e.delivered) for e in result.bypass.entries],
            [("U1", 10), ("U2", 10), ("U3", 0)],
        )

    def test_zero_capacity_bypass_still_unsafe_topologically(self):
        nodes, pipes = build_bypass_fault_state()
        # 安全判定为拓扑可达（与复供/水源切换同口径），与旁通容量大小无关
        result = evaluate_bypass(
            nodes, pipes, "A", "D", 0,
            [("S", 1000)],
            self._caps(pipes),
            [("U1", 1, 1)],
            blocked_pipe_ids=frozenset({"P4", "P6"}),
            pending_fault_pipe_ids=frozenset({"P6"}),
        )
        self.assertFalse(result.safe)
        self.assertIsNone(result.bypass)

    def test_zero_capacity_bypass_safe_and_no_gain(self):
        nodes, pipes = build_demo()
        result = evaluate_bypass(
            nodes, pipes, "S", "U1", 0,
            [("S", 1000)],
            self._caps(pipes, {"P3": 6}),
            [("U1", 10, 1)],
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.baseline.total_delivered, 6)
        self.assertEqual(result.bypass.total_delivered, 6)

    def test_level_deltas_follow_allocate_rules(self):
        nodes, pipes = build_demo()
        # P2 瓶颈 15：基线等级 2 合计 15（U2=10、U3=5）；
        # 旁通 A->U3 容量 5 后等级 2 合计 20（U3=10），等级 1 不变
        result = evaluate_bypass(
            nodes, pipes, "A", "U3", 5,
            [("S", 1000)],
            self._caps(pipes, {"P2": 15}),
            [("U1", 10, 1), ("U2", 10, 2), ("U3", 10, 2)],
        )
        self.assertTrue(result.safe)
        self.assertEqual(
            [(l.level, l.delivered) for l in result.baseline.levels],
            [(1, 10), (2, 15)],
        )
        self.assertEqual(
            [(l.level, l.delivered) for l in result.bypass.levels],
            [(1, 10), (2, 20)],
        )
        self.assertEqual(
            [(e.sink_id, e.delivered) for e in result.bypass.entries],
            [("U1", 10), ("U2", 10), ("U3", 10)],
        )

    def test_evaluate_bypass_does_not_mutate_inputs(self):
        nodes, pipes = build_demo()
        result = evaluate_bypass(
            nodes, pipes, "S", "U1", 4,
            [("S", 1000)],
            self._caps(pipes),
            [("U1", 10, 1)],
        )
        self.assertTrue(result.safe)
        # 虚拟旁通管段只存在于内存副本：输入的管网不被修改
        self.assertNotIn("__BYPASS__", pipes)
        self.assertEqual(set(pipes), {"P1", "P2", "P3", "P4", "P5", "P6"})
        self.assertTrue(all(not p.valve_closed for p in pipes.values()))

    def test_virtual_id_avoids_collision_with_existing_pipe(self):
        nodes, pipes = build_demo()
        # 现有管段占用了默认虚拟编号：评估仍正常（内部自动避让）
        pipes["__BYPASS__"] = Pipe("__BYPASS__", "A", "B")
        result = evaluate_bypass(
            nodes, pipes, "S", "U1", 4,
            [("S", 1000)],
            self._caps(pipes, {"P3": 6}),
            [("U1", 10, 1)],
        )
        self.assertTrue(result.safe)
        self.assertEqual(result.bypass.total_delivered, 10)

    def test_validate_bypass_request_endpoint_issues(self):
        nodes, pipes = build_demo()
        caps = self._caps(pipes)
        # 端点不存在
        issues = validate_bypass_request(
            nodes, pipes, "XX", "U1", [("S", 1)], caps, [("U1", 1)]
        )
        self.assertEqual([i.code for i in issues], [BYPASS_ENDPOINT_NOT_FOUND])
        # 两端相同
        issues = validate_bypass_request(
            nodes, pipes, "A", "A", [("S", 1)], caps, [("U1", 1)]
        )
        self.assertEqual([i.code for i in issues], [BYPASS_ENDPOINT_SAME])
        # 两端相同且不存在：SAME + NOT_FOUND（同一缺失端点不重复报告）
        issues = validate_bypass_request(
            nodes, pipes, "XX", "XX", [("S", 1)], caps, [("U1", 1)]
        )
        self.assertEqual(
            [i.code for i in issues],
            [BYPASS_ENDPOINT_SAME, BYPASS_ENDPOINT_NOT_FOUND],
        )
        # 编号集合问题沿用分档交付同一套问题代码
        issues = validate_bypass_request(
            nodes, pipes, "A", "D", [], caps, [("U1", 1)]
        )
        self.assertEqual([i.code for i in issues], [DELIVERY_SOURCE_MISSING])
        # 合法请求（允许旁通与现有管段并联，端点类型不限）
        issues = validate_bypass_request(
            nodes, pipes, "S", "A", [("S", 1)], caps, [("U1", 1)]
        )
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
