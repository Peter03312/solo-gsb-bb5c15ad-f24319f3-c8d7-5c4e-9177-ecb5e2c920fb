"""供水管网故障隔离服务 —— FastAPI 入口。

接口前缀 /api：
- GET  /health                                         健康检查
- POST /api/network/import                             整网导入（替换式，版本重置为 1）
- GET  /api/network                                    读取当前管网与版本
- POST /api/isolation/recommend                        关阀建议（只读，按当前管网计算可提交方案）
- POST /api/isolation/preview                          隔离方案预演（只读）
- POST /api/isolation/submit                           隔离方案提交（原子关阀 + 版本 +1）
- GET  /api/isolation/events                           隔离/复供操作历史
- POST /api/isolation/events/{event_id}/restore/preview 复供预演（只读）
- POST /api/isolation/events/{event_id}/restore        复供确认（单事务复核 + 重开阀门）
- POST /api/sources/switch/preview                     水源停运/恢复预演（只读）
- POST /api/sources/switch                             水源停运/恢复确认（单事务复核 + 原子改状态）
- POST /api/delivery/evaluate                          用水点最低流量核算（只读：现有阀位/故障下最大可交付总量）
- POST /api/delivery/allocate                          保障等级分档交付核算（只读：按等级总量、再逐点字典序最大化交付）
- POST /api/delivery/bypass                            临时旁通管评估（只读：基线与内存接入旁通后的分档交付对照，重新进水判不可用）
- POST /api/meters/reconcile                           管段流量计读数核对（只读：一致性 + 缺报读数可行流量范围）
- POST /api/drill                                      调度演练（只读：按顺序演练一串隔离/复供/水源切换，逐步套用提交校验）

状态回看（快照）：
- GET  /api/snapshots                                  快照列表（按编号升序，支持 after 游标）
- GET  /api/snapshots/{snapshot_id}                    按编号读取冻结快照（节点/水源/阀位/待修复故障/供水点）
- GET  /api/snapshots/{snapshot_id}/diff               与相邻上一条快照的六类净变化（可用 base_id 指定对照基线）

调度审计（快照摘要链）：
- GET  /api/audit/chain                                摘要链信息（创世摘要、建链起点、现存/已入链快照范围）
- POST /api/audit/verify                               只读核验快照区间：各条摘要与首个内容损坏/链路断裂位置
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import db
from . import audit
from .config import get_settings
from .planning import (
    Node,
    Pipe,
    DrillStep,
    diff_states,
    allocate_delivery,
    evaluate_bypass,
    evaluate_delivery,
    evaluate_plan,
    evaluate_restore,
    evaluate_source_switch,
    recommend_plan,
    simulate_drill,
    validate_bypass_request,
    validate_delivery_request,
    validate_reconcile_request,
    reconcile_meters,
    validate_import,
    BLOCKING_ISSUE_CODES,
    DRILL_STEP_ISOLATION,
    DRILL_STEP_RESTORE,
    DRILL_STEP_SOURCE_SWITCH,
)
from .schemas import (
    ImportIn,
    PlanIn,
    RecommendIn,
    RestoreIn,
    SourceSwitchIn,
    DeliveryEvaluateIn,
    DeliveryAllocateIn,
    DeliveryBypassIn,
    MeterReconcileIn,
    DrillIn,
    DrillIsolationStepIn,
    DrillRestoreStepIn,
    ImportOut,
    NetworkOut,
    PreviewOut,
    RecommendOut,
    SubmitOut,
    RestorePreviewOut,
    RestoreOut,
    SourceSwitchPreviewOut,
    SourceSwitchOut,
    DeliveryEvaluateOut,
    DeliveryAllocateOut,
    DeliveryBypassOut,
    MeterReconcileOut,
    DrillOut,
    EventOut,
    SnapshotOut,
    SnapshotMetaOut,
    SnapshotDiffOut,
    AuditVerifyIn,
    AuditVerifyOut,
    AuditChainInfoOut,
)
from .serializers import (
    serialize_preview,
    serialize_recommend,
    serialize_restore_preview,
    serialize_source_switch_preview,
    serialize_delivery,
    serialize_allocation,
    serialize_bypass,
    serialize_reconcile,
    serialize_drill,
    serialize_snapshot,
    serialize_snapshot_row,
    serialize_snapshot_diff,
)


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, issues: list | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.issues = issues or []
        super().__init__(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="供水管网故障隔离服务",
    version="1.4.0",
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(request, exc: ApiError):
    body: dict = {"error": {"code": exc.code, "message": exc.message}}
    if exc.issues:
        body["error"]["issues"] = [i.as_dict() for i in exc.issues]
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(db.VersionConflict)
async def version_conflict_handler(request, exc: db.VersionConflict):
    return JSONResponse(
        status_code=409,
        content={
            "error": {
                "code": "VERSION_CONFLICT",
                "message": f"版本过期：当前版本为 {exc.current}，提交基于版本 {exc.expected}，请重新读取后再试",
                "current_version": exc.current,
                "expected_version": exc.expected,
            }
        },
    )


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- 管网


def _to_domain_nodes(payload: ImportIn) -> list[Node]:
    return [Node(id=n.id, type=n.type, name=n.name) for n in payload.nodes]


def _to_domain_pipes(payload: ImportIn) -> list[Pipe]:
    return [
        Pipe(
            id=p.id,
            from_node=p.from_node,
            to_node=p.to_node,
            has_valve=p.has_valve,
            valve_closed=p.valve_closed,
            name=p.name,
        )
        for p in payload.pipes
    ]


@app.post(
    "/api/network/import",
    response_model=ImportOut,
    tags=["管网"],
    summary="整网导入（替换当前管网，版本重置为 1）",
)
def import_network(payload: ImportIn):
    nodes = _to_domain_nodes(payload)
    pipes = _to_domain_pipes(payload)

    issues = validate_import(nodes, pipes)
    if issues:
        raise ApiError(
            422,
            "IMPORT_INVALID",
            "管网数据校验失败：存在悬空端点、重号、自环或无阀管段被标记关闭",
            issues=issues,
        )

    with db.transaction() as conn:
        version, snapshot_id = db.replace_network(conn, nodes, pipes)

    return {
        "version": version,
        "node_count": len(nodes),
        "pipe_count": len(pipes),
        "snapshot_id": snapshot_id,
    }


@app.get("/api/network", response_model=NetworkOut, tags=["管网"], summary="读取当前管网与版本")
def get_network():
    with db.get_conn() as conn:
        version, updated_at, nodes, pipes = db.load_snapshot(conn)
    return serialize_snapshot(version, updated_at, nodes, pipes)


# ---------------------------------------------------------------- 隔离


@app.post(
    "/api/isolation/recommend",
    response_model=RecommendOut,
    tags=["故障隔离"],
    summary="关阀建议：只知道故障管段时，按当前管网计算一套可提交的关阀方案（只读）",
)
def recommend_isolation(payload: RecommendIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

    result = recommend_plan(
        nodes,
        pipes,
        payload.fault_pipe_id,
        pending_fault_pipe_ids=pending_fault_ids,
    )
    return serialize_recommend(version, payload.version, payload.fault_pipe_id, result)


@app.post(
    "/api/isolation/preview",
    response_model=PreviewOut,
    tags=["故障隔离"],
    summary="预演：判断能否隔离并计算失供用水点（只读，不改状态）",
)
def preview_isolation(payload: PlanIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

    result = evaluate_plan(
        nodes,
        pipes,
        payload.fault_pipe_id,
        list(payload.close_pipe_ids),
        for_submit=False,
        pending_fault_pipe_ids=pending_fault_ids,
    )
    return serialize_preview(
        version, payload.version, payload.fault_pipe_id, payload.close_pipe_ids, result
    )


@app.post(
    "/api/isolation/submit",
    response_model=SubmitOut,
    tags=["故障隔离"],
    summary="提交隔离：基于当前版本重新校验，原子更新阀位与版本",
)
def submit_isolation(payload: PlanIn):
    close_ids = list(dict.fromkeys(payload.close_pipe_ids))  # 去重但保序（重复仍会被拒）

    with db.transaction() as conn:
        # 锁定当前版本后基于最新数据重新校验
        version, _updated_at, nodes, pipes = db.load_snapshot(conn, for_update=True)

        if version != payload.version:
            raise db.VersionConflict(version, payload.version)

        # 其他待修复事件的故障管段仍阻断通水
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

        result = evaluate_plan(
            nodes,
            pipes,
            payload.fault_pipe_id,
            list(payload.close_pipe_ids),
            for_submit=True,
            pending_fault_pipe_ids=pending_fault_ids,
        )

        blocking = [i for i in result.issues if i.code in BLOCKING_ISSUE_CODES]
        if blocking:
            raise ApiError(
                422,
                "PLAN_INVALID",
                "隔离方案非法（管段不存在、无阀管段、重复关闭或把故障管段当作阀门）",
                issues=blocking,
            )
        if not result.isolatable:
            raise ApiError(
                422,
                "NOT_ISOLATABLE",
                "该方案无法隔离故障管段：故障管段至少有一端在方案图中仍可从水源到达",
                issues=result.issues,
            )

        outcome = db.submit_isolation(
            conn,
            expected_version=payload.version,
            fault_pipe_id=payload.fault_pipe_id,
            close_pipe_ids=close_ids,
            result=result,
        )

    return {
        "version": outcome["version"],
        "fault_pipe_id": payload.fault_pipe_id,
        "closed_pipe_ids": close_ids,
        "affected_sink_ids": result.affected_sink_ids,
        "event_id": outcome["event_id"],
        "snapshot_id": outcome["snapshot_id"],
    }


@app.get(
    "/api/isolation/events",
    response_model=list[EventOut],
    tags=["故障隔离"],
    summary="隔离/复供操作历史（按时间倒序，含待修复与已复供状态）",
)
def get_events():
    settings = get_settings()
    with db.get_conn() as conn:
        return db.list_events(conn, settings.history_limit)


# ---------------------------------------------------------------- 复供


def _load_restore_context(conn, event_id: int, *, for_update: bool = False):
    """读取事件、快照与其他待修复事件。调用方负责事件存在性/状态/版本判定。"""
    event = db.get_event(conn, event_id, for_update=for_update)
    if event is None:
        raise ApiError(404, "EVENT_NOT_FOUND", f"隔离事件不存在：{event_id}")
    if event["status"] != db.EVENT_OPEN:
        raise ApiError(
            409,
            "EVENT_ALREADY_RESTORED",
            f"隔离事件 {event_id} 已复供，不能重复操作",
        )
    version, _updated_at, nodes, pipes = db.load_snapshot(
        conn, for_update=for_update
    )
    other_open = db.list_open_events(conn, exclude_event_id=event_id,
                                     for_update=for_update)
    other_fault_ids = frozenset(e["fault_pipe_id"] for e in other_open)
    result = evaluate_restore(
        nodes,
        pipes,
        reopen_pipe_ids=frozenset(event["closed_pipe_ids"]),
        other_fault_pipe_ids=other_fault_ids,
    )
    return version, event, result


@app.post(
    "/api/isolation/events/{event_id}/restore/preview",
    response_model=RestorePreviewOut,
    tags=["故障复供"],
    summary="复供预演：重开该事件阀门后的安全性与预计恢复用水点（只读）",
)
def restore_preview(event_id: int, payload: RestoreIn):
    with db.get_conn() as conn:
        version, event, result = _load_restore_context(conn, event_id)

    return serialize_restore_preview(version, payload.version, event, result)


@app.post(
    "/api/isolation/events/{event_id}/restore",
    response_model=RestoreOut,
    tags=["故障复供"],
    summary="复供确认：同一事务内复核版本与安全性，原子重开阀门并更新事件状态",
)
def restore_confirm(event_id: int, payload: RestoreIn):
    with db.transaction() as conn:
        # 锁定事件行/版本行后基于最新数据复核，保证不产生部分变更
        version, event, result = _load_restore_context(conn, event_id, for_update=True)

        if version != payload.version:
            raise db.VersionConflict(version, payload.version)
        if not result.safe:
            raise ApiError(
                422,
                "RESTORE_UNSAFE",
                "复供存在重新进水风险：其他待修复故障管段的端点将重新通水",
                issues=result.blocks,
            )

        reopen_ids = list(event["closed_pipe_ids"])
        outcome = db.confirm_restore(
            conn,
            event_id=event_id,
            expected_version=payload.version,
            reopened_pipe_ids=reopen_ids,
            restored_sink_ids=result.restored_sink_ids,
        )

    return {
        "version": outcome["version"],
        "event_id": event_id,
        "status": db.EVENT_RESTORED,
        "fault_pipe_id": event["fault_pipe_id"],
        "reopened_pipe_ids": reopen_ids,
        "restored_sink_ids": result.restored_sink_ids,
        "snapshot_id": outcome["snapshot_id"],
    }


# ---------------------------------------------------------------- 水源状态切换


def _load_source_switch_context(conn, payload: SourceSwitchIn, *, for_update: bool = False):
    """读取快照、待修复故障并评估水源切换。返回 (version, result, targets)。"""
    version, _updated_at, nodes, pipes = db.load_snapshot(conn, for_update=for_update)
    pending_fault_ids = frozenset(
        e["fault_pipe_id"] for e in db.list_open_events(conn)
    )
    targets = [(t.source_id, t.running) for t in payload.targets]
    result = evaluate_source_switch(
        nodes,
        pipes,
        targets,
        pending_fault_pipe_ids=pending_fault_ids,
    )
    return version, result, targets


def _switch_response_body(version: int, targets, result, snapshot_id: int | None = None) -> dict:
    return {
        "version": version,
        "changed": result.changed,
        "targets": [
            {"source_id": source_id, "running": running}
            for source_id, running in targets
        ],
        "affected_sink_ids": result.affected_sink_ids,
        "restored_sink_ids": result.restored_sink_ids,
        # 全部目标状态已满足（无实际变化）时不产生快照，恒为 null
        "snapshot_id": snapshot_id,
    }


@app.post(
    "/api/sources/switch/preview",
    response_model=SourceSwitchPreviewOut,
    tags=["水源调度"],
    summary="水源停运/恢复预演：失供与恢复用水点、待修复故障重新进水风险（只读）",
)
def source_switch_preview(payload: SourceSwitchIn):
    with db.get_conn() as conn:
        version, result, targets = _load_source_switch_context(conn, payload)

    return serialize_source_switch_preview(version, payload.version, result)


@app.post(
    "/api/sources/switch",
    response_model=SourceSwitchOut,
    tags=["水源调度"],
    summary="水源停运/恢复确认：锁版本复核安全性，原子更新水源状态与版本",
)
def source_switch_confirm(payload: SourceSwitchIn):
    with db.transaction() as conn:
        # 锁定版本行后基于最新状态复核，保证不产生部分变更
        version, result, targets = _load_source_switch_context(
            conn, payload, for_update=True
        )

        if version != payload.version:
            raise db.VersionConflict(version, payload.version)
        if result.issues:
            raise ApiError(
                422,
                "SOURCE_SWITCH_INVALID",
                "水源切换请求非法（编号不存在、节点不是水源或同批编号重复）",
                issues=result.issues,
            )

        # 全部目标状态已满足：不推进版本、不产生快照，直接回显当前状态
        if not result.changed:
            return _switch_response_body(version, targets, result, snapshot_id=None)

        if not result.safe:
            raise ApiError(
                422,
                "SOURCE_SWITCH_UNSAFE",
                "水源切换存在重新进水风险：恢复水源后待修复故障管段的端点将重新通水",
                issues=result.blocks,
            )

        outcome = db.switch_sources(
            conn, expected_version=payload.version, targets=targets
        )

    return _switch_response_body(
        outcome["version"], targets, result, outcome["snapshot_id"]
    )


# ---------------------------------------------------------------- 流量核算（只读）


@app.post(
    "/api/delivery/evaluate",
    response_model=DeliveryEvaluateOut,
    tags=["流量核算"],
    summary="用水点最低流量核算：现有阀位与故障状态下的最大可交付总量（只读）",
)
def evaluate_delivery_request(payload: DeliveryEvaluateIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        # 已关闭阀门与待修复故障管段不可通行：阻断口径与既有可达性接口完全一致
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

    source_caps = [(s.source_id, s.capacity) for s in payload.sources]
    pipe_caps = [(p.pipe_id, p.capacity) for p in payload.pipes]
    demands = [(d.sink_id, d.demand) for d in payload.demands]

    issues = validate_delivery_request(nodes, pipes, source_caps, pipe_caps, demands)
    if issues:
        raise ApiError(
            422,
            "DELIVERY_REQUEST_INVALID",
            "流量核算请求非法（编号未覆盖当前管网、同类重复、用水点不存在或需求列表为空）",
            issues=issues,
        )

    blocked_pipe_ids = frozenset(
        p.id for p in pipes.values() if p.has_valve and p.valve_closed
    ) | pending_fault_ids

    result = evaluate_delivery(
        nodes,
        pipes,
        source_caps,
        pipe_caps,
        demands,
        blocked_pipe_ids=blocked_pipe_ids,
    )
    return serialize_delivery(version, payload.version, result)


@app.post(
    "/api/delivery/allocate",
    response_model=DeliveryAllocateOut,
    tags=["流量核算"],
    summary="保障等级分档交付：按等级总量、再逐点字典序最大化各用水点交付（只读）",
)
def allocate_delivery_request(payload: DeliveryAllocateIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        # 已关闭阀门与待修复故障管段不可通行：阻断口径与既有可达性接口完全一致
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

    source_caps = [(s.source_id, s.capacity) for s in payload.sources]
    pipe_caps = [(p.pipe_id, p.capacity) for p in payload.pipes]
    demands = [(d.sink_id, d.demand) for d in payload.demands]
    graded_demands = [(d.sink_id, d.demand, d.level) for d in payload.demands]

    # 编号/覆盖/重复/类型/非空校验与现有总量核算接口完全一致
    issues = validate_delivery_request(nodes, pipes, source_caps, pipe_caps, demands)
    if issues:
        raise ApiError(
            422,
            "DELIVERY_REQUEST_INVALID",
            "分档交付请求非法（编号未覆盖当前管网、同类重复、用水点不存在、"
            "类型错误或需求列表为空）",
            issues=issues,
        )

    blocked_pipe_ids = frozenset(
        p.id for p in pipes.values() if p.has_valve and p.valve_closed
    ) | pending_fault_ids

    result = allocate_delivery(
        nodes,
        pipes,
        source_caps,
        pipe_caps,
        graded_demands,
        blocked_pipe_ids=blocked_pipe_ids,
    )
    return serialize_allocation(version, payload.version, result)


@app.post(
    "/api/delivery/bypass",
    response_model=DeliveryBypassOut,
    tags=["流量核算"],
    summary="临时旁通管评估：基线与内存接入旁通后的分档交付对照，重新进水风险判不可用（只读）",
)
def evaluate_bypass_request(payload: DeliveryBypassIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        # 已关闭阀门与待修复故障管段不可通行：阻断口径与既有可达性接口完全一致
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

    source_caps = [(s.source_id, s.capacity) for s in payload.sources]
    pipe_caps = [(p.pipe_id, p.capacity) for p in payload.pipes]
    demands = [(d.sink_id, d.demand) for d in payload.demands]
    graded_demands = [(d.sink_id, d.demand, d.level) for d in payload.demands]

    # 旁通端点 + 编号/覆盖/重复/类型/非空校验（编号规则与分档交付接口一致）
    issues = validate_bypass_request(
        nodes,
        pipes,
        payload.bypass.from_node,
        payload.bypass.to_node,
        source_caps,
        pipe_caps,
        demands,
    )
    if issues:
        raise ApiError(
            422,
            "BYPASS_REQUEST_INVALID",
            "临时旁通评估请求非法（旁通端点不存在或相同、编号未覆盖当前管网、"
            "同类重复、用水点不存在或需求列表为空）",
            issues=issues,
        )

    blocked_pipe_ids = frozenset(
        p.id for p in pipes.values() if p.has_valve and p.valve_closed
    ) | pending_fault_ids

    result = evaluate_bypass(
        nodes,
        pipes,
        from_node=payload.bypass.from_node,
        to_node=payload.bypass.to_node,
        bypass_capacity=payload.bypass.capacity,
        source_caps=source_caps,
        pipe_caps=pipe_caps,
        demands=graded_demands,
        blocked_pipe_ids=blocked_pipe_ids,
        pending_fault_pipe_ids=pending_fault_ids,
    )
    return serialize_bypass(
        version,
        payload.version,
        payload.bypass.from_node,
        payload.bypass.to_node,
        payload.bypass.capacity,
        result,
    )


# ---------------------------------------------------------------- 读数核对（只读）


@app.post(
    "/api/meters/reconcile",
    response_model=MeterReconcileOut,
    tags=["读数核对"],
    summary="管段流量计读数核对：按当前状态校验一致性，并为缺报管段推断可行流量范围（只读）",
)
def reconcile_meter_readings(payload: MeterReconcileIn):
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        # 阻断口径与既有可达性 / 流量核算接口完全一致：已关闭阀门 + 待修复故障管段
        pending_fault_ids = frozenset(
            e["fault_pipe_id"] for e in db.list_open_events(conn)
        )

    injections = [(s.source_id, s.injection) for s in payload.sources]
    withdrawals = [(d.sink_id, d.withdrawal) for d in payload.sinks]
    pipe_readings = [(p.pipe_id, p.capacity, p.reading) for p in payload.pipes]

    issues = validate_reconcile_request(
        nodes, pipes, injections, withdrawals, pipe_readings
    )
    if issues:
        raise ApiError(
            422,
            "METER_RECONCILE_INVALID",
            "读数核对请求非法（编号未覆盖当前管网、同类重复、未知节点或用水点类型错误）",
            issues=issues,
        )

    blocked_pipe_ids = frozenset(
        p.id for p in pipes.values() if p.has_valve and p.valve_closed
    ) | pending_fault_ids

    result = reconcile_meters(
        nodes,
        pipes,
        injections,
        withdrawals,
        pipe_readings,
        blocked_pipe_ids=blocked_pipe_ids,
    )
    return serialize_reconcile(version, payload.version, pipes, result)


# ---------------------------------------------------------------- 状态快照回看


def _get_snapshot_or_404(conn, snapshot_id: int) -> dict:
    row = db.get_state_snapshot(conn, snapshot_id)
    if row is None:
        raise ApiError(
            404,
            "SNAPSHOT_NOT_FOUND",
            f"状态快照不存在：{snapshot_id}（快照编号不可复用，请核对编号后重试）",
        )
    return row


@app.get(
    "/api/snapshots",
    response_model=list[SnapshotMetaOut],
    tags=["状态快照"],
    summary="状态快照列表（按编号升序；after 为分页游标）",
)
def list_snapshots(
    limit: int = Query(default=100, ge=1, le=500, description="单次返回上限（1-500）"),
    after_id: int | None = Query(
        default=None, ge=1, description="游标：只返回编号严格大于该值的快照"
    ),
):
    with db.get_conn() as conn:
        rows = db.list_state_snapshots(conn, limit=limit, after_id=after_id)
    return [serialize_snapshot_row(r) for r in rows]


@app.get(
    "/api/snapshots/{snapshot_id}",
    response_model=SnapshotOut,
    tags=["状态快照"],
    summary="按编号读取冻结快照：节点、水源运行状态、管段阀位、待修复故障、由运行水源可达的用水点",
)
def get_snapshot(snapshot_id: int):
    with db.get_conn() as conn:
        row = _get_snapshot_or_404(conn, snapshot_id)
    return serialize_snapshot_row(row)


@app.get(
    "/api/snapshots/{snapshot_id}/diff",
    response_model=SnapshotDiffOut,
    tags=["状态快照"],
    summary="相邻快照对照：节点/管段/阀位/水源状态/待修复故障/供水点的净变化",
)
def diff_snapshot(
    snapshot_id: int,
    base_id: int | None = Query(
        default=None,
        ge=1,
        description="对照基线快照编号；缺省为目标快照相邻的上一条快照。"
        "首条快照无基线时，目标状态全部计为新增。",
    ),
):
    with db.get_conn() as conn:
        target = _get_snapshot_or_404(conn, snapshot_id)
        if base_id is not None:
            base = db.get_state_snapshot(conn, base_id)
            if base is None:
                raise ApiError(
                    404,
                    "SNAPSHOT_NOT_FOUND",
                    f"对照基线状态快照不存在：{base_id}",
                )
            if base_id >= snapshot_id:
                raise ApiError(
                    422,
                    "SNAPSHOT_DIFF_INVALID",
                    "对照基线快照编号必须小于目标快照编号，且两条快照须相邻",
                )
            previous = db.get_previous_state_snapshot(conn, snapshot_id)
            previous_id = previous["id"] if previous else None
            if previous_id != base_id:
                raise ApiError(
                    422,
                    "SNAPSHOT_NOT_ADJACENT",
                    f"快照 {base_id} 与 {snapshot_id} 不相邻："
                    f"{snapshot_id} 相邻的上一条快照为 {previous_id}",
                )
        else:
            base = db.get_previous_state_snapshot(conn, snapshot_id)

    # 只基于两份冻结载荷计算，绝不读取当前管网或后续事件回填
    diff = diff_states(base["state"] if base else None, target["state"])
    return serialize_snapshot_diff(base, target, diff)


# ---------------------------------------------------------------- 调度审计（快照摘要链）


@app.get(
    "/api/audit/chain",
    response_model=AuditChainInfoOut,
    tags=["调度审计"],
    summary="摘要链信息：创世摘要、建链起点、现存/已入链快照范围（只读）",
)
def get_audit_chain():
    with db.get_conn() as conn:
        info = db.get_chain_info(conn)
    return {
        "genesis_digest": audit.GENESIS_DIGEST,
        "chain_tag": audit.CHAIN_TAG,
        **info,
    }


@app.post(
    "/api/audit/verify",
    response_model=AuditVerifyOut,
    tags=["调度审计"],
    summary="只读核验快照区间：返回各条摘要与首个可定位的内容损坏/链路断裂位置",
)
def audit_verify(payload: AuditVerifyIn):
    # 请求边界先校验：倒序明确报错（格式非法由 pydantic 422 拦截）
    if payload.end_id < payload.start_id:
        raise ApiError(
            422,
            "AUDIT_RANGE_INVALID",
            f"核验区间倒序：起始 {payload.start_id} 大于终止 {payload.end_id}",
        )
    if not audit.is_hex_digest(payload.pre_start_digest) or not audit.is_hex_digest(
        payload.end_digest
    ):
        raise ApiError(
            422,
            "AUDIT_DIGEST_INVALID",
            "起点前/终点摘要必须是 64 位十六进制 SHA-256 摘要",
        )

    with db.get_conn() as conn:
        # 端点不存在须明确报错（回滚造成的编号空洞也算不存在）
        if not db.state_snapshot_exists(conn, payload.start_id):
            raise ApiError(
                404,
                "SNAPSHOT_NOT_FOUND",
                f"核验区间起始快照不存在：{payload.start_id}",
            )
        if not db.state_snapshot_exists(conn, payload.end_id):
            raise ApiError(
                404,
                "SNAPSHOT_NOT_FOUND",
                f"核验区间终止快照不存在：{payload.end_id}",
            )
        rows = db.list_snapshot_chain_rows(
            conn, payload.start_id, payload.end_id
        )

    return audit.verify_interval(
        rows,
        pre_start_digest=payload.pre_start_digest,
        end_digest=payload.end_digest,
    )


# ---------------------------------------------------------------- 调度演练（只读）


def _to_domain_drill_steps(payload: DrillIn) -> list[DrillStep]:
    steps: list[DrillStep] = []
    for s in payload.steps:
        if isinstance(s, DrillIsolationStepIn):
            steps.append(
                DrillStep(
                    type=DRILL_STEP_ISOLATION,
                    fault_pipe_id=s.fault_pipe_id,
                    close_pipe_ids=list(s.close_pipe_ids),
                )
            )
        elif isinstance(s, DrillRestoreStepIn):
            steps.append(
                DrillStep(
                    type=DRILL_STEP_RESTORE,
                    event_id=s.event_id,
                    temp_event_ref=s.temp_event_ref,
                )
            )
        else:
            steps.append(
                DrillStep(
                    type=DRILL_STEP_SOURCE_SWITCH,
                    targets=[(t.source_id, t.running) for t in s.targets],
                )
            )
    return steps


@app.post(
    "/api/drill",
    response_model=DrillOut,
    tags=["调度演练"],
    summary="调度演练：以当前状态为起点，按顺序只读演练一串隔离/复供/水源切换操作",
)
def drill_operations(payload: DrillIn):
    # 全程只读：仅读取当前状态，在内存副本上逐步推演，不写版本/阀位/事件/快照
    with db.get_conn() as conn:
        version, _updated_at, nodes, pipes = db.load_snapshot(conn)
        # 版本过期：返回当前版本并拒绝演练（不执行任何步骤）
        if version != payload.version:
            raise db.VersionConflict(version, payload.version)
        # 现存事件：全部待修复事件 + 复供步骤引用到的其他现存事件
        # （已复供事件仅用于精确区分 EVENT_NOT_FOUND / EVENT_ALREADY_RESTORED）
        real_events = {e["id"]: e for e in db.list_open_events(conn)}
        for step in payload.steps:
            if (
                isinstance(step, DrillRestoreStepIn)
                and step.event_id is not None
                and step.event_id not in real_events
            ):
                event = db.get_event(conn, step.event_id)
                if event is not None:
                    real_events[step.event_id] = event

    result = simulate_drill(
        nodes, pipes, real_events, _to_domain_drill_steps(payload)
    )
    return serialize_drill(version, result)
