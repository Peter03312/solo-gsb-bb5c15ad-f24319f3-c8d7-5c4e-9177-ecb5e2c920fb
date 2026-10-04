"""快照摘要链：为冻结的操作后状态建立连续 SHA-256 链，供调度审计事后核验。

链的固定编码（v1，任何实现逐字节一致即可复现）：

    SHA256( canonical_json({
        "chain": "water-snapshot-chain/v1",
        "id": <快照编号>,
        "op_type": <操作类型>,
        "network_version": <管网版本>,
        "event_id": <关联事件编号或 null>,
        "created_at": <创建时间，UTC，形如 2026-10-04T08:30:00.123456Z>,
        "state": <完整冻结状态（原样规范化 JSON）>,
        "prev_digest": <前一条快照摘要；首条为固定创世摘要>,
    }) )

canonical_json 的规则见 :func:`canonical_json`：键升序、紧凑分隔、ensure_ascii=False，
再以 UTF-8 编码。时间一律转成带微秒的 UTC（Z 后缀），因此与数据库时区无关。

首条入链快照（建链起点 anchor）的 prev_digest 使用固定创世摘要
``GENESIS_DIGEST``（对固定字符串 "water-snapshot-chain/v1:genesis" 取 SHA-256）。

回滚造成的快照编号空洞（BIGSERIAL 跳号）不影响接链：每条快照链接的是
“实际存在的、编号严格小于自身的最近一条”，核验时同样跳过空洞。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

# 链编码版本标识：写入创世输入、信封与核验规则，改动即构成新链
CHAIN_TAG = "water-snapshot-chain/v1"
GENESIS_INPUT = f"{CHAIN_TAG}:genesis".encode("utf-8")
GENESIS_DIGEST = hashlib.sha256(GENESIS_INPUT).hexdigest()

# 核验结论（HTTP 始终 200，结论在 verdict 中）
VERDICT_OK = "ok"                    # 内部自洽，且起点前/终点边界摘要均相符
VERDICT_BOUNDARY_MISMATCH = "boundary_mismatch"  # 仅边界摘要不符
VERDICT_CONTENT_CORRUPTED = "content_corrupted"  # 首个可定位的内容损坏
VERDICT_CHAIN_BROKEN = "chain_broken"            # 首个可定位的链路断裂

# 建链起点（anchor）的来源
ANCHOR_INITIAL_BACKFILL = "initial_backfill"  # 首次启用：为现存快照建链
ANCHOR_LIVE_ORIGIN = "live_origin"            # 空库启用：首条状态变更快照即为起点


def canonical_json(value: Any) -> str:
    """固定可复现的 JSON 编码：键按码位升序、无多余空白、非 ASCII 原样输出。

    载荷中的全部取值都是字符串/整数/布尔/null 及其数组、对象（freeze_state
    保证），不存在浮点数等随实现变化的类型；sort_keys 消除 PostgreSQL JSONB
    可能重排对象键的影响。
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_created_at(created_at: datetime) -> str:
    """把 TIMESTAMPTZ 读出的时间规范化为 UTC、带微秒、Z 后缀的字符串。"""
    if created_at.tzinfo is None:
        # 防御：列定义为 TIMESTAMPTZ，正常读出均带时区；缺时区按 UTC 处理
        created_at = created_at.replace(tzinfo=timezone.utc)
    return (
        created_at.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _normalized_state(state: Any) -> Any:
    # state 来自 JSONB（可能为 dict 或 json 字符串）：统一解析后随信封重新规范化
    if isinstance(state, str):
        state = json.loads(state)
    return state


def snapshot_digest(
    *,
    snapshot_id: int,
    op_type: str,
    network_version: int,
    event_id: int | None,
    created_at: datetime,
    state: Any,
    prev_digest: str,
) -> str:
    """按固定编码计算单条快照的 SHA-256 摘要（十六进制小写）。"""
    envelope = {
        "chain": CHAIN_TAG,
        "id": snapshot_id,
        "op_type": op_type,
        "network_version": network_version,
        "event_id": event_id,
        "created_at": canonical_created_at(created_at),
        "state": _normalized_state(state),
        "prev_digest": prev_digest,
    }
    # 为确保“固定可复现编码”，信封整体走同一个 canonical_json
    payload = canonical_json(envelope).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def compute_snapshot_digest(row: dict, prev_digest: str) -> str:
    """对一条快照行（含 state、created_at）计算摘要。

    row 字段与 db._SNAPSHOT_COLUMNS 读出的字典一致。
    """
    return snapshot_digest(
        snapshot_id=row["id"],
        op_type=row["op_type"],
        network_version=row["network_version"],
        event_id=row["event_id"],
        created_at=row["created_at"],
        state=row["state"],
        prev_digest=prev_digest,
    )


def is_hex_digest(value: str) -> bool:
    """是否为 64 位十六进制 SHA-256 摘要（允许大小写，调用时统一转小写）。"""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789abcdefABCDEF" for c in value)
    )


