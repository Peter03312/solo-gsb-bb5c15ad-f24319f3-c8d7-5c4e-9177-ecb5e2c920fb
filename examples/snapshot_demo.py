#!/usr/bin/env python3
"""状态快照回看调用示例（仅依赖标准库）。

用法：
    python3 examples/snapshot_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

调度员按操作顺序回看供水状态：整网导入、隔离提交、复供确认、水源切换
每次**实际改变状态**时，系统在同一事务内冻结一条操作后快照
（不可复用编号 + 操作类型 + 当时管网版本 + 关联事件编号 + 冻结状态）。
本脚本依次演示：
  1. 整网导入            -> 快照 1（network_import，版本 1）
  2. 隔离提交（关 P4）    -> 快照 2（isolation_submit，事件 1，版本 2；U3 失供）
  3. 相邻对照 1 -> 2     -> P4 阀位变化、新增待修复故障 P6、供水点 U3 移除
  4. 失败操作不留快照     -> 版本冲突 409 / 不可隔离 422 / 空切换均无新快照
  5. 复供确认            -> 快照 3（restore_confirm，事件 1，版本 4；U3 恢复）
  6. 水源停运/恢复       -> 快照 4/5（source_switch），对照含水源运行状态净变化
  7. 按编号读取快照      -> 节点、水源运行状态、管段阀位、待修复故障、可达用水点
  8. 整网重新导入        -> 版本重置为 1，但快照编号继续递增、不覆盖此前快照
  9. 未知编号明确报错     -> 404 SNAPSHOT_NOT_FOUND；非相邻对照 -> 422
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
    os.path.dirname(os.path.abspath(__file__)), "sample_network.json"
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


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 整网导入：同一事务冻结快照 1（network_import，版本 1）
    status, body = call("POST", "/api/network/import", network)
    show("1. 整网导入（预期版本 1、快照 1）", status, body)
    assert status == 200 and body["version"] == 1, body
    assert body["snapshot_id"] == 1, body

    # 2. 隔离提交：关 P4 隔离故障 P6 -> 快照 2（isolation_submit，事件 1，版本 2）
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("2. 隔离提交（预期版本 2、事件 1、快照 2，U3 失供）", status, body)
    assert status == 200, body
    assert body["event_id"] == 1 and body["snapshot_id"] == 2, body
    assert body["affected_sink_ids"] == ["U3"], body

    # 3. 相邻快照对照 1 -> 2：阀位 / 待修复故障 / 供水点净变化
    status, body = call("GET", "/api/snapshots/2/diff")
    show("3. 快照对照 1 -> 2（P4 关闭、新增待修复 P6、U3 移出供水点）", status, body)
    assert status == 200, body
    assert body["base_snapshot_id"] == 1 and body["target_snapshot_id"] == 2, body
    assert body["valve_positions"]["changed"] == [
        {"pipe_id": "P4", "valve_closed_before": False, "valve_closed_after": True}
    ], body
    assert body["open_faults"]["added"] == [
        {"event_id": 1, "fault_pipe_id": "P6"}
    ], body
    assert body["supplied_sinks"]["removed"] == ["U3"], body

    # 4. 失败操作不留快照：版本冲突 409 / 不可隔离 422 / 水源空切换
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
    )
    show("4a. 过期版本提交（预期 409 VERSION_CONFLICT，不留快照）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body

    # 注意：版本 2 时 P4 已关，故障 P6 本就处于隔离态（不能再用它演示拒绝）。
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P5", "close_pipe_ids": ["P3"], "version": 2},
    )
    show("4b. 不可隔离方案提交（关 P3 无法隔离 P5，预期 422 NOT_ISOLATABLE，不留快照）", status, body)
    assert status == 422 and body["error"]["code"] == "NOT_ISOLATABLE", body

    status, body = call(
        "POST",
        "/api/sources/switch",
        {"targets": [{"source_id": "S", "running": True}], "version": 2},
    )
    show("4c. 水源目标状态已满足（预期 changed=false、无快照）", status, body)
    assert status == 200 and body["changed"] is False, body
    assert body["snapshot_id"] is None, body

    ids_before = [s["id"] for s in call("GET", "/api/snapshots")[1]]
    assert ids_before == [1, 2], ids_before

    # 5. 复供确认：重开 P4 -> 快照 3（restore_confirm，事件 1，版本 3；U3 恢复）
    status, body = call(
        "POST", "/api/isolation/events/1/restore", {"version": 2}
    )
    show("5. 复供确认（预期版本 3、快照 3，U3 恢复供水）", status, body)
    assert status == 200 and body["snapshot_id"] == 3, body
    assert body["restored_sink_ids"] == ["U3"], body

    # 6a. 停运水源 S（允许全网无运行水源）-> 快照 4（source_switch，版本 4）
    status, body = call(
        "POST",
        "/api/sources/switch",
        {"targets": [{"source_id": "S", "running": False}], "version": 3},
    )
    show("6a. 停运唯一水源 S（预期版本 4、快照 4，全部用水点失供）", status, body)
    assert status == 200 and body["snapshot_id"] == 4, body
    assert body["affected_sink_ids"] == ["U1", "U2", "U3"], body

    # 对照 3 -> 4：水源状态 S true->false；供水点 U1/U2/U3 全部移除
    status, body = call("GET", "/api/snapshots/4/diff")
    show("6b. 快照对照 3 -> 4（S 停运，供水点全部移除）", status, body)
    assert status == 200, body
    assert body["source_states"]["changed"] == [
        {"source_id": "S", "running_before": True, "running_after": False}
    ], body
    assert body["supplied_sinks"]["removed"] == ["U1", "U2", "U3"], body

    # 6c. 恢复 S -> 快照 5（版本 5）；对照 4 -> 5 供水点全部恢复
    status, body = call(
        "POST",
        "/api/sources/switch",
        {"targets": [{"source_id": "S", "running": True}], "version": 4},
    )
    show("6c. 恢复水源 S（预期版本 5、快照 5）", status, body)
    assert status == 200 and body["snapshot_id"] == 5, body
    status, body = call("GET", "/api/snapshots/5/diff")
    assert body["source_states"]["changed"] == [
        {"source_id": "S", "running_before": False, "running_after": True}
    ], body
    assert body["supplied_sinks"]["added"] == ["U1", "U2", "U3"], body

    # 7. 按编号读取快照 2（历史结果，不被当前管网/后续事件回填）
    status, body = call("GET", "/api/snapshots/2")
    show("7. 读取快照 2（当时：P4 已关、P6 待修复、U3 不可达）", status, body)
    assert status == 200, body
    assert body["op_type"] == "isolation_submit", body
    assert body["network_version"] == 2 and body["event_id"] == 1, body
    assert body["state"]["open_faults"] == [
        {"event_id": 1, "fault_pipe_id": "P6"}
    ], body
    assert body["state"]["supplied_sink_ids"] == ["U1", "U2"], body
    p4 = next(p for p in body["state"]["pipes"] if p["id"] == "P4")
    assert p4["valve_closed"] is True, body

    # 8. 整网重新导入：版本重置为 1，但此前快照不被覆盖，编号继续递增
    status, body = call("POST", "/api/network/import", network)
    show("8. 整网重新导入（版本重置 1；快照编号继续递增为 6，不覆盖 1-5）", status, body)
    assert status == 200 and body["version"] == 1, body
    assert body["snapshot_id"] == 6, body
    status, body = call("GET", "/api/snapshots?limit=100")
    assert [s["id"] for s in body] == [1, 2, 3, 4, 5, 6], body
    # 快照 2 仍是旧网旧版本内容
    old = call("GET", "/api/snapshots/2")[1]
    assert old["network_version"] == 2 and len(old["state"]["nodes"]) == 7, old

    # 9. 未知编号 / 非相邻对照
    status, body = call("GET", "/api/snapshots/999")
    show("9a. 未知快照编号（预期 404 SNAPSHOT_NOT_FOUND）", status, body)
    assert status == 404 and body["error"]["code"] == "SNAPSHOT_NOT_FOUND", body

    status, body = call("GET", "/api/snapshots/6/diff?base_id=1")
    show("9b. 非相邻快照对照（预期 422 SNAPSHOT_NOT_ADJACENT）", status, body)
    assert status == 422 and body["error"]["code"] == "SNAPSHOT_NOT_ADJACENT", body

    print("\n全部状态快照回看示例执行完成。")


if __name__ == "__main__":
    main()
