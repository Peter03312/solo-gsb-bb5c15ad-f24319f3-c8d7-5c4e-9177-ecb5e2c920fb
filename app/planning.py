"""管网故障隔离与复供的纯领域逻辑（不依赖数据库 / Web 框架）。

规则概述：
- 管网为无向图，节点分为水源(source)/用水点(sink)/连接点(junction)。
- 已关闭阀门的管段视为不可通行。
- 其他“待修复(open)”隔离事件的故障管段同样视为不可通行（故障仍在，不能通水）。
- 方案图 = 当前管网移除故障管段，再移除/关闭所有“拟关闭管段”，
  同时已关闭阀门、其他待修复故障管段继续保持阻断。
- 只有故障管段**两端**在方案图中都无法从任一水源到达，方案才可隔离(isolatable)。
- 失供用水点 = 方案前由任一水源可达、方案后不可达的 sink，按编号排序。

复供（restore）规则：
- 复供图 = 当前管网重新打开本事件关闭的阀门（从“仍关闭阀门”集合中剔除），
  再继续移除“其他待修复事件”的故障管段。
- 仅当重开后，其余每条待修复故障管段的**两端**都仍无法从水源到达，才允许复供。
- 预计恢复供水用水点 = 复供前不可达、复供图中可达的 sink，按编号排序。

关阀建议（recommend）规则：
- 只读计算：为故障管段给出一套可直接提交的关阀方案（拟关阀门 + 预计失供用水点）。
- 只能从尚未关闭的有阀管段中选取（不含故障管段本身），方案须使故障管段两端
  都与所有水源断开；优选顺序：新增失供用水点数最少 → 本次关阀数最少 →
  关阀编号序列字典序最小。

水源状态（source running）规则：
- 每个水源节点有运行(running)/停运(stopped)状态；整网导入时水源初始视为运行。
- 可达性计算只从“运行中”的水源出发（停运水源不再向管网供水）；
  读网、隔离预演/提交、关阀建议、复供预演/确认全部按运行水源计算。
- 水源切换预演为只读：返回当前版本、版本是否匹配、切换后新增失供与恢复供水的
  用水点，以及每条待修复故障管段的重新进水风险；仅水源节点可切换，同批编号
  不得重复；全部目标状态已满足（无实际变化）时不推进版本；停运可导致全网无
  运行水源（此时所有用水点失供，不额外报错）。

流量核算（delivery）规则（只读，不改阀位/事件/版本/快照）：
- 调度员携带已读版本、每个水源的供水上限、每条管段的双向共用输水上限与
  非空用水点需求（流量均为非负整数升/小时），核算现有阀位与故障状态下
  能否满足一组用水点的最低流量。
- 水源与管段编号必须**完整覆盖当前管网**（不多不少）、同类不得重复；
  用水点编号必须存在（仅列出本次有需求的点，未列需求的点不消耗流量）。
- 停运水源不供水（容量视为 0）；已关闭阀门与待修复故障管段不可通行
  （容量视为 0，与可达性口径完全一致）。
- 管段为双向共用输水上限：两方向净流量合计（即净流量绝对值）不得超过上限，
  按无向边容量建模；最大可交付总量 = 超级源→各水源→管网→各需求点→超级汇
  的整数最大流（对总需求封顶）。全部为整数容量，最大流为整数。

保障等级分档交付（delivery allocate）规则（只读，语义独立于上述总量核算）：
- 每个申报用水点除非负整数需求外还带**正整数保障等级**（数字越小越优先），
  未列用水点按零需求、不参与分配；编号/覆盖/重复/类型校验与总量核算完全一致。
- 优化目标（字典序）：先按等级从小到大依次最大化该等级交付总量，
  再在各等级总量均为最优的条件下，按（等级, 用水点编号字典序）逐点最大化
  交付量；各点交付不得超过需求；最终各点交付由**一组同时可行的整数流量**实现。
- 实现用“有下界整数环流 + 残量环流补偿增广”：超级源汇以返回弧闭合为环流，
  阶段 A 逐级把已定等级汇边锁定为 low=high 后最大化本级汇边（更高等级暂不
  入网，避免被未来流量挤占）；阶段 B 锁定全部等级总量并逐点锁定已最优汇水
  弧，在残量网上做弧终点→起点的最大补偿增广；锁定弧不开放反向残量，已锁定
  的更优目标不可能被后续增广挤占（与读数核对缺报范围同一项残量环流技术）。

读数核对（meter reconcile）规则（只读，不改阀位/事件/版本/快照）：
- 调度员携带已读版本、每个水源的实际注入量、每个用水点的实际取用量、每条管段的
  双向共用容量与可为 null 的带符号读数（以管段 from→to 为正，升/小时），核对
  当前水源运行状态、关阀与待修复故障下读数是否自洽，并为缺报（null）管段求全部
  可行解中的最小/最大流量（最小=最大时可确定）。
- 硬约束：停运水源注入必须为 0；阻断管段（已关阀门/待修复故障）流量必须为 0；
  每个节点 流入 + 注入 = 流出 + 取用；|管段流量| <= 双向共用容量。
- 有向弧下界可行流（Dinic + 超级源/汇）判定可行并求一组可行整数流；缺报弧的
  流量范围在剔除该弧的残量网络上，以两次最大流求可增/可减量。无可行解时
  consistent=false 且不输出补值。
- 注入、取用、容量均为非负整数；编号覆盖不足/重复/未知节点/非整数或负容量
  整体拒绝（422）；版本过期仍按当前状态计算，仅标记 version_match=false。

调度演练（drill）规则（只读，不改版本/阀位/水源状态/事件/快照）：
- 调度员携带已读版本与一串有序的隔离、复供、水源切换步骤；以当前状态为起点，
  在状态副本上逐步套用与既有提交接口完全一致的校验（隔离 for_submit 口径：
  故障两端须均可隔离、已关闭阀门不得重复关闭；复供与恢复水源不得使任一待修复
  故障端点重新进水）。
- 复供步骤可引用现存待修复事件（event_id），或本次演练先前隔离步骤产生的
  临时事件（temp_event_ref，形如 "T<步骤序号>"，序号从 1 起）。
- 每步的新增失供/恢复供水用水点均相对上一步结束状态计算。
- 任一步非法或不安全即停止：记录首个失败步骤与原因，后续步骤不再演练；
  全部成功才汇总最终运行水源、阀位、待修复故障与通水用水点。

临时旁通管评估（bypass）规则（只读，不改阀位/事件/版本/快照）：
- 抢修期间调度员携带已读版本、两个现有节点（旁通端点）、旁通管双向共用容量、
  覆盖当前管网的水源/管段容量与带保障等级的用水需求，评估一条临时旁通管的
  供水效果；旁通允许与现有管段并联，且只在内存副本中接入，绝不写入管网。
- 先按当前阀位、运行水源与待修复故障核算基线分档交付；再在内存副本中接入
  旁通管：若接入后任一待修复故障管段的任一端从运行水源可达（重新进水风险，
  与复供/水源切换同口径的拓扑判定，与旁通容量大小无关），判为不可用，
  返回故障编号与危险端点，不输出旁通交付量。
- 安全时按与分档交付完全一致的规则分别核算基线与接入旁通后的各等级、
  各用水点交付量并给出差值；版本过期仍按当前状态核算，仅标记
  version_match=false；端点不存在或相同、容量或编号集合非法时整体拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque
from itertools import combinations
import sys

# 问题代码 ---------------------------------------------------------------

# 阻断类问题：出现任一即不能隔离、提交时拒绝（422）
FAULT_PIPE_NOT_FOUND = "FAULT_PIPE_NOT_FOUND"
CLOSE_PIPE_NOT_FOUND = "CLOSE_PIPE_NOT_FOUND"
CLOSE_PIPE_NO_VALVE = "CLOSE_PIPE_NO_VALVE"
CLOSE_PIPE_DUPLICATE = "CLOSE_PIPE_DUPLICATE"
CLOSE_PIPE_IS_FAULT = "CLOSE_PIPE_IS_FAULT"
# 复供阻断：重开阀门后其他待修复故障管段有一端重新进水
FAULT_REWATER_RISK = "FAULT_REWATER_RISK"
# 关阀建议：关闭全部可用阀门后故障管段仍有端点与水源相通
NO_FEASIBLE_PLAN = "NO_FEASIBLE_PLAN"
# 导入期问题
DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
DUPLICATE_PIPE_ID = "DUPLICATE_PIPE_ID"
PIPE_SELF_LOOP = "PIPE_SELF_LOOP"
PIPE_ENDPOINT_UNKNOWN = "PIPE_ENDPOINT_UNKNOWN"
NON_VALVE_PIPE_CLOSED = "NON_VALVE_PIPE_CLOSED"

# 水源切换问题
SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"          # 编号不存在
SOURCE_NOT_SWITCHABLE = "SOURCE_NOT_SWITCHABLE"  # 节点存在但不是水源，不可切换
SOURCE_TARGET_DUPLICATE = "SOURCE_TARGET_DUPLICATE"  # 同批水源编号重复

# 流量核算请求问题
DELIVERY_SOURCE_NOT_FOUND = "DELIVERY_SOURCE_NOT_FOUND"  # 水源编号在当前管网不存在
DELIVERY_SOURCE_MISSING = "DELIVERY_SOURCE_MISSING"  # 当前管网水源未在请求中列出（未覆盖）
DELIVERY_SOURCE_DUPLICATE = "DELIVERY_SOURCE_DUPLICATE"  # 水源编号重复
DELIVERY_PIPE_NOT_FOUND = "DELIVERY_PIPE_NOT_FOUND"  # 管段编号在当前管网不存在
DELIVERY_PIPE_MISSING = "DELIVERY_PIPE_MISSING"  # 当前管网管段未在请求中列出（未覆盖）
DELIVERY_PIPE_DUPLICATE = "DELIVERY_PIPE_DUPLICATE"  # 管段编号重复
DELIVERY_SINK_NOT_FOUND = "DELIVERY_SINK_NOT_FOUND"  # 用水点编号不存在
DELIVERY_SINK_NOT_SINK = "DELIVERY_SINK_NOT_SINK"  # 节点存在但不是用水点
DELIVERY_SINK_DUPLICATE = "DELIVERY_SINK_DUPLICATE"  # 用水点编号重复
DELIVERY_DEMANDS_EMPTY = "DELIVERY_DEMANDS_EMPTY"  # 用水点需求列表为空

# 临时旁通评估请求问题（结构 / 端点 -> 422 整体拒绝）
BYPASS_ENDPOINT_NOT_FOUND = "BYPASS_ENDPOINT_NOT_FOUND"  # 旁通端点在当前管网不存在
BYPASS_ENDPOINT_SAME = "BYPASS_ENDPOINT_SAME"  # 旁通两端为同一节点（自环）

# 读数核对请求问题（结构 / 编号 / 覆盖 / 重复 -> 422 整体拒绝）
RECONCILE_SOURCE_NOT_FOUND = "RECONCILE_SOURCE_NOT_FOUND"
RECONCILE_SOURCE_MISSING = "RECONCILE_SOURCE_MISSING"
RECONCILE_SOURCE_DUPLICATE = "RECONCILE_SOURCE_DUPLICATE"
RECONCILE_SINK_NOT_FOUND = "RECONCILE_SINK_NOT_FOUND"
RECONCILE_SINK_NOT_SINK = "RECONCILE_SINK_NOT_SINK"
RECONCILE_SINK_MISSING = "RECONCILE_SINK_MISSING"
RECONCILE_SINK_DUPLICATE = "RECONCILE_SINK_DUPLICATE"
RECONCILE_PIPE_NOT_FOUND = "RECONCILE_PIPE_NOT_FOUND"
RECONCILE_PIPE_MISSING = "RECONCILE_PIPE_MISSING"
RECONCILE_PIPE_DUPLICATE = "RECONCILE_PIPE_DUPLICATE"

# 读数核对结果原因（200，consistent=false：当前状态/读数下无可行解或硬约束被违反）
STOPPED_SOURCE_INJECTION = "STOPPED_SOURCE_INJECTION"  # 停运水源申报注入非 0
BLOCKED_PIPE_FLOW = "BLOCKED_PIPE_FLOW"  # 阻断管段（关阀/待修复）读数非 0
READING_OVER_CAPACITY = "READING_OVER_CAPACITY"  # 读数绝对值超过双向共用容量
READINGS_INCONSISTENT = "READINGS_INCONSISTENT"  # 节点平衡/容量/停运阻断下无可行解

# 演练步骤失败原因码（与既有单步提交接口的拒绝码一致）
PLAN_INVALID = "PLAN_INVALID"            # 隔离步骤结构非法（明细在 issues）
NOT_ISOLATABLE = "NOT_ISOLATABLE"        # 隔离步骤无法隔离故障管段
EVENT_NOT_FOUND = "EVENT_NOT_FOUND"      # 复供步骤引用的事件不存在
EVENT_ALREADY_RESTORED = "EVENT_ALREADY_RESTORED"  # 复供步骤引用的事件已复供
RESTORE_UNSAFE = "RESTORE_UNSAFE"        # 复供步骤存在重新进水风险
SOURCE_SWITCH_INVALID = "SOURCE_SWITCH_INVALID"  # 水源切换步骤结构非法
SOURCE_SWITCH_UNSAFE = "SOURCE_SWITCH_UNSAFE"    # 水源切换步骤存在重新进水风险

# 结构性阻断问题（预演会给出，提交据此拒绝）
BLOCKING_ISSUE_CODES = frozenset(
    {
        FAULT_PIPE_NOT_FOUND,
        CLOSE_PIPE_NOT_FOUND,
        CLOSE_PIPE_NO_VALVE,
        CLOSE_PIPE_DUPLICATE,
        CLOSE_PIPE_IS_FAULT,
    }
)


# 快照操作类型
OP_NETWORK_IMPORT = "network_import"    # 整网导入
OP_ISOLATION_SUBMIT = "isolation_submit"  # 隔离提交
OP_RESTORE_CONFIRM = "restore_confirm"  # 复供确认
OP_SOURCE_SWITCH = "source_switch"      # 水源切换
OP_BASELINE = "baseline"                # 旧库上线基线（仅当下状态，不伪造历史）

SNAPSHOT_OP_TYPES = frozenset(
    {
        OP_NETWORK_IMPORT,
        OP_ISOLATION_SUBMIT,
        OP_RESTORE_CONFIRM,
        OP_SOURCE_SWITCH,
        OP_BASELINE,
    }
)


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # source / sink / junction
    name: str | None = None
    # 仅水源节点有意义：True=运行（供水），False=停运。导入的水源初始为运行。
    running: bool = True


@dataclass(frozen=True)
class Pipe:
    id: str
    from_node: str
    to_node: str
    has_valve: bool = False
    valve_closed: bool = False
    name: str | None = None


@dataclass
class Issue:
    code: str
    message: str
    pipe_id: str | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "pipe_id": self.pipe_id}


@dataclass
class PlanResult:
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 方案中实际被阻断的管段（已关 ∪ 拟关 ∪ 待修复故障，故障管段单独移除）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class ReopenBlock:
    """复供阻断原因：某条其他待修复故障管段在重开阀门后有一端重新进水。"""

    fault_pipe_id: str
    reachable_endpoints: list[str]

    def as_dict(self) -> dict:
        return {
            "code": FAULT_REWATER_RISK,
            "message": (
                f"重开阀门后，待修复故障管段 {self.fault_pipe_id} 的 "
                f"{ '、'.join(self.reachable_endpoints) } 端将重新进水，禁止复供"
            ),
            "pipe_id": self.fault_pipe_id,
            "reachable_endpoints": list(self.reachable_endpoints),
        }


@dataclass
class RestoreResult:
    safe: bool
    restored_sink_ids: list[str]
    blocks: list[ReopenBlock] = field(default_factory=list)
    # 复供评估中仍不可通行的管段（其他待修复故障管段 ∪ 仍关闭阀门）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SourceSwitchBlock:
    """水源切换阻断：切换后某条待修复故障管段有一端将重新进水。"""

    fault_pipe_id: str
    reachable_endpoints: list[str]

    def as_dict(self) -> dict:
        return {
            "code": FAULT_REWATER_RISK,
            "message": (
                f"切换后，待修复故障管段 {self.fault_pipe_id} 的 "
                f"{ '、'.join(self.reachable_endpoints) } 端将重新进水，禁止切换水源状态"
            ),
            "pipe_id": self.fault_pipe_id,
            "reachable_endpoints": list(self.reachable_endpoints),
        }


@dataclass
class SourceSwitchResult:
    """水源状态切换预演结果（只读计算，不改任何状态）。

    - changed：是否存在实际状态变化；全部目标状态已满足时为 False，确认不推进版本；
    - affected_sink_ids：切换后“新增失供”的用水点（切换前可达、切换后不可达）；
    - restored_sink_ids：切换后“恢复供水”的用水点（切换前不可达、切换后可达）；
    - blocks：每条待修复故障管段在切换后的重新进水风险（任一端可达即列入）；
    存在风险时 safe=False，确认接口据此拒绝（422 SOURCE_SWITCH_UNSAFE）。
    """

    changed: bool
    safe: bool
    targets: list[tuple[str, bool]]
    affected_sink_ids: list[str]
    restored_sink_ids: list[str]
    blocks: list[SourceSwitchBlock] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


@dataclass
class RecommendResult:
    """关阀建议结果（只读计算，不改任何状态）。

    feasible=False 时 reason 给出原因：
    - FAULT_PIPE_NOT_FOUND：故障管段不存在；
    - NO_FEASIBLE_PLAN：关闭全部可用阀门后故障管段仍有端点与水源相通。
    """

    feasible: bool
    recommended_close_pipe_ids: list[str]
    affected_sink_ids: list[str]
    reason: Issue | None = None


# ---------------------------------------------------------------- 导入校验


def validate_import(
    nodes: list[Node], pipes: list[Pipe]
) -> list[Issue]:
    """校验整网导入数据：重号、自环、悬空端点、无阀管段初始关闭。"""
    issues: list[Issue] = []

    node_ids: set[str] = set()
    for n in nodes:
        if n.id in node_ids:
            issues.append(Issue(DUPLICATE_NODE_ID, f"节点编号重复：{n.id}", n.id))
        node_ids.add(n.id)

    pipe_ids: set[str] = set()
    for p in pipes:
        if p.id in pipe_ids:
            issues.append(Issue(DUPLICATE_PIPE_ID, f"管段编号重复：{p.id}", p.id))
        pipe_ids.add(p.id)

        if p.from_node == p.to_node:
            issues.append(Issue(PIPE_SELF_LOOP, f"管段自环（两端为同一节点）：{p.id}", p.id))
        for endpoint in (p.from_node, p.to_node):
            if endpoint not in node_ids:
                issues.append(
                    Issue(
                        PIPE_ENDPOINT_UNKNOWN,
                        f"管段 {p.id} 的端点 {endpoint} 未在节点列表中定义（悬空端点）",
                        p.id,
                    )
                )
        if p.valve_closed and not p.has_valve:
            issues.append(
                Issue(
                    NON_VALVE_PIPE_CLOSED,
                    f"管段 {p.id} 没有阀门，不能设置为关闭状态",
                    p.id,
                )
            )
    return issues


# ---------------------------------------------------------------- 可达性


def _reachable_from(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
    seeds,
) -> set[str]:
    """无向 BFS：从 seeds 出发，返回可达节点集合。

    removed_pipe_ids 中的管段视为不存在/不可通行。
    """
    adjacency: dict[str, list[str]] = {nid: [] for nid in nodes}
    for p in pipes:
        if p.id in removed_pipe_ids:
            continue
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        adjacency[p.from_node].append(p.to_node)
        adjacency[p.to_node].append(p.from_node)

    reachable: set[str] = set()
    queue: deque[str] = deque()
    for nid in seeds:
        if nid in nodes and nid not in reachable:
            reachable.add(nid)
            queue.append(nid)

    while queue:
        current = queue.popleft()
        for nxt in adjacency.get(current, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    return reachable


def _reachable(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
) -> set[str]:
    """无向 BFS：从全部“运行中”的水源出发，返回可达节点集合。

    停运(running=False)的水源不供水，不作为通水起点，因此读网、隔离、
    关阀建议与复供的通水判断均按运行水源计算。
    """
    return _reachable_from(
        nodes,
        pipes,
        removed_pipe_ids,
        [
            nid
            for nid, node in nodes.items()
            if node.type == "source" and node.running
        ],
    )


# ---------------------------------------------------------------- 方案评估


def validate_plan_request(
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool,
) -> list[Issue]:
    """校验故障/拟关列表本身的合法性。

    for_submit=True 时额外校验“已关闭阀门不得重复关闭”。
    """
    issues: list[Issue] = []

    if fault_pipe_id not in pipes:
        issues.append(
            Issue(FAULT_PIPE_NOT_FOUND, f"故障管段不存在：{fault_pipe_id}", fault_pipe_id)
        )

    seen: set[str] = set()
    for pid in close_pipe_ids:
        if pid in seen:
            issues.append(Issue(CLOSE_PIPE_DUPLICATE, f"拟关闭管段重复：{pid}", pid))
            continue
        seen.add(pid)

        if pid == fault_pipe_id:
            issues.append(
                Issue(CLOSE_PIPE_IS_FAULT, "不能把故障管段本身作为关闭阀门", pid)
            )
            continue
        pipe = pipes.get(pid)
        if pipe is None:
            issues.append(Issue(CLOSE_PIPE_NOT_FOUND, f"拟关闭管段不存在：{pid}", pid))
        elif not pipe.has_valve:
            issues.append(Issue(CLOSE_PIPE_NO_VALVE, f"管段没有阀门，无法关闭：{pid}", pid))
        elif for_submit and pipe.valve_closed:
            issues.append(
                Issue(
                    CLOSE_PIPE_DUPLICATE,
                    f"阀门已处于关闭状态，不能重复关闭：{pid}",
                    pid,
                )
            )
    return issues


def evaluate_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool = False,
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
) -> PlanResult:
    """评估隔离方案。

    预演(for_submit=False)：只做结构校验，已关闭阀门不会产生“重复关闭”问题；
    提交(for_submit=True)：对已关闭阀门再次关闭视为 CLOSE_PIPE_DUPLICATE。

    pending_fault_pipe_ids：其他“待修复”隔离事件的故障管段编号集合，
    无论方案前/方案后都视为不可通行（故障未修复，不能通水）。
    """
    issues = validate_plan_request(
        pipes, fault_pipe_id, close_pipe_ids, for_submit=for_submit
    )
    blocking = [i for i in issues if i.code in BLOCKING_ISSUE_CODES]

    fault = pipes.get(fault_pipe_id)
    if fault is None or blocking:
        # 结构不合法：无法进行可达性评估
        return PlanResult(
            isolatable=False, affected_sink_ids=[], issues=issues
        )

    pipe_list = list(pipes.values())
    pending_faults = frozenset(pid for pid in pending_fault_pipe_ids if pid in pipes)

    # 方案前（当前态）：已关闭阀门、其他待修复故障管段不可通行，本次故障尚未移除
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    base_removed = already_closed | pending_faults
    before_reachable = _reachable(nodes, pipe_list, base_removed)

    # 方案后：故障管段移除；已关阀门、其他待修复故障继续生效；拟关阀门全部关闭
    blocked = base_removed | frozenset(close_pipe_ids)
    after_removed = blocked | frozenset({fault_pipe_id})
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    both_ends_isolated = (
        fault.from_node not in after_reachable
        and fault.to_node not in after_reachable
    )

    affected = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid in before_reachable and nid not in after_reachable
    )

    return PlanResult(
        isolatable=both_ends_isolated,
        affected_sink_ids=affected,
        issues=issues,
        blocked_pipe_ids=frozenset(blocked),
    )


# ---------------------------------------------------------------- 复供评估


def evaluate_restore(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    reopen_pipe_ids: frozenset[str],
    other_fault_pipe_ids: frozenset[str],
) -> RestoreResult:
    """评估复供安全性（只读，不改任何状态）。

    - reopen_pipe_ids：本事件提交时关闭的阀门管段，复供后重新通水；
    - other_fault_pipe_ids：其他“待修复”事件的故障管段，继续视为不可通行。

    仅当其余待修复故障管段的两端在复供图中仍均不可从水源到达，safe=True。
    预计恢复供水用水点 = 复供前不可达、复供后可达的 sink。
    """
    pipe_list = list(pipes.values())
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    other_faults = frozenset(
        pid for pid in other_fault_pipe_ids if pid in pipes
    )

    # 复供前（当前态）：仍关闭阀门 + 其他待修复故障管段不可通行
    before_removed = already_closed | other_faults
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 复供后：本事件阀门重开（从阻断集合剔除）；其他待修复故障仍然阻断
    after_removed = (already_closed - frozenset(reopen_pipe_ids)) | other_faults
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    blocks: list[ReopenBlock] = []
    for fid in sorted(other_faults):
        fault = pipes[fid]
        reachable_ends = [
            end
            for end in (fault.from_node, fault.to_node)
            if end in after_reachable
        ]
        if reachable_ends:
            blocks.append(
                ReopenBlock(fault_pipe_id=fid, reachable_endpoints=reachable_ends)
            )

    restored = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid not in before_reachable and nid in after_reachable
    )

    return RestoreResult(
        safe=not blocks,
        restored_sink_ids=restored,
        blocks=blocks,
        blocked_pipe_ids=frozenset(after_removed),
    )


# ---------------------------------------------------------------- 关阀建议


def recommend_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    *,
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
) -> RecommendResult:
    """按当前管网为故障管段计算一套可提交的关阀建议（只读，不改任何状态）。

    规则：
    - 已关闭阀门与其他待修复事件的故障管段均不可通行；
    - 只能从“尚未关闭的有阀管段”中选取，且不含故障管段本身
      （与提交接口 CLOSE_PIPE_IS_FAULT / CLOSE_PIPE_DUPLICATE 约束一致，
      保证建议可直接提交）；
    - 方案须使故障管段两端都与所有水源断开；
    - 优选顺序：新增失供用水点数最少 → 本次关阀数最少 →
      关阀编号序列（按字典序排序后）字典序最小。

    实现：候选阀门为“位于某条水源→故障端路径上”的未关闭有阀管段
    （不在任何此类路径上的阀门无助于隔离，只会增加失供与关阀数，
    任何包含它的方案都被去掉它后的方案严格占优）；
    按关阀数升序、同数按编号字典序枚举候选子集，用 evaluate_plan 评估。
    新增失供数以“仅移除故障管段、不关任何阀”时的失供数为理论下界，
    枚举中首个达到下界的方案即全局最优，可提前结束。
    """
    fault = pipes.get(fault_pipe_id)
    if fault is None:
        return RecommendResult(
            feasible=False,
            recommended_close_pipe_ids=[],
            affected_sink_ids=[],
            reason=Issue(
                FAULT_PIPE_NOT_FOUND, f"故障管段不存在：{fault_pipe_id}", fault_pipe_id
            ),
        )

    pipe_list = list(pipes.values())
    pending_faults = frozenset(pid for pid in pending_fault_pipe_ids if pid in pipes)
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    base_removed = already_closed | pending_faults

    # 空方案（只移除故障管段）：若已可隔离即最优（失供最少且关阀数为 0）；
    # 其失供数同时是任何方案新增失供的理论下界（关阀只会移除更多管段）。
    empty = evaluate_plan(
        nodes, pipes, fault_pipe_id, [], pending_fault_pipe_ids=pending_faults
    )
    if empty.isolatable:
        return RecommendResult(
            feasible=True,
            recommended_close_pipe_ids=[],
            affected_sink_ids=empty.affected_sink_ids,
        )
    min_affected_count = len(empty.affected_sink_ids)

    # 方案图（移除故障管段后）中：水源侧与故障端侧各自的可达范围
    fault_removed = base_removed | frozenset({fault_pipe_id})
    source_side = _reachable(nodes, pipe_list, fault_removed)
    fault_side = _reachable_from(
        nodes, pipe_list, fault_removed, (fault.from_node, fault.to_node)
    )

    # 候选阀门：未关闭、非故障管段本身，且一端在水源侧、一端在故障端侧
    candidates = sorted(
        p.id
        for p in pipe_list
        if p.has_valve
        and not p.valve_closed
        and p.id != fault_pipe_id
        and (
            (p.from_node in source_side and p.to_node in fault_side)
            or (p.to_node in source_side and p.from_node in fault_side)
        )
    )

    best: tuple[tuple, list[str], list[str]] | None = None
    for size in range(1, len(candidates) + 1):
        for combo in combinations(candidates, size):
            result = evaluate_plan(
                nodes,
                pipes,
                fault_pipe_id,
                list(combo),
                pending_fault_pipe_ids=pending_faults,
            )
            if not result.isolatable:
                continue
            key = (len(result.affected_sink_ids), size, combo)
            if best is None or key < best[0]:
                best = (key, list(combo), result.affected_sink_ids)
            if len(result.affected_sink_ids) == min_affected_count:
                # 达到理论下界：size 升序 + 组合字典序，首个即全局最优
                return RecommendResult(
                    feasible=True,
                    recommended_close_pipe_ids=list(combo),
                    affected_sink_ids=result.affected_sink_ids,
                )

    if best is None:
        return RecommendResult(
            feasible=False,
            recommended_close_pipe_ids=[],
            affected_sink_ids=[],
            reason=Issue(
                NO_FEASIBLE_PLAN,
                f"关闭全部可用阀门后，故障管段 {fault_pipe_id} 仍至少有一端"
                "可从水源到达，无可行关阀方案",
                fault_pipe_id,
            ),
        )
    return RecommendResult(
        feasible=True,
        recommended_close_pipe_ids=best[1],
        affected_sink_ids=best[2],
    )


# ---------------------------------------------------------------- 水源状态切换


def validate_source_targets(
    nodes: dict[str, Node],
    targets: list[tuple[str, bool]],
) -> list[Issue]:
    """校验水源切换请求本身：编号存在、节点是水源、同批不重复。"""
    issues: list[Issue] = []
    seen: set[str] = set()
    for source_id, _running in targets:
        if source_id in seen:
            issues.append(
                Issue(
                    SOURCE_TARGET_DUPLICATE,
                    f"同批水源编号不得重复：{source_id}",
                    source_id,
                )
            )
            continue
        seen.add(source_id)

        node = nodes.get(source_id)
        if node is None:
            issues.append(
                Issue(SOURCE_NOT_FOUND, f"水源节点不存在：{source_id}", source_id)
            )
        elif node.type != "source":
            issues.append(
                Issue(
                    SOURCE_NOT_SWITCHABLE,
                    f"节点 {source_id} 不是水源，仅水源节点可切换运行状态",
                    source_id,
                )
            )
    return issues


def evaluate_source_switch(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    targets: list[tuple[str, bool]],
    *,
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
) -> SourceSwitchResult:
    """评估水源运行状态切换（只读，不改任何状态）。

    targets：(水源编号, 目标运行状态) 列表，True=恢复运行、False=停运。
    pending_fault_pipe_ids：待修复(open)隔离事件的故障管段，通水时仍视为不可通行
    （已关闭阀门同样阻断）。

    返回：
    - changed：目标状态与当前是否有差异（无差异确认时不推进版本）；
    - affected_sink_ids：切换后新增失供用水点（切换前可达 → 切换后不可达）；
    - restored_sink_ids：切换后恢复供水用水点（切换前不可达 → 切换后可达）；
    - blocks：切换后每条待修复故障管段的重新进水风险（任一端从运行水源可达）。
      停运只会收缩可达范围，不会引入风险；风险只可能由恢复水源引发。
    """
    issues = validate_source_targets(nodes, targets)
    if issues:
        return SourceSwitchResult(
            changed=False,
            safe=False,
            targets=list(targets),
            affected_sink_ids=[],
            restored_sink_ids=[],
            issues=issues,
        )

    pipe_list = list(pipes.values())
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    pending_faults = frozenset(pid for pid in pending_fault_pipe_ids if pid in pipes)
    blocked = already_closed | pending_faults

    target_map = dict(targets)
    changed = any(nodes[sid].running != running for sid, running in targets)

    # 切换前（当前态）：按当前运行水源计算可达
    before_reachable = _reachable(nodes, pipe_list, blocked)

    # 切换后：仅水源运行状态改变，管网结构/阀位/故障管段不变
    switched_nodes = {
        nid: Node(
            id=n.id,
            type=n.type,
            name=n.name,
            running=target_map.get(nid, n.running),
        )
        for nid, n in nodes.items()
    }
    after_reachable = _reachable(switched_nodes, pipe_list, blocked)

    affected = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid in before_reachable and nid not in after_reachable
    )
    restored = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid not in before_reachable and nid in after_reachable
    )

    # 每条待修复故障：切换后任一端可从运行水源到达即有重新进水风险
    blocks: list[SourceSwitchBlock] = []
    for fid in sorted(pending_faults):
        fault = pipes[fid]
        reachable_ends = [
            end
            for end in (fault.from_node, fault.to_node)
            if end in after_reachable
        ]
        if reachable_ends:
            blocks.append(
                SourceSwitchBlock(
                    fault_pipe_id=fid, reachable_endpoints=reachable_ends
                )
            )

    return SourceSwitchResult(
        changed=changed,
        safe=not blocks,
        targets=list(targets),
        affected_sink_ids=affected,
        restored_sink_ids=restored,
        blocks=blocks,
        issues=[],
    )


# ---------------------------------------------------------------- 流量核算


@dataclass
class DeliveryResult:
    """用水点最低流量核算结果（只读计算，不改任何状态）。

    - total_demand：本次请求的需求总量（未列需求的用水点不消耗流量）；
    - max_delivered_total：现有阀位与故障状态下，按各水源供水上限与各管段
      双向共用输水上限可交付到这些用水点的最大总量（对总需求封顶）；
    - feasible：max_delivered_total == total_demand，即全部最低需求均可满足。
    """

    total_demand: int
    max_delivered_total: int
    feasible: bool
    issues: list[Issue] = field(default_factory=list)


def validate_delivery_request(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    source_caps: list[tuple[str, int]],
    pipe_caps: list[tuple[str, int]],
    demands: list[tuple[str, int]],
) -> list[Issue]:
    """校验流量核算请求本身（结构 / 编号 / 覆盖 / 重复）。

    - 水源、管段编号必须完整覆盖当前管网：存在性 + 无重复 + 无遗漏（不多不少）；
    - 用水点编号必须存在且节点类型为 sink，同批不得重复；需求列表非空。
    容量与需求的非负整数约束由请求模型（pydantic）保证，此处不再重复。
    """
    issues: list[Issue] = []

    network_source_ids = {nid for nid, n in nodes.items() if n.type == "source"}
    seen_sources: set[str] = set()
    for source_id, _cap in source_caps:
        if source_id in seen_sources:
            issues.append(
                Issue(
                    DELIVERY_SOURCE_DUPLICATE,
                    f"水源编号重复：{source_id}",
                    source_id,
                )
            )
            continue
        seen_sources.add(source_id)
        if source_id not in nodes or nodes[source_id].type != "source":
            # 编号不存在或节点不是水源，均视为该水源不属于当前管网
            issues.append(
                Issue(
                    DELIVERY_SOURCE_NOT_FOUND,
                    f"水源编号在当前管网不存在：{source_id}",
                    source_id,
                )
            )
    for missing in sorted(network_source_ids - seen_sources):
        issues.append(
            Issue(
                DELIVERY_SOURCE_MISSING,
                f"当前管网水源未在请求中列出（水源容量须覆盖全部水源）：{missing}",
                missing,
            )
        )

    seen_pipes: set[str] = set()
    for pipe_id, _cap in pipe_caps:
        if pipe_id in seen_pipes:
            issues.append(
                Issue(
                    DELIVERY_PIPE_DUPLICATE,
                    f"管段编号重复：{pipe_id}",
                    pipe_id,
                )
            )
            continue
        seen_pipes.add(pipe_id)
        if pipe_id not in pipes:
            issues.append(
                Issue(
                    DELIVERY_PIPE_NOT_FOUND,
                    f"管段编号在当前管网不存在：{pipe_id}",
                    pipe_id,
                )
            )
    for missing in sorted(set(pipes) - seen_pipes):
        issues.append(
            Issue(
                DELIVERY_PIPE_MISSING,
                f"当前管网管段未在请求中列出（管段容量须覆盖全部管段）：{missing}",
                missing,
            )
        )

    if not demands:
        issues.append(
            Issue(
                DELIVERY_DEMANDS_EMPTY,
                "用水点需求列表不能为空：至少给出一个非空（列名）用水点需求",
            )
        )
    seen_sinks: set[str] = set()
    for sink_id, _demand in demands:
        if sink_id in seen_sinks:
            issues.append(
                Issue(
                    DELIVERY_SINK_DUPLICATE,
                    f"用水点编号重复：{sink_id}",
                    sink_id,
                )
            )
            continue
        seen_sinks.add(sink_id)
        node = nodes.get(sink_id)
        if node is None:
            issues.append(
                Issue(
                    DELIVERY_SINK_NOT_FOUND,
                    f"用水点编号不存在：{sink_id}",
                    sink_id,
                )
            )
        elif node.type != "sink":
            issues.append(
                Issue(
                    DELIVERY_SINK_NOT_SINK,
                    f"节点 {sink_id} 不是用水点，仅用水点节点可申报需求",
                    sink_id,
                )
            )
    return issues


def _undirected_max_flow(
    node_ids: list[str],
    edges: list[tuple[str, str, int]],
    sources: list[tuple[str, int]],
    sinks: list[tuple[str, int]],
) -> int:
    """整数无向（双向共用容量）多源多汇最大流，Dinic 实现。

    - edges：(u, v, c)，无向边净流量 |f| <= c（两方向合计不得超过 c）。
      残差网络中该边初始两个方向的剩余容量均为 c：向一个方向输水 d 后，
      该方向剩余 c-d、反向剩余 c+d（c 为可回撤量、d 为换向余量），
      天然保证任意时刻两方向净流量之和不超过 c。
    - sources：(节点, 供水上限)；sinks：(节点, 需求上限)。
    - 增加超级源/超级汇把多源多汇化为单源单汇；汇边容量为各点需求，
      因而最大流天然对总需求封顶（其值即“可交付到这些需求点的最大总量”）。
    所有容量均为非负整数，最大流值为整数。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    super_source = len(node_ids)
    super_sink = super_source + 1
    n = super_sink + 1

    graph: list[list[list[int]]] = [[] for _ in range(n)]

    def add_arc(u: int, v: int, cap: int) -> int:
        graph[u].append([v, cap, len(graph[v])])
        graph[v].append([u, 0, len(graph[u]) - 1])
        return len(graph[u]) - 1

    for u, v, cap in edges:
        if cap <= 0:
            continue
        iu, iv = index[u], index[v]
        # 无向边：两条相互独立的有向弧，初始容量均为 cap。
        # 二者互为“输水余量 + 对向可回撤量”，见函数说明。
        add_arc(iu, iv, cap)
        add_arc(iv, iu, cap)
    for nid, cap in sources:
        if cap > 0:
            add_arc(super_source, index[nid], cap)
    for nid, cap in sinks:
        if cap > 0:
            add_arc(index[nid], super_sink, cap)

    total = 0
    # 图规模受管网节点数限制；提高递归上限，避免深链管网触发默认限制
    sys.setrecursionlimit(max(sys.getrecursionlimit(), n * 4 + 1000))
    while True:
        # BFS 分层
        level = [-1] * n
        level[super_source] = 0
        queue: deque[int] = deque([super_source])
        while queue:
            u = queue.popleft()
            for v, cap, rev in graph[u]:
                if cap > 0 and level[v] < 0:
                    level[v] = level[u] + 1
                    queue.append(v)
        if level[super_sink] < 0:
            return total

        cur = [0] * n

        def dfs(u: int, pushed: int) -> int:
            if u == super_sink:
                return pushed
            while cur[u] < len(graph[u]):
                edge = graph[u][cur[u]]
                v, cap, rev = edge
                if cap > 0 and level[v] == level[u] + 1:
                    sent = dfs(v, min(pushed, cap))
                    if sent:
                        edge[1] -= sent
                        graph[v][rev][1] += sent
                        return sent
                cur[u] += 1
            return 0

        while True:
            pushed = dfs(super_source, 10**18)
            if not pushed:
                break
            total += pushed


