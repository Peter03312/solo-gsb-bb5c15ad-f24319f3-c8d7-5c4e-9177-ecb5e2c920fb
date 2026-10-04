# 供水管网故障隔离与复供服务

基于 **Python 3.12 + FastAPI + PostgreSQL** 的供水管网故障隔离与复供服务。

支持录入带编号的**水源(source)**、**用水点(sink)**、**连接点(junction)**及两端节点明确的**管段**（管段可设阀门）；
水源有**运行(running)/停运(stopped)**状态：既有及整网导入的水源初始视为运行，
操作员可按已读版本对一批水源做停运/恢复的只读预演与原子确认；
操作员只知道**故障管段编号**时，可请求**关阀建议**：按当前管网只读计算一套可直接提交的关阀方案；
针对一条故障管段，预演并提交阀门关闭方案，自动判断**能否隔离**并计算**失供用水点**；
隔离提交后生成带编号的隔离事件（区分**待修复 open / 已复供 restored**），
修复完成后操作员按**事件编号 + 当前管网版本**预演并确认**复供**，系统复核重开阀门后其余待修复故障是否仍被安全隔离；
**整网导入、隔离提交、复供确认、水源切换**每次实际改变状态时，在**同一事务**内冻结一条
不可复用编号的**操作后状态快照**，并在同一事务内接入连续的 **SHA-256 摘要链**
（首条使用固定创世摘要；首次启用时为现存快照补建链并标明建链起点）；
供调度员按操作顺序回看当时的节点、水源运行状态、
管段阀位、待修复故障与由运行水源可达的用水点，支持相邻快照六类净变化对照，
调度审计员还可凭事先保存的起点前、终点摘要**只读核验**任意区间，定位首个
内容损坏或链路断裂位置（回滚编号空洞不算断链，整网重导不断链，并发写入不分叉）。
调度员还可在**现有阀位与故障状态**下携带各水源供水上限、各管段双向共用输水上限与
用水点最低需求，**只读核算**管网能否满足这组用水点的最低流量（整数最大流）；
供水不足时还可携带各用水点的**保障等级**（数字越小越优先），**只读核算分档交付**：
先按等级依次最大化该等级交付总量，再在各等级总量最优条件下按（等级、用水点编号）
逐点最大化交付量，返回各点交付与缺口，结果由一组同时可行的整数流量实现。
调度员在执行前还可把一串**有序的隔离、复供、水源切换操作**交给**调度演练**接口
**只读预检**：以当前状态为起点逐步套用既有提交校验，复供可引用现存待修复事件或
本次演练先前隔离产生的**临时事件**，逐步给出新增失供/恢复供水用水点与最终
运行水源、阀位、待修复故障、通水用水点，首个非法或不安全步骤即标明原因并停止。
抢修期间还可评估一条**临时旁通管**的供水效果：输入已读版本、两个现有节点、
旁通管双向共用容量与覆盖全网的水源/管段容量、带保障等级的用水需求，
服务先按当前状态核算**基线**分档交付，再**仅在内存中**接入旁通（可与现有管段并联）——
若接入后任一待修复故障管段的任一端从运行水源可达（重新进水风险），
判为**不可用**并返回故障编号与危险端点，不输出旁通交付量；
安全时返回基线与接入旁通后的各等级、各用水点交付量及差值，全程只读。

> 全部通水计算（读网、关阀建议、隔离预演/提交、复供预演/确认、水源切换预演、
> 流量核算、分档交付、调度演练、临时旁通评估）只以**运行中**的水源为供水起点；停运水源不再供水。

---

## 一、快速启动（Docker Compose 一键启动）

前置条件：安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build
```

该命令会：

1. 启动 PostgreSQL 16（端口 `5432`），并通过 `healthcheck` 等待数据库就绪；
2. 构建并启动 API 服务（端口 `8000`）；
3. API 启动时自动执行 `CREATE TABLE IF NOT EXISTS ...` 完成数据库初始化（无需手工建表）。

停止：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时清空数据库数据卷
```

## 二、服务地址与端口

| 服务 | 地址 |
| --- | --- |
| API 基础地址 | <http://localhost:8000> |
| 健康检查 | <http://localhost:8000/health> |
| Swagger 交互文档 | <http://localhost:8000/docs> |
| ReDoc 文档 | <http://localhost:8000/redoc> |
| OpenAPI Schema | <http://localhost:8000/openapi.json> |
| PostgreSQL | `localhost:5432`（库/用户/密码默认均为 `water`） |

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://water:water@localhost:5432/water` | PostgreSQL 连接串；Compose 中指向 `db` 服务 |
| `DB_CONNECT_RETRIES` | `30` | 启动时等待数据库就绪的重试次数 |
| `DB_CONNECT_RETRY_INTERVAL` | `1.0` | 重试间隔秒数 |
| `HISTORY_LIMIT` | `100` | 历史记录接口返回上限 |

### 本地（非 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 确保 PostgreSQL 已建库（create database water;）
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## 四、核心业务规则

**导入校验**（任一不满足即整体拒绝，返回全部问题明细，不写入数据）：

- 管段两端节点必须存在 —— 拒绝**悬空端点**；
- 节点编号、管段编号分别不可重复 —— 拒绝**重号**；
- 管段两端不可为同一节点 —— 拒绝**自环**；
- 没有阀门的管段不能标记为已关闭。

导入（含既有库）的**水源初始视为运行**；水源运行状态只能通过水源切换接口改变。

**隔离判定**（方案图 = 移除故障管段 + 关闭拟关阀门，**已关闭阀门继续生效**）：

- 仅当故障管段**两端**在方案图中都**无法从任一水源到达**，方案才 `isolatable=true`，方可提交；
- 失供用水点 = **方案前**由任一水源可达、**方案后**不可达的用水点，按编号（字符串字典序）排序；
  方案前已经失供的点不会重复计入。
- 其他**待修复(open)**隔离事件的故障管段在方案前/方案后均视为不可通行（故障仍在，不能通水）；
  已复供(restored)事件的故障管段不再计入。

**提交约束**（任一不满足即拒绝且不改任何状态）：

- `version` 必须等于当前管网版本，否则 `409 VERSION_CONFLICT`（版本过期）；
- 故障管段、拟关管段必须存在；拟关管段必须**带阀门**；
- 拟关列表不得重复、不得包含故障管段本身；已关闭的阀门不得重复关闭；
- 方案必须可隔离（`isolatable=true`），否则 `422 NOT_ISOLATABLE`；
- 提交在**单个数据库事务**内完成：行级锁校验版本 → 更新阀位 → 版本号 +1 → 写隔离事件（状态 `open`），原子生效。
- 整网导入为替换式：重写全部节点/管段、清空历史、版本重置为 `1`（旧事件随之失效）。

**关阀建议**（`POST /api/isolation/recommend`，**只读**，不改阀位/版本/事件）：

- 入参只需 `fault_pipe_id` 与已读 `version`；返回当前版本、`version_match`、
  拟关闭的尚未关闭阀门（`recommended_close_pipe_ids`）与预计失供用水点（`affected_sink_ids`）；
- 已关闭阀门与其他待修复事件的故障管段均不可通行；故障管段**两端**都须与所有水源断开；
- 本次只从**尚未关闭的有阀管段**中选取，且不含故障管段本身（与提交接口约束一致，保证建议可直接提交）；
- 优选顺序：**新增失供用水点数最少** → **本次关阀数最少** → 关阀编号序列（排序后）**字典序最小**；
- 故障管段不存在（`FAULT_PIPE_NOT_FOUND`）或关闭全部可用阀门仍无法隔离（`NO_FEASIBLE_PLAN`）时，
  返回 `feasible=false` 并在 `reason` 中给出原因，**不改任何状态**；
- 建议的提交仍走现有 `POST /api/isolation/submit`，按最新状态复核；
  版本过期沿用 `409 VERSION_CONFLICT` 冲突处理（重新建议后再提交即可）。

**复供判定**（操作员按 `事件编号 + 当前版本` 操作；复供图 = 重开该事件阀门，其他待修复故障管段继续移除）：

- 复供预演 `POST /api/isolation/events/{event_id}/restore/preview` 为**只读**：
  返回该事件所关阀门（`closed_pipe_ids`）、预计恢复供水的用水点（`restored_sink_ids`）
  及阻断原因（`block_reasons`），**不改变事件状态、阀位与版本**；
- 仅当重开本事件阀门后，**其余每条待修复故障管段的两端仍均无法从水源到达**，才 `safe=true`，方可确认；
  任一待修复故障有任一端重新进水，即存在**重新进水风险**，返回阻断原因
  `FAULT_REWATER_RISK`（含故障管段编号与重新进水的端点 `reachable_endpoints`）；
- 计算通水时排除：其他待修复事件的故障管段、以及**仍关闭的阀门**（仅重开本事件所关阀门）；
- 复供确认 `POST /api/isolation/events/{event_id}/restore` 在**同一事务**内：
  锁定事件行与版本行 → 复核版本 → 复核安全性 → 重开该事件阀门 → 版本 +1 → 事件置为 `restored`，原子生效。
- 复供明确拒绝（均不产生部分变更）：
  版本过期 `409 VERSION_CONFLICT`、事件不存在 `404 EVENT_NOT_FOUND`、
  事件已复供 `409 EVENT_ALREADY_RESTORED`、重新进水风险 `422 RESTORE_UNSAFE`。

**水源状态切换**（`POST /api/sources/switch/preview` 只读预演、`POST /api/sources/switch` 原子确认）：

- 入参为一批 `{source_id, running}`（`running=false` 停运、`running=true` 恢复）加已读 `version`；
  **仅水源节点**可切换，同批编号**不得重复**；
- 既有及整网导入的水源**初始视为运行**；`GET /api/network` 的节点增量返回水源运行状态 `running`；
- 预演返回当前版本、`version_match`、是否有实际变化 `changed`、是否安全 `safe`、
  切换后**新增失供**用水点 `affected_sink_ids`（切换前可达 → 切换后不可达）、
  **恢复供水**用水点 `restored_sink_ids`（切换前不可达 → 切换后可达），
  以及每条**待修复故障管段**的重新进水风险（`block_reasons`，代码同为 `FAULT_REWATER_RISK`，
  含故障管段编号与重新进水的端点 `reachable_endpoints`）；编号无效等结构问题在 `issues` 中给出；
