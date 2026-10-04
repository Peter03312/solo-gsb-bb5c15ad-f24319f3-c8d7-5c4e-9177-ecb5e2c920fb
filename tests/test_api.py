"""API 路由层集成测试。

本机无 PostgreSQL，使用内存假连接替身替换 app.db 的数据访问函数，
覆盖 FastAPI 路由、请求校验、事务与错误码的完整行为。
运行：python3 -m unittest tests.test_api -v
（需要 fastapi/httpx 依赖）
"""
import json
import os
import sys
import unittest
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app import audit  # noqa: E402
from app.main import app  # noqa: E402
from app.planning import Node, Pipe, freeze_state  # noqa: E402


class FakeStore:
    def __init__(self):
        self.version = 0
        self.updated_at = None
        self.nodes: dict[str, Node] = {}
        self.pipes: dict[str, Pipe] = {}
        self.events = []
        self.next_event_id = 1
        self.snapshots = []
        self.next_snapshot_id = 1
        # 快照摘要链：{snapshot_id: {"digest", "prev_digest"}}
        self.chains = {}
        # 建链起点：None 表示尚未启用（空库）
        self.anchor = None
        self.fail_commit = False


STORE = FakeStore()


@contextmanager
def fake_get_conn():
    yield "FAKE_CONN"


@contextmanager
def fake_transaction():
    conn = "FAKE_TX"
    # 简单快照：提交时把暂存区落盘
    snapshot = (
        STORE.version,
        dict(STORE.nodes),
        {k: Pipe(**v.__dict__) for k, v in STORE.pipes.items()},
    )
    tmp = {
        "version": STORE.version,
        "nodes": dict(STORE.nodes),
        "pipes": dict(STORE.pipes),
        "events": list(STORE.events),
        "next_id": STORE.next_event_id,
        "snapshots": list(STORE.snapshots),
        "next_snapshot_id": STORE.next_snapshot_id,
        "chains": dict(STORE.chains),
        "anchor": STORE.anchor,
    }
    try:
        yield conn
        if STORE.fail_commit:
            raise RuntimeError("simulated commit failure")
    except Exception:
        STORE.version = tmp["version"]
        STORE.nodes = tmp["nodes"]
        STORE.pipes = tmp["pipes"]
        STORE.events = tmp["events"]
        STORE.next_event_id = tmp["next_id"]
        STORE.snapshots = tmp["snapshots"]
        STORE.next_snapshot_id = tmp["next_snapshot_id"]
        STORE.chains = tmp["chains"]
        STORE.anchor = tmp["anchor"]
        raise


def fake_init_db():
    return None


def fake_load_snapshot(conn, for_update=False):
    return STORE.version, STORE.updated_at, dict(STORE.nodes), dict(STORE.pipes)


def fake_open_faults():
    return [
        (e["id"], e["fault_pipe_id"])
        for e in STORE.events
        if e.get("status", "open") == "open"
    ]


def fake_freeze_current():
    return freeze_state(dict(STORE.nodes), dict(STORE.pipes), fake_open_faults())


def fake_insert_state_snapshot(
    conn, *, op_type, network_version, state, event_id=None, anchor_kind=None
):
    import datetime

    sid = STORE.next_snapshot_id
    STORE.next_snapshot_id += 1
    created_at = datetime.datetime(2026, 9, 30, 12, 0, 0)
    STORE.snapshots.append(
        {
            "id": sid,
            "op_type": op_type,
            "network_version": network_version,
            "event_id": event_id,
            "state": state,
            "created_at": created_at,
        }
    )

    # 复制 db.insert_state_snapshot 的接链语义：链接现存上一条，首条接创世摘要
    existing = [s["id"] for s in STORE.snapshots if s["id"] < sid]
    if not existing:
        prev_digest = audit.GENESIS_DIGEST
        kind = anchor_kind or audit.ANCHOR_LIVE_ORIGIN
        if STORE.anchor is None:
            STORE.anchor = {"snapshot_id": sid, "kind": kind}
    else:
        prev_id = max(existing)
        if prev_id not in STORE.chains:
            raise RuntimeError(f"快照 {prev_id} 缺少摘要链记录，无法接链")
        prev_digest = STORE.chains[prev_id]["digest"]
    digest = audit.snapshot_digest(
        snapshot_id=sid,
        op_type=op_type,
        network_version=network_version,
        event_id=event_id,
        created_at=created_at,
        state=state,
        prev_digest=prev_digest,
    )
    STORE.chains[sid] = {"digest": digest, "prev_digest": prev_digest}
    return sid


def fake_replace_network(conn, nodes, pipes):
    STORE.version = 1
    STORE.nodes = {n.id: n for n in nodes}
    STORE.pipes = {p.id: p for p in pipes}
    STORE.events = []
    STORE.next_event_id = 1
    sid = fake_insert_state_snapshot(
        conn,
        op_type="network_import",
        network_version=1,
        state=freeze_state(dict(STORE.nodes), dict(STORE.pipes), []),
    )
    return 1, sid


def fake_submit_isolation(conn, expected_version, fault_pipe_id, close_pipe_ids, result):
    # 复制 db.submit_isolation 的关键语义：锁行版本比对 -> 更新阀位 -> +1 -> 事件
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for pid in close_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, True, p.name
        )
    before = STORE.version
    STORE.version += 1
    eid = STORE.next_event_id
    STORE.next_event_id += 1
    import datetime

    event = {
        "id": eid,
        "version_before": before,
        "version_after": STORE.version,
        "fault_pipe_id": fault_pipe_id,
        "closed_pipe_ids": list(close_pipe_ids),
        "affected_sink_ids": list(result.affected_sink_ids),
        "status": "open",
        "restored_at": None,
        "created_at": datetime.datetime(2026, 9, 30, 12, 0, 0),
    }
    STORE.events.append(event)
    sid = fake_insert_state_snapshot(
        conn,
        op_type="isolation_submit",
        network_version=STORE.version,
        state=fake_freeze_current(),
        event_id=eid,
    )
    return {
        "version": STORE.version,
        "event_id": eid,
        "created_at": event["created_at"],
        "snapshot_id": sid,
    }


def fake_list_events(conn, limit):
    # 与 db.list_events 一致：按事件编号倒序
    return list(reversed(STORE.events))[:limit]


def fake_get_event(conn, event_id, for_update=False):
    for e in STORE.events:
        if e["id"] == event_id:
            return e
    return None


def fake_list_open_events(conn, exclude_event_id=None, for_update=False):
    return [
        e
        for e in STORE.events
        if e.get("status", "open") == "open" and e["id"] != exclude_event_id
    ]


def fake_confirm_restore(
    conn, event_id, expected_version, reopened_pipe_ids, restored_sink_ids
):
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    event = next((e for e in STORE.events if e["id"] == event_id), None)
    if event is None or event.get("status", "open") != "open":
        raise RuntimeError("event not open")
    for pid in reopened_pipe_ids:
        p = STORE.pipes[pid]
        STORE.pipes[pid] = Pipe(
            p.id, p.from_node, p.to_node, p.has_valve, False, p.name
        )
    before = STORE.version
    STORE.version += 1
    import datetime

    restored_at = datetime.datetime(2026, 9, 30, 13, 0, 0)
    event["status"] = "restored"
    event["restored_at"] = restored_at
    sid = fake_insert_state_snapshot(
        conn,
        op_type="restore_confirm",
        network_version=STORE.version,
        state=fake_freeze_current(),
        event_id=event_id,
    )
    return {"version": STORE.version, "restored_at": restored_at, "snapshot_id": sid}


def fake_switch_sources(conn, expected_version, targets):
    # 复制 db.switch_sources 的关键语义：锁行版本比对 -> 更新水源运行状态 -> +1
    if STORE.version != expected_version:
        raise db.VersionConflict(STORE.version, expected_version)
    for source_id, running in targets:
        node = STORE.nodes.get(source_id)
        if node is None or node.type != "source":
            raise RuntimeError(f"水源节点不存在或不可切换：{source_id}")
        STORE.nodes[source_id] = Node(node.id, node.type, node.name, running)
    STORE.version += 1
    sid = fake_insert_state_snapshot(
        conn,
        op_type="source_switch",
        network_version=STORE.version,
        state=fake_freeze_current(),
    )
    return {"version": STORE.version, "snapshot_id": sid}


def fake_get_state_snapshot(conn, snapshot_id):
    for s in STORE.snapshots:
        if s["id"] == snapshot_id:
            return s
    return None


def fake_list_state_snapshots(conn, *, limit, after_id=None):
    rows = [s for s in STORE.snapshots if after_id is None or s["id"] > after_id]
    return rows[:limit]


def fake_get_previous_state_snapshot(conn, snapshot_id):
    previous = [s for s in STORE.snapshots if s["id"] < snapshot_id]
    return previous[-1] if previous else None


# ---- 快照摘要链（审计）替身 ----


def fake_state_snapshot_exists(conn, snapshot_id):
    return any(s["id"] == snapshot_id for s in STORE.snapshots)


def fake_list_snapshot_chain_rows(conn, start_id, end_id):
    rows = []
    for s in sorted(STORE.snapshots, key=lambda r: r["id"]):
        if start_id <= s["id"] <= end_id:
            chain = STORE.chains.get(s["id"])
            rows.append(
                {
                    **s,
                    "stored_digest": chain["digest"] if chain else None,
                    "stored_prev_digest": chain["prev_digest"] if chain else None,
                }
            )
    return rows


def fake_get_chain_info(conn):
    ids = [s["id"] for s in STORE.snapshots]
    return {
        "anchor_snapshot_id": STORE.anchor["snapshot_id"] if STORE.anchor else None,
        "anchor_kind": STORE.anchor["kind"] if STORE.anchor else None,
        "anchored_at": None,
        "snapshot_count": len(ids),
        "chained_count": len(STORE.chains),
        "earliest_snapshot_id": min(ids) if ids else None,
        "latest_snapshot_id": max(ids) if ids else None,
    }


# 替换数据访问层
db.get_conn = fake_get_conn
db.transaction = fake_transaction
db.init_db = fake_init_db
db.load_snapshot = fake_load_snapshot
db.replace_network = fake_replace_network
db.submit_isolation = fake_submit_isolation
db.list_events = fake_list_events
db.get_event = fake_get_event
db.list_open_events = fake_list_open_events
db.confirm_restore = fake_confirm_restore
db.switch_sources = fake_switch_sources
db.insert_state_snapshot = fake_insert_state_snapshot
db.get_state_snapshot = fake_get_state_snapshot
db.list_state_snapshots = fake_list_state_snapshots
db.get_previous_state_snapshot = fake_get_previous_state_snapshot
db.state_snapshot_exists = fake_state_snapshot_exists
db.list_snapshot_chain_rows = fake_list_snapshot_chain_rows
db.get_chain_info = fake_get_chain_info


VALID_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source", "name": "一水厂"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
    ],
}