def evaluate_delivery(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    source_caps: list[tuple[str, int]],
    pipe_caps: list[tuple[str, int]],
    demands: list[tuple[str, int]],
    *,
    blocked_pipe_ids: frozenset[str] = frozenset(),
) -> DeliveryResult:
    """在现有阀位与故障状态下核算用水点最低流量能否满足（只读）。

    blocked_pipe_ids：已关闭阀门与待修复故障管段，按与可达性计算完全一致的
    口径视为不可通行（容量按 0 处理）。停运水源(running=False)不供水
    （容量按 0 处理）。调用方须先通过 validate_delivery_request 结构校验。
    """
    total_demand = sum(d for _sid, d in demands)

    source_cap_map = dict(source_caps)
    running_sources = [
        (nid, source_cap_map[nid])
        for nid, node in nodes.items()
        if node.type == "source" and node.running and source_cap_map.get(nid, 0) > 0
    ]

    sink_demands = [(nid, d) for nid, d in demands if d > 0]

    pipe_cap_map = dict(pipe_caps)
    edges = [
        (p.from_node, p.to_node, 0 if p.id in blocked_pipe_ids else pipe_cap_map[p.id])
        for p in pipes.values()
    ]

    if total_demand == 0 or not running_sources or not sink_demands:
        max_delivered = 0
    else:
        max_delivered = _undirected_max_flow(
            list(nodes.keys()), edges, running_sources, sink_demands
        )
        # 汇边容量已对各点需求封顶，整体不超过需求总量（防御性夹取）
        max_delivered = min(max_delivered, total_demand)

    return DeliveryResult(
        total_demand=total_demand,
        max_delivered_total=max_delivered,
        feasible=max_delivered == total_demand,
    )


