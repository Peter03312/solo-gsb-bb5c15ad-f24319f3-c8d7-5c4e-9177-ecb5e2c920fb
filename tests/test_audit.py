"""快照摘要链纯算法测试（不依赖数据库与第三方库）。

运行：python3 -m unittest tests.test_audit -v
"""
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import audit  # noqa: E402


def _state(i):
    return {
        "nodes": [
            {"id": "S", "type": "source", "name": f"水源{i}", "running": True}
        ],
        "pipes": [],
        "open_faults": [],
        "supplied_sink_ids": [],
    }


def build_rows(n, *, start=1, gap_before=None):
    """构造 n 条已正确接链的快照行；gap_before 给出的步（0 起）编号 +4 模拟空洞。"""
    rows = []
    prev = audit.GENESIS_DIGEST
    sid = start
    for i in range(n):
        if gap_before is not None and i == gap_before:
            sid += 3
        row = {
            "id": sid,
            "op_type": "network_import",
            "network_version": i + 1,
            "event_id": None,
            "state": _state(i),
            "created_at": datetime(2026, 10, 4, 8, 0, i, tzinfo=timezone.utc),
        }
        digest = audit.compute_snapshot_digest(row, prev)
        row.update(stored_digest=digest, stored_prev_digest=prev)
        rows.append(row)
        prev = digest
        sid += 1
    return rows


class CanonicalEncodingTests(unittest.TestCase):
    def test_genesis_digest_is_fixed_sha256(self):
        import hashlib

        self.assertEqual(len(audit.GENESIS_DIGEST), 64)
        self.assertEqual(
            audit.GENESIS_DIGEST,
            hashlib.sha256(b"water-snapshot-chain/v1:genesis").hexdigest(),
        )

    def test_digest_covers_all_required_fields(self):
        row = build_rows(1)[0]
        base = audit.compute_snapshot_digest(row, audit.GENESIS_DIGEST)
        for field, newval in [
            ("id", 999),
            ("op_type", "source_switch"),
            ("network_version", 42),
            ("event_id", 7),
            ("state", {"different": True}),
        ]:
            changed = dict(row, **{field: newval})
            self.assertNotEqual(
                audit.compute_snapshot_digest(changed, audit.GENESIS_DIGEST),
                base,
                f"摘要必须覆盖字段 {field}",
            )
        # 创建时间与前一条摘要也必须进入摘要
        later = dict(row, created_at=row["created_at"] + timedelta(seconds=1))
        self.assertNotEqual(
            audit.compute_snapshot_digest(later, audit.GENESIS_DIGEST), base
        )
        self.assertNotEqual(
            audit.compute_snapshot_digest(row, "f" * 64), base
        )

    def test_timezone_does_not_change_digest(self):
        row = build_rows(1)[0]
        local = dict(row)
        local["created_at"] = row["created_at"].astimezone(
            timezone(timedelta(hours=8))
        )
        self.assertEqual(
            audit.compute_snapshot_digest(local, audit.GENESIS_DIGEST),
            audit.compute_snapshot_digest(row, audit.GENESIS_DIGEST),
        )
        self.assertEqual(
            audit.canonical_created_at(local["created_at"]),
            "2026-10-04T08:00:00.000000Z",
        )

    def test_state_json_key_order_does_not_change_digest(self):
        import json

        row = build_rows(1)[0]
        reorder = dict(row)
        # JSONB 可能重排键：以不同键序的 JSON 字符串输入应得到同一摘要
        reorder["state"] = json.dumps(row["state"], sort_keys=False)
        self.assertEqual(
            audit.compute_snapshot_digest(reorder, audit.GENESIS_DIGEST),
            audit.compute_snapshot_digest(row, audit.GENESIS_DIGEST),
        )

    def test_canonical_json_is_deterministic(self):
        self.assertEqual(
            audit.canonical_json({"b": 1, "a": [1, {"c": 2}]}),
            audit.canonical_json({"a": [1, {"c": 2}], "b": 1}),
        )
        self.assertIn("水", audit.canonical_json({"x": "水源"}))