- 通水时**已关闭阀门与待修复故障管段仍阻断**；切换只改变水源运行状态，不改管网结构；
- 停运只会收缩可达范围，**停运可导致全网无运行水源**（此时所有用水点失供，不额外报错）；
  恢复水源后，若任何待修复故障管段的**任一端**可从运行水源到达，`safe=false`、禁止确认；
- **全部目标状态已满足时不推进版本**：确认返回 `changed=false` 且版本不变；
  有实际变化且安全时版本 +1；
- 确认在**单个数据库事务**内完成：锁定版本行 → 按最新状态复核版本 → 复核结构与安全性 →
  原子更新水源运行状态、版本 +1；
- 水源切换明确拒绝（均不产生部分变更）：
  版本过期 `409 VERSION_CONFLICT`、
  编号不存在 `SOURCE_NOT_FOUND` / 节点不是水源 `SOURCE_NOT_SWITCHABLE` /
  同批编号重复 `SOURCE_TARGET_DUPLICATE`（确认时统一为 `422 SOURCE_SWITCH_INVALID`，
  明细在 `issues`；预演不拒绝，返回 200 并在 `issues` 中列出）、
  重新进水风险 `422 SOURCE_SWITCH_UNSAFE`。

**流量核算**（`POST /api/delivery/evaluate`，**只读**，不改阀位/事件/版本/快照）：

- 入参为已读 `version` + **每个水源**的供水上限 `sources` + **每条管段**的双向共用
  输水上限 `pipes` + **非空**用水点需求 `demands`；所有流量均为**非负整数升/小时**
  （布尔值、小数、负数均按类型/数值错误处理，`422` 整体拒绝）。
- 编号规则（任一不满足即整体拒绝，`422 DELIVERY_REQUEST_INVALID`，返回全部问题明细）：
  - 水源、管段编号必须**完整覆盖当前管网**（不多不少）：遗漏报
    `DELIVERY_SOURCE_MISSING` / `DELIVERY_PIPE_MISSING`；
    编号不存在报 `DELIVERY_SOURCE_NOT_FOUND` / `DELIVERY_PIPE_NOT_FOUND`；
    同类重复报 `DELIVERY_SOURCE_DUPLICATE` / `DELIVERY_PIPE_DUPLICATE`；
  - 用水点编号必须**存在且为 sink**：不存在 `DELIVERY_SINK_NOT_FOUND`、
    节点不是用水点 `DELIVERY_SINK_NOT_SINK`、重复 `DELIVERY_SINK_DUPLICATE`；
    只需列出本次有需求的点（需求可为 0），`demands` 为空报 `DELIVERY_DEMANDS_EMPTY`。
- 通水口径与既有可达性接口**完全一致**：停运水源不供水（容量按 0）；
  **已关闭阀门与待修复(open)故障管段不可通行**（容量按 0）；
  未列需求的用水点不消耗流量。
- 管段为**双向共用**输水上限：两方向净流量合计（净流量绝对值）不得超过该管段上限，
  按无向边容量建模；以超级源→运行水源、需求点→超级汇构造整数网络流，
  汇边容量为各点需求，因此结果对总需求封顶。
- 返回当前版本 `version`、版本是否匹配 `version_match`、需求总量 `total_demand`、
  **最大可交付总量** `max_delivered_total` 与**能否全部满足** `feasible`
  （`feasible = max_delivered_total == total_demand`）；
  过期版本**不拒绝**：仍按当前状态核算，仅 `version_match=false`（与预演类接口一致）。

**保障等级分档交付**（`POST /api/delivery/allocate`，**只读**，不改阀位/事件/版本/快照；
与现有 `/api/delivery/evaluate` 并存、互不影响）：

- 供水不足时调度员按**保障等级**核算各用水点可获得的流量（单位均为升/小时）：
  每个申报用水点给非负整数 `demand` 与**正整数** `level`，**数字越小越优先**；
  未列用水点按零需求、不参与分配。
- 入参的版本、水源容量、管段容量及编号覆盖/重复/未知/用水点类型/非负整数等校验规则
  与 `/api/delivery/evaluate` **完全一致**（同一套 `DELIVERY_*` 问题代码，非法整体
  拒绝 `422 DELIVERY_REQUEST_INVALID` 并返回全部问题明细）；`demands` 仍须非空。
  额外约束：`level` 必须为正整数（`0`、负数、小数、布尔等均按 422 拒绝）。
- 通水口径与总量核算一致：沿用当前**运行水源**、**关阀**与**待修复(open)故障**
  （停运水源容量按 0、阻断管段不可通行）；管段容量仍为**双向共用**（两方向净流量
  合计不得超过上限）；各点交付不得超过其需求。
- 优化目标（字典序，整数最大流）：
  1. **先按等级从小到大依次最大化该等级交付总量**——低等级总量未达最优前，
     高等级不占用可支撑低等级的输水能力；
  2. **再在各等级总量均为最优的条件下，按（等级, 用水点编号字符串字典序）
     逐点最大化交付量**——同等级总量内优先把编号靠前的用水点灌满；
  3. 最终各点交付由**一组同时可行的整数流量**实现（内部以有下界整数环流 +
     残量环流补偿增广逐级锁定，锁定目标以 `low=high` 固定，后续增广不可回撤）。
- 返回当前版本 `version`、`version_match`、`total_demand`、`total_delivered`、
  `all_satisfied`（总交付 == 总需求）、按等级数值升序的 `levels`
  （每级 `level/demand/delivered/shortfall/all_satisfied`）以及按
  （等级, 用水点编号）排序的 `deliveries`
  （每点 `sink_id/level/demand/delivered/shortfall/satisfied`）。
- 过期版本**不拒绝**：仍按当前状态核算，仅 `version_match=false`。

**读数核对**（`POST /api/meters/reconcile`，**只读**，不改阀位/水源状态/事件/版本/快照）：

- 调度员核对一批管段流量计读数，并在表计**缺报**时判断其可推断范围。入参为已读
  `version` + **每个水源**的实际注入量 `sources` + **每个用水点**的实际取用量
  `sinks` + **每条管段**的双向共用容量与可为空带符号读数 `pipes`；
  注入、取用、容量均为**非负整数升/小时**（布尔/小数/负数按类型或数值错误处理），
  读数为带符号整数或 `null`：以管段导入方向 **`from→to` 为正**，`null`（或缺省）表示该管段缺报。
- 编号规则（任一不满足即整体拒绝，`422 METER_RECONCILE_INVALID`，返回全部问题明细）：
  水源、用水点、管段编号均须**完整覆盖当前管网**（不多不少）、同类不重复；
  水源编号必须是 source、用水点编号必须是 sink。问题代码分别为
  `RECONCILE_SOURCE_MISSING/NOT_FOUND/DUPLICATE`、
  `RECONCILE_SINK_MISSING/NOT_FOUND/NOT_SINK/DUPLICATE`、
  `RECONCILE_PIPE_MISSING/NOT_FOUND/DUPLICATE`。
- 按**当前水源运行状态、关阀与待修复故障**核对四条硬约束：
  - **停运水源注入必须为 0**：否则 `STOPPED_SOURCE_INJECTION`（含水源编号）；
  - **阻断管段流量必须为 0**：已关闭阀门与待修复(open)故障管段（与可达性/流量核算
    同口径）读数非 0 即 `BLOCKED_PIPE_FLOW`（含管段编号）；
  - **节点平衡**：每个节点 `流入 + 注入 = 流出 + 取用`；
  - **容量约束**：管段净流量绝对值不得超过双向共用容量，已申报读数超限即
    `READING_OVER_CAPACITY`（含管段编号）。
- 可行解用**整数有向弧下界可行流**（Dinic + 超级源/汇）求解；无可行解时
  `consistent=false`、`reasons=[{code:"READINGS_INCONSISTENT",...}]`，且
  **不输出任何补值**（`readings=[]`）。
- 对**缺报管段**，在剔除该弧的残量网络上以两次最大流（环流可增/可减量）求全部
  可行整数解中的**最小/最大流量**：返回 `pipe_id`、方向回显 `from`/`to`、
  `min_flow`、`max_flow` 与 `determinable`（`min_flow == max_flow` 时可确定）。
  阻断管段或容量 0 的管段缺报时恒返回 `min_flow=max_flow=0`；已申报读数不在补值
  列表中返回。
- 过期版本**不拒绝**：仍按当前状态计算，仅 `version_match=false`。
- 该接口为纯只读新增，**原有流量核算接口 `/api/delivery/evaluate` 的语义与返回
  保持不变**（内部新增可复用的 Dinic 类，既有 `_undirected_max_flow` 未改动）。

**调度演练**（`POST /api/drill`，**只读**，不改版本/阀位/水源状态/事件/快照）：

- 入参为已读 `version` + 一串**有序步骤**（`steps`，可为空列表——只回显当前状态）；
  每步三选一（按 `type` 区分）：
  - `isolation`：隔离，`fault_pipe_id` + `close_pipe_ids`（拟关阀门）；
  - `restore`：复供，`event_id`（现存待修复事件）或 `temp_event_ref`
    （本次演练先前隔离步骤产生的**临时事件**，形如 `T<步骤序号>`，序号从 1 起），
    二者必须且只能给出一个；
  - `source_switch`：水源切换，`targets` 为一组 `{source_id, running}`（同批不重复）。
- **版本过期整体拒绝**：`version` 不等于当前版本时返回
  `409 VERSION_CONFLICT`（响应体含当前版本 `current_version`），不执行任何步骤；
  这与提交类接口一致——演练是对一串**提交**的预检。
- 以当前状态为起点，在**状态副本**上逐步套用与既有提交接口**完全一致**的校验：
  隔离按提交口径（`for_submit`：故障两端须均可隔离、已关闭阀门不得重复关闭、
  无阀管段不可关等）；复供与恢复水源不得使任一**待修复**故障管段端点重新进水；
  其他待修复故障管段在每一步的通水计算中持续阻断。
- 每步的**新增失供**（`affected_sink_ids`）与**恢复供水**（`restored_sink_ids`）
  用水点均**相对上一步结束状态**计算；隔离步骤成功后产生临时事件
  （回显 `temp_event_ref`，如 `T1`、`T3`），供后续复供步骤引用。
