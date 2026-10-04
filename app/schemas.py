"""HTTP 请求 / 响应模型。

节点类型：
- source  水源
- sink    用水点
- junction 连接点

编号（id）为字符串，便于保留 "N01" 这类业务编号；排序时按字符串字典序。
"""
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator

NodeType = Literal["source", "sink", "junction"]


class NodeIn(BaseModel):
    id: str = Field(min_length=1, description="节点编号，唯一")
    type: NodeType
    name: str | None = Field(default=None, description="可选名称")


class PipeIn(BaseModel):
    id: str = Field(min_length=1, description="管段编号，唯一")
    # JSON 字段为 from/to（from 是 Python 关键字），Python 属性名为 from_node/to_node
    from_node: str = Field(min_length=1, validation_alias="from")
    to_node: str = Field(min_length=1, validation_alias="to")
    has_valve: bool = Field(default=False, description="该管段是否设有阀门")
    # 阀门初始状态：默认开启（未关闭）。非阀门管段传 true 会在导入时被拒绝。
    valve_closed: bool = False
    name: str | None = None

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ImportIn(BaseModel):
    nodes: list[NodeIn]
    pipes: list[PipeIn]


class PlanIn(BaseModel):
    """故障隔离预演 / 提交入参。"""

    fault_pipe_id: str = Field(min_length=1, description="故障管段编号")
    close_pipe_ids: list[str] = Field(
        default_factory=list, description="拟关闭的阀门管段编号列表"
    )
    version: int = Field(ge=0, description="读取管网时的版本号，空网为 0")


class RecommendIn(BaseModel):
    """关阀建议入参：只知道故障管段编号与已读版本。"""

    fault_pipe_id: str = Field(min_length=1, description="故障管段编号")
    version: int = Field(ge=0, description="读取管网时的版本号，空网为 0")


class SourceTargetIn(BaseModel):
    """单个水源的目标运行状态。"""

    source_id: str = Field(min_length=1, description="水源节点编号")
    running: bool = Field(description="目标运行状态：true 恢复运行，false 停运")

    model_config = {"extra": "forbid"}


class SourceSwitchIn(BaseModel):
    """水源状态切换预演 / 确认入参：一批水源及各自目标运行状态 + 已读版本。"""

    targets: list[SourceTargetIn] = Field(
        min_length=1, description="水源编号与目标运行状态列表（同批编号不得重复）"
    )
    version: int = Field(ge=0, description="读取管网时的版本号，空网为 0")


class DeliverySourceIn(BaseModel):
    """单个水源的供水上限（非负整数升/小时；停运水源不供水，仍须列出）。"""

    source_id: str = Field(min_length=1, description="水源节点编号")
    # 流量均为非负整数：StrictInt 拒绝 true/false 等非整数类型（类型错误整体拒绝）
    capacity: StrictInt = Field(ge=0, description="供水上限（非负整数，升/小时）")

    model_config = {"extra": "forbid"}


class DeliveryPipeIn(BaseModel):
    """单条管段的双向共用输水上限（两方向合计不得超过该值）。"""

    pipe_id: str = Field(min_length=1, description="管段编号")
    capacity: StrictInt = Field(
        ge=0, description="双向共用输水上限（非负整数，升/小时）"
    )

    model_config = {"extra": "forbid"}


class DeliveryDemandIn(BaseModel):
    """单个用水点的最低流量需求（未列出的用水点不消耗流量）。"""

    sink_id: str = Field(min_length=1, description="用水点节点编号")
    demand: StrictInt = Field(ge=0, description="最低流量需求（非负整数，升/小时）")

    model_config = {"extra": "forbid"}


class DeliveryEvaluateIn(BaseModel):
    """流量核算入参：已读版本 + 每个水源供水上限 + 每条管段双向共用上限 + 用水点需求。

    水源与管段编号须完整覆盖当前管网（不多不少、同类不重复）；
    用水点编号须存在且为 sink；需求列表非空（允许单点需求为 0）。
    """

    version: StrictInt = Field(ge=0, description="读取管网时的版本号，空网为 0")
    sources: list[DeliverySourceIn] = Field(
        description="每个水源的供水上限（须覆盖当前管网全部水源；空网为空列表）"
    )
    pipes: list[DeliveryPipeIn] = Field(
        description="每条管段的双向共用输水上限（须覆盖全部管段；空网为空列表）"
    )
    demands: list[DeliveryDemandIn] = Field(
        description="非空用水点需求列表（未列需求的用水点不消耗流量）；"
        "为空时由领域校验统一返回 DELIVERY_DEMANDS_EMPTY"
    )


