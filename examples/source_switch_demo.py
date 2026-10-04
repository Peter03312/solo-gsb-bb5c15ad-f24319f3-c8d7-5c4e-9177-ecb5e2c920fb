#!/usr/bin/env python3
"""水源停运 / 恢复调用示例（仅依赖标准库）。

用法：
    python3 examples/source_switch_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sources_network.json，双水源联网）：

    S1(水源) --Q1[阀]-- C(联络点) --Q2[阀]-- U1(用水点)
                        |
    S2(水源) --Q3[阀]-- C --Q4[阀]-- U2(用水点)

脚本依次演示：
  1. 导入管网（版本 1，两座水源初始均运行，读网可见 running=true）
  2. 预演停运 S1：S2 经 C 顶供，无用水点失供（只读，不改状态）
  3. 确认停运 S1 -> 版本 2；隔离 / 关阀建议此时只按运行水源 S2 计算
  4. “目标状态已满足”不推进版本（停运已停运的 S1）
  5. 同时停运两座水源（允许全网无运行水源）-> U1/U2 全部新增失供
  6. 恢复 S1 -> U1/U2 恢复供水
  7. 保持 S2 停运，隔离故障管段 Q2：只切断运行水源 S1（关 Q1，事件 1）
  8. 预演恢复 S2：待修复故障 Q2 的 C 端将经 Q3 重新进水 -> safe=false
  9. 强行确认恢复 S2 -> 422 SOURCE_SWITCH_UNSAFE（无部分写入）
 10. 过期版本确认 -> 409 VERSION_CONFLICT；编号无效/非水源/重复 -> 422
 11. 复供故障事件（重开 Q1）后，恢复 S2 不再有重新进水风险 -> 成功
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
    os.path.dirname(os.path.abspath(__file__)), "sources_network.json"
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


def switch(targets: list[list], version: int, confirm: bool = True):
    path = "/api/sources/switch" if confirm else "/api/sources/switch/preview"
    payload = {
        "targets": [
            {"source_id": sid, "running": running} for sid, running in targets
        ],
        "version": version,
    }
    return call("POST", path, payload)


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网：水源初始视为运行
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入双水源管网", status, body)
    assert status == 200 and body["version"] == 1, body

    status, body = call("GET", "/api/network")
    sources = {n["id"]: n["running"] for n in body["nodes"] if n["type"] == "source"}
    print(f"\n[读网] 水源运行状态：{sources}")
    assert sources == {"S1": True, "S2": True}, sources

    # 2. 预演停运 S1（只读）：S2 顶供，无失供
    status, body = switch([["S1", False]], version=1, confirm=False)
    show("2. 预演停运 S1（预期 changed/safe=true，无失供，只读）", status, body)
    assert status == 200 and body["safe"] is True, body
    assert body["affected_sink_ids"] == [], body

    # 3. 确认停运 S1 -> 版本 2
    status, body = switch([["S1", False]], version=1)
    show("3. 确认停运 S1（预期版本 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    # 停运 S1 后关阀建议只按运行水源 S2 计算：隔离 Q2 只需关 Q3
    status, body = call(
        "POST",
        "/api/isolation/recommend",
        {"fault_pipe_id": "Q2", "version": 2},
    )
    show("3b. 停运 S1 后的关阀建议（预期建议关 Q3）", status, body)
    assert status == 200, body
    assert body["recommended_close_pipe_ids"] == ["Q3"], body

    # 4. 空操作：把已停运的 S1 目标仍设为停运 -> changed=false，版本不推进
    status, body = switch([["S1", False]], version=2)
    show("4. 停运已停运的 S1（预期 changed=false，版本不推进）", status, body)
    assert status == 200 and body["changed"] is False and body["version"] == 2, body

    # 5. 同时停运两座水源（允许全网无运行水源）-> U1/U2 全部新增失供，版本 3
    status, body = switch([["S1", False], ["S2", False]], version=2)
    show("5. 同时停运两座水源（预期版本 3，U1/U2 失供）", status, body)
    assert status == 200 and body["version"] == 3, body
    assert body["affected_sink_ids"] == ["U1", "U2"], body

    # 6. 恢复 S1 -> U1/U2 恢复，版本 4（S2 仍停运）
    status, body = switch([["S1", True]], version=3)
    show("6. 恢复 S1（预期版本 4，U1/U2 恢复供水；S2 仍停运）", status, body)
    assert status == 200 and body["version"] == 4, body
    assert body["restored_sink_ids"] == ["U1", "U2"], body

    # 7. 保持 S2 停运，只切断运行水源 S1 即可隔离故障 Q2：关 Q1 -> 事件 1，版本 5
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "Q2", "close_pipe_ids": ["Q1"], "version": 4},
    )
    show("7. 提交隔离事件（S2 停运；故障 Q2，关 Q1，U1/U2 失供）", status, body)
    assert status == 200 and body["event_id"] == 1, body
    assert body["version"] == 5, body

    # 8. 预演恢复 S2：待修复故障 Q2 的 C 端经 Q3 重新进水 -> safe=false。
    #    C 进水后 U2 恢复；U1 因故障管段 Q2 本身仍阻断，暂不恢复。
    status, body = switch([["S2", True]], version=5, confirm=False)
    show("8. 预演恢复 S2（预期 safe=false，Q2 的 C 端重新进水，U2 恢复）", status, body)
    assert status == 200 and body["safe"] is False, body
    block = body["block_reasons"][0]
    assert block["code"] == "FAULT_REWATER_RISK", body
    assert block["pipe_id"] == "Q2" and block["reachable_endpoints"] == ["C"], body
    assert body["restored_sink_ids"] == ["U2"], body

    # 9. 强行确认 -> 422 SOURCE_SWITCH_UNSAFE，无部分写入
    status, body = switch([["S2", True]], version=5)
    show("9. 强行确认恢复 S2（预期 422 SOURCE_SWITCH_UNSAFE）", status, body)
    assert status == 422 and body["error"]["code"] == "SOURCE_SWITCH_UNSAFE", body

    # 10a. 过期版本确认 -> 409
    status, body = switch([["S2", True]], version=1)
    show("10a. 过期版本确认（预期 409 VERSION_CONFLICT）", status, body)
    assert status == 409 and body["error"]["code"] == "VERSION_CONFLICT", body

    # 10b. 非法编号 / 非水源节点 / 同批重复 -> 422 SOURCE_SWITCH_INVALID
    status, body = call(
        "POST",
        "/api/sources/switch",
        {
            "targets": [
                {"source_id": "SX", "running": False},
                {"source_id": "U1", "running": False},
                {"source_id": "S1", "running": False},
                {"source_id": "S1", "running": True},
            ],
            "version": 5,
        },
    )
    show("10b. 非法水源切换请求（预期 422 SOURCE_SWITCH_INVALID）", status, body)
    assert status == 422 and body["error"]["code"] == "SOURCE_SWITCH_INVALID", body
    codes = {i["code"] for i in body["error"]["issues"]}
    assert codes == {
        "SOURCE_NOT_FOUND",
        "SOURCE_NOT_SWITCHABLE",
        "SOURCE_TARGET_DUPLICATE",
    }, codes

    # 11a. 先复供故障事件（重开 Q1）：其余无待修复故障，安全 -> 版本 6。
    #      重开后运行水源 S1 恢复向 C 供水，U1/U2 在复供响应中恢复。
    status, body = call(
        "POST", "/api/isolation/events/1/restore", {"version": 5}
    )
    show("11a. 复供隔离事件 1（重开 Q1，U1/U2 恢复）", status, body)
    assert status == 200 and body["version"] == 6, body
    assert body["restored_sink_ids"] == ["U1", "U2"], body

    # 11b. 故障复供后恢复 S2：Q2 已修复通水，无重新进水风险 -> 成功，版本 7，
    #      双水源恢复运行。
    status, body = switch([["S2", True]], version=6)
    show("11b. 故障复供后恢复 S2（预期成功，版本 7，双水源运行）", status, body)
    assert status == 200 and body["version"] == 7, body
    assert body["restored_sink_ids"] == [], body  # S2 仅恢复冗余供水，无新增用水点

    status, body = call("GET", "/api/network")
    sources = {n["id"]: n["running"] for n in body["nodes"] if n["type"] == "source"}
    print(f"\n[读网] 最终水源运行状态：{sources}")
    assert sources == {"S1": True, "S2": True}, sources

    print("\n全部水源调度示例执行完成。")


if __name__ == "__main__":
    main()