# ---------------------------------------------------- 保障等级分档交付


@dataclass
class SinkAllocation:
    """单个用水点的分档交付结果（只读计算）。"""

    sink_id: str
    level: int
    demand: int
    delivered: int

    @property
    def shortfall(self) -> int:
        return self.demand - self.delivered


@dataclass
class LevelAllocation:
    """单个保障等级的交付汇总（按等级数值升序返回）。"""

    level: int
    demand: int
    delivered: int
    shortfall: int
    all_satisfied: bool


@dataclass
class AllocationResult:
    """保障等级分档交付结果（只读计算，不改任何状态）。

    - entries：按（等级升序、用水点编号字典序）排列的各点交付；
    - levels：按等级数值升序排列的分档汇总；
    - 先按等级依次最大化该等级交付总量，再在各等级总量最优条件下按
      （等级, 用水点编号）逐点最大化交付；delivered 由一组同时可行的整数流量实现。
    """

    total_demand: int
    total_delivered: int
    all_satisfied: bool
    entries: list[SinkAllocation] = field(default_factory=list)
    levels: list[LevelAllocation] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)


def _lex_bounded_circulation(
    n: int,
    arcs: list[tuple[int, int, int, int]],
):
    """有下界整数环流可行性（约定同 _bounded_feasible_flow，外部供给恒为 0）。

    arcs：(u, v, low, high)。对每条弧先强制流下界 low：b(u)-=low、b(v)+=low；
    b(n)>0（净需入）加 SS->n，b(n)<0（净需出）加 n->TT；SS->TT 最大流全部
    饱和即存在可行环流。返回 (是否可行, refs)，refs 每条为
    [u, v, low, high, 正向边索引, 总流量]，供残量补偿增广复用。
    """
    ss = n
    tt = n + 1
    dinic = _Dinic(n + 2)
    refs: list[list] = []
    b = [0] * n
    for u, v, low, high in arcs:
        idx = dinic.add_arc(u, v, high - low)
        refs.append([u, v, low, high, idx])
        b[u] -= low
        b[v] += low
    required = 0
    for node, balance in enumerate(b):
        if balance > 0:
            dinic.add_arc(ss, node, balance)
            required += balance
        elif balance < 0:
            dinic.add_arc(node, tt, -balance)
    if dinic.max_flow(ss, tt) != required:
        return False, []
    for ref in refs:
        u, _v, low, _high, idx = ref
        ref.append(low + dinic.graph[u][idx][4])
    return True, refs