class DeliveryAllocationDemandIn(BaseModel):
    """保障等级分档交付中的单个用水点需求（未列出的用水点按零需求、不参与分配）。"""

    sink_id: str = Field(min_length=1, description="用水点节点编号")
    demand: StrictInt = Field(ge=0, description="需求流量（非负整数，升/小时；交付不超过需求）")
    # 保障等级：正整数，数字越小越优先（StrictInt 拒绝布尔/小数）
    level: StrictInt = Field(ge=1, description="保障等级（正整数，数字越小越优先）")

    model_config = {"extra": "forbid"}


class DeliveryAllocateIn(BaseModel):
    """保障等级分档交付入参。

    水源与管段编号须完整覆盖当前管网（不多不少、同类不重复）；
    用水点编号须存在且为 sink、同批不重复；需求列表非空；
    等级为正整数，数字越小越优先。先按等级依次最大化该等级交付总量，
    再在各等级总量最优条件下按（等级, 用水点编号）逐点最大化交付。
    """

    version: StrictInt = Field(ge=0, description="读取管网时的版本号，空网为 0")
    sources: list[DeliverySourceIn] = Field(
        description="每个水源的供水上限（须覆盖当前管网全部水源；空网为空列表）"
    )
    pipes: list[DeliveryPipeIn] = Field(
        description="每条管段的双向共用输水上限（须覆盖全部管段；空网为空列表）"
    )
    demands: list[DeliveryAllocationDemandIn] = Field(
        description="非空用水点需求列表（sink_id/demand/level）；"
        "未列需求的用水点按零需求、不参与分配"
    )


class BypassPipeIn(BaseModel):
    """临时旁通管规格：两个现有节点 + 双向共用容量（仅在内存中接入评估，不写入管网）。

    允许与现有管段并联（两端之间已存在管段时视为新增一条并联通道）；
    两端必须存在且互不相同。
    """

    # JSON 字段为 from/to（与管段导入一致），Python 属性名为 from_node/to_node
    from_node: str = Field(min_length=1, validation_alias="from", description="旁通端点 A（现有节点编号）")
    to_node: str = Field(min_length=1, validation_alias="to", description="旁通端点 B（现有节点编号，与 A 不同）")
    capacity: StrictInt = Field(
        ge=0, description="旁通管双向共用容量（非负整数，升/小时）"
    )

    model_config = {"populate_by_name": True, "extra": "forbid"}


class DeliveryBypassIn(BaseModel):
    """临时旁通管评估入参：已读版本 + 旁通规格 + 覆盖全网的水源/管段容量
    + 带保障等级的用水需求（只读）。

    水源/管段/用水点编号规则与 /api/delivery/allocate 完全一致；
    旁通端点须为两个不同的现有节点；版本过期不拒绝，仅 version_match=false。
    """

    version: StrictInt = Field(ge=0, description="读取管网时的版本号，空网为 0")
    bypass: BypassPipeIn = Field(
        description="临时旁通管：两个现有节点（from/to）+ 双向共用容量"
    )
    sources: list[DeliverySourceIn] = Field(
        description="每个水源的供水上限（须覆盖当前管网全部水源；空网为空列表）"
    )
    pipes: list[DeliveryPipeIn] = Field(
        description="每条管段的双向共用输水上限（须覆盖全部管段；空网为空列表）"
    )
    demands: list[DeliveryAllocationDemandIn] = Field(
        description="非空用水点需求列表（sink_id/demand/level）；"
        "未列需求的用水点按零需求、不参与分配"
    )


# ---------------------------------------------------------------- 响应模型


class NodeOut(BaseModel):
    id: str
    type: NodeType
    name: str | None = None
    # 仅水源节点有意义：true=运行中，false=已停运；非水源节点恒为 true
    running: bool = True


