#!/usr/bin/env python3
"""用水点最低流量核算调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/delivery_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

脚本依次演示：
  1. 导入管网（版本 1）
  2. 容量充足：U1/U2/U3 各 10 L/h，全部可满足
  3. 管段瓶颈：P3 双向共用上限只有 6，U1 要 10 -> 最大可交付不足，feasible=false
  4. 已读版本过期：version_match=false，但仍按当前状态核算
  5. 水源上限不足：S 只能供 15 -> 需求 30 无法全部满足
  6. 隔离 P6（关 P4）后再核算：U3 因故障/关阀断供，只交付 U1/U2
  7. 非法请求：水源/管段未覆盖、编号重复/不存在、布尔/负数容量 -> 422 整体拒绝
  8. 确认全程只读：版本仍为 2、阀位/事件不变
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_network.json")

# sample_network 的全部水源与管段（编号须完整覆盖，不多不少）
SOURCE_CAPS = [{"source_id": "S", "capacity": 1000}]
PIPE_CAPS = [
    {"pipe_id": "P1", "capacity": 100},
    {"pipe_id": "P2", "capacity": 100},
    {"pipe_id": "P3", "capacity": 100},
    {"pipe_id": "P4", "capacity": 100},
    {"pipe_id": "P5", "capacity": 100},
    {"pipe_id": "P6", "capacity": 100},
]


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


def delivery(version, sources, pipes, demands):
    return call(
        "POST",
        "/api/delivery/evaluate",
        {"version": version, "sources": sources, "pipes": pipes, "demands": demands},
    )


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    demands = [
        {"sink_id": "U1", "demand": 10},
        {"sink_id": "U2", "demand": 10},
        {"sink_id": "U3", "demand": 10},
    ]

    # 2. 容量充足：全部满足
    status, body = delivery(1, SOURCE_CAPS, PIPE_CAPS, demands)
    show("2. 容量充足（需求 30，全部满足）", status, body)
    assert status == 200, body
    assert body["total_demand"] == 30
    assert body["max_delivered_total"] == 30
    assert body["feasible"] is True
    assert body["version_match"] is True

    # 3. 管段瓶颈：U1 的唯一通道 P3 只能过 6
    tight_pipes = [
        {"pipe_id": "P1", "capacity": 100},
        {"pipe_id": "P2", "capacity": 100},
        {"pipe_id": "P3", "capacity": 6},
        {"pipe_id": "P4", "capacity": 100},
        {"pipe_id": "P5", "capacity": 100},
        {"pipe_id": "P6", "capacity": 100},
    ]
    status, body = delivery(1, SOURCE_CAPS, tight_pipes, demands)
    show("3. P3 双向共用上限 6（预期最大交付 26，不能全部满足）", status, body)
    assert body["max_delivered_total"] == 26 and body["feasible"] is False, body

    # 4. 过期版本：仍按当前状态核算
    status, body = delivery(99, SOURCE_CAPS, tight_pipes, demands)
    show("4. 已读版本过期（version_match=false，仍按当前版本 1 核算）", status, body)
    assert body["version"] == 1 and body["version_match"] is False, body
    assert body["max_delivered_total"] == 26, body

    # 5. 水源供水上限不足
    status, body = delivery(
        1, [{"source_id": "S", "capacity": 15}], PIPE_CAPS, demands
    )
    show("5. 水源 S 上限 15（需求 30，预期最大交付 15）", status, body)
    assert body["max_delivered_total"] == 15 and body["feasible"] is False, body

    # 6. 隔离 P6（关 P4）后：U3 断供，U1/U2 不受影响
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("6a. 提交隔离 P6（关 P4，版本 -> 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    status, body = delivery(2, SOURCE_CAPS, PIPE_CAPS, demands)
    show("6b. 隔离后核算（P4 已关、P6 待修复；U3 不可交付，预期交付 20）", status, body)
    assert body["max_delivered_total"] == 20 and body["feasible"] is False, body

    # 只申报 U1/U2（未列需求的 U3 不消耗流量）即可全部满足
    status, body = delivery(
        2, SOURCE_CAPS, PIPE_CAPS, demands[:2]
    )
    show("6c. 只申报 U1/U2（U3 未列需求、不消耗）", status, body)
    assert body["max_delivered_total"] == 20 and body["feasible"] is True, body

    # 7. 非法请求（整体拒绝，返回全部问题明细）
    bad = {
        "version": 2,
        # 水源未覆盖（缺 S）且编号重复/不存在
        "sources": [
            {"source_id": "SX", "capacity": 10},
            {"source_id": "SX", "capacity": 10},
        ],
        # 管段未覆盖（缺 P6）、重复（P1）、不存在（PX）
        "pipes": [
            {"pipe_id": "P1", "capacity": 100},
            {"pipe_id": "P1", "capacity": 100},
            {"pipe_id": "PX", "capacity": 100},
            {"pipe_id": "P2", "capacity": 100},
            {"pipe_id": "P3", "capacity": 100},
            {"pipe_id": "P4", "capacity": 100},
            {"pipe_id": "P5", "capacity": 100},
        ],
        # 用水点重复（U1）、不是用水点（S）、不存在（UX）
        "demands": [
            {"sink_id": "U1", "demand": 1},
            {"sink_id": "U1", "demand": 1},
            {"sink_id": "S", "demand": 1},
            {"sink_id": "UX", "demand": 1},
        ],
    }
    status, body = call("POST", "/api/delivery/evaluate", bad)
    show("7a. 编号未覆盖/重复/不存在 -> 422 DELIVERY_REQUEST_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "DELIVERY_REQUEST_INVALID", body

    bad_type = {
        "version": 2,
        "sources": [{"source_id": "S", "capacity": True}],  # 布尔不是非负整数
        "pipes": PIPE_CAPS,
        "demands": [{"sink_id": "U1", "demand": -1}],       # 负数
    }
    status, body = call("POST", "/api/delivery/evaluate", bad_type)
    show("7b. 类型/数值错误（布尔容量、负需求）-> 422", status, body)
    assert status == 422, body

    # 8. 只读核对：版本仍为 2、只有隔离事件 1、P4 仍关闭（核算未改任何状态）
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 2, net
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["fault_pipe_id"] == "P6"
    print(
        "\n8. 只读核对通过：版本仍为 2，仅 1 条隔离事件（P6），"
        "阀位未被核算接口改动。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