- **首个失败步骤即停**：任一步非法或不安全时，响应 `ok=false`、
  `failed_step_index` 为失败步骤序号（从 1 起），`steps` 只包含到该步为止的结果，
  该步 `error` 给出原因码与明细，后续步骤不再演练，`final` 为 `null`。
  失败原因码与单步接口一致：`PLAN_INVALID` / `NOT_ISOLATABLE`（隔离）、
  `EVENT_NOT_FOUND` / `EVENT_ALREADY_RESTORED` / `RESTORE_UNSAFE`（复供，
  风险明细在 `error.block_reasons`，代码同为 `FAULT_REWATER_RISK`）、
  `SOURCE_SWITCH_INVALID` / `SOURCE_SWITCH_UNSAFE`（水源切换）。
- 全部步骤成功时 `ok=true`，`final` 给出推演终态：全部水源运行状态 `sources`、
  全部有阀管段阀位 `valves`、仍待修复的故障 `open_faults`
  （现存事件给 `event_id`、临时事件给 `temp_event_ref`）与
  通水用水点 `supplied_sink_ids`（口径与快照一致：仅运行水源可达）。
- 请求体结构非法（未知步骤类型、复供步骤引用缺失或重复、目标列表为空等）
  返回 `422` 请求校验错误；演练本身**永不写入**：不产生版本推进、阀位变化、
  事件变化或状态快照。既有单步接口（预演/提交/确认）的契约保持不变。

**临时旁通管评估**（`POST /api/delivery/bypass`，**只读**，不改阀位/事件/版本/快照）：

- 抢修期间评估一条**临时旁通管**的供水效果。入参为已读 `version` +
  `bypass`（两个现有节点 `from`/`to` + 双向共用 `capacity`，非负整数升/小时）+
  **每个水源**的供水上限 `sources` + **每条管段**的双向共用输水上限 `pipes` +
  **非空**带保障等级的用水需求 `demands`（`sink_id`/`demand`/`level`，
  与 `/api/delivery/allocate` 同一模型）。
- 旁通**允许与现有管段并联**（两端之间已有管段时视为新增一条并联通道）；
  旁通管**只在内存副本中接入**（内部生成不与现有管段重号的虚拟编号），
  绝不写入管网，评估后管网、阀位、事件、版本与快照均不变。
- 编号与端点规则（任一不满足即整体拒绝 `422 BYPASS_REQUEST_INVALID`，
  返回全部问题明细）：水源/管段/用水点编号规则与分档交付接口**完全一致**
  （同一套 `DELIVERY_*` 问题代码）；旁通端点不存在报 `BYPASS_ENDPOINT_NOT_FOUND`、
  两端为同一节点报 `BYPASS_ENDPOINT_SAME`；容量/等级/需求为布尔、小数、
  负数等类型或数值错误按 `422` 请求校验拒绝。
- 评估顺序：先按**当前阀位、运行水源与待修复故障**核算**基线**分档交付
  （规则与 `/api/delivery/allocate` 一致），再在内存中接入旁通做**安全判定**：
  若接入后任一**待修复(open)**故障管段的**任一端**从运行水源可达
  （重新进水风险，与复供/水源切换同口径的**拓扑可达**判定，与旁通容量大小无关），
  判为**不可用**：响应 `safe=false`、`block_reasons` 给出故障管段编号与危险端点
  （代码同为 `FAULT_REWATER_RISK`），`with_bypass` 与 `deltas` 恒为 `null`
  （**不输出旁通交付量**），`baseline` 仍按当前状态给出。
- 安全时（`safe=true`）按现有分档交付规则分别返回 `baseline` 与 `with_bypass`
  两组的各等级、各用水点交付量，并在 `deltas` 中给出总量、各等级、各用水点的
  交付差值（`bypass_delivered - baseline_delivered`，旁通只增不减输水能力，
  差值恒非负）。
- 过期版本**不拒绝**：仍按当前状态核算，仅 `version_match=false`（与预演类接口一致）。

**状态快照（操作后回看）**（只增不改，编号由数据库序列全局单调分配，**不可复用**）：

- 仅四类**实际改变状态**的写操作产生快照，且与状态变更在**同一事务**内冻结：
  整网导入 `network_import`、隔离提交 `isolation_submit`、
  复供确认 `restore_confirm`、水源切换 `source_switch`；
- 每条快照记录：快照编号、操作类型、**当时管网版本**、关联事件编号
  （仅隔离/复供有，其余为 `null`）与冻结的操作后状态；
- 冻结状态包含：全部节点（含水源运行状态）、全部管段阀位、
  当时全部**待修复故障**（事件编号 + 故障管段编号）、以及按与通水计算完全一致的口径
  （仅运行水源、已关阀门与待修复故障管段阻断）得出的**由运行水源可达用水点**；
- **整网导入不覆盖此前快照**：版本虽重置为 `1`，快照表不截断，编号继续单调递增；
  快照载荷自包含（不引用事件表外键），故重新导入清空事件后旧快照依旧可读；
- **操作失败、版本冲突、整批水源目标状态已满足（changed=false）不产生新快照**；
  只读接口（预演、关阀建议、读网）从不产生快照；
- **历史结果绝不以当前管网或后续事件回填**：读取与对照都只基于当时冻结的载荷；
- **旧库上线**只在快照表为空且当前存在管网时，以**当下状态**建立一条
  `baseline` 基线快照（记录当前版本与当前待修复故障），**不伪造**早期操作记录；
  空库与已有快照的库不产生基线，建基线本身幂等；
- 相邻快照对照输出六类**净变化**：节点新增/移除、管段新增/移除、
  共有管段阀位开/关变化、共有水源运行状态变化、待修复故障新增/移除（按事件编号）、
  可达用水点新增/移除；首条快照无更早基线时，其状态全部计为新增；
  显式 `base_id` 必须与目标快照**相邻**，否则 `422 SNAPSHOT_NOT_ADJACENT`；
  未知快照编号统一 `404 SNAPSHOT_NOT_FOUND`，绝不静默返回当前状态。

**快照摘要链（调度审计，事后改写核验）**（只增不改、随状态变更同事务接链）：

- 每条快照在**原状态变更事务内**附带一条 SHA-256 摘要，整网导入即使版本重置
  为 `1` 也**不断链**（快照编号继续单调递增）；快照行与摘要链行同事务写入，
  事务回滚二者同时回滚，链上不存在半完成记录；
- 摘要以**固定可复现编码** `canonical_json`（键升序、紧凑分隔、UTF-8）覆盖：
  快照编号、操作类型、管网版本、关联事件、创建时间（统一规范化为
  UTC `…Z`，与数据库时区无关）、**完整冻结状态**、前一条摘要；信封还固定带
  `"chain":"water-snapshot-chain/v1"` 版本标识；
- 链按**实际存在**的快照编号顺序连续：每条链接“编号严格小于自身的最近一条
  现存快照”。**回滚造成的 BIGSERIAL 编号空洞自动跳过、不算断链**；
- **首条入链快照使用固定创世摘要**
  `13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714`
  （即 `SHA256("water-snapshot-chain/v1:genesis")`）；
- **首次启用**时（升级到本版本后首次启动），服务在同一启动事务内为**所有现存
  快照**按编号顺序补建链，并在链元数据中把最早一条标为建链起点
  `anchor_kind=initial_backfill`：**此前快照是否曾被改写不作断言**，链只保证
  建链之后可核验；空库启用则首条状态变更快照即起点（`live_origin`），直连创世摘要；
- **并发写入不形成分叉**：所有接链先取同一事务级咨询锁（写路径本身还持有版本
  行锁），临界区内读取“上一条现存快照”→ 计算摘要 → 写链行串行完成；发现上一条
  现存快照无链记录时 fail-closed 直接报错，绝不静默接创世摘要；
- **只读核验** `POST /api/audit/verify`：审计员输入起止快照编号，以及**事先离线
  保存**的起点前摘要（核验首条入链快照时传创世摘要）与终点摘要；服务只读取出
  闭区间内现存快照并逐条重算，返回区间内各条摘要与**首个可定位**的问题：
  - `content_corrupted`：某条冻结内容（编号/操作/版本/事件/时间/完整状态）被事后
    改写——按存储链接重算的摘要与链上摘要不符，`failure.snapshot_id` 即损坏位置，
    `digests` 只含损坏点之前已核验通过的条目；
  - `chain_broken`：某条的前一条摘要指针与上一条现存快照摘要不符（或链记录缺失），
    `failure` 同时给出应有/实际前驱摘要与上一条现存快照编号；
  - `boundary_mismatch`：**内部链自洽**，但审计员提供的起点前或终点摘要与链上记录
    不符（`start_boundary_ok` / `end_boundary_ok` 分别标明），只报告边界不符，
    并在 `expected_pre_start_digest` / `expected_end_digest` 回显链上真实值；
  - `ok`：区间自洽且两条边界摘要均相符；
- 核验区间端点（含回滚空洞编号）**不存在**返回 `404 SNAPSHOT_NOT_FOUND`；
  起止倒序返回 `422 AUDIT_RANGE_INVALID`；摘要非 64 位十六进制返回
  `422 AUDIT_DIGEST_INVALID`；核验全程只读，不写版本/阀位/事件/快照/链；
- `GET /api/audit/chain` 返回创世摘要、建链起点（编号/类型/建链时间）及现存、
  已入链快照数与最早/最晚编号，供审计员选取核验边界；
- **现有快照查询接口的字段与含义保持不变**，摘要链信息只通过审计接口暴露。

