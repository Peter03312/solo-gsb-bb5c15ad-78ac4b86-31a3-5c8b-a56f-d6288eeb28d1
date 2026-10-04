# 港口潮汐通航行程规划服务

基于 **Python + FastAPI + SQLite** 的潮汐窗口航路规划服务。录入港口节点、单向航段
（正整数分钟航时、静水深度、可选桥底净空）与逐分钟潮位后，给定船舶吃水、水面以上
高度、安全余量及不超过 1440 分钟的规划窗口，计算**最早抵达**行程；同一抵达时刻按
**完整航段编号序列**做字典序择优（节点等待不产生航段编号、不计入序列）。规划同时
执行**航段独占**：两艘已确认船舶不得在同一航段
的同一分钟占用（每段从进入分钟 `d` 到抵达前一分钟 `d+t-1` 独占；节点等待不占航段），
既有已确认快照即使彼此重叠也原样保留、作为后续规划必须避开的占用。潮位以**修订号**
版本化管理，分两类：

- **名义潮位更正**（`POST /api/tide/corrections`）：以某修订为基础全量继承名义潮位
  **与误差**，仅覆盖给定分钟的名义潮位（被覆盖分钟的误差仍继承，新分钟误差为 0），
  生成新修订；
- **预测误差修订**（`POST /api/tide/error-revisions`）：潮位预测存在**逐分钟误差**，
  调度员按「非负整分钟 → 非负误差（米）」提交更改；**名义潮位保持不变**，未列出分钟
  继承基础修订误差；旧修订一律视为误差 **0**。

修订号自增、内容**不可变**。核验水深/净空时按修订的**误差带取保守极值**：

- 水深：`静水深度 + 名义潮位 − 误差 ≥ 吃水 + 安全余量`（真实潮位可能偏低）
- 桥高：`桥底净空 − 名义潮位 − 误差 ≥ 水面以上高度 + 安全余量`（真实潮位可能偏高）
- 等待不占用航段，不校验水深/净空。

确认行程时在同一数据库事务中复核修订号与行程，过期即冲突且不保存，已确认行程作为
不可变快照长期保留。潮位更正（或误差修订）后，调度员可凭**已确认行程号 + 目标潮位
修订号**复核原航路：按快照原占用分钟逐段用误差带校验水深与桥高；原行程不安全时，
在保持航段编号顺序、船舶参数、原起始分钟与规划窗口不变、仅允许节点重新等待的前提下
求**改期行程**（变动航段最少 → 抵达最早 → 出发分钟序列字典序），窗口内无解则只返回
风险与无解标记，**绝不修改已确认快照**。

**本轮新增同修订批次**：调度员可把**同一潮位修订**下按优先顺序排列的 **2～5 艘船**
作为一个批次**预排**（`POST /api/trips/batch/plan`，只读不占位）——逐艘沿用单船
最早抵达 + 同刻航段序列字典序择优，后船同时避开**全部已确认占用**与**本批前船的拟
占用**，任一船无解即返回首个失败序号；随后用**批次幂等键**一次性**确认整批**
（`POST /api/trips/batch/confirm`）：单事务内核订最新修订与当时占用、逐艘复算并
逐段比对，过期 `REVISION_STALE`、任一船无解或不一致 `PLAN_MISMATCH` 且**全批不
保存**；通过则一次写入全部不可变快照。同键同内容重试返回原结果（`replayed=true`，
即使修订已更新），同键异内容 `BATCH_KEY_CONFLICT`。旧单船接口语义完全不变。

**本轮新增航段分时航时管制**：调度员以航段编号提交一组**半开分钟区间
`[start, end)` 及正整数通行分钟**（`PUT /api/segments/{seg_id}/travel-controls`），
**整组替换**该航段旧管制并返回生效清单；空组恢复航段基础航时。拒绝未知航段
（`SEGMENT_NOT_FOUND`）、非法区间（`INVALID_CONTROL_INTERVAL`）及组内交叠
（`OVERLAPPING_CONTROL_INTERVAL`），**失败不改旧管制**。船舶在**进入航段的分钟**
选定航时——命中区间按该区间通行分钟计，途中跨越管制边界也不变，未命中区间沿用
航段基础航时。单船规划/确认与同修订批次预排/确认均按**实际航时**计算抵达、全程
潮位/桥高与航段独占；确认时在原事务中以**当时管制**重算，变化导致无解或行程不一致
仍按 `PLAN_MISMATCH` 拒绝且不保存（批次保持全成全败与同键重放）。**已确认快照的
既有占用时长不随管制变化**；潮位复核仍按原快照占用，仅在发生改期时以当前管制求解。
旧接口路径与字段完全不变。

**本轮新增通行费费率版本、账单与冲销单**：财务按航段提交整组**半开分钟费率
区间（起点非负、终点大于起点、费率为非负整数分/分钟）**
（`PUT /api/segments/{seg_id}/toll-rates`），**整组替换该段旧费率并继承其他
航段**，形成全局不可变费率版本（版本号自增）；组内交叠或未知航段**整次拒绝**，
**未覆盖分钟费率为 0**。港口对已确认行程开单（`POST /api/trips/{id}/bill`，
输入行程号）：同一事务按**当时最新费率版本**与**确认快照中各段进入分钟**选
费率，以**固化航时**计费，输出版本号、逐段金额与整数分总额；**旧账单不随
费率改变**。每个行程只开一次，**重试返回原账单**（200、`replayed=true`）；
未知行程 `404 TRIP_NOT_FOUND`，**已作废且未开单**的行程拒绝开单
（`409 TRIP_ALREADY_VOIDED`）。**作废已开单行程时在原作废事务内生成等额
冲销单**（逐段金额与总额取账单相反数、版本沿用账单版本），重复作废不重复
冲销，账单与冲销单均可查。开单/作废并发不遗漏或重复冲销，且不改已确认快照、
批次幂等结果或规划占用；**原作废接口的请求、响应和状态码保持不变**。

**本轮新增误确认行程作废**：调度员误确认后，可凭**行程号 + 非空作废原因**
将该行程作废（`POST /api/trips/{trip_id}/void`），返回行程号、原因与作废时间；
作废记录可按行程号查询（`GET /api/trips/{trip_id}/void`，未知行程
`TRIP_NOT_FOUND`、尚未作废 `VOID_RECORD_NOT_FOUND`）。**重复作废返回首次记录**
（HTTP 200、`replayed=true`，原因与时间均不覆盖）。作废**不删除**任何确认
数据——已确认行程与原批次确认结果仍作为**历史快照**可查（`GET /api/trips*`
新增可空字段 `voided_at`，其余字段完全不变），作废只使该行程按**固化航时**
计算的航段占用及**狭窄水道组**展开占用从**后续调度**中失效：单船/批次的
预排、确认复算与改期建议此后都只避开**仍有效**的快照。作废与确认在
`BEGIN IMMEDIATE` 单事务中串行，二者并发也不会产生相互重叠的有效确认行程；
已作废批次行程的**原幂等键重放仍返回首次确认结果**、不重新产生占用；对已
作废行程的原行程**潮位安全复核仍按历史快照**执行（改期建议则按当前有效占用）。

**本轮新增狭窄水道组**：调度员可把**至少两条现有航段**登记为同一狭窄水道组
（`POST /api/channel-groups`，提交航段编号集合，返回组号及成员）。组建成后
**不可修改**（无更新/解散），航段**不得重复归组**；未知航段
（`SEGMENT_NOT_FOUND`）、提交含重复编号（`DUPLICATE_SEGMENT_IN_GROUP`）或
成员已归组（`SEGMENT_ALREADY_GROUPED`）一律拒绝，单事务内**不留半成品**。
船进入组内任一航段后，从**进入分钟到抵达前一分钟**（`d..d+t-1`）**独占整组**——
不同成员航段的通行也不得重叠，抵达分钟 `d+t` 可由下一船进入。**组建前**的已确认
快照按其**原航段与固化航时**计入新组占用，既有快照**不改写**、组建前已有重叠也
**不清理**。单船与批次**预排**均须避开组占用；**确认**时在原事务内按**当前组
关系**与已确认及批内拟占用复算，不一致沿用 `PLAN_MISMATCH` 且整批回滚（批次的
同键重放语义不变）。**改期建议**须避开其他快照的组占用并排除自身，而原行程
**安全复核仍只判断潮位**。旧接口路径与字段完全不变。

**本轮新增船舶档案**：调度员按**唯一船号**管理船舶档案——**非负吃水、水面
以上高度、安全余量及启停状态**（`POST /api/ships` 建档，初始版本 1；
`PUT /api/ships/{ship_no}` 变更，未给出的字段沿用当前值）。每次**有效变更**
（四项中任一项实际变化）使版本号递增，未实际改变任何字段的提交不递增版本；
当前版本（`GET /api/ships/{ship_no}`）与全部历史版本
（`GET /api/ships/{ship_no}/versions[/{version}]`）均可查，历史版本不可变。
重复船号（`SHIP_EXISTS`）、未知船号（`SHIP_NOT_FOUND`）、非法参数
（`VALIDATION_ERROR`）明确拒绝且不留半成品。单船与同修订批次**预排**可用
**船号代替三项参数**（`ship_no` 与 `draft/height/margin` 互斥，**混用两种输入
一律 422 拒绝**），按档案**当前启用版本**的参数规划（档案已停用
`SHIP_DISABLED`），结果回传**船号、版本及所用参数**；直接提交参数的既有请求、
响应与择优规则完全兼容。档案行程**确认**须回填规划所得**船号、版本及行程**，
在原事务内先核对档案**仍启用且版本未变**（版本已变 `SHIP_VERSION_STALE`），
再按现有潮位、航时管制与占用复算；任一失效即拒绝且不保存，**批次全退**。
已确认快照**固化所用参数及档案引用**（船号 + 版本），潮位复核与改期建议只用
快照参数，后续档案变更不改写历史；作废与批次同键重放沿用旧语义。

**本轮新增不安全航次改期确认**：调度员凭**已确认行程号、目标潮位修订号、
只读复核建议中的逐段出发分钟与非空幂等键**执行不安全航次的改期
（`POST /api/trips/{id}/reschedule`）。仅当①原行程仍有效（未作废）、②目标
修订仍为最新、③原快照在该修订下按误差带复核**不安全**、④窗口内有解时，才在
**同一 `BEGIN IMMEDIATE` 事务**内按原航段顺序、**船舶快照参数**、原起始分钟、
原窗口及泊位作业分钟，依**当前航时管制、狭窄水道组、泊位日历与其他有效占用
（排除原行程）**重算最优建议，并逐段核对提交的出发分钟；未知行程
`TRIP_NOT_FOUND`、方案不一致 `PLAN_MISMATCH`、关联档案船已停用
`SHIP_DISABLED`/版本已变 `SHIP_VERSION_STALE`/档案已删 `SHIP_NOT_FOUND`、修订
过期 `REVISION_STALE`、原行程仍安全 `RESCHEDULE_NOT_NEEDED`、窗口无解
`RESCHEDULE_NO_SOLUTION` 均**拒绝且不改变状态**。通过后在该事务内**作废原
行程并确认新快照**——旧占用（航段/狭窄水道组/泊位作业）失效、新占用生效；
**原账单若存在在该事务内等额冲销一次**（金额取反、费率版本沿用原账单），
**新行程待另行开单**。响应给出新旧行程号、复算行程与冲销状态。**同键同内容
重试返回首次结果**（`replayed=true`，即使此后修订又变化也先重放），同键异内容
`RESCHEDULE_KEY_CONFLICT`。原确认快照、批次幂等记录及现有接口契约保持不变。

