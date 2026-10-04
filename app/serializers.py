"""领域对象 -> 响应 dict 的转换。"""
from .planning import (
    Node,
    Pipe,
    PlanResult,
    RecommendResult,
    RestoreResult,
    SourceSwitchResult,
    DeliveryResult,
    AllocationResult,
    BypassEvaluationResult,
    ReconcileResult,
    DrillResult,
    DrillStepOutcome,
)

def serialize_node(node: Node) -> dict:
    return {"id": node.id, "type": node.type, "name": node.name, "running": node.running}


def serialize_pipe(pipe: Pipe) -> dict:
    return {
        "id": pipe.id,
        "from_": pipe.from_node,
        "to_": pipe.to_node,
        "has_valve": pipe.has_valve,
        "valve_closed": pipe.valve_closed if pipe.has_valve else None,
        "name": pipe.name,
    }


def serialize_snapshot(version: int, updated_at, nodes, pipes) -> dict:
    return {
        "version": version,
        "updated_at": updated_at,
        "nodes": [serialize_node(nodes[nid]) for nid in sorted(nodes)],
        "pipes": [serialize_pipe(pipes[pid]) for pid in sorted(pipes)],
    }


def serialize_preview(
    version: int,
    expected_version: int,
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    result: PlanResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        "fault_pipe_id": fault_pipe_id,
        "close_pipe_ids": list(close_pipe_ids),
        "isolatable": result.isolatable,
        "affected_sink_ids": result.affected_sink_ids,
        "issues": [i.as_dict() for i in result.issues],
    }


def serialize_restore_preview(
    version: int,
    expected_version: int,
    event: dict,
    result: RestoreResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        "event_id": event["id"],
        "status": event["status"],
        "fault_pipe_id": event["fault_pipe_id"],
        "closed_pipe_ids": list(event["closed_pipe_ids"]),
        "safe": result.safe,
        "restored_sink_ids": result.restored_sink_ids,
        "block_reasons": [b.as_dict() for b in result.blocks],
    }


def serialize_recommend(
    version: int,
    expected_version: int,
    fault_pipe_id: str,
    result: RecommendResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        "fault_pipe_id": fault_pipe_id,
        "feasible": result.feasible,
        "recommended_close_pipe_ids": list(result.recommended_close_pipe_ids),
        "affected_sink_ids": list(result.affected_sink_ids),
        "reason": result.reason.as_dict() if result.reason else None,
    }


def serialize_source_switch_preview(
    version: int,
    expected_version: int,
    result: SourceSwitchResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        "changed": result.changed,
        "safe": result.safe,
        "targets": [
            {"source_id": source_id, "running": running}
            for source_id, running in result.targets
        ],
        "affected_sink_ids": list(result.affected_sink_ids),
        "restored_sink_ids": list(result.restored_sink_ids),
        "block_reasons": [b.as_dict() for b in result.blocks],
        "issues": [i.as_dict() for i in result.issues],
    }


def serialize_delivery(
    version: int,
    expected_version: int,
    result: DeliveryResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        "total_demand": result.total_demand,
        "max_delivered_total": result.max_delivered_total,
        "feasible": result.feasible,
    }


def _allocation_body(result: AllocationResult) -> dict:
    """分档交付结果的公共部分（总量 / 各等级 / 各用水点），不含版本字段。"""
    return {
        "total_demand": result.total_demand,
        "total_delivered": result.total_delivered,
        "all_satisfied": result.all_satisfied,
        "levels": [
            {
                "level": item.level,
                "demand": item.demand,
                "delivered": item.delivered,
                "shortfall": item.shortfall,
                "all_satisfied": item.all_satisfied,
            }
            for item in result.levels
        ],
        "deliveries": [
            {
                "sink_id": item.sink_id,
                "level": item.level,
                "demand": item.demand,
                "delivered": item.delivered,
                "shortfall": item.shortfall,
                "satisfied": item.delivered == item.demand,
            }
            for item in result.entries
        ],
    }


