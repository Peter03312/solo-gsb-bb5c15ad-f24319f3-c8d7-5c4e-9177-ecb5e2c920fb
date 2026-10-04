#!/usr/bin/env python3
"""管段流量计读数核对调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/meter_reconcile_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

脚本依次演示：
  1. 导入管网（版本 1）
  2. 全部缺报：按注入/取用/容量推断各管段流量（树形管网全部可确定）
  3. 部分缺报 + 部分带符号读数：只返回缺报管段的最小/最大可行流量
  4. 读数不一致：P1 读数 0 但 S 实际注入 30 -> consistent=false，不输出补值
  5. 读数超过双向共用容量（READING_OVER_CAPACITY）
  6. 隔离 P6（关 P4）后核对：关阀/故障管段读数非 0 拒绝，缺报可确定为 0
  7. 硬约束：停运水源注入非 0（STOPPED_SOURCE_INJECTION）
  8. 已读版本过期：仍按当前状态计算，仅 version_match=false
  9. 非法请求：编号未覆盖/重复/未知节点、布尔/小数/负数 -> 422 整体拒绝
  10. 确认全程只读：版本、阀位、事件均未被改写
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

ALL_PIPE_IDS = ("P1", "P2", "P3", "P4", "P5", "P6")
ALL_SINK_IDS = ("U1", "U2", "U3")


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


def reconcile(version, sources, sinks, pipes):
    return call(
        "POST",
        "/api/meters/reconcile",
        {"version": version, "sources": sources, "sinks": sinks, "pipes": pipes},
    )


def pipes_with(capacity=100, readings=None):
    """readings: {pipe_id: 读数或 None}，未列出视为缺报（None）。"""
    readings = readings or {}
    return [
        {"pipe_id": pid, "capacity": capacity, "reading": readings.get(pid)}
        for pid in ALL_PIPE_IDS
    ]


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    zero_sinks = [{"sink_id": sid, "withdrawal": 0} for sid in ALL_SINK_IDS]

    # 2. 全部缺报：S 注入 30，U1/U2/U3 各取 10
    status, body = reconcile(
        1,
        [{"source_id": "S", "injection": 30}],
        [{"sink_id": sid, "withdrawal": 10} for sid in ALL_SINK_IDS],
        pipes_with(),
    )
    show("2. 全部缺报（树形管网各管段流量被节点平衡唯一确定）", status, body)
    assert status == 200 and body["consistent"] is True, body
    expected = {"P1": 30, "P2": 20, "P3": 10, "P4": 20, "P5": 10, "P6": 10}
    for item in body["readings"]:
        assert item["min_flow"] == item["max_flow"] == expected[item["pipe_id"]], item
        assert item["determinable"] is True
    # 方向回显：读数符号以 from->to 为正
    assert body["readings"][0]["from"] == "S" and body["readings"][0]["to"] == "A"

    # 3. 部分缺报 + 部分已报（带符号）：P1/P3/P6 已报，P2/P4/P5 缺报
    readings = {"P1": 30, "P3": 10, "P6": 10}
    status, body = reconcile(
        1,
        [{"source_id": "S", "injection": 30}],
        [{"sink_id": sid, "withdrawal": 10} for sid in ALL_SINK_IDS],
        pipes_with(readings=readings),
    )
    show("3. 部分缺报（只返回 P2/P4/P5 的可行范围）", status, body)
    assert status == 200 and body["consistent"] is True, body
    assert {x["pipe_id"] for x in body["readings"]} == {"P2", "P4", "P5"}
    assert {(x["pipe_id"], x["min_flow"], x["max_flow"]) for x in body["readings"]} == {
        ("P2", 20, 20), ("P4", 20, 20), ("P5", 10, 10),
    }

    # 4. 读数不一致：P1 读数 0，但 S 实际注入 30 -> 无可行解，不输出补值
    status, body = reconcile(
        1,
        [{"source_id": "S", "injection": 30}],
        [{"sink_id": sid, "withdrawal": 10} for sid in ALL_SINK_IDS],
        pipes_with(readings={"P1": 0}),
    )
    show("4. 读数不一致（P1=0 与 S 注入 30 矛盾，不输出补值）", status, body)
    assert status == 200 and body["consistent"] is False, body
    assert body["readings"] == []
    assert body["reasons"][0]["code"] == "READINGS_INCONSISTENT"

    # 5. 读数超过双向共用容量：P1 容量 5，读数 6
    status, body = reconcile(
        1,
        [{"source_id": "S", "injection": 6}],
        zero_sinks,
        [{"pipe_id": "P1", "capacity": 5, "reading": 6}]
        + [
            {"pipe_id": pid, "capacity": 100, "reading": 0}
            for pid in ALL_PIPE_IDS
            if pid != "P1"
        ],
    )
    show("5. P1 读数 6 超过双向共用容量 5（READING_OVER_CAPACITY）", status, body)
    assert body["consistent"] is False
    assert body["reasons"][0]["code"] == "READING_OVER_CAPACITY"

    # 6. 隔离 P6（关 P4）后核对：P4 已关、P6 待修复，阻断管段流量必须为 0
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("6a. 提交隔离 P6（关 P4，版本 -> 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    # 全网零注入/零取用：阻断管段缺报也可确定为 0
    status, body = reconcile(2, [{"source_id": "S", "injection": 0}], zero_sinks,
                             pipes_with())
    show("6b. 隔离后核对（阻断的 P4/P6 缺报也确定为 0）", status, body)
    assert body["consistent"] is True, body
    blocked_ranges = {x["pipe_id"]: x for x in body["readings"]}
    for pid in ("P4", "P6"):
        assert blocked_ranges[pid]["min_flow"] == 0
        assert blocked_ranges[pid]["max_flow"] == 0
        assert blocked_ranges[pid]["determinable"] is True

    # 阻断管段读数非 0 -> BLOCKED_PIPE_FLOW
    status, body = reconcile(
        2,
        [{"source_id": "S", "injection": 0}],
        zero_sinks,
        pipes_with(readings={"P4": 3}),
    )
    show("6c. 阻断管段 P4 读数 3（BLOCKED_PIPE_FLOW）", status, body)
    assert body["consistent"] is False
    assert body["reasons"][0]["code"] == "BLOCKED_PIPE_FLOW"

    # 7. 停运水源硬约束：在版本 2 基础上停运 S（版本 -> 3），注入非 0 即拒绝
    status, sw = call(
        "POST",
        "/api/sources/switch",
        {"targets": [{"source_id": "S", "running": False}], "version": 2},
    )
    assert status == 200 and sw["version"] == 3, sw
    status, body = reconcile(
        3,
        [{"source_id": "S", "injection": 5}],
        zero_sinks,
        pipes_with(),
    )
    show("7. 停运水源 S 申报注入 5（STOPPED_SOURCE_INJECTION）", status, body)
    assert body["consistent"] is False
    assert body["reasons"][0]["code"] == "STOPPED_SOURCE_INJECTION"
    # 停运且注入/取用全为 0：仍可核对一致（阻断状态不变）
    status, body = reconcile(3, [{"source_id": "S", "injection": 0}], zero_sinks,
                             pipes_with())
    assert status == 200 and body["consistent"] is True, body

    # 8. 已读版本过期：version=99 仍按当前版本 3 计算，仅 version_match=false
    status, body = reconcile(99, [{"source_id": "S", "injection": 0}], zero_sinks,
                             pipes_with())
    show("8. 已读版本过期（version_match=false，仍按当前版本 3 核对）", status, body)
    assert body["version"] == 3 and body["version_match"] is False, body

    # 9. 非法请求（整体拒绝，返回全部问题明细）
    bad = {
        "version": 3,
        # 水源未覆盖（缺 S）+ 重复 + 不存在
        "sources": [
            {"source_id": "SX", "injection": 1},
            {"source_id": "SX", "injection": 1},
        ],
        # 缺水点 U3 + 重复 U1 + 连接点 A 不可申报取用
        "sinks": [
            {"sink_id": "U1", "withdrawal": 1},
            {"sink_id": "U1", "withdrawal": 1},
            {"sink_id": "U2", "withdrawal": 1},
            {"sink_id": "A", "withdrawal": 1},
        ],
        # 缺 P6 + 重复 P1 + 不存在 PX
        "pipes": [
            {"pipe_id": "P1", "capacity": 100, "reading": None},
            {"pipe_id": "P1", "capacity": 100, "reading": None},
            {"pipe_id": "PX", "capacity": 100, "reading": None},
        ] + [
            {"pipe_id": pid, "capacity": 100, "reading": None}
            for pid in ("P2", "P3", "P4", "P5")
        ],
    }
    status, body = call("POST", "/api/meters/reconcile", bad)
    show("9a. 编号未覆盖/重复/未知节点 -> 422 METER_RECONCILE_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "METER_RECONCILE_INVALID", body

    bad_type = {
        "version": 3,
        "sources": [{"source_id": "S", "injection": True}],   # 布尔不是非负整数
        "sinks": zero_sinks,
        "pipes": pipes_with(),
    }
    status, body = call("POST", "/api/meters/reconcile", bad_type)
    show("9b. 布尔注入 / 小数读数 / 负容量等类型错误 -> 422", status, body)
    assert status == 422, body
    bad_type["sources"] = [{"source_id": "S", "injection": 0}]
    bad_type["pipes"][0]["reading"] = 1.5
    assert call("POST", "/api/meters/reconcile", bad_type)[0] == 422
    bad_type["pipes"][0]["reading"] = None
    bad_type["pipes"][0]["capacity"] = -1
    assert call("POST", "/api/meters/reconcile", bad_type)[0] == 422

    # 10. 只读核对：版本仍为 3、只有隔离事件 1、P4 仍关闭
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 3, net
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["fault_pipe_id"] == "P6"
    print(
        "\n10. 只读核对通过：版本仍为 3，仅 1 条隔离事件（P6），"
        "读数核对接口未改动阀位、水源状态与版本。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