**本轮修复：正航时回路下同刻字典序航路丢失**。旧 DP 对每个状态
`(节点, 分钟)` 只保留字典序最小的航段编号前缀；但航段航时均为正整数，
正航时回路（如 `B→C→B`）会让**长度不同**的序列在同一 `(节点, 分钟)` 会合，
当较短序列恰是较长序列的前缀时，旧剪枝会反转追加同一后缀后的字典序关系，
从而把同刻字典序最小的**完整**航路误删。修复后规划改为三遍时间展开 DP：
①正向可达求**最早抵达**（第一目标不变）；②反向可达标记仍能按时抵达/开工
的状态；③自起点贪心重建，在等待与各可行航段导出的**完整序列**间取字典序
最小者（等待不产生编号、不计入序列）。潮位误差带、分时航时、航段及狭窄水
道组独占、终点泊位作业约束仍是可行性判断的一部分，语义不变。例：航段
1 `A→B`、2 `B→C`、3 `C→B`、4 `B→D` 各耗时 1 分钟，1～3 在分钟 0～3 可
通行，4 仅分钟 3 满足水深；从 A 于分钟 0 起、窗口 4 分钟，旧结果
`[1,4]`，修复后 `[1,2,3,4]`，两者均于分钟 4 抵达。单船预排/确认复算与
批次预排/确认共用同一规划内核，结果保持一致；**已确认快照不改写**，请求/
响应字段、错误码、批次幂等重放与改期幂等重放均保持兼容（见
[5. 算法说明](#5-算法说明)）。

---

## 1. 启动

### 方式一：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up --build -d
```

服务启动时自动：

1. 在命名卷 `tide-data` 中创建 SQLite 数据库 `/data/tide.db`；
2. 建表；
3. 若库为空（`SEED_DEMO=true`，默认），写入演示数据：
   - 节点 `A（外锚地）、B、C、D（内港码头）`
   - 航段 `1:A→B(30min,深6m)`、`2:B→C(40min,深4m,净空8m)`、`3:C→D(20min,深5m)`、`4:B→D(90min,深3.5m)`
   - 潮位修订 `#1`：分钟 `0..1439`，名义潮位 `2.0 + 1.5·sin(2π·m/720)`（米），
     预测误差 0（可随后经 `/api/tide/error-revisions` 登记逐分钟误差）

查看日志 / 停止：

```bash
docker compose logs -f
docker compose down            # 停止（保留数据卷）
docker compose down -v         # 停止并删除数据库卷（下次启动重新初始化）
```

### 方式二：Docker

```bash
docker build -t tide-planner .
docker run -d --name tide-planner -p 8000:8000 \
  -v "$PWD/data:/data" -e DB_PATH=/data/tide.db tide-planner
```

### 方式三：本地 Python（3.11+）

```bash
pip install -r requirements.txt
DB_PATH=./tide.db SEED_DEMO=true uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 地址与端口

| 项目 | 值 |
| --- | --- |
| 服务地址 | http://localhost:8000 |
| 健康检查 | http://localhost:8000/health |
| Swagger UI（可在线调用） | http://localhost:8000/docs |
| OpenAPI 文档 | http://localhost:8000/openapi.json |

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/tide.db` | SQLite 数据库文件路径，所在目录自动创建 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |
| `SEED_DEMO` | `true` | 空库启动时是否写入演示节点/航段/初始潮位修订 |
| `MAX_WINDOW` | `1440` | 规划窗口上限（分钟） |

---

## 2. 领域规则

- **节点**：港口位置点。**航段为单向**，航时必须为**正整数**分钟；可设桥底净空（不设表示无桥）。
- **时间轴**：绝对"分钟"（非负整数），与潮位表的 `minute` 对齐。规划窗口为
  `[start_minute, start_minute + window_minutes)`，长度 ≤ 1440。
- **等待**：船可在任一节点的任意整分钟等待（等待不占用水深/净空资源，**不参与校验**，
  也**不占用航段**）。
- **航段分时航时管制**：调度员可按航段维护一组**半开分钟区间 `[start_minute,
  end_minute)` 与正整数通行分钟**，整组替换、空组恢复基础航时：
  - 航时在船**进入航段的分钟**选定：命中某区间即按该区间的 `travel_time` 计航时，
    **途中跨越管制边界航时也不变**；未命中任何区间沿用航段基础航时。
  - 因此进入时刻不同，同一航段的实际航时与占用分钟数可能不同；抵达 = 进入分钟 +
    当时选定的实际航时。等待发生在节点上，下一段航时按该段**自身**进入分钟重新选定。
  - 校验口径不变：实际航时覆盖的**每一分钟**仍须满足潮位/桥高误差带与航段独占；
    实际航程越出窗口的进入分钟不可行。
  - 管制的提交、替换与错误：未知航段 `404 SEGMENT_NOT_FOUND`；区间
    `start<0`、`end<=start` 或通行分钟非正 → `422 INVALID_CONTROL_INTERVAL`；
    组内任两个区间分钟交叠（半开含义下 `next.start < prev.end`）→
    `422 OVERLAPPING_CONTROL_INTERVAL`；首尾相接（`prev.end == next.start`）合法。
    任一校验失败**旧管制原样保留**（替换在单事务内先全删后插，失败回滚）。
  - **已确认快照的占用时长锁定**：快照中的每段 `travel_time` 与占用分钟
    （`d..d+t-1`）在确认时固化，之后管制如何变化都不改写；`load_occupancy` 仍按
    快照行程的 `travel_time` 计算占用。新规划只对**尚未进入的新航程**适用当前管制。
  - **确认 / 批次 / 复核改期与管制**：单船确认与批次确认在原事务中以**当时管制**
    重新求解，管制变化导致窗口无解或与提交行程逐段不一致 → 既有 `409
    PLAN_MISMATCH`（不保存 / 全成全败），幂等重放仍优先且返回首次结果；复核原航路
    时潮位检查仍按**原快照占用分钟**（不随管制变化），仅当需要**改期**时以**当前
    管制**求各段航时与出发分钟。
- **航段占用（独占）**：于整分钟 `d` 进入、航时 `t`，则占用 `d, d+1, …, d+t-1` 共
  `t` 分钟，于 `d+t` 抵达；**抵达分钟 `d+t` 已不占用该航段**（后船可在该分钟进入）。
  - 同一 `(航段, 分钟)` 同一时刻只允许一艘已确认船舶占用；不同航段互不相干。
  - **已确认快照即占用事实**：规划与确认重算必须避开所有既有已确认快照的占用；
    既有快照**即使彼此重叠也原样保留**（历史数据不清理、不改写），重叠分钟对后续
    规划仍然封闭。
  - **规划接口只读、不占位**：调用 `/api/trips/plan` 不产生任何占用，只有
    `/api/trips/confirm` 成功后才生成占用。
- **狭窄水道组（独占整组）**：调度员可把至少两条现有航段登记为同一狭窄水道组。
  - **组建**（`POST /api/channel-groups`）：提交航段编号集合（至少 2 条，
    schema 层 `VALIDATION_ERROR` 兜底），返回组号（自增）与成员（按提交次序）。
    组**建成即不可变**：无更新、无解散接口；每条航段至多属于一个组。
  - **拒绝口径（任一失败均不留半成品，组建在单事务内完成）**：
    提交编号含重复 → `422 DUPLICATE_SEGMENT_IN_GROUP`；任一航段不存在 →
    `404 SEGMENT_NOT_FOUND`（`details.unknown_seg_ids`）；任一航段已归组 →
    `409 SEGMENT_ALREADY_GROUPED`（`details.conflicts` 给出冲突航段与既有组号，
    并发竞争由数据库唯一约束兜底同样返回此码）。
  - **独占整组**：船于分钟 `d` 进入组内**任一**成员航段、该段固化航时 `t`，
    则 `d..d+t-1` 这同一批分钟对组内**全部**成员航段封闭（于 `d+t` 抵达，
    抵达分钟可由下一船进入任一成员航段）。即不同成员航段的通行也不得重叠；
    一条船连续走过组内多段时，其前后各段占用按同一展开口径也不会误伤自身
    （预排/复算只把**其他船**的占用计入 blocked）。
  - **组建前快照回溯计入、但不改写**：组建后，组建前已确认快照只要其原行程
    经过组内任一航段，该段按**原航段与固化 `travel_time`** 的占用即展开到全组；
    快照本身绝不改写，组建前快照间**已有的重叠也不清理**（展开后重叠分钟仍
    原样对后续规划封闭）。
  - **对既有流程的影响**：单船 `/api/trips/plan`、批次 `/api/trips/batch/plan`
    自动避开展开后的组占用（批次后船还须避开前船**拟占用**的组展开）；单船
    `/api/trips/confirm` 与批次 `/api/trips/batch/confirm` 在原事务内按
    **当前组关系** + 当时已确认占用（批次另含批内复算占用）重新求解，组关系
    变化使窗口无解或行程不一致仍为 `409 PLAN_MISMATCH`（批次整批回滚），
    同键重放仍优先返回首次结果；**改期建议**避开其他快照的组占用但排除自身
    快照（`exclude_trip_id`），而**原行程潮位安全复核完全不读取占用/组关系**。
    查询：`GET /api/channel-groups`（全量）、`GET /api/channel-groups/{id}`
    （详情，未知组 `404 CHANNEL_GROUP_NOT_FOUND`）。
  每个修订的逐分钟数据含**名义潮位 `level`** 与**非负预测误差
  `error`（米）**，真实潮位落在 `[level−error, level+error]`。占用的**每一分钟**都
  必须同时满足（按误差带取对应极值）：
  - 水深：`静水深度 + 名义潮位 − 误差 ≥ 吃水 + 安全余量`
  - 桥高（仅当该航段有桥）：`桥底净空 − 名义潮位 − 误差 ≥ 水面以上高度 + 安全余量`
  - 独占：该 `(航段, 分钟)` 未被其他已确认快照占用
- **择优**：到达分钟最小；同一到达分钟，按航段编号（`seg_id`，自增）组成的**完整
  序列**做字典序比较，序列小者优先（先比较首段编号，相同再比较下一段）；节点等待
  不产生航段编号、不计入序列。正航时回路可能使长度不同的序列在同一 `(节点, 分钟)`
  会合，规划不再因此剪掉较长前缀（三遍 DP，见算法说明）。
- **潮位修订**：修订号自增、内容不可变，分两种提交方式（均可省略 `base_revision`
  而默认基于**最新**修订；未知基础修订报 `TIDE_REVISION_NOT_FOUND`）：
  - **名义潮位更正** `/api/tide/corrections`：全量继承基础修订的名义潮位与误差，
    仅覆盖给定分钟的名义潮位（被覆盖分钟**继承误差**；基础修订没有的分钟作为新
    分钟补入、误差取 0）；
  - **预测误差修订** `/api/tide/error-revisions`：载荷为 `{分钟: 非负误差}`，
    **名义潮位不变**；未列出分钟**继承**基础修订的误差，列出分钟覆盖为给定误差；
    误差必须非负（否则 `422 VALIDATION_ERROR`）；所提交分钟在基础修订中必须已有
    名义潮位，否则 `422 MISSING_TIDE_DATA`（误差修订不能凭空新增名义潮位）。
  - **旧修订视为误差 0**：早于误差功能的修订（含旧版数据库升级后的既有行）均无
    误差数据，按误差 0 处理；规划/复核目标窗口内任一分钟缺**名义潮位**即报
    `MISSING_TIDE_DATA`。
- **规划**：默认使用**最新**修订，也可显式指定修订号。窗口内任何一分钟缺少名义
  潮位即报错。水深/净空均按所据修订的误差带保守极值核验，且每个出发区间都不得与
  既有已确认快照的航段占用重叠（重叠即视为该进入分钟不可行，等价于低潮/净空不足）。
- **确认**：请求必须携带规划所据 `revision`。服务在单个 SQLite 事务（`BEGIN IMMEDIATE`）中：
  1. 检查该修订是否仍为最新——否则 `409 REVISION_STALE`，**不保存**；
  2. 依据该修订与**当时全部已确认占用**（同一事务内读取，既有快照即使重叠也计入；
     本行程尚未落库故不包含自身）**重新计算**——若窗口内因占用变化已无可行行程，
     或重算结果与提交行程逐段不一致，均为 `409 PLAN_MISMATCH`，**不保存**；
  3. 复核通过后保存**当时的结果快照**（修订号、参数、到达分钟、完整行程 JSON）。
     之后再发生潮位更正不影响已确认行程；快照的占用也随之永久生效，后续规划/确认
     必须避开（快照彼此重叠亦原样保留）。
- **批次预排**（同一潮位修订下 2～5 艘船）：
  - `ships` 按**优先顺序**给出（2～5 艘，数量越界或任一船字段非法均
    `422 VALIDATION_ERROR`）；整批共用一个 `revision`（可省略，省略时解析为
    **最新**修订，预排响应回带实际所据修订号）。
  - 逐艘调用与单船完全相同的规划（最早抵达 + 同刻航段编号序列字典序）；第 i 艘船
    须避开的占用 = 窗口覆盖区间内的**全部已确认快照占用** ∪ 本批第 0..i-1 艘船的
    **拟占用**（与已确认占用同口径：进入分钟 `d` 占 `d..d+t-1`，抵达分钟不占）。
  - 预排**只读、不占位**：不写库、不产生占用，可任意重复调用；每次调用内部从第 0 艘
    重新串行规划。
  - 任一船规划失败时按**首个失败序号**（`details.ship_index`，0 起）返回，沿用既有
    错误码与状态：`NODE_NOT_FOUND`(404) / `MISSING_TIDE_DATA`(422) /
    `NO_FEASIBLE_ITINERARY`(404)。
- **批次确认**：请求携带**预排所据修订号**（必填）、各船按预排结果原样回填的
  `itinerary` 与**批次幂等键** `idempotency_key`（非空字符串，≤128 字符）。服务在
  单个 SQLite 事务（`BEGIN IMMEDIATE`）中：
  1. 幂等键已存在且内容（修订号 + 各船参数与回填行程的规范化哈希）一致 ->
     **直接返回首次结果**（HTTP 200、`replayed=true`），**不重新计算、不新增快照**，
     即使其间修订已更新或占用已变化；同键异内容（含不同修订号）->
     `409 BATCH_KEY_CONFLICT`；
  2. 新键：先核订修订仍为最新，否则 `409 REVISION_STALE`，全批不保存；
  3. 同一事务读取**当时全部已确认占用**，按请求顺序逐艘复算：窗口内无解 ->
     `409 PLAN_MISMATCH`（`details.ship_index` 指出首个失败序号、
     `reason=no_feasible_itinerary`）；复算行程与提交行程逐段不一致（或抵达分钟
     不一致）-> `409 PLAN_MISMATCH`（`reason=itinerary_mismatch`）；前船的**复算**
     行程随即并入后船的当时占用；任一船失败**全批回滚、不保存任何快照**；
  4. 全部通过后在同一事务内一次写入各船不可变快照（与单船确认写入同一张
     `confirmed_trips` 表，立即可经 `/api/trips` 读取、占用立即生效）并登记幂等记录，
     返回各船 `index / trip_id / arrival_minute / itinerary`。
- **更正后复核 / 改期**（只读，不改写快照、建议不占位）：携带已确认行程号与目标
  修订号（名义更正或误差修订均可），
  1. **原航路复核**：**只按目标修订检查潮位**——按快照中的航段编号顺序与原占用分钟
     （进入分钟 `d` 占用 `d..d+t-1`），依目标修订逐分钟的名义潮位与**误差带**逐段
     校验（水深用 `名义−误差`、净空用 `名义+误差` 的保守极值）；**不检查**与其他
     已确认快照的占用重叠（既有快照彼此重叠原样保留，不构成潮位风险）。首分钟违规即
     判定原行程不安全，返回首次违规的航段（`leg_index`/`seg_id`）、分钟及原因
     （`depth` 水深不足 / `clearance` 桥高净空不足），并给出该分钟实际/要求的水深与
     净空——实际值按**对应极值**计算
     （`available_depth = 静水深度 + 名义潮位 − 误差`、
     `available_clearance = 桥底净空 − 名义潮位 − 误差`），另返回
     `tide_level`（名义潮位）与 `tide_error`（误差）；
  2. **改期**：不安全时启用，仍按原规则（误差带）求可行进入分钟。**保持**原航段
     编号顺序、船舶参数（吃水/水面以上高度/余量）、原起始分钟与规划窗口（窗口内
     占用才合法），**仅允许在节点重新等待**——即各段首尾相接、只调整每段出发分钟。
     改期建议还须**避开其他已确认快照的航段占用，但排除自身快照**（自身原占用不挡
     自己；建议本身不占位、不新增确认）。择优依次为：
     ① 相对快照**出发分钟变动的航段个数**（`changed_leg_count`）最少；
     ② **抵达分钟最早**；③ 前述均相同，按各段出发分钟序列做字典序（小者优先）；
  3. 原行程安全时直接返回 `safe=true`（零航段行程恒安全，无需改期）；潮位可行但
     所有可行进入区间均被其他快照占满、窗口内无改期解时返回原行程风险加
     `no_solution=true`，此时仍可在 `/api/trips` 读到原封不动的快照。缺名义潮位、
     未知行程、未知修订沿用 `MISSING_TIDE_DATA` / `TRIP_NOT_FOUND` /
     `TIDE_REVISION_NOT_FOUND` 既有错误语义。
- **不安全航次改期确认**（执行复核建议；`POST /api/trips/{trip_id}/reschedule`）：
  调度员凭**已确认行程号、目标潮位修订号、复核建议中的逐段出发分钟
  （`departure_minutes`，绝对分钟、按原航段顺序）与非空幂等键
  （`idempotency_key`）**执行。整个状态变更在单个 `BEGIN IMMEDIATE` 事务内，
  条件与动作如下：
  - **前提（任一不满足即拒绝且不改变状态）**：
    1. **原行程仍有效**：快照存在（未知 -> `404 TRIP_NOT_FOUND`）且未作废
       （已作废 -> `409 TRIP_ALREADY_VOIDED`）；
    2. **档案核对**（仅当原快照固化了船号引用）：关联档案船仍须**启用且版本与
       快照一致**——档案被删 `404 SHIP_NOT_FOUND`、停用 `409 SHIP_DISABLED`、
       版本已变 `409 SHIP_VERSION_STALE`。复算船舶参数始终取**快照**的吃水/
       水面以上高度/安全余量，不读取档案当前参数；
    3. **目标修订为最新**：修订未知 `404 TIDE_REVISION_NOT_FOUND`；目标修订
       已过期（其后又生成更新修订）-> `409 REVISION_STALE`；
    4. **原快照在该修订下不安全**：与只读复核同一误差带口径按原占用分钟逐段
       校验；原行程仍安全 -> `409 RESCHEDULE_NOT_NEEDED`；
    5. **窗口内有解且方案一致**：按**原航段编号顺序、快照船舶参数、原起始
       分钟、原窗口及泊位作业分钟**，依**当前分时航时管制、狭窄水道组、泊位
       开放日历与其他有效占用（排除原行程）**重算最优改期建议（择优与只读
       复核一致：变动航段数最少 -> 抵达最早 -> 出发分钟序列字典序最小）。
       窗口内无解 -> `409 RESCHEDULE_NO_SOLUTION`（details 带首次违规）；
       `departure_minutes` 段数与快照航段数不符 -> `422 VALIDATION_ERROR`，
       含负值/缺失/幂等键为空或超长 -> `422 VALIDATION_ERROR`；与复算最优
       出发分钟不一致 -> `409 PLAN_MISMATCH`（details 回传 `recalculated`）。
  - **通过后的同事务动作**：①插入**新确认快照**（固化目标修订号、快照船舶
    参数与档案引用、复算行程、原泊位与即时作业区间，新占用随即生效）；
    ②插入原行程的**不可变作废记录**（系统生成原因，旧占用即时失效，历史
    快照与 `GET /api/trips*` 查询契约不变）；③**原账单若存在，在该事务内
    等额冲销恰好一次**（沿用作废冲销语义：逐段金额与总额取账单原值相反数、
    费率版本沿用账单版本；经 `GET /api/trips/{旧id}/bill` 与
    `/reversal` 可查）；原行程未开单则无冲销。**新行程不在本接口开单，待
    财务另行调用** `POST /api/trips/{新id}/bill`。
  - **响应**：`old_trip_id`/`new_trip_id`、新旧修订号、与提交一致的复算
    行程 `reschedule`、原作废记录 `old_void` 与冲销状态
    （`reversal_created` + 可空 `reversal`）。首次成功 `201`。
  - **幂等**：同键同内容（行程号/目标修订/逐段出发分钟指纹一致）重试返回
    **首次结果** `200 replayed:true`——服务不复算、不再作废/确认/冲销，
    **即使此后修订又变化、原行程已被本次改期作废，也先重放**；同键异内容
    （含换行程号或目标修订）-> `409 RESCHEDULE_KEY_CONFLICT`，不保存。
    被拒绝的请求（校验/冲突失败）不登记幂等记录，同一键之后仍可用于首次
    成功提交。
  - **不改变的东西**：原确认快照行不删不改（仅新增作废记录）、批次幂等记录
    （`batch_confirmations`）与既有全部接口的请求/响应契约不变；只读复核
    `/review` 仍不写任何状态。
- **误确认行程作废**：调度员误确认后，可凭**行程号与非空作废原因**作废已确认行程。
  - **作废接口**（`POST /api/trips/{trip_id}/void`，请求体
    `{"reason": "…"}`）：原因剥离首尾空白后不能为空（空串/纯空白 ->
    `422 VALIDATION_ERROR`）；未知行程 -> `404 TRIP_NOT_FOUND`。首次作废
    返回 `201` 与 `{trip_id, reason, voided_at, replayed:false}`；**重复
    作废**（同一行程再次调用，即使给出不同原因）返回 `200`、
    `replayed:true` 与**首次**的 `reason/voided_at`，绝不覆盖。作废记录
    不可变（数据库 `voided_trips` 中每行程唯一一行，唯一约束兜底并发）。
  - **作废记录查询**（`GET /api/trips/{trip_id}/void`）：已作废返回
    `{trip_id, reason, voided_at}`（首次原因与时间）；未知行程
    `404 TRIP_NOT_FOUND`；行程存在但未作废 `404 VOID_RECORD_NOT_FOUND`。
  - **历史快照保留、仅占用失效**：作废**不删除** `confirmed_trips` 行、
    不改写 `itinerary`，也不改动 `batch_confirmations` 中的批次首次结果。
    `GET /api/trips` / `GET /api/trips/{id}` 对已作废行程照常返回原确认快照
    （新增可空字段 `voided_at`：未作废为 `null`，已作废为首次作废时间；
    其余字段与旧响应完全一致）。作废唯一的调度效果是：该行程每段按快照
    **固化航时** `d..d+t-1` 的航段占用与**狭窄水道组展开占用**从
    `load_occupancy` 中排除——即对后续单船/批次的**预排、确认复算、改期
    建议**不再封闭；新规划可以立即重新使用这些分钟。
  - **只避开仍有效的快照**：单船 `/api/trips/plan`、`/api/trips/confirm`
    与批次 `/api/trips/batch/plan`、`/api/trips/batch/confirm` 的占用读取
    统一经 `voided_trips` 过滤；`review` 的改期建议同样只避开未作废的其他
    快照（并继续排除被复核行程自身）。占用释放后以占用期的旧规划结果去
    确认，仍按既有语义 `409 PLAN_MISMATCH` 拒绝（批次整批回滚）。
  - **作废与确认并发串行**：作废与确认都在单个 `BEGIN IMMEDIATE` 事务中
    完成「状态变更 + 占用复算」，SQLite 写锁将二者串行——确认先提交则
    后到的作废看到新快照（新快照不受影响）、作废先提交则后到的确认在
    事务内读到释放后的占用，按复算结果决定成功或 `PLAN_MISMATCH`，
    **任何顺序下都不会产生相互重叠的有效确认行程**。
  - **批次幂等重放不受作废影响**：批次中的某条行程被作废后，用该批次的
    **原幂等键 + 原内容**重放 `POST /api/trips/batch/confirm`，仍返回
    `200 replayed:true` 与**首次相同的各行程号**（包含已作废的行程号），
    服务不复算、不新增快照、不重新产生占用；同键异内容仍
    `BATCH_KEY_CONFLICT`。
  - **复核仍按历史快照**：对已作废行程调用
    `POST /api/trips/{id}/review` 时，原航路潮位安全复核仍读取其**原确认
    快照**的航段顺序与占用分钟（与作废无关）；只有在需要改期时，建议
    才按当时**仍有效**的其他快照（及当前管制/组关系）求解。
- **通行费费率版本**：财务按航段提交整组**半开分钟费率区间
  `[start_minute, end_minute)` 与非负整数费率（分/分钟）**，每次成功提交
  生成一个**全局不可变费率版本**（版本号自增）：
  - **整组替换 + 全局继承**（`PUT /api/segments/{seg_id}/toll-rates`）：
    新版本继承前一版本的**全部航段**费率区间，仅把被提交航段的区间整组
    替换为本次内容；`rates: []` 表示该航段此后**未覆盖分钟费率为 0**。
    第一个版本无需基线（其他航段即隐式为 0）。版本一经生成永不修改。
  - **区间校验**：起点非负、终点大于起点、费率为非负整数（允许 0，免费
    时段）-> 非法 `422 INVALID_TOLL_RATE_INTERVAL`；组内半开区间交叠
    （`next.start < prev.end`；首尾相接合法）->
    `422 OVERLAPPING_TOLL_RATE_INTERVAL`；未知航段
    `404 SEGMENT_NOT_FOUND`。任一失败**整次拒绝、不生成新版本**，旧版本
    原样保留。
  - **未覆盖分钟费率为 0**：开单时某段进入分钟未命中该航段任何费率区间
    （或该航段在该版本无任何区间），费率取 0，不报错。
  - **版本查询**：`GET /api/toll/rate-versions`（全部版本，各含其全部航段
    费率区间）、`GET /api/toll/rate-versions/{version}`（未知版本
    `404 TOLL_RATE_VERSION_NOT_FOUND`）。
- **通行费账单**：港口对**已确认行程**出具通行费账单，输入只有行程号
  （`POST /api/trips/{trip_id}/bill`，无请求体）。
  - **同一事务固化**：在单个 `BEGIN IMMEDIATE` 事务内取**当时最新费率
    版本**，读取**确认快照** `itinerary`，每段按其**进入分钟**
    （快照的 `start_minute`，与分时航时管制同一"进入分钟选定"口径；途中
    跨区间边界不重选）在该版本中选费率；段金额 = 快照**固化航时**
    `travel_time` × 费率，账单总额 = 各段金额之和（**整数分**）。账单
    固化版本号、逐段金额与总额；**旧账单不随之后的新费率版本改变**。
  - **每行程只开一次**：首次开单 `201`、`replayed:false`；对同一行程重试
    返回**原账单** `200`、`replayed:true`（版本/金额/开单时间均为首次值，
    即使其间费率已更新）。未知行程 `404 TRIP_NOT_FOUND`；**已作废且未
    开单**的行程拒绝开单 `409 TRIP_ALREADY_VOIDED`；系统中尚无任何费率
    版本时 `409 TOLL_RATE_VERSION_NOT_FOUND`。零航段行程可开单，总额 0。
  - **账单查询**：`GET /api/trips/{id}/bill`（未开单 `404 BILL_NOT_FOUND`）、
    `GET /api/toll/bills`（全部账单，按行程号）；账单视图附可空字段
    `reversal`（见下），账单本身永不被作废流程改写。
  - **开单不影响既有数据**：开单与费率替换都不写 `confirmed_trips`、不改
    批次幂等结果、不改变规划占用口径。
- **作废与等额冲销**：作废**已开单**行程时，在**原作废事务内**
  （`POST /api/trips/{id}/void` 的同一 `BEGIN IMMEDIATE`）生成一张等额
  冲销单：
  - **等额**：冲销单逐段金额与总额取账单原值的**相反数**（账单非负、冲销
    非正，绝对值完全相等），费率**版本沿用账单固化版本**；未开单行程的
    作废不产生冲销单。**重复作废不重复冲销**（重复作废本就返回首次作废
    记录）。
  - **可查**：`GET /api/trips/{id}/reversal` 返回
    `{trip_id, version, total_cents(≤0), segments(金额取反), reversed_at}`；
    未知行程 `404 TRIP_NOT_FOUND`、未开单 `404 BILL_NOT_FOUND`、已开单但
    未作废 `404 REVERSAL_NOT_FOUND`。账单详情与账单列表内嵌的 `reversal`
    字段同样可读到冲销单。
  - **原作废接口不变**：作废请求体、响应字段
    （`{trip_id, reason, voided_at, replayed}`）与状态码（首次 `201`、
    重复 `200`、未知行程 `404`、空原因 `422`）完全保持原样；冲销单只在
    同事务内额外落库，通过账单/冲销查询接口获取。
  - **并发安全**：开单与作废都持有写锁、由 SQLite 串行化：作废先提交则
    后到的开单读到作废标识而 `TRIP_ALREADY_VOIDED`（无账单、无冲销）；
    开单先提交则后到的作废在**同一事务**内读到账单并原子写入冲销（有账单、
    必有冲销）。唯一约束（`toll_bills.trip_id`、
    `toll_reversals.bill_trip_id`）兜底并发竞争，**不遗漏也不重复冲销**。
- **船舶档案**：调度员按**唯一船号**管理档案，每份档案记录**非负吃水、
  水面以上高度、安全余量及启停状态**。
  - **建档**（`POST /api/ships`）：初始版本 1；重复船号
    `409 SHIP_EXISTS`、非法参数（负值等）`422 VALIDATION_ERROR`、
    空船号 `422 VALIDATION_ERROR`，失败不留半成品。
  - **变更**（`PUT /api/ships/{ship_no}`）：未给出的字段沿用当前值；
    四项中任一项**实际变化**才构成**有效变更**、版本 +1（响应
    `changed=true`）；与当前完全一致（含空提交）不递增版本
    （`changed=false`）。未知船号 `404 SHIP_NOT_FOUND`。
  - **版本可查**：当前版本 `GET /api/ships/{ship_no}`、全部历史版本
    `GET /api/ships/{ship_no}/versions`、指定版本
    `GET /api/ships/{ship_no}/versions/{version}`（未知版本
    `404 SHIP_VERSION_NOT_FOUND`）；历史版本不可变。
  - **船号预排**：单船 `/api/trips/plan` 与批次 `/api/trips/batch/plan`
    的每艘船可用 `ship_no` **代替** `draft/height/margin` 三项参数，
    按档案**当前启用版本**的参数规划（档案已停用 `409 SHIP_DISABLED`、
    未知船号 `404 SHIP_NOT_FOUND`，批次场景附 `details.ship_index`）；
    响应回传 `ship_no`、`ship_version` 及实际所用 `draft/height/margin`。
    **两种输入互斥**：同一请求同时给出船号与任一参数、或两者皆缺，均
    `422 VALIDATION_ERROR`；批次内不同船可各自选择输入方式。直接提交
    参数的既有请求、响应（新增可空 `ship_no/ship_version` 字段，值为
    `null`）与择优规则完全兼容。
  - **档案行程确认**：按船号预排后，确认须回填规划所得 `ship_no`、
    `ship_version` 及 `itinerary`（缺 `ship_version` 或无船号却带版本
    均 `422`）。确认在原事务内**先核对档案仍启用且版本未变**（停用
    `409 SHIP_DISABLED`、版本已变 `409 SHIP_VERSION_STALE`，批次附
    `ship_index`），再核订潮位修订并按现有潮位、航时管制与占用复算；
    任一失效即拒绝且不保存，**批次全退**（全成全败语义不变）。
  - **快照固化档案引用**：已确认快照写入所用参数与
    `ship_no/ship_version`（直接提交参数确认为 `null`），
    `GET /api/trips*` 新增可空字段 `ship_no/ship_version`；潮位复核与
    改期建议**只用快照参数**、不读档案，后续档案变更（含停用）不改写
    历史快照；作废与批次同键重放沿用旧语义（重放优先、不复核档案）。
- **终点泊位与开放日历（本轮新增）**：调度员可为**现有节点**登记泊位
  （全局唯一泊位编号），并以一组互不重叠的**半开分钟区间
  `[start_minute, end_minute)`** **整组替换**该泊位的开放日历。
  - **登记与日历**：`POST /api/nodes/{node_id}/berths`（泊位建成时日历为空、
    全程不开放；未知节点 `404 NODE_NOT_FOUND`、泊位编号重复
    `409 BERTH_EXISTS`）；`PUT /api/berths/{berth_id}/calendar` 整组替换
    （空组 `[]` 表示全程不开放），起点非负、终点大于起点，否则
    `422 INVALID_BERTH_INTERVAL`；组内半开交叠
    `422 OVERLAPPING_BERTH_INTERVAL`（首尾相接合法）；未知泊位
    `404 BERTH_NOT_FOUND`；**任一校验失败旧日历原样保留**。查询：
    `GET /api/berths`（可 `?node=X` 过滤）、`GET /api/berths/{berth_id}`。
  - **指定泊位预排**：单船 `/api/trips/plan` 与批次
    `/api/trips/batch/plan` 可携带**成对出现**的 `berth_id`（终点泊位）与
    `work_minutes`（**正整数**作业分钟）。船**抵达即开工**，作业区间
    `[arrival, arrival+work_minutes)` 必须：
    ① **完整落在同一开放区间**内（不得跨两个开放区间拼接）；
    ② 不与**有效确认行程**（未作废）在该泊位的固化作业区间半开重叠
    （`[a,a+w)` 与 `[b,b+v)` 在 `a<b+v 且 b<a+w` 时冲突，首尾相接合法）；
    ③ 作业区间不越规划窗口。响应回传 `berth_id`、`work_minutes` 及
    `work_start_minute`（= 抵达分钟）/ `work_end_minute`；窗口内无满足
    条件的抵达仍返回 `404 NO_FEASIBLE_ITINERARY`。
  - **等待语义**：船**不能在终点等待**（抵达即开工），但仍可在起点与
    途经节点等待——泊位约束随"最早抵达、同刻航段编号序列字典序"的既有
    择优一起求解，即选择最早的、能在泊位完成作业的抵达。
  - **校验口径**：未知泊位 `404 BERTH_NOT_FOUND`；泊位不属终点
    `422 BERTH_NOT_AT_DESTINATION`（批次附 `details.ship_index`）；作业
    分钟非正整数或与泊位编号不成对 `422 VALIDATION_ERROR`。
  - **未指定泊位完全不变**：不带泊位字段的旧请求与旧响应结构完全兼容
    （响应仅新增 4 个值为 `null` 的可空泊位字段），规划、确认、批次、
    账单逻辑均不读取泊位。
  - **确认事务内复算**：`/api/trips/confirm` 与
    `/api/trips/batch/confirm` 在原事务内按**当时开放日历与泊位占用**
    （批次另含前船**拟作业占用**，后船避前船）连同既有潮位/管制/航段
    占用一并复算；日历或泊位占用变化导致无可行抵达，或行程/泊位作业
    区间与提交不一致，仍为 `409 PLAN_MISMATCH`（批次附 `ship_index`、
    **全批回滚**）；同键同内容重放仍返回首次结果（即使日历已变化）。
  - **快照固化、日历变更不改历史**：确认成功时泊位编号、作业分钟与作业
    区间（`work_start_minute/work_end_minute`）写入不可变快照；之后
    开放日历如何替换都不改写历史，泊位占用始终按**固化区间**计算。
    作废行程即释放其泊位作业占用（与航段占用同一口径），历史快照
    （含泊位字段）仍可查。
  - **复核与改期建议**：`POST /api/trips/{id}/review` 的原航路潮位复核
    **仍只按原快照**判断安全，与泊位日历/占用无关；响应回传快照泊位
    字段。原行程不安全需要改期时，若快照指定了泊位，改期建议必须
    **同时**在**原泊位**按**当前开放日历**与当时其他有效快照的泊位占用
    找到一个可用的**即时作业区间**（抵达即开工、建议不预留）；找不到则
    `no_solution=true`（即使航段时间上可改期）。建议响应
    `reschedule` 内附 `berth_id/work_start_minute/work_end_minute`。

### 错误响应格式

```json
{ "error": { "code": "NODE_NOT_FOUND", "message": "未知节点: ZZ", "details": {"node_id": "ZZ"} } }
```

| 场景 | HTTP | code |
| --- | --- | --- |
| 起点或终点未知 | 404 | `NODE_NOT_FOUND` |
| 指定潮位修订不存在 / 复核时目标修订未知 / 误差修订的基础修订未知 | 404 | `TIDE_REVISION_NOT_FOUND` |
| 复核时行程号未知 | 404 | `TRIP_NOT_FOUND` |
| 窗口内有分钟缺名义潮位 / 误差修订提交分钟在基础修订无名义潮位 | 422 | `MISSING_TIDE_DATA`（`details` 给出缺失分钟列表与数量） |
| 窗口内无可行行程（含所有潮位可行区间均被已确认占用占满） | 404 | `NO_FEASIBLE_ITINERARY` |
| 参数校验失败（窗口>1440、航时非正、误差为负、分钟为负等） | 422 | `VALIDATION_ERROR` |
| 确认时修订已过期（单船与批次一致） | 409 | `REVISION_STALE` |
| 确认时行程复核不一致（潮位重算结果不同，或占用变化使提交行程不再最优/已无可行解） | 409 | `PLAN_MISMATCH` |
| 批次预排中任一船失败（未知节点/缺潮位/无可行行程） | 同既有码 | 既有码，`details.ship_index` 给出首个失败序号（0 起） |
| 批次确认中任一船无解或与复算不一致 | 409 | `PLAN_MISMATCH`（`details.ship_index`/`reason`，全批不保存） |
| 同批次幂等键提交了不同内容（或不同修订号） | 409 | `BATCH_KEY_CONFLICT`（不保存） |
| 批次船数不在 2～5、幂等键缺失/过长、字段非法 | 422 | `VALIDATION_ERROR` |
| 分时管制航段未知（替换/查询） | 404 | `SEGMENT_NOT_FOUND` |
| 分时管制区间非法（`start<0` / `end<=start` / 通行分钟非正整数） | 422 | `INVALID_CONTROL_INTERVAL`（失败不改旧管制） |
| 分时管制组内半开区间分钟交叠 | 422 | `OVERLAPPING_CONTROL_INTERVAL`（失败不改旧管制） |
| 狭窄水道组成员不足 2 条（含空集合） | 422 | `VALIDATION_ERROR` |
| 狭窄水道组提交编号含重复 | 422 | `DUPLICATE_SEGMENT_IN_GROUP`（不组建） |
| 狭窄水道组含未知航段 | 404 | `SEGMENT_NOT_FOUND`（`details.unknown_seg_ids`，不组建） |
| 狭窄水道组成员已归属某组（含并发竞争） | 409 | `SEGMENT_ALREADY_GROUPED`（不组建、旧组不变） |
| 查询的狭窄水道组不存在 | 404 | `CHANNEL_GROUP_NOT_FOUND` |
| 作废/查询作废记录时行程号未知 | 404 | `TRIP_NOT_FOUND` |
| 作废原因为空（空串或纯空白） | 422 | `VALIDATION_ERROR` |
| 查询作废记录时行程存在但尚未作废 | 404 | `VOID_RECORD_NOT_FOUND` |
| 船号未知（建档查询/变更/预排/确认） | 404 | `SHIP_NOT_FOUND`（批次附 `details.ship_index`） |
| 重复船号建档 | 409 | `SHIP_EXISTS`（不留半成品） |
| 档案已停用（预排/确认） | 409 | `SHIP_DISABLED`（批次附 `ship_index`，批次确认全退） |
| 确认时档案版本已变更 | 409 | `SHIP_VERSION_STALE`（`provided_version/current_version`，批次附 `ship_index`） |
| 查询的档案历史版本不存在 | 404 | `SHIP_VERSION_NOT_FOUND` |
| 船号与三项参数混用 / 两者皆缺 / 缺 `ship_version` / 非法档案参数 | 422 | `VALIDATION_ERROR` |
| 费率区间非法（`start<0` / `end<=start` / 费率为负） | 422 | `INVALID_TOLL_RATE_INTERVAL`（整次拒绝、不生成新版本） |
| 费率区间组内半开交叠 | 422 | `OVERLAPPING_TOLL_RATE_INTERVAL`（整次拒绝、不生成新版本） |
| 查询的费率版本不存在 | 404 | `TOLL_RATE_VERSION_NOT_FOUND` |
| 开单时系统中尚无任何费率版本 | 409 | `TOLL_RATE_VERSION_NOT_FOUND` |
| 对已作废且未开单的行程开单 | 409 | `TRIP_ALREADY_VOIDED`（不出账单、不出冲销） |
| 查询账单时行程尚未开单 | 404 | `BILL_NOT_FOUND` |
| 查询冲销单时已开单但行程未作废（无冲销） | 404 | `REVERSAL_NOT_FOUND` |
| 登记泊位时节点未知 | 404 | `NODE_NOT_FOUND` |
| 泊位未知（查询/日历替换/预排） | 404 | `BERTH_NOT_FOUND` |
| 泊位编号重复登记 | 409 | `BERTH_EXISTS`（不留半成品） |
| 指定泊位不属于预排终点（批次附 `ship_index`） | 422 | `BERTH_NOT_AT_DESTINATION` |
| 泊位开放区间非法（`start<0` / `end<=start`），替换失败不改旧日历 | 422 | `INVALID_BERTH_INTERVAL` |
| 泊位开放区间组内半开交叠，替换失败不改旧日历 | 422 | `OVERLAPPING_BERTH_INTERVAL` |
| 作业分钟非正整数、泊位与作业分钟不成对 | 422 | `VALIDATION_ERROR` |
| 改期确认的行程号未知 / 逐段出发分钟段数不符、含负值、幂等键为空或超长 | 404 `TRIP_NOT_FOUND` / 422 `VALIDATION_ERROR` |
| 改期确认时原行程已作废（含同键重放原行程已被本次改期作废之外的其他作废） | 409 | `TRIP_ALREADY_VOIDED` |
| 改期确认时关联档案船已停用 / 档案版本已变 / 档案被删 | 409 `SHIP_DISABLED` / 409 `SHIP_VERSION_STALE` / 404 `SHIP_NOT_FOUND` |
| 改期确认时目标修订已过期（其后又出现更新修订） | 409 | `REVISION_STALE` |
| 改期确认时原快照在目标修订下仍安全 | 409 | `RESCHEDULE_NOT_NEEDED`（不改变状态） |
| 改期确认时按当前管制/组/日历/占用重算后窗口内无改期解 | 409 | `RESCHEDULE_NO_SOLUTION`（details 带首次违规） |
| 改期确认提交的逐段出发分钟与复算最优建议不一致 | 409 | `PLAN_MISMATCH`（details 回传 `recalculated`） |
| 同一改期幂等键提交不同内容（不同行程号/目标修订/出发分钟） | 409 | `RESCHEDULE_KEY_CONFLICT`（不保存；同键同内容重放 200 `replayed=true`） |

---

## 3. HTTP API

> **本轮用法（不安全航次改期确认）**：新增 1 个写接口
> `POST /api/trips/{trip_id}/reschedule`；既有全部接口路径、请求/响应契约
> 不变（只读复核 `/review` 仅内部函数拆分、行为完全一致）——
> - 凭**已确认行程号、目标潮位修订号、只读复核建议中的逐段出发分钟
>   `departure_minutes` 与非空 `idempotency_key`** 执行；
> - 仅当原行程仍有效、目标修订为最新、原快照在该修订下不安全且窗口内有解时，
>   在同一事务内按原航段顺序、**船舶快照参数**、起始分钟、窗口及泊位作业分钟，
>   依**当前航时管制、狭窄水道组、泊位日历与其他有效占用（排除原行程）**重算
>   最优建议并与提交逐段比对；通过后**作废原行程 + 确认新快照**（旧占用失效、
>   新占用生效），**原账单若存在同事务等额冲销一次**，新行程待另行开单；
> - 返回新旧行程号（`old_trip_id`/`new_trip_id`）、复算行程（`reschedule`）
>   与冲销状态（`reversal_created`/`reversal`、`old_void`）；
> - 拒绝口径：未知行程 `TRIP_NOT_FOUND`、原已作废 `TRIP_ALREADY_VOIDED`、
>   档案停用/版本已变/已删 `SHIP_DISABLED`/`SHIP_VERSION_STALE`/
>   `SHIP_NOT_FOUND`、修订未知或过期 `TIDE_REVISION_NOT_FOUND`/
>   `REVISION_STALE`、原仍安全 `RESCHEDULE_NOT_NEEDED`、无解
>   `RESCHEDULE_NO_SOLUTION`、段数不符或字段非法 `VALIDATION_ERROR`、
>   方案不一致 `PLAN_MISMATCH`，**均不改变状态**；
> - **同键同内容重试返回首次结果**（200 `replayed=true`，即使修订此后又变化
>   也先重放）；同键异内容 `409 RESCHEDULE_KEY_CONFLICT`。原确认快照、批次
>   幂等记录与现有接口契约保持不变。
>
> **本轮用法（终点泊位与开放日历）**：新增 4 个泊位端点，并为单船/批次
> 预排与确认扩展可选泊位字段；不带泊位字段的旧请求与响应**完全兼容**
> （仅新增值为 `null` 的可空字段）——
> - `POST /api/nodes/{node_id}/berths`：为现有节点登记泊位（全局唯一泊位
>   编号 + 可选备注），初始开放日历为空；未知节点 `404 NODE_NOT_FOUND`、
>   重复编号 `409 BERTH_EXISTS`；
> - `PUT /api/berths/{berth_id}/calendar`：以互不重叠的半开分钟区间
>   `[start_minute, end_minute)` 整组替换开放日历（空组 `[]` 全程不开放），
>   返回泊位视图；起点非负、终点大于起点否则
>   `422 INVALID_BERTH_INTERVAL`，组内交叠
>   `422 OVERLAPPING_BERTH_INTERVAL`，未知泊位 `404 BERTH_NOT_FOUND`，
>   **失败不改旧日历**；日历变更只影响之后的预排/确认复算，**不改已确认
>   快照**；
> - `GET /api/berths`（可 `?node=X` 过滤，未知节点 `404`）/
>   `GET /api/berths/{berth_id}`（未知泊位 `404 BERTH_NOT_FOUND`）；
> - `POST /api/trips/plan` / `POST /api/trips/batch/plan`：可携带成对的
>   `berth_id` 与正整数 `work_minutes`；船抵达终点即开工，作业区间
>   `[arrival, arrival+work)` 须完整落在泊位**同一开放区间**、不与有效
>   确认行程的泊位作业占用半开重叠（批次后船另避前船拟作业占用）且不越
>   窗口；响应回传 `berth_id/work_minutes/work_start_minute/
>   work_end_minute`；无解仍为 `404 NO_FEASIBLE_ITINERARY`（批次附
>   `ship_index`）；未知泊位 `404 BERTH_NOT_FOUND`、泊位不属终点
>   `422 BERTH_NOT_AT_DESTINATION`、作业分钟非法/不成对
>   `422 VALIDATION_ERROR`；
> - `POST /api/trips/confirm` / `POST /api/trips/batch/confirm`：原事务内
>   按**当时日历与泊位占用**复算，变化致行程或作业区间不一致
>   `409 PLAN_MISMATCH`（批次全批回滚、同键同内容重放原结果）；成功时
>   快照**固化**泊位与作业区间，后续日历变更不改历史，`GET /api/trips*`
>   回传泊位字段；
> - 作废行程即释放其泊位作业占用（历史快照仍可查）；
>   `POST /api/trips/{id}/review` 的潮位复核仍按原快照；涉及泊位的改期
>   建议须同时找到原泊位可用的即时作业区间（只建议不预留），找不到则
>   `no_solution=true`。
>
> **上一轮用法（通行费费率版本、账单与冲销单）**：新增 6 个端点；旧 API
> 路径与请求/响应完全兼容（作废接口的请求、响应与状态码保持不变，仅在
> 作废已开单行程时于同事务额外落库冲销单）——
> - `PUT /api/segments/{seg_id}/toll-rates`：财务按航段提交整组半开分钟
>   费率区间（起点非负、终点大于起点、费率非负整数分/分钟），整组替换该段
>   旧费率并**继承其他航段**，生成全局不可变新版本（`201`，回传新版本
>   全部航段费率）；空组 `rates: []` 表示该段未覆盖分钟费率 0；未知航段
>   `404 SEGMENT_NOT_FOUND`、非法区间 `422 INVALID_TOLL_RATE_INTERVAL`、
>   组内交叠 `422 OVERLAPPING_TOLL_RATE_INTERVAL`，任一失败整次拒绝；
> - `GET /api/toll/rate-versions` / `GET /api/toll/rate-versions/{v}`：
>   全部版本 / 指定版本（各版本含其全部航段费率；未知版本
>   `404 TOLL_RATE_VERSION_NOT_FOUND`）；
> - `POST /api/trips/{trip_id}/bill`：输入行程号（无请求体），同一事务按
>   **当时最新费率版本**与确认快照各段**进入分钟**选费率、按固化航时计费，
>   输出版本号、逐段金额与整数分总额；首次 `201`，重试返回原账单 `200`
>   `replayed:true`；未知行程 `404 TRIP_NOT_FOUND`、已作废且未开单
>   `409 TRIP_ALREADY_VOIDED`、尚无费率版本
>   `409 TOLL_RATE_VERSION_NOT_FOUND`；旧账单不随新费率改变；
> - `GET /api/trips/{trip_id}/bill` / `GET /api/toll/bills`：账单详情 /
>   全部账单（未开单 `404 BILL_NOT_FOUND`；账单内嵌可空 `reversal`）；
> - `GET /api/trips/{trip_id}/reversal`：等额冲销单（总额与逐段金额为账单
>   相反数、版本沿用账单版本）；未开单 `404 BILL_NOT_FOUND`、已开单未作废
>   `404 REVERSAL_NOT_FOUND`；
> - 作废**已开单**行程时，冲销单在**原作废事务内**生成（不遗漏、不重复），
>   作废请求/响应/状态码不变；开单、费率替换与冲销都不改确认快照、批次
>   幂等结果或规划占用。
>
> **上一轮用法（船舶档案）**：新增档案管理端点，规划/确认/批次可用船号代替
> 三项参数；旧 API 路径与请求/响应结构完全兼容（`PlanResponse`/`TripOut`
> 等仅新增可空字段 `ship_no`/`ship_version`）——
> - `POST /api/ships`：建档（唯一船号 + 非负吃水/水面以上高度/安全余量 +
>   启停状态），初始版本 1；重复船号 `409 SHIP_EXISTS`；
> - `PUT /api/ships/{ship_no}`：变更（未给出的字段沿用当前值）；任一字段
>   实际变化版本 +1（`changed=true`），无实际变化不递增（`changed=false`）；
> - `GET /api/ships` / `GET /api/ships/{ship_no}`：列表 / 当前版本；
>   `GET /api/ships/{ship_no}/versions[/{version}]`：全部 / 指定历史版本；
> - `POST /api/trips/plan`：提交 `ship_no` 代替 `draft/height/margin`，按
>   档案**当前启用版本**规划（停用 `409 SHIP_DISABLED`）；响应回传
>   `ship_no`、`ship_version` 及所用参数；**混用两种输入 422 拒绝**；
> - `POST /api/trips/confirm`：回填 `ship_no` + `ship_version` + 行程，
>   事务内先核对档案仍启用且版本未变（`SHIP_DISABLED` /
>   `SHIP_VERSION_STALE`），再按潮位/管制/占用复算；快照固化所用参数与
>   档案引用，后续档案变更不改写；
> - `POST /api/trips/batch/plan` / `POST /api/trips/batch/confirm`：每艘船
>   可各自选择船号或三项参数；确认时档案失效全批回滚（附 `ship_index`），
>   同键重放沿用旧语义；
> - 复核/改期只用快照参数；作废语义不变。
>
> **上一轮用法（误确认行程作废）**：新增两个端点，旧 API 路径与请求/响应
> 结构完全兼容（`TripOut` 仅新增可空字段 `voided_at`）——
> - `POST /api/trips/{trip_id}/void`：请求体 `{"reason": "非空原因"}`，
>   首次作废 `201`，返回 `{trip_id, reason, voided_at, replayed:false}`；
>   重复作废 `200`、`replayed:true`，返回**首次原因与时间**（不覆盖）；
>   未知行程 `404 TRIP_NOT_FOUND`、空原因 `422 VALIDATION_ERROR`；
> - `GET /api/trips/{trip_id}/void`：查询作废记录；未知行程
>   `404 TRIP_NOT_FOUND`、未作废 `404 VOID_RECORD_NOT_FOUND`；
> - 作废**不删快照**：`GET /api/trips`、`GET /api/trips/{id}` 仍返回原确认
>   内容（已作废者带 `voided_at`）；仅该行程固化航时的航段与狭窄水道组
>   占用从后续单船/批次**预排、确认复算、改期建议**中失效；
> - 作废与确认均在 `BEGIN IMMEDIATE` 事务中串行，并发也不产生相互重叠的
>   有效确认行程；批次行程作废后，**原幂等键重放仍返回首次确认结果**、
>   不重新产生占用；已作废行程的 `review` 潮位复核仍按历史快照执行。
>
> **上一轮用法（狭窄水道组）**：新增三个只读/登记端点，旧 API 路径与请求/响应
> 结构完全不变；组建成即不可变，所有规划/确认/批次/改期自动按当前组关系工作——
> - `POST /api/channel-groups`：提交**航段编号集合**（至少 2 条）登记狭窄水道组，
>   返回 `group_id` 与按提交次序排列的 `members`（含航段起终点/基础航时/水深/净空）；
>   重复编号 `422 DUPLICATE_SEGMENT_IN_GROUP`、未知航段
>   `404 SEGMENT_NOT_FOUND`、成员已归组 `409 SEGMENT_ALREADY_GROUPED`，
>   任一失败均不留下半成品组；
> - `GET /api/channel-groups` / `GET /api/channel-groups/{group_id}`：列出全部组
>   或读取指定组（未知组 `404 CHANNEL_GROUP_NOT_FOUND`）；
> - **独占整组**：船进入组内任一航段即按该段固化航时 `d..d+t-1` 独占整组，
>   `POST /api/trips/plan` 与 `POST /api/trips/batch/plan` 自动避开展开后的组
>   占用（批次后船同时避开前船拟占用的组展开，仍只读不占位）；
> - `POST /api/trips/confirm` / `POST /api/trips/batch/confirm`：在原事务内按
>   **当前组关系**与当时已确认及批内拟占用复算，无解/不一致仍为
>   `409 PLAN_MISMATCH`（批次整批回滚、同键重放返回首次结果）；
> - **组建前**的已确认快照按原航段与固化航时回溯计入新组占用，快照不改写、
>   既有重叠不清理；
> - `POST /api/trips/{id}/review`：原行程潮位安全复核**仍只判断潮位**；需要
>   改期时，建议避开**其他**快照的组占用（排除自身），建议不占位。
>
> **上一轮用法（航段分时航时管制）**：旧 API 路径与请求/响应结构完全不变，
> 新增一个按航段整组替换管制的端点；规划/确认/批次/复核自动按实际航时工作——
> - `PUT /api/segments/{seg_id}/travel-controls`：以航段编号提交一组半开分钟区间
>   `[start_minute, end_minute)` 与正整数 `travel_time`，**整组替换**旧管制并返回
>   生效清单（按起点升序）；`controls: []` 恢复基础航时；`GET
>   /api/segments/{seg_id}/travel-controls` 查询当前清单。未知航段
>   `SEGMENT_NOT_FOUND`、非法区间 `INVALID_CONTROL_INTERVAL`、组内交叠
>   `OVERLAPPING_CONTROL_INTERVAL`，失败均不改旧管制；
> - `POST /api/trips/plan`：船在**进入航段的分钟**按管制选定航时（途中跨边界不变，
>   未命中区间用基础航时），抵达、全程潮位/桥高与航段独占均按实际航时计算
>   （**只读，不占位**）；
> - `POST /api/trips/confirm`：在同一事务内以最新修订 + **当时管制** + 当时占用
>   重新计算；管制变化使提交行程不再最优或窗口无解，沿用 `409 PLAN_MISMATCH`
>   不保存；
> - `POST /api/trips/batch/plan` / `POST /api/trips/batch/confirm`：逐艘按实际航时
>   串行规划/事务复算（后船避前船拟占用），`PLAN_MISMATCH` 全批不保存，同键重放
>   仍返回首次结果；
> - `POST /api/trips/{id}/review`：原航路潮位复核仍按**快照原占用分钟**（占用时长
>   不随管制变化）；需要改期时，以**当前管制**求各段航时与出发分钟（建议不占位、
>   快照不改写）；
> - 启动方式不变（`docker compose up` / `docker run` / `uvicorn app.main:app`）。
>
> **上一轮用法（同修订批次）**：`POST /api/trips/batch/plan` 按优先顺序预排 2～5 艘船
>   （后船避开已确认占用 + 本批前船拟占用，只读不占位，首个失败序号见
>   `details.ship_index`）；`POST /api/trips/batch/confirm` 在单事务内核订最新修订、
>   逐艘复算逐段比对，`REVISION_STALE` / `PLAN_MISMATCH` 全批不保存，成功凭
>   `idempotency_key` 一次写入全部快照，同键同内容重放原结果、同键异内容
>   `BATCH_KEY_CONFLICT`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/nodes` | 录入节点 |
| GET | `/api/nodes` | 节点列表 |
| POST | `/api/nodes/{node_id}/berths` | **本轮新增**：为现有节点登记泊位（全局唯一编号，初始日历为空）；未知节点 `NODE_NOT_FOUND`、重复 `BERTH_EXISTS` |
| GET | `/api/berths` | **本轮新增**：泊位列表（可 `?node=X` 过滤；各附当前开放日历） |
| GET | `/api/berths/{berth_id}` | **本轮新增**：泊位详情（未知泊位 `BERTH_NOT_FOUND`） |
| PUT | `/api/berths/{berth_id}/calendar` | **本轮新增**：以半开分钟区间整组替换泊位开放日历（空组全程不开放）；非法/交叠区间 422 且旧日历不变；未知泊位 `BERTH_NOT_FOUND` |
| POST | `/api/segments` | 录入单向航段 |
| GET | `/api/segments` | 航段列表（按编号排序） |
| PUT | `/api/segments/{seg_id}/travel-controls` | 以航段编号整组替换分时航时管制（半开区间 + 通行分钟，空组恢复基础航时），返回生效清单 |
| GET | `/api/segments/{seg_id}/travel-controls` | 查询航段当前生效的分时管制清单（含基础航时） |
| PUT | `/api/segments/{seg_id}/toll-rates` | **本轮新增**：财务按航段整组提交半开分钟费率区间（非负整数分/分钟），整组替换该段并继承其他航段，生成全局不可变新版本（201，回传新版本全部航段费率）；空组表示该段未覆盖分钟费率为 0 |
| GET | `/api/toll/rate-versions` | **本轮新增**：全部通行费费率版本（各含其全部航段费率区间） |
| GET | `/api/toll/rate-versions/{version}` | **本轮新增**：指定费率版本（不可变；未知版本 `TOLL_RATE_VERSION_NOT_FOUND`） |
| POST | `/api/channel-groups` | **本轮新增**：提交航段编号集合（≥2）登记狭窄水道组，返回组号及按提交次序的成员；建成不可变、不可重复归组 |
| GET | `/api/channel-groups` | **本轮新增**：狭窄水道组列表（按组号升序） |
| GET | `/api/channel-groups/{group_id}` | **本轮新增**：狭窄水道组详情（组号、备注、成员航段信息） |
| POST | `/api/ships` | **本轮新增**：建立船舶档案（唯一船号 + 三项参数 + 启停状态），初始版本 1 |
| GET | `/api/ships` | **本轮新增**：船舶档案列表（各船号当前版本） |
| GET | `/api/ships/{ship_no}` | **本轮新增**：指定船号当前版本 |
| PUT | `/api/ships/{ship_no}` | **本轮新增**：变更档案（有效变更递增版本，无实际变化不递增） |
| GET | `/api/ships/{ship_no}/versions` | **本轮新增**：全部历史版本（按版本号升序） |
| GET | `/api/ships/{ship_no}/versions/{version}` | **本轮新增**：指定历史版本 |
| GET | `/api/tide/revisions` | 潮位修订列表 |
| GET | `/api/tide/revisions/{rev}?from_minute=&to_minute=` | 取某修订逐分钟名义潮位 `levels` 与预测误差 `errors` |
| POST | `/api/tide/corrections` | 名义潮位更正（继承误差），生成新修订 |
| POST | `/api/tide/error-revisions` | 预测误差修订（名义潮位不变），生成新修订 |
| POST | `/api/trips/plan` | 规划最早抵达行程；**本轮扩展**：可携 `berth_id`+`work_minutes` 指定终点泊位（抵达即开工，作业区间完整落在同一开放区间且不与有效泊位占用重叠） |
| POST | `/api/trips/confirm` | 携带修订号确认行程（事务复核）；指定泊位时按当时日历/占用复算并固化泊位作业区间 |
| POST | `/api/trips/batch/plan` | **本轮扩展泊位**：同修订 2～5 艘船按优先顺序批次预排（只读不占位，后船避前船航段与泊位拟占用） |
| POST | `/api/trips/batch/confirm` | 携带修订号 + 幂等键批次确认（事务逐艘复算，全成全败、幂等重放；泊位作业区间一并复算固化） |
| POST | `/api/trips/{trip_id}/review` | 更正后按目标修订复核原行程，不安全时求改期；**本轮扩展**：快照指定泊位时改期建议须同时给出原泊位可用即时作业区间（只建议不预留），否则 `no_solution` |
| POST | `/api/trips/{trip_id}/void` | 凭非空原因作废误确认行程（重复作废 200 返回首次记录）；作废已开单行程时同事务生成等额冲销单（接口请求/响应/状态码不变） |
| GET | `/api/trips/{trip_id}/void` | 按行程号查询作废记录（未作废 `VOID_RECORD_NOT_FOUND`） |
| POST | `/api/trips/{trip_id}/bill` | **本轮新增**：为已确认行程开具通行费账单（按当时最新版本 + 快照进入分钟固化；重试 200 返回原账单；已作废未开单 `TRIP_ALREADY_VOIDED`） |
| GET | `/api/trips/{trip_id}/bill` | **本轮新增**：查询账单（含冲销单；未开单 `BILL_NOT_FOUND`） |
| GET | `/api/trips/{trip_id}/reversal` | **本轮新增**：查询等额冲销单（未开单 `BILL_NOT_FOUND`、未作废 `REVERSAL_NOT_FOUND`） |
| GET | `/api/toll/bills` | **本轮新增**：全部通行费账单（按行程号，各附冲销单若已生成） |
| GET | `/api/trips` | 已确认行程列表（含已作废历史快照，`voided_at` 标识是否已作废） |
| GET | `/api/trips/{trip_id}` | 已确认行程快照详情 |
| GET | `/health` | 健康检查 |

### 预测误差修订请求 / 响应示例

名义潮位不变，只登记逐分钟预测误差（米，非负）；未列出分钟继承基础修订误差，
旧修订视为误差 0：

```json
POST /api/tide/error-revisions
{
  "errors": {"100": 2.0, "101": 0.5},
  "base_revision": 1,
  "note": "人工修订：100/101 分钟预测误差"
}
```

- `errors`：`{非负整数分钟: 非负误差(米)}`，不能为空；分钟或误差为负返回
  `422 VALIDATION_ERROR`；
- `base_revision` 可省略，省略时基于**最新**修订；基础修订不存在返回
  `404 TIDE_REVISION_NOT_FOUND`；
- 提交的任一分钟在基础修订中没有名义潮位，返回 `422 MISSING_TIDE_DATA`，不产生
  新修订；
- 成功返回 `201` 与新修订号（与名义更正共用同一自增序列）：

```json
{ "revision": 3, "note": "人工修订：100/101 分钟预测误差", "created_at": "2026-10-01 08:00:00" }
```

随后 `GET /api/tide/revisions/3?from_minute=99&to_minute=102` 可同时读到
`levels`（名义潮位，与基础修订一致）与 `errors`（100=2.0、101=0.5、其余继承为 0）。

规划/确认/复核携带该修订号时，水深按 `名义−误差`、净空按 `名义+误差` 的保守极值
核验；确认冲突（`REVISION_STALE`）、择优（最早抵达 + 航段序列字典序）、响应字段
均与既有规则一致。

### 航段分时航时管制请求 / 响应示例（本轮新增）

以航段编号整组提交一组**半开分钟区间 `[start_minute, end_minute)` 与正整数
通行分钟**：

```json
PUT /api/segments/2/travel-controls
{
  "controls": [
    {"start_minute": 0,   "end_minute": 360, "travel_time": 60},
    {"start_minute": 360, "end_minute": 720, "travel_time": 45}
  ]
}
```

- 区间为半开：于 `start_minute` 进入命中、于 `end_minute` 进入已不命中；
  首尾相接（前一区间 `end` == 后一区间 `start`）**不算重叠**。
- 整组替换该航段旧管制，返回 `200` 与生效清单（按起点升序，含基础航时）：

```json
{
  "seg_id": 2,
  "base_travel_time": 40,
  "controls": [
    {"start_minute": 0,   "end_minute": 360, "travel_time": 60},
    {"start_minute": 360, "end_minute": 720, "travel_time": 45}
  ]
}
```

- 空组恢复航段基础航时：

```bash
curl -s -X PUT $BASE/api/segments/2/travel-controls \
  -H 'Content-Type: application/json' -d '{"controls":[]}'
# {"seg_id":2,"base_travel_time":40,"controls":[]}
```

- 查询当前清单：`GET /api/segments/2/travel-controls`。
- 错误（均不改动旧管制）：
  - 航段不存在 -> `404 SEGMENT_NOT_FOUND`；
  - `start_minute<0`、`end_minute<=start_minute` 或 `travel_time<=0` ->
    `422 INVALID_CONTROL_INTERVAL`（`details.index` 指出首个非法区间）；
  - 组内区间分钟交叠 -> `422 OVERLAPPING_CONTROL_INTERVAL`
    （`details.previous/current` 给出冲突区间）。

随后任意规划请求字段不变，但响应中每段的 `travel_time` 与 `start/end_minute`
均按**进入分钟当时管制**给出。例如演示库航段 2（B→C，基础 40 分钟）在
`[0,360)` 管制为 60 分钟时，于第 30 分钟进入 B→C 将占 30..89、第 90 分钟抵达
C（即使第 360 分钟已跨越管制边界，航时仍为 60）；第 360 分钟及以后进入则
沿用 `[360,720)` 的 45 分钟或区间外的基础 40 分钟。

### 狭窄水道组请求 / 响应示例（本轮新增）

提交**至少两条**现有航段的编号集合即可登记为同一狭窄水道组；成员按**提交
次序**返回（集合中的重复编号会被拒绝）：

```json
POST /api/channel-groups
{
  "segments": [1, 2],
  "note": "北岸狭水道：A->B 与 B->C 单向交替通行"
}
```

成功返回 `201`：

```json
{
  "group_id": 1,
  "note": "北岸狭水道：A->B 与 B->C 单向交替通行",
  "created_at": "2026-10-02 15:24:27",
  "members": [
    {"position": 0, "seg_id": 1, "origin": "A", "destination": "B",
     "travel_time": 30, "depth": 6.0, "clearance": null},
    {"position": 1, "seg_id": 2, "origin": "B", "destination": "C",
     "travel_time": 40, "depth": 4.0, "clearance": 8.0}
  ]
}
```

- 组**建成即不可变**：没有更新/解散端点，航段也不能再加入其他组
  （再次提交含 `1` 或 `2` 的集合 → `409 SEGMENT_ALREADY_GROUPED`，
  `details.conflicts` 给出 `[{"seg_id":1,"group_id":1}]`）；
- `segments` 少于 2 条（含空）→ `422 VALIDATION_ERROR`；
  同一编号重复出现（如 `[1,2,1]`）→ `422 DUPLICATE_SEGMENT_IN_GROUP`；
  含未知航段（如 `[1,404]`）→ `404 SEGMENT_NOT_FOUND`
  （`details.unknown_seg_ids=[404]`）；
- 任一失败在单事务内回滚，**不产生半成品组**（`GET /api/channel-groups`
  仍只列出此前已建成的组）。

**独占整组的规划效果**：组 `{1,2}` 建成后，一艘已确认船于第 0 分钟进入
航段 1（航时 30，占 `0..29`、第 30 分钟抵达 B），则航段 2 的 `0..29` 分钟
同步封闭；此时从 B 出发经航段 2 去 C 的规划最早只能于**第 30 分钟**进入
航段 2（先船的**抵达分钟即可进入**），而组外的航段 3（C→D）不受影响、
第 0 分钟仍可进入。组建前已确认、经过组内任一航段的快照也按其固化航时
同样展开（快照本身不改写）。

### 误确认行程作废请求 / 响应示例（本轮新增）

已确认行程（单船或批次确认均可）发现误确认后，凭**行程号 + 非空原因**作废：

```json
POST /api/trips/7/void
{ "reason": "调度员误确认：该船实际未离泊，撤销本次占用" }
```

首次作废返回 `201`：

```json
{
  "trip_id": 7,
  "reason": "调度员误确认：该船实际未离泊，撤销本次占用",
  "voided_at": "2026-10-02 16:05:31",
  "replayed": false
}
```

- **重复作废**：同一行程再次调用（即使原因不同）返回 `200` 与**首次记录**，
  原因与作废时间都不覆盖：

```json
POST /api/trips/7/void
{ "reason": "另一个原因" }
// HTTP 200
{ "trip_id": 7, "reason": "调度员误确认：该船实际未离泊，撤销本次占用",
  "voided_at": "2026-10-02 16:05:31", "replayed": true }
```

- **查询作废记录**：`GET /api/trips/7/void` 返回
  `{"trip_id":7,"reason":"…","voided_at":"…"}`（无 `replayed` 字段）；
  未知行程 `404 TRIP_NOT_FOUND`；行程存在但未作废
  `404 VOID_RECORD_NOT_FOUND`。
- **历史快照仍在**：`GET /api/trips/7` 与 `GET /api/trips` 仍返回原确认
  内容，只是 `voided_at` 由 `null` 变为作废时间；作废后该行程固化航时的
  占用立即对后续规划/确认/改期失效（狭窄水道组的整组展开同步失效）。
- 作废原因为空串或纯空白 -> `422 VALIDATION_ERROR`（首尾空白会被剥离，
  `"  原因  "` 以 `原因` 入库）。

### 通行费费率版本 / 账单 / 冲销单请求与响应示例（本轮新增）

**① 财务提交费率、生成全局不可变版本**：按航段编号整组提交半开分钟区间，
费率为非负整数分/分钟；新版本继承其他航段，仅整组替换被提交航段：

```json
PUT /api/segments/2/toll-rates
{
  "rates": [
    {"start_minute": 0,   "end_minute": 360,  "rate_cents_per_minute": 5},
    {"start_minute": 360, "end_minute": 1080, "rate_cents_per_minute": 8}
  ],
  "note": "B->C 日间费率调整"
}
```

```json
// 201 —— 返回新版本（此处为 #2）及其全部航段费率（seg1 继承自 #1，seg2 本次替换）
{
  "version": 2,
  "changed_seg_id": 2,
  "note": "B->C 日间费率调整",
  "created_at": "2026-10-03 10:00:00",
  "rates": [
    {"seg_id": 1, "start_minute": 0,    "end_minute": 360,  "rate_cents_per_minute": 2},
    {"seg_id": 1, "start_minute": 360,  "end_minute": 720,  "rate_cents_per_minute": 1},
    {"seg_id": 1, "start_minute": 720,  "end_minute": 1440, "rate_cents_per_minute": 2},
    {"seg_id": 2, "start_minute": 0,    "end_minute": 360,  "rate_cents_per_minute": 5},
    {"seg_id": 2, "start_minute": 360,  "end_minute": 1080, "rate_cents_per_minute": 8}
  ]
}
```

- 非法区间（`start<0`、`end<=start`、费率为负）->
  `422 INVALID_TOLL_RATE_INTERVAL`；组内交叠 ->
  `422 OVERLAPPING_TOLL_RATE_INTERVAL`；未知航段 ->
  `404 SEGMENT_NOT_FOUND`。任一失败**整次拒绝、不生成新版本**（版本号不跳号）。
- 空组 `{"rates": []}` 合法：该航段在此新版本中未覆盖分钟费率为 0。
- 查询：`GET /api/toll/rate-versions`（全量）、
  `GET /api/toll/rate-versions/2`（指定版本）。

**② 开单**：输入只有行程号，事务内按当时最新版本与确认快照各段进入分钟
固化（段金额 = 固化航时 × 进入分钟费率；未覆盖分钟费率 0）：

```
POST /api/trips/7/bill        # 无请求体
```

```json
// 201 首次开单（重试同一行程返回 200 且 replayed=true、内容为原账单）
{
  "trip_id": 7,
  "version": 2,
  "total_cents": 360,
  "segments": [
    {"leg_index": 0, "seg_id": 1, "origin": "A", "destination": "B",
     "entry_minute": 0,  "travel_time": 30,
     "rate_cents_per_minute": 2, "amount_cents": 60},
    {"leg_index": 1, "seg_id": 2, "origin": "B", "destination": "C",
     "entry_minute": 30, "travel_time": 40,
     "rate_cents_per_minute": 5, "amount_cents": 200},
    {"leg_index": 2, "seg_id": 3, "origin": "C", "destination": "D",
     "entry_minute": 70, "travel_time": 20,
     "rate_cents_per_minute": 0, "amount_cents": 0}
  ],
  "billed_at": "2026-10-03 10:01:00",
  "reversal": null,
  "replayed": false
}
```

- 第 2 段在分钟 30 进入，命中 `[0,360)` 的费率 5；途中跨过 360 边界**不重选**。
- 第 3 段在版本 #2 无费率区间 -> 费率 0、金额 0。
- 之后费率再出新版，`POST /api/trips/7/bill` 重试与 `GET` 查询都仍是
  version 2、360 分（**旧账单不随费率改变**）。
- 未知行程 `404 TRIP_NOT_FOUND`；已作废且未开单
  `409 TRIP_ALREADY_VOIDED`；尚无费率版本
  `409 TOLL_RATE_VERSION_NOT_FOUND`。

**③ 作废已开单行程 -> 同事务等额冲销**：作废接口的请求/响应/状态码不变；
冲销单另走查询接口：

```json
POST /api/trips/7/void
{ "reason": "调度员误确认：撤销" }
// 201（与以往完全一致）
{ "trip_id": 7, "reason": "调度员误确认：撤销",
  "voided_at": "2026-10-03 10:05:00", "replayed": false }
```

```json
GET /api/trips/7/reversal
// 200 —— 等额冲销：逐段金额与总额取账单相反数，版本沿用账单版本
{
  "trip_id": 7,
  "version": 2,
  "total_cents": -360,
  "segments": [
    {"leg_index": 0, "seg_id": 1, "origin": "A", "destination": "B",
     "entry_minute": 0,  "travel_time": 30,
     "rate_cents_per_minute": 2, "amount_cents": -60},
    {"leg_index": 1, "seg_id": 2, "origin": "B", "destination": "C",
     "entry_minute": 30, "travel_time": 40,
     "rate_cents_per_minute": 5, "amount_cents": -200},
    {"leg_index": 2, "seg_id": 3, "origin": "C", "destination": "D",
     "entry_minute": 70, "travel_time": 20,
     "rate_cents_per_minute": 0, "amount_cents": 0}
  ],
  "reversed_at": "2026-10-03 10:05:00"
}
```

- 账单仍可查（`GET /api/trips/7/bill`、`GET /api/toll/bills`），其
  `reversal` 字段内嵌上述冲销单；未开单行程作废不产生冲销（冲销查询
  `404 BILL_NOT_FOUND`）；已开单未作废查冲销 `404 REVERSAL_NOT_FOUND`。
- 重复作废仍返回首次作废记录（200 `replayed:true`），**不重复冲销**。

### 船舶档案与船号规划请求 / 响应示例（本轮新增）

**建档**（唯一船号 + 非负吃水/水面以上高度/安全余量 + 启停状态，初始版本 1）：

```json
POST /api/ships
{ "ship_no": "EVERGREEN", "draft": 6.5, "height": 2.0, "margin": 0.5,
  "enabled": true, "note": "巴拿马籍集装箱船" }
```

成功返回 `201` 与当前版本：

```json
{ "ship_no": "EVERGREEN", "version": 1, "draft": 6.5, "height": 2.0,
  "margin": 0.5, "enabled": true, "note": "巴拿马籍集装箱船",
  "created_at": "2026-10-03 08:00:00" }
```

- 重复船号 -> `409 SHIP_EXISTS`；负参数/空船号 -> `422 VALIDATION_ERROR`；
  失败不留半成品。

**变更**（未给出的字段沿用当前值；任一字段实际变化才递增版本）：

```json
PUT /api/ships/EVERGREEN
{ "draft": 7.0 }
// 200 { "ship_no": "EVERGREEN", "version": 2, "draft": 7.0, "height": 2.0,
//        "margin": 0.5, "enabled": true, ..., "changed": true }

PUT /api/ships/EVERGREEN
{ "draft": 7.0 }
// 200 { ..., "version": 2, "changed": false }   <- 无实际变化，不递增版本
```

**版本查询**：`GET /api/ships/EVERGREEN`（当前）、
`GET /api/ships/EVERGREEN/versions`（全部历史，升序）、
`GET /api/ships/EVERGREEN/versions/1`（指定版本；未知版本
`404 SHIP_VERSION_NOT_FOUND`）。

**船号预排**：用 `ship_no` 代替三项参数（二者互斥，混用 `422`）：

```json
POST /api/trips/plan
{ "origin": "A", "destination": "D", "start_minute": 0, "window_minutes": 1440,
  "ship_no": "EVERGREEN", "revision": 1 }
```

响应回传**船号、版本及所用参数**（取自档案当前启用版本，其余字段与直接
提交参数的规划响应完全一致）：

```json
{ "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 7.0, "height": 2.0, "margin": 0.5,
  "ship_no": "EVERGREEN", "ship_version": 2,
  "arrival_minute": 144, "total_wait_minutes": 54,
  "itinerary": [ {"seg_id": 1, "origin": "A", "destination": "B",
                  "start_minute": 54, "end_minute": 84, "travel_time": 30},
                 {"seg_id": 2, "origin": "B", "destination": "C",
                  "start_minute": 84, "end_minute": 124, "travel_time": 40},
                 {"seg_id": 3, "origin": "C", "destination": "D",
                  "start_minute": 124, "end_minute": 144, "travel_time": 20} ] }
```

**船号确认**：回填规划所得 `ship_no`、`ship_version` 与 `itinerary`
（不要回填三项参数——船号与参数混用会被拒绝）：

```json
POST /api/trips/confirm
{ "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "ship_no": "EVERGREEN", "ship_version": 2,
  "itinerary": [ /* 预排响应 itinerary 原样回填 */ ] }
```

确认事务内**先核对档案仍启用且版本未变**，再核订潮位修订并按当时管制与
占用复算：档案停用 -> `409 SHIP_DISABLED`；档案已变更到新版本 ->
`409 SHIP_VERSION_STALE`（`details` 给出 `provided_version/current_version`），
均不保存。成功 `201` 的快照**固化所用参数与档案引用**：

```json
{ "trip_id": 7, "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 7.0, "height": 2.0, "margin": 0.5,
  "ship_no": "EVERGREEN", "ship_version": 2,
  "arrival_minute": 144, "itinerary": [ /* … */ ],
  "confirmed_at": "2026-10-03 08:05:00", "voided_at": null }
```

此后档案再变更（含停用）都不改写该快照；`POST /api/trips/7/review` 的潮位
复核与改期建议只用快照中的 `draft/height/margin`。批次预排/确认中每艘船可
各自选择船号或三项参数；确认时任一船档案失效（附 `details.ship_index`）
**全批回滚**，同键重放仍返回首次结果。

### 泊位登记 / 开放日历 / 泊位预排请求与响应示例（本轮新增）

**1）为现有节点登记泊位**（初始日历为空，全程不开放）：

```json
POST /api/nodes/D/berths
{ "berth_id": "D2", "note": "内港 2 号泊位" }
```

```json
201
{ "berth_id": "D2", "node_id": "D", "note": "内港 2 号泊位",
  "created_at": "2026-10-04 01:00:00", "calendar": [] }
```

**2）整组替换开放日历**（互不交叠的半开分钟区间；空组 `[]` 全程不开放）：

```json
PUT /api/berths/D2/calendar
{ "intervals": [
    {"start_minute": 0,    "end_minute": 360},
    {"start_minute": 360,  "end_minute": 1440}
] }
```

返回替换后的泊位视图（`calendar` 按起点升序）。非法区间
`422 INVALID_BERTH_INTERVAL`、组内交叠
`422 OVERLAPPING_BERTH_INTERVAL` 时旧日历不变；未知泊位
`404 BERTH_NOT_FOUND`。

**3）指定终点泊位 + 作业分钟预排**（`berth_id` 与 `work_minutes` 成对；
船抵达即开工）：

```json
POST /api/trips/plan
{
  "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 3.0, "height": 0.0, "margin": 0.0,
  "berth_id": "D2", "work_minutes": 30
}
```

响应在常规行程基础上回传泊位与作业起止分钟（下例 90 抵达，作业
`[90,120)` 完整落在 `[0,360)` 开放区间）：

```json
{
  "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 3.0, "height": 0.0, "margin": 0.0,
  "ship_no": null, "ship_version": null,
  "berth_id": "D2", "work_minutes": 30,
  "work_start_minute": 90, "work_end_minute": 120,
  "arrival_minute": 90, "total_wait_minutes": 0,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

若开放日历 / 既有泊位作业占用使窗口内任何抵达都无法完整容纳作业区间，
返回 `404 NO_FEASIBLE_ITINERARY`；泊位不属终点
`422 BERTH_NOT_AT_DESTINATION`、未知泊位 `404 BERTH_NOT_FOUND`、作业
分钟非正整数或与泊位不成对 `422 VALIDATION_ERROR`。确认时把该响应
**原样回填**即可（快照固化 `work_start_minute/work_end_minute`）；
`GET /api/trips*` 的泊位字段与本响应一致，作废后增加 `voided_at`。

批次中各船可独立携带 `berth_id/work_minutes`；后船自动避开前船的拟作业
区间，任一船冲突按 `details.ship_index` 返回，详见下节。

### 规划请求 / 响应示例

请求：

```json
POST /api/trips/plan
{
  "origin": "A",
  "destination": "D",
  "start_minute": 0,
  "window_minutes": 1440,
  "draft": 5.0,
  "height": 2.0,
  "margin": 0.5,
  "revision": 1
}
```

响应（每段给出 `seg_id` 与起止分钟；`end_minute` 为抵达下一节点的分钟）：

```json
{
  "revision": 1,
  "origin": "A",
  "destination": "D",
  "start_minute": 0,
  "window_minutes": 1440,
  "draft": 5.0,
  "height": 2.0,
  "margin": 0.5,
  "arrival_minute": 90,
  "total_wait_minutes": 0,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

### 确认请求 / 响应示例

将规划响应**原样回填**（含 `revision` 与每段 `seg_id/起止分钟`）即可：

```json
POST /api/trips/confirm
{
  "revision": 1, "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 1440,
  "draft": 5.0, "height": 2.0, "margin": 0.5,
  "itinerary": [
    {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,  "end_minute": 30, "travel_time": 30},
    {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 30, "end_minute": 70, "travel_time": 40},
    {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 70, "end_minute": 90, "travel_time": 20}
  ]
}
```

成功返回 `201`，响应为含 `trip_id / revision / arrival_minute / itinerary / confirmed_at`
的不可变快照；过期返回 `409 REVISION_STALE`。

### 批次预排 / 确认请求与响应示例（本轮新增）

同一潮位修订下，把按**优先顺序**排列的 2～5 艘船一次预排。每艘船的字段与单船
`/api/trips/plan` 完全一致；`revision` 提升到批次级（可省略，省略时取最新修订，
响应回带实际所据修订号）：

```json
POST /api/trips/batch/plan
{
  "revision": 1,
  "ships": [
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5},
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5}
  ]
}
```

响应为 `revision` + `ships`；每艘船带 `index`（0 起，即优先顺序）与单船规划同形的
行程结果。船 1 的结果会避开船 0 的**拟占用**（演示库中船 0 于第 90 分钟抵达，
船 1 整体后移，第 130 分钟抵达）：

```json
{
  "revision": 1,
  "ships": [
    {"index":0,"arrival_minute":90,"itinerary":[ /* seg1: 0..30, seg2: 30..70, seg3: 70..90 */ ]},
    {"index":1,"arrival_minute":130,"itinerary":[ /* seg1: 40..70, seg2: 70..110, seg3: 110..130 */ ]}
  ]
}
```

- 船数不在 2～5 或任一船字段非法（窗口 >1440、负吃水等）-> `422 VALIDATION_ERROR`；
- 任一船未知节点 / 窗口缺潮位 / 无可行行程 -> 沿用 `NODE_NOT_FOUND` /
  `MISSING_TIDE_DATA` / `NO_FEASIBLE_ITINERARY`，`details.ship_index` 给出首个
  失败序号（0 起）；
- 预排**不占位**，重复调用结果一致（前提是占用/修订未变）。

确认时回填各船预排行程，并额外提供**必填** `revision` 与批次**幂等键**
`idempotency_key`：

```json
POST /api/trips/batch/confirm
{
  "revision": 1,
  "idempotency_key": "tide-window-2026-10-02-am-batch",
  "ships": [
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
     "draft":5.0,"height":2.0,"margin":0.5,
     "itinerary":[ /* 预排 ships[0].itinerary 原样回填 */ ]},
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
     "draft":5.0,"height":2.0,"margin":0.5,
     "itinerary":[ /* 预排 ships[1].itinerary 原样回填 */ ]}
  ]
}
```

首次成功返回 `201`，一次写入整批不可变快照：

```json
{
  "idempotency_key": "tide-window-2026-10-02-am-batch",
  "revision": 1,
  "replayed": false,
  "trips": [
    {"index":0,"trip_id":7,"revision":1,"arrival_minute":90, "itinerary":[ /* … */ ]},
    {"index":1,"trip_id":8,"revision":1,"arrival_minute":130,"itinerary":[ /* … */ ]}
  ]
}
```

- **同键同内容重试**：返回 `200`、`replayed=true` 与**首次相同的各行程号**，
  服务不再复算、不新增快照——即使这期间潮位已生成新修订或占用已变化；
- **同键异内容**（改任一船参数/行程，或换修订号）：`409 BATCH_KEY_CONFLICT`，
  不保存；请换新幂等键；
- **修订过期**：`409 REVISION_STALE`（`provided_revision/current_revision`）；
- **复算无解或逐段不一致**：`409 PLAN_MISMATCH`，`details.ship_index` 指出首个
  失败船、`reason` 为 `no_feasible_itinerary` 或 `itinerary_mismatch`，**全批回滚**；
- 批次写入的每条快照都出现在 `GET /api/trips` / `GET /api/trips/{id}` 中，占用
  立即对后续单船/批次规划生效；旧单船 `/api/trips/plan`、`/api/trips/confirm`
  接口路径、字段与冲突语义均保持不变。

### 更正后复核 / 改期请求与响应示例

潮位发生更正后，凭**已确认行程号**与**目标潮位修订号**复核（`target_revision`
可省略，省略时取最新修订；请求体也可整体省略）：

```json
POST /api/trips/1/review
{ "target_revision": 2 }
```

**原行程安全**：`safe=true`，`first_violation/reschedule` 均为 `null`。

**原行程不安全且窗口内可改期**（下例为演示库中 `A→D`、吃水 6.5/船高 2.0/余量 0.5、
窗口 720 的已确认行程，第 100 分钟被更正为低潮 0.2m）：

```json
{
  "trip_id": 1,
  "snapshot_revision": 1,
  "target_revision": 2,
  "origin": "A", "destination": "D",
  "start_minute": 0, "window_minutes": 720,
  "draft": 6.5, "height": 2.0, "margin": 0.5,
  "original_arrival_minute": 144,
  "safe": false,
  "first_violation": {
    "leg_index": 1,
    "seg_id": 2,
    "origin": "B", "destination": "C",
    "minute": 100,
    "reasons": ["depth"],
    "tide_level": 0.2,
    "tide_error": 0.0,
    "depth": 4.0,
    "clearance": 8.0,
    "required_depth": 7.0,
    "required_clearance": 2.5,
    "available_depth": 4.2,
    "available_clearance": 7.8
  },
  "reschedule": {
    "revision": 2,
    "arrival_minute": 161,
    "total_wait_minutes": 71,
    "changed_leg_count": 2,
    "changed_legs": [
      {"leg_index": 1, "seg_id": 2, "original_departure_minute": 84,  "new_departure_minute": 101},
      {"leg_index": 2, "seg_id": 3, "original_departure_minute": 124, "new_departure_minute": 141}
    ],
    "itinerary": [
      {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 54,  "end_minute": 84,  "travel_time": 30},
      {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 101, "end_minute": 141, "travel_time": 40},
      {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 141, "end_minute": 161, "travel_time": 20}
    ]
  },
  "no_solution": false,
  "original_itinerary": [ /* 快照原行程，逐段原样返回 */ ]
}
```

要点：

- 第一段出发分钟仍是快照中的 `54`（**变动航段数最少**优先）；第二、三段因需避开
  低潮而整体后移，`changed_leg_count=2`，改期抵达 161。
- `first_violation` 按快照航段顺序、占用分钟递增给出**首次**违规；`reasons` 可能为
  `["depth"]`、`["clearance"]` 或二者同时出现。`tide_level`/`tide_error` 为该分钟
  目标修订的名义潮位与预测误差；`available_depth = depth + tide_level − tide_error`、
  `available_clearance = clearance − tide_level − tide_error`（无桥为 `null`），
  即按对应保守极值计算的实际可用值。
- **窗口内无改期解**：`reschedule=null` 且 `no_solution=true`，`first_violation`
  仍给出原行程风险；快照保持不变（`GET /api/trips/{trip_id}` 读到的仍是原内容）。
- **涉及泊位的快照**：顶层回传快照的 `berth_id/work_minutes/
  work_start_minute/work_end_minute`；原航路潮位安全判断仍完全按原快照
  （与开放日历变化无关）。需要改期时，`reschedule` 必须同时给出**原泊位**
  按当前日历与其他有效快照占用求得的即时作业区间
  （`berth_id="D2"`、`work_start_minute=新抵达`、
  `work_end_minute=新抵达+work_minutes`，只建议不预留）；航段能改期但
  原泊位窗口内无处作业时同样 `no_solution=true`。
- 该接口为**只读**：不新增、不修改任何确认行程，也不产生新修订。

### 不安全航次改期确认请求 / 响应示例（本轮新增）

先按上节调用只读复核 `POST /api/trips/{id}/review` 取得建议；原行程不安全且
`reschedule` 非空时，凭**原行程号、目标修订号、建议中的逐段出发分钟**与
**非空幂等键**执行改期（路径中的行程号亦参与内容指纹；幂等键 1～128 字符）：

```json
POST /api/trips/1/reschedule
{
  "target_revision": 2,
  "departure_minutes": [54, 101, 141],
  "idempotency_key": "tide-correction-2-resched-trip-1"
}
```

- `departure_minutes` 必须按原航段顺序给出**每段的出发绝对分钟**（个数与原
  快照航段数一致，零航段行程恒安全不会进入本接口）；通常直接取
  `review.reschedule.itinerary[*].start_minute` 原样回填。
- 事务内核验原行程仍有效、目标修订仍最新、原快照确实不安全，并按**当前航时
  管制、狭窄水道组、泊位日历与其他有效占用（排除原行程）**重算最优建议后与
  提交逐段比对。

首次成功返回 `201`：原行程作废（`old_void`）、新行程确认（`new_trip_id`）、
原账单若已存在则同事务等额冲销（`reversal_created=true` 并内嵌冲销单）：

```json
{
  "idempotency_key": "tide-correction-2-resched-trip-1",
  "old_trip_id": 1,
  "new_trip_id": 9,
  "snapshot_revision": 1,
  "target_revision": 2,
  "replayed": false,
  "reschedule": {
    "revision": 2,
    "arrival_minute": 161,
    "total_wait_minutes": 71,
    "changed_leg_count": 2,
    "changed_legs": [ /* 与只读复核 reschedule 同形 */ ],
    "itinerary": [
      {"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 54,  "end_minute": 84,  "travel_time": 30},
      {"seg_id": 2, "origin": "B", "destination": "C", "start_minute": 101, "end_minute": 141, "travel_time": 40},
      {"seg_id": 3, "origin": "C", "destination": "D", "start_minute": 141, "end_minute": 161, "travel_time": 20}
    ],
    "berth_id": "D1", "work_minutes": 30,
    "work_start_minute": 161, "work_end_minute": 191
  },
  "old_void": {
    "trip_id": 1,
    "reason": "改期至新行程 #9（目标潮位修订 #2），原行程 #1 由调度改期确认自动作废",
    "voided_at": "2026-10-04 03:10:00"
  },
  "reversal_created": true,
  "reversal": {
    "trip_id": 1, "version": 1, "total_cents": -150,
    "segments": [ /* 逐段金额取原账单相反数 */ ],
    "reversed_at": "2026-10-04 03:10:00"
  }
}
```

要点：

- **新行程待另行开单**：本接口不出新账单；成功后对 `new_trip_id` 调用
  `POST /api/trips/{new_trip_id}/bill` 按当时最新费率版本开单。原账单的冲销单
  仍走既有查询（`GET /api/trips/{old_trip_id}/bill` 内嵌 `reversal`、
  `GET /api/trips/{old_trip_id}/reversal`）；原行程未开单时
  `reversal_created=false`、`reversal=null`。
- **历史快照可查、占用即时切换**：`GET /api/trips/1` 仍返回原确认内容且
  `voided_at` 非空；其航段/狭窄水道组/泊位作业占用立即失效，`new_trip_id`
  的固化占用立即生效。新快照固化目标修订号、**原快照船舶参数与档案引用**
  （船号 + 版本；无档案引用为 `null`）与原泊位即时作业区间。
- **拒绝口径（均不改变状态）**：未知行程 `404 TRIP_NOT_FOUND`；原行程已作废
  `409 TRIP_ALREADY_VOIDED`；档案停用 `409 SHIP_DISABLED`、版本已变
  `409 SHIP_VERSION_STALE`、档案已删 `404 SHIP_NOT_FOUND`；目标修订未知
  `404 TIDE_REVISION_NOT_FOUND`、已过期 `409 REVISION_STALE`；原行程仍安全
  `409 RESCHEDULE_NOT_NEEDED`；窗口内无改期解 `409 RESCHEDULE_NO_SOLUTION`
  （`details.first_violation` 给出原行程风险）；出发分钟段数不符/含负值
  `422 VALIDATION_ERROR`；与复算最优建议不一致 `409 PLAN_MISMATCH`
  （`details.recalculated` 回传当时最优建议，可据此重新提交）。
- **幂等**：同键同内容重试（含路径行程号、`target_revision`、
  `departure_minutes` 全一致）返回 `200`、`replayed=true` 与**首次结果**
  （相同的新旧行程号、复算行程、作废与冲销记录），即使这期间又生成了更新
  修订、占用/管制/日历再次变化，服务也**先重放不复算**；同键异内容
  `409 RESCHEDULE_KEY_CONFLICT`（`existing_target_revision` 附首次修订号）。
  被拒绝的尝试不消耗幂等键。
- 与批次幂等相互独立：原批次确认记录（`batch_confirmations`）与其重放语义
  完全不变；改期幂等记录单独存于 `trip_reschedules`。

---

## 4. 调用示例（curl）

```bash
BASE=http://localhost:8000

# 健康检查
curl -s $BASE/health

# 查看演示航段
curl -s $BASE/api/segments

# 0) 本轮新增：终点泊位与开放日历（演示库启动即为内港节点 D 带全天开放泊位 D1）
# 0.1) 为现有节点登记泊位（初始日历为空）；未知节点 404 NODE_NOT_FOUND，
#      重复编号 409 BERTH_EXISTS
curl -s -X POST $BASE/api/nodes/D/berths \
  -H 'Content-Type: application/json' -d '{"berth_id":"D2","note":"内港 2 号泊位"}'
# 0.2) 整组替换开放日历（半开区间、互不交叠；空组 [] 全程不开放）
curl -s -X PUT $BASE/api/berths/D2/calendar \
  -H 'Content-Type: application/json' \
  -d '{"intervals":[{"start_minute":0,"end_minute":720},
                    {"start_minute":720,"end_minute":1440}]}'