def _lex_residual_circulation_delta(
    n: int,
    refs: list[list],
    target: tuple[int, int],
) -> int:
    """残量环流补偿：目标弧 u->v 在不回撤任何锁定弧前提下还能再增多少。

    剔除目标弧自身（正反向均剔除）后，在残量网上做 v->u 最大流（与
    reconcile_meters 缺报范围同一原理）。每条**自由弧**（low<high）贡献
    正向余量 high-f 与反向回撤 f 两条独立残量弧；**锁定弧（low=high）只
    保留正向余量、不开放反向回撤**，否则补偿环流会沿其反向残量抽走已锁定
    的更低等级总量/逐点交付，违背字典序约束。
    """
    tu, tv = target
    residual = _Dinic(n)
    for u, v, low, high, _idx, f in refs:
        if (u, v) == (tu, tv):
            continue  # 目标弧自身：正向余量与反向回撤都剔除
        locked = low >= high
        if high - f > 0:
            residual.add_arc(u, v, high - f)
        if not locked and f > 0:
            residual.add_arc(v, u, f)
    return residual.max_flow(tv, tu)


def allocate_delivery(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    source_caps: list[tuple[str, int]],
    pipe_caps: list[tuple[str, int]],
    demands: list[tuple[str, int, int]],
    *,
    blocked_pipe_ids: frozenset[str] = frozenset(),
) -> AllocationResult:
    """按保障等级分档核算各用水点可获得的交付流量（只读）。

    demands：(用水点编号, 非负整数需求, 正整数等级)，等级数字越小越优先。
    blocked_pipe_ids：已关闭阀门与待修复故障管段，容量按 0（与可达性口径一致）。
    停运水源(running=False)不供水（容量按 0）。调用方须先通过
    validate_delivery_request（demands 取 (sink_id, demand)）结构校验。

    优化序：先按等级从小到大依次最大化该等级交付总量，再在各等级总量均为
    最优的条件下，按（等级, 用水点编号字典序）逐点最大化交付；各点交付不
    超过需求；每一步以有下界整数环流 + 残量环流补偿增广推进，锁定量以
    low=high 固定弧保住，最终各点交付由一组同时可行的整数流量实现。

    环流网络：超级源 SRC -> 运行水源（[0, 供水上限]）-> 无向管段网（两条
    独立 [0,c] 有向弧，双向共用同总量核算口径）-> 用水点（[low, 需求]）
    -> 等级收集器 C_l -> 超级汇 SNK（等级弧 [low, high]），并以返回弧
    SNK->SRC（[0, 总需求]）闭合为环流。残量补偿沿“目标弧终点 -> 起点”
    推动一整圈环流（经返回弧闭合）来增加目标弧流量，故不会产生脱离管网的
    人工流量；锁定弧（low=high）不开放反向残量，已锁定等级总量/逐点交付
    不可能被补偿增广挤占。

    - 阶段 A 处理等级 l 时只让 <=l 的等级入网：更低等级总量弧锁定为
      [总量, 总量]，更高等级用水点不入网（在已锁定更低等级总量下，本级
      可达最大值与全网一致——未来用水点不消耗即可得到同一流量）；
    - 阶段 B 全部等级总量弧锁定为 [总量, 总量]，已逐点锁定的用水点汇水弧
      也锁定为 [交付, 交付]；同等级未锁定点的残量回撤天然实现“本级总量
      不变下把水让给优先点”。
    """
    # (sink_id, demand, level)，排序键：等级升序 -> 用水点编号字典序
    ordered = sorted(demands, key=lambda t: (t[2], t[0]))
    total_demand = sum(d for _sid, d, _lvl in ordered)
    levels_present = sorted({lvl for _sid, _d, lvl in ordered})

    source_cap_map = dict(source_caps)
    running_sources = [
        (nid, source_cap_map[nid])
        for nid, node in nodes.items()
        if node.type == "source" and node.running and source_cap_map.get(nid, 0) > 0
    ]
    pipe_cap_map = dict(pipe_caps)

    node_ids = list(nodes.keys())
    index = {nid: i for i, nid in enumerate(node_ids)}
    src = len(node_ids)
    collector_base = src + 1
    collector = {lvl: collector_base + i for i, lvl in enumerate(levels_present)}
    snk = collector_base + len(levels_present)
    n_nodes = snk + 1

    level_demand: dict[int, int] = {}
    for _sid, d, lvl in ordered:
        level_demand[lvl] = level_demand.get(lvl, 0) + d
    demand_of = {sid: d for sid, d, _lvl in ordered}

    def solve_step(
        active_levels: set[int],
        locked_levels: dict[int, int],
        locked_sinks: dict[str, int],
        target: tuple[int, int, int],
    ) -> int:
        """构建一次有下界环流，返回目标弧 (u,v,上界) 的最大可达流量。"""
        tu, tv, thigh = target
        arcs: list[tuple[int, int, int, int]] = [(snk, src, 0, total_demand)]
        for nid, cap in running_sources:
            arcs.append((src, index[nid], 0, cap))
        for p in pipes.values():
            cap = 0 if p.id in blocked_pipe_ids else pipe_cap_map[p.id]
            if cap > 0:
                arcs.append((index[p.from_node], index[p.to_node], 0, cap))
                arcs.append((index[p.to_node], index[p.from_node], 0, cap))
        for sid, d, lvl in ordered:
            if lvl not in active_levels:
                continue  # 阶段 A：更高等级用水点不入网、不消耗
            locked = locked_sinks.get(sid, 0)
            if locked:
                arcs.append((index[sid], collector[lvl], locked, locked))
            else:
                arcs.append((index[sid], collector[lvl], 0, d))
        for lvl, c in collector.items():
            if lvl not in active_levels:
                continue
            total = locked_levels.get(lvl, 0)
            if total:
                arcs.append((c, snk, total, total))  # 锁定弧 low=high
            else:
                arcs.append((c, snk, 0, level_demand[lvl]))

        feasible, refs = _lex_bounded_circulation(n_nodes, arcs)
        # 锁定值都是此前各步在更松约束下取得的可达值，归纳保证可行
        assert feasible, "保障等级分档：锁定约束下应始终存在可行环流"
        current = next(ref[5] for ref in refs if ref[0] == tu and ref[1] == tv)
        gain = _lex_residual_circulation_delta(n_nodes, refs, (tu, tv))
        return min(thigh, current + gain)

    # 阶段 A：按等级升序依次最大化该等级交付总量（更高等级暂不入网）
    level_totals: dict[int, int] = {}
    active: set[int] = set()
    for lvl in levels_present:
        active.add(lvl)
        level_totals[lvl] = solve_step(
            active,
            dict(level_totals),
            {},
            (collector[lvl], snk, level_demand[lvl]),
        )

    # 阶段 B：各等级总量固定为最优，按（等级, 编号）逐点最大化交付。
    # 目标弧为用水点 -> 其等级收集器，补偿增广沿收集器 -> 用水点推动环流。
    locked_sinks: dict[str, int] = {}
    all_levels = set(levels_present)
    for sid, _d, _lvl in ordered:
        locked_sinks[sid] = solve_step(
            all_levels,
            dict(level_totals),
            dict(locked_sinks),
            (index[sid], collector[_lvl], demand_of[sid]),
        )

    demand_map = {sid: (d, lvl) for sid, d, lvl in ordered}
    entries = [
        SinkAllocation(
            sink_id=sid,
            level=demand_map[sid][1],
            demand=demand_map[sid][0],
            delivered=locked_sinks[sid],
        )
        for sid, _d, _lvl in ordered
    ]
    level_summaries: list[LevelAllocation] = []
    for lvl in levels_present:
        members = [e for e in entries if e.level == lvl]
        d_sum = sum(e.demand for e in members)
        delivered_sum = sum(e.delivered for e in members)
        level_summaries.append(
            LevelAllocation(
                level=lvl,
                demand=d_sum,
                delivered=delivered_sum,
                shortfall=d_sum - delivered_sum,
                all_satisfied=delivered_sum == d_sum,
            )
        )

    total_delivered = sum(e.delivered for e in entries)
    return AllocationResult(
        total_demand=total_demand,
        total_delivered=total_delivered,
        all_satisfied=total_delivered == total_demand,
        entries=entries,
        levels=level_summaries,
    )


