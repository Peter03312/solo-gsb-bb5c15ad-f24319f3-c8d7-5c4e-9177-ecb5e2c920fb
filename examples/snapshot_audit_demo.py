#!/usr/bin/env python3
"""调度审计：快照摘要链只读核验调用示例（仅依赖标准库）。

用法：
    python3 examples/snapshot_audit_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

系统为每条状态快照在其**原状态变更事务内**冻结一条 SHA-256 摘要，按实际
存在的快照编号顺序接成连续摘要链：摘要以固定可复现编码覆盖快照编号、操作
类型、管网版本、关联事件、创建时间、完整冻结状态及前一条摘要，首条锚定
固定创世摘要；整网导入即使重置版本，快照编号仍递增、链不断。首次启用时
为现存快照一次性建链并标明建链起点（建链前历史是否曾被改写不作断言）。

本脚本演示调度审计员的只读核验流程：
  1. 读取建链状态（启用标记、建链起点、创世摘要、链尾摘要）；
  2. 产生若干快照后，保存“起点前摘要”（此处为创世摘要）与“终点摘要”；
  3. 用 POST /api/snapshots/chain/verify 只读核验区间，各条摘要全部自洽；
  4. 子区间核验（起点前摘要 = 区间首条之前一条的已保存摘要）；
  5. 整网重新导入（版本重置）后核验，链仍然连续；
  6. 错误约定：区间倒序、端点不存在（含回滚编号空洞）明确报错；
  7. （只读演示不篡改库内数据）说明内部链自洽但终点摘要不符时只报告边界
     不符，而冻结状态或链指针在事后被改写时会给出首个损坏/断裂位置。
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

    # 0. 建链状态：enabled、建链起点、创世摘要、链尾摘要
    status, body = call("GET", "/api/snapshots/chain/status")
    show("0. 摘要链建链状态", status, body)
    assert status == 200 and body["enabled"] is True, body
    genesis = body["genesis_digest"]

    # 1. 产生三段状态：导入（1）→ 隔离提交（2）→ 复供确认（3）
    call("POST", "/api/network/import", network)
    call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    call("POST", "/api/isolation/events/1/restore", {"version": 2})

    # 2. 审计员事先保存：起点前摘要（全链首条 -> 创世摘要）与终点摘要（快照 3）
    status, before_end = call("GET", "/api/snapshots/chain/status")
    saved_start_prev = genesis
    saved_end_digest = before_end["last_digest"]
    print(f"\n[审计员保存] 起点前摘要 = {saved_start_prev}")
    print(f"[审计员保存] 终点摘要(3) = {saved_end_digest}")

    # 3. 只读核验全区间 [1,3]：各条摘要、内容与链路判定
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {
            "start_id": 1,
            "end_id": 3,
            "start_prev_digest": saved_start_prev,
            "end_digest": saved_end_digest,
        },
    )
    show("3. 只读核验 [1,3]（预期全部自洽、边界一致）", status, body)
    assert status == 200, body
    assert body["chain_intact"] and body["boundary_match"], body
    assert body["start_boundary_match"] and body["end_digest_match"], body
    assert [e["snapshot_id"] for e in body["entries"]] == [1, 2, 3], body
    assert all(e["content_ok"] and e["link_ok"] for e in body["entries"]), body
    # 返回各条摘要（digest 为存储值，computed_digest 为按固定编码的重算值）
    for e in body["entries"]:
        assert e["digest"] == e["computed_digest"], e

    # 4. 子区间 [2,3]：起点前摘要须为“首条之前一条”，即快照 1 的已保存摘要
    snap1 = body["entries"][0]["digest"]
    snap3 = body["entries"][2]["digest"]
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {
            "start_id": 2,
            "end_id": 3,
            "start_prev_digest": snap1,
            "end_digest": snap3,
        },
    )
    show("4. 子区间核验 [2,3]（起点前=快照1摘要，预期一致）", status, body)
    assert status == 200 and body["boundary_match"], body
    assert [e["snapshot_id"] for e in body["entries"]] == [2, 3], body

    # 5. 整网重新导入：版本重置为 1，快照编号继续为 4，链不断
    status, imp = call("POST", "/api/network/import", network)
    show("5a. 整网重新导入（版本重置 1；快照编号为 4，不覆盖 1-3）", status, imp)
    assert imp["version"] == 1 and imp["snapshot_id"] == 4, imp
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {"start_id": 1, "end_id": 4},
    )
    show("5b. 重导后核验 [1,4]（版本重置但链连续）", status, body)
    assert status == 200 and body["chain_intact"], body
    # 快照 4 的前链摘要等于快照 3 的摘要
    assert body["entries"][3]["prev_digest"] == body["entries"][2]["digest"], body

    # 6. 错误约定
    # 6a. 倒序区间 -> 422 SNAPSHOT_RANGE_INVALID
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {"start_id": 3, "end_id": 1},
    )
    show("6a. 倒序区间（预期 422 SNAPSHOT_RANGE_INVALID）", status, body)
    assert status == 422 and body["error"]["code"] == "SNAPSHOT_RANGE_INVALID", body

    # 6b. 端点不存在（快照编号不可复用，回滚事务可能留下编号空洞）-> 404
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {"start_id": 1, "end_id": 999},
    )
    show("6b. 终止端点不存在（预期 404 SNAPSHOT_NOT_FOUND）", status, body)
    assert status == 404 and body["error"]["code"] == "SNAPSHOT_NOT_FOUND", body

    # 6c. 摘要格式非法 -> 422 SNAPSHOT_CHAIN_DIGEST_INVALID
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {"start_id": 1, "end_id": 1, "end_digest": "not-a-sha256"},
    )
    show("6c. 摘要格式非法（预期 422 SNAPSHOT_CHAIN_DIGEST_INVALID）", status, body)
    assert status == 422
    assert body["error"]["code"] == "SNAPSHOT_CHAIN_DIGEST_INVALID", body

    # 7. 边界不符：内部链自洽，但审计员保存了错误的终点摘要
    status, body = call(
        "POST",
        "/api/snapshots/chain/verify",
        {"start_id": 1, "end_id": 4, "end_digest": "0" * 64},
    )
    show("7. 终点摘要不符（预期内部自洽、仅 end_digest_match=false）", status, body)
    assert status == 200, body
    assert body["chain_intact"] and body["first_issue"] is None, body
    assert body["end_digest_match"] is False, body

    print("\n核验请求示例（curl）：")
    print(
        "curl -s -X POST http://localhost:8000/api/snapshots/chain/verify \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        f"  -d '{{\"start_id\":1,\"end_id\":4,\"end_digest\":\""
        f"{body['end_digest']}\"}}'"
    )

    print("\n全部摘要链核验示例执行完成。")
    print(
        "说明：若任一快照的冻结状态/链字段在事后被直接改写，"
        "核验会在 first_issue 返回首个可定位位置"
        "（SNAPSHOT_CONTENT_MISMATCH 内容损坏 / SNAPSHOT_CHAIN_BROKEN 链路断裂 /"
        " SNAPSHOT_NOT_CHAINED 漏接链），本示例全程只读，不触发这些情况。"
    )


if __name__ == "__main__":
    main()