def verify_interval(
    rows: list[dict],
    *,
    pre_start_digest: str,
    end_digest: str,
) -> dict:
    """只读核验一段连续取出的快照行（编号升序、含链路摘要列）。

    rows 每项除快照片段外须含：
      - stored_digest：建链时写入的摘要；无链行（未入链）时为 None
      - stored_prev_digest：建链时写入的前一条摘要；无链行时为 None

    本函数不做端点存在性/倒序检查（由路由层负责）。返回结构：

      {
        "ok": bool,
        "verdict": ...,
        "start_snapshot_id", "end_snapshot_id",
        "genesis_digest": GENESIS_DIGEST,
        "expected_pre_start_digest", "pre_start_digest": 规范化后的入参,
        "expected_end_digest", "end_digest": 规范化后的入参,
        "start_boundary_ok": bool|None, "end_boundary_ok": bool|None,
        "failure": None | {
            "kind": "content_corrupted" | "chain_broken",
            "snapshot_id": 出问题的快照编号,
            "expected_digest" / "expected_prev_digest": 应有值,
            "actual_digest" / "actual_prev_digest": 实际值,
            "previous_snapshot_id": 链接指向的上一条现存编号（链断时）,
            "message": ...,
        },
        "digests": [{"snapshot_id", "digest", "prev_digest"}, ...]  # 已核验部分
      }

    核验顺序（首个可定位的问题即停止）：
      1. 无链行（stored_digest 为 None）→ 链路断裂（该条）；
      2. 从第二条起，stored_prev_digest 必须等于上一条现存快照的
         stored_digest；不符 → 链路断裂（该条）；
      3. 用**存储的**前一条摘要重算内容摘要，与 stored_digest 不符 → 内容损坏。
         区间首条与入参 pre_start_digest 的比对属于**边界**，不在这里判定，
         因此审计员给错起点前摘要不会被误报为内容损坏；
      4. 内部链全部自洽后，再比较起点前 / 终点两条边界摘要；仅边界不符时
         verdict=boundary_mismatch，failure 只描述边界。
    """
    pre = pre_start_digest.lower()
    end = end_digest.lower()
    start_id = rows[0]["id"]
    end_id = rows[-1]["id"]

    result: dict = {
        "ok": False,
        "verdict": None,
        "start_snapshot_id": start_id,
        "end_snapshot_id": end_id,
        "genesis_digest": GENESIS_DIGEST,
        "pre_start_digest": pre,
        "end_digest": end,
        "expected_pre_start_digest": None,
        "expected_end_digest": None,
        "start_boundary_ok": None,
        "end_boundary_ok": None,
        "failure": None,
        "digests": [],
    }

    prev_stored: str | None = None          # 上一条现存快照的存储摘要
    prev_id: int | None = None
    first_pointer_ok: bool | None = None    # 首条 prev 与入参 pre 是否相符

    for row in rows:
        sid = row["id"]
        stored = row["stored_digest"]
        stored_prev = row["stored_prev_digest"]
        expected_prev = pre if prev_stored is None else prev_stored

        # 1) 无链行：建链范围之外（理论上启动建链后不会出现），按链断报告
        if stored is None:
            result["verdict"] = VERDICT_CHAIN_BROKEN
            result["failure"] = {
                "kind": VERDICT_CHAIN_BROKEN,
                "snapshot_id": sid,
                "previous_snapshot_id": prev_id,
                "expected_prev_digest": expected_prev,
                "actual_prev_digest": None,
                "message": (
                    f"快照 {sid} 没有摘要链记录（未入链），链路在该条断裂"
                ),
            }
            return result

        pointer_ok = stored_prev is not None and stored_prev.lower() == expected_prev
        if prev_stored is None:
            # 首条：与入参起点前摘要的比对结果留到边界阶段判定
            first_pointer_ok = pointer_ok
        elif not pointer_ok:
            # 2) 后续条：链接指针与上一条现存快照摘要不符 → 链断
            result["verdict"] = VERDICT_CHAIN_BROKEN
            result["failure"] = {
                "kind": VERDICT_CHAIN_BROKEN,
                "snapshot_id": sid,
                "previous_snapshot_id": prev_id,
                "expected_prev_digest": expected_prev,
                "actual_prev_digest": stored_prev.lower() if stored_prev else None,
                "message": (
                    f"快照 {sid} 的前一条摘要与上一条现存快照（编号 {prev_id}）"
                    f"的摘要不符，链路在该条断裂"
                ),
            }
            return result

        # 3) 以存储的 prev_digest 重算：内容是否被事后改写只取决于
        #    “按链上记录的链接重算能否复现链上记录的摘要”
        recompute_prev = stored_prev.lower() if stored_prev else expected_prev
        recomputed = compute_snapshot_digest(row, recompute_prev)
        if recomputed != stored.lower():
            result["verdict"] = VERDICT_CONTENT_CORRUPTED
            result["failure"] = {
                "kind": VERDICT_CONTENT_CORRUPTED,
                "snapshot_id": sid,
                "previous_snapshot_id": prev_id,
                "expected_digest": recomputed,
                "actual_digest": stored.lower(),
                "message": (
                    f"快照 {sid} 的冻结内容与其建链摘要不符：内容已被改写或损坏"
                ),
            }
            return result

        result["digests"].append(
            {"snapshot_id": sid, "digest": stored.lower(),
             "prev_digest": stored_prev.lower()}
        )
        prev_stored = stored.lower()
        prev_id = sid

    # 4) 内部链自洽（逐条按存储链接重算一致）：核验起点前 / 终点边界
    expected_pre = rows[0]["stored_prev_digest"]
    expected_pre = expected_pre.lower() if expected_pre is not None else None
    expected_end = rows[-1]["stored_digest"].lower()
    result["expected_pre_start_digest"] = expected_pre
    result["expected_end_digest"] = expected_end
    start_ok = bool(first_pointer_ok) and pre == expected_pre
    end_ok = end == expected_end
    result["start_boundary_ok"] = start_ok
    result["end_boundary_ok"] = end_ok

    if start_ok and end_ok:
        result["ok"] = True
        result["verdict"] = VERDICT_OK
    else:
        result["verdict"] = VERDICT_BOUNDARY_MISMATCH
        which = []
        if not start_ok:
            which.append("起点前摘要")
        if not end_ok:
            which.append("终点摘要")
        result["failure"] = {
            "kind": "boundary",
            "snapshot_id": None,
            "message": (
                "区间内部链自洽，但提供的" + "、".join(which)
                + "与链上记录不符（仅边界不符）"
            ),
        }
    return result