## 五、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/network/import` | 整网导入（版本重置为 1，旧事件失效） |
| GET | `/api/network` | 读取当前管网与版本 |
| POST | `/api/isolation/recommend` | 关阀建议（只读，按当前管网计算可提交方案） |
| POST | `/api/isolation/preview` | 隔离方案预演（只读，计入待修复故障） |
| POST | `/api/isolation/submit` | 提交隔离（原子关阀 + 版本 +1，事件状态 open） |
| GET | `/api/isolation/events` | 隔离/复供操作历史（含 open / restored 状态） |
| POST | `/api/isolation/events/{event_id}/restore/preview` | 复供预演（只读） |
| POST | `/api/isolation/events/{event_id}/restore` | 复供确认（单事务复核 + 重开阀门） |
| POST | `/api/sources/switch/preview` | 水源停运/恢复预演（只读：失供/恢复用水点、待修复故障重新进水风险） |
| POST | `/api/sources/switch` | 水源停运/恢复确认（锁版本复核 + 原子更新水源状态） |
| POST | `/api/delivery/evaluate` | 用水点最低流量核算（只读：现有阀位/故障下的最大可交付总量，整数最大流） |
| POST | `/api/delivery/allocate` | 保障等级分档交付（只读：先按等级总量、再按等级/编号逐点最大化各点交付与缺口） |
| POST | `/api/delivery/bypass` | 临时旁通管评估（只读：基线与内存接入旁通后的分档交付对照及差值，重新进水风险判不可用） |
| POST | `/api/meters/reconcile` | 管段流量计读数核对（只读：一致性校验 + 缺报管段可行流量最小/最大值，整数有界可行流） |
| POST | `/api/drill` | 调度演练（只读：有序隔离/复供/水源切换逐步预检，临时事件引用，首个失败步骤即停） |
| GET | `/api/snapshots` | 状态快照列表（编号升序，`limit` / `after_id` 游标分页） |
| GET | `/api/snapshots/{snapshot_id}` | 按编号读取冻结快照（节点/水源运行状态/管段阀位/待修复故障/可达用水点） |
| GET | `/api/snapshots/{snapshot_id}/diff?base_id=` | 相邻快照六类净变化对照（缺省对照上一条；首条无基线全计为新增） |
| GET | `/api/audit/chain` | 摘要链信息（创世摘要、建链起点、现存/已入链快照范围，只读） |
| POST | `/api/audit/verify` | 快照区间只读核验：各条摘要与首个内容损坏/链路断裂/边界不符位置 |

业务错误统一返回：

```json
{ "error": { "code": "NOT_ISOLATABLE", "message": "...", "issues": [ ... ] } }
```

## 六、调用示例

### 1. 导入管网

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/sample_network.json
```

示例管网（`examples/sample_network.json`）：

```
S(水源) --P1[阀]-- A(连接点) --P2[阀]-- B(连接点) --P4[阀]-- D(连接点) --P6(无阀,故障)-- U3(用水点)
                    |                  |
                  P3[阀]             P5[阀]
                    |                  |
                   U1(用水点)          U2(用水点)
```

响应：

```json
{ "version": 1, "node_count": 7, "pipe_count": 6 }
```

导入成功后 `GET /api/network` 的节点中，水源节点带 `"running": true`（停运后变为 `false`；
非水源节点该字段恒为 `true`）。

导入非法数据（如悬空端点）会返回 `422 IMPORT_INVALID`：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d '{"nodes":[{"id":"S","type":"source"}],"pipes":[{"id":"P1","from":"S","to":"X","has_valve":false}]}'
```

### 2. 读取版本

```bash
curl -s http://localhost:8000/api/network
```

### 3. 预演：只关 P5（不能隔离）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P5"],"version":1}'
```

`D` 点仍可经 P4 进水，故障管段 P6 的 D 端仍可达，故 `isolatable=false`。

### 4. 预演：关 P4（可以隔离，仅 U3 失供）

```bash
curl -s -X POST http://localhost:8000/api/isolation/preview \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

响应：

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P6",
  "close_pipe_ids": ["P4"],
  "isolatable": true,
  "affected_sink_ids": ["U3"],
  "issues": []
}
```

`version_match=false` 表示你读取的版本已过期（预演仍基于当前管网计算，但不应直接提交）。

### 5. 提交隔离

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

成功响应（P4 阀位原子置为关闭，版本变为 2）：

```json
{
  "version": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "event_id": 1
}
```

常见拒绝：

- 再次用 `"version":1` 提交 → `409 VERSION_CONFLICT`（版本过期，状态不变）；
- 取到版本 2 后再次关闭已关的 `P4` → `422 PLAN_INVALID` / `CLOSE_PIPE_DUPLICATE`；
- 关闭无阀管段 `P6` → `422 PLAN_INVALID` / `CLOSE_PIPE_NO_VALVE`；
- 提交不可隔离方案 → `422 NOT_ISOLATABLE`。

### 6. 隔离历史

```bash
curl -s http://localhost:8000/api/isolation/events
```

历史事件在既有字段基础上增量返回 `status`（`open` 待修复 / `restored` 已复供）
与 `restored_at`（复供时间，待修复时为 `null`），其余字段与旧版完全一致：

```json
{
  "id": 1,
  "version_before": 1,
  "version_after": 2,
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "status": "open",
  "restored_at": null,
  "created_at": "2026-09-30T12:00:00Z"
}
```

### 7. 复供预演与确认（按事件编号 + 当前版本）

复供演示使用包含两个叠加故障的管网 `examples/restore_network.json`：

```
S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀,故障1)-- U3
               |                     |
             P3[阀]                 P7(无阀,故障2)-- E --P8[阀]-- U4
               |
              U1
```

先导入并制造两条待修复事件：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' \
  -d @examples/restore_network.json
# 事件 1：故障 P6，关 P4 隔离整个 D 片区（U3/U4 失供）
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
# 事件 2：D 已被 P4 隔离，区内故障 P7 无需再关阀门即可隔离
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P7","close_pipe_ids":[],"version":2}'
```

复供预演事件 1（只读，不改状态）：重开 P4 会使待修复故障 P7 的 D 端重新进水，`safe=false`：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore/preview \
  -H 'Content-Type: application/json' -d '{"version":3}'
```

```json
{
  "version": 3,
  "version_match": true,
  "event_id": 1,
  "status": "open",
  "fault_pipe_id": "P6",
  "closed_pipe_ids": ["P4"],
  "safe": false,
  "restored_sink_ids": ["U3"],
  "block_reasons": [
    {
      "code": "FAULT_REWATER_RISK",
      "message": "重开阀门后，待修复故障管段 P7 的 D 端将重新进水，禁止复供",
      "pipe_id": "P7",
      "reachable_endpoints": ["D"]
    }
  ]
}
```

此时强行确认复供事件 1 → `422 RESTORE_UNSAFE`（明确拒绝，阀位/版本/事件状态均不变）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore \
  -H 'Content-Type: application/json' -d '{"version":3}'
```

先复供区内事件 2（安全；预演 `safe=true`），确认后版本变为 4，事件 2 状态变为 `restored`：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/2/restore/preview \
  -H 'Content-Type: application/json' -d '{"version":3}'
curl -s -X POST http://localhost:8000/api/isolation/events/2/restore \
  -H 'Content-Type: application/json' -d '{"version":3}'
```

再用当前版本复供事件 1（此时其余待修复故障已不存在），P4 重开、U3/U4 恢复、版本变为 5：

```bash
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore \
  -H 'Content-Type: application/json' -d '{"version":4}'
```

```json
{
  "version": 5,
  "event_id": 1,
  "status": "restored",
  "fault_pipe_id": "P6",
  "reopened_pipe_ids": ["P4"],
  "restored_sink_ids": ["U3", "U4"]
}
```

其他拒绝：

- 版本过期（如事件 2 复供后仍用 `"version":3` 复供事件 1）→ `409 VERSION_CONFLICT`；
- 事件不存在（如 `events/999`）→ `404 EVENT_NOT_FOUND`；
- 已复供事件再次复供 → `409 EVENT_ALREADY_RESTORED`；
- 整网重新导入后旧事件编号失效 → `404 EVENT_NOT_FOUND`。
- 复供预演遇到过期版本不拒绝，只返回 `version_match=false`（与隔离预演一致）。

### 8. 关阀建议（只知道故障管段时）

操作员只知道故障管段编号时，无需手工构造关阀列表，直接请求建议（**只读**，不改任何状态）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/recommend \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","version":1}'
```

响应（建议关 `P4`，预计 `U3` 失供；`version_match=false` 表示已读版本过期，应重新读取）：

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P6",
  "feasible": true,
  "recommended_close_pipe_ids": ["P4"],
  "affected_sink_ids": ["U3"],
  "reason": null
}
```

建议方案可直接交给现有提交接口（按最新状态复核，版本过期仍为 `409 VERSION_CONFLICT`）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
```

无法给出建议时返回 `feasible=false` 并在 `reason` 中说明原因（状态不变）：

- 故障管段不存在 → `reason.code = FAULT_PIPE_NOT_FOUND`；
- 关闭全部可用阀门后故障管段仍有端点与水源相通（如故障管段直连水源且无阀可关）
  → `reason.code = NO_FEASIBLE_PLAN`。

```json
{
  "version": 1,
  "version_match": true,
  "fault_pipe_id": "P1",
  "feasible": false,
  "recommended_close_pipe_ids": [],
  "affected_sink_ids": [],
  "reason": {
    "code": "NO_FEASIBLE_PLAN",
    "message": "关闭全部可用阀门后，故障管段 P1 仍至少有一端可从水源到达，无可行关阀方案",
    "pipe_id": "P1"
  }
}
```

关阀建议全流程演示脚本（标准库实现）：

```bash
python3 examples/recommend_demo.py
# 或指定地址：
python3 examples/recommend_demo.py http://localhost:8000
```

脚本依次演示：导入 → 建议（故障 P6）→ 按建议提交 → 已隔离故障的空方案建议 →
新故障建议（不含已关闭阀门）→ 过期版本提交 409 → 按当前版本提交 →
无可行方案 / 故障管段不存在 → 确认建议全程只读。

### 9. 水源停运 / 恢复（双水源联网）

水源调度演示使用双水源管网 `examples/sources_network.json`：

```
S1(水源) --Q1[阀]-- C(联络点) --Q2[阀]-- U1(用水点)
                    |
S2(水源) --Q3[阀]-- C --Q4[阀]-- U2(用水点)
```

导入后两座水源初始均为运行（`GET /api/network` 中水源节点 `running=true`）。

预演停运 S1（**只读**，S2 经 C 顶供，无用水点失供）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch/preview \
  -H 'Content-Type: application/json' \
  -d '{"targets":[{"source_id":"S1","running":false}],"version":1}'