# 非法 / 交叠区间分别 422 INVALID_BERTH_INTERVAL /
# 422 OVERLAPPING_BERTH_INTERVAL，旧日历不变；未知泊位 404 BERTH_NOT_FOUND
curl -s -i -X PUT $BASE/api/berths/D2/calendar -H 'Content-Type: application/json' \
  -d '{"intervals":[{"start_minute":0,"end_minute":720},
                    {"start_minute":700,"end_minute":900}]}' | head -1
# 查询（?node=D 可按节点过滤）
curl -s $BASE/api/berths
curl -s $BASE/api/berths/D2

# 0.3) 指定终点泊位 + 正整数作业分钟预排（抵达即开工；作业区间完整落在同一
#      开放区间且不与有效确认的泊位作业占用重叠；无解 404 NO_FEASIBLE_ITINERARY）
curl -s -X POST $BASE/api/trips/plan -H 'Content-Type: application/json' -d '{
  "origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
  "draft":3.0,"height":0.0,"margin":0.0,
  "berth_id":"D1","work_minutes":30}'
# -> {"arrival_minute":90,"berth_id":"D1","work_minutes":30,
#     "work_start_minute":90,"work_end_minute":120,"itinerary":[...]}
# 泊位不属终点 422 BERTH_NOT_AT_DESTINATION；未知泊位 404 BERTH_NOT_FOUND；
# work_minutes<=0 或只给一个字段 422 VALIDATION_ERROR
# 0.4) 确认：把预排响应原样回填即可；事务内按当时日历与泊位占用复算，
#      日历/占用变化致不一致 409 PLAN_MISMATCH 不保存；成功后快照固化作业区间
PLAN=$(curl -s -X POST $BASE/api/trips/plan -H 'Content-Type: application/json' -d '{
  "origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
  "draft":3.0,"height":0.0,"margin":0.0,"revision":1,
  "berth_id":"D1","work_minutes":30}')