RESTORE_NETWORK = {
    "nodes": [
        {"id": "S", "type": "source"},
        {"id": "A", "type": "junction"},
        {"id": "B", "type": "junction"},
        {"id": "D", "type": "junction"},
        {"id": "E", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
        {"id": "U3", "type": "sink"},
        {"id": "U4", "type": "sink"},
    ],
    "pipes": [
        {"id": "P1", "from": "S", "to": "A", "has_valve": True},
        {"id": "P2", "from": "A", "to": "B", "has_valve": True},
        {"id": "P3", "from": "A", "to": "U1", "has_valve": True},
        {"id": "P5", "from": "B", "to": "U2", "has_valve": True},
        {"id": "P4", "from": "B", "to": "D", "has_valve": True},
        {"id": "P6", "from": "D", "to": "U3", "has_valve": False},
        # P7 为已隔离区内的无阀管段（E2 故障管段），自身无阀可关
        {"id": "P7", "from": "D", "to": "E", "has_valve": False},
        {"id": "P8", "from": "E", "to": "U4", "has_valve": True},
    ],
}


# 双水源管网（演示水源停运/恢复）：
# S1 --Q1[阀]-- C --Q2[阀]-- U1
# S2 --Q3[阀]-- C --Q4[阀]-- U2
TWO_SOURCE_NETWORK = {
    "nodes": [
        {"id": "S1", "type": "source"},
        {"id": "S2", "type": "source"},
        {"id": "C", "type": "junction"},
        {"id": "U1", "type": "sink"},
        {"id": "U2", "type": "sink"},
    ],
    "pipes": [
        {"id": "Q1", "from": "S1", "to": "C", "has_valve": True},
        {"id": "Q3", "from": "S2", "to": "C", "has_valve": True},
        {"id": "Q2", "from": "C", "to": "U1", "has_valve": True},
        {"id": "Q4", "from": "C", "to": "U2", "has_valve": True},
    ],
}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # lifespan 会调用 init_db（已替换为 fake）
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _import(self):
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_health(self):
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"status": "ok"})

    def test_full_happy_path(self):
        info = self._import()
        self.assertEqual(info["version"], 1)

        r = self.client.get("/api/network")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertEqual(len(body["nodes"]), 7)
        self.assertEqual(len(body["pipes"]), 6)
        # 序列化别名：from/to
        p1 = next(p for p in body["pipes"] if p["id"] == "P1")
        self.assertEqual(p1["from"], "S")
        self.assertEqual(p1["to"], "A")
        # 无阀管段 valve_closed 序列化为 null
        p6 = next(p for p in body["pipes"] if p["id"] == "P6")
        self.assertIsNone(p6["valve_closed"])
        # 导入的水源初始视为运行；非水源节点 running 恒为 true
        src = next(n for n in body["nodes"] if n["id"] == "S")
        self.assertTrue(src["running"])
        u1 = next(n for n in body["nodes"] if n["id"] == "U1")
        self.assertTrue(u1["running"])

        # 不可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["isolatable"])

        # 可行预演
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertTrue(body["version_match"])

        # 提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertEqual(body["closed_pipe_ids"], ["P4"])
        self.assertEqual(body["event_id"], 1)

        # 阀位确实落库
        r = self.client.get("/api/network")
        p4 = next(p for p in r.json()["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])

        # 历史
        r = self.client.get("/api/isolation/events")
        self.assertEqual(r.status_code, 200)
        events = r.json()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["fault_pipe_id"], "P6")
        self.assertEqual(events[0]["closed_pipe_ids"], ["P4"])

    def test_import_rejects_dangling_self_loop_duplicate(self):
        bad = {
            "nodes": [
                {"id": "S", "type": "source"},
                {"id": "S", "type": "junction"},
            ],
            "pipes": [
                {"id": "P1", "from": "S", "to": "X"},
                {"id": "P1", "from": "S", "to": "S"},
            ],
        }
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("DUPLICATE_NODE_ID", codes)
        self.assertIn("DUPLICATE_PIPE_ID", codes)
        self.assertIn("PIPE_ENDPOINT_UNKNOWN", codes)
        self.assertIn("PIPE_SELF_LOOP", codes)
        # 拒绝后状态不变（空网版本 0）
        r = self.client.get("/api/network")
        self.assertEqual(r.json()["version"], 0)

    def test_non_valve_pipe_initial_closed_rejected(self):
        bad = dict(VALID_NETWORK)
        bad["pipes"] = [
            dict(p, valve_closed=True) if p["id"] == "P6" else p
            for p in VALID_NETWORK["pipes"]
        ]
        r = self.client.post("/api/network/import", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertIn("NON_VALVE_PIPE_CLOSED", r.text)

    def test_submit_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 1)

    def test_submit_no_valve_pipe(self):
        self._import()
        # 关闭无阀管段 P6（以 P5 为故障管段）-> 422 CLOSE_PIPE_NO_VALVE
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_NO_VALVE", r.text)
        self.assertEqual(STORE.version, 1)

    def test_submit_duplicate_and_already_closed(self):
        self._import()
        # 列表内重复
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4", "P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

        # 先成功关 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)

        # 版本 2 下再次关已关闭的 P4
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 2},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_DUPLICATE", r.text)

    def test_submit_not_isolatable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "NOT_ISOLATABLE")
        self.assertEqual(STORE.version, 1)

    def test_submit_fault_pipe_as_valve(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P6"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("CLOSE_PIPE_IS_FAULT", r.text)

    def test_preview_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)

    def test_request_body_validation_422(self):
        self._import()
        # version 不允许负数
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P6", "close_pipe_ids": [], "version": -1},
        )
        self.assertEqual(r.status_code, 422)

    def test_submit_against_empty_network_is_version_conflict(self):
        # 空网版本为 0：基于版本 1 提交应得到 409，而非参数校验错误
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")

    def test_reimport_resets_version_and_events(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(STORE.version, 2)
        # 重新导入 -> 版本重置为 1、历史清空
        r = self.client.post("/api/network/import", json=VALID_NETWORK)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(STORE.events, [])

    def test_failed_submit_rolls_back(self):
        self._import()
        STORE.fail_commit = True
        # TestClient 默认抛出服务端异常；真实 HTTP 下客户端会收到 500。
        # 关键断言：事务回滚后版本与阀位均不变。
        with self.assertRaises(RuntimeError):
            self.client.post(
                "/api/isolation/submit",
                json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
            )
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    # ---------------------------------------------------------- 复供

    def _submit_two_faults(self):
        # 导入复供演示管网
        r = self.client.post("/api/network/import", json=RESTORE_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        # 事件 1：故障 P6（D-U3），关 P4（B-D）隔离整个 D 片区
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 1)
        # 事件 2：故障 P7（D-E，无阀）。D 片区已被 P4 隔离，
        # 故无需再关阀门（P8 阀保持开启），提交空方案即可隔离
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P7", "close_pipe_ids": [], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 2)
        self.assertEqual(STORE.version, 3)

    def test_restore_preview_readonly_and_safe_for_inner_fault(self):
        self._submit_two_faults()
        # 复供事件 2（片区内故障 P7，未关任何阀）：重开集合为空，
        # 仍关闭的 P4 继续阻断，其余故障 P6 两端 D/U3 均不可达 -> 安全
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["version_match"])
        self.assertEqual(body["status"], "open")
        self.assertEqual(body["fault_pipe_id"], "P7")
        self.assertEqual(body["closed_pipe_ids"], [])
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], [])
        self.assertEqual(body["block_reasons"], [])
        # 预演不改状态
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertFalse(STORE.pipes["P8"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "open")

    def test_restore_preview_flags_rewater_risk_for_outer_fault(self):
        self._submit_two_faults()
        # 复供事件 1（外层故障 P6，关 P4）：重开 P4 后 D 区进水，
        # 仍待修复的 P7 的 D 端将重新从水源到达（仅一端进水即有风险）-> 禁止复供。
        # U3 恢复；U4 因故障管段 P7 本身仍阻断，暂不恢复。
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3"])
        self.assertEqual(len(body["block_reasons"]), 1)
        block = body["block_reasons"][0]
        self.assertEqual(block["code"], "FAULT_REWATER_RISK")
        self.assertEqual(block["pipe_id"], "P7")
        self.assertEqual(block["reachable_endpoints"], ["D"])

    def test_restore_confirm_rejected_when_unsafe(self):
        self._submit_two_faults()
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 3}
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "RESTORE_UNSAFE")
        self.assertIn("FAULT_REWATER_RISK", r.text)
        # 无部分变更：阀位、版本、事件状态均不变
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[-1]["status"], "open")
        self.assertEqual(STORE.events[0]["status"], "open")

    def test_restore_confirm_safe_then_outer_becomes_restorable(self):
        self._submit_two_faults()
        # 先安全复供区内事件 2（没有阀门被重开，不恢复用水点）
        r = self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 4)
        self.assertEqual(body["status"], "restored")
        self.assertEqual(body["reopened_pipe_ids"], [])
        self.assertEqual(body["restored_sink_ids"], [])
        # P4 仍关闭
        self.assertTrue(STORE.pipes["P4"].valve_closed)

        # 其余待修复故障只剩 P6，复供事件 1 变安全，U3/U4 恢复
        r = self.client.post(
            "/api/isolation/events/1/restore/preview", json={"version": 4}
        )
        body = r.json()
        self.assertTrue(body["safe"])
        self.assertEqual(body["restored_sink_ids"], ["U3", "U4"])

        r = self.client.post("/api/isolation/events/1/restore", json={"version": 4})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 5)
        self.assertEqual(r.json()["restored_sink_ids"], ["U3", "U4"])
        self.assertFalse(STORE.pipes["P4"].valve_closed)

    def test_restore_event_not_found_and_already_restored(self):
        self._submit_two_faults()
        # 不存在的事件 -> 404
        r = self.client.post(
            "/api/isolation/events/999/restore/preview", json={"version": 3}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")
        r = self.client.post(
            "/api/isolation/events/999/restore", json={"version": 3}
        )
        self.assertEqual(r.status_code, 404)

        # 成功复供事件 2 后重复复供 -> 409
        r = self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 4}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "EVENT_ALREADY_RESTORED")
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 4}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(STORE.version, 4)

    def test_restore_version_conflict(self):
        self._submit_two_faults()
        r = self.client.post(
            "/api/isolation/events/2/restore", json={"version": 99}
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 状态不变
        self.assertEqual(STORE.version, 3)
        self.assertTrue(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events[0]["status"], "open")
        # 预演不拒绝过期版本，只标记 version_match=false
        r = self.client.post(
            "/api/isolation/events/2/restore/preview", json={"version": 99}
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])

    def test_events_history_contains_status_fields(self):
        self._submit_two_faults()
        r = self.client.get("/api/isolation/events")
        events = r.json()
        self.assertEqual([e["id"] for e in events], [2, 1])
        for e in events:
            self.assertEqual(e["status"], "open")
            self.assertIsNone(e["restored_at"])
        # 复供后历史反映新状态
        self.client.post("/api/isolation/events/2/restore", json={"version": 3})
        events = self.client.get("/api/isolation/events").json()
        e2 = next(e for e in events if e["id"] == 2)
        self.assertEqual(e2["status"], "restored")
        self.assertIsNotNone(e2["restored_at"])

    def test_preview_and_submit_count_pending_faults(self):
        self._submit_two_faults()
        # 两条待修复事件后：D 片区（U3/U4）已因关 P4 与故障 P7 失供。
        # 对 P5（B-U2）做隔离预演（关 P2 隔离 B 侧）：通水计算排除待修复故障
        # 管段 P6/P7，U3/U4 在方案前已失供不重复计入；新增失供为 U2。
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 3},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U2"])

    # ---------------------------------------------------------- 关阀建议

    def test_recommend_basic_and_submittable(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["version_match"])
        self.assertEqual(body["fault_pipe_id"], "P6")
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], ["P4"])
        self.assertEqual(body["affected_sink_ids"], ["U3"])
        self.assertIsNone(body["reason"])
        # 只读：版本、阀位、事件均未变化
        self.assertEqual(STORE.version, 1)
        self.assertFalse(STORE.pipes["P4"].valve_closed)
        self.assertEqual(STORE.events, [])
        # 建议可直接提交：同一版本提交成功
        r = self.client.post(
            "/api/isolation/submit",
            json={
                "fault_pipe_id": "P6",
                "close_pipe_ids": body["recommended_close_pipe_ids"],
                "version": body["version"],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["closed_pipe_ids"], ["P4"])
        self.assertEqual(r.json()["affected_sink_ids"], ["U3"])

    def test_recommend_stale_version_flag(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 5},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["version_match"])
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["feasible"])

    def test_recommend_fault_pipe_not_found(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "PX", "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], [])
        self.assertEqual(body["affected_sink_ids"], [])
        self.assertEqual(body["reason"]["code"], "FAULT_PIPE_NOT_FOUND")
        # 不改状态
        self.assertEqual(STORE.version, 1)

    def test_recommend_no_feasible_plan(self):
        self._import()
        # 故障管段 P1 直连水源：S 端恒可达，无可行方案
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P1", "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["feasible"])
        self.assertEqual(body["reason"]["code"], "NO_FEASIBLE_PLAN")
        self.assertEqual(STORE.version, 1)

    def test_recommend_excludes_closed_valves_and_counts_pending_faults(self):
        self._import()
        # 先提交事件：故障 P6，关 P4（版本变为 2）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 新故障 P5（B-U2）：建议不得包含已关闭的 P4，
        # 待修复故障 P6 不计通行，U3 已失供不重复计入
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P5", "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], ["P2"])
        self.assertEqual(body["affected_sink_ids"], ["U2"])
        # 只读：建议后版本与阀位不变
        self.assertEqual(STORE.version, 2)
        # 按建议提交成功（版本 3）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 3)

    def test_recommend_empty_plan_when_fault_already_isolated(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 故障管段两端已与水源断开：建议空方案
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 2},
        )
        body = r.json()
        self.assertTrue(body["feasible"])
        self.assertEqual(body["recommended_close_pipe_ids"], [])
        self.assertEqual(body["affected_sink_ids"], [])

    def test_recommend_submit_stale_version_conflict(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 1},
        )
        rec = r.json()
        # 期间发生另一次提交，版本变为 2：按旧版本提交建议 -> 409（沿用现有冲突处理）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P5", "close_pipe_ids": ["P2"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post(
            "/api/isolation/submit",
            json={
                "fault_pipe_id": "P6",
                "close_pipe_ids": rec["recommended_close_pipe_ids"],
                "version": rec["version"],
            },
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        # 重新按最新版本建议并提交 -> 成功
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": 2},
        )
        rec = r.json()
        self.assertTrue(rec["version_match"])
        r = self.client.post(
            "/api/isolation/submit",
            json={
                "fault_pipe_id": "P6",
                "close_pipe_ids": rec["recommended_close_pipe_ids"],
                "version": rec["version"],
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 3)

    def test_recommend_request_body_validation_422(self):
        self._import()
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "P6", "version": -1},
        )
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/api/isolation/recommend", json={"version": 1})
        self.assertEqual(r.status_code, 422)

    def test_reimport_invalidates_old_events_for_restore(self):
        self._submit_two_faults()
        r = self.client.post("/api/network/import", json=RESTORE_NETWORK)
        self.assertEqual(r.status_code, 200)
        # 整网替换后旧事件失效（历史清空）：复供旧事件编号 -> 404
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "EVENT_NOT_FOUND")

    # ---------------------------------------------------------- 水源状态切换

    def test_imported_sources_initially_running(self):
        r = self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        self.assertEqual(r.status_code, 200, r.text)
        nodes = {n["id"]: n for n in self.client.get("/api/network").json()["nodes"]}
        self.assertTrue(nodes["S1"]["running"])
        self.assertTrue(nodes["S2"]["running"])

    def test_switch_preview_stop_one_source_is_read_only(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 停运 S1：S2 经 C 顶供，全部用水点仍可达，无新增失供
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["version_match"])
        self.assertTrue(body["changed"])
        self.assertTrue(body["safe"])
        self.assertEqual(body["targets"], [{"source_id": "S1", "running": False}])
        self.assertEqual(body["affected_sink_ids"], [])
        self.assertEqual(body["restored_sink_ids"], [])
        self.assertEqual(body["block_reasons"], [])
        self.assertEqual(body["issues"], [])
        # 只读：版本与水源状态不变
        self.assertEqual(STORE.version, 1)
        self.assertTrue(STORE.nodes["S1"].running)

    def test_switch_confirm_stop_all_sources_cuts_all_sinks(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 同时停运两座水源：允许全网无运行水源，U1/U2 全部新增失供
        r = self.client.post(
            "/api/sources/switch",
            json={
                "targets": [
                    {"source_id": "S1", "running": False},
                    {"source_id": "S2", "running": False},
                ],
                "version": 1,
            },
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["version"], 2)
        self.assertEqual(body["affected_sink_ids"], ["U1", "U2"])
        self.assertEqual(body["restored_sink_ids"], [])
        # 水源状态落库
        self.assertFalse(STORE.nodes["S1"].running)
        self.assertFalse(STORE.nodes["S2"].running)

    def test_switch_restore_source_restores_sinks(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 停运全部 -> 恢复 S1：U1/U2 恢复供水，版本 3
        self.client.post(
            "/api/sources/switch",
            json={
                "targets": [
                    {"source_id": "S1", "running": False},
                    {"source_id": "S2", "running": False},
                ],
                "version": 1,
            },
        )
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": True}], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["version"], 3)
        self.assertEqual(body["restored_sink_ids"], ["U1", "U2"])
        self.assertEqual(body["affected_sink_ids"], [])
        self.assertTrue(STORE.nodes["S1"].running)
        self.assertFalse(STORE.nodes["S2"].running)

    def test_switch_noop_does_not_advance_version(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 目标状态全部已满足：changed=false，版本不变（预演与确认一致）
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"targets": [{"source_id": "S1", "running": True}], "version": 1},
        )
        body = r.json()
        self.assertFalse(body["changed"])
        self.assertTrue(body["safe"])

        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": True}], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertFalse(body["changed"])
        self.assertEqual(body["version"], 1)
        self.assertEqual(STORE.version, 1)
        self.assertTrue(STORE.nodes["S1"].running)

    def test_switch_invalid_source_in_preview_and_confirm(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 预演：编号不存在 / 非水源节点 / 同批重复，进入 issues（只读，200）
        r = self.client.post(
            "/api/sources/switch/preview",
            json={
                "targets": [
                    {"source_id": "SX", "running": False},
                    {"source_id": "U1", "running": False},
                    {"source_id": "S1", "running": False},
                    {"source_id": "S1", "running": True},
                ],
                "version": 1,
            },
        )
        self.assertEqual(r.status_code, 200)
        codes = {i["code"] for i in r.json()["issues"]}
        self.assertIn("SOURCE_NOT_FOUND", codes)
        self.assertIn("SOURCE_NOT_SWITCHABLE", codes)
        self.assertIn("SOURCE_TARGET_DUPLICATE", codes)
        self.assertFalse(r.json()["safe"])
        self.assertEqual(STORE.version, 1)

        # 确认：结构问题 -> 422 SOURCE_SWITCH_INVALID，无部分写入
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "SX", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SOURCE_SWITCH_INVALID")
        self.assertIn("SOURCE_NOT_FOUND", r.text)
        self.assertEqual(STORE.version, 1)

        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "U1", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("SOURCE_NOT_SWITCHABLE", r.text)

        r = self.client.post(
            "/api/sources/switch",
            json={
                "targets": [
                    {"source_id": "S1", "running": False},
                    {"source_id": "S1", "running": True},
                ],
                "version": 1,
            },
        )
        self.assertEqual(r.status_code, 422)
        self.assertIn("SOURCE_TARGET_DUPLICATE", r.text)

    def test_switch_version_conflict(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "VERSION_CONFLICT")
        self.assertEqual(STORE.version, 1)
        self.assertTrue(STORE.nodes["S1"].running)
        # 预演对过期版本不拒绝，只标记 version_match=false
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 99},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["version_match"])

    def test_switch_restore_blocked_by_pending_fault_rewater_risk(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 故障 Q2（C-U1，无阀）：C 侧多源，关阀无法隔离（Q1/Q3 都进水），
        # 先停运 S2 再隔离（停运不推进故障风险，只是改水源运行态）：
        # 停运 S2 后，C 只由 S1 供水；关 Q1 即可隔离 Q2 的 C 端。
        self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S2", "running": False}], "version": 1},
        )
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "Q2", "close_pipe_ids": ["Q1"], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 1)
        # 此时待修复故障 Q2 的 C/U1 两端均不可达（S1 被 Q1 关断，S2 停运）

        # 预演恢复 S2：Q2 的 C 端将经 Q3 重新进水 -> safe=false
        r = self.client.post(
            "/api/sources/switch/preview",
            json={"targets": [{"source_id": "S2", "running": True}], "version": 3},
        )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["changed"])
        self.assertFalse(body["safe"])
        self.assertEqual(len(body["block_reasons"]), 1)
        block = body["block_reasons"][0]
        self.assertEqual(block["code"], "FAULT_REWATER_RISK")
        self.assertEqual(block["pipe_id"], "Q2")
        self.assertEqual(block["reachable_endpoints"], ["C"])
        # U2 恢复供水（C 经 S2 进水但 Q2 阻断 U1，故仅 U2）
        self.assertEqual(body["restored_sink_ids"], ["U2"])

        # 强行确认 -> 422 SOURCE_SWITCH_UNSAFE，无部分写入
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S2", "running": True}], "version": 3},
        )
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SOURCE_SWITCH_UNSAFE")
        self.assertIn("FAULT_REWATER_RISK", r.text)
        self.assertEqual(STORE.version, 3)
        self.assertFalse(STORE.nodes["S2"].running)

    def test_switch_stop_source_is_always_safe(self):
        # 停运只会收缩可达范围：有待修复故障时停运水源也不会引入重新进水风险。
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 停运 S2（v2）-> 关 Q1 隔离待修复故障 Q2（v3）：C 两侧均断开
        self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S2", "running": False}], "version": 1},
        )
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "Q2", "close_pipe_ids": ["Q1"], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        # 再停运仍在运行的 S1：运行状态发生变化（changed=true），
        # 但 C 此前已不可达，无新增失供、无重新进水风险，版本推进到 4
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 3},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["changed"])
        self.assertEqual(body["affected_sink_ids"], [])
        self.assertEqual(body["restored_sink_ids"], [])
        self.assertEqual(body["version"], 4)
        self.assertFalse(STORE.nodes["S1"].running)
        self.assertFalse(STORE.nodes["S2"].running)

    def test_isolation_and_recommend_ignore_stopped_sources(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # 停运 S1 后，对 Q2（C-U1）做隔离：只有 S2 仍供水，关 Q3 即可隔离 C
        self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 1},
        )
        r = self.client.post(
            "/api/isolation/preview",
            json={"fault_pipe_id": "Q2", "close_pipe_ids": ["Q3"], "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["isolatable"])
        self.assertEqual(body["affected_sink_ids"], ["U1", "U2"])

        # 关阀建议同样只按运行水源计算：建议关 Q3（而非停运的 S1 侧 Q1）
        r = self.client.post(
            "/api/isolation/recommend",
            json={"fault_pipe_id": "Q2", "version": 2},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["recommended_close_pipe_ids"], ["Q3"])

    def test_switch_request_body_validation_422(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        # targets 不能为空
        r = self.client.post(
            "/api/sources/switch/preview", json={"targets": [], "version": 1}
        )
        self.assertEqual(r.status_code, 422)
        # version 不允许负数 / 缺字段
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": False}], "version": -1},
        )
        self.assertEqual(r.status_code, 422)
        r = self.client.post(
            "/api/sources/switch", json={"targets": [{"running": False}], "version": 1}
        )
        self.assertEqual(r.status_code, 422)

    # ---------------------------------------------------------- 状态快照

    def _snapshot(self, snapshot_id):
        r = self.client.get(f"/api/snapshots/{snapshot_id}")
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_import_freezes_snapshot_and_response_carries_id(self):
        info = self._import()
        self.assertIn("snapshot_id", info)
        sid = info["snapshot_id"]
        self.assertEqual(sid, 1)
        snap = self._snapshot(sid)
        self.assertEqual(snap["op_type"], "network_import")
        self.assertEqual(snap["network_version"], 1)
        self.assertIsNone(snap["event_id"])
        self.assertEqual(len(snap["state"]["nodes"]), 7)
        self.assertEqual(len(snap["state"]["pipes"]), 6)
        # 无待修复故障；全部用水点由运行水源可达
        self.assertEqual(snap["state"]["open_faults"], [])
        self.assertEqual(
            snap["state"]["supplied_sink_ids"], ["U1", "U2", "U3"]
        )
        # 冻结载荷里无阀管段 valve_closed 为布尔 false（非 null）
        p6 = next(p for p in snap["state"]["pipes"] if p["id"] == "P6")
        self.assertFalse(p6["valve_closed"])

    def test_snapshot_records_valves_faults_and_supplied_sinks_after_submit(self):
        self._import()
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = r.json()
        sid = body["snapshot_id"]
        self.assertEqual(sid, 2)
        snap = self._snapshot(sid)
        self.assertEqual(snap["op_type"], "isolation_submit")
        self.assertEqual(snap["network_version"], 2)
        self.assertEqual(snap["event_id"], body["event_id"])
        # 阀位已冻结为关闭
        p4 = next(p for p in snap["state"]["pipes"] if p["id"] == "P4")
        self.assertTrue(p4["valve_closed"])
        # 待修复故障记录了事件编号与故障管段
        self.assertEqual(
            snap["state"]["open_faults"],
            [{"event_id": body["event_id"], "fault_pipe_id": "P6"}],
        )
        # U3 因隔离不可达；U1/U2 仍供水
        self.assertEqual(snap["state"]["supplied_sink_ids"], ["U1", "U2"])

    def test_snapshot_not_found_is_explicit_error(self):
        r = self.client.get("/api/snapshots/999")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_NOT_FOUND")
        r = self.client.get("/api/snapshots/999/diff")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_NOT_FOUND")

    def test_snapshot_list_order_limit_cursor(self):
        self._import()  # 快照 1
        self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )  # 快照 2
        self.client.post(
            "/api/isolation/events/1/restore", json={"version": 2}
        )  # 快照 3
        r = self.client.get("/api/snapshots")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([s["id"] for s in r.json()], [1, 2, 3])
        # limit
        r = self.client.get("/api/snapshots?limit=2")
        self.assertEqual([s["id"] for s in r.json()], [1, 2])
        # 游标翻页
        r = self.client.get("/api/snapshots?after_id=2")
        self.assertEqual([s["id"] for s in r.json()], [3])
        r = self.client.get("/api/snapshots?after_id=3")
        self.assertEqual(r.json(), [])

    def test_first_snapshot_diff_treats_all_as_added(self):
        self._import()
        r = self.client.get("/api/snapshots/1/diff")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIsNone(body["base_snapshot_id"])
        self.assertEqual(body["target_snapshot_id"], 1)
        self.assertIsNone(body["base_network_version"])
        self.assertEqual(body["target_network_version"], 1)
        self.assertEqual(len(body["nodes"]["added"]), 7)
        self.assertEqual(body["nodes"]["removed"], [])
        self.assertEqual(len(body["pipes"]["added"]), 6)
        self.assertEqual(
            body["supplied_sinks"]["added"], ["U1", "U2", "U3"]
        )
        self.assertEqual(body["supplied_sinks"]["removed"], [])
        self.assertEqual(body["valve_positions"]["changed"], [])
        self.assertEqual(body["source_states"]["changed"], [])
        self.assertEqual(body["open_faults"]["added"], [])

    def test_diff_isolation_submit_lists_valve_fault_sink_net_changes(self):
        self._import()
        self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        r = self.client.get("/api/snapshots/2/diff")
        body = r.json()
        self.assertEqual(body["base_snapshot_id"], 1)
        # 节点/管段无增删
        self.assertEqual(body["nodes"]["added"], [])
        self.assertEqual(body["nodes"]["removed"], [])
        self.assertEqual(body["pipes"]["added"], [])
        self.assertEqual(body["pipes"]["removed"], [])
        # P4 阀位：false -> true
        self.assertEqual(
            body["valve_positions"]["changed"],
            [
                {
                    "pipe_id": "P4",
                    "valve_closed_before": False,
                    "valve_closed_after": True,
                }
            ],
        )
        # 新增待修复故障（事件 1 / P6）
        self.assertEqual(
            body["open_faults"]["added"],
            [{"event_id": 1, "fault_pipe_id": "P6"}],
        )
        self.assertEqual(body["open_faults"]["removed"], [])
        # 供水点净变化：U3 移除
        self.assertEqual(body["supplied_sinks"]["added"], [])
        self.assertEqual(body["supplied_sinks"]["removed"], ["U3"])
        # 水源状态未变
        self.assertEqual(body["source_states"]["changed"], [])

    def test_diff_restore_confirms_reopens_valve_removes_fault_restores_sink(self):
        self._import()
        self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        r = self.client.post(
            "/api/isolation/events/1/restore", json={"version": 2}
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = self.client.get("/api/snapshots/3/diff").json()
        self.assertEqual(
            body["valve_positions"]["changed"],
            [
                {
                    "pipe_id": "P4",
                    "valve_closed_before": True,
                    "valve_closed_after": False,
                }
            ],
        )
        self.assertEqual(
            body["open_faults"]["removed"],
            [{"event_id": 1, "fault_pipe_id": "P6"}],
        )
        self.assertEqual(body["supplied_sinks"]["added"], ["U3"])
        self.assertEqual(body["supplied_sinks"]["removed"], [])

    def test_diff_source_switch_lists_source_state_and_sink_changes(self):
        self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)  # 快照 1
        # 同时停运双水源 -> 快照 2：U1/U2 全部失供
        self.client.post(
            "/api/sources/switch",
            json={
                "targets": [
                    {"source_id": "S1", "running": False},
                    {"source_id": "S2", "running": False},
                ],
                "version": 1,
            },
        )
        body = self.client.get("/api/snapshots/2/diff").json()
        self.assertEqual(
            sorted(body["source_states"]["changed"], key=lambda c: c["source_id"]),
            [
                {"source_id": "S1", "running_before": True, "running_after": False},
                {"source_id": "S2", "running_before": True, "running_after": False},
            ],
        )
        self.assertEqual(body["supplied_sinks"]["removed"], ["U1", "U2"])
        self.assertEqual(body["open_faults"]["added"], [])

        # 恢复 S1 -> 快照 3：U1/U2 恢复
        self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": True}], "version": 2},
        )
        body = self.client.get("/api/snapshots/3/diff").json()
        self.assertEqual(
            body["source_states"]["changed"],
            [{"source_id": "S1", "running_before": False, "running_after": True}],
        )
        self.assertEqual(body["supplied_sinks"]["added"], ["U1", "U2"])

    def test_noop_switch_leaves_no_snapshot(self):
        self._import()
        before = self.client.get("/api/snapshots").json()
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S", "running": True}], "version": 1},
        )
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["changed"])
        self.assertIsNone(r.json()["snapshot_id"])
        after = self.client.get("/api/snapshots").json()
        self.assertEqual(before, after)  # 无新快照

    def test_failed_writes_leave_no_snapshot(self):
        self._import()  # 快照 1
        # 版本冲突：无新快照
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 99},
        )
        self.assertEqual(r.status_code, 409)
        # 不可隔离方案 422：无新快照
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P5"], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        # 非法水源切换 422：无新快照
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "SX", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 422)
        ids = [s["id"] for s in self.client.get("/api/snapshots").json()]
        self.assertEqual(ids, [1])

    def test_reimport_does_not_overwrite_earlier_snapshots(self):
        self._import()  # 快照 1（旧网 7 节点）
        self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )  # 快照 2
        # 整网重新导入：版本重置为 1，但快照编号继续递增、不得覆盖此前快照
        r = self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["version"], 1)
        self.assertEqual(r.json()["snapshot_id"], 3)
        ids = [s["id"] for s in self.client.get("/api/snapshots").json()]
        self.assertEqual(ids, [1, 2, 3])
        # 旧快照仍是旧网内容（节点 7 个、版本 2），未被当前管网回填
        old = self._snapshot(2)
        self.assertEqual(old["network_version"], 2)
        self.assertEqual(len(old["state"]["nodes"]), 7)
        self.assertEqual(
            old["state"]["open_faults"],
            [{"event_id": 1, "fault_pipe_id": "P6"}],
        )
        # 新快照为新网（5 节点、版本 1、无待修复故障）
        new = self._snapshot(3)
        self.assertEqual(new["network_version"], 1)
        self.assertEqual(len(new["state"]["nodes"]), 5)
        self.assertEqual(new["state"]["open_faults"], [])

    def test_reimport_diff_shows_node_pipe_replacement(self):
        self._import()  # 快照 1
        r = self.client.post("/api/network/import", json=TWO_SOURCE_NETWORK)  # 快照 2
        self.assertEqual(r.json()["snapshot_id"], 2)
        body = self.client.get("/api/snapshots/2/diff").json()
        added_nodes = {n["id"] for n in body["nodes"]["added"]}
        removed_nodes = {n["id"] for n in body["nodes"]["removed"]}
        self.assertEqual(added_nodes, {"S1", "S2", "C"})  # U1/U2 保留
        self.assertEqual(removed_nodes, {"S", "A", "B", "D", "U3"})
        added_pipes = {p["id"] for p in body["pipes"]["added"]}
        removed_pipes = {p["id"] for p in body["pipes"]["removed"]}
        self.assertEqual(added_pipes, {"Q1", "Q2", "Q3", "Q4"})
        self.assertEqual(removed_pipes, {"P1", "P2", "P3", "P4", "P5", "P6"})

    def test_diff_adjacent_base_validation(self):
        self._import()  # 1
        self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )  # 2
        self.client.post(
            "/api/isolation/events/1/restore", json={"version": 2}
        )  # 3
        # 显式指定相邻基线 2 -> 正常
        r = self.client.get("/api/snapshots/3/diff?base_id=2")
        self.assertEqual(r.status_code, 200)
        # 跨越 2 指定 1 -> 422 SNAPSHOT_NOT_ADJACENT
        r = self.client.get("/api/snapshots/3/diff?base_id=1")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_NOT_ADJACENT")
        # 基线编号大于目标编号 -> 422 SNAPSHOT_DIFF_INVALID
        r = self.client.get("/api/snapshots/2/diff?base_id=3")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_DIFF_INVALID")
        r = self.client.get("/api/snapshots/2/diff?base_id=2")
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_DIFF_INVALID")
        # 基线不存在（编号小于目标但无此快照）-> 404
        r = self.client.get("/api/snapshots/3/diff?base_id=99")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_NOT_FOUND")


