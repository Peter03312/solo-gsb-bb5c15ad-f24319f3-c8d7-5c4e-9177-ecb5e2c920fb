"""PostgreSQL 访问层：建表、当前快照读取、整网导入、隔离提交、复供、水源切换、状态回看快照。"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

import psycopg2
import psycopg2.extras
from psycopg2.errors import UndefinedTable

from .config import get_settings
from .planning import (
    Node,
    Pipe,
    PlanResult,
    OP_BASELINE,
    OP_ISOLATION_SUBMIT,
    OP_NETWORK_IMPORT,
    OP_RESTORE_CONFIRM,
    OP_SOURCE_SWITCH,
    freeze_state,
)

DDL = """
CREATE TABLE IF NOT EXISTS network_meta (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    version     INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT network_meta_singleton CHECK (id = 1)
);

INSERT INTO network_meta (id, version)
VALUES (1, 0)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS nodes (
    id      TEXT PRIMARY KEY,
    type    TEXT NOT NULL CHECK (type IN ('source', 'sink', 'junction')),
    name    TEXT,
    running BOOLEAN NOT NULL DEFAULT TRUE
);

-- 兼容旧库：建表后补齐水源运行状态列（旧库水源视为运行中）
ALTER TABLE nodes
    ADD COLUMN IF NOT EXISTS running BOOLEAN NOT NULL DEFAULT TRUE;

CREATE TABLE IF NOT EXISTS pipes (
    id           TEXT PRIMARY KEY,
    from_node    TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    to_node      TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    has_valve    BOOLEAN NOT NULL DEFAULT FALSE,
    valve_closed BOOLEAN NOT NULL DEFAULT FALSE,
    name         TEXT,
    CONSTRAINT pipes_no_self_loop CHECK (from_node <> to_node)
);

CREATE INDEX IF NOT EXISTS idx_pipes_from_node ON pipes (from_node);
CREATE INDEX IF NOT EXISTS idx_pipes_to_node ON pipes (to_node);

CREATE TABLE IF NOT EXISTS isolation_events (
    id                BIGSERIAL PRIMARY KEY,
    version_before    INTEGER NOT NULL,
    version_after     INTEGER NOT NULL,
    fault_pipe_id     TEXT NOT NULL,
    closed_pipe_ids   JSONB NOT NULL,
    affected_sink_ids JSONB NOT NULL,
    status            TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open', 'restored')),
    restored_at       TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 兼容旧库：建表后补齐状态列（整网替换会清空事件，旧事件随替换失效）
ALTER TABLE isolation_events
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open';
ALTER TABLE isolation_events
    ADD COLUMN IF NOT EXISTS restored_at TIMESTAMPTZ;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'isolation_events_status_check'
    ) THEN
        ALTER TABLE isolation_events
            ADD CONSTRAINT isolation_events_status_check
            CHECK (status IN ('open', 'restored'));
    END IF;
END$$;