echo "$PLAN" | curl -s -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @-
# 之后再收紧日历不影响已固化快照；仅影响之后的预排/确认复算
curl -s -X PUT $BASE/api/berths/D1/calendar -H 'Content-Type: application/json' \
  -d '{"intervals":[{"start_minute":0,"end_minute":100}]}' >/dev/null
curl -s $BASE/api/trips/1   # work_start_minute/work_end_minute 仍为固化值
# 作废即释放泊位作业占用（历史快照仍可查）
curl -s -X POST $BASE/api/trips/1/void -H 'Content-Type: application/json' \
  -d '{"reason":"误确认：泊位作业冲突"}'

# 0a) 上一轮新增：通行费费率版本（财务按航段整组提交；继承其他航段、全局不可变）
#     为航段 2 提交费率：[0,360) 5 分/分钟、[360,1080) 8 分/分钟
curl -s -X PUT $BASE/api/segments/2/toll-rates \
  -H 'Content-Type: application/json' \
  -d '{"rates":[{"start_minute":0,"end_minute":360,"rate_cents_per_minute":5},
                {"start_minute":360,"end_minute":1080,"rate_cents_per_minute":8}],
       "note":"B->C 日间费率"}'
# 201 {"version":2,"changed_seg_id":2,...,"rates":[...seg1 继承自 v1, seg2 本次替换...]}
# 非法区间 / 组内交叠分别返回
# 422 INVALID_TOLL_RATE_INTERVAL / 422 OVERLAPPING_TOLL_RATE_INTERVAL
# 未知航段 404 SEGMENT_NOT_FOUND；任一失败整次拒绝、不生成新版本
curl -s -i -X PUT $BASE/api/segments/2/toll-rates -H 'Content-Type: application/json' \
  -d '{"rates":[{"start_minute":0,"end_minute":10,"rate_cents_per_minute":-1}]}' | head -1