class PipeOut(BaseModel):
    id: str
    # 出参 JSON 键名为 from/to；Python 属性使用 from_/to_
    from_: str = Field(serialization_alias="from")
    to_: str = Field(serialization_alias="to")
    has_valve: bool
    valve_closed: bool | None = None
    name: str | None = None


class ImportOut(BaseModel):
    version: int
    node_count: int
    pipe_count: int
    # 整网导入在同一事务内冻结的状态快照编号（增量字段，旧客户端可忽略）
    snapshot_id: int | None = None


class NetworkOut(BaseModel):
    version: int
    updated_at: datetime | None = None
    nodes: list[NodeOut]
    pipes: list[PipeOut]


class IssueOut(BaseModel):
    code: str
    message: str
    pipe_id: str | None = None


class PreviewOut(BaseModel):
    version: int
    version_match: bool
    fault_pipe_id: str
    close_pipe_ids: list[str]
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[IssueOut]


class RecommendReasonOut(BaseModel):
    code: str
    message: str
    pipe_id: str | None = None


class RecommendOut(BaseModel):
    version: int
    version_match: bool
    fault_pipe_id: str
    feasible: bool
    recommended_close_pipe_ids: list[str]
    affected_sink_ids: list[str]
    reason: RecommendReasonOut | None = None


class SubmitOut(BaseModel):
    version: int
    fault_pipe_id: str
    closed_pipe_ids: list[str]
    affected_sink_ids: list[str]
    event_id: int
    snapshot_id: int | None = None


class RestoreIn(BaseModel):
    """复供预演 / 确认入参：按隔离事件编号与当前管网版本操作。"""

    version: int = Field(ge=0, description="读取管网时的版本号，空网为 0")


class ReopenBlockOut(BaseModel):
    code: str
    message: str
    pipe_id: str | None = None
    reachable_endpoints: list[str] = []


class RestorePreviewOut(BaseModel):
    version: int
    version_match: bool
    event_id: int
    status: str
    fault_pipe_id: str
    closed_pipe_ids: list[str]
    safe: bool
    restored_sink_ids: list[str]
    block_reasons: list[ReopenBlockOut]


class RestoreOut(BaseModel):
    version: int
    event_id: int
    status: str
    fault_pipe_id: str
    reopened_pipe_ids: list[str]
    restored_sink_ids: list[str]
    snapshot_id: int | None = None


class SourceTargetOut(BaseModel):
    source_id: str
    running: bool


class SourceSwitchPreviewOut(BaseModel):
    version: int
    version_match: bool
    changed: bool
    safe: bool
    targets: list[SourceTargetOut]
    affected_sink_ids: list[str]
    restored_sink_ids: list[str]
    block_reasons: list[ReopenBlockOut]
    issues: list[IssueOut]


class SourceSwitchOut(BaseModel):
    version: int
    changed: bool
    targets: list[SourceTargetOut]
    affected_sink_ids: list[str]
    restored_sink_ids: list[str]
    # 仅在确有状态变化时产生快照；全部目标状态已满足（changed=false）时为 null
    snapshot_id: int | None = None


class DeliveryEvaluateOut(BaseModel):
    """流量核算只读结果：当前版本/版本是否匹配/需求总量/最大可交付总量/能否全部满足。"""

    version: int
    version_match: bool
    total_demand: int
    max_delivered_total: int
    feasible: bool


class SinkDeliveryOut(BaseModel):
    """单个用水点的分档交付：需求、实际交付与缺口。"""

    sink_id: str
    level: int
    demand: int
    delivered: int
    shortfall: int
    satisfied: bool


class LevelDeliveryOut(BaseModel):
    """单个保障等级的交付汇总（按等级数值升序）。"""

    level: int
    demand: int
    delivered: int
    shortfall: int
    all_satisfied: bool


class DeliveryAllocateOut(BaseModel):
    """保障等级分档交付只读结果。

    先按等级依次最大化该等级交付总量，再在各等级总量最优条件下按
    （等级, 用水点编号）逐点最大化交付；deliveries 按该顺序排列，
    各点交付由一组同时可行的整数流量实现。
    """

    version: int
    version_match: bool
    total_demand: int
    total_delivered: int
    all_satisfied: bool
    levels: list[LevelDeliveryOut]
    deliveries: list[SinkDeliveryOut]