# 流量核算请求样例（基于 VALID_NETWORK：S-P1-A-P2-B-P4-D-P6-U3；A-P3-U1；B-P5-U2）
def _delivery_body(version=1, *, source_cap=100, pipe_caps=None, demands=None):
    return {
        "version": version,
        "sources": [{"source_id": "S", "capacity": source_cap}],
        "pipes": [
            {"pipe_id": pid, "capacity": cap}
            for pid, cap in (
                pipe_caps
                or [("P1", 100), ("P2", 100), ("P3", 100),
                    ("P4", 100), ("P5", 100), ("P6", 100)]
            )
        ],
        "demands": [
            {"sink_id": sid, "demand": d}
            for sid, d in (demands or [("U1", 1), ("U2", 1), ("U3", 1)])
        ],
    }


class DeliveryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _import(self, network=VALID_NETWORK):
        r = self.client.post("/api/network/import", json=network)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_feasible_when_capacity_abundant(self):
        self._import()
        r = self.client.post("/api/delivery/evaluate", json=_delivery_body())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            r.json(),
            {
                "version": 1,
                "version_match": True,
                "total_demand": 3,
                "max_delivered_total": 3,
                "feasible": True,
            },
        )

    def test_pipe_capacity_bottleneck(self):
        self._import()
        # P3 是 A-U1 的唯一管段，只能过 2
        body = _delivery_body(
            pipe_caps=[("P1", 100), ("P2", 100), ("P3", 2),
                       ("P4", 100), ("P5", 100), ("P6", 100)],
            demands=[("U1", 5), ("U2", 1), ("U3", 1)],
        )
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 200)
        body_out = r.json()
        self.assertEqual(body_out["total_demand"], 7)
        self.assertEqual(body_out["max_delivered_total"], 4)
        self.assertFalse(body_out["feasible"])

    def test_source_capacity_bottleneck(self):
        self._import()
        body = _delivery_body(source_cap=2)
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.json()["max_delivered_total"], 2)
        self.assertFalse(r.json()["feasible"])

    def test_stopped_source_delivers_nothing(self):
        self._import(TWO_SOURCE_NETWORK)
        # 停运 S1（版本 2）
        r = self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = {
            "version": 2,
            "sources": [{"source_id": "S1", "capacity": 100},
                        {"source_id": "S2", "capacity": 100}],
            "pipes": [{"pipe_id": pid, "capacity": 100}
                      for pid in ("Q1", "Q3", "Q2", "Q4")],
            "demands": [{"sink_id": "U1", "demand": 1},
                        {"sink_id": "U2", "demand": 1}],
        }
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["max_delivered_total"], 2)
        self.assertTrue(r.json()["feasible"])
        # 两座水源全部停运：交付 0
        self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S2", "running": False}], "version": 2},
        )
        body["version"] = 3
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.json()["max_delivered_total"], 0)
        self.assertFalse(r.json()["feasible"])

    def test_closed_valve_and_open_fault_block(self):
        self._import()
        # 隔离 P6 关 P4：D 片区断源，U3 不可交付
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = _delivery_body(version=2)
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        # U1、U2 可满足（各 1），U3 因 P6 待修复 + P4 关闭断供
        self.assertEqual(r.json()["max_delivered_total"], 2)
        self.assertFalse(r.json()["feasible"])

    def test_unlisted_sinks_not_consumed(self):
        self._import()
        body = _delivery_body(demands=[("U3", 1)])
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.json()["total_demand"], 1)
        self.assertEqual(r.json()["max_delivered_total"], 1)
        self.assertTrue(r.json()["feasible"])

    def test_stale_version_still_evaluated_against_current(self):
        self._import()
        body = _delivery_body(version=42)
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertFalse(out["version_match"])
        self.assertEqual(out["version"], 1)
        self.assertTrue(out["feasible"])

    def test_empty_network_coverage(self):
        # 空网（版本 0）：水源/管段为空列表，用水点不可能存在 -> 422
        body = {
            "version": 0,
            "sources": [],
            "pipes": [],
            "demands": [{"sink_id": "U1", "demand": 1}],
        }
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "DELIVERY_REQUEST_INVALID")
        self.assertEqual(
            r.json()["error"]["issues"][0]["code"], "DELIVERY_SINK_NOT_FOUND"
        )

    def test_missing_source_and_pipe_rejected(self):
        self._import()
        body = _delivery_body()
        body["sources"] = []
        body["pipes"] = [p for p in body["pipes"] if p["pipe_id"] != "P6"]
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 422)
        err = r.json()["error"]
        self.assertEqual(err["code"], "DELIVERY_REQUEST_INVALID")
        codes = {i["code"] for i in err["issues"]}
        self.assertEqual(codes, {"DELIVERY_SOURCE_MISSING", "DELIVERY_PIPE_MISSING"})

    def test_unknown_and_duplicate_ids_rejected(self):
        self._import()
        body = _delivery_body()
        body["sources"].append({"source_id": "S", "capacity": 1})
        body["sources"].append({"source_id": "SX", "capacity": 1})
        body["pipes"].append({"pipe_id": "P1", "capacity": 1})
        body["pipes"].append({"pipe_id": "PX", "capacity": 1})
        body["demands"].append({"sink_id": "U1", "demand": 1})
        body["demands"].append({"sink_id": "S", "demand": 1})
        body["demands"].append({"sink_id": "UX", "demand": 1})
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 422)
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertEqual(
            codes,
            {
                "DELIVERY_SOURCE_DUPLICATE",
                "DELIVERY_SOURCE_NOT_FOUND",
                "DELIVERY_PIPE_DUPLICATE",
                "DELIVERY_PIPE_NOT_FOUND",
                "DELIVERY_SINK_DUPLICATE",
                "DELIVERY_SINK_NOT_SINK",
                "DELIVERY_SINK_NOT_FOUND",
            },
        )

    def test_empty_demands_rejected(self):
        self._import()
        body = _delivery_body()
        body["demands"] = []
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(
            r.json()["error"]["issues"][0]["code"], "DELIVERY_DEMANDS_EMPTY"
        )

    def test_non_integer_values_rejected(self):
        self._import()

        def post_raw(raw):
            return self.client.post(
                "/api/delivery/evaluate",
                content=json.dumps(raw),
                headers={"Content-Type": "application/json"},
            )

        base = _delivery_body()
        # 布尔不是整数
        bad = json.loads(json.dumps(base))
        bad["sources"][0]["capacity"] = True
        self.assertEqual(post_raw(bad).status_code, 422)
        # 负数
        bad = json.loads(json.dumps(base))
        bad["demands"][0]["demand"] = -3
        self.assertEqual(post_raw(bad).status_code, 422)
        # 小数
        bad = json.loads(json.dumps(base))
        bad["pipes"][0]["capacity"] = 1.5
        self.assertEqual(post_raw(bad).status_code, 422)
        # 字符串数字不自动转型
        bad = json.loads(json.dumps(base))
        bad["version"] = "1"
        self.assertEqual(post_raw(bad).status_code, 422)
        # 额外字段
        bad = json.loads(json.dumps(base))
        bad["sources"][0]["extra"] = 1
        self.assertEqual(post_raw(bad).status_code, 422)

    def test_read_only_no_version_valve_or_snapshot_change(self):
        info = self._import()
        snapshot_after_import = info["snapshot_id"]
        # 连续多次核算（含被拒绝的非法请求）不得推进版本或产生快照
        self.client.post("/api/delivery/evaluate", json=_delivery_body())
        self.client.post("/api/delivery/evaluate", json=_delivery_body(version=99))
        bad = _delivery_body()
        bad["sources"] = []
        self.client.post("/api/delivery/evaluate", json=bad)

        net = self.client.get("/api/network").json()
        self.assertEqual(net["version"], 1)
        snap = self.client.get("/api/snapshots").json()
        self.assertEqual([s["id"] for s in snap], [snapshot_after_import])
        # 阀位不变：有阀管段均未关闭（无阀管段 P6 序列化为 null）
        self.assertTrue(
            all(p["valve_closed"] is False for p in net["pipes"] if p["has_valve"])
        )
        # 无新增事件
        self.assertEqual(self.client.get("/api/isolation/events").json(), [])