```

```json
{
  "version": 1,
  "version_match": true,
  "changed": true,
  "safe": true,
  "targets": [{"source_id": "S1", "running": false}],
  "affected_sink_ids": [],
  "restored_sink_ids": [],
  "block_reasons": [],
  "issues": []
}
```

确认停运（安全且有实际变化时版本 +1；同时停运两座水源将导致全网无运行水源，
允许执行，U1/U2 全部新增失供）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"targets":[{"source_id":"S1","running":false},{"source_id":"S2","running":false}],"version":2}'
```

```json
{
  "version": 3,
  "changed": true,
  "targets": [
    {"source_id": "S1", "running": false},
    {"source_id": "S2", "running": false}
  ],
  "affected_sink_ids": ["U1", "U2"],
  "restored_sink_ids": []
}
```

恢复水源后，系统按**待修复故障管段**复核重新进水风险。
下例中保持 S2 停运、隔离故障 Q2（关 Q1）后预演恢复 S2：
故障管段 Q2 的 `C` 端将经 Q3 重新进水，`safe=false`（U2 恢复；U1 因 Q2 本身仍阻断暂不恢复）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch/preview \
  -H 'Content-Type: application/json' \
  -d '{"targets":[{"source_id":"S2","running":true}],"version":5}'
```

```json
{
  "version": 5,
  "version_match": true,
  "changed": true,
  "safe": false,
  "targets": [{"source_id": "S2", "running": true}],
  "affected_sink_ids": [],
  "restored_sink_ids": ["U2"],
  "block_reasons": [
    {
      "code": "FAULT_REWATER_RISK",
      "message": "切换后，待修复故障管段 Q2 的 C 端将重新进水，禁止切换水源状态",
      "pipe_id": "Q2",
      "reachable_endpoints": ["C"]
    }
  ],
  "issues": []
}
```

此时强行确认 → `422 SOURCE_SWITCH_UNSAFE`（阀位/版本/水源状态均不变）：

```bash
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"targets":[{"source_id":"S2","running":true}],"version":5}'
```

其他行为：

- 全部目标状态已满足（如停运已停运的 S1）→ `changed=false`，**版本不推进**；
- 版本过期 → `409 VERSION_CONFLICT`；预演遇到过期版本不拒绝，只返回 `version_match=false`；
- 编号不存在 / 节点不是水源 / 同批编号重复：预演在 `issues` 中返回
  `SOURCE_NOT_FOUND` / `SOURCE_NOT_SWITCHABLE` / `SOURCE_TARGET_DUPLICATE`；
  确认则 `422 SOURCE_SWITCH_INVALID` 且不部分写入；
- 停运 S1 后，隔离预演、关阀建议、复供等既有接口均只按**运行水源** S2 计算。

水源调度全流程演示脚本（标准库实现）：

```bash
python3 examples/source_switch_demo.py
# 或指定地址：
python3 examples/source_switch_demo.py http://localhost:8000
```

脚本依次演示：导入（水源初始运行）→ 停运预演只读 → 确认停运 → 空操作不推进版本 →
全网停运 → 恢复供水 → 停运源下隔离故障 → 恢复水源的重新进水风险拒绝 →
过期/非法请求拒绝 → 故障复供后安全恢复水源。

### 10. 状态快照回看（调度员按操作顺序回看）

四类写操作的成功响应都**增量返回**同一事务冻结的 `snapshot_id`（旧客户端可忽略，字段可空）：

```bash
# 整网导入 -> snapshot_id=1（network_import，版本重置为 1）
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' -d @examples/sample_network.json
# {"version":1,"node_count":7,"pipe_count":6,"snapshot_id":1}

# 隔离提交 -> snapshot_id=2（isolation_submit，关联 event_id=1）
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
# {"version":2,...,"event_id":1,"snapshot_id":2}

# 复供确认 -> snapshot_id=3（restore_confirm，关联同一事件）
curl -s -X POST http://localhost:8000/api/isolation/events/1/restore \
  -H 'Content-Type: application/json' -d '{"version":2}'
# {"version":3,...,"snapshot_id":3}

# 水源切换 -> snapshot_id=4（source_switch；目标状态全部已满足时 snapshot_id=null）
curl -s -X POST http://localhost:8000/api/sources/switch \
  -H 'Content-Type: application/json' \
  -d '{"targets":[{"source_id":"S","running":false}],"version":3}'
# {"version":4,"changed":true,...,"snapshot_id":4}
```

列出快照（编号升序，支持游标分页）：

```bash
curl -s 'http://localhost:8000/api/snapshots?limit=100'
curl -s 'http://localhost:8000/api/snapshots?after_id=2&limit=50'
```

按编号读取当时完整状态（历史只读，不被当前管网/后续事件回填）：

```bash
curl -s http://localhost:8000/api/snapshots/2
```

```json
{
  "id": 2,
  "op_type": "isolation_submit",
  "network_version": 2,
  "event_id": 1,
  "state": {
    "nodes": [ ... ],
    "pipes": [ ... ],
    "open_faults": [{"event_id": 1, "fault_pipe_id": "P6"}],
    "supplied_sink_ids": ["U1", "U2"]
  },
  "created_at": "2026-10-01T20:18:58.255683Z"
}
```

相邻快照对照（缺省对照上一条；首条快照无基线时全部计为新增）：

```bash
curl -s http://localhost:8000/api/snapshots/2/diff
```

```json
{
  "base_snapshot_id": 1,
  "target_snapshot_id": 2,
  "base_network_version": 1,
  "target_network_version": 2,
  "nodes": {"added": [], "removed": []},
  "pipes": {"added": [], "removed": []},
  "valve_positions": {
    "changed": [{"pipe_id": "P4", "valve_closed_before": false, "valve_closed_after": true}]
  },
  "source_states": {"changed": []},
  "open_faults": {
    "added": [{"event_id": 1, "fault_pipe_id": "P6"}],
    "removed": []
  },
  "supplied_sinks": {"added": [], "removed": ["U3"]}
}
```

整网重新导入后版本重置为 `1`，但快照编号继续递增、旧快照原封不动：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' -d @examples/sample_network.json
# {"version":1,...,"snapshot_id":6}   # 编号不是 1，且 1-5 仍可读
```

错误约定：

- 未知快照编号（读详情或对照）→ `404 SNAPSHOT_NOT_FOUND`；
- `base_id` 不是目标快照相邻的上一条 → `422 SNAPSHOT_NOT_ADJACENT`；
- `base_id` ≥ 目标编号 → `422 SNAPSHOT_DIFF_INVALID`。

状态快照回看全流程演示脚本（标准库实现）：

```bash
python3 examples/snapshot_demo.py
# 或指定地址：
python3 examples/snapshot_demo.py http://localhost:8000
```

脚本依次演示：导入/隔离/复供/水源切换四类操作各自冻结快照 → 按编号读取当时状态 →
相邻对照六类净变化 → 版本冲突、不可隔离、水源空切换均不留快照 →
整网重导版本重置但旧快照不覆盖、编号不复用 → 未知编号与非相邻对照报错。

### 11. 快照摘要链核验（调度审计，只读）

每条状态变更快照都在**同一事务**内接一条 SHA-256 摘要链。先查看链信息取得
创世摘要与建链起点：

```bash
curl -s http://localhost:8000/api/audit/chain
```

```json
{
  "genesis_digest": "13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714",
  "chain_tag": "water-snapshot-chain/v1",
  "anchor_snapshot_id": 1,
  "anchor_kind": "live_origin",
  "anchored_at": null,
  "snapshot_count": 2,
  "chained_count": 2,
  "earliest_snapshot_id": 1,
  "latest_snapshot_id": 2
}
```

审计员事先离线保存的两个边界值：**起点前摘要**（核验首条入链快照时传创世摘要；
核验子区间时传“区间起点之前最近一条现存快照”的摘要）与**终点快照摘要**。
两者都可在首次核验时由服务回显的 `expected_pre_start_digest` /
`expected_end_digest` 取得并保存，此后作为独立凭据反复比对。

核验请求示例（核验快照 1–2，起点为首条故传创世摘要，终点摘要为事先保存值；
下方摘要为示意值，实际以 `/api/audit/chain` 与首次核验回显的 `expected_*` 为准）：

```bash
curl -s -X POST http://localhost:8000/api/audit/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "start_id": 1,
    "end_id": 2,
    "pre_start_digest": "13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714",
    "end_digest": "4f2a9c7e8b1d6a3f5c0e2b9a7d4f1c8e6b3a0d2f5c8e1b4a7d0f3c6e9b2a5d8f"
  }'
```

```json
{
  "ok": true,
  "verdict": "ok",
  "start_snapshot_id": 1,
  "end_snapshot_id": 2,
  "genesis_digest": "13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714",
  "pre_start_digest": "13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714",
  "end_digest": "4f2a9c7e8b1d6a3f5c0e2b9a7d4f1c8e6b3a0d2f5c8e1b4a7d0f3c6e9b2a5d8f",
  "expected_pre_start_digest": "13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714",
  "expected_end_digest": "4f2a9c7e8b1d6a3f5c0e2b9a7d4f1c8e6b3a0d2f5c8e1b4a7d0f3c6e9b2a5d8f",
  "start_boundary_ok": true,
  "end_boundary_ok": true,
  "failure": null,
  "digests": [
    {"snapshot_id": 1, "digest": "9c1d0000000000000000000000000000000000000000000000000000000000ab", "prev_digest": "13ffb519ed1f000b83d4fc715feb79da314f24b5d2a7ceb6c06ba083bf05e714"},
    {"snapshot_id": 2, "digest": "4f2a9c7e8b1d6a3f5c0e2b9a7d4f1c8e6b3a0d2f5c8e1b4a7d0f3c6e9b2a5d8f", "prev_digest": "9c1d0000000000000000000000000000000000000000000000000000000000ab"}
  ]
}
```

返回约定（HTTP 恒为 200，结论在 `verdict`）：

- `ok`：区间内逐条重算全部相符，且两条边界摘要均与事先保存值相符；
- `content_corrupted`：首个被改写的快照编号（冻结的编号/操作/版本/事件/时间/
  完整状态任一字段变化都会使重算摘要不符），`digests` 只含损坏点之前的条目；
- `chain_broken`：首个断链位置（前驱摘要指针不符或链记录缺失，含上一条现存编号
  与应有/实际前驱摘要）；回滚造成的编号空洞不算断链；