def serialize_allocation(
    version: int,
    expected_version: int,
    result: AllocationResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        **_allocation_body(result),
    }


def serialize_bypass(
    version: int,
    expected_version: int,
    from_node: str,
    to_node: str,
    capacity: int,
    result: BypassEvaluationResult,
) -> dict:
    """临时旁通评估结果：基线恒给出；不安全时旁通交付量与差值均为 null。"""
    with_bypass = None
    deltas = None
    if result.bypass is not None:
        baseline, bypassed = result.baseline, result.bypass
        with_bypass = _allocation_body(bypassed)
        base_levels = {item.level: item for item in baseline.levels}
        bypass_levels = {item.level: item for item in bypassed.levels}
        base_entries = {item.sink_id: item for item in baseline.entries}
        deltas = {
            "total_delivered_delta": (
                bypassed.total_delivered - baseline.total_delivered
            ),
            "levels": [
                {
                    "level": lvl,
                    "baseline_delivered": base_levels[lvl].delivered,
                    "bypass_delivered": bypass_levels[lvl].delivered,
                    "delivered_delta": (
                        bypass_levels[lvl].delivered - base_levels[lvl].delivered
                    ),
                }
                for lvl in sorted(bypass_levels)
            ],
            "deliveries": [
                {
                    "sink_id": item.sink_id,
                    "level": item.level,
                    "baseline_delivered": base_entries[item.sink_id].delivered,
                    "bypass_delivered": item.delivered,
                    "delivered_delta": (
                        item.delivered - base_entries[item.sink_id].delivered
                    ),
                }
                for item in bypassed.entries
            ],
        }
    return {
        "version": version,
        "version_match": version == expected_version,
        "bypass": {"from_": from_node, "to_": to_node, "capacity": capacity},
        "safe": result.safe,
        "block_reasons": [b.as_dict() for b in result.blocks],
        "baseline": _allocation_body(result.baseline),
        "with_bypass": with_bypass,
        "deltas": deltas,
    }


def serialize_reconcile(
    version: int,
    expected_version: int,
    pipes: dict[str, Pipe],
    result: ReconcileResult,
) -> dict:
    return {
        "version": version,
        "version_match": version == expected_version,
        "consistent": result.consistent,
        "readings": [
            {
                "pipe_id": r.pipe_id,
                "from_": pipes[r.pipe_id].from_node,
                "to_": pipes[r.pipe_id].to_node,
                "min_flow": r.min_flow,
                "max_flow": r.max_flow,
                "determinable": r.determinable,
            }
            for r in result.readings
        ],
        "reasons": [reason.as_dict() for reason in result.reasons],
    }


# ---------------------------------------------------------------- 调度演练


def serialize_drill_step(step: DrillStepOutcome) -> dict:
    error = None
    if step.error is not None:
        error = {
            "code": step.error.code,
            "message": step.error.message,
            "issues": [i.as_dict() for i in step.error.issues],
            "block_reasons": [b.as_dict() for b in step.error.blocks],
        }
    return {
        "index": step.index,
        "type": step.type,
        "ok": step.ok,
        "fault_pipe_id": step.fault_pipe_id,
        "close_pipe_ids": (
            list(step.close_pipe_ids) if step.close_pipe_ids is not None else None
        ),
        "temp_event_ref": step.temp_event_ref,
        "event_id": step.event_id,
        "reopened_pipe_ids": (
            list(step.reopened_pipe_ids)
            if step.reopened_pipe_ids is not None
            else None
        ),
        "targets": (
            [
                {"source_id": source_id, "running": running}
                for source_id, running in step.targets
            ]
            if step.targets is not None
            else None
        ),
        "changed": step.changed,
        "affected_sink_ids": list(step.affected_sink_ids),
        "restored_sink_ids": list(step.restored_sink_ids),
        "error": error,
    }