# ---------------------------------------------------- 临时旁通管评估


@dataclass
class BypassBlock:
    """旁通安全风险：接入旁通后某条待修复故障管段有一端将从运行水源重新进水。"""

    fault_pipe_id: str
    reachable_endpoints: list[str]

    def as_dict(self) -> dict:
        return {
            "code": FAULT_REWATER_RISK,
            "message": (
                f"接入旁通后，待修复故障管段 {self.fault_pipe_id} 的 "
                f"{ '、'.join(self.reachable_endpoints) } 端将重新进水，旁通不可用"
            ),
            "pipe_id": self.fault_pipe_id,
            "reachable_endpoints": list(self.reachable_endpoints),
        }


@dataclass
class BypassEvaluationResult:
    """临时旁通管供水效果评估结果（只读计算，不改任何状态）。

    - baseline：未接入旁通时按现有分档交付规则的基线结果（无论安全与否恒给出）；
    - bypass：接入旁通后的分档交付结果；存在重新进水风险时为 None
      （判为不可用，不输出旁通交付量）；
    - blocks：每条待修复故障管段在接入旁通后的重新进水风险（任一端从运行
      水源可达即列入）；存在风险时 safe=False。
    """

    baseline: AllocationResult
    bypass: AllocationResult | None
    safe: bool
    blocks: list[BypassBlock] = field(default_factory=list)


def validate_bypass_request(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    from_node: str,
    to_node: str,
    source_caps: list[tuple[str, int]],
    pipe_caps: list[tuple[str, int]],
    demands: list[tuple[str, int]],
) -> list[Issue]:
    """校验临时旁通评估请求本身（结构 / 编号 / 覆盖 / 重复 / 旁通端点）。

    水源、管段、用水点编号规则与 validate_delivery_request 完全一致；
    旁通端点必须是当前管网中两个不同的现有节点（允许与现有管段并联，
    端点类型不限）。旁通容量的非负整数约束由请求模型（pydantic）保证。
    """
    issues = validate_delivery_request(nodes, pipes, source_caps, pipe_caps, demands)

    if from_node == to_node:
        issues.append(
            Issue(
                BYPASS_ENDPOINT_SAME,
                f"旁通管两端不能是同一节点：{from_node}",
                from_node,
            )
        )
    seen: set[str] = set()
    for endpoint in (from_node, to_node):
        if endpoint in seen:
            continue
        seen.add(endpoint)
        if endpoint not in nodes:
            issues.append(
                Issue(
                    BYPASS_ENDPOINT_NOT_FOUND,
                    f"旁通端点在当前管网不存在：{endpoint}",
                    endpoint,
                )
            )
    return issues


def evaluate_bypass(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    from_node: str,
    to_node: str,
    bypass_capacity: int,
    source_caps: list[tuple[str, int]],
    pipe_caps: list[tuple[str, int]],
    demands: list[tuple[str, int, int]],
    *,
    blocked_pipe_ids: frozenset[str] = frozenset(),
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
) -> BypassEvaluationResult:
    """评估一条临时旁通管的供水效果（只读，不改任何状态）。

    - from_node / to_node：旁通两端（须已过 validate_bypass_request 校验，
      允许与现有管段并联）；bypass_capacity：旁通管双向共用容量；
    - demands：(用水点编号, 非负整数需求, 正整数等级)，与 allocate_delivery 一致；
    - blocked_pipe_ids：已关闭阀门与待修复故障管段（容量按 0，不可通行）；
    - pending_fault_pipe_ids：待修复故障管段，用于接入旁通后的重新进水判定。

    先按当前状态核算基线分档交付；再仅在内存副本中接入旁通管（生成一个
    不与现有管段重号的虚拟编号，输入的 nodes/pipes 不被修改）：
    接入后任一待修复故障管段的任一端从运行水源可达即判不可用
    （拓扑可达判定，与复供/水源切换同口径，与旁通容量大小无关），
    不核算旁通交付量；否则按同一分档规则核算接入后的交付。
    """
    baseline = allocate_delivery(
        nodes,
        pipes,
        source_caps,
        pipe_caps,
        demands,
        blocked_pipe_ids=blocked_pipe_ids,
    )

    # 内存副本中接入旁通：虚拟编号避开现有管段（允许与现有管段并联）
    virtual_id = "__BYPASS__"
    while virtual_id in pipes:
        virtual_id = "_" + virtual_id
    bypass_pipes = dict(pipes)
    bypass_pipes[virtual_id] = Pipe(virtual_id, from_node, to_node)

    # 安全判定：接入旁通后，待修复故障管段任一端从运行水源可达即重新进水
    pending_faults = frozenset(pid for pid in pending_fault_pipe_ids if pid in pipes)
    after_reachable = _reachable(nodes, list(bypass_pipes.values()), blocked_pipe_ids)
    blocks: list[BypassBlock] = []
    for fid in sorted(pending_faults):
        fault = pipes[fid]
        reachable_ends = [
            end
            for end in (fault.from_node, fault.to_node)
            if end in after_reachable
        ]
        if reachable_ends:
            blocks.append(
                BypassBlock(fault_pipe_id=fid, reachable_endpoints=reachable_ends)
            )
    if blocks:
        return BypassEvaluationResult(
            baseline=baseline, bypass=None, safe=False, blocks=blocks
        )

    bypass_result = allocate_delivery(
        nodes,
        bypass_pipes,
        source_caps,
        list(pipe_caps) + [(virtual_id, bypass_capacity)],
        demands,
        blocked_pipe_ids=blocked_pipe_ids,
    )
    return BypassEvaluationResult(
        baseline=baseline, bypass=bypass_result, safe=True, blocks=[]
    )


# ---------------------------------------------------------------- 读数核对


@dataclass
class ReconcileReason:
    """读数核对不一致原因（200 响应，consistent=false 时给出）。"""

    code: str
    message: str
    pipe_id: str | None = None
    node_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "pipe_id": self.pipe_id,
            "node_id": self.node_id,
        }


@dataclass
class MeterRange:
    """单条缺报管段在全部可行解中的流量范围（带符号，from→to 为正）。"""

    pipe_id: str
    min_flow: int
    max_flow: int
    # min_flow == max_flow 时该缺报读数可唯一确定
    determinable: bool