# 查询版本（演示库启动即带版本 #1；未覆盖分钟费率为 0）
curl -s $BASE/api/toll/rate-versions
curl -s $BASE/api/toll/rate-versions/2

# 0b) 本轮新增：对已确认行程开单（输入只有行程号；同一事务按当时最新版本 +
#     确认快照各段进入分钟固化，旧账单不随新费率改变）
TID=1   # 用确认接口返回的行程号替换
curl -s -i -X POST $BASE/api/trips/$TID/bill            # 首次 201
# 201 {"trip_id":1,"version":2,"total_cents":...,"segments":[
#       {"leg_index":0,"seg_id":1,"entry_minute":0,"travel_time":30,
#        "rate_cents_per_minute":2,"amount_cents":60}, ...],
#      "billed_at":"...","reversal":null,"replayed":false}
curl -s -i -X POST $BASE/api/trips/$TID/bill | head -1  # 重试 200、replayed=true
# 未知行程 404 TRIP_NOT_FOUND；已作废且未开单 409 TRIP_ALREADY_VOIDED；
# 尚无费率版本 409 TOLL_RATE_VERSION_NOT_FOUND
curl -s $BASE/api/trips/$TID/bill                       # 查询账单（含冲销单）
curl -s $BASE/api/toll/bills                            # 全部账单