- `boundary_mismatch`：内部链自洽，仅起点前或终点边界摘要不符，
  `start_boundary_ok` / `end_boundary_ok` 分别标明哪一侧不符。

错误约定：端点（含回滚空洞）不存在 → `404 SNAPSHOT_NOT_FOUND`；
区间倒序 → `422 AUDIT_RANGE_INVALID`；摘要格式非法 → `422 AUDIT_DIGEST_INVALID`。

```bash
python3 examples/audit_chain_demo.py
# 或指定地址：
python3 examples/audit_chain_demo.py http://localhost:8000
```

脚本依次演示：制造快照 → 读取链信息 → 用事先保存的边界摘要全区间核验通过 →
终点摘要不符只报 boundary_mismatch → 端点不存在、区间倒序、摘要格式非法的报错。
内容损坏/链路断裂需直接改写数据库才能复现（正常 API 没有任何改写快照的路径），
其定位逻辑由 `tests/test_audit.py` 覆盖。

### 12. 用水点最低流量核算（只读）

调度员在**现有阀位与故障状态**下，携带已读版本、每个水源的供水上限、每条管段的
双向共用输水上限与本次申报需求的用水点，核算最大可交付总量（**只读**，不改任何状态）：

```bash
curl -s -X POST http://localhost:8000/api/delivery/evaluate \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "sources": [{"source_id": "S", "capacity": 1000}],
    "pipes": [
      {"pipe_id": "P1", "capacity": 100},
      {"pipe_id": "P2", "capacity": 100},
      {"pipe_id": "P3", "capacity": 6},
      {"pipe_id": "P4", "capacity": 100},
      {"pipe_id": "P5", "capacity": 100},
      {"pipe_id": "P6", "capacity": 100}
    ],
    "demands": [
      {"sink_id": "U1", "demand": 10},
      {"sink_id": "U2", "demand": 10},
      {"sink_id": "U3", "demand": 10}
    ]
  }'
```

响应（`U1` 的唯一通道 `P3` 双向共用上限只有 6，最多交付 6，总需求 30 只能交付 26）：

```json
{
  "version": 1,
  "version_match": true,
  "total_demand": 30,
  "max_delivered_total": 26,
  "feasible": false
}
```

本轮用法要点：

- `sources` / `pipes` 必须**逐一覆盖当前管网全部水源与管段**（停运水源仍须列出，
  已关闭阀门/待修复故障管段也仍须列出）；`demands` 只列本次申报需求的用水点，
  未列出的点不消耗流量（例如只核算 U1/U2 时不传 U3）。
- 容量与需求均为**非负整数升/小时**；停运水源、已关阀门、待修复故障管段
  分别按容量 0 / 不可通行处理（传 0 或传更大的值结果相同，状态由管网决定）。
- 一条管段两方向合计不得超过其 `capacity`（无向共用容量）；
  `max_delivered_total` 对总需求封顶，`feasible=true` 即全部最低需求可满足。
- 用过期版本（如 `"version": 99`）调用不报错，仍按当前状态核算，
  仅 `version_match=false`，据此提示调度员重新读网。
- 编号未覆盖/重复/不存在、用水点类型错误、需求列表为空、布尔/小数/负数等
  类型或数值错误均 `422 DELIVERY_REQUEST_INVALID` 整体拒绝，明细在 `error.issues`。

隔离 P6（关 P4）后再核算同一需求：故障管段 P6 与已关阀门 P4 阻断，U3 断供，
只交付 U1/U2 的 20 L/h，`feasible=false`；只读核对版本仍为 2、阀位与事件不变：

```bash
# 先制造故障态
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'
# 再核算（version 传 2）：U3 不可交付
```

流量核算全流程演示脚本（标准库实现，全程只读）：

```bash
python3 examples/delivery_demo.py
# 或指定地址：
python3 examples/delivery_demo.py http://localhost:8000
```

脚本依次演示：容量充足全部满足 → 管段瓶颈交付不足 → 过期版本仍按当前状态核算 →
水源上限不足 → 隔离关阀后故障片区断供 / 只申报可达用水点即可满足 →
编号未覆盖/重复/类型错误的 422 拒绝 → 确认版本、阀位与事件全程未被改写。

### 13. 保障等级分档交付（只读，供水不足时按等级核算）

调度员在**现有阀位与故障状态**下，携带已读版本、覆盖全部水源/管段的非负整数
容量，以及**非空**用水点需求（每点给非负整数需求 `demand` 与正整数保障等级
`level`，数字越小越优先）。服务**只读**返回各用水点可获得的交付与缺口，
不改任何状态：

```bash
curl -s -X POST http://localhost:8000/api/delivery/allocate \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "sources": [{"source_id": "S", "capacity": 1000}],
    "pipes": [
      {"pipe_id": "P1", "capacity": 100},
      {"pipe_id": "P2", "capacity": 100},
      {"pipe_id": "P3", "capacity": 6},
      {"pipe_id": "P4", "capacity": 100},
      {"pipe_id": "P5", "capacity": 100},
      {"pipe_id": "P6", "capacity": 100}
    ],
    "demands": [
      {"sink_id": "U1", "demand": 10, "level": 1},
      {"sink_id": "U2", "demand": 10, "level": 2},
      {"sink_id": "U3", "demand": 10, "level": 2}
    ]
  }'
```

上例中 `U1`（等级 1）的唯一通道 `P3` 双向共用上限只有 6：服务先把等级 1
总量最大化到 6（`U1=6`），再在等级 1 总量锁定的前提下最大化等级 2 总量
（`U2=10`、`U3=10`），并在等级 2 总量 20 已最优下按编号逐点灌满：

```json
{
  "version": 1,
  "version_match": true,
  "total_demand": 30,
  "total_delivered": 26,
  "all_satisfied": false,
  "levels": [
    {"level": 1, "demand": 10, "delivered": 6, "shortfall": 4, "all_satisfied": false},
    {"level": 2, "demand": 20, "delivered": 20, "shortfall": 0, "all_satisfied": true}
  ],
  "deliveries": [
    {"sink_id": "U1", "level": 1, "demand": 10, "delivered": 6, "shortfall": 4, "satisfied": false},
    {"sink_id": "U2", "level": 2, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true},
    {"sink_id": "U3", "level": 2, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true}
  ]
}
```

本轮用法要点：

- **先保等级总量**：低等级（数字小）交付总量未达最优前，高等级不会占用可
  支撑低等级的输水能力；再在各等级总量都最优的条件下，同等级内按
  **用水点编号字符串字典序**逐点优先灌满编号靠前的点。
- `deliveries` 恒按（等级升序、用水点编号升序）排列；各点 `delivered`
  不超过 `demand`，`shortfall = demand - delivered`，`all_satisfied` 仅在
  总交付等于总需求时为 true。上述各点交付由**一组同时可行的整数流量**实现。
- 编号规则与 `/api/delivery/evaluate` 完全相同：`sources`/`pipes` 逐一覆盖
  当前全部水源与管段（停运源、关阀/故障管段也仍须列出）；`demands` 只列本次
  申报需求的用水点，未列点按零需求、不参与分配；编号缺失/重复/未知、用水点
  类型错误、容量或需求为布尔/小数/负数、需求列表为空均
  `422 DELIVERY_REQUEST_INVALID` 整体拒绝；`level` 还必须是**正整数**。
- 通水口径沿用当前**运行水源、关阀与待修复故障**（停运源不供水、阻断管段
  不可通行、管段双向共用容量），用过期版本（如 `"version": 99`）调用不报错，
  仍按当前状态核算，仅 `version_match=false`。
- 该接口为**只读新增**：现有 `/api/delivery/evaluate` 的输入输出与含义
  **保持不变**（无 `level` 字段、仍只返回总量口径）。

保障等级分档交付全流程演示脚本（标准库实现，全程只读）：

```bash
python3 examples/allocate_demo.py
# 或指定地址：
python3 examples/allocate_demo.py http://localhost:8000
```

脚本依次演示：容量充足全部满足 → P3 瓶颈下等级 1 的 U1 先保 6、等级 2 拿余量 →
水源上限不足时等级优先 → 同等级按编号序逐点灌满 → 过期版本仍按当前状态核算 →
隔离关阀后故障片区断供 → 等级非正整数 / 编号未覆盖 / 重复 / 类型错误的 422 拒绝 →
确认版本、阀位与事件全程未被改写。

### 14. 管段流量计读数核对（只读，缺报推断）

调度员携带已读版本、每个水源的**实际注入量**、每个用水点的**实际取用量**、
覆盖全部管段的**双向共用容量**与可为空的**带符号读数**（`from→to` 为正，升/小时），
按当前水源运行状态、关阀与待修复故障核对一致性；`reading` 传 `null` 表示该表缺报，
服务返回它在全部可行解中的最小/最大流量（**只读**，不改任何状态）：

```bash
curl -s -X POST http://localhost:8000/api/meters/reconcile \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "sources": [{"source_id": "S", "injection": 30}],
    "sinks": [
      {"sink_id": "U1", "withdrawal": 10},
      {"sink_id": "U2", "withdrawal": 10},
      {"sink_id": "U3", "withdrawal": 10}
    ],
    "pipes": [
      {"pipe_id": "P1", "capacity": 100, "reading": null},
      {"pipe_id": "P2", "capacity": 100, "reading": null},
      {"pipe_id": "P3", "capacity": 100, "reading": null},
      {"pipe_id": "P4", "capacity": 100, "reading": null},
      {"pipe_id": "P5", "capacity": 100, "reading": null},
      {"pipe_id": "P6", "capacity": 100, "reading": null}
    ]
  }'
```

树形管网下节点平衡把每条缺报管段唯一确定（`determinable=true`），并回显方向：

```json
{
  "version": 1,
  "version_match": true,
  "consistent": true,
  "readings": [
    {"pipe_id": "P1", "from": "S", "to": "A", "min_flow": 30, "max_flow": 30, "determinable": true},
    {"pipe_id": "P2", "from": "A", "to": "B", "min_flow": 20, "max_flow": 20, "determinable": true},
    {"pipe_id": "P3", "from": "A", "to": "U1", "min_flow": 10, "max_flow": 10, "determinable": true},
    {"pipe_id": "P4", "from": "B", "to": "D", "min_flow": 20, "max_flow": 20, "determinable": true},
    {"pipe_id": "P5", "from": "B", "to": "U2", "min_flow": 10, "max_flow": 10, "determinable": true},
    {"pipe_id": "P6", "from": "D", "to": "U3", "min_flow": 10, "max_flow": 10, "determinable": true}
  ],
  "reasons": []
}
```