@dataclass
class ReconcileResult:
    """读数核对结果（只读计算，不改任何状态）。

    - consistent：当前水源运行状态/关阀/待修复故障与注入、取用、容量、读数下
      是否存在可行的整数流量分配；为 False 时 reasons 给出原因且 readings 为空。
    - readings：缺报（null）管段的可行流量范围，按管段编号排序；
      已申报读数原样可信，不在补值列表中返回。
    """

    consistent: bool
    readings: list[MeterRange] = field(default_factory=list)
    reasons: list[ReconcileReason] = field(default_factory=list)


def validate_reconcile_request(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    injections: list[tuple[str, int]],
    withdrawals: list[tuple[str, int]],
    pipe_readings: list[tuple[str, int, int | None]],
) -> list[Issue]:
    """校验读数核对请求本身（结构 / 编号 / 覆盖 / 重复）。

    - 水源编号必须完整覆盖当前管网全部水源（不多不少）、同批不重复；
    - 用水点编号必须存在且为 sink，且完整覆盖全部 sink、同批不重复
      （每个用水点的实际取用量都要申报，未取用按 0 申报）；
    - 管段编号必须完整覆盖全部管段、同批不重复。
    非负整数 / 读数带符号整数 / 非 null 类型由请求模型（pydantic）保证。
    """
    issues: list[Issue] = []

    network_source_ids = {nid for nid, n in nodes.items() if n.type == "source"}
    seen_sources: set[str] = set()
    for source_id, _inj in injections:
        if source_id in seen_sources:
            issues.append(
                Issue(
                    RECONCILE_SOURCE_DUPLICATE,
                    f"水源编号重复：{source_id}",
                    source_id,
                )
            )
            continue
        seen_sources.add(source_id)
        node = nodes.get(source_id)
        if node is None or node.type != "source":
            issues.append(
                Issue(
                    RECONCILE_SOURCE_NOT_FOUND,
                    f"水源编号在当前管网不存在：{source_id}",
                    source_id,
                )
            )
    for missing in sorted(network_source_ids - seen_sources):
        issues.append(
            Issue(
                RECONCILE_SOURCE_MISSING,
                f"当前管网水源未在请求中列出（注入量须覆盖全部水源）：{missing}",
                missing,
            )
        )

    network_sink_ids = {nid for nid, n in nodes.items() if n.type == "sink"}
    seen_sinks: set[str] = set()
    for sink_id, _wd in withdrawals:
        if sink_id in seen_sinks:
            issues.append(
                Issue(
                    RECONCILE_SINK_DUPLICATE,
                    f"用水点编号重复：{sink_id}",
                    sink_id,
                )
            )
            continue
        seen_sinks.add(sink_id)
        node = nodes.get(sink_id)
        if node is None:
            issues.append(
                Issue(
                    RECONCILE_SINK_NOT_FOUND,
                    f"用水点编号不存在：{sink_id}",
                    sink_id,
                )
            )
        elif node.type != "sink":
            issues.append(
                Issue(
                    RECONCILE_SINK_NOT_SINK,
                    f"节点 {sink_id} 不是用水点，仅用水点节点可申报取用量",
                    sink_id,
                )
            )
    for missing in sorted(network_sink_ids - seen_sinks):
        issues.append(
            Issue(
                RECONCILE_SINK_MISSING,
                f"当前管网用水点未在请求中列出（取用量须覆盖全部用水点）：{missing}",
                missing,
            )
        )

    seen_pipes: set[str] = set()
    for pipe_id, _cap, _reading in pipe_readings:
        if pipe_id in seen_pipes:
            issues.append(
                Issue(
                    RECONCILE_PIPE_DUPLICATE,
                    f"管段编号重复：{pipe_id}",
                    pipe_id,
                )
            )
            continue
        seen_pipes.add(pipe_id)
        if pipe_id not in pipes:
            issues.append(
                Issue(
                    RECONCILE_PIPE_NOT_FOUND,
                    f"管段编号在当前管网不存在：{pipe_id}",
                    pipe_id,
                )
            )
    for missing in sorted(set(pipes) - seen_pipes):
        issues.append(
            Issue(
                RECONCILE_PIPE_MISSING,
                f"当前管网管段未在请求中列出（容量与读数须覆盖全部管段）：{missing}",
                missing,
            )
        )
    return issues


class _Dinic:
    """整数容量 Dinic 最大流（残量边以可变 list 表示）。

    与既有 _undirected_max_flow 相同的增广路算法，抽出为可复用类：
    有下界可行流判定（超级源/汇）与缺报弧范围计算（剔除目标弧的残量网）共用。

    边为 [v, cap, rev, sign, flow]：sign=1 为配对正向边、sign=-1 为其反向边。
    需要“独立的对向容量弧”（如无向边的初始双向容量、y 残量的两个方向）时
    由调用方再 add_arc 一次。flow 只记录本配对正向弧在最大流中承担的净流量：
    沿正向边增广 +、沿其反向边回撤 -，多次 add_arc 的独立弧互不污染。
    """

    def __init__(self, n: int):
        self.n = n
        self.graph: list[list[list[int]]] = [[] for _ in range(n)]

    def add_arc(self, u: int, v: int, cap: int) -> int:
        """添加配对有向弧 u->v（容量 cap，反向残量初值 0），返回正向边索引。"""
        # [终点, 容量, 对边索引, 方向(1正向/-1反向), 该正向弧承担的净流量]
        self.graph[u].append([v, cap, len(self.graph[v]), 1, 0])
        self.graph[v].append([u, 0, len(self.graph[u]) - 1, -1, 0])
        return len(self.graph[u]) - 1

    def max_flow(self, source: int, sink: int) -> int:
        total = 0
        sys.setrecursionlimit(max(sys.getrecursionlimit(), self.n * 4 + 1000))
        while True:
            level = [-1] * self.n
            level[source] = 0
            queue: deque[int] = deque([source])
            while queue:
                u = queue.popleft()
                for v, cap, _rev, _sign, _flow in self.graph[u]:
                    if cap > 0 and level[v] < 0:
                        level[v] = level[u] + 1
                        queue.append(v)
            if level[sink] < 0:
                return total

            cur = [0] * self.n

            def dfs(u: int, pushed: int) -> int:
                if u == sink:
                    return pushed
                while cur[u] < len(self.graph[u]):
                    edge = self.graph[u][cur[u]]
                    v, cap, rev, sign, _flow = edge
                    if cap > 0 and level[v] == level[u] + 1:
                        sent = dfs(v, min(pushed, cap))
                        if sent:
                            edge[1] -= sent
                            back = self.graph[v][rev]
                            back[1] += sent
                            # 沿正向边增广：该弧净流量 += sent；
                            # 沿反向边增广：所对正向弧（即 back）净流量 -= sent。
                            if sign == 1:
                                edge[4] += sent
                            else:
                                back[4] -= sent
                            return sent
                    cur[u] += 1
                return 0

            while True:
                pushed = dfs(source, 10**18)
                if not pushed:
                    break
                total += pushed


def _bounded_feasible_flow(
    node_ids: list[str],
    arcs: list[tuple[str, str, int, int]],
    balances: dict[str, int],
):
    """有向弧下界可行流（整数）。

    - arcs：(u, v, low, high)，0 <= low <= high；
    - balances：各节点净供给 b(n)=注入-取用。
      平衡条件为 流入 + b = 流出，即 流出-流入 = b。
    对每条弧先强制流下界 low：b(u)-=low、b(v)+=low；
    b(n)>0（净需入）时加超级源 SS->n（容量 b(n)），
    b(n)<0（净需出）时加 n->TT（容量 -b(n)），跑 SS→TT 最大流，
    全部 SS 出边饱和即存在可行流。返回 (是否可行, 各弧实际流量)。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    ss = len(node_ids)
    tt = ss + 1
    dinic = _Dinic(tt + 1)

    arc_edges: list[tuple[int, int, int]] = []  # (正向边所在节点, 边索引, low)
    b = {nid: int(balances.get(nid, 0)) for nid in node_ids}
    required = 0
    for u, v, low, high in arcs:
        iu, iv = index[u], index[v]
        edge_idx = dinic.add_arc(iu, iv, high - low)
        arc_edges.append((iu, edge_idx, low))
        b[u] -= low
        b[v] += low

    for nid in node_ids:
        need = b[nid]
        if need > 0:
            dinic.add_arc(ss, index[nid], need)
            required += need
        elif need < 0:
            dinic.add_arc(index[nid], tt, -need)

    flow_total = dinic.max_flow(ss, tt)
    if flow_total != required:
        return False, []

    # 下界可行流中弧流量 = low + 该正向弧在超级源流中实际承担的净流量
    # （用 add_arc 维护的净流量计，独立反向弧不会污染该值）
    flows: list[int] = []
    for iu, edge_idx, low in arc_edges:
        used = dinic.graph[iu][edge_idx][4]
        flows.append(low + used)
    return True, flows


def _residual_max_flow(
    node_ids: list[str],
    residual_arcs: list[tuple[str, str, int]],
    source_id: str,
    sink_id: str,
) -> int:
    """在给定有向残量弧（u,v,剩余容量）上求 source_id→sink_id 最大流。

    每条残量弧按标准配对有向弧添加（自带反向回撤边）：这里的残量弧本身就是
    某条有向弧的一个方向剩余容量，其对向回撤由 Dinic 的配对反向边承载。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    dinic = _Dinic(len(node_ids))
    for u, v, cap in residual_arcs:
        if cap > 0:
            dinic.add_arc(index[u], index[v], cap)
    return dinic.max_flow(index[source_id], index[sink_id])