-- 状态回看快照：只增不改、编号全局单调、永不复用。
-- 整网导入不截断本表，因此即使版本重置为 1，此前快照依然保留可读。
-- event_id 不加外键：整网导入会清空隔离事件，但历史快照仍须自包含可读。
CREATE TABLE IF NOT EXISTS state_snapshots (
    id           BIGSERIAL PRIMARY KEY,
    op_type      TEXT NOT NULL CHECK (op_type IN
                    ('network_import', 'isolation_submit',
                     'restore_confirm', 'source_switch', 'baseline')),
    network_version INTEGER NOT NULL,
    event_id     BIGINT,
    state        JSONB NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_state_snapshots_version
    ON state_snapshots (network_version);
"""

# 事件状态：待修复 / 已复供
EVENT_OPEN = "open"
EVENT_RESTORED = "restored"


@contextmanager
def get_conn() -> Iterator:
    """获取一个数据库连接，调用方负责 commit/rollback。"""
    conn = psycopg2.connect(get_settings().database_url)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction() -> Iterator:
    """事务上下文：正常结束提交，异常回滚。"""
    with get_conn() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def init_db() -> None:
    """启动时建表；等待 PostgreSQL 就绪。

    旧库上线（已存在管网数据但从无快照）时，仅以**当下状态**建立一条
    baseline 基线快照；绝不按既有事件伪造早期快照。空库与已有快照的库
    不产生基线。
    """
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(1, settings.db_connect_retries + 1):
        try:
            with get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(DDL)
                _ensure_baseline_if_legacy(conn)
                conn.commit()
            return
        except (psycopg2.OperationalError, UndefinedTable) as exc:  # pragma: no cover
            last_error = exc
            time.sleep(settings.db_connect_retry_interval)
    raise RuntimeError(f"数据库初始化失败：{last_error}")


def _ensure_baseline_if_legacy(conn) -> None:
    """旧库上线基线：快照表为空且当前存在管网时，冻结当下状态为 baseline。

    必须在建表 DDL 之后、同一事务内执行；不修改管网版本与任何业务状态。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM state_snapshots")
        if cur.fetchone()[0] > 0:
            return
        cur.execute("SELECT count(*) FROM nodes")
        if cur.fetchone()[0] == 0:
            return
    version, _updated_at, nodes, pipes = load_snapshot(conn)
    open_faults = [(e["id"], e["fault_pipe_id"]) for e in list_open_events(conn)]
    insert_state_snapshot(
        conn,
        op_type=OP_BASELINE,
        network_version=version,
        state=freeze_state(nodes, pipes, open_faults),
    )


# ---------------------------------------------------------------- 快照


def load_snapshot(conn, *, for_update: bool = False) -> tuple[int, datetime | None, dict[str, Node], dict[str, Pipe]]:
    """读取当前版本与全部节点/管段。for_update=True 时锁定元数据行。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT version, updated_at FROM network_meta WHERE id = 1"
            f"{' FOR UPDATE' if for_update else ''}"
        )
        row = cur.fetchone()
        version = row["version"] if row else 0
        updated_at = row["updated_at"] if row else None

        cur.execute("SELECT id, type, name, running FROM nodes")
        nodes = {
            r["id"]: Node(
                id=r["id"], type=r["type"], name=r["name"], running=r["running"]
            )
            for r in cur.fetchall()
        }

        cur.execute(
            "SELECT id, from_node, to_node, has_valve, valve_closed, name "
            "FROM pipes ORDER BY id"
        )
        pipes = {
            r["id"]: Pipe(
                id=r["id"],
                from_node=r["from_node"],
                to_node=r["to_node"],
                has_valve=r["has_valve"],
                valve_closed=r["valve_closed"],
                name=r["name"],
            )
            for r in cur.fetchall()
        }
    return version, updated_at, nodes, pipes


# ---------------------------------------------------------------- 整网导入


def replace_network(conn, nodes: list[Node], pipes: list[Pipe]) -> tuple[int, int]:
    """在已开启的事务中整网替换：清空旧数据并重写，版本重置为 1。

    返回 (新版本号, 快照编号)。导入是对管网的整体重新定义，因此版本从头
    计数；历史事件也随整网替换一并清空，避免引用旧编号。

    状态快照表（state_snapshots）**不**清空：编号不可复用，导入后版本
    虽重置为 1，此前快照依旧保留；本次导入在同一事务内另冻结一条新快照。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        if cur.fetchone() is None:
            cur.execute("INSERT INTO network_meta (id, version) VALUES (1, 0)")

        # 外键 pipes -> nodes：必须在同一条 TRUNCATE 中清空（CASCADE 仅作用于
        # 业务表）；state_snapshots 刻意不在清空列表内，历史快照永不被导入覆盖。
        cur.execute("TRUNCATE isolation_events, pipes, nodes")

        cur.executemany(
            "INSERT INTO nodes (id, type, name, running) "
            "VALUES (%s, %s, %s, %s)",
            [(n.id, n.type, n.name, n.running) for n in nodes],
        )
        cur.executemany(
            "INSERT INTO pipes (id, from_node, to_node, has_valve, valve_closed, name) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            [
                (p.id, p.from_node, p.to_node, p.has_valve, p.valve_closed, p.name)
                for p in pipes
            ],
        )
        cur.execute(
            "UPDATE network_meta SET version = 1, updated_at = now() WHERE id = 1"
        )

    # 同一事务内冻结“操作后”快照：版本 1、无历史事件、无待修复故障
    node_map = {n.id: n for n in nodes}
    pipe_map = {p.id: p for p in pipes}
    snapshot_id = insert_state_snapshot(
        conn,
        op_type=OP_NETWORK_IMPORT,
        network_version=1,
        state=freeze_state(node_map, pipe_map, []),
    )
    return 1, snapshot_id


# ---------------------------------------------------------------- 隔离提交