class BypassSpecOut(BaseModel):
    """回显本次评估的临时旁通管规格（出参 JSON 键名为 from/to）。"""

    from_: str = Field(serialization_alias="from")
    to_: str = Field(serialization_alias="to")
    capacity: int


class BypassAllocationOut(BaseModel):
    """某一状态（基线 / 接入旁通后）下的分档交付结果。"""

    total_demand: int
    total_delivered: int
    all_satisfied: bool
    levels: list[LevelDeliveryOut]
    deliveries: list[SinkDeliveryOut]


class BypassLevelDeltaOut(BaseModel):
    """单个保障等级接入旁通前后的交付差值（按等级数值升序）。"""

    level: int
    baseline_delivered: int
    bypass_delivered: int
    delivered_delta: int


class BypassSinkDeltaOut(BaseModel):
    """单个用水点接入旁通前后的交付差值（按（等级, 用水点编号）排序）。"""

    sink_id: str
    level: int
    baseline_delivered: int
    bypass_delivered: int
    delivered_delta: int


class BypassDeltasOut(BaseModel):
    """接入旁通前后的交付差值汇总（总量 / 各等级 / 各用水点）。"""

    total_delivered_delta: int
    levels: list[BypassLevelDeltaOut]
    deliveries: list[BypassSinkDeltaOut]


class DeliveryBypassOut(BaseModel):
    """临时旁通管评估只读结果。

    baseline 恒按当前状态给出；safe=false 时存在重新进水风险
    （block_reasons 给出故障管段编号与危险端点），判为不可用：
    with_bypass 与 deltas 恒为 null，不输出旁通交付量。
    版本过期不拒绝，仅 version_match=false。
    """

    version: int
    version_match: bool
    bypass: BypassSpecOut
    safe: bool
    block_reasons: list[ReopenBlockOut]
    baseline: BypassAllocationOut
    with_bypass: BypassAllocationOut | None
    deltas: BypassDeltasOut | None


class ReconcileSourceIn(BaseModel):
    """单个水源的实际注入量（非负整数升/小时；停运水源注入必须为 0，仍须列出）。"""

    source_id: str = Field(min_length=1, description="水源节点编号")
    injection: StrictInt = Field(ge=0, description="实际注入量（非负整数，升/小时）")

    model_config = {"extra": "forbid"}


class ReconcileSinkIn(BaseModel):
    """单个用水点的实际取用量（非负整数升/小时；未取用按 0 申报，仍须列出）。"""

    sink_id: str = Field(min_length=1, description="用水点节点编号")
    withdrawal: StrictInt = Field(ge=0, description="实际取用量（非负整数，升/小时）")

    model_config = {"extra": "forbid"}


class ReconcilePipeIn(BaseModel):
    """单条管段的双向共用容量与可为空的带符号读数。

    读数以管段 from→to 为正（升/小时）；null 表示该管段缺报，由服务推断
    全部可行解中的最小/最大流量。
    """

    pipe_id: str = Field(min_length=1, description="管段编号")
    capacity: StrictInt = Field(
        ge=0, description="双向共用容量（非负整数，升/小时）"
    )
    # 带符号读数允许负数（to→from 方向）；StrictInt 拒绝布尔/小数（非整数整体拒绝）
    reading: StrictInt | None = Field(
        default=None,
        description="带符号流量计读数（from→to 为正）；null/缺省表示缺报",
    )

    model_config = {"extra": "forbid"}


class MeterReconcileIn(BaseModel):
    """读数核对入参：已读版本 + 每个水源实际注入量 + 每个用水点实际取用量
    + 覆盖全部管段的双向共用容量与可为空带符号读数。

    三类编号均须完整覆盖当前管网（不多不少）、同类不得重复；
    注入、取用、容量均为非负整数升/小时；读数为带符号整数或 null。
    """

    version: StrictInt = Field(ge=0, description="读取管网时的版本号，空网为 0")
    sources: list[ReconcileSourceIn] = Field(
        description="每个水源的实际注入量（须覆盖全部水源；空网为空列表）"
    )
    sinks: list[ReconcileSinkIn] = Field(
        description="每个用水点的实际取用量（须覆盖全部用水点；空网为空列表）"
    )
    pipes: list[ReconcilePipeIn] = Field(
        description="每条管段的双向共用容量与可为空带符号读数（须覆盖全部管段）"
    )


