#!/usr/bin/env python3
"""保障等级分档交付调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/allocate_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

供水不足时，调度员按**保障等级**（正整数，数字越小越优先）申报各用水点需求：
服务先按等级从小到大依次最大化该等级交付总量，再在各等级总量最优条件下，
按（等级, 用水点编号）逐点最大化交付；各点交付不超过需求，结果由一组同时
可行的整数流量实现。本脚本依次演示：

  1. 导入管网（版本 1）
  2. 容量充足：各等级全部满足
  3. 管段瓶颈 + 等级优先：U1 的唯一通道 P3 只能过 6，等级 1 先保 U1=6，
     余量分给等级 2 的 U2/U3（总交付 26/30）
  4. 水源上限不足：等级 1 先满足，余量才给等级 2
  5. 同等级编号序：等级相同、共享瓶颈时，编号小的点先满
  6. 已读版本过期：version_match=false，仍按当前状态核算
  7. 隔离 P6（关 P4）后：U3 断供，等级再高/低都不影响阻断口径
  8. 非法请求：等级非正整数、编号未覆盖/重复/类型错误 -> 422 整体拒绝
  9. 确认全程只读：版本仍为 2、阀位/事件不变
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


def allocate(version, sources, pipes, demands):
    return call(
        "POST",
        "/api/delivery/allocate",
        {"version": version, "sources": sources, "pipes": pipes, "demands": demands},
    )


def summarize(body: dict) -> list:
    return [
        (d["sink_id"], d["level"], d["delivered"], d["demand"])
        for d in body["deliveries"]
    ]


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 容量充足：U1 等级 1，U2/U3 等级 2，各 10 L/h，全部满足
    demands = [
        {"sink_id": "U1", "demand": 10, "level": 1},
        {"sink_id": "U2", "demand": 10, "level": 2},
        {"sink_id": "U3", "demand": 10, "level": 2},
    ]
    status, body = allocate(1, SOURCE_CAPS, PIPE_CAPS, demands)
    show("2. 容量充足（等级 1 的 U1 与等级 2 的 U2/U3 全部满足）", status, body)
    assert status == 200, body
    assert body["all_satisfied"] is True
    assert body["total_delivered"] == 30

    # 3. 管段瓶颈 + 等级优先：P3 只能过 6
    tight_pipes = [
        {"pipe_id": "P1", "capacity": 100},
        {"pipe_id": "P2", "capacity": 100},
        {"pipe_id": "P3", "capacity": 6},
        {"pipe_id": "P4", "capacity": 100},
        {"pipe_id": "P5", "capacity": 100},
        {"pipe_id": "P6", "capacity": 100},
    ]
    status, body = allocate(1, SOURCE_CAPS, tight_pipes, demands)
    show("3. P3 瓶颈=6（等级 1 的 U1 先保 6，等级 2 的 U2/U3 各 10）", status, body)
    assert summarize(body) == [
        ("U1", 1, 6, 10),
        ("U2", 2, 10, 10),
        ("U3", 2, 10, 10),
    ], body
    assert body["all_satisfied"] is False

    # 4. 水源上限不足：S 只能供 15 -> 等级 1 的 U1=10，等级 2 合计仅 5
    status, body = allocate(
        1, [{"source_id": "S", "capacity": 15}], PIPE_CAPS, demands
    )
    show("4. 水源 S 上限 15（等级 1 满 10，等级 2 合计 5，按编号先给 U2）", status, body)
    assert summarize(body) == [
        ("U1", 1, 10, 10),
        ("U2", 2, 5, 10),
        ("U3", 2, 0, 10),
    ], body

    # 5. 同等级编号序：U1/U2/U3 同为等级 1，总瓶颈在水源 12
    same_level = [
        {"sink_id": "U1", "demand": 10, "level": 1},
        {"sink_id": "U2", "demand": 10, "level": 1},
        {"sink_id": "U3", "demand": 10, "level": 1},
    ]
    status, body = allocate(
        1, [{"source_id": "S", "capacity": 12}], PIPE_CAPS, same_level
    )
    show("5. 同等级、水源上限 12（编号序：U1=10、U2=2、U3=0）", status, body)
    assert summarize(body) == [
        ("U1", 1, 10, 10),
        ("U2", 1, 2, 10),
        ("U3", 1, 0, 10),
    ], body

    # 6. 过期版本：仍按当前状态核算
    status, body = allocate(99, SOURCE_CAPS, tight_pipes, demands)
    show("6. 已读版本过期（version_match=false，结果同第 3 步）", status, body)
    assert body["version"] == 1 and body["version_match"] is False, body
    assert summarize(body) == [
        ("U1", 1, 6, 10),
        ("U2", 2, 10, 10),
        ("U3", 2, 10, 10),
    ], body

    # 7. 隔离 P6（关 P4）后：U3 断供
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("7a. 提交隔离 P6（关 P4，版本 -> 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    status, body = allocate(2, SOURCE_CAPS, PIPE_CAPS, demands)
    show("7b. 隔离后分档（P4 已关、P6 待修复；等级 2 的 U3 断供为 0）", status, body)
    assert summarize(body) == [
        ("U1", 1, 10, 10),
        ("U2", 2, 10, 10),
        ("U3", 2, 0, 10),
    ], body
    assert body["total_delivered"] == 20

    # 8. 非法请求（整体拒绝，返回全部问题明细）
    bad_level = {
        "version": 2,
        "sources": SOURCE_CAPS,
        "pipes": PIPE_CAPS,
        # 等级必须为正整数：0 / 小数 / 布尔均非法
        "demands": [{"sink_id": "U1", "demand": 10, "level": 0}],
    }
    status, body = call("POST", "/api/delivery/allocate", bad_level)
    show("8a. 等级非正整数 -> 422（请求结构校验）", status, body)
    assert status == 422, body

    bad_ids = {
        "version": 2,
        "sources": [],  # 未覆盖水源 S
        "pipes": PIPE_CAPS + [{"pipe_id": "P1", "capacity": 1}],  # P1 重复
        "demands": [
            {"sink_id": "U1", "demand": 10, "level": 1},
            {"sink_id": "S", "demand": 10, "level": 1},   # 不是用水点
            {"sink_id": "UX", "demand": 10, "level": 1},  # 编号不存在
        ],
    }
    status, body = call("POST", "/api/delivery/allocate", bad_ids)
    show("8b. 编号未覆盖/重复/类型错误 -> 422 DELIVERY_REQUEST_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "DELIVERY_REQUEST_INVALID", body
    codes = {i["code"] for i in body["error"]["issues"]}
    assert {
        "DELIVERY_SOURCE_MISSING",
        "DELIVERY_PIPE_DUPLICATE",
        "DELIVERY_SINK_NOT_SINK",
        "DELIVERY_SINK_NOT_FOUND",
    } <= codes, codes

    # 9. 只读核对：版本仍为 2、只有隔离事件 1、P4 仍关闭
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 2, net
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["fault_pipe_id"] == "P6"
    print(
        "\n9. 只读核对通过：版本仍为 2，仅 1 条隔离事件（P6），"
        "阀位未被分档核算接口改动。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