def reconcile_meters(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    injections: list[tuple[str, int]],
    withdrawals: list[tuple[str, int]],
    pipe_readings: list[tuple[str, int, int | None]],
    *,
    blocked_pipe_ids: frozenset[str] = frozenset(),
) -> ReconcileResult:
    """按当前水源运行状态、关阀与待修复故障核对管段流量计读数（只读）。

    injections：(水源编号, 实际注入量)；withdrawals：(用水点编号, 实际取用量)；
    pipe_readings：(管段编号, 双向共用容量, 带符号读数或 None)。
    读数以管段 from→to 为正；None 表示该管段缺报，需推断可行流量范围。
    blocked_pipe_ids：已关闭阀门与待修复故障管段，流量必须为 0
    （与可达性/流量核算口径完全一致）。调用方须先过 validate_reconcile_request。

    带符号流量 f∈[-c,c] 做偏移 y=f+c∈[0,2c]（等价于先强制 v→u 流 c）；
    已申报读数直接固定（f=reading，y 固定），缺报弧留作自由变量。
    """
    injection_map = dict(injections)
    withdrawal_map = dict(withdrawals)
    reading_map = {pid: (cap, reading) for pid, cap, reading in pipe_readings}

    # 硬约束（先于可行流求解，直接给出可读原因）：
    # 停运水源注入必须为 0；阻断管段流量必须为 0；|读数| <= 容量。
    hard_reasons: list[ReconcileReason] = []
    for nid in sorted(injection_map):
        node = nodes[nid]
        if not node.running and injection_map[nid] != 0:
            hard_reasons.append(
                ReconcileReason(
                    STOPPED_SOURCE_INJECTION,
                    f"水源 {nid} 已停运，实际注入量必须为 0，申报为 "
                    f"{injection_map[nid]}",
                    node_id=nid,
                )
            )

    for pid in sorted(reading_map):
        cap, reading = reading_map[pid]
        if pid in blocked_pipe_ids:
            if reading not in (None, 0):
                hard_reasons.append(
                    ReconcileReason(
                        BLOCKED_PIPE_FLOW,
                        f"管段 {pid} 已关闭阀门或为待修复故障管段，流量必须为 0，"
                        f"读数为 {reading}",
                        pipe_id=pid,
                    )
                )
        elif reading is not None and abs(reading) > cap:
            hard_reasons.append(
                ReconcileReason(
                    READING_OVER_CAPACITY,
                    f"管段 {pid} 读数 {reading} 的绝对值超过双向共用容量 {cap}",
                    pipe_id=pid,
                )
            )

    if hard_reasons:
        return ReconcileResult(consistent=False, readings=[], reasons=hard_reasons)

    node_ids = list(nodes.keys())
    # b(n) = 注入 - 取用（junction 恒为 0）
    balances = {
        nid: int(injection_map.get(nid, 0)) - int(withdrawal_map.get(nid, 0))
        for nid in node_ids
    }

    # 注入总量与取用总量不守恒时，任何节点平衡都无法同时成立 -> 无可行解
    if sum(injection_map.values()) != sum(withdrawal_map.values()):
        return ReconcileResult(
            consistent=False,
            readings=[],
            reasons=[
                ReconcileReason(
                    READINGS_INCONSISTENT,
                    f"水源实际注入总量 {sum(injection_map.values())} 与用水点实际取用"
                    f"总量 {sum(withdrawal_map.values())} 不相等，节点平衡无法全部成立",
                )
            ],
        )

    arcs: list[tuple[str, str, int, int]] = []
    reported: dict[str, int] = {}
    missing: list[str] = []
    # 带符号流量 f∈[-c,c] 偏移为 y=f+c∈[0,2c]：偏移量等价于一条 v→u 的
    # 固定流 c，因此对**每条容量 c 的管段**（含阻断/固定读数）都先平移节点
    # 平衡（u 的 y 净出多 c、v 少 c），再由 y 弧的下界承载实际值。
    shifted_balances = dict(balances)
    for pid in sorted(pipes):
        pipe = pipes[pid]
        cap, reading = reading_map[pid]
        shifted_balances[pipe.from_node] = (
            shifted_balances.get(pipe.from_node, 0) + cap
        )
        shifted_balances[pipe.to_node] = (
            shifted_balances.get(pipe.to_node, 0) - cap
        )
        if pid in blocked_pipe_ids or cap == 0:
            # 阻断管段或容量 0：f=0，y=c 固定
            arcs.append((pipe.from_node, pipe.to_node, cap, cap))
            if reading is None:
                missing.append(pid)
        elif reading is None:
            # 缺报自由弧：f∈[-c,c] -> y∈[0,2c]
            arcs.append((pipe.from_node, pipe.to_node, 0, 2 * cap))
            missing.append(pid)
        else:
            # 已申报读数固定：f=reading -> y=reading+cap
            y_fixed = reading + cap
            arcs.append((pipe.from_node, pipe.to_node, y_fixed, y_fixed))
            reported[pid] = reading

    feasible, arc_flows = _bounded_feasible_flow(
        node_ids, arcs, shifted_balances
    )
    if not feasible:
        return ReconcileResult(
            consistent=False,
            readings=[],
            reasons=[
                ReconcileReason(
                    READINGS_INCONSISTENT,
                    "当前水源运行状态、关阀与待修复故障下，注入、取用、容量与已申报"
                    "读数无法同时满足节点平衡与容量约束，无可行流量分配",
                )
            ],
        )

    # 一组可行流（偏移量）：y -> f = y - c（arcs 按 sorted(pipes) 顺序构建）
    flows: dict[str, int] = {}
    for pid, y_flow in zip(sorted(pipes), arc_flows):
        cap = reading_map[pid][0]
        flows[pid] = y_flow - cap
    # 已申报弧恢复后必须等于读数（下界=上界，数值上恒成立）
    for pid, reading in reported.items():
        assert flows[pid] == reading, (pid, flows[pid], reading)

    # 缺报范围：以可行流构造有向残量网（残量=上界-当前 y、下界方向=当前 y），
    # 剔除目标缺报弧自身后，两次最大流求该弧 y 可增/可减的最大量。
    free_pipes = [
        pid
        for pid in sorted(pipes)
        if pid not in blocked_pipe_ids and reading_map[pid][0] > 0
        and reading_map[pid][1] is None
    ]

    def residual_arcs_excluding(target: str) -> list[tuple[str, str, int]]:
        """其余自由缺报管段在当前可行流（偏移 y=f+c∈[0,2c]）下的残量弧。

        每条自由弧是 [0,2c] 上的独立有向弧：正向（from→to）可增 2c-y，
        反向（to→from）可回撤 y。平行管段（同一对节点多条管段）各自独立。
        """
        out: list[tuple[str, str, int]] = []
        for pid in free_pipes:
            if pid == target:
                continue
            pipe = pipes[pid]
            cap = reading_map[pid][0]
            y = flows[pid] + cap
            out.append((pipe.from_node, pipe.to_node, 2 * cap - y))
            out.append((pipe.to_node, pipe.from_node, y))
        return out

    ranges: list[MeterRange] = []
    for pid in sorted(missing):
        pipe = pipes[pid]
        cap = reading_map[pid][0]
        if pid in blocked_pipe_ids or cap == 0:
            ranges.append(
                MeterRange(pipe_id=pid, min_flow=0, max_flow=0, determinable=True)
            )
            continue

        residual = residual_arcs_excluding(pid)
        f0 = flows[pid]
        y0 = f0 + cap
        # 残量环流定理：增大 u→v 弧流量需要其余边提供一条 v→u 的补偿路径，
        # 减小则需要 u→v 路径；增量同时受该弧自身上界 2c / 下界 0 限制。
        inc = min(
            2 * cap - y0,
            _residual_max_flow(node_ids, residual, pipe.to_node, pipe.from_node),
        )
        dec = min(
            y0,
            _residual_max_flow(node_ids, residual, pipe.from_node, pipe.to_node),
        )
        f_min = f0 - dec
        f_max = f0 + inc
        ranges.append(
            MeterRange(
                pipe_id=pid,
                min_flow=f_min,
                max_flow=f_max,
                determinable=f_min == f_max,
            )
        )

    return ReconcileResult(consistent=True, readings=ranges, reasons=[])


# ---------------------------------------------------------------- 调度演练

# 演练步骤类型
DRILL_STEP_ISOLATION = "isolation"          # 隔离：故障管段 + 拟关阀门
DRILL_STEP_RESTORE = "restore"              # 复供：现存事件编号或临时事件引用
DRILL_STEP_SOURCE_SWITCH = "source_switch"  # 水源切换：一批水源目标运行状态


@dataclass
class DrillStep:
    """演练单步入参：type 决定有效字段（隔离 / 复供 / 水源切换）。"""

    type: str
    # 隔离步骤：故障管段 + 拟关阀门
    fault_pipe_id: str | None = None
    close_pipe_ids: list[str] = field(default_factory=list)
    # 复供步骤：现存事件编号 或 本次演练临时事件引用（"T<步骤序号>"），二者必居其一
    event_id: int | None = None
    temp_event_ref: str | None = None
    # 水源切换步骤：(水源编号, 目标运行状态)
    targets: list[tuple[str, bool]] = field(default_factory=list)


@dataclass
class DrillStepError:
    """演练单步失败原因：原因码 + 明细（结构问题 issues / 重新进水风险 blocks）。"""

    code: str
    message: str
    issues: list[Issue] = field(default_factory=list)
    # ReopenBlock / SourceSwitchBlock（复供、水源切换的重新进水风险）
    blocks: list = field(default_factory=list)


@dataclass
class DrillStepOutcome:
    """演练单步结果。

    ok=True 时给出相对上一步结束状态的新增失供（affected_sink_ids）与
    恢复供水（restored_sink_ids）用水点；ok=False 时 error 给出首个失败原因。
    """

    index: int
    type: str
    ok: bool
    affected_sink_ids: list[str] = field(default_factory=list)
    restored_sink_ids: list[str] = field(default_factory=list)
    # 隔离步骤回显：故障管段、拟关阀门、产生的临时事件引用
    fault_pipe_id: str | None = None
    close_pipe_ids: list[str] | None = None
    temp_event_ref: str | None = None
    # 复供步骤回显：引用的事件与重开的阀门
    event_id: int | None = None
    reopened_pipe_ids: list[str] | None = None
    # 水源切换步骤回显：目标状态与是否有实际变化
    targets: list[tuple[str, bool]] | None = None
    changed: bool | None = None
    error: DrillStepError | None = None


@dataclass
class DrillFinalState:
    """全部步骤成功后的最终模拟状态（推演结果，未真实生效）。"""

    sources: list[tuple[str, bool]]  # (水源编号, 运行状态)，按编号排序
    valves: list[tuple[str, bool]]   # (管段编号, 阀门是否关闭)，全部有阀管段，按编号排序
    # (现存事件编号|None, 临时事件引用|None, 故障管段编号)：现存事件按编号升序，
    # 临时事件按产生步骤序号升序
    open_faults: list[tuple[int | None, str | None, str]]
    supplied_sink_ids: list[str]     # 由运行水源可达的用水点（通水用水点）


@dataclass
class DrillResult:
    """调度演练结果（只读计算，不改任何状态）。

    ok=False 时 failed_step_index 为首个失败步骤序号（从 1 起），steps 只包含
    到该步骤为止的结果（后续步骤不再演练），final 为 None。
    """

    ok: bool
    failed_step_index: int | None
    steps: list[DrillStepOutcome] = field(default_factory=list)
    final: DrillFinalState | None = None