class VerifyIntervalTests(unittest.TestCase):
    def test_ok_full_range_with_genesis(self):
        rows = build_rows(3)
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest=rows[-1]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_OK)
        self.assertTrue(r["ok"])
        self.assertEqual([d["snapshot_id"] for d in r["digests"]], [1, 2, 3])
        self.assertEqual(r["digests"][0]["prev_digest"], audit.GENESIS_DIGEST)
        self.assertTrue(r["start_boundary_ok"])
        self.assertTrue(r["end_boundary_ok"])

    def test_ok_subinterval_with_prev_existing_digest(self):
        rows = build_rows(4)
        r = audit.verify_interval(
            rows[1:3],
            pre_start_digest=rows[0]["stored_digest"],
            end_digest=rows[2]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_OK)

    def test_id_gap_is_not_a_break(self):
        rows = build_rows(2, gap_before=1)  # id 1, 5
        self.assertEqual([r["id"] for r in rows], [1, 5])
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest=rows[-1]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_OK)

    def test_wrong_end_digest_only_boundary(self):
        rows = build_rows(3)
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest="0" * 64,
        )
        self.assertEqual(r["verdict"], audit.VERDICT_BOUNDARY_MISMATCH)
        self.assertTrue(r["start_boundary_ok"])
        self.assertFalse(r["end_boundary_ok"])
        self.assertEqual(r["failure"]["kind"], "boundary")
        self.assertEqual(len(r["digests"]), 3)  # 全部条目仍回显
        self.assertEqual(r["expected_end_digest"], rows[-1]["stored_digest"])

    def test_wrong_pre_start_digest_only_boundary_not_corruption(self):
        rows = build_rows(3)
        r = audit.verify_interval(
            rows,
            pre_start_digest="a" * 64,
            end_digest=rows[-1]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_BOUNDARY_MISMATCH)
        self.assertFalse(r["start_boundary_ok"])
        self.assertTrue(r["end_boundary_ok"])
        self.assertEqual(
            r["expected_pre_start_digest"], audit.GENESIS_DIGEST
        )

    def test_content_corruption_located_at_first(self):
        rows = build_rows(3)
        rows[0]["state"]["nodes"][0]["name"] = "TAMPERED"
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest=rows[-1]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_CONTENT_CORRUPTED)
        self.assertEqual(r["failure"]["snapshot_id"], 1)
        self.assertEqual(r["digests"], [])

    def test_content_corruption_located_at_middle_and_stops(self):
        rows = build_rows(4)
        rows[2]["state"]["supplied_sink_ids"] = ["X"]
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest=rows[-1]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_CONTENT_CORRUPTED)
        self.assertEqual(r["failure"]["snapshot_id"], 3)
        # 仅返回损坏点之前已核验通过的摘要
        self.assertEqual(
            [d["snapshot_id"] for d in r["digests"]], [1, 2]
        )
        self.assertEqual(
            r["failure"]["expected_digest"],
            audit.compute_snapshot_digest(
                rows[2], rows[2]["stored_prev_digest"]
            ),
        )

    def test_chain_break_located_at_linking_row(self):
        rows = build_rows(4)
        rows[2]["stored_prev_digest"] = "9" * 64
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest=rows[-1]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_CHAIN_BROKEN)
        self.assertEqual(r["failure"]["snapshot_id"], 3)
        self.assertEqual(r["failure"]["previous_snapshot_id"], 2)
        self.assertEqual(
            r["failure"]["expected_prev_digest"], rows[1]["stored_digest"]
        )
        self.assertEqual(r["failure"]["actual_prev_digest"], "9" * 64)
        self.assertEqual(
            [d["snapshot_id"] for d in r["digests"]], [1, 2]
        )

    def test_missing_chain_row_reports_break(self):
        rows = build_rows(3)
        rows[1]["stored_digest"] = None
        rows[1]["stored_prev_digest"] = None
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest="0" * 64,
        )
        self.assertEqual(r["verdict"], audit.VERDICT_CHAIN_BROKEN)
        self.assertEqual(r["failure"]["snapshot_id"], 2)

    def test_internal_corruption_takes_precedence_over_end_boundary(self):
        # 内部损坏 + 终点摘要也不对：必须报告内部损坏，而非仅边界不符
        rows = build_rows(3)
        rows[1]["state"] = {"z": 1}
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest="0" * 64,
        )
        self.assertEqual(r["verdict"], audit.VERDICT_CONTENT_CORRUPTED)
        self.assertEqual(r["failure"]["snapshot_id"], 2)

    def test_digest_case_normalized(self):
        rows = build_rows(2)
        r = audit.verify_interval(
            copy.deepcopy(rows),
            pre_start_digest=audit.GENESIS_DIGEST.upper(),
            end_digest=rows[-1]["stored_digest"].upper(),
        )
        self.assertEqual(r["verdict"], audit.VERDICT_OK)

    def test_singleton_interval(self):
        rows = build_rows(1)
        r = audit.verify_interval(
            rows,
            pre_start_digest=audit.GENESIS_DIGEST,
            end_digest=rows[0]["stored_digest"],
        )
        self.assertEqual(r["verdict"], audit.VERDICT_OK)
        self.assertEqual(len(r["digests"]), 1)

    def test_is_hex_digest(self):
        self.assertTrue(audit.is_hex_digest("a" * 64))
        self.assertTrue(audit.is_hex_digest("A" * 64))
        self.assertFalse(audit.is_hex_digest("g" * 64))
        self.assertFalse(audit.is_hex_digest("a" * 63))
        self.assertFalse(audit.is_hex_digest(None))


if __name__ == "__main__":
    unittest.main()
