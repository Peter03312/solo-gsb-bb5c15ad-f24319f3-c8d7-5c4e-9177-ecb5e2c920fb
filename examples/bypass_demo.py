#!/usr/bin/env python3
"""临时旁通管供水效果评估调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/bypass_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

抢修期间，调度员携带已读版本、两个现有节点（旁通端点）、旁通管双向共用容量、
覆盖当前管网的水源/管段容量与带保障等级的用水需求，评估一条临时旁通管的
供水效果。服务先按当前阀位、运行水源与待修复故障核算基线分档交付，再仅在
内存中接入旁通：若接入后任一待修复故障管段的任一端从运行水源可达
（重新进水风险），判为不可用并返回故障编号与危险端点，不输出旁通交付量；
安全时返回基线与接入旁通后的各等级、各用水点交付量及差值。本脚本依次演示：

  1. 导入管网（版本 1）
  2. 无故障：P3 瓶颈 6，旁通 S->U1 容量 4（与现有管段并联）-> U1 6->10，
     安全，展示基线 / 旁通后 / 差值
  3. 提交隔离 P6（关 P4，版本 -> 2）：U3 断供
  4. 旁通 A->D：使待修复故障 P6 的 D 端重新进水 -> safe=false，
     返回故障编号与危险端点，不输出旁通交付量（基线仍给出）
  5. 故障态下安全旁通 S->U1：不触碰故障区，U1 6->10、U3 仍断供
  6. 已读版本过期：version_match=false，仍按当前状态核算
  7. 非法请求：端点不存在 / 两端相同 / 编号未覆盖 / 容量非负整数 -> 422
  8. 确认全程只读：版本仍为 2、阀位与事件不变、管网中无虚拟旁通管段
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

# sample_network 的全部水源与管段（编号须完整覆盖，不多不少）；P3 瓶颈 6
SOURCE_CAPS = [{"source_id": "S", "capacity": 1000}]
PIPE_CAPS = [
    {"pipe_id": "P1", "capacity": 100},
    {"pipe_id": "P2", "capacity": 100},
    {"pipe_id": "P3", "capacity": 6},
    {"pipe_id": "P4", "capacity": 100},
    {"pipe_id": "P5", "capacity": 100},
    {"pipe_id": "P6", "capacity": 100},
]
DEMANDS = [
    {"sink_id": "U1", "demand": 10, "level": 1},
    {"sink_id": "U2", "demand": 10, "level": 2},
    {"sink_id": "U3", "demand": 10, "level": 2},
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


def bypass(version, bypass_spec, sources=None, pipes=None, demands=None):
    return call(
        "POST",
        "/api/delivery/bypass",
        {
            "version": version,
            "bypass": bypass_spec,
            "sources": sources if sources is not None else SOURCE_CAPS,
            "pipes": pipes if pipes is not None else PIPE_CAPS,
            "demands": demands if demands is not None else DEMANDS,
        },
    )


def deliveries(body: dict) -> list:
    return [(d["sink_id"], d["delivered"]) for d in body["deliveries"]]


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 无故障：旁通 S->U1 容量 4（与现有管段并联），P3 瓶颈 6
    status, body = bypass(1, {"from": "S", "to": "U1", "capacity": 4})
    show("2. 旁通 S->U1 容量 4（安全：U1 6->10，总交付 26->30）", status, body)
    assert status == 200 and body["safe"] is True, body
    assert deliveries(body["baseline"]) == [("U1", 6), ("U2", 10), ("U3", 10)], body
    assert deliveries(body["with_bypass"]) == [("U1", 10), ("U2", 10), ("U3", 10)], body
    assert body["deltas"]["total_delivered_delta"] == 4, body
    assert [
        (d["sink_id"], d["delivered_delta"]) for d in body["deltas"]["deliveries"]
    ] == [("U1", 4), ("U2", 0), ("U3", 0)], body

    # 3. 提交隔离 P6（关 P4，版本 -> 2）
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("3. 提交隔离 P6（关 P4，版本 -> 2，U3 断供）", status, body)
    assert status == 200 and body["version"] == 2, body

    # 4. 旁通 A->D：待修复故障 P6 的 D 端将重新进水 -> 判不可用
    status, body = bypass(2, {"from": "A", "to": "D", "capacity": 50})
    show("4. 旁通 A->D（不安全：P6 的 D 端重新进水，不输出旁通交付量）", status, body)
    assert status == 200 and body["safe"] is False, body
    assert [
        (b["code"], b["pipe_id"], b["reachable_endpoints"])
        for b in body["block_reasons"]
    ] == [("FAULT_REWATER_RISK", "P6", ["D"])], body
    assert body["with_bypass"] is None and body["deltas"] is None, body
    # 基线仍按当前状态给出：U3 断供
    assert deliveries(body["baseline"]) == [("U1", 6), ("U2", 10), ("U3", 0)], body

    # 5. 故障态下安全旁通 S->U1：不触碰故障区
    status, body = bypass(2, {"from": "S", "to": "U1", "capacity": 4})
    show("5. 故障态旁通 S->U1（安全：U1 6->10，U3 仍断供）", status, body)
    assert status == 200 and body["safe"] is True, body
    assert deliveries(body["with_bypass"]) == [("U1", 10), ("U2", 10), ("U3", 0)], body

    # 6. 已读版本过期：仍按当前状态核算
    status, body = bypass(99, {"from": "S", "to": "U1", "capacity": 4})
    show("6. 已读版本过期（version_match=false，结果同第 5 步）", status, body)
    assert status == 200, body
    assert body["version"] == 2 and body["version_match"] is False, body
    assert deliveries(body["with_bypass"]) == [("U1", 10), ("U2", 10), ("U3", 0)], body

    # 7. 非法请求（整体拒绝，返回全部问题明细）
    status, body = bypass(2, {"from": "XX", "to": "U1", "capacity": 4})
    show("7a. 旁通端点不存在 -> 422 BYPASS_REQUEST_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "BYPASS_REQUEST_INVALID", body
    assert [i["code"] for i in body["error"]["issues"]] == [
        "BYPASS_ENDPOINT_NOT_FOUND"
    ], body

    status, body = bypass(2, {"from": "A", "to": "A", "capacity": 4})
    show("7b. 旁通两端为同一节点 -> 422", status, body)
    assert status == 422, body
    assert [i["code"] for i in body["error"]["issues"]] == [
        "BYPASS_ENDPOINT_SAME"
    ], body

    status, body = bypass(
        2,
        {"from": "S", "to": "U1", "capacity": 4},
        sources=[],  # 水源未覆盖
        pipes=PIPE_CAPS + [{"pipe_id": "P1", "capacity": 1}],  # P1 重复
    )
    show("7c. 编号未覆盖/重复 -> 422（沿用 DELIVERY_* 问题代码）", status, body)
    assert status == 422 and body["error"]["code"] == "BYPASS_REQUEST_INVALID", body
    codes = {i["code"] for i in body["error"]["issues"]}
    assert {"DELIVERY_SOURCE_MISSING", "DELIVERY_PIPE_DUPLICATE"} <= codes, codes

    status, body = bypass(2, {"from": "S", "to": "U1", "capacity": -1})
    show("7d. 旁通容量为负数 -> 422（请求结构校验）", status, body)
    assert status == 422, body

    # 8. 只读核对：版本仍为 2、仅 1 条隔离事件、P4 仍关闭、管网无虚拟旁通管段
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 2, net
    assert "__BYPASS__" not in [p["id"] for p in net["pipes"]], net
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["fault_pipe_id"] == "P6"
    print(
        "\n8. 只读核对通过：版本仍为 2，仅 1 条隔离事件（P6），"
        "阀位未被评估接口改动，管网中无虚拟旁通管段。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