存在并联路径时缺报读数只能给出范围。例如 S→U 两条并联管段容量各 10、总流量 2：

```json
{"pipe_id": "Q1", "from": "S", "to": "U", "min_flow": -8, "max_flow": 10, "determinable": false}
```

本轮用法要点：

- `sources` / `sinks` / `pipes` 均须**逐一覆盖当前管网**（停运水源、各用水点——
  包括取用量为 0 的点——以及关阀/故障管段都仍须列出）；已申报读数的管段与缺报
  管段混在同一 `pipes` 列表，用 `reading` 是否为 `null` 区分。
- 读数以**导入管段的 `from→to` 为正**：负读数表示实际水流方向为 `to→from`；
  响应中每条范围也回显 `from`/`to`，避免符号歧义。
- 停运水源注入非 0（`STOPPED_SOURCE_INJECTION`）、关阀/待修复故障管段读数非 0
  （`BLOCKED_PIPE_FLOW`）、读数绝对值超过容量（`READING_OVER_CAPACITY`）、
  或整体平衡/容量无可行解（`READINGS_INCONSISTENT`）时 `consistent=false`，
  原因在 `reasons`（含 `pipe_id` / `node_id`），且 **`readings` 恒为空、不补值**。
- 用过期版本（如 `"version": 99`）调用不报错，仍按当前状态核对，
  仅 `version_match=false`。
- 编号覆盖不足/重复/未知节点（含把连接点当水源/用水点）、注入/取用/容量为
  布尔/小数/负数、读数为小数等均 `422 METER_RECONCILE_INVALID` 整体拒绝，
  明细在 `error.issues`。

读数核对全流程演示脚本（标准库实现，全程只读）：

```bash
python3 examples/meter_reconcile_demo.py
# 或指定地址：
python3 examples/meter_reconcile_demo.py http://localhost:8000
```

脚本依次演示：全部缺报由平衡唯一确定 → 部分缺报/部分已报 → 读数不一致不补值 →
读数超容量 → 隔离关阀与待修复故障管段阻断（缺报确定为 0、非零读数拒绝）→
停运水源注入非 0 拒绝 → 过期版本仍按当前状态核对 →
编号未覆盖/重复/未知节点与类型错误的 422 拒绝 → 确认版本、阀位、水源状态与事件
全程未被改写。

### 15. 调度演练（只读：一串操作执行前的逐步预检）

调度员把一串**有序的隔离、复供、水源切换操作**连同已读版本一起提交演练；
服务以当前状态为起点逐步套用既有提交校验，**全程只读**（不改版本、阀位、
事件与快照）。演练使用复供演示管网 `examples/restore_network.json`：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' -d @examples/restore_network.json
```

五步连贯演练（隔离 → 复供临时事件 → 再隔离 → 停运 → 恢复水源）：

```bash
curl -s -X POST http://localhost:8000/api/drill \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "steps": [
      {"type": "isolation", "fault_pipe_id": "P6", "close_pipe_ids": ["P4"]},
      {"type": "restore", "temp_event_ref": "T1"},
      {"type": "isolation", "fault_pipe_id": "P7", "close_pipe_ids": ["P4", "P8"]},
      {"type": "source_switch", "targets": [{"source_id": "S", "running": false}]},
      {"type": "source_switch", "targets": [{"source_id": "S", "running": true}]}
    ]
  }'
```

响应（节选）：第 1 步隔离 P6 产生临时事件 `T1`、U3/U4 新增失供；第 2 步复供
`T1` 使其恢复；第 3 步隔离 P7 产生 `T3`；第 4/5 步停运再恢复水源 S，
失供/恢复均**相对上一步**计算；`final` 给出推演终态：

```json
{
  "version": 1,
  "ok": true,
  "failed_step_index": null,
  "steps": [
    {"index": 1, "type": "isolation", "ok": true, "fault_pipe_id": "P6",
     "close_pipe_ids": ["P4"], "temp_event_ref": "T1",
     "affected_sink_ids": ["U3", "U4"], "restored_sink_ids": [], "error": null},
    {"index": 2, "type": "restore", "ok": true, "temp_event_ref": "T1",
     "reopened_pipe_ids": ["P4"], "affected_sink_ids": [],
     "restored_sink_ids": ["U3", "U4"], "error": null}
  ],
  "final": {
    "sources": [{"source_id": "S", "running": true}],
    "valves": [
      {"pipe_id": "P1", "valve_closed": false},
      {"pipe_id": "P4", "valve_closed": true},
      {"pipe_id": "P8", "valve_closed": true}
    ],
    "open_faults": [{"event_id": null, "temp_event_ref": "T3", "fault_pipe_id": "P7"}],
    "supplied_sink_ids": ["U1"]
  }
}
```

本轮用法要点：

- **临时事件引用**：第 `k` 步（从 1 起）隔离成功后产生临时事件 `T<k>`，
  后续复供步骤用 `"temp_event_ref": "T<k>"` 引用；复供现存待修复事件用
  `"event_id": <事件编号>`，二者必须且只能给出一个。
- **版本过期整体拒绝**：`version` 与当前版本不一致时返回
  `409 VERSION_CONFLICT`（响应体含 `current_version`），不执行任何步骤。
- **首个失败步骤即停**：如“隔离区内故障 P7（临时事件 T1）后再复供外层事件”，
  重开阀门会使 P7 的 D 端重新进水——响应 `ok=false`、`failed_step_index=2`、
  该步 `error.code=RESTORE_UNSAFE`（`error.block_reasons` 含故障管段与
  重新进水端点），后续步骤不再演练、`final` 为 `null`。
- 复供引用不存在的事件编号或未产生的临时引用 → 该步
  `error.code=EVENT_NOT_FOUND`；重复复供 → `EVENT_ALREADY_RESTORED`；
  隔离步骤结构非法 → `PLAN_INVALID`（明细在 `error.issues`）；
  不可隔离 → `NOT_ISOLATABLE`；水源切换编号非法 → `SOURCE_SWITCH_INVALID`、
  重新进水风险 → `SOURCE_SWITCH_UNSAFE`。
- 演练后真实状态不变：版本、阀位、事件、快照均与演练前一致；随后按既有
  单步接口（提交/确认）执行时仍按各自契约复核版本与安全性。

调度演练全流程演示脚本（标准库实现，全程只读）：

```bash
python3 examples/drill_demo.py
# 或指定地址：
python3 examples/drill_demo.py http://localhost:8000
```

脚本依次演示：五步连贯演练（隔离 → 复供临时事件 → 再隔离 → 停运/恢复水源）
与最终状态汇总 → 演练只读核对 → 真实提交隔离事件 → 演练复供现存事件 →
复供外层事件触发区内故障重新进水（RESTORE_UNSAFE，首败即停）→
过期版本 409 → 引用不存在事件 EVENT_NOT_FOUND → 再次确认只读，
并按既有接口真实复供（单步契约不变）。

### 16. 临时旁通管评估（只读：抢修期间评估旁通供水效果）

抢修期间，调度员携带已读版本、两个现有节点（旁通端点）、旁通管双向共用容量、
覆盖全部水源/管段的容量与带保障等级的用水需求，评估一条临时旁通管的供水效果
（**只读**，旁通只在内存中接入，不改任何状态）。沿用示例管网
`examples/sample_network.json`，`P3`（A→U1 的唯一通道）双向共用上限只有 6：

```bash
curl -s -X POST http://localhost:8000/api/network/import \
  -H 'Content-Type: application/json' -d @examples/sample_network.json

# 旁通 S->U1 容量 4（与现有管段并联）：基线 U1=6，接入后 U1=10
curl -s -X POST http://localhost:8000/api/delivery/bypass \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 1,
    "bypass": {"from": "S", "to": "U1", "capacity": 4},
    "sources": [{"source_id": "S", "capacity": 1000}],
    "pipes": [
      {"pipe_id": "P1", "capacity": 100},
      {"pipe_id": "P2", "capacity": 100},
      {"pipe_id": "P3", "capacity": 6},
      {"pipe_id": "P4", "capacity": 100},
      {"pipe_id": "P5", "capacity": 100},
      {"pipe_id": "P6", "capacity": 100}
    ],
    "demands": [
      {"sink_id": "U1", "demand": 10, "level": 1},
      {"sink_id": "U2", "demand": 10, "level": 2},
      {"sink_id": "U3", "demand": 10, "level": 2}
    ]
  }'
```

响应（节选）：`safe=true`，`baseline` 与 `with_bypass` 分别给出两组的各等级、
各用水点交付量，`deltas` 给出总量/各等级/各用水点差值（U1 交付 6→10，总量 26→30）：

```json
{
  "version": 1,
  "version_match": true,
  "bypass": {"from": "S", "to": "U1", "capacity": 4},
  "safe": true,
  "block_reasons": [],
  "baseline": {
    "total_demand": 30,
    "total_delivered": 26,
    "all_satisfied": false,
    "levels": [
      {"level": 1, "demand": 10, "delivered": 6, "shortfall": 4, "all_satisfied": false},
      {"level": 2, "demand": 20, "delivered": 20, "shortfall": 0, "all_satisfied": true}
    ],
    "deliveries": [
      {"sink_id": "U1", "level": 1, "demand": 10, "delivered": 6, "shortfall": 4, "satisfied": false},
      {"sink_id": "U2", "level": 2, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true},
      {"sink_id": "U3", "level": 2, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true}
    ]
  },
  "with_bypass": {
    "total_demand": 30,
    "total_delivered": 30,
    "all_satisfied": true,
    "levels": [
      {"level": 1, "demand": 10, "delivered": 10, "shortfall": 0, "all_satisfied": true},
      {"level": 2, "demand": 20, "delivered": 20, "shortfall": 0, "all_satisfied": true}
    ],
    "deliveries": [
      {"sink_id": "U1", "level": 1, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true},
      {"sink_id": "U2", "level": 2, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true},
      {"sink_id": "U3", "level": 2, "demand": 10, "delivered": 10, "shortfall": 0, "satisfied": true}
    ]
  },
  "deltas": {
    "total_delivered_delta": 4,
    "levels": [
      {"level": 1, "baseline_delivered": 6, "bypass_delivered": 10, "delivered_delta": 4},
      {"level": 2, "baseline_delivered": 20, "bypass_delivered": 20, "delivered_delta": 0}
    ],
    "deliveries": [
      {"sink_id": "U1", "level": 1, "baseline_delivered": 6, "bypass_delivered": 10, "delivered_delta": 4},
      {"sink_id": "U2", "level": 2, "baseline_delivered": 10, "bypass_delivered": 10, "delivered_delta": 0},
      {"sink_id": "U3", "level": 2, "baseline_delivered": 10, "bypass_delivered": 10, "delivered_delta": 0}
    ]
  }
}
```

隔离 P6（关 P4）后，旁通 A→D 会使待修复故障 P6 的 D 端重新进水：
`safe=false`、`block_reasons` 给出故障编号与危险端点，`with_bypass` 与
`deltas` 为 `null`（不输出旁通交付量），`baseline` 仍给出（U3 断供）：

```bash
curl -s -X POST http://localhost:8000/api/isolation/submit \
  -H 'Content-Type: application/json' \
  -d '{"fault_pipe_id":"P6","close_pipe_ids":["P4"],"version":1}'

