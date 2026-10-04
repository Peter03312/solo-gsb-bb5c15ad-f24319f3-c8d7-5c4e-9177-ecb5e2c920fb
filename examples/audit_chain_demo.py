#!/usr/bin/env python3
"""调度审计：快照摘要链核验调用示例（仅依赖标准库）。

用法：
    python3 examples/audit_chain_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

每次实际改变状态（整网导入、隔离提交、复供确认、水源切换）时，系统在
**同一事务**内为操作后快照接一条 SHA-256 摘要链；首条使用固定创世摘要，
每条摘要以固定可复现编码覆盖：快照编号、操作类型、管网版本、关联事件、
创建时间、完整冻结状态、前一条摘要。首次启用时还会为现存快照补建链并
标明建链起点（anchor_kind=initial_backfill）。

本脚本依次演示（全程**只读核验**，不改任何状态）：
  1. 制造两条快照（整网导入 + 隔离提交）；
  2. GET  /api/audit/chain                 查看创世摘要与建链起点；
  3. POST /api/audit/verify 全区间核验     先取链上的起点前/终点摘要，
     再以审计员“事先保存”的这两个摘要重验 -> verdict=ok；
  4. 终点摘要故意不符   -> verdict=boundary_mismatch（内部链自洽，仅边界不符）；
  5. 端点不存在         -> 404 SNAPSHOT_NOT_FOUND；
  6. 区间倒序           -> 422 AUDIT_RANGE_INVALID；摘要非十六进制 -> 422 AUDIT_DIGEST_INVALID。

注意：内容损坏 / 链路断裂（content_corrupted / chain_broken）需要有人绕过
服务直接改写数据库才能复现，正常 API 没有任何改写快照的路径；核验逻辑见
app/audit.py 的 verify_interval。
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

# 64 位十六进制占位摘要（仅用于第一次“取边界”调用，必然不匹配）
PLACEHOLDER_DIGEST = "0" * 64


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


def verify(start_id: int, end_id: int, pre: str, end: str) -> tuple[int, dict]:
    return call(
        "POST",
        "/api/audit/verify",
        {
            "start_id": start_id,
            "end_id": end_id,
            "pre_start_digest": pre,
            "end_digest": end,
        },
    )


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 制造两条快照：整网导入（快照 1）+ 隔离提交关 P4 隔离 P6（快照 2）
    status, body = call("POST", "/api/network/import", network)
    assert status == 200, body
    first_id = body["snapshot_id"]
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    assert status == 200, body
    last_id = body["snapshot_id"]
    print(f"已产生快照 {first_id}..{last_id}（整网导入 + 隔离提交）")

    # 2. 链信息：创世摘要、建链起点、现存/已入链范围
    status, chain = call("GET", "/api/audit/chain")
    show("2. 摘要链信息 /api/audit/chain", status, chain)
    assert status == 200, chain
    genesis = chain["genesis_digest"]
    assert len(genesis) == 64

    # 3. 审计员取得链上的起点前摘要与终点摘要（模拟“事先离线保存”）。
    #    第一次故意带占位终点摘要：内部链自洽时返回 boundary_mismatch，
    #    并在 expected_* 字段回显链上真实边界值。
    status, probe = verify(first_id, last_id, genesis, PLACEHOLDER_DIGEST)
    show("3a. 占位终点摘要探测（预期 boundary_mismatch 并回显真实边界）", status, probe)
    assert status == 200 and probe["verdict"] == "boundary_mismatch", probe
    saved_pre = probe["expected_pre_start_digest"]
    saved_end = probe["expected_end_digest"]

    # 3b. 用事先保存的两个边界摘要正式核验 -> ok，返回区间内各条摘要
    status, body = verify(first_id, last_id, saved_pre, saved_end)
    show("3b. 保存边界摘要后全区间核验（预期 verdict=ok）", status, body)
    assert status == 200 and body["ok"] is True, body
    assert body["verdict"] == "ok", body
    assert [d["snapshot_id"] for d in body["digests"]] == [first_id, last_id], body
    # 起点前摘要正是固定创世摘要
    assert saved_pre == genesis, body

    # 4. 终点摘要被换成审计员自己另存的错误值：只报边界不符
    status, body = verify(first_id, last_id, saved_pre, "f" * 64)
    show("4. 终点摘要不符（预期 boundary_mismatch，failure.kind=boundary）", status, body)
    assert status == 200, body
    assert body["verdict"] == "boundary_mismatch", body
    assert body["end_boundary_ok"] is False and body["start_boundary_ok"] is True, body
    assert body["failure"]["kind"] == "boundary", body

    # 5. 端点不存在：回滚空洞 / 从未分配的编号都明确 404
    status, body = verify(last_id + 900, last_id + 900, genesis, saved_end)
    show("5. 端点不存在（预期 404 SNAPSHOT_NOT_FOUND）", status, body)
    assert status == 404 and body["error"]["code"] == "SNAPSHOT_NOT_FOUND", body

    # 6a. 区间倒序：明确报错
    status, body = verify(last_id, first_id, genesis, saved_end)
    show("6a. 区间倒序（预期 422 AUDIT_RANGE_INVALID）", status, body)
    assert status == 422 and body["error"]["code"] == "AUDIT_RANGE_INVALID", body

    # 6b. 摘要不是 64 位十六进制
    status, body = verify(first_id, last_id, "not-a-sha256-digest", saved_end)
    show("6b. 起点前摘要格式非法（预期 422 AUDIT_DIGEST_INVALID）", status, body)
    assert status == 422 and body["error"]["code"] == "AUDIT_DIGEST_INVALID", body

    print("\n全部审计核验示例通过。")


if __name__ == "__main__":
    main()