# 0c) 本轮新增：作废已开单行程（作废请求/响应/状态码不变；同事务生成等额冲销）
curl -s -X POST $BASE/api/trips/$TID/void \
  -H 'Content-Type: application/json' -d '{"reason":"误确认撤销"}'
# 201 {"trip_id":1,"reason":"误确认撤销","voided_at":"...","replayed":false}
curl -s $BASE/api/trips/$TID/reversal
# 200 {"trip_id":1,"version":2,"total_cents":-...,"segments":[...金额取反...],
#      "reversed_at":"..."}   （未开单 404 BILL_NOT_FOUND；未作废 404 REVERSAL_NOT_FOUND）
# 重复作废仍 200 replayed=true，不重复冲销

# 0) 本轮新增：误确认后作废行程（行程号 + 非空原因；重复作废返回首次记录）
#    （若该行程已开单，作废会在同一事务内生成等额冲销单；作废响应本身不变）
curl -s -X POST $BASE/api/trips/$TID/void \
  -H 'Content-Type: application/json' \
  -d '{"reason":"调度员误确认：船舶未离泊，撤销占用"}'
# 201 {"trip_id":1,"reason":"…","voided_at":"…","replayed":false}
# 查询作废记录（未知行程 TRIP_NOT_FOUND；未作废 404 VOID_RECORD_NOT_FOUND）
curl -s $BASE/api/trips/$TID/void
# 重复作废 -> HTTP 200、replayed=true，原因/时间仍是首次值（不覆盖）
curl -s -i -X POST $BASE/api/trips/$TID/void -H 'Content-Type: application/json' \
  -d '{"reason":"另一个原因"}' | head -1
# 历史快照仍可查（voided_at 非空即已作废）；其占用立即对后续规划/确认失效
curl -s $BASE/api/trips/$TID

# 0b) 上一轮新增：登记狭窄水道组（提交至少两条航段编号集合；建成不可变）
curl -s -X POST $BASE/api/channel-groups \
  -H 'Content-Type: application/json' \
  -d '{"segments":[1,2],"note":"北岸狭水道"}'
# {"group_id":1,"note":"北岸狭水道","created_at":"...","members":[{"position":0,...seg_id:1...},{"position":1,...seg_id:2...}]}
# 查询
curl -s $BASE/api/channel-groups
curl -s $BASE/api/channel-groups/1
# 重复编号 / 未知航段 / 冲突归组分别返回
# 422 DUPLICATE_SEGMENT_IN_GROUP / 404 SEGMENT_NOT_FOUND / 409 SEGMENT_ALREADY_GROUPED
curl -s -i -X POST $BASE/api/channel-groups -H 'Content-Type: application/json' \
  -d '{"segments":[1,2,1]}' | head -1   # HTTP/1.1 422

# 组建后规划自动按整组独占避让（只读不占位）；确认/批次确认在事务内按
# 当前组关系复算，组建后用旧规划结果确认 -> 409 PLAN_MISMATCH（批次整批回滚）

# 0b) 上一轮新增：为航段 2 设置分时航时管制（整组替换；空组 [] 恢复基础航时）
#    [0,360) 内进入航时 60 分钟，[360,720) 内进入航时 45 分钟，其余分钟用基础航时
curl -s -X PUT $BASE/api/segments/2/travel-controls \
  -H 'Content-Type: application/json' \
  -d '{"controls":[{"start_minute":0,"end_minute":360,"travel_time":60},
                   {"start_minute":360,"end_minute":720,"travel_time":45}]}'
# 查询当前生效清单（含 base_travel_time）
curl -s $BASE/api/segments/2/travel-controls
# 非法区间 / 组内交叠分别返回 422 INVALID_CONTROL_INTERVAL /
# 422 OVERLAPPING_CONTROL_INTERVAL，且旧管制保持不变
curl -s -i -X PUT $BASE/api/segments/2/travel-controls -H 'Content-Type: application/json' \
  -d '{"controls":[{"start_minute":0,"end_minute":5,"travel_time":0}]}' | head -1

# 0c) 本轮新增：船舶档案（建档 -> 船号预排 -> 船号确认 -> 版本失效）
curl -s -X POST $BASE/api/ships -H 'Content-Type: application/json' \
  -d '{"ship_no":"EVERGREEN","draft":6.5,"height":2.0,"margin":0.5,"note":"巴拿马籍"}'
# 201 {"ship_no":"EVERGREEN","version":1,"draft":6.5,...,"enabled":true}
# 重复船号 -> 409 SHIP_EXISTS；负参数 -> 422 VALIDATION_ERROR

# 船号代替三项参数预排（混用 ship_no+draft 会被 422 拒绝）
curl -s -X POST $BASE/api/trips/plan -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
       "ship_no":"EVERGREEN","revision":1}' > /tmp/ship_plan.json
# 响应回传 ship_no / ship_version 及所用 draft/height/margin

# 回填船号+版本+行程确认（不要回填三项参数）
python3 - <<'PY'
import json
p = json.load(open("/tmp/ship_plan.json"))
body = {"revision": p["revision"], "origin": p["origin"], "destination": p["destination"],
        "start_minute": p["start_minute"], "window_minutes": p["window_minutes"],
        "ship_no": p["ship_no"], "ship_version": p["ship_version"],
        "itinerary": p["itinerary"]}
json.dump(body, open("/tmp/ship_confirm.json", "w"))
PY
curl -s -X POST $BASE/api/trips/confirm -H 'Content-Type: application/json' \
  -d @/tmp/ship_confirm.json
# 201 快照固化所用参数与 ship_no/ship_version

# 档案变更：有效变更版本 +1（changed=true）；同值提交不递增（changed=false）
curl -s -X PUT $BASE/api/ships/EVERGREEN -H 'Content-Type: application/json' \
  -d '{"draft":7.0}'
# 历史版本可查
curl -s $BASE/api/ships/EVERGREEN/versions
curl -s $BASE/api/ships/EVERGREEN/versions/1

# 版本已变后用旧版本确认 -> 409 SHIP_VERSION_STALE（不保存）
curl -s -i -X POST $BASE/api/trips/confirm -H 'Content-Type: application/json' \
  -d @/tmp/ship_confirm.json | head -1
# 停用档案 -> 预排/确认 409 SHIP_DISABLED；快照仍原样可查（复核只用快照参数）
curl -s -X PUT $BASE/api/ships/EVERGREEN -H 'Content-Type: application/json' \
  -d '{"enabled":false}'
curl -s $BASE/api/trips/$TID   # ship_no/ship_version 与参数保持确认时快照

# 1) 规划（航时按各段进入分钟当时管制；途中跨管制边界也不变）
curl -s -X POST $BASE/api/trips/plan \
  -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
       "draft":5.0,"height":2.0,"margin":0.5,"revision":1}' | tee /tmp/plan.json

# 2) 确认（规划响应可直接作为确认请求体）
curl -s -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json

# 3) 潮位更正：把第 100、101 分钟改为低潮 0.2m（继承最新修订，生成新修订）
curl -s -X POST $BASE/api/tide/corrections \
  -H 'Content-Type: application/json' \
  -d '{"corrections":{"100":0.2,"101":0.2},"note":"人工低潮更正"}'

# 3b) 预测误差修订：第 100、101 分钟误差 0.3m（名义潮位不变，未列出分钟继承误差）
curl -s -X POST $BASE/api/tide/error-revisions \
  -H 'Content-Type: application/json' \
  -d '{"errors":{"100":0.3,"101":0.3},"base_revision":2,"note":"预报误差带修订"}'

# 查看某修订的名义潮位与误差
curl -s "$BASE/api/tide/revisions/3?from_minute=99&to_minute=102"

# 4) 用旧修订号再次确认 -> 409 REVISION_STALE
curl -i -X POST $BASE/api/trips/confirm \
  -H 'Content-Type: application/json' -d @/tmp/plan.json

# 5) 更正后复核已确认行程（目标修订取最新；first_violation 给首次违规）
curl -s -X POST $BASE/api/trips/$TID/review \
  -H 'Content-Type: application/json' -d '{"target_revision":2}'
# 请求体可省略（默认最新修订）：
curl -s -X POST $BASE/api/trips/$TID/review

# 6) 航段独占：第二艘同航路船规划时自动避开第一艘的占用（plan 只读不占位，
#    confirm 成功后该占用才生效）；用旧规划结果去确认会 409 PLAN_MISMATCH
curl -s -X POST $BASE/api/trips/plan -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,
       "draft":5.0,"height":2.0,"margin":0.5,"revision":1}' > /tmp/plan2.json
# /tmp/plan2.json 的出发分钟必然整体晚于已确认船（避开 seg1 的 0..29、seg2 的 30..69…）
curl -s -X POST $BASE/api/trips/confirm -H 'Content-Type: application/json' \
  -d @/tmp/plan2.json

# 7) 潮位修订后复核改期：改期建议避开其他已确认船的占用（排除被复核行程自身），
#    建议不占位、快照不改写
curl -s -X POST $BASE/api/trips/$TID/review -H 'Content-Type: application/json' -d '{}'

# 8) 本轮新增：同修订批次预排（2～5 艘，按优先顺序；后船自动避开前船拟占用，不占位）
curl -s -X POST $BASE/api/trips/batch/plan -H 'Content-Type: application/json' -d '{
  "revision": 1,
  "ships": [
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5},
    {"origin":"A","destination":"D","start_minute":0,"window_minutes":1440,"draft":5.0,"height":2.0,"margin":0.5}
  ]
}' > /tmp/batch_plan.json

# 9) 批次确认：把预排各船行程连同 revision 与批次幂等键一起提交（全成全败）
python3 - <<'PY'
import json
plan = json.load(open("/tmp/batch_plan.json"))
body = {
    "revision": plan["revision"],
    "idempotency_key": "demo-batch-0001",
    "ships": [
        {"origin": s["origin"], "destination": s["destination"],
         "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
         "draft": s["draft"], "height": s["height"], "margin": s["margin"],
         "itinerary": s["itinerary"]}
        for s in plan["ships"]
    ],
}
json.dump(body, open("/tmp/batch_confirm.json", "w"), ensure_ascii=False)
PY
curl -s -X POST $BASE/api/trips/batch/confirm \
  -H 'Content-Type: application/json' -d @/tmp/batch_confirm.json
# 同键同内容重试 -> 200 且 replayed=true、行程号不变；
# 同键异内容 -> 409 BATCH_KEY_CONFLICT；修订过期 -> 409 REVISION_STALE
curl -s -i -X POST $BASE/api/trips/batch/confirm \
  -H 'Content-Type: application/json' -d @/tmp/batch_confirm.json | head -1

# 10) 本轮新增：不安全航次改期确认（先只读复核取建议，再凭逐段出发分钟 +
#     非空幂等键执行；原行程作废、新行程确认、原账单等额冲销，同一事务）
# 10.1) 只读复核，确认原行程不安全且有改期建议
curl -s -X POST $BASE/api/trips/$TID/review \
  -H 'Content-Type: application/json' -d '{"target_revision":2}' > /tmp/review.json
# 10.2) 取出建议中的逐段出发分钟（绝对分钟），连同目标修订号与幂等键提交
python3 - <<'PY'
import json
rv = json.load(open("/tmp/review.json"))
assert not rv["safe"] and not rv["no_solution"], "仅不安全且有解时才能改期"
rs = rv["reschedule"]
body = {
    "target_revision": rv["target_revision"],
    "departure_minutes": [leg["start_minute"] for leg in rs["itinerary"]],
    "idempotency_key": "resched-trip-demo-0001",
}
json.dump(body, open("/tmp/reschedule.json", "w"), ensure_ascii=False)
PY
curl -s -X POST $BASE/api/trips/$TID/reschedule \
  -H 'Content-Type: application/json' -d @/tmp/reschedule.json
# -> 201 {"old_trip_id":…,"new_trip_id":…,"target_revision":2,"reschedule":{…},
#         "old_void":{…},"reversal_created":false,"reversal":null,"replayed":false}
# 原行程若已开单：reversal_created=true 且 reversal.total_cents 为原账单总额相反数
# 10.3) 同键同内容重试 -> 200 replayed=true，新旧行程号与首次完全相同
#      （即使此后又生成更新修订也先重放）；同键异内容 -> 409 RESCHEDULE_KEY_CONFLICT
curl -s -i -X POST $BASE/api/trips/$TID/reschedule \
  -H 'Content-Type: application/json' -d @/tmp/reschedule.json | head -1
# 10.4) 新行程待另行开单；旧账单冲销走既有查询
# curl -s -X POST $BASE/api/trips/$NEW_TID/bill
# curl -s $BASE/api/trips/$TID/reversal
# 其他拒绝：已作废 409 TRIP_ALREADY_VOIDED；修订过期 409 REVISION_STALE；
# 原行程仍安全 409 RESCHEDULE_NOT_NEEDED；无解 409 RESCHEDULE_NO_SOLUTION；
# 出发分钟与复算不一致 409 PLAN_MISMATCH（details.recalculated 给出当时最优建议）
```

Python 示例（规划 → 确认 → 更正 → 复核/改期）：

```python
import requests

B = "http://localhost:8000"
plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D",
    "start_minute": 0, "window_minutes": 720,
    "draft": 6.5, "height": 2.0, "margin": 0.5,
}).json()
trip = requests.post(f"{B}/api/trips/confirm", json=plan).json()
tid = trip["trip_id"]
print("已确认行程", tid, "第", trip["arrival_minute"], "分钟抵达")

# 潮位更正：第 100 分钟异常低潮
requests.post(f"{B}/api/tide/corrections", json={"corrections": {100: 0.2}})

# 预测误差修订：第 100 分钟名义潮位不变、误差 0.3m（旧修订与未列出分钟误差为 0）
r = requests.post(f"{B}/api/tide/error-revisions", json={"errors": {100: 0.3}})
print("误差修订号", r.json()["revision"])

# 复核原航路并取改期建议（只读，快照不变）
rv = requests.post(f"{B}/api/trips/{tid}/review", json={}).json()
if rv["safe"]:
    print("原行程在最新潮位下仍安全")
elif rv["no_solution"]:
    v = rv["first_violation"]
    print("窗口内无改期解；首次违规：航段", v["seg_id"], "分钟", v["minute"], v["reasons"])