def submit_isolation(
    conn,
    expected_version: int,
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    result: PlanResult,
) -> dict:
    """在已开启的事务中完成提交。

    调用方须先用当前快照重新评估并确认 result.isolatable 且无阻断问题。
    本函数只负责：锁行 → 版本比对 → 原子更新阀位、版本 → 写事件。
    返回新版本信息；版本过期抛出 VersionConflict。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        if close_pipe_ids:
            cur.execute(
                "UPDATE pipes SET valve_closed = TRUE "
                "WHERE id = ANY(%s) AND has_valve = TRUE",
                (close_pipe_ids,),
            )
        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )
        cur.execute(
            "INSERT INTO isolation_events "
            "(version_before, version_after, fault_pipe_id, "
            " closed_pipe_ids, affected_sink_ids, status) "
            "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s) RETURNING id, created_at",
            (
                current_version,
                new_version,
                fault_pipe_id,
                json.dumps(close_pipe_ids),
                json.dumps(result.affected_sink_ids),
                EVENT_OPEN,
            ),
        )
        event_id, created_at = cur.fetchone()

    # 同一事务内冻结“操作后”快照：新阀位 + 全部待修复故障（含本事件）
    _post_version, _updated_at, post_nodes, post_pipes = load_snapshot(conn)
    open_faults = [(e["id"], e["fault_pipe_id"]) for e in list_open_events(conn)]
    snapshot_id = insert_state_snapshot(
        conn,
        op_type=OP_ISOLATION_SUBMIT,
        network_version=new_version,
        event_id=event_id,
        state=freeze_state(post_nodes, post_pipes, open_faults),
    )

    return {
        "version": new_version,
        "event_id": event_id,
        "created_at": created_at,
        "snapshot_id": snapshot_id,
    }


class VersionConflict(Exception):
    def __init__(self, current: int, expected: int):
        self.current = current
        self.expected = expected
        super().__init__(f"版本过期：当前版本 {current}，提交基于版本 {expected}")


# ---------------------------------------------------------------- 水源状态切换


def switch_sources(
    conn,
    expected_version: int,
    targets: list[tuple[str, bool]],
) -> dict:
    """在已开启的事务中原子完成水源运行状态切换。

    调用方须先用当前快照完成结构与安全性复核（仅水源可切换、无重复、
    切换后待修复故障无重新进水风险）。
    本函数只负责：锁版本行 → 版本比对 → 更新水源状态 → 版本 +1。
    版本过期抛出 VersionConflict；无实际变化不应调用本函数。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        for source_id, running in targets:
            # 仅水源节点可切换（调用方已复核，SQL 再约束一次防止部分写入）
            cur.execute(
                "UPDATE nodes SET running = %s WHERE id = %s AND type = 'source'",
                (running, source_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError(f"水源节点不存在或不可切换：{source_id}")

        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )

    # 同一事务内冻结“操作后”快照：新水源运行状态、阀位/故障不变
    _post_version, _updated_at, post_nodes, post_pipes = load_snapshot(conn)
    open_faults = [(e["id"], e["fault_pipe_id"]) for e in list_open_events(conn)]
    snapshot_id = insert_state_snapshot(
        conn,
        op_type=OP_SOURCE_SWITCH,
        network_version=new_version,
        state=freeze_state(post_nodes, post_pipes, open_faults),
    )

    return {"version": new_version, "snapshot_id": snapshot_id}


# ---------------------------------------------------------------- 复供


def _decode_jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def _serialize_event_row(r: dict) -> dict:
    return {
        "id": r["id"],
        "version_before": r["version_before"],
        "version_after": r["version_after"],
        "fault_pipe_id": r["fault_pipe_id"],
        "closed_pipe_ids": _decode_jsonb(r["closed_pipe_ids"]),
        "affected_sink_ids": _decode_jsonb(r["affected_sink_ids"]),
        "status": r["status"],
        "restored_at": r.get("restored_at"),
        "created_at": r["created_at"],
    }


_EVENT_COLUMNS = (
    "id, version_before, version_after, fault_pipe_id, "
    "closed_pipe_ids, affected_sink_ids, status, restored_at, created_at"
)


def get_event(conn, event_id: int, *, for_update: bool = False) -> dict | None:
    """按编号读取隔离事件；for_update=True 时锁定事件行。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_EVENT_COLUMNS} FROM isolation_events WHERE id = %s"
            f"{' FOR UPDATE' if for_update else ''}",
            (event_id,),
        )
        row = cur.fetchone()
    return _serialize_event_row(row) if row else None


def list_open_events(conn, *, exclude_event_id: int | None = None,
                     for_update: bool = False) -> list[dict]:
    """读取全部待修复(status=open)事件；可排除指定事件（即正在复供的事件）。"""
    sql = f"SELECT {_EVENT_COLUMNS} FROM isolation_events WHERE status = %s"
    params: list = [EVENT_OPEN]
    if exclude_event_id is not None:
        sql += " AND id <> %s"
        params.append(exclude_event_id)
    sql += " ORDER BY id"
    if for_update:
        sql += " FOR UPDATE"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_serialize_event_row(r) for r in rows]


def confirm_restore(
    conn,
    event_id: int,
    expected_version: int,
    reopened_pipe_ids: list[str],
    restored_sink_ids: list[str],
) -> dict:
    """在已开启的事务内完成复供确认。

    调用方须已锁定事件行与版本行，并基于当前快照完成安全性复核。
    本函数只负责：版本复核 → 重开阀门 → 版本 +1 → 事件置为已复供，原子生效。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM network_meta WHERE id = 1 FOR UPDATE")
        row = cur.fetchone()
        current_version = row[0] if row else 0
        if current_version != expected_version:
            raise VersionConflict(current_version, expected_version)

        if reopened_pipe_ids:
            cur.execute(
                "UPDATE pipes SET valve_closed = FALSE "
                "WHERE id = ANY(%s) AND has_valve = TRUE",
                (reopened_pipe_ids,),
            )
        new_version = current_version + 1
        cur.execute(
            "UPDATE network_meta SET version = %s, updated_at = now() WHERE id = 1",
            (new_version,),
        )
        cur.execute(
            "UPDATE isolation_events "
            "SET status = %s, restored_at = now() "
            "WHERE id = %s AND status = %s",
            (EVENT_RESTORED, event_id, EVENT_OPEN),
        )
        if cur.rowcount != 1:  # 并发下事件已被复供：拒绝且不产生部分变更
            raise RuntimeError(f"隔离事件 {event_id} 已复供或不存在")
        cur.execute("SELECT restored_at FROM isolation_events WHERE id = %s", (event_id,))
        restored_at = cur.fetchone()[0]

    # 同一事务内冻结“操作后”快照：阀门已重开、本事件不再计入待修复故障
    _post_version, _updated_at, post_nodes, post_pipes = load_snapshot(conn)
    open_faults = [(e["id"], e["fault_pipe_id"]) for e in list_open_events(conn)]
    snapshot_id = insert_state_snapshot(
        conn,
        op_type=OP_RESTORE_CONFIRM,
        network_version=new_version,
        event_id=event_id,
        state=freeze_state(post_nodes, post_pipes, open_faults),
    )

    return {
        "version": new_version,
        "restored_at": restored_at,
        "snapshot_id": snapshot_id,
    }


# ---------------------------------------------------------------- 历史


def list_events(conn, limit: int) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_EVENT_COLUMNS} "
            "FROM isolation_events ORDER BY id DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [_serialize_event_row(r) for r in rows]