# 保障等级分档交付请求样例（基于 VALID_NETWORK）
def _allocate_body(
    version=1,
    *,
    source_cap=100,
    pipe_caps=None,
    demands=None,
):
    return {
        "version": version,
        "sources": [{"source_id": "S", "capacity": source_cap}],
        "pipes": [
            {"pipe_id": pid, "capacity": cap}
            for pid, cap in (
                pipe_caps
                or [("P1", 100), ("P2", 100), ("P3", 100),
                    ("P4", 100), ("P5", 100), ("P6", 100)]
            )
        ],
        "demands": [
            {"sink_id": sid, "demand": d, "level": lvl}
            for sid, d, lvl in (
                demands
                or [("U1", 1, 1), ("U2", 1, 2), ("U3", 1, 2)]
            )
        ],
    }


class DeliveryAllocateApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _import(self, network=VALID_NETWORK):
        r = self.client.post("/api/network/import", json=network)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_priority_level_lexicographic_allocation(self):
        self._import()
        # P3 是 A->U1 的唯一管段（容量 6）：U1 等级 1 先拿 6，
        # 等级 2 的 U2/U3 共享其余能力并按编号逐点最大化
        body = _allocate_body(
            pipe_caps=[("P1", 100), ("P2", 100), ("P3", 6),
                       ("P4", 100), ("P5", 100), ("P6", 100)],
            demands=[("U1", 10, 1), ("U2", 10, 2), ("U3", 10, 2)],
        )
        r = self.client.post("/api/delivery/allocate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["version"], 1)
        self.assertTrue(out["version_match"])
        self.assertEqual(out["total_demand"], 30)
        # 等级 1 总量 6；等级 2 总量 20（U2/U3 各 10）；总交付 26
        self.assertEqual(
            [(l["level"], l["delivered"], l["demand"]) for l in out["levels"]],
            [(1, 6, 10), (2, 20, 20)],
        )
        self.assertEqual(
            [(d["sink_id"], d["level"], d["delivered"], d["shortfall"])
             for d in out["deliveries"]],
            [
                ("U1", 1, 6, 4),
                ("U2", 2, 10, 0),
                ("U3", 2, 10, 0),
            ],
        )
        self.assertFalse(out["all_satisfied"])
        for item in out["deliveries"]:
            self.assertEqual(item["satisfied"], item["delivered"] == item["demand"])

    def test_abundant_capacity_all_satisfied(self):
        self._import()
        r = self.client.post("/api/delivery/allocate", json=_allocate_body())
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertTrue(out["all_satisfied"])
        self.assertEqual(out["total_delivered"], 3)
        self.assertTrue(all(d["delivered"] == 1 for d in out["deliveries"]))

    def test_level_1_starves_before_level_2(self):
        self._import()
        # 水源只能供 1：给等级 1 的 U1，不给等级 2 的 U2/U3
        body = _allocate_body(
            source_cap=1,
            demands=[("U1", 1, 1), ("U2", 1, 2), ("U3", 1, 2)],
        )
        out = self.client.post("/api/delivery/allocate", json=body).json()
        self.assertEqual(
            [(d["sink_id"], d["delivered"]) for d in out["deliveries"]],
            [("U1", 1), ("U2", 0), ("U3", 0)],
        )
        self.assertEqual(
            [(l["level"], l["delivered"]) for l in out["levels"]],
            [(1, 1), (2, 0)],
        )

    def test_blocked_valve_and_open_fault_respected(self):
        self._import()
        # 隔离 P6 关 P4：U3 断供（故障管段 P6 + 已关阀门 P4）
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = _allocate_body(
            version=2,
            demands=[("U1", 1, 1), ("U2", 1, 1), ("U3", 1, 2)],
        )
        out = self.client.post("/api/delivery/allocate", json=body).json()
        self.assertEqual(
            [(d["sink_id"], d["delivered"]) for d in out["deliveries"]],
            [("U1", 1), ("U2", 1), ("U3", 0)],
        )
        # 同等级 U1/U2 拿满；等级 2 的 U3 因阻断为 0
        self.assertEqual(
            [(l["level"], l["delivered"], l["shortfall"]) for l in out["levels"]],
            [(1, 2, 0), (2, 0, 1)],
        )

    def test_stale_version_marked_mismatch_but_computed(self):
        self._import()
        body = _allocate_body(version=99)
        out = self.client.post("/api/delivery/allocate", json=body).json()
        self.assertEqual(out["version"], 1)
        self.assertFalse(out["version_match"])
        self.assertTrue(out["all_satisfied"])

    def test_zero_demand_point_listed_allowed(self):
        self._import()
        body = _allocate_body(demands=[("U1", 0, 1)])
        out = self.client.post("/api/delivery/allocate", json=body).json()
        self.assertEqual(out["total_demand"], 0)
        self.assertEqual(out["total_delivered"], 0)
        self.assertTrue(out["all_satisfied"])
        self.assertEqual(out["deliveries"][0]["delivered"], 0)

    def test_unlisted_sinks_not_consumed(self):
        self._import()
        body = _allocate_body(demands=[("U3", 5, 1)])
        out = self.client.post("/api/delivery/allocate", json=body).json()
        self.assertEqual(out["total_demand"], 5)
        self.assertEqual(out["total_delivered"], 5)
        self.assertTrue(out["all_satisfied"])
        self.assertEqual([d["sink_id"] for d in out["deliveries"]], ["U3"])

    def test_invalid_level_values_rejected(self):
        self._import()

        def post_raw(raw):
            return self.client.post(
                "/api/delivery/allocate",
                content=json.dumps(raw),
                headers={"Content-Type": "application/json"},
            )

        for bad_level in (0, -1, 1.5, True, "1"):
            body = _allocate_body()
            bad = json.loads(json.dumps(body))
            bad["demands"][0]["level"] = bad_level
            r = post_raw(bad)
            self.assertEqual(r.status_code, 422, (bad_level, r.text))

    def test_coverage_duplicate_type_errors_rejected(self):
        self._import()
        body = _allocate_body()
        # 水源未覆盖 + 管段重复 + 用水点不是 sink / 不存在 / 重复
        body["sources"] = []
        body["pipes"].append({"pipe_id": "P1", "capacity": 1})
        body["demands"].extend(
            [
                {"sink_id": "U1", "demand": 1, "level": 1},
                {"sink_id": "S", "demand": 1, "level": 1},
                {"sink_id": "UX", "demand": 1, "level": 1},
            ]
        )
        r = self.client.post("/api/delivery/allocate", json=body)
        self.assertEqual(r.status_code, 422)
        err = r.json()["error"]
        self.assertEqual(err["code"], "DELIVERY_REQUEST_INVALID")
        codes = {i["code"] for i in err["issues"]}
        self.assertIn("DELIVERY_SOURCE_MISSING", codes)
        self.assertIn("DELIVERY_PIPE_DUPLICATE", codes)
        self.assertIn("DELIVERY_SINK_DUPLICATE", codes)
        self.assertIn("DELIVERY_SINK_NOT_SINK", codes)
        self.assertIn("DELIVERY_SINK_NOT_FOUND", codes)

    def test_empty_demands_rejected(self):
        self._import()
        body = _allocate_body()
        body["demands"] = []
        r = self.client.post("/api/delivery/allocate", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(
            r.json()["error"]["issues"][0]["code"], "DELIVERY_DEMANDS_EMPTY"
        )

    def test_extra_field_and_non_integer_values_rejected(self):
        self._import()

        def post_raw(raw):
            return self.client.post(
                "/api/delivery/allocate",
                content=json.dumps(raw),
                headers={"Content-Type": "application/json"},
            )

        base = _allocate_body()
        bad = json.loads(json.dumps(base))
        bad["demands"][0]["extra"] = 1
        self.assertEqual(post_raw(bad).status_code, 422)
        bad = json.loads(json.dumps(base))
        bad["sources"][0]["capacity"] = -3
        self.assertEqual(post_raw(bad).status_code, 422)
        bad = json.loads(json.dumps(base))
        bad["pipes"][0]["capacity"] = 2.5
        self.assertEqual(post_raw(bad).status_code, 422)

    def test_read_only_no_state_change(self):
        info = self._import()
        self.client.post("/api/delivery/allocate", json=_allocate_body())
        self.client.post("/api/delivery/allocate", json=_allocate_body(version=99))
        bad = _allocate_body()
        bad["sources"] = []
        self.client.post("/api/delivery/allocate", json=bad)
        net = self.client.get("/api/network").json()
        self.assertEqual(net["version"], 1)
        snaps = self.client.get("/api/snapshots").json()
        self.assertEqual([s["id"] for s in snaps], [info["snapshot_id"]])
        self.assertEqual(self.client.get("/api/isolation/events").json(), [])

    def test_existing_evaluate_endpoint_unchanged(self):
        # 现有 /api/delivery/evaluate 的输入输出保持不变（无 level 字段）
        self._import()
        body = {
            "version": 1,
            "sources": [{"source_id": "S", "capacity": 100}],
            "pipes": [
                {"pipe_id": pid, "capacity": 100}
                for pid in ("P1", "P2", "P3", "P4", "P5", "P6")
            ],
            "demands": [
                {"sink_id": "U1", "demand": 1},
                {"sink_id": "U2", "demand": 1},
                {"sink_id": "U3", "demand": 1},
            ],
        }
        r = self.client.post("/api/delivery/evaluate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            set(r.json().keys()),
            {"version", "version_match", "total_demand",
             "max_delivered_total", "feasible"},
        )


def _reconcile_body(
    version=1,
    *,
    injections=None,
    withdrawals=None,
    pipe_entries=None,
):
    # VALID_NETWORK：S；U1/U2/U3；P1..P6
    return {
        "version": version,
        "sources": [
            {"source_id": sid, "injection": inj}
            for sid, inj in (injections if injections is not None else [("S", 0)])
        ],
        "sinks": [
            {"sink_id": sid, "withdrawal": wd}
            for sid, wd in (
                withdrawals
                if withdrawals is not None
                else [("U1", 0), ("U2", 0), ("U3", 0)]
            )
        ],
        "pipes": [
            {"pipe_id": pid, "capacity": cap, "reading": reading}
            for pid, cap, reading in (
                pipe_entries
                if pipe_entries is not None
                else [(pid, 100, None) for pid in ("P1", "P2", "P3", "P4", "P5", "P6")]
            )
        ],
    }


class MeterReconcileApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _import(self, network=VALID_NETWORK):
        r = self.client.post("/api/network/import", json=network)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_all_reported_consistent(self):
        self._import()
        body = _reconcile_body(
            injections=[("S", 30)],
            withdrawals=[("U1", 10), ("U2", 10), ("U3", 10)],
            pipe_entries=[
                ("P1", 100, 30), ("P2", 100, 20), ("P3", 100, 10),
                ("P4", 100, 20), ("P5", 100, 10), ("P6", 100, 10),
            ],
        )
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertTrue(out["consistent"])
        self.assertEqual(out["readings"], [])
        self.assertEqual(out["reasons"], [])
        self.assertTrue(out["version_match"])
        self.assertEqual(out["version"], 1)

    def test_missing_ranges_all_determined_by_balance(self):
        self._import()
        # 全部缺报：S 注入 30，各点取用 10，树形管网各管段被唯一确定
        body = _reconcile_body(
            injections=[("S", 30)],
            withdrawals=[("U1", 10), ("U2", 10), ("U3", 10)],
        )
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertTrue(out["consistent"])
        got = {x["pipe_id"]: x for x in out["readings"]}
        # 方向回显与 VALID_NETWORK 一致
        self.assertEqual(got["P1"]["from"], "S")
        self.assertEqual(got["P1"]["to"], "A")
        self.assertEqual(
            {pid: (v["min_flow"], v["max_flow"]) for pid, v in got.items()},
            {"P1": (30, 30), "P2": (20, 20), "P3": (10, 10),
             "P4": (20, 20), "P5": (10, 10), "P6": (10, 10)},
        )
        self.assertTrue(all(v["determinable"] for v in got.values()))

    def test_indeterminate_range(self):
        # 并联双管：S ->(Q1/Q2)-> U，总流量 2，缺报两管按容量自由分担
        network = {
            "nodes": [
                {"id": "S", "type": "source"},
                {"id": "U", "type": "sink"},
            ],
            "pipes": [
                {"id": "Q1", "from": "S", "to": "U", "has_valve": True},
                {"id": "Q2", "from": "S", "to": "U", "has_valve": True},
            ],
        }
        self._import(network)
        body = {
            "version": 1,
            "sources": [{"source_id": "S", "injection": 2}],
            "sinks": [{"sink_id": "U", "withdrawal": 2}],
            "pipes": [
                {"pipe_id": "Q1", "capacity": 10, "reading": None},
                {"pipe_id": "Q2", "capacity": 10, "reading": None},
            ],
        }
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertTrue(out["consistent"])
        got = {x["pipe_id"]: x for x in out["readings"]}
        # f1+f2=2，f1∈[-8,10]（f2 随之反向分担）
        for x in got.values():
            self.assertEqual((x["min_flow"], x["max_flow"]), (-8, 10))
            self.assertFalse(x["determinable"])

        # 补报 Q1=3 后，Q2 缺报被唯一确定为 -1（即实际 U->S 方向 1，与 Q1 抵消 1）
        body["pipes"][0]["reading"] = 3
        r = self.client.post("/api/meters/reconcile", json=body)
        out = r.json()
        self.assertTrue(out["consistent"])
        only = out["readings"]
        self.assertEqual(len(only), 1)
        self.assertEqual(only[0]["pipe_id"], "Q2")
        self.assertEqual((only[0]["min_flow"], only[0]["max_flow"]), (-1, -1))
        self.assertTrue(only[0]["determinable"])

    def test_inconsistent_returns_no_supplement(self):
        self._import()
        # P1 报 0 但 S 注入 30：平衡不可满足
        body = _reconcile_body(
            injections=[("S", 30)],
            withdrawals=[("U1", 10), ("U2", 10), ("U3", 10)],
            pipe_entries=[
                ("P1", 100, 0), ("P2", 100, None), ("P3", 100, None),
                ("P4", 100, None), ("P5", 100, None), ("P6", 100, None),
            ],
        )
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertFalse(out["consistent"])
        self.assertEqual(out["readings"], [])
        self.assertEqual(out["reasons"][0]["code"], "READINGS_INCONSISTENT")

    def test_stopped_source_and_blocked_pipe_hard_constraints(self):
        self._import(TWO_SOURCE_NETWORK)
        # 停运 S1 却申报注入 5
        self.client.post(
            "/api/sources/switch",
            json={"targets": [{"source_id": "S1", "running": False}], "version": 1},
        )
        body = {
            "version": 2,
            "sources": [{"source_id": "S1", "injection": 5},
                        {"source_id": "S2", "injection": 0}],
            "sinks": [{"sink_id": "U1", "withdrawal": 0},
                      {"sink_id": "U2", "withdrawal": 5}],
            "pipes": [
                {"pipe_id": "Q1", "capacity": 10, "reading": 5},
                {"pipe_id": "Q3", "capacity": 10, "reading": 0},
                {"pipe_id": "Q2", "capacity": 10, "reading": 0},
                {"pipe_id": "Q4", "capacity": 10, "reading": 5},
            ],
        }
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertFalse(out["consistent"])
        self.assertEqual(out["reasons"][0]["code"], "STOPPED_SOURCE_INJECTION")
        self.assertEqual(out["reasons"][0]["node_id"], "S1")

        # 关阀阻断：隔离 P6 关 P4 后，P4 读数非 0 -> BLOCKED_PIPE_FLOW
        self._import()
        self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        body = _reconcile_body(
            version=2,
            injections=[("S", 0)],
            withdrawals=[("U1", 0), ("U2", 0), ("U3", 0)],
            pipe_entries=[
                ("P1", 100, 0), ("P2", 100, 0), ("P3", 100, 0),
                ("P4", 100, 3), ("P5", 100, 0), ("P6", 100, 0),
            ],
        )
        r = self.client.post("/api/meters/reconcile", json=body)
        out = r.json()
        self.assertFalse(out["consistent"])
        self.assertEqual(out["reasons"][0]["code"], "BLOCKED_PIPE_FLOW")
        self.assertEqual(out["reasons"][0]["pipe_id"], "P4")
        # P6 待修复故障管段同样阻断（读数 0 即一致，缺报也可确定为 0）
        body["pipes"][3]["reading"] = 0
        body["pipes"][5]["reading"] = None
        r = self.client.post("/api/meters/reconcile", json=body)
        out = r.json()
        self.assertTrue(out["consistent"], out)
        p6 = [x for x in out["readings"] if x["pipe_id"] == "P6"][0]
        self.assertEqual((p6["min_flow"], p6["max_flow"]), (0, 0))

    def test_reading_over_capacity(self):
        self._import()
        body = _reconcile_body(
            injections=[("S", 0)],
            withdrawals=[("U1", 0), ("U2", 0), ("U3", 0)],
            pipe_entries=[
                ("P1", 5, 6), ("P2", 100, 0), ("P3", 100, 0),
                ("P4", 100, 0), ("P5", 100, 0), ("P6", 100, 0),
            ],
        )
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertFalse(r.json()["consistent"])
        self.assertEqual(r.json()["reasons"][0]["code"], "READING_OVER_CAPACITY")

    def test_stale_version_flag_but_still_evaluated(self):
        self._import()
        body = _reconcile_body(version=42)
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertFalse(out["version_match"])
        self.assertEqual(out["version"], 1)
        self.assertTrue(out["consistent"])

    def test_coverage_duplicate_unknown_rejected_422(self):
        self._import()
        bad = {
            "version": 1,
            # 缺 S + 重复 + 不存在
            "sources": [{"source_id": "SX", "injection": 1},
                        {"source_id": "SX", "injection": 1}],
            # 缺 U3 + 重复 U1 + 连接点 A 不可申报取用
            "sinks": [{"sink_id": "U1", "withdrawal": 1},
                      {"sink_id": "U1", "withdrawal": 1},
                      {"sink_id": "U2", "withdrawal": 1},
                      {"sink_id": "A", "withdrawal": 1}],
            # 缺 P6 + 重复 P1 + 不存在 PX
            "pipes": [
                {"pipe_id": "P1", "capacity": 1, "reading": None},
                {"pipe_id": "P1", "capacity": 1, "reading": None},
                {"pipe_id": "PX", "capacity": 1, "reading": None},
                {"pipe_id": "P2", "capacity": 1, "reading": None},
                {"pipe_id": "P3", "capacity": 1, "reading": None},
                {"pipe_id": "P4", "capacity": 1, "reading": None},
                {"pipe_id": "P5", "capacity": 1, "reading": None},
            ],
        }
        r = self.client.post("/api/meters/reconcile", json=bad)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "METER_RECONCILE_INVALID")
        codes = {i["code"] for i in r.json()["error"]["issues"]}
        self.assertIn("RECONCILE_SOURCE_MISSING", codes)
        self.assertIn("RECONCILE_SINK_MISSING", codes)
        self.assertIn("RECONCILE_PIPE_MISSING", codes)
        self.assertIn("RECONCILE_PIPE_NOT_FOUND", codes)
        self.assertIn("RECONCILE_SINK_NOT_SINK", codes)

    def test_non_integer_or_negative_rejected_422(self):
        self._import()
        # 布尔注入、负容量、浮点读数
        bad = _reconcile_body()
        bad["sources"] = [{"source_id": "S", "injection": True}]
        r = self.client.post("/api/meters/reconcile", json=bad)
        self.assertEqual(r.status_code, 422)
        bad = _reconcile_body()
        bad["pipes"][0]["capacity"] = -1
        r = self.client.post("/api/meters/reconcile", json=bad)
        self.assertEqual(r.status_code, 422)
        bad = _reconcile_body()
        bad["pipes"][0]["reading"] = 1.5
        r = self.client.post("/api/meters/reconcile", json=bad)
        self.assertEqual(r.status_code, 422)
        # 负取用量
        bad = _reconcile_body()
        bad["sinks"][0]["withdrawal"] = -1
        r = self.client.post("/api/meters/reconcile", json=bad)
        self.assertEqual(r.status_code, 422)

    def test_empty_network_coverage(self):
        body = {
            "version": 0,
            "sources": [],
            "sinks": [],
            "pipes": [],
        }
        r = self.client.post("/api/meters/reconcile", json=body)
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertTrue(out["consistent"])
        self.assertEqual(out["readings"], [])

    def test_readonly_does_not_change_state(self):
        self._import()
        r = self.client.post("/api/meters/reconcile", json=_reconcile_body())
        self.assertEqual(r.status_code, 200)
        net = self.client.get("/api/network").json()
        self.assertEqual(net["version"], 1)
        self.assertEqual(self.client.get("/api/isolation/events").json(), [])


class DrillApiTests(unittest.TestCase):
    """调度演练（POST /api/drill）路由层测试：全程只读，逐步套用提交校验。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _import(self, network=RESTORE_NETWORK):
        r = self.client.post("/api/network/import", json=network)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _drill(self, steps, version=1):
        return self.client.post("/api/drill", json={"version": version, "steps": steps})

    def _assert_read_only(self, version, event_count, snapshot_count):
        net = self.client.get("/api/network").json()
        self.assertEqual(net["version"], version)
        self.assertTrue(
            all(not p["valve_closed"] for p in net["pipes"] if p["has_valve"])
        )
        self.assertEqual(len(self.client.get("/api/isolation/events").json()), event_count)
        self.assertEqual(len(self.client.get("/api/snapshots").json()), snapshot_count)

    def test_happy_path_full_sequence_and_final_state(self):
        self._import()
        steps = [
            {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]},
            {"type": "restore", "temp_event_ref": "T1"},
            {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": ["P4", "P8"]},
            {"type": "source_switch", "targets": [{"source_id": "S", "running": False}]},
            {"type": "source_switch", "targets": [{"source_id": "S", "running": True}]},
        ]
        r = self._drill(steps)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["version"], 1)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["failed_step_index"])
        self.assertEqual(len(out["steps"]), 5)

        s1, s2, s3, s4, s5 = out["steps"]
        # 第 1 步：隔离 P6 关 P4 -> 临时事件 T1，U3/U4 新增失供
        self.assertEqual((s1["index"], s1["type"], s1["ok"]), (1, "isolation", True))
        self.assertEqual(s1["temp_event_ref"], "T1")
        self.assertEqual(s1["affected_sink_ids"], ["U3", "U4"])
        self.assertEqual(s1["restored_sink_ids"], [])
        # 第 2 步：复供临时事件 T1 -> U3/U4 恢复
        self.assertEqual((s2["type"], s2["ok"]), ("restore", True))
        self.assertEqual(s2["temp_event_ref"], "T1")
        self.assertEqual(s2["reopened_pipe_ids"], ["P4"])
        self.assertEqual(s2["restored_sink_ids"], ["U3", "U4"])
        # 第 3 步：隔离 P7 关 P4/P8 -> 临时事件 T3
        self.assertEqual(s3["temp_event_ref"], "T3")
        self.assertEqual(s3["affected_sink_ids"], ["U3", "U4"])
        # 第 4/5 步：停运再恢复水源 S（相对上一步计算失供/恢复）
        self.assertEqual(s4["affected_sink_ids"], ["U1", "U2"])
        self.assertTrue(s4["changed"])
        self.assertEqual(s5["restored_sink_ids"], ["U1", "U2"])

        final = out["final"]
        self.assertEqual(final["sources"], [{"source_id": "S", "running": True}])
        valves = {v["pipe_id"]: v["valve_closed"] for v in final["valves"]}
        self.assertEqual(
            valves,
            {"P1": False, "P2": False, "P3": False, "P4": True, "P5": False, "P8": True},
        )
        self.assertEqual(
            final["open_faults"],
            [{"event_id": None, "temp_event_ref": "T3", "fault_pipe_id": "P7"}],
        )
        self.assertEqual(final["supplied_sink_ids"], ["U1", "U2"])
        # 全程只读：版本/阀位/事件/快照均未变
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)

    def test_stale_version_rejected_409(self):
        self._import()
        r = self._drill(
            [{"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]}],
            version=99,
        )
        self.assertEqual(r.status_code, 409)
        err = r.json()["error"]
        self.assertEqual(err["code"], "VERSION_CONFLICT")
        self.assertEqual(err["current_version"], 1)
        self.assertEqual(err["expected_version"], 99)
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)

    def test_isolation_failure_marks_first_step_and_stops(self):
        self._import()
        steps = [
            # 空关阀：D 端仍经 P4 进水 -> 不可隔离
            {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": []},
            {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": []},
        ]
        r = self._drill(steps)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_step_index"], 1)
        self.assertEqual(len(out["steps"]), 1)  # 后续步骤不再演练
        self.assertEqual(out["steps"][0]["error"]["code"], "NOT_ISOLATABLE")
        self.assertIsNone(out["final"])
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)

    def test_isolation_plan_invalid_commit_semantics(self):
        self._import()
        # 关闭无阀管段 P6（以 P7 为故障管段）-> PLAN_INVALID / CLOSE_PIPE_NO_VALVE
        r = self._drill(
            [{"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": ["P6"]}]
        )
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["steps"][0]["error"]["code"], "PLAN_INVALID")
        codes = {i["code"] for i in out["steps"][0]["error"]["issues"]}
        self.assertIn("CLOSE_PIPE_NO_VALVE", codes)

        # 提交口径：上一步已关的阀门不得重复关闭
        r = self._drill(
            [
                {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]},
                {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": ["P4"]},
            ]
        )
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_step_index"], 2)
        self.assertEqual(out["steps"][1]["error"]["code"], "PLAN_INVALID")
        codes = {i["code"] for i in out["steps"][1]["error"]["issues"]}
        self.assertIn("CLOSE_PIPE_DUPLICATE", codes)

    def test_restore_rewater_risk_stops_drill(self):
        self._import()
        steps = [
            {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]},
            {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": []},
            # 复供 T1 重开 P4 -> 待修复故障 P7 的 D 端重新进水
            {"type": "restore", "temp_event_ref": "T1"},
        ]
        r = self._drill(steps)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_step_index"], 3)
        self.assertEqual(len(out["steps"]), 3)
        err = out["steps"][2]["error"]
        self.assertEqual(err["code"], "RESTORE_UNSAFE")
        block = err["block_reasons"][0]
        self.assertEqual(block["code"], "FAULT_REWATER_RISK")
        self.assertEqual(block["pipe_id"], "P7")
        self.assertEqual(block["reachable_endpoints"], ["D"])
        self.assertIsNone(out["final"])
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)

    def test_restore_real_event_and_reference_errors(self):
        self._import()
        # 先真实提交一条隔离事件（P6 故障，关 P4）-> 事件 1，版本 2
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["event_id"], 1)

        # 演练复供现存待修复事件 1 -> 成功，U3/U4 恢复
        r = self._drill([{"type": "restore", "event_id": 1}], version=2)
        out = r.json()
        self.assertTrue(out["ok"])
        step = out["steps"][0]
        self.assertEqual(step["event_id"], 1)
        self.assertEqual(step["reopened_pipe_ids"], ["P4"])
        self.assertEqual(step["restored_sink_ids"], ["U3", "U4"])
        self.assertEqual(out["final"]["open_faults"], [])
        self.assertEqual(out["final"]["supplied_sink_ids"], ["U1", "U2", "U3", "U4"])

        # 引用不存在的事件编号 / 未产生的临时引用 -> EVENT_NOT_FOUND
        for step in (
            {"type": "restore", "event_id": 999},
            {"type": "restore", "temp_event_ref": "T9"},
        ):
            r = self._drill([step], version=2)
            out = r.json()
            self.assertFalse(out["ok"])
            self.assertEqual(out["failed_step_index"], 1)
            self.assertEqual(out["steps"][0]["error"]["code"], "EVENT_NOT_FOUND")

        # 同一演练中重复复供同一事件 -> EVENT_ALREADY_RESTORED
        r = self._drill(
            [
                {"type": "restore", "event_id": 1},
                {"type": "restore", "event_id": 1},
            ],
            version=2,
        )
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_step_index"], 2)
        self.assertEqual(out["steps"][1]["error"]["code"], "EVENT_ALREADY_RESTORED")

        # 真实复供事件 1 后，演练再复供 -> EVENT_ALREADY_RESTORED（已复供事件可精确区分）
        r = self.client.post("/api/isolation/events/1/restore", json={"version": 2})
        self.assertEqual(r.status_code, 200, r.text)
        r = self._drill([{"type": "restore", "event_id": 1}], version=3)
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["steps"][0]["error"]["code"], "EVENT_ALREADY_RESTORED")
        # 演练全程未改变真实状态（版本 3、事件 1 已复供、无新增快照）
        self.assertEqual(STORE.version, 3)
        self.assertEqual(len(STORE.snapshots), 3)

    def test_source_switch_steps_and_unsafe_stop(self):
        self._import(TWO_SOURCE_NETWORK)
        # 停运 S1（S2 顶供无失供）-> 恢复 S1
        r = self._drill(
            [
                {"type": "source_switch", "targets": [{"source_id": "S1", "running": False}]},
                {"type": "source_switch", "targets": [{"source_id": "S1", "running": True}]},
            ]
        )
        out = r.json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["steps"][0]["affected_sink_ids"], [])
        self.assertTrue(out["steps"][0]["changed"])
        self.assertEqual(out["steps"][1]["restored_sink_ids"], [])
        self.assertEqual(
            out["final"]["sources"],
            [{"source_id": "S1", "running": True}, {"source_id": "S2", "running": True}],
        )

        # 停运 S2 -> 隔离 Q2（关 Q1）-> 恢复 S2 使故障 Q2 的 C 端重新进水 -> 不安全
        r = self._drill(
            [
                {"type": "source_switch", "targets": [{"source_id": "S2", "running": False}]},
                {"type": "isolation", "fault_pipe_id": "Q2", "close_pipe_ids": ["Q1"]},
                {"type": "source_switch", "targets": [{"source_id": "S2", "running": True}]},
            ]
        )
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_step_index"], 3)
        err = out["steps"][2]["error"]
        self.assertEqual(err["code"], "SOURCE_SWITCH_UNSAFE")
        self.assertEqual(err["block_reasons"][0]["pipe_id"], "Q2")
        self.assertEqual(err["block_reasons"][0]["reachable_endpoints"], ["C"])
        self.assertIsNone(out["final"])
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)

    def test_source_switch_invalid_step(self):
        self._import(TWO_SOURCE_NETWORK)
        r = self._drill(
            [
                {
                    "type": "source_switch",
                    "targets": [
                        {"source_id": "SX", "running": False},
                        {"source_id": "U1", "running": False},
                        {"source_id": "S1", "running": False},
                        {"source_id": "S1", "running": True},
                    ],
                }
            ]
        )
        out = r.json()
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_step_index"], 1)
        self.assertEqual(out["steps"][0]["error"]["code"], "SOURCE_SWITCH_INVALID")
        codes = {i["code"] for i in out["steps"][0]["error"]["issues"]}
        self.assertEqual(
            codes, {"SOURCE_NOT_FOUND", "SOURCE_NOT_SWITCHABLE", "SOURCE_TARGET_DUPLICATE"}
        )

    def test_empty_steps_returns_current_state(self):
        self._import()
        r = self._drill([])
        out = r.json()
        self.assertTrue(out["ok"])
        self.assertEqual(out["steps"], [])
        final = out["final"]
        self.assertEqual(final["sources"], [{"source_id": "S", "running": True}])
        self.assertEqual(final["open_faults"], [])
        self.assertEqual(final["supplied_sink_ids"], ["U1", "U2", "U3", "U4"])

    def test_request_body_validation_422(self):
        self._import()
        bad_payloads = [
            # 未知步骤类型
            {"version": 1, "steps": [{"type": "unknown"}]},
            # 复供步骤同时给出两种引用
            {"version": 1, "steps": [{"type": "restore", "event_id": 1, "temp_event_ref": "T1"}]},
            # 复供步骤两种引用都缺失
            {"version": 1, "steps": [{"type": "restore"}]},
            # 隔离步骤缺故障管段
            {"version": 1, "steps": [{"type": "isolation", "close_pipe_ids": ["P4"]}]},
            # 水源切换步骤目标为空
            {"version": 1, "steps": [{"type": "source_switch", "targets": []}]},
            # 多余字段
            {"version": 1, "steps": [{"type": "isolation", "fault_pipe_id": "P6", "foo": 1}]},
            # 版本为布尔
            {"version": True, "steps": []},
        ]
        for payload in bad_payloads:
            r = self.client.post("/api/drill", json=payload)
            self.assertEqual(r.status_code, 422, payload)
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)

    def test_drill_does_not_change_real_state(self):
        self._import()
        # 成功演练 + 失败演练交替执行，真实状态始终不变
        self._drill(
            [
                {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]},
                {"type": "restore", "temp_event_ref": "T1"},
            ]
        )
        self._drill([{"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": []}])
        self._drill(
            [
                {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": ["P4", "P8"]},
                {"type": "source_switch", "targets": [{"source_id": "S", "running": False}]},
            ]
        )
        self._assert_read_only(version=1, event_count=0, snapshot_count=1)
        # 既有单步接口契约不变：演练后按版本 1 仍可正常提交
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["version"], 2)


# 临时旁通管评估请求样例（基于 VALID_NETWORK；默认 P3 瓶颈 6，旁通 S->U1 容量 4）
def _bypass_body(
    version=1,
    *,
    bypass=None,
    source_cap=1000,
    pipe_caps=None,
    demands=None,
):
    return {
        "version": version,
        "bypass": bypass or {"from": "S", "to": "U1", "capacity": 4},
        "sources": [{"source_id": "S", "capacity": source_cap}],
        "pipes": [
            {"pipe_id": pid, "capacity": cap}
            for pid, cap in (
                pipe_caps
                or [("P1", 100), ("P2", 100), ("P3", 6),
                    ("P4", 100), ("P5", 100), ("P6", 100)]
            )
        ],
        "demands": [
            {"sink_id": sid, "demand": d, "level": lvl}
            for sid, d, lvl in (
                demands
                or [("U1", 10, 1), ("U2", 10, 2), ("U3", 10, 2)]
            )
        ],
    }


class DeliveryBypassApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _import(self, network=VALID_NETWORK):
        r = self.client.post("/api/network/import", json=network)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _isolate_p6(self):
        # 隔离 P6 关 P4：版本 -> 2，P6 待修复，U3 断供
        r = self.client.post(
            "/api/isolation/submit",
            json={"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
        )
        self.assertEqual(r.status_code, 200, r.text)

    def test_safe_bypass_returns_baseline_bypass_and_deltas(self):
        self._import()
        r = self.client.post("/api/delivery/bypass", json=_bypass_body())
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["version"], 1)
        self.assertTrue(out["version_match"])
        self.assertEqual(
            out["bypass"], {"from": "S", "to": "U1", "capacity": 4}
        )
        self.assertTrue(out["safe"])
        self.assertEqual(out["block_reasons"], [])
        # 基线：P3 瓶颈 6 -> U1=6；接入旁通后 U1=10，全部满足
        self.assertEqual(
            [(d["sink_id"], d["delivered"]) for d in out["baseline"]["deliveries"]],
            [("U1", 6), ("U2", 10), ("U3", 10)],
        )
        self.assertEqual(out["baseline"]["total_delivered"], 26)
        self.assertFalse(out["baseline"]["all_satisfied"])
        self.assertEqual(
            [(d["sink_id"], d["delivered"]) for d in out["with_bypass"]["deliveries"]],
            [("U1", 10), ("U2", 10), ("U3", 10)],
        )
        self.assertEqual(out["with_bypass"]["total_delivered"], 30)
        self.assertTrue(out["with_bypass"]["all_satisfied"])
        # 差值：总量 +4，等级 1 +4，仅 U1 +4
        self.assertEqual(out["deltas"]["total_delivered_delta"], 4)
        self.assertEqual(
            [
                (l["level"], l["baseline_delivered"],
                 l["bypass_delivered"], l["delivered_delta"])
                for l in out["deltas"]["levels"]
            ],
            [(1, 6, 10, 4), (2, 20, 20, 0)],
        )
        self.assertEqual(
            [
                (d["sink_id"], d["baseline_delivered"],
                 d["bypass_delivered"], d["delivered_delta"])
                for d in out["deltas"]["deliveries"]
            ],
            [("U1", 6, 10, 4), ("U2", 10, 10, 0), ("U3", 10, 10, 0)],
        )

    def test_unsafe_bypass_returns_blocks_without_delivery(self):
        self._import()
        self._isolate_p6()
        # 旁通 A->D 使待修复故障 P6 的 D 端重新进水：判不可用
        body = _bypass_body(version=2, bypass={"from": "A", "to": "D", "capacity": 50})
        r = self.client.post("/api/delivery/bypass", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertFalse(out["safe"])
        self.assertEqual(
            [
                (b["code"], b["pipe_id"], b["reachable_endpoints"])
                for b in out["block_reasons"]
            ],
            [("FAULT_REWATER_RISK", "P6", ["D"])],
        )
        # 判为不可用：不输出旁通交付量与差值，基线仍给出（U3 断供）
        self.assertIsNone(out["with_bypass"])
        self.assertIsNone(out["deltas"])
        self.assertEqual(
            [(d["sink_id"], d["delivered"]) for d in out["baseline"]["deliveries"]],
            [("U1", 6), ("U2", 10), ("U3", 0)],
        )

    def test_safe_bypass_in_fault_state_away_from_fault(self):
        self._import()
        self._isolate_p6()
        # 旁通 S->U1 不触碰故障区：安全，U1 提升，U3 仍断供
        out = self.client.post(
            "/api/delivery/bypass", json=_bypass_body(version=2)
        ).json()
        self.assertTrue(out["safe"])
        self.assertEqual(
            [
                (d["sink_id"], d["baseline_delivered"], d["bypass_delivered"])
                for d in out["deltas"]["deliveries"]
            ],
            [("U1", 6, 10), ("U2", 10, 10), ("U3", 0, 0)],
        )

    def test_bypass_parallel_to_existing_pipe(self):
        self._import()
        # 旁通 S->A 与现有管段 P1（容量 5）并联：总进水 5 -> 10
        body = _bypass_body(
            bypass={"from": "S", "to": "A", "capacity": 5},
            pipe_caps=[("P1", 5), ("P2", 100), ("P3", 100),
                       ("P4", 100), ("P5", 100), ("P6", 100)],
            demands=[("U1", 10, 1)],
        )
        out = self.client.post("/api/delivery/bypass", json=body).json()
        self.assertTrue(out["safe"])
        self.assertEqual(out["baseline"]["total_delivered"], 5)
        self.assertEqual(out["with_bypass"]["total_delivered"], 10)

    def test_stale_version_marked_mismatch_but_computed(self):
        self._import()
        out = self.client.post(
            "/api/delivery/bypass", json=_bypass_body(version=99)
        ).json()
        self.assertEqual(out["version"], 1)
        self.assertFalse(out["version_match"])
        self.assertTrue(out["safe"])
        self.assertEqual(out["with_bypass"]["total_delivered"], 30)

    def test_endpoint_not_found_and_same_rejected(self):
        self._import()
        body = _bypass_body(bypass={"from": "XX", "to": "U1", "capacity": 4})
        r = self.client.post("/api/delivery/bypass", json=body)
        self.assertEqual(r.status_code, 422)
        err = r.json()["error"]
        self.assertEqual(err["code"], "BYPASS_REQUEST_INVALID")
        self.assertEqual(
            [i["code"] for i in err["issues"]], ["BYPASS_ENDPOINT_NOT_FOUND"]
        )
        # 两端为同一节点
        body = _bypass_body(bypass={"from": "A", "to": "A", "capacity": 4})
        r = self.client.post("/api/delivery/bypass", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(
            [i["code"] for i in r.json()["error"]["issues"]],
            ["BYPASS_ENDPOINT_SAME"],
        )
        # 两端相同且不存在：SAME + NOT_FOUND
        body = _bypass_body(bypass={"from": "XX", "to": "XX", "capacity": 4})
        r = self.client.post("/api/delivery/bypass", json=body)
        self.assertEqual(
            [i["code"] for i in r.json()["error"]["issues"]],
            ["BYPASS_ENDPOINT_SAME", "BYPASS_ENDPOINT_NOT_FOUND"],
        )

    def test_delivery_id_errors_rejected(self):
        self._import()
        body = _bypass_body()
        body["sources"] = []  # 水源未覆盖
        body["pipes"].append({"pipe_id": "P1", "capacity": 1})  # 管段重复
        body["demands"].append({"sink_id": "UX", "demand": 1, "level": 1})  # 不存在
        r = self.client.post("/api/delivery/bypass", json=body)
        self.assertEqual(r.status_code, 422)
        err = r.json()["error"]
        self.assertEqual(err["code"], "BYPASS_REQUEST_INVALID")
        codes = {i["code"] for i in err["issues"]}
        self.assertIn("DELIVERY_SOURCE_MISSING", codes)
        self.assertIn("DELIVERY_PIPE_DUPLICATE", codes)
        self.assertIn("DELIVERY_SINK_NOT_FOUND", codes)

    def test_empty_demands_rejected(self):
        self._import()
        body = _bypass_body()
        body["demands"] = []
        r = self.client.post("/api/delivery/bypass", json=body)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(
            r.json()["error"]["issues"][0]["code"], "DELIVERY_DEMANDS_EMPTY"
        )

    def test_non_integer_and_extra_fields_rejected(self):
        self._import()

        def post_raw(raw):
            return self.client.post(
                "/api/delivery/bypass",
                content=json.dumps(raw),
                headers={"Content-Type": "application/json"},
            )

        base = _bypass_body()
        # 旁通容量：布尔 / 小数 / 负数均非法
        for bad_cap in (True, 1.5, -1):
            bad = json.loads(json.dumps(base))
            bad["bypass"]["capacity"] = bad_cap
            self.assertEqual(post_raw(bad).status_code, 422, bad_cap)
        # 旁通对象额外字段
        bad = json.loads(json.dumps(base))
        bad["bypass"]["extra"] = 1
        self.assertEqual(post_raw(bad).status_code, 422)
        # 等级非正整数
        bad = json.loads(json.dumps(base))
        bad["demands"][0]["level"] = 0
        self.assertEqual(post_raw(bad).status_code, 422)
        # 缺少旁通对象
        bad = json.loads(json.dumps(base))
        del bad["bypass"]
        self.assertEqual(post_raw(bad).status_code, 422)

    def test_zero_capacity_bypass_still_unsafe_when_rewatering(self):
        self._import()
        self._isolate_p6()
        # 安全判定为拓扑可达：容量 0 的旁通接通故障端点仍判不可用
        body = _bypass_body(version=2, bypass={"from": "A", "to": "D", "capacity": 0})
        out = self.client.post("/api/delivery/bypass", json=body).json()
        self.assertFalse(out["safe"])
        self.assertIsNone(out["with_bypass"])

    def test_read_only_no_state_change(self):
        info = self._import()
        self._isolate_p6()
        # 安全 / 不安全 / 非法请求均不得改变任何状态
        self.client.post("/api/delivery/bypass", json=_bypass_body(version=2))
        self.client.post(
            "/api/delivery/bypass",
            json=_bypass_body(version=2, bypass={"from": "A", "to": "D", "capacity": 50}),
        )
        self.client.post(
            "/api/delivery/bypass",
            json=_bypass_body(version=2, bypass={"from": "XX", "to": "U1", "capacity": 4}),
        )
        net = self.client.get("/api/network").json()
        self.assertEqual(net["version"], 2)
        # 阀位不变：仅 P4 处于关闭
        self.assertEqual(
            {p["id"]: p["valve_closed"] for p in net["pipes"] if p["has_valve"]},
            {"P1": False, "P2": False, "P3": False, "P4": True, "P5": False},
        )
        # 虚拟旁通管段绝不写入管网
        self.assertNotIn("__BYPASS__", [p["id"] for p in net["pipes"]])
        events = self.client.get("/api/isolation/events").json()
        self.assertEqual(len(events), 1)
        snaps = self.client.get("/api/snapshots").json()
        self.assertEqual(
            [s["id"] for s in snaps],
            [info["snapshot_id"], info["snapshot_id"] + 1],
        )


class SnapshotChainAuditTests(unittest.TestCase):
    """调度审计：快照摘要链接链与只读区间核验。"""

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def setUp(self):
        STORE.version = 0
        STORE.nodes = {}
        STORE.pipes = {}
        STORE.events = []
        STORE.next_event_id = 1
        STORE.snapshots = []
        STORE.next_snapshot_id = 1
        STORE.chains = {}
        STORE.anchor = None
        STORE.fail_commit = False

    def _make_chain(self, count=3):
        """导入一次后再做 count-1 次隔离提交，制造 count 条同事务接链快照。"""
        self._import()
        for i in range(count - 1):
            # 每条提交在不同管段制造快照（故障管段编号需存在，复用 P6 已隔离后
            # 再提交会受校验限制，故这里只测链：直接调 fake 写入更简单）
            STORE.version += 1
            fake_insert_state_snapshot(
                "FAKE_TX",
                op_type="source_switch",
                network_version=STORE.version,
                state=fake_freeze_current(),
            )
        return [s["id"] for s in STORE.snapshots]

    def _import(self):
        return self.client.post("/api/network/import", json=VALID_NETWORK)

    def _verify(self, start, end, pre, end_digest):
        return self.client.post(
            "/api/audit/verify",
            json={
                "start_id": start,
                "end_id": end,
                "pre_start_digest": pre,
                "end_digest": end_digest,
            },
        )

    def test_chain_info_first_snapshot_is_live_origin_anchor(self):
        r = self._import()
        self.assertEqual(r.status_code, 200)
        sid = r.json()["snapshot_id"]
        info = self.client.get("/api/audit/chain").json()
        self.assertEqual(info["anchor_snapshot_id"], sid)
        self.assertEqual(info["anchor_kind"], audit.ANCHOR_LIVE_ORIGIN)
        self.assertEqual(info["snapshot_count"], 1)
        self.assertEqual(info["chained_count"], 1)
        self.assertEqual(info["genesis_digest"], audit.GENESIS_DIGEST)
        self.assertEqual(info["earliest_snapshot_id"], sid)
        self.assertEqual(info["latest_snapshot_id"], sid)

    def test_verify_full_interval_ok_with_genesis(self):
        ids = self._make_chain(3)
        end_digest = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[0], ids[-1], audit.GENESIS_DIGEST, end_digest)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["verdict"], "ok")
        self.assertTrue(body["ok"])
        self.assertEqual([d["snapshot_id"] for d in body["digests"]], ids)
        self.assertEqual(body["digests"][0]["prev_digest"], audit.GENESIS_DIGEST)
        self.assertTrue(body["start_boundary_ok"])
        self.assertTrue(body["end_boundary_ok"])

    def test_verify_subinterval_uses_prev_existing_snapshot_digest(self):
        ids = self._make_chain(3)
        pre = STORE.chains[ids[0]]["digest"]
        end_digest = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[1], ids[-1], pre, end_digest)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["verdict"], "ok")

    def test_verify_singleton_interval(self):
        ids = self._make_chain(1)
        d = STORE.chains[ids[0]]["digest"]
        r = self._verify(ids[0], ids[0], audit.GENESIS_DIGEST, d)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["verdict"], "ok")

    def test_verify_wrong_end_digest_only_reports_boundary(self):
        ids = self._make_chain(3)
        r = self._verify(ids[0], ids[-1], audit.GENESIS_DIGEST, "f" * 64)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["verdict"], "boundary_mismatch")
        self.assertFalse(body["ok"])
        self.assertTrue(body["start_boundary_ok"])
        self.assertFalse(body["end_boundary_ok"])
        self.assertEqual(body["failure"]["kind"], "boundary")
        # 链上真实终点摘要被回显，供审计员定位是哪一侧边界不符
        self.assertEqual(
            body["expected_end_digest"], STORE.chains[ids[-1]]["digest"]
        )

    def test_verify_wrong_pre_start_digest_only_reports_boundary(self):
        ids = self._make_chain(3)
        end_digest = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[0], ids[-1], "a" * 64, end_digest)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["verdict"], "boundary_mismatch")
        self.assertFalse(body["start_boundary_ok"])
        self.assertTrue(body["end_boundary_ok"])
        self.assertEqual(
            body["expected_pre_start_digest"], audit.GENESIS_DIGEST
        )

    def test_verify_content_corruption_is_located(self):
        ids = self._make_chain(3)
        # 绕过 API 直接改写中间快照的冻结状态（模拟事后篡改数据库）
        target = next(s for s in STORE.snapshots if s["id"] == ids[1])
        target["state"] = {**target["state"], "supplied_sink_ids": ["HACKED"]}
        end_digest = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[0], ids[-1], audit.GENESIS_DIGEST, end_digest)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["verdict"], "content_corrupted")
        self.assertEqual(body["failure"]["snapshot_id"], ids[1])
        # 只返回损坏点之前已核验通过的摘要
        self.assertEqual(
            [d["snapshot_id"] for d in body["digests"]], ids[:1]
        )

    def test_verify_chain_break_is_located(self):
        ids = self._make_chain(3)
        # 篡改链上第二条的 prev 指针（内容未变），断链应在引用它的第三条定位
        STORE.chains[ids[2]]["prev_digest"] = "9" * 64
        end_digest = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[0], ids[-1], audit.GENESIS_DIGEST, end_digest)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["verdict"], "chain_broken")
        self.assertEqual(body["failure"]["snapshot_id"], ids[2])
        self.assertEqual(body["failure"]["previous_snapshot_id"], ids[1])
        self.assertEqual(
            body["failure"]["expected_prev_digest"], STORE.chains[ids[1]]["digest"]
        )
        self.assertEqual(body["failure"]["actual_prev_digest"], "9" * 64)

    def test_verify_missing_chain_row_reports_chain_broken(self):
        ids = self._make_chain(3)
        del STORE.chains[ids[1]]  # 链行被外部删除
        r = self._verify(ids[0], ids[-1], audit.GENESIS_DIGEST, "0" * 64)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["verdict"], "chain_broken")
        self.assertEqual(body["failure"]["snapshot_id"], ids[1])

    def test_verify_rollback_id_gap_is_not_a_break(self):
        # 模拟回滚造成的编号空洞：快照 id 为 1 和 5，二者直接接链
        self._import()
        STORE.next_snapshot_id = 5
        STORE.version = 2
        fake_insert_state_snapshot(
            "FAKE_TX",
            op_type="source_switch",
            network_version=2,
            state=fake_freeze_current(),
        )
        ids = [s["id"] for s in STORE.snapshots]
        self.assertEqual(ids, [1, 5])
        end_digest = STORE.chains[5]["digest"]
        r = self._verify(1, 5, audit.GENESIS_DIGEST, end_digest)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["verdict"], "ok")
        # 空洞编号不存在：端点不存在明确报错
        r = self._verify(1, 4, audit.GENESIS_DIGEST, end_digest)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_NOT_FOUND")

    def test_verify_reimport_keeps_chain_across_version_reset(self):
        r = self._import()
        first = r.json()["snapshot_id"]
        r = self._import()  # 整网重导：版本重置为 1，但不断链
        second = r.json()["snapshot_id"]
        self.assertGreater(second, first)
        end_digest = STORE.chains[second]["digest"]
        r = self._verify(first, second, audit.GENESIS_DIGEST, end_digest)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["verdict"], "ok")

    def test_verify_nonexistent_endpoints_404(self):
        ids = self._make_chain(2)
        d = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[0], ids[-1] + 100, audit.GENESIS_DIGEST, d)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "SNAPSHOT_NOT_FOUND")
        r = self._verify(ids[0] + 100, ids[-1] + 100, audit.GENESIS_DIGEST, d)
        self.assertEqual(r.status_code, 404)

    def test_verify_reversed_range_422(self):
        ids = self._make_chain(2)
        d = STORE.chains[ids[-1]]["digest"]
        r = self._verify(ids[-1], ids[0], audit.GENESIS_DIGEST, d)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "AUDIT_RANGE_INVALID")

    def test_verify_bad_digest_format_422(self):
        ids = self._make_chain(1)
        r = self._verify(ids[0], ids[0], "XYZ", STORE.chains[ids[0]]["digest"])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["error"]["code"], "AUDIT_DIGEST_INVALID")

    def test_verify_is_read_only(self):
        ids = self._make_chain(2)
        before = (len(STORE.snapshots), len(STORE.chains), STORE.version)
        d = STORE.chains[ids[-1]]["digest"]
        self._verify(ids[0], ids[-1], audit.GENESIS_DIGEST, d)
        after = (len(STORE.snapshots), len(STORE.chains), STORE.version)
        self.assertEqual(before, after)

    def test_snapshot_insert_and_chain_share_transaction_rollback(self):
        # 提交事务失败：快照与链同时回滚，不留下半完成记录
        self._import()
        STORE.fail_commit = True
        with self.assertRaises(RuntimeError):
            with db.transaction() as conn:
                STORE.version += 1
                fake_insert_state_snapshot(
                    conn,
                    op_type="source_switch",
                    network_version=STORE.version,
                    state=fake_freeze_current(),
                )
        self.assertEqual(len(STORE.snapshots), len(STORE.chains))


if __name__ == "__main__":
    unittest.main()