# ---------------------------------------------------------------- 调度演练


class DrillIsolationStepIn(BaseModel):
    """演练单步：隔离（故障管段 + 拟关阀门，按提交口径校验）。"""

    type: Literal["isolation"]
    fault_pipe_id: str = Field(min_length=1, description="故障管段编号")
    close_pipe_ids: list[str] = Field(
        default_factory=list, description="拟关闭的阀门管段编号列表"
    )

    model_config = {"extra": "forbid"}


class DrillRestoreStepIn(BaseModel):
    """演练单步：复供（引用现存待修复事件，或本次演练先前隔离产生的临时事件）。

    临时事件引用形如 "T<步骤序号>"（序号从 1 起）：T3 即第 3 步隔离产生的
    临时事件；event_id 与 temp_event_ref 必须且只能给出一个。
    """

    type: Literal["restore"]
    event_id: int | None = Field(default=None, description="现存隔离事件编号")
    temp_event_ref: str | None = Field(
        default=None, min_length=1,
        description="本次演练先前隔离步骤产生的临时事件引用（形如 T<步骤序号>）",
    )

    model_config = {"extra": "forbid"}

    @model_validator(mode="after")
    def _exactly_one_ref(self):
        if (self.event_id is None) == (self.temp_event_ref is None):
            raise ValueError(
                "复供步骤必须且只能给出 event_id 或 temp_event_ref 之一"
            )
        return self


class DrillSourceSwitchStepIn(BaseModel):
    """演练单步：水源切换（一批水源的目标运行状态）。"""

    type: Literal["source_switch"]
    targets: list[SourceTargetIn] = Field(
        min_length=1, description="水源编号与目标运行状态列表（同批编号不得重复）"
    )

    model_config = {"extra": "forbid"}


# 按 type 字段判别步骤类型；结构非法（未知类型、缺字段、复供引用缺失等）整体 422
DrillStepIn = Annotated[
    DrillIsolationStepIn | DrillRestoreStepIn | DrillSourceSwitchStepIn,
    Field(discriminator="type"),
]


class DrillIn(BaseModel):
    """调度演练入参：已读版本 + 一串有序的隔离/复供/水源切换步骤（只读）。

    版本必须等于当前管网版本，否则 409 VERSION_CONFLICT 拒绝演练；
    空步骤列表只回显当前状态汇总。
    """

    version: StrictInt = Field(ge=0, description="读取管网时的版本号，空网为 0")
    steps: list[DrillStepIn] = Field(
        default_factory=list,
        description="有序操作步骤：隔离 / 复供 / 水源切换（空列表只回显当前状态）",
    )


class ReconcileReasonOut(BaseModel):
    code: str
    message: str
    pipe_id: str | None = None
    node_id: str | None = None


class MeterReadingRangeOut(BaseModel):
    """单条缺报管段在全部可行解中的流量范围（带符号，from→to 为正）。"""

    pipe_id: str
    # 出参保留管段方向，便于调度员核对符号含义
    from_: str = Field(serialization_alias="from")
    to_: str = Field(serialization_alias="to")
    min_flow: int
    max_flow: int
    # min_flow == max_flow：缺报读数可唯一确定
    determinable: bool


class MeterReconcileOut(BaseModel):
    """读数核对结果：当前版本/版本是否匹配/是否一致/缺报管段可行流量范围。

    consistent=false（无可行解或硬约束被违反）时 readings 恒为空列表，
    原因见 reasons；版本过期不拒绝，仍按当前状态计算，仅 version_match=false。
    """

    version: int
    version_match: bool
    consistent: bool
    readings: list[MeterReadingRangeOut]
    reasons: list[ReconcileReasonOut]


class EventOut(BaseModel):
    id: int
    version_before: int
    version_after: int
    fault_pipe_id: str
    closed_pipe_ids: list[str]
    affected_sink_ids: list[str]
    status: str = "open"
    restored_at: datetime | None = None
    created_at: datetime