def simulate_drill(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    real_events: dict[int, dict],
    steps: list[DrillStep],
) -> DrillResult:
    """按顺序只读演练一串隔离/复供/水源切换操作（不改任何真实状态）。

    real_events：现存事件（事件编号 -> 事件 dict，含 fault_pipe_id /
    closed_pipe_ids / status）；待修复(open)事件的故障管段在通水计算中阻断，
    已复供(restored)事件仅用于复供步骤的精确报错（EVENT_ALREADY_RESTORED）。

    输入的 nodes/pipes/real_events 不会被修改（全部状态演进发生在内部副本上）。
    """
    sim_nodes = dict(nodes)
    sim_pipes = dict(pipes)

    # 模拟事件表：("id", 现存事件编号) / ("temp", 临时事件引用) -> 事件状态
    sim_events: dict[tuple[str, object], dict] = {}
    for event_id, event in real_events.items():
        sim_events[("id", event_id)] = {
            "fault_pipe_id": event["fault_pipe_id"],
            "closed_pipe_ids": list(event["closed_pipe_ids"]),
            "status": event["status"],
            "temp_index": None,
        }

    def pending_fault_pipe_ids() -> frozenset[str]:
        return frozenset(
            ev["fault_pipe_id"]
            for ev in sim_events.values()
            if ev["status"] == "open"
        )

    outcomes: list[DrillStepOutcome] = []

    for index, step in enumerate(steps, start=1):
        outcome: DrillStepOutcome
        if step.type == DRILL_STEP_ISOLATION:
            close_ids = list(step.close_pipe_ids)
            # 与提交接口同一口径：for_submit=True（已关闭阀门不得重复关闭等）
            plan = evaluate_plan(
                sim_nodes,
                sim_pipes,
                step.fault_pipe_id,
                close_ids,
                for_submit=True,
                pending_fault_pipe_ids=pending_fault_pipe_ids(),
            )
            blocking = [i for i in plan.issues if i.code in BLOCKING_ISSUE_CODES]
            if blocking:
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=False,
                    fault_pipe_id=step.fault_pipe_id,
                    close_pipe_ids=close_ids,
                    error=DrillStepError(
                        PLAN_INVALID,
                        "隔离方案非法（管段不存在、无阀管段、重复关闭或把故障管段当作阀门）",
                        issues=blocking,
                    ),
                )
            elif not plan.isolatable:
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=False,
                    fault_pipe_id=step.fault_pipe_id,
                    close_pipe_ids=close_ids,
                    error=DrillStepError(
                        NOT_ISOLATABLE,
                        "该方案无法隔离故障管段：故障管段至少有一端在方案图中仍可从水源到达",
                        issues=plan.issues,
                    ),
                )
            else:
                # 应用本步：关闭拟关阀门，登记临时隔离事件（待修复）
                for pid in dict.fromkeys(close_ids):
                    p = sim_pipes[pid]
                    sim_pipes[pid] = Pipe(
                        p.id, p.from_node, p.to_node, p.has_valve, True, p.name
                    )
                temp_ref = f"T{index}"
                sim_events[("temp", temp_ref)] = {
                    "fault_pipe_id": step.fault_pipe_id,
                    "closed_pipe_ids": list(dict.fromkeys(close_ids)),
                    "status": "open",
                    "temp_index": index,
                }
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=True,
                    fault_pipe_id=step.fault_pipe_id,
                    close_pipe_ids=close_ids,
                    temp_event_ref=temp_ref,
                    affected_sink_ids=plan.affected_sink_ids,
                    restored_sink_ids=[],
                )
        elif step.type == DRILL_STEP_RESTORE:
            if step.event_id is not None:
                key: tuple[str, object] = ("id", step.event_id)
                label = str(step.event_id)
            else:
                key = ("temp", step.temp_event_ref)
                label = str(step.temp_event_ref)
            event = sim_events.get(key)
            if event is None:
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=False,
                    event_id=step.event_id,
                    temp_event_ref=step.temp_event_ref,
                    error=DrillStepError(
                        EVENT_NOT_FOUND, f"隔离事件不存在：{label}"
                    ),
                )
            elif event["status"] != "open":
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=False,
                    event_id=step.event_id,
                    temp_event_ref=step.temp_event_ref,
                    error=DrillStepError(
                        EVENT_ALREADY_RESTORED,
                        f"隔离事件 {label} 已复供，不能重复操作",
                    ),
                )
            else:
                other_faults = frozenset(
                    ev["fault_pipe_id"]
                    for k, ev in sim_events.items()
                    if k != key and ev["status"] == "open"
                )
                restore = evaluate_restore(
                    sim_nodes,
                    sim_pipes,
                    reopen_pipe_ids=frozenset(event["closed_pipe_ids"]),
                    other_fault_pipe_ids=other_faults,
                )
                if not restore.safe:
                    outcome = DrillStepOutcome(
                        index=index,
                        type=step.type,
                        ok=False,
                        event_id=step.event_id,
                        temp_event_ref=step.temp_event_ref,
                        error=DrillStepError(
                            RESTORE_UNSAFE,
                            "复供存在重新进水风险：其他待修复故障管段的端点将重新通水",
                            blocks=restore.blocks,
                        ),
                    )
                else:
                    # 应用本步：重开该事件阀门（与复供确认同口径），事件置为已复供
                    for pid in event["closed_pipe_ids"]:
                        p = sim_pipes.get(pid)
                        if p is not None and p.has_valve:
                            sim_pipes[pid] = Pipe(
                                p.id, p.from_node, p.to_node,
                                p.has_valve, False, p.name,
                            )
                    event["status"] = "restored"
                    outcome = DrillStepOutcome(
                        index=index,
                        type=step.type,
                        ok=True,
                        event_id=step.event_id,
                        temp_event_ref=step.temp_event_ref,
                        reopened_pipe_ids=list(event["closed_pipe_ids"]),
                        affected_sink_ids=[],
                        restored_sink_ids=restore.restored_sink_ids,
                    )
        else:  # DRILL_STEP_SOURCE_SWITCH
            targets = list(step.targets)
            switch = evaluate_source_switch(
                sim_nodes,
                sim_pipes,
                targets,
                pending_fault_pipe_ids=pending_fault_pipe_ids(),
            )
            if switch.issues:
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=False,
                    targets=targets,
                    error=DrillStepError(
                        SOURCE_SWITCH_INVALID,
                        "水源切换请求非法（编号不存在、节点不是水源或同批编号重复）",
                        issues=switch.issues,
                    ),
                )
            elif not switch.safe:
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=False,
                    targets=targets,
                    error=DrillStepError(
                        SOURCE_SWITCH_UNSAFE,
                        "水源切换存在重新进水风险：恢复水源后待修复故障管段的端点将重新通水",
                        blocks=switch.blocks,
                    ),
                )
            else:
                # 应用本步：仅水源运行状态变化（无实际变化时为空操作，状态不变）
                target_map = dict(targets)
                sim_nodes = {
                    nid: Node(
                        n.id, n.type, n.name, target_map.get(nid, n.running)
                    )
                    for nid, n in sim_nodes.items()
                }
                outcome = DrillStepOutcome(
                    index=index,
                    type=step.type,
                    ok=True,
                    targets=targets,
                    changed=switch.changed,
                    affected_sink_ids=switch.affected_sink_ids,
                    restored_sink_ids=switch.restored_sink_ids,
                )
        outcomes.append(outcome)
        if not outcome.ok:
            # 首个失败步骤：标明原因并停止后续演练，不产出最终状态
            return DrillResult(
                ok=False, failed_step_index=index, steps=outcomes, final=None
            )

    # 全部步骤成功：汇总最终状态（运行水源 / 阀位 / 待修复故障 / 通水用水点）
    blocked = frozenset(
        p.id for p in sim_pipes.values() if p.has_valve and p.valve_closed
    ) | pending_fault_pipe_ids()
    real_open = sorted(
        key[1]
        for key, ev in sim_events.items()
        if key[0] == "id" and ev["status"] == "open"
    )
    temp_open = sorted(
        (ev["temp_index"], key[1])
        for key, ev in sim_events.items()
        if key[0] == "temp" and ev["status"] == "open"
    )
    open_faults = [
        (event_id, None, sim_events[("id", event_id)]["fault_pipe_id"])
        for event_id in real_open
    ] + [
        (None, temp_ref, sim_events[("temp", temp_ref)]["fault_pipe_id"])
        for _temp_index, temp_ref in temp_open
    ]
    final = DrillFinalState(
        sources=sorted(
            (nid, n.running) for nid, n in sim_nodes.items() if n.type == "source"
        ),
        valves=sorted(
            (p.id, p.valve_closed) for p in sim_pipes.values() if p.has_valve
        ),
        open_faults=open_faults,
        supplied_sink_ids=reachable_sink_ids(
            sim_nodes, list(sim_pipes.values()), blocked
        ),
    )
    return DrillResult(ok=True, failed_step_index=None, steps=outcomes, final=final)


# ---------------------------------------------------------------- 状态快照


def reachable_sink_ids(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe] | list[Pipe],
    removed_pipe_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """返回由运行中水源可达的用水点编号（按编号排序）。

    快照冻结“供水点”时统一使用该结果：只从 running=True 的水源出发，
    已关闭阀门与待修复故障管段视为不可通行（由调用方计入 removed_pipe_ids）。
    """
    pipe_list = list(pipes.values()) if isinstance(pipes, dict) else list(pipes)
    reachable = _reachable(nodes, pipe_list, removed_pipe_ids)
    return sorted(
        nid for nid, node in nodes.items() if node.type == "sink" and nid in reachable
    )


def freeze_state(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    open_faults: list[tuple[int, str]] | None = None,
) -> dict:
    """把当前领域状态冻结为不可变（JSON 可序列化）的快照载荷。

    open_faults：(事件编号, 故障管段编号) 列表，即全部待修复(open)故障，
    按事件编号排序；快照据此记录“当时待修复故障”，不引用后续事件回填。

    载荷中的 supplied_sink_ids 为“由运行水源可达的用水点”，计算时排除
    已关闭阀门与待修复故障管段，与隔离/复供/切换的通水口径完全一致。
    """
    frozen_nodes = [
        {
            "id": node.id,
            "type": node.type,
            "name": node.name,
            "running": node.running,
        }
        for node in (nodes[nid] for nid in sorted(nodes))
    ]
    frozen_pipes = [
        {
            "id": pipe.id,
            "from": pipe.from_node,
            "to": pipe.to_node,
            "has_valve": pipe.has_valve,
            "valve_closed": pipe.valve_closed,
            "name": pipe.name,
        }
        for pipe in (pipes[pid] for pid in sorted(pipes))
    ]
    blocked = frozenset(
        p.id for p in pipes.values() if p.has_valve and p.valve_closed
    ) | frozenset(fault_pid for _eid, fault_pid in (open_faults or []))
    frozen_faults = [
        {"event_id": event_id, "fault_pipe_id": fault_pipe_id}
        for event_id, fault_pipe_id in sorted(open_faults or [])
    ]
    return {
        "nodes": frozen_nodes,
        "pipes": frozen_pipes,
        "open_faults": frozen_faults,
        "supplied_sink_ids": reachable_sink_ids(nodes, list(pipes.values()), blocked),
    }


def _node_index(state: dict) -> dict[str, dict]:
    return {n["id"]: n for n in state.get("nodes", [])}


def _pipe_index(state: dict) -> dict[str, dict]:
    return {p["id"]: p for p in state.get("pipes", [])}


def _fault_index(state: dict) -> dict[int, dict]:
    return {f["event_id"]: f for f in state.get("open_faults", [])}


def diff_states(base: dict | None, target: dict) -> dict:
    """对照两份冻结状态，返回六类净变化。

    - nodes：节点新增/移除（整网导入为替换式，通常整批增删）；
    - pipes：管段新增/移除；
    - valve_positions：共有管段中阀门位置（开/关）变化；
    - source_states：共有水源节点运行(running)状态变化；
    - open_faults：待修复故障（按事件编号）新增/移除；
    - supplied_sinks：由运行水源可达用水点集合的新增/移除（净变化）。

    base 为 None（首条快照没有更早基线）时，目标状态全部计为新增。
    历史结果只取自两份冻结载荷，不读取当前管网或后续事件。
    """
    base = base or {}

    base_nodes, target_nodes = _node_index(base), _node_index(target)
    base_pipes, target_pipes = _pipe_index(base), _pipe_index(target)
    base_faults, target_faults = _fault_index(base), _fault_index(target)

    node_added = [target_nodes[nid] for nid in sorted(target_nodes.keys() - base_nodes.keys())]
    node_removed = [base_nodes[nid] for nid in sorted(base_nodes.keys() - target_nodes.keys())]

    pipe_added = [target_pipes[pid] for pid in sorted(target_pipes.keys() - base_pipes.keys())]
    pipe_removed = [base_pipes[pid] for pid in sorted(base_pipes.keys() - target_pipes.keys())]

    valve_changes: list[dict] = []
    for pid in sorted(base_pipes.keys() & target_pipes.keys()):
        before_p, after_p = base_pipes[pid], target_pipes[pid]
        if (
            before_p.get("has_valve")
            and after_p.get("has_valve")
            and bool(before_p.get("valve_closed")) != bool(after_p.get("valve_closed"))
        ):
            valve_changes.append(
                {
                    "pipe_id": pid,
                    "valve_closed_before": bool(before_p.get("valve_closed")),
                    "valve_closed_after": bool(after_p.get("valve_closed")),
                }
            )

    source_changes: list[dict] = []
    for nid in sorted(base_nodes.keys() & target_nodes.keys()):
        before_n, after_n = base_nodes[nid], target_nodes[nid]
        if (
            before_n.get("type") == "source"
            and after_n.get("type") == "source"
            and bool(before_n.get("running")) != bool(after_n.get("running"))
        ):
            source_changes.append(
                {
                    "source_id": nid,
                    "running_before": bool(before_n.get("running")),
                    "running_after": bool(after_n.get("running")),
                }
            )

    fault_added = [
        target_faults[eid] for eid in sorted(target_faults.keys() - base_faults.keys())
    ]
    fault_removed = [
        base_faults[eid] for eid in sorted(base_faults.keys() - target_faults.keys())
    ]

    base_sinks = set(base.get("supplied_sink_ids", []))
    target_sinks = set(target.get("supplied_sink_ids", []))
    sink_added = sorted(target_sinks - base_sinks)
    sink_removed = sorted(base_sinks - target_sinks)

    return {
        "nodes": {"added": node_added, "removed": node_removed},
        "pipes": {"added": pipe_added, "removed": pipe_removed},
        "valve_positions": {"changed": valve_changes},
        "source_states": {"changed": source_changes},
        "open_faults": {"added": fault_added, "removed": fault_removed},
        "supplied_sinks": {"added": sink_added, "removed": sink_removed},
    }