def serialize_drill(version: int, result: DrillResult) -> dict:
    final = None
    if result.final is not None:
        final = {
            "sources": [
                {"source_id": source_id, "running": running}
                for source_id, running in result.final.sources
            ],
            "valves": [
                {"pipe_id": pipe_id, "valve_closed": valve_closed}
                for pipe_id, valve_closed in result.final.valves
            ],
            "open_faults": [
                {
                    "event_id": event_id,
                    "temp_event_ref": temp_event_ref,
                    "fault_pipe_id": fault_pipe_id,
                }
                for event_id, temp_event_ref, fault_pipe_id in result.final.open_faults
            ],
            "supplied_sink_ids": list(result.final.supplied_sink_ids),
        }
    return {
        "version": version,
        "ok": result.ok,
        "failed_step_index": result.failed_step_index,
        "steps": [serialize_drill_step(s) for s in result.steps],
        "final": final,
    }


# ---------------------------------------------------------------- 状态快照


def serialize_frozen_state(state: dict) -> dict:
    """冻结载荷 -> 响应结构（管段键名 from/to；与 GET /api/network 口径一致）。"""
    nodes = [
        {
            "id": n["id"],
            "type": n["type"],
            "name": n.get("name"),
            "running": n["running"],
        }
        for n in state.get("nodes", [])
    ]
    pipes = [
        {
            "id": p["id"],
            "from_": p["from"],
            "to_": p["to"],
            "has_valve": p["has_valve"],
            "valve_closed": p["valve_closed"],
            "name": p.get("name"),
        }
        for p in state.get("pipes", [])
    ]
    return {
        "nodes": nodes,
        "pipes": pipes,
        "open_faults": [
            {"event_id": f["event_id"], "fault_pipe_id": f["fault_pipe_id"]}
            for f in state.get("open_faults", [])
        ],
        "supplied_sink_ids": list(state.get("supplied_sink_ids", [])),
    }


def serialize_snapshot_row(row: dict) -> dict:
    return {
        "id": row["id"],
        "op_type": row["op_type"],
        "network_version": row["network_version"],
        "event_id": row["event_id"],
        "state": serialize_frozen_state(row["state"]),
        "created_at": row["created_at"],
    }


def serialize_snapshot_diff(
    base_row: dict | None, target_row: dict, diff: dict
) -> dict:
    def _pipe(p: dict) -> dict:
        return {
            "id": p["id"],
            "from_": p["from"],
            "to_": p["to"],
            "has_valve": p["has_valve"],
            "valve_closed": p["valve_closed"],
            "name": p.get("name"),
        }

    def _node(n: dict) -> dict:
        return {
            "id": n["id"],
            "type": n["type"],
            "name": n.get("name"),
            "running": n["running"],
        }

    return {
        "base_snapshot_id": base_row["id"] if base_row else None,
        "target_snapshot_id": target_row["id"],
        "base_network_version": (
            base_row["network_version"] if base_row else None
        ),
        "target_network_version": target_row["network_version"],
        "nodes": {
            "added": [_node(n) for n in diff["nodes"]["added"]],
            "removed": [_node(n) for n in diff["nodes"]["removed"]],
        },
        "pipes": {
            "added": [_pipe(p) for p in diff["pipes"]["added"]],
            "removed": [_pipe(p) for p in diff["pipes"]["removed"]],
        },
        "valve_positions": {"changed": list(diff["valve_positions"]["changed"])},
        "source_states": {"changed": list(diff["source_states"]["changed"])},
        "open_faults": {
            "added": [
                {"event_id": f["event_id"], "fault_pipe_id": f["fault_pipe_id"]}
                for f in diff["open_faults"]["added"]
            ],
            "removed": [
                {"event_id": f["event_id"], "fault_pipe_id": f["fault_pipe_id"]}
                for f in diff["open_faults"]["removed"]
            ],
        },
        "supplied_sinks": {
            "added": list(diff["supplied_sinks"]["added"]),
            "removed": list(diff["supplied_sinks"]["removed"]),
        },
    }