# ---------------------------------------------------------------- 状态快照


_SNAPSHOT_COLUMNS = "id, op_type, network_version, event_id, state, created_at"


def _serialize_snapshot_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "op_type": row["op_type"],
        "network_version": row["network_version"],
        "event_id": row["event_id"],
        "state": _decode_jsonb(row["state"]),
        "created_at": row["created_at"],
    }


def insert_state_snapshot(
    conn,
    *,
    op_type: str,
    network_version: int,
    state: dict,
    event_id: int | None = None,
) -> int:
    """在调用方事务内冻结一条状态快照，返回不可复用的快照编号。

    仅插入、不更新、不删除；快照编号由 BIGSERIAL 全局单调分配，整网导入
    也不重置该序列。必须在状态实际变更后、同一事务内调用。
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO state_snapshots (op_type, network_version, event_id, state) "
            "VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
            (op_type, network_version, event_id, json.dumps(state)),
        )
        return cur.fetchone()[0]


def get_state_snapshot(conn, snapshot_id: int) -> dict | None:
    """按编号读取冻结快照；不存在返回 None（路由层据此报 SNAPSHOT_NOT_FOUND）。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM state_snapshots WHERE id = %s",
            (snapshot_id,),
        )
        row = cur.fetchone()
    return _serialize_snapshot_row(row) if row else None


def list_state_snapshots(
    conn, *, limit: int, after_id: int | None = None
) -> list[dict]:
    """按编号升序列出快照元数据（含冻结状态）。

    after_id 为游标：只返回编号严格大于它的快照；不传时从头返回。
    """
    sql = f"SELECT {_SNAPSHOT_COLUMNS} FROM state_snapshots"
    params: list = []
    if after_id is not None:
        sql += " WHERE id > %s"
        params.append(after_id)
    sql += " ORDER BY id ASC LIMIT %s"
    params.append(limit)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_serialize_snapshot_row(r) for r in rows]


def get_previous_state_snapshot(conn, snapshot_id: int) -> dict | None:
    """读取编号严格小于 snapshot_id 的最近一条快照；为首条快照时返回 None。"""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM state_snapshots "
            "WHERE id < %s ORDER BY id DESC LIMIT 1",
            (snapshot_id,),
        )
        row = cur.fetchone()
    return _serialize_snapshot_row(row) if row else None