curl -s -X POST http://localhost:8000/api/delivery/bypass \
  -H 'Content-Type: application/json' \
  -d '{
    "version": 2,
    "bypass": {"from": "A", "to": "D", "capacity": 50},
    "sources": [{"source_id": "S", "capacity": 1000}],
    "pipes": [
      {"pipe_id": "P1", "capacity": 100},
      {"pipe_id": "P2", "capacity": 100},
      {"pipe_id": "P3", "capacity": 6},
      {"pipe_id": "P4", "capacity": 100},
      {"pipe_id": "P5", "capacity": 100},
      {"pipe_id": "P6", "capacity": 100}
    ],
    "demands": [
      {"sink_id": "U1", "demand": 10, "level": 1},
      {"sink_id": "U2", "demand": 10, "level": 2},
      {"sink_id": "U3", "demand": 10, "level": 2}
    ]
  }'
```

```json
{
  "version": 2,
  "version_match": true,
  "bypass": {"from": "A", "to": "D", "capacity": 50},
  "safe": false,
  "block_reasons": [
    {
      "code": "FAULT_REWATER_RISK",
      "message": "接入旁通后，待修复故障管段 P6 的 D 端将重新进水，旁通不可用",
      "pipe_id": "P6",
      "reachable_endpoints": ["D"]
    }
  ],
  "baseline": { "total_demand": 30, "total_delivered": 16, "all_satisfied": false,
                "levels": [ ... ], "deliveries": [ ... ] },
  "with_bypass": null,
  "deltas": null
}
```

本轮用法要点：

- `bypass.from` / `bypass.to` 必须是**两个不同的现有节点**（类型不限，
  允许与现有管段并联）；`bypass.capacity` 为**非负整数**升/小时。
  端点不存在（`BYPASS_ENDPOINT_NOT_FOUND`）或相同（`BYPASS_ENDPOINT_SAME`）
  整体拒绝 `422 BYPASS_REQUEST_INVALID`。
- `sources` / `pipes` 编号规则与 `/api/delivery/allocate` **完全一致**
  （逐一覆盖当前全部水源与管段，同一套 `DELIVERY_*` 问题代码）；
  `demands` 非空，每点给非负整数 `demand` 与正整数 `level`。
- 安全判定为**拓扑可达**（与复供/水源切换同口径）：接入旁通后任一待修复
  故障管段的任一端从运行水源可达即判不可用——即使旁通容量为 0 也不例外；
  判不可用时 `with_bypass` / `deltas` 恒为 `null`，`baseline` 仍给出。
- 旁通管只在内存中接入评估：评估后版本、阀位、事件、快照与管网均不变，
  管网中不会出现虚拟旁通管段；用过期版本调用不报错，仅 `version_match=false`。

临时旁通管评估全流程演示脚本（标准库实现，全程只读）：

```bash
python3 examples/bypass_demo.py
# 或指定地址：
python3 examples/bypass_demo.py http://localhost:8000
```

脚本依次演示：无故障时旁通并联提升瓶颈点交付（基线/旁通/差值）→
隔离故障后旁通接通故障端点判不可用（FAULT_REWATER_RISK，不输出旁通交付量）→
故障态下远离故障区的安全旁通 → 过期版本仍按当前状态核算 →
端点不存在/相同、编号未覆盖、容量非法的 422 拒绝 →
确认版本、阀位、事件与管网全程未被改写。

### 一键完整演示脚本（标准库实现，无需安装依赖）

```bash
python3 examples/demo.py
# 或指定地址：
python3 examples/demo.py http://localhost:8000
```

脚本依次演示：导入 → 读取版本 → 不可行/可行预演 → 过期版本 409 → 成功提交 → 重复提交 409 → 重复关阀 422 → 历史。

复供流程（两条叠加故障：风险拒绝 → 区内事件安全复供 → 版本/状态拒绝 → 外层事件安全复供 → 历史）：

```bash
python3 examples/restore_demo.py
# 或指定地址：
python3 examples/restore_demo.py http://localhost:8000
```

水源调度流程（停运顶供 → 全网停运 → 恢复供水 → 恢复水源的重新进水风险拒绝 → 故障复供后恢复）：

```bash
python3 examples/source_switch_demo.py
# 或指定地址：
python3 examples/source_switch_demo.py http://localhost:8000
```

调度演练流程（五步连贯演练与最终状态 → 复供现存/临时事件 → 重新进水风险首败即停 → 过期版本 409 → 引用不存在事件 → 全程只读核对）：

```bash
python3 examples/drill_demo.py
# 或指定地址：
python3 examples/drill_demo.py http://localhost:8000
```

临时旁通管评估流程（旁通并联提升瓶颈交付 → 接通故障端点判不可用 → 故障态安全旁通 → 过期版本仍核算 → 非法请求 422 → 全程只读核对）：

```bash
python3 examples/bypass_demo.py
# 或指定地址：
python3 examples/bypass_demo.py http://localhost:8000
```

## 七、测试

核心图算法与校验逻辑（`tests/test_planning.py`，114 个用例）与快照摘要链
（`tests/test_audit.py`，18 个用例：固定编码/时区无关/逐条重算/损坏与断链定位/
仅边界不符/编号空洞/摘要格式）均不依赖数据库与第三方库，可直接用标准库运行：

```bash
python3 -m unittest tests.test_planning tests.test_audit -v
```

路由层集成测试（`tests/test_api.py`，130 个用例）用内存替身替换数据库，覆盖完整 HTTP 契约（含关阀建议、复供预演/确认、水源停运/恢复预演与确认、流量只读核算、**保障等级分档交付（等级总量/逐点字典序/阻断口径/422/只读）**、读数核对（一致性/缺报范围/硬约束/422）、**调度演练（逐步预检/临时事件引用/首败即停/409/只读）**、**临时旁通管评估（基线对照/重新进水判不可用/并联/422/只读）**、版本与安全拒绝、整网替换失效、状态快照冻结/读取/相邻对照/失败不留痕/重导不覆盖、**摘要链同事务接链/区间核验 ok·边界不符·内容损坏·链断定位/编号空洞不断链/倒序与缺端点 4xx/只读**），需先安装依赖：

```bash
pip install -r requirements.txt httpx
python3 -m unittest discover -s tests -v
```

## 八、项目结构

```
app/
  config.py       # 环境变量配置
  schemas.py      # Pydantic 请求/响应模型
  planning.py     # 纯领域逻辑：导入校验、无向图可达性(BFS，仅运行水源)、隔离评估、复供安全评估、关阀建议、水源切换评估、流量核算(校验+整数无向最大流)、保障等级分档交付(有下界整数环流+残量环流补偿的字典序最大流)、临时旁通管评估(内存接入旁通的基线/交付对照与重新进水判定)、读数核对(校验+整数有界可行流+缺报流量范围)、调度演练(状态副本上逐步套用提交校验的只读推演)、状态快照冻结与相邻对照
  audit.py        # 快照 SHA-256 摘要链：固定可复现编码、创世摘要、只读区间核验(内容损坏/链路断裂/边界不符定位)
  db.py           # PostgreSQL：DDL/迁移、快照、整网导入、事务化隔离提交、事务化复供、事务化水源切换、状态快照写入/同事务接链、首次启用补建链与起点元数据、旧库上线基线、审计区间只读查询、历史
  serializers.py  # 响应序列化
  main.py         # FastAPI 路由与错误处理
examples/
  sample_network.json     # 隔离演示管网
  restore_network.json    # 复供演示管网（叠加故障）
  sources_network.json    # 水源调度演示管网（双水源联网）
  demo.py                 # 隔离全流程示例
  restore_demo.py         # 复供全流程示例
  recommend_demo.py       # 关阀建议全流程示例
  source_switch_demo.py   # 水源停运/恢复全流程示例
  delivery_demo.py        # 用水点最低流量核算全流程示例（只读：水源/管段上限、瓶颈、停运/关阀阻断）
  allocate_demo.py        # 保障等级分档交付全流程示例（只读：等级总量优先、同等级编号序、缺口、阻断口径、422）
  meter_reconcile_demo.py # 管段流量计读数核对全流程示例（只读：一致性、缺报范围、停运/关阀/故障硬约束、422）
  drill_demo.py           # 调度演练全流程示例（只读：有序步骤逐步预检、临时事件引用、首败即停、409/只读核对）
  bypass_demo.py          # 临时旁通管评估全流程示例（只读：基线/旁通交付对照与差值、重新进水判不可用、并联、422）
  snapshot_demo.py        # 状态快照回看全流程示例（四类操作冻结/读取/对照/不留痕/重导不覆盖）
  audit_chain_demo.py     # 快照摘要链核验示例（只读：链信息/区间核验/边界不符/缺端点/倒序/摘要格式）
tests/
  test_planning.py
  test_audit.py
  test_api.py
Dockerfile
docker-compose.yml
requirements.txt
```
