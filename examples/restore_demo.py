#!/usr/bin/env python3
"""故障隔离 -> 复供完整调用示例（仅依赖标准库）。

用法：
    python3 examples/restore_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/restore_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀,故障1)-- U3
                   |                       |
                 P3[阀]                   P7(无阀,故障2)-- E --P8[阀]-- U4
                   |
                  U1

脚本依次演示：
  1. 导入管网（版本 1）
  2. 提交隔离事件 1：故障 P6，关 P4 隔离整个 D 片区（U3/U4 失供）-> 版本 2
  3. 在已隔离区内提交隔离事件 2：故障 P7，无需再关阀门 -> 版本 3
  4. 复供预演事件 1：重开 P4 会让待修复的 P7 的 D 端重新进水 -> safe=false
  5. 复供确认事件 1：422 RESTORE_UNSAFE，明确拒绝且无部分变更
  6. 复供预演事件 2：片区内事件，无阀可开，P4 仍保护故障 -> safe=true
  7. 复供确认事件 2：成功，版本 4
  8. 再次复供事件 2：409 EVENT_ALREADY_RESTORED
  9. 过期版本复供事件 1：409 VERSION_CONFLICT
 10. 用最新版本复供事件 1：成功，U3/U4 恢复，版本 5
 11. 复供不存在的事件：404 EVENT_NOT_FOUND
 12. 查看历史（含 open / restored 状态）
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


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入复供演示管网", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 提交隔离事件 1：故障 P6，关 P4
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("2. 提交隔离事件 1（故障 P6，关 P4）", status, body)
    assert status == 200 and body["event_id"] == 1, body
    event1 = body["event_id"]

    # 3. 提交隔离事件 2：已隔离区内的故障 P7，空方案
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P7", "close_pipe_ids": [], "version": 2},
    )
    show("3. 提交隔离事件 2（区内故障 P7，空方案）", status, body)
    assert status == 200 and body["event_id"] == 2, body
    event2 = body["event_id"]

    # 4. 复供预演事件 1：应报重新进水风险
    status, body = call(
        "POST", f"/api/isolation/events/{event1}/restore/preview", {"version": 3}
    )
    show("4. 复供预演事件 1（预期 safe=false，阻断于 P7）", status, body)
    assert status == 200 and body["safe"] is False, body
    assert body["block_reasons"][0]["code"] == "FAULT_REWATER_RISK"
    assert body["block_reasons"][0]["pipe_id"] == "P7"

    # 5. 强行确认事件 1 -> 422 RESTORE_UNSAFE
    status, body = call(
        "POST", f"/api/isolation/events/{event1}/restore", {"version": 3}
    )
    show("5. 确认复供事件 1（预期 422 RESTORE_UNSAFE）", status, body)
    assert status == 422 and body["error"]["code"] == "RESTORE_UNSAFE", body

    # 6. 复供预演事件 2：安全
    status, body = call(
        "POST", f"/api/isolation/events/{event2}/restore/preview", {"version": 3}
    )
    show("6. 复供预演事件 2（预期 safe=true）", status, body)
    assert status == 200 and body["safe"] is True, body

    # 7. 确认复供事件 2 -> 版本 4
    status, body = call(
        "POST", f"/api/isolation/events/{event2}/restore", {"version": 3}
    )
    show("7. 确认复供事件 2（预期成功，版本 4）", status, body)
    assert status == 200 and body["version"] == 4, body

    # 8. 重复复供事件 2 -> 409
    status, body = call(
        "POST", f"/api/isolation/events/{event2}/restore", {"version": 4}
    )
    show("8. 重复复供事件 2（预期 409 EVENT_ALREADY_RESTORED）", status, body)
    assert status == 409 and body["error"]["code"] == "EVENT_ALREADY_RESTORED", body

    # 9. 过期版本复供事件 1 -> 409
    status, body = call(
        "POST", f"/api/isolation/events/{event1}/restore", {"version": 3}
    )
    show("9. 过期版本复供事件 1（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body

    # 10. 最新版本复供事件 1 -> 成功，版本 5，U3/U4 恢复
    status, body = call(
        "POST", f"/api/isolation/events/{event1}/restore", {"version": 4}
    )
    show("10. 确认复供事件 1（预期成功，U3/U4 恢复，版本 5）", status, body)
    assert status == 200 and body["version"] == 5, body
    assert body["restored_sink_ids"] == ["U3", "U4"], body

    # 11. 不存在的事件 -> 404
    status, body = call(
        "POST", "/api/isolation/events/999/restore", {"version": 5}
    )
    show("11. 复供不存在的事件（预期 404 EVENT_NOT_FOUND）", status, body)
    assert status == 404 and body["error"]["code"] == "EVENT_NOT_FOUND", body

    # 12. 历史
    status, body = call("GET", "/api/isolation/events")
    show("12. 隔离/复供历史", status, body)
    assert status == 200
    statuses = {e["id"]: e["status"] for e in body}
    assert statuses == {1: "restored", 2: "restored"}, statuses

    print("\n全部复供示例执行完成。")


if __name__ == "__main__":
    main()