# ---------------------------------------------------------------- 状态快照


class SnapshotNodeOut(BaseModel):
    id: str
    type: NodeType
    name: str | None = None
    running: bool = True


class SnapshotPipeOut(BaseModel):
    id: str
    from_: str = Field(serialization_alias="from")
    to_: str = Field(serialization_alias="to")
    has_valve: bool
    valve_closed: bool
    name: str | None = None


class SnapshotFaultOut(BaseModel):
    event_id: int
    fault_pipe_id: str


class SnapshotStateOut(BaseModel):
    """操作后冻结的完整供水状态（历史只读，绝不以后续状态回填）。"""

    nodes: list[SnapshotNodeOut]
    pipes: list[SnapshotPipeOut]
    open_faults: list[SnapshotFaultOut]
    supplied_sink_ids: list[str]


class SnapshotOut(BaseModel):
    """单条状态快照：编号 + 操作类型 + 当时管网版本 + 关联事件编号 + 冻结状态。"""

    id: int
    op_type: str
    network_version: int
    event_id: int | None = None
    state: SnapshotStateOut
    created_at: datetime


class SnapshotMetaOut(BaseModel):
    """快照列表项：与详情一致（列表与详情均返回冻结状态，便于调度员直接回看）。"""

    id: int
    op_type: str
    network_version: int
    event_id: int | None = None
    state: SnapshotStateOut
    created_at: datetime


class SnapshotNodeChangeOut(BaseModel):
    id: str
    type: NodeType
    name: str | None = None
    running: bool = True


class SnapshotPipeChangeOut(BaseModel):
    id: str
    from_: str = Field(serialization_alias="from")
    to_: str = Field(serialization_alias="to")
    has_valve: bool
    valve_closed: bool
    name: str | None = None


class SnapshotNodeDiffOut(BaseModel):
    added: list[SnapshotNodeChangeOut]
    removed: list[SnapshotNodeChangeOut]


class SnapshotPipeDiffOut(BaseModel):
    added: list[SnapshotPipeChangeOut]
    removed: list[SnapshotPipeChangeOut]


class ValvePositionChangeOut(BaseModel):
    pipe_id: str
    valve_closed_before: bool
    valve_closed_after: bool


class ValvePositionDiffOut(BaseModel):
    changed: list[ValvePositionChangeOut]


class SourceStateChangeOut(BaseModel):
    source_id: str
    running_before: bool
    running_after: bool


class SourceStateDiffOut(BaseModel):
    changed: list[SourceStateChangeOut]


class SnapshotFaultDiffOut(BaseModel):
    added: list[SnapshotFaultOut]
    removed: list[SnapshotFaultOut]


class SuppliedSinksDiffOut(BaseModel):
    added: list[str]
    removed: list[str]


class SnapshotDiffOut(BaseModel):
    """相邻快照的六类净变化（节点/管段/阀位/水源状态/待修复故障/供水点）。"""

    base_snapshot_id: int | None
    target_snapshot_id: int
    base_network_version: int | None
    target_network_version: int
    nodes: SnapshotNodeDiffOut
    pipes: SnapshotPipeDiffOut
    valve_positions: ValvePositionDiffOut
    source_states: SourceStateDiffOut
    open_faults: SnapshotFaultDiffOut
    supplied_sinks: SuppliedSinksDiffOut


# ---------------------------------------------------------------- 快照摘要链核验


class AuditVerifyIn(BaseModel):
    """调度审计员只读核验区间的入参。

    pre_start_digest / end_digest 为审计员**事先离线保存**的摘要：
    起点快照在区间起点之前的前一条摘要（核验首条入链快照时传创世摘要）、
    终点快照自身摘要。
    """

    start_id: int = Field(ge=1, description="区间起始快照编号（含）")
    end_id: int = Field(ge=1, description="区间终止快照编号（含，不得小于 start_id）")
    pre_start_digest: str = Field(
        min_length=64,
        max_length=64,
        description="事先保存的起点前摘要（64 位十六进制；首条入链快照传创世摘要）",
    )
    end_digest: str = Field(
        min_length=64,
        max_length=64,
        description="事先保存的终点快照摘要（64 位十六进制）",
    )

    model_config = {"extra": "forbid"}