else:
    rs = rv["reschedule"]
    print("改期：变动航段", rs["changed_leg_count"], "个，改至第",
          rs["arrival_minute"], "分钟抵达")
    print("新出发分钟：", [l["start_minute"] for l in rs["itinerary"]])
```

Python 示例（不安全航次改期确认 → 同键重放 → 新行程开单）：

```python
import requests

B = "http://localhost:8000"
TID = 1  # 已确认、在目标修订下不安全的行程

# 1) 只读复核取建议（目标修订 2）
rv = requests.post(f"{B}/api/trips/{TID}/review",
                   json={"target_revision": 2}).json()
assert not rv["safe"] and rv["reschedule"] and not rv["no_solution"]
rs = rv["reschedule"]

# 2) 凭逐段出发分钟 + 非空幂等键执行改期（原作废/新确认/冲销同事务）
body = {
    "target_revision": 2,
    "departure_minutes": [leg["start_minute"] for leg in rs["itinerary"]],
    "idempotency_key": "resched-2026-10-04-trip-1",
}
r = requests.post(f"{B}/api/trips/{TID}/reschedule", json=body)
assert r.status_code == 201, r.text
data = r.json()
new_tid = data["new_trip_id"]
print("旧行程", data["old_trip_id"], "-> 新行程", new_tid)
if data["reversal_created"]:
    print("原账单已等额冲销：", data["reversal"]["total_cents"], "分")

# 3) 同键同内容重试：200 + 首次结果（即使修订又变化也先重放，不复算）
r2 = requests.post(f"{B}/api/trips/{TID}/reschedule", json=body)
assert r2.status_code == 200 and r2.json()["replayed"] is True
assert r2.json()["new_trip_id"] == new_tid

# 4) 新行程待另行开单（按当时最新费率版本）
print(requests.post(f"{B}/api/trips/{new_tid}/bill").json()["total_cents"])
```

Python 示例（同修订批次预排 → 确认 → 幂等重试）：

```python
import requests

B = "http://localhost:8000"
ships = [
    {"origin": "A", "destination": "D", "start_minute": 0, "window_minutes": 1440,
     "draft": 5.0, "height": 2.0, "margin": 0.5},
    {"origin": "B", "destination": "D", "start_minute": 60, "window_minutes": 1380,
     "draft": 4.0, "height": 1.0, "margin": 0.3},
]

# 1) 批次预排（只读不占位；ships[1] 会避开 ships[0] 的拟占用）
plan = requests.post(f"{B}/api/trips/batch/plan",
                     json={"revision": 1, "ships": ships}).json()

# 2) 原样回填各船行程 + 修订号 + 批次幂等键，一次确认整批
confirm_body = {
    "revision": plan["revision"],
    "idempotency_key": "batch-2026-10-02-001",
    "ships": [
        {**ships[i], "itinerary": plan["ships"][i]["itinerary"]}
        for i in range(len(ships))
    ],
}
r = requests.post(f"{B}/api/trips/batch/confirm", json=confirm_body)
r.raise_for_status()
for t in r.json()["trips"]:
    print("船", t["index"], "-> 行程号", t["trip_id"],
          "抵达分钟", t["arrival_minute"])

# 3) 网络重试：同键同内容安全重放（即使期间已产生新修订）
retry = requests.post(f"{B}/api/trips/batch/confirm", json=confirm_body)
assert retry.status_code == 200 and retry.json()["replayed"] is True
```

Python 示例（航段分时航时管制 → 规划 → 确认；管制变化后旧结果确认被拒）：

```python
import requests

B = "http://localhost:8000"

# 整组替换航段 2 的分时管制：[0,360) 航时 60 分钟；提交 [] 可随时恢复基础航时
r = requests.put(f"{B}/api/segments/2/travel-controls", json={
    "controls": [
        {"start_minute": 0, "end_minute": 360, "travel_time": 60},
        {"start_minute": 360, "end_minute": 720, "travel_time": 45},
    ],
})
r.raise_for_status()
print("基础航时", r.json()["base_travel_time"], "生效区间", r.json()["controls"])

plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D",
    "start_minute": 0, "window_minutes": 1440,
    "draft": 5.0, "height": 2.0, "margin": 0.5,
}).json()
# 每段 travel_time 按该段进入分钟当时管制；途中跨越 360 边界航时也不变
print([(l["seg_id"], l["start_minute"], l["travel_time"]) for l in plan["itinerary"]])
requests.post(f"{B}/api/trips/confirm", json=plan).json()

# 管制再次调整后，已确认快照占用时长不变；用旧规划结果确认 -> PLAN_MISMATCH
requests.put(f"{B}/api/segments/2/travel-controls",
             json={"controls": [{"start_minute": 0, "end_minute": 1440,
                                 "travel_time": 80}]})
stale = requests.post(f"{B}/api/trips/confirm", json=plan)
assert stale.status_code == 409 and stale.json()["error"]["code"] == "PLAN_MISMATCH"
```

Python 示例（狭窄水道组登记 → 整组独占规划/确认 → 旧预排批次整批回滚）：

```python
import requests

B = "http://localhost:8000"

# 1) 把航段 1（A->B）与航段 2（B->C）登记为同一狭窄水道组
r = requests.post(f"{B}/api/channel-groups",
                  json={"segments": [1, 2], "note": "北岸狭水道"})
r.raise_for_status()
group = r.json()
print("组号", group["group_id"], "成员", [m["seg_id"] for m in group["members"]])

# 重复归组 / 重复编号 / 未知航段均拒绝，且不留半成品
conflict = requests.post(f"{B}/api/channel-groups", json={"segments": [1, 3]})
assert conflict.status_code == 409
assert conflict.json()["error"]["code"] == "SEGMENT_ALREADY_GROUPED"

# 2) 船 0 确认走 A->B：seg1 占 0..29
plan0 = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "B", "start_minute": 0, "window_minutes": 1440,
    "draft": 5.0, "height": 2.0, "margin": 0.5,
}).json()
requests.post(f"{B}/api/trips/confirm", json=plan0).raise_for_status()

# 3) 船 1 走 B->C：seg2 自身从未被直接占用，但组展开封锁 0..29，
#    最早第 30 分钟（船 0 的抵达分钟）进入
plan1 = requests.post(f"{B}/api/trips/plan", json={
    "origin": "B", "destination": "C", "start_minute": 0, "window_minutes": 1440,
    "draft": 3.0, "height": 2.0, "margin": 0.5,
}).json()
assert plan1["itinerary"][0]["start_minute"] == 30
print("seg2 可进入分钟", plan1["itinerary"][0]["start_minute"])

# 4) 批次预排后组建水道组，再确认 -> 事务内按当前组关系复算，PLAN_MISMATCH、
#    整批回滚（GET /api/trips 不会多出任何一条）；重新预排后确认即可成功
ships = [
    {"origin": "A", "destination": "B", "start_minute": 0, "window_minutes": 1440,
     "draft": 5.0, "height": 2.0, "margin": 0.5},
    {"origin": "B", "destination": "C", "start_minute": 0, "window_minutes": 1440,
     "draft": 3.0, "height": 2.0, "margin": 0.5},
]
# （略：在另一条未归组航段集上可完整复现；错误码断言如下）
# assert confirm.status_code == 409
# assert confirm.json()["error"]["code"] == "PLAN_MISMATCH"
```

Python 示例（误确认作废：释放占用、重复作废幂等、批次重放不受影响）：

```python
import requests

B = "http://localhost:8000"

# 1) 规划并确认一条行程
plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D", "start_minute": 0,
    "window_minutes": 1440, "draft": 5.0, "height": 2.0, "margin": 0.5,
}).json()
trip = requests.post(f"{B}/api/trips/confirm", json=plan).json()
tid = trip["trip_id"]

# 2) 误确认：凭行程号 + 非空原因作废（首次 201）
r = requests.post(f"{B}/api/trips/{tid}/void",
                  json={"reason": "调度员误确认：船舶未离泊"})
assert r.status_code == 201 and r.json()["replayed"] is False

# 3) 重复作废（不同原因也一样）：200 + 首次原因/时间，不覆盖
again = requests.post(f"{B}/api/trips/{tid}/void", json={"reason": "别的原因"})
assert again.status_code == 200 and again.json()["replayed"] is True
assert again.json()["reason"] == "调度员误确认：船舶未离泊"

# 4) 查询作废记录；历史快照仍在（voided_at 非空），但占用已释放
rec = requests.get(f"{B}/api/trips/{tid}/void").json()
print("作废时间", rec["voided_at"])
snap = requests.get(f"{B}/api/trips/{tid}").json()
assert snap["voided_at"] == rec["voided_at"]
assert snap["arrival_minute"] == trip["arrival_minute"]   # 快照原样保留

# 5) 释放后下一艘同航路船可立即使用原分钟；拿占用期旧规划去确认会 PLAN_MISMATCH
new_plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D", "start_minute": 0,
    "window_minutes": 1440, "draft": 5.0, "height": 2.0, "margin": 0.5,
}).json()
requests.post(f"{B}/api/trips/confirm", json=new_plan).raise_for_status()

# 6) 批次中某条行程作废后，原幂等键重放仍返回首次结果（含已作废行程号）
# replay = requests.post(f"{B}/api/trips/batch/confirm", json=batch_body)
# assert replay.status_code == 200 and replay.json()["replayed"] is True
```

Python 示例（船舶档案：建档 → 船号预排/确认 → 版本失效 → 历史版本）：

```python
import requests

B = "http://localhost:8000"

# 1) 建档：唯一船号 + 非负吃水/水面以上高度/安全余量 + 启停状态（初始版本 1）
r = requests.post(f"{B}/api/ships", json={
    "ship_no": "EVERGREEN", "draft": 6.5, "height": 2.0, "margin": 0.5,
    "enabled": True, "note": "巴拿马籍集装箱船",
})
assert r.status_code == 201 and r.json()["version"] == 1

# 2) 船号代替三项参数预排（混用 ship_no + draft 会被 422 拒绝）
plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D", "start_minute": 0, "window_minutes": 1440,
    "ship_no": "EVERGREEN",
}).json()
print("按档案版本", plan["ship_version"], "规划，所用吃水", plan["draft"])

# 3) 回填船号 + 版本 + 行程确认（不要回填三项参数）
confirm_body = {
    "revision": plan["revision"], "origin": plan["origin"],
    "destination": plan["destination"], "start_minute": plan["start_minute"],
    "window_minutes": plan["window_minutes"],
    "ship_no": plan["ship_no"], "ship_version": plan["ship_version"],
    "itinerary": plan["itinerary"],
}
trip = requests.post(f"{B}/api/trips/confirm", json=confirm_body).json()
print("已确认行程", trip["trip_id"], "固化档案引用", trip["ship_no"],
      "v" + str(trip["ship_version"]))

# 4) 有效变更递增版本；同值提交不递增
r = requests.put(f"{B}/api/ships/EVERGREEN", json={"draft": 7.0})
assert r.json()["changed"] is True and r.json()["version"] == 2
r = requests.put(f"{B}/api/ships/EVERGREEN", json={"draft": 7.0})
assert r.json()["changed"] is False and r.json()["version"] == 2

# 5) 档案版本已变：旧版本确认被拒（不保存），重新预排即可
stale = requests.post(f"{B}/api/trips/confirm", json=confirm_body)
assert stale.status_code == 409
assert stale.json()["error"]["code"] == "SHIP_VERSION_STALE"

# 6) 历史版本可查；已确认快照不受档案变更影响（复核只用快照参数）
versions = requests.get(f"{B}/api/ships/EVERGREEN/versions").json()
assert [v["version"] for v in versions] == [1, 2]
snap = requests.get(f"{B}/api/trips/{trip['trip_id']}").json()
assert snap["draft"] == 6.5 and snap["ship_version"] == 1   # 确认时快照原样
```

Python 示例（本轮新增：泊位登记/日历 → 泊位预排/确认 → 占用与日历复算）：

```python
import requests

B = "http://localhost:8000"

# 1) 为现有节点 D 登记泊位，并整组设置开放日历（半开区间、互不交叠）
r = requests.post(f"{B}/api/nodes/D/berths", json={"berth_id": "D2", "note": "2 号泊位"})
assert r.status_code == 201 and r.json()["calendar"] == []
requests.put(f"{B}/api/berths/D2/calendar", json={"intervals": [
    {"start_minute": 0, "end_minute": 720},
    {"start_minute": 720, "end_minute": 1440},
]}).raise_for_status()

# 2) 指定终点泊位 + 正整数作业分钟预排（抵达即开工）
plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D", "start_minute": 0, "window_minutes": 1440,
    "draft": 3.0, "height": 0.0, "margin": 0.0,
    "berth_id": "D2", "work_minutes": 30,
}).json()
assert plan["arrival_minute"] == plan["work_start_minute"] == 90
assert plan["work_end_minute"] == 120

# 3) 原样回填确认；快照固化泊位与作业区间
trip = requests.post(f"{B}/api/trips/confirm", json=plan).json()
assert (trip["berth_id"], trip["work_start_minute"], trip["work_end_minute"]) == ("D2", 90, 120)

# 4) 日历变更不影响历史快照；第二艘船预排自动避开 [90,120) 的泊位作业占用
requests.put(f"{B}/api/berths/D2/calendar",
             json={"intervals": [{"start_minute": 0, "end_minute": 1440}]}).raise_for_status()
assert requests.get(f"{B}/api/trips/{trip['trip_id']}").json()["work_end_minute"] == 120

# 5) 用日历收紧后的旧规划结果再确认 -> PLAN_MISMATCH（不保存）
requests.put(f"{B}/api/berths/D2/calendar",
             json={"intervals": [{"start_minute": 0, "end_minute": 100}]}).raise_for_status()
stale = requests.post(f"{B}/api/trips/confirm", json=plan)
assert stale.status_code == 409 and stale.json()["error"]["code"] == "PLAN_MISMATCH"
```

本轮修复示例（正航时回路：最早抵达优先，同刻按**完整**航段编号序列字典序）：

```bash
# 自定义 4 节点 + 回路图：1 A->B、2 B->C、3 C->B、4 B->D，各耗时 1 分钟
for n in A B C D; do
  curl -s -X POST $BASE/api/nodes -H 'Content-Type: application/json' \
    -d "{\"node_id\":\"$n\"}" >/dev/null
done
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"A","destination":"B","travel_time":1,"depth":99}' >/dev/null   # seg 1
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"B","destination":"C","travel_time":1,"depth":99}' >/dev/null   # seg 2
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"C","destination":"B","travel_time":1,"depth":99}' >/dev/null   # seg 3
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"B","destination":"D","travel_time":1,"depth":0.5}' >/dev/null  # seg 4：浅水
# 潮位：先用初始化脚本/直接写库生成修订 #1（分钟 0..7 名义潮位 0），再以
# 名义潮位更正把分钟 3..7 抬到 3.0，得到修订 #2（分钟 0..2 低潮，seg 4
# 水深 0.5 < 吃水 1.0 不可行；分钟 3 高潮，进入 seg 4 占用分钟 3 可行）。
# 演示库本身即有修订 #1（分钟 0..1439），可直接对其更正：
curl -s -X POST $BASE/api/tide/corrections -H 'Content-Type: application/json' \
  -d '{"base_revision":1,"corrections":{"3":3.0,"4":3.0,"5":3.0,"6":3.0,"7":3.0}}'
# 规划：从 A 于分钟 0 起、窗口 8 分钟、按修订 #2（seg 4 仅分钟 3 的航程可行）
curl -s -X POST $BASE/api/trips/plan -H 'Content-Type: application/json' -d '{
  "origin":"A","destination":"D","start_minute":0,"window_minutes":8,
  "draft":1.0,"height":0.0,"margin":0.0,"revision":2}'
# -> {"arrival_minute":4,"total_wait_minutes":0,
#     "itinerary":[
#       {"seg_id":1,"origin":"A","destination":"B","start_minute":0,"end_minute":1,"travel_time":1},
#       {"seg_id":2,"origin":"B","destination":"C","start_minute":1,"end_minute":2,"travel_time":1},
#       {"seg_id":3,"origin":"C","destination":"B","start_minute":2,"end_minute":3,"travel_time":1},
#       {"seg_id":4,"origin":"B","destination":"D","start_minute":3,"end_minute":4,"travel_time":1}]}
# 旧实现会在 (B, 分钟3) 把较长前缀 (1,2,3) 剪掉而返回 [1,4]（先在 B 等到
# 分钟 3）；修复后两条航路同于分钟 4 抵达，完整序列 (1,2,3,4) < (1,4)。
# 把该响应原样 POST /api/trips/confirm 即与复算一致地确认（201）；批次
# /api/trips/batch/plan 与 /api/trips/batch/confirm 结果相同。
```

```python
# 同一场景的 Python 调用（requests）
import requests

B = "http://localhost:8000"
for n in ("A", "B", "C", "D"):
    requests.post(f"{B}/api/nodes", json={"node_id": n})
for o, d, depth in [("A", "B", 99), ("B", "C", 99), ("C", "B", 99), ("B", "D", 0.5)]:
    requests.post(f"{B}/api/segments",
                  json={"origin": o, "destination": d, "travel_time": 1, "depth": depth})
# 演示库自带修订 #1（0..1439）；把分钟 3..7 抬到高潮生成修订 #2
rev = requests.post(f"{B}/api/tide/corrections",
                    json={"base_revision": 1,
                          "corrections": {3: 3.0, 4: 3.0, 5: 3.0, 6: 3.0, 7: 3.0}}).json()["revision"]
plan = requests.post(f"{B}/api/trips/plan", json={
    "origin": "A", "destination": "D", "start_minute": 0, "window_minutes": 8,
    "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
}).json()
assert [leg["seg_id"] for leg in plan["itinerary"]] == [1, 2, 3, 4]
assert plan["arrival_minute"] == 4 and plan["total_wait_minutes"] == 0
# 确认复算一致
r = requests.post(f"{B}/api/trips/confirm", json=plan)
assert r.status_code == 201
```

录入自定义数据：

```bash
curl -s -X POST $BASE/api/nodes    -H 'Content-Type: application/json' -d '{"node_id":"E","name":"新码头"}'
curl -s -X POST $BASE/api/segments -H 'Content-Type: application/json' \
  -d '{"origin":"D","destination":"E","travel_time":15,"depth":4.2,"clearance":7.0}'
