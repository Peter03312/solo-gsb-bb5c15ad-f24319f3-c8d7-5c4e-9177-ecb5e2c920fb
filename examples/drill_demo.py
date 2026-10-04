#!/usr/bin/env python3
"""调度演练调用示例（仅依赖标准库）。

用法：
    python3 examples/drill_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/restore_network.json，两条可叠加故障）：

    S(水源) --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3(用水点)
                        |                     |
                      P3[阀]                P7(无阀)-- E --P8[阀]-- U4(用水点)
                        |
                       U1(用水点)

调度演练（POST /api/drill）在执行前只读检查一串有序的隔离 / 复供 / 水源切换
操作：以当前状态为起点逐步套用既有提交校验，复供可引用现存待修复事件
（event_id）或本次演练先前隔离产生的临时事件（temp_event_ref，形如 T<步骤
序号>）；每步的新增失供 / 恢复供水用水点相对上一步计算；版本过期整体 409
拒绝；任一步非法或不安全即标明首个失败步骤并停止后续演练；全程不改真实
版本、阀位、事件与快照。

脚本依次演示：
  1. 导入管网（版本 1）
  2. 五步连贯演练全部成功：隔离 -> 复供临时事件 -> 再隔离 -> 停运 -> 恢复水源，
     查看每步失供/恢复与最终运行水源、阀位、待修复故障、通水用水点
  3. 确认演练只读：版本/阀位/事件/快照均未变
  4. 真实提交一条隔离事件（事件 1，版本 2）
  5. 演练复供现存待修复事件（event_id=1）-> 成功
  6. 演练“隔离区内故障 + 复供外层事件”-> 第 2 步重新进水风险（RESTORE_UNSAFE）
  7. 过期版本演练 -> 409 VERSION_CONFLICT
  8. 引用不存在的事件编号 / 临时引用 -> EVENT_NOT_FOUND
  9. 再次确认演练只读，且既有单步接口契约不变（按版本 2 可正常复供）
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "restore_network.json"
)


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def drill(steps: list[dict], version: int) -> tuple[int, dict]:
    return call("POST", "/api/drill", {"version": version, "steps": steps})


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网（版本 1，水源 S 初始运行）
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body
    # 记录演练前基线（快照编号全局单调、整网导入也不复用，故按相对数量核对）
    _, snapshots = call("GET", "/api/snapshots")
    snapshot_baseline = len(snapshots)

    # 2. 五步连贯演练（全部成功）：
    #    T1 隔离 P6（关 P4）-> 复供 T1 -> T3 隔离 P7（关 P4/P8）-> 停运 S -> 恢复 S
    steps = [
        {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]},
        {"type": "restore", "temp_event_ref": "T1"},
        {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": ["P4", "P8"]},
        {"type": "source_switch", "targets": [{"source_id": "S", "running": False}]},
        {"type": "source_switch", "targets": [{"source_id": "S", "running": True}]},
    ]
    status, body = drill(steps, version=1)
    show("2. 五步连贯演练（预期 ok=true，临时事件 T1/T3）", status, body)
    assert status == 200 and body["ok"] is True, body
    s1, s2, s3, s4, s5 = body["steps"]
    assert s1["temp_event_ref"] == "T1" and s1["affected_sink_ids"] == ["U3", "U4"], s1
    assert s2["restored_sink_ids"] == ["U3", "U4"], s2
    assert s3["temp_event_ref"] == "T3" and s3["affected_sink_ids"] == ["U3", "U4"], s3
    assert s4["affected_sink_ids"] == ["U1"] and s4["changed"] is True, s4
    assert s5["restored_sink_ids"] == ["U1"], s5
    final = body["final"]
    assert final["sources"] == [{"source_id": "S", "running": True}], final
    valves = {v["pipe_id"]: v["valve_closed"] for v in final["valves"]}
    assert valves["P4"] is True and valves["P8"] is True, final
    assert final["open_faults"] == [
        {"event_id": None, "temp_event_ref": "T3", "fault_pipe_id": "P7"}
    ], final
    assert final["supplied_sink_ids"] == ["U1"], final

    # 3. 演练只读：版本仍为 1、无事件、快照数量与演练前一致
    status, net = call("GET", "/api/network")
    assert net["version"] == 1, net
    assert all(not p["valve_closed"] for p in net["pipes"] if p["has_valve"]), net
    status, events = call("GET", "/api/isolation/events")
    assert events == [], events
    status, snapshots = call("GET", "/api/snapshots")
    assert len(snapshots) == snapshot_baseline, snapshots
    print("\n[只读核对] 演练后版本/阀位/事件/快照均未变化")

    # 4. 真实提交一条隔离事件（故障 P6，关 P4）-> 事件 1，版本 2
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("4. 真实提交隔离事件（事件 1，版本 2）", status, body)
    assert status == 200 and body["event_id"] == 1 and body["version"] == 2, body

    # 5. 演练复供现存待修复事件（event_id=1）-> 成功，U3/U4 恢复
    status, body = drill([{"type": "restore", "event_id": 1}], version=2)
    show("5. 演练复供现存事件 1（预期 ok=true，U3/U4 恢复）", status, body)
    assert status == 200 and body["ok"] is True, body
    assert body["steps"][0]["restored_sink_ids"] == ["U3", "U4"], body
    assert body["final"]["open_faults"] == [], body
    assert body["final"]["supplied_sink_ids"] == ["U1", "U3", "U4"], body

    # 6. 演练“隔离区内故障 P7 + 复供外层事件 1”：
    #    第 2 步重开 P4 会使临时事件 T1 的故障管段 P7 的 D 端重新进水 -> 停止
    steps = [
        {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": []},
        {"type": "restore", "event_id": 1},
        {"type": "source_switch", "targets": [{"source_id": "S", "running": False}]},
    ]
    status, body = drill(steps, version=2)
    show("6. 复供外层事件使区内故障重新进水（预期第 2 步 RESTORE_UNSAFE）", status, body)
    assert status == 200 and body["ok"] is False, body
    assert body["failed_step_index"] == 2, body
    assert len(body["steps"]) == 2, body  # 后续步骤不再演练
    error = body["steps"][1]["error"]
    assert error["code"] == "RESTORE_UNSAFE", error
    block = error["block_reasons"][0]
    assert block["pipe_id"] == "P7" and block["reachable_endpoints"] == ["D"], block
    assert body["final"] is None, body

    # 7. 过期版本演练 -> 409 VERSION_CONFLICT（返回当前版本并拒绝演练）
    status, body = drill([{"type": "restore", "event_id": 1}], version=1)
    show("7. 过期版本演练（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body
    assert body["error"]["current_version"] == 2, body

    # 8. 引用不存在的事件编号 / 未产生的临时引用 -> 首步 EVENT_NOT_FOUND
    for step in (
        {"type": "restore", "event_id": 999},
        {"type": "restore", "temp_event_ref": "T9"},
    ):
        status, body = drill([step], version=2)
        show(f"8. 引用不存在的事件 {step}（预期 EVENT_NOT_FOUND）", status, body)
        assert status == 200 and body["ok"] is False, body
        assert body["steps"][0]["error"]["code"] == "EVENT_NOT_FOUND", body

    # 9. 再次确认演练只读：版本 2、事件 1 仍待修复、快照仅新增真实提交那一条；
    #    既有单步接口契约不变：按版本 2 可正常复供事件 1
    status, net = call("GET", "/api/network")
    assert net["version"] == 2, net
    status, events = call("GET", "/api/isolation/events")
    assert len(events) == 1 and events[0]["status"] == "open", events
    status, snapshots = call("GET", "/api/snapshots")
    assert len(snapshots) == snapshot_baseline + 1, snapshots
    print("\n[只读核对] 全部演练后版本/事件/快照均未变化")

    status, body = call("POST", "/api/isolation/events/1/restore", {"version": 2})
    show("9. 演练后按既有接口真实复供事件 1（预期成功，版本 3）", status, body)
    assert status == 200 and body["version"] == 3, body
    assert body["restored_sink_ids"] == ["U3", "U4"], body

    print("\n全部调度演练示例执行完成。")


if __name__ == "__main__":
    main()