class AuditDigestItemOut(BaseModel):
    snapshot_id: int
    digest: str
    prev_digest: str


class AuditFailureOut(BaseModel):
    kind: str
    snapshot_id: int | None = None
    previous_snapshot_id: int | None = None
    expected_digest: str | None = None
    actual_digest: str | None = None
    expected_prev_digest: str | None = None
    actual_prev_digest: str | None = None
    message: str


class AuditVerifyOut(BaseModel):
    """区间核验结果。

    verdict：
    - ok：区间内部链自洽，且起点前/终点摘要均与事先保存值相符；
    - boundary_mismatch：内部链自洽，仅起点前或终点边界摘要不符；
    - content_corrupted：首个可定位的内容损坏（冻结字段被事后改写）；
    - chain_broken：首个可定位的链路断裂（前一条摘要指针不符或无链记录）。
    """

    ok: bool
    verdict: str
    start_snapshot_id: int
    end_snapshot_id: int
    genesis_digest: str
    pre_start_digest: str
    end_digest: str
    expected_pre_start_digest: str | None
    expected_end_digest: str | None
    start_boundary_ok: bool | None
    end_boundary_ok: bool | None
    failure: AuditFailureOut | None
    digests: list[AuditDigestItemOut]


class AuditChainInfoOut(BaseModel):
    """摘要链基本信息：创世摘要与建链起点（供审计员选取核验边界）。"""

    genesis_digest: str
    chain_tag: str
    anchor_snapshot_id: int | None
    anchor_kind: str | None
    anchored_at: datetime | None
    snapshot_count: int
    chained_count: int
    earliest_snapshot_id: int | None
    latest_snapshot_id: int | None


# ---------------------------------------------------------------- 调度演练


class DrillStepErrorOut(BaseModel):
    """演练单步失败原因：原因码 + 结构问题明细 / 重新进水风险明细。"""

    code: str
    message: str
    issues: list[IssueOut] = []
    block_reasons: list[ReopenBlockOut] = []


class DrillStepOut(BaseModel):
    """演练单步结果（按步骤类型回显对应字段，其余为 null）。

    ok=true 时 affected_sink_ids / restored_sink_ids 为相对上一步结束状态的
    新增失供 / 恢复供水用水点；ok=false 时 error 给出失败原因。
    """

    index: int
    type: str
    ok: bool
    # 隔离步骤：故障管段、拟关阀门、产生的临时事件引用
    fault_pipe_id: str | None = None
    close_pipe_ids: list[str] | None = None
    temp_event_ref: str | None = None
    # 复供步骤：引用的事件（现存编号或临时引用）与重开的阀门
    event_id: int | None = None
    reopened_pipe_ids: list[str] | None = None
    # 水源切换步骤：目标状态与是否有实际变化
    targets: list[SourceTargetOut] | None = None
    changed: bool | None = None
    # 公共：相对上一步的新增失供 / 恢复供水用水点
    affected_sink_ids: list[str] = []
    restored_sink_ids: list[str] = []
    error: DrillStepErrorOut | None = None


class DrillFinalSourceOut(BaseModel):
    source_id: str
    running: bool


class DrillFinalValveOut(BaseModel):
    pipe_id: str
    valve_closed: bool


class DrillOpenFaultOut(BaseModel):
    """最终仍待修复的故障：现存事件给 event_id，演练临时事件给 temp_event_ref。"""

    event_id: int | None = None
    temp_event_ref: str | None = None
    fault_pipe_id: str


class DrillFinalOut(BaseModel):
    """全部步骤成功后的最终模拟状态（推演结果，未真实生效）。"""

    sources: list[DrillFinalSourceOut]
    valves: list[DrillFinalValveOut]
    open_faults: list[DrillOpenFaultOut]
    supplied_sink_ids: list[str]


class DrillOut(BaseModel):
    """调度演练只读结果。

    ok=false 时 failed_step_index 为首个失败步骤序号（从 1 起），steps 只包含
    到该步骤为止的结果，final 为 null；版本过期整体 409 拒绝演练。
    """

    version: int
    ok: bool
    failed_step_index: int | None
    steps: list[DrillStepOut]
    final: DrillFinalOut | None