```

---

## 5. 算法说明

窗口不超过 1440 分钟，采用**时间展开动态规划**（实现见 `app/services/planning.py`）：

1. 校验节点与修订；取窗口内全部名义潮位与预测误差，任一分钟缺名义潮位立即报
   `MISSING_TIDE_DATA`；同时读取窗口内全部已确认快照的航段占用
   （`load_occupancy`，既有快照彼此重叠也原样计入，并按当前狭窄水道组关系
   展开为整组占用）与各航段的分时航时管制（`load_controls`）；
2. 对每条航段用"不可行分钟前缀和"预处理：任意进入分钟先按管制选定**实际航时**
   （进入分钟命中区间取该区间通行分钟，否则取基础航时；途中跨边界不变），再判定
   该实际航程是否全程满足误差带约束（水深 `深度+名义−误差`、净空
   `净空−名义−误差`）**且无任何一分钟被其他已确认快照占用**、且不越出窗口，
   查询为 O(1)；
3. 时间展开**三遍 DP**（状态 `(节点, 分钟)`）：
   - **第一遍（正向可达，求最早抵达）**：只记录可达性、不保留序列。转移为
     等待（同一节点推进到下一分钟，不追加任何编号）与出发（在该节点的整
     分钟进入可行航段，落入更晚分钟）。终点为吸收态：非泊位模式终点首次
     可达分钟即最早抵达 `T`；泊位模式按分钟升序取首个泊位作业可行的新鲜
     抵达。分时航时下"晚出发反而早到"也按抵达分钟升序被发现（展开自更早
     迭代，航时恒为正整数）；
   - **第二遍（反向可达）**：判定每个 `(节点, 分钟)` 是否仍能恰于 `T` 抵达
     终点（终点只在 `(终点, T)` 可接受——更早抵达不可等待、更晚非最早）；
     并为每条航段预处理"不早于当前分钟的下一个既全程可行、到达点状态又能
     按时完成的进入分钟"（倒序 O(1) 继承）；
   - **第三遍（贪心重建）**：自 `(起点, 0)` 起，在"等待"（不追加编号）与
     各可行航段（追加该段编号）所导出的**完整序列**中取字典序最小者。同一
     航段的多个可进入分钟里取最早者：在同一节点，晚一刻可导出的序列集合是
     早一刻的子集（等待不增加任何航段编号），最早进入分钟的后缀字典序必不
     更差；
4. 沿重建结果输出每段的进入/抵达分钟；中间跳过的分钟即节点等待。

> **为什么不能对 `(节点, 分钟)` 只保留字典序最小前缀**（旧实现的缺陷）：
> 所有航段航时均为正整数，**正航时回路**会让长度不同的序列在同一
> `(节点, 分钟)` 会合；当较短序列恰是较长序列的**前缀**时，二者追加同一
> 后缀后字典序大小关系会**反转**（Python 序列比较把短前缀判小，但
> `s1+x` 与 `s2+x` 在 `s1` 为 `s2` 前缀时以后缀首个编号决胜）。旧剪枝因此
> 会把全局字典序最小的完整航路误删。例：航段 1 `A→B`、2 `B→C`、
> 3 `C→B`、4 `B→D` 各耗时 1 分钟，1～3 在分钟 0～3 可通行，4 仅分钟 3
> 满足水深；从 A 于分钟 0 起、窗口 4 分钟，`(1)` 与 `(1,2,3)` 在
> `(B, 分钟3)` 会合，旧实现返回 `[1,4]`，修复后返回同刻（分钟 4）抵达的
> `[1,2,3,4]`——完整序列 `(1,2,3,4) < (1,4)`，中间等待不产生编号、不计
> 入序列。三遍 DP 只做"能否按时到达"的可达剪枝（同一 `(节点, 分钟)` 的
> 可行后续完全相同），不比较前缀，故不会丢失任何完整航路。复杂度
> O(W·E)（W≤1440）。

### 终点泊位与开放日历（实现见 `app/services/berths.py`、`planning.py`）

1. 建表两张：`berths`（泊位编号主键、所属节点 `node_id` 外键、建成即固定；
   登记时未知节点 `NODE_NOT_FOUND`、编号重复 `BERTH_EXISTS`）与
   `berth_open_intervals`（`berth_id, start_minute` 主键的半开区间）。日历
   整组替换在单事务内"先全删后插"，服务层先校验区间合法性
   （`INVALID_BERTH_INTERVAL`）与组内半开交叠
   （`OVERLAPPING_BERTH_INTERVAL`），任一失败回滚、旧日历原样保留。
2. 泊位作业可行性是抵达分钟上的额外门槛（`planning._berth_window_ok`）：
   作业区间 `[a, a+w)` 不越窗口、完整落在**同一**开放区间
   （`berths.work_fits_calendar`，跨区间拼接不合法）、且不与
   `load_berth_occupancy` 读出的有效确认作业区间半开重叠
   （已作废快照经 `voided_trips` 过滤即时释放；批次另把前船复算作业区间
   并入内存占用）。
3. **DP 调整**：船抵达终点即开工、不能在终点等待——泊位模式下终点是
   "吸收态"：等待传播与航段展开均不经过终点，落入终点的航段在对应抵达
   分钟标记为"新鲜抵达"。第一遍正向 DP 按分钟升序取首个泊位作业可行的
   新鲜抵达分钟 `T`；第二/三遍在"终点仅 `(终点, T)` 可接受"的边界下做
   反向可达与完整序列字典序贪心（正航时回路同样不再误剪前缀）。未指定
   泊位时 DP 同样按三遍算法执行，响应仅补 4 个 `null` 泊位字段。
4. 确认（单船/批次）在原事务内以当时日历与泊位占用复算，除行程逐段一致
   外还比对泊位与作业分钟（不一致 `PLAN_MISMATCH`，批次全批回滚）；通过
   后把 `berth_id/work_minutes/work_start_minute/work_end_minute` 写入
   `confirmed_trips` 不可变快照——之后日历替换不改历史，占用始终按固化
   区间计算；旧库经 `_migrate` 自动补四列（既有行程为 `NULL`）。
5. 改期 DP（`reschedule._reschedule`）在候选抵达集合上按同一门槛过滤
   （原泊位当前日历 + 其他有效快照泊位占用，排除自身），无候选抵达即
   `no_solution`；原航路潮位复核 `_check_original` 不读泊位。

### 狭窄水道组（实现见 `app/services/groups.py`）

1. 建表两张：`channel_groups`（组号自增、备注；建成后**无任何更新/删除路径**）
   与 `channel_group_members`（`group_id, seg_id, position`，`seg_id UNIQUE`
   从数据库层保证航段不得重复归组）。
2. `create_group`：先在内存判重（`DUPLICATE_SEGMENT_IN_GROUP`），再一次查出
   未知航段（`SEGMENT_NOT_FOUND`）与已归组航段（`SEGMENT_ALREADY_GROUPED`），
   全部通过后在单个 `BEGIN IMMEDIATE` 事务内插入组与全部成员；任一异常
   `ROLLBACK`（不留半成品），并发竞争由 `seg_id` 唯一约束兜底并转成同一
   冲突码。
3. **组占用展开**：`load_occupancy` 读出全部快照的原占用后，调用
   `expand_group_occupancy` 取闭包——凡某航段属于组，其已计入的占用分钟
   同步到同组每个成员，反复传播至稳定。展开按各快照段**固化的
   `travel_time`**（`d..d+t-1`）进行，因此**组建前**的快照只要经过组内任一
   航段即自动回溯计入新组占用，而快照 JSON 绝不改写；组建前已有的重叠同样
   不清理（展开后仍为重叠事实）。
4. 单船规划/确认、批次预排/确认均经由 `load_occupancy` 取得展开后的占用；
   批次内前船的**拟占用/复算占用**在并入内存 blocked 后也调用同一展开函数
   （组关系在批次确认事务开始时读取一次，组建成后不可变，事务内无失效窗口）。
5. 复核改期以 `load_occupancy(exclude_trip_id=本行程)` 读占用，故只避开
   **其他**快照的组占用、排除自身；原航路潮位复核 `_check_original` 完全不
   读取占用与组关系，口径仍为只判潮位误差带。

### 误确认行程作废（实现见 `app/services/voids.py`）

1. 建表 `voided_trips(trip_id PRIMARY KEY REFERENCES confirmed_trips,
   reason NOT NULL, voided_at)`：每个行程至多一行，`trip_id` 主键从数据库层
   兜底并发重复作废；旧库启动时随 `SCHEMA` 幂等建表，无需手工迁移。
2. **作废**（`void_trip`，单事务 `BEGIN IMMEDIATE`）：先核验行程存在
   （否则 `TRIP_NOT_FOUND`），再查既有作废记录——命中则 `COMMIT` 后直接
   返回**首次记录**（标记 `replayed=true`）；否则插入不可变记录并返回。
   并发竞争下若 `INSERT` 撞主键，回滚后重读首次记录返回（同样不覆盖）。
   作废不写 `confirmed_trips`、不改正 `batch_confirmations`。
3. **占用失效在读侧**：`load_occupancy` 以
   `confirmed_trips LEFT JOIN voided_trips … WHERE v.trip_id IS NULL`
   过滤，已作废快照的原占用（含按固化航时的组展开）不再进入 blocked；
   快照 JSON 与历史查询原样保留。因所有写路径（单船确认、批次确认、作废）
   均在 `BEGIN IMMEDIATE` 事务内串行，作废先提交时后到确认读到的就是释放后
   的占用，确认先提交时后到作废看到的是新快照——不存在相互重叠的有效确认。
4. **批次重放优先**：`confirm_batch` 以幂等键命中分支为第一优先级，重放既
   不读占用也不检查作废状态，故已作废批次行程的原键重放仍返回首次结果且不
   重新产生占用；只有全新键的确认复算才会应用「只避有效快照」的占用口径。
5. **复核按历史快照**：`review_and_reschedule` 直接读 `confirmed_trips`
   行做原航路潮位检查，与是否作废无关；仅改期 DP 的 blocked 来自
   `load_occupancy(exclude_trip_id=…)`，天然只含仍有效的其他快照。
6. **冲销在作废事务内生成**：首次作废插入 `voided_trips` 后，同一事务调用
   `toll.create_reversal_in_void`——已开单则写入 `toll_reversals`
   （逐段金额与总额取账单相反数、版本沿用账单版本），未开单为空操作；
   重复作废在第 2 步的重放分支提前返回，绝不重复冲销。作废 HTTP 响应
   不含冲销信息（请求/响应/状态码保持原样），冲销单另走
   `GET /api/trips/{id}/reversal` 与账单内嵌 `reversal` 查询。

### 通行费费率版本 / 账单 / 冲销单（实现见 `app/services/toll.py`）

1. 建表三张：`toll_rate_versions(version PK, changed_seg_id, note,
   created_at)`、`toll_rates(version, seg_id, start_minute, end_minute,
   rate_cents_per_minute)`（每版本**全量物化**全部航段区间，版本不可变、
   独立可查）、`toll_bills(trip_id PK, version, total_cents, segments JSON,
   billed_at)`，以及 `toll_reversals(bill_trip_id PK, version, total_cents≤0,
   segments JSON, reversed_at)`。两张主键唯一约束从数据库层兜底重复开单与
   重复冲销；旧库启动随 `SCHEMA` 幂等建表，无需手工迁移。
2. **费率替换**（`replace_segment_rates`，单事务）：先在事务外完成未知航段
   与区间校验（非法 `INVALID_TOLL_RATE_INTERVAL`、交叠
   `OVERLAPPING_TOLL_RATE_INTERVAL`，失败不出版本）；事务内插入版本行，
   从上一版本 `INSERT … SELECT` 复制全部航段区间，再删除并重建该航段区间
   （空组即只删不插）。空库第一次提交即版本 1，其他航段隐式为 0。
3. **开单**（`issue_bill`，单事务 `BEGIN IMMEDIATE`）：行程未知
   `TRIP_NOT_FOUND`；已有账单直接重放首次值（`replayed=true`）；已作废
   且未开单 `TRIP_ALREADY_VOIDED`；无费率版本
   `TOLL_RATE_VERSION_NOT_FOUND`。通过后取 `MAX(version)` 为当时最新版本，
   逐段以快照 `start_minute`（进入分钟）在该版本区间中选费率
   （`rate_at`，未覆盖返回 0），金额 = 快照固化 `travel_time` × 费率，
   逐段 JSON 与整数分总额一次落库。新费率版本不触碰历史账单行。
4. **冲销**（`create_reversal_in_void`）：由 `void_trip` 在其**已持有的
   作废事务**内调用，账单不存在为空操作；存在则插入逐段金额取反、总额为
   账单相反数的冲销行。因开单与作废都在 `BEGIN IMMEDIATE` 下串行：
   开单先提交 -> 作废同事务必读到账单并写冲销；作废先提交 -> 开单读到
   作废标识而拒绝。任何顺序下「有账单且已作废 ⇒ 恰有一张等额冲销」。
5. **演示数据**：`SEED_DEMO=true` 的空库初始化时附带费率版本 #1（seg1
   A->B 的 `[0,360)` 2 分、`[360,720)` 1 分、`[720,1440)` 2 分/分钟），
   启动即可体验开单全流程；其余航段未覆盖分钟费率 0。

### 船舶档案（实现见 `app/services/ships.py`）

1. 建表两张：`ships`（船号主键，身份行）与 `ship_versions`
   （`PRIMARY KEY(ship_no, version)`，每版本一行，含非负吃水/水面以上
   高度/安全余量/启停状态与备注）。**当前版本即各船号的最大版本**；
   历史版本只增不改，版本查询直接读表。`confirmed_trips` 增加可空
   `ship_no/ship_version` 两列（旧库经 `_migrate` 以 `ALTER TABLE`
   平滑补齐，既有行程为 `NULL`，表示无档案引用）。
2. **建档 / 变更**均在单个 `BEGIN IMMEDIATE` 事务内完成：建档插入身份行
   + 版本 1，重复船号由主键兜底转 `SHIP_EXISTS`；变更先把未给出字段合并
   为当前值，四项任一实际不同才插入 `version+1` 行（响应 `changed=true`），
   完全一致的提交不递增版本（`changed=false`）；任一失败 `ROLLBACK`，
   不留半成品。
3. **船号预排**：`resolve_for_plan` 取当前版本，停用即 `SHIP_DISABLED`；
   解析出的参数走与直接提交参数完全相同的 `find_itinerary` 与择优规则，
   响应附带 `ship_no/ship_version` 与所用参数。请求模型层用
   `model_validator` 保证船号与三项参数**互斥且必居其一**（混用或皆缺
   `422 VALIDATION_ERROR`）。
4. **档案确认**：`resolve_for_confirm` 在确认事务（`BEGIN IMMEDIATE`）内
   先核对档案仍启用（`SHIP_DISABLED`）且当前版本等于回填版本
   （`SHIP_VERSION_STALE`），再做既有的修订新鲜度检查与占用/管制复算；
   批次确认在幂等重放判定之后、修订检查之前对全部船统一核对（附
   `ship_index`，任一失效全批回滚）。快照写入所用参数与
   `ship_no/ship_version`，此后档案变更与快照无关。
5. **快照口径**：复核/改期只读 `confirmed_trips` 的固化参数，从不查询
   档案表；作废、批次幂等重放等既有路径不感知档案（重放优先、不重新
   核对），语义完全不变。

### 同修订批次预排 / 确认（实现见 `app/services/batch.py`）

1. **批次预排**：先解析批次修订号（显式指定须存在，否则取最新；未知沿用
   `TIDE_REVISION_NOT_FOUND`）；一次读取覆盖全批各船窗口并集区间
   `[min(start), max(start+window-1)]` 的全部已确认占用（含狭窄水道组
   展开）。随后按 `ships` 优先顺序逐艘调用上面的单船 DP（同一修订、同一
   择优规则），每艘规划成功后把其行程 `d..d+t-1` 并入内存中的 blocked 并
   **按当前组关系同步展开到整组**，作为下一艘的占用；预排全程只读。
   任一船抛 `NODE_NOT_FOUND` / `MISSING_TIDE_DATA` / `NO_FEASIBLE_ITINERARY` 时，
   原样附加 `ship_index`（首个失败序号，0 起）后抛出。
2. **批次确认**（单事务 `BEGIN IMMEDIATE`）：先按幂等键查 `batch_confirmations`：
   命中且内容哈希（请求体去掉幂等键后的规范化 SHA-256）与修订号一致 -> 直接返回
   存储的首次结果（标记 `replayed=true`，**优先于修订新鲜度检查**）；命中但不一致
   -> `BATCH_KEY_CONFLICT`。新键时依次做：修订新鲜度（`REVISION_STALE`）->
   读取当时已确认占用（含狭窄水道组展开）与当前组关系 -> 逐艘复算
   （`NO_FEASIBLE_ITINERARY` 转
   `PLAN_MISMATCH(no_feasible_itinerary)`，与提交行程六元组逐段或抵达分钟不一致转
   `PLAN_MISMATCH(itinerary_mismatch)`，均带 `ship_index`）-> 复算通过即在同一事务
   写入该船快照并把其**复算行程**按组展开并入后船占用。全部通过后写入幂等记录并 `COMMIT`；
   任一异常 `ROLLBACK`，已插入快照随之撤销，实现全成全败。唯一键竞争（并发同键）
   在回滚后按已存在记录重判重放/冲突。

### 更正后复核 / 改期（实现见 `app/services/reschedule.py`）

1. 只读取出已确认快照（行程号、快照修订、船舶参数、原起始分钟/窗口、完整行程 JSON），
   校验目标修订（缺省取最新；未知修订 `TIDE_REVISION_NOT_FOUND`），取全窗口名义
   潮位与误差，任一分钟缺名义潮位报 `MISSING_TIDE_DATA`；未知行程报 `TRIP_NOT_FOUND`。
   另以 `load_occupancy(..., exclude_trip_id=本行程)` 读取**其他**已确认快照的占用
   （同样按当前狭窄水道组关系展开为整组占用；本快照自身排除）。
2. **原航路复核**：按快照航段顺序遍历每段原占用分钟（`d..d+t-1`），只用与规划相同的
   误差带 `_minute_ok` 判定（水深 `深度+名义−误差`、净空 `净空−名义−误差`），
   **不检查与其他快照的占用重叠、也不读取组关系**；返回首个违规分钟及其
   `depth`/`clearance` 原因；`available_depth`/`available_clearance` 按上述对应极值
   计算。
3. **改期 DP**：航段序列固定（快照的 k 个 `seg_id`），状态为「完成前 i 段、于某分钟
   抵达该段终点」，每状态只保留一个最优代价
   `(相对快照的出发分钟变动段数, 各段出发分钟序列)`（字典序）。可行表复用规划的
   不可行分钟前缀和，每分钟的不可行原因同时包含误差带违规、**他船航段占用**与
   **他船经狭窄水道组展开的整组占用**（本快照自身已排除）；各段航时按**改期请求时的
   当前管制**在出发分钟选定；第 i 段用「抵达分钟
   ≤ 当前出发分钟」的逐分钟最优前缀扫描（等待由前缀隐式表达），命中可行进入分钟即
   追加该段出发分钟与当时航时，单次判定 O(1)。
4. 终态在全部抵达分钟上取 `(变动段数, 抵达分钟, 出发分钟序列)` 字典序最小者；无终态
   即 `no_solution`。复杂度 O(k·W)，且全程不写库。

剪枝依据：同一 `(i, 抵达分钟)` 上若代价 `(c1, d1) < (c2, d2)`，则追加同一段的同一
出发分钟后，变动段数同增 0/1、出发序列同追加同一值，字典序关系不变，故每状态只保留
一个最优代价不会丢失最优解。

### 不安全航次改期确认（实现见 `app/services/reschedule_confirm.py`）

1. 新增表 `trip_reschedules(idempotency_key PK, old_trip_id, target_revision,
   content_hash, result JSON, confirmed_at)`：保存每次**首次成功**改期的提交
   指纹（行程号 + 目标修订 + 逐段出发分钟的规范化 SHA-256，与批次同算法）与
   首次结果；旧库启动随 `SCHEMA` 幂等建表，无需手工迁移。
2. **单事务 `BEGIN IMMEDIATE`，幂等重放优先于一切**：命中同键先比对内容
   指纹，一致即直接返回存储结果（标记 `replayed=true`），**早于**修订新鲜度、
   作废状态等全部检查——故重放后即使修订又更新、原行程已被本次改期作废也照
   常返回首次的新旧行程号；指纹不同 -> `RESCHEDULE_KEY_CONFLICT`。
3. **前提校验链**（任一失败 `ROLLBACK`、不写任何状态，也不登记幂等键）：
   原快照存在（`TRIP_NOT_FOUND`）且未作废（`TRIP_ALREADY_VOIDED`）->
   提交段数等于快照航段数（`VALIDATION_ERROR`）-> 快照固化船号时核对档案
   存在/启用/版本未变（`SHIP_NOT_FOUND`/`SHIP_DISABLED`/
   `SHIP_VERSION_STALE`；复算参数始终取快照值）-> 目标修订存在且为
   `MAX(revision)`（`TIDE_REVISION_NOT_FOUND`/`REVISION_STALE`）->
   复用只读复核的上下文加载与 `_check_original`（缺潮位
   `MISSING_TIDE_DATA`），原行程仍安全 -> `RESCHEDULE_NOT_NEEDED` ->
   调 `solve_reschedule`（与只读 review 同一求解函数：当前管制/组/泊位
   日历/他人占用，`exclude_trip_id` 排除原行程）求最优建议，无解 ->
   `RESCHEDULE_NO_SOLUTION` -> 提交出发分钟与复算
   `itinerary[*].start_minute` 逐段比对，不一致 ->
   `PLAN_MISMATCH(details.recalculated)`。
4. **提交动作（同事务）**：先插入新 `confirmed_trips`（revision=目标修订、
   参数与档案引用取自旧快照、行程/泊位作业区间取自复算结果），再插入原行程
   `voided_trips` 作废行（系统生成原因；旧占用经读侧 `voided_trips` 过滤
   即时失效），随后调用既有的 `toll.create_reversal_in_void`——原账单存在则
   等额冲销恰好一次（未开单为空操作），最后写入幂等记录并 `COMMIT`。
   幂等键唯一约束兜底并发竞争（回滚后按已落库记录重判重放/冲突）。
5. **契约不变**：不删改任何 `confirmed_trips` 行、不触碰
   `batch_confirmations`；只读 `review` 路径仅做了无行为变化的函数拆分
   （`_load_review_context` + `solve_reschedule` 供两处复用）。

---

## 6. 项目结构

```
app/
  config.py             # 环境变量配置
  database.py           # SQLite 连接、建表、演示数据初始化
  errors.py             # 统一业务错误（含 trip_not_found 构造器）
  schemas.py            # Pydantic 模型与入参校验（窗口<=1440、航时>0 等）
  services/controls.py  # 航段分时航时管制：区间校验、整组替换、按进入分钟取实际航时
  services/groups.py    # 狭窄水道组：登记（单事务、不留半成品）、组关系读取与整组占用展开
  services/planning.py  # 潮汐约束与时间展开 DP 规划算法（航时按进入分钟管制选定；load_occupancy 展开组占用）
  services/batch.py     # 同修订批次预排/确认（逐艘串行规划 + 事务复算 + 幂等键；批内拟占用同步展开组占用）
  services/reschedule.py# 更正后原行程复核与改期 DP（复核只判潮位；改期避开他人组占用且排除自身、按当前管制，只读）
  services/reschedule_confirm.py# 不安全航次改期确认：单事务幂等（重放优先）、档案/修订/不安全/有解校验、新快照+原作废+原账单等额冲销
  services/voids.py     # 误确认作废：事务内作废（重复作废返回首次记录）、作废记录查询；已开单则同事务生成等额冲销
  services/toll.py      # 通行费：费率版本（整组替换+继承，不可变）、开单（按当时最新版本与快照进入分钟固化）、冲销/查询
  services/ships.py     # 船舶档案：建档/版本化变更（单事务）、当前与历史版本查询、规划/确认用档案解析
  main.py               # FastAPI 路由（节点/航段/分时管制/狭窄水道组/船舶档案/潮位更正与误差修订/单船与批次规划确认/复核改期/改期确认/作废）
tests/                  # pytest 测试（229 个，含择优、缺潮位、事务冲突、误差带、复核改期、改期确认与幂等/冲销、航段占用、批次预排/确认/幂等、分时航时管制、狭窄水道组、作废与并发串行、船舶档案与船号规划确认等）
Dockerfile
docker-compose.yml
requirements.txt
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest -q
```
