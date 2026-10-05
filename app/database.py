"""SQLite 连接管理、建表与初始化（含演示数据）。"""
import os
import sqlite3
from contextlib import contextmanager

from . import config
from .errors import ApiError


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    node_id     TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS segments (
    seg_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    origin       TEXT NOT NULL REFERENCES nodes(node_id),
    destination  TEXT NOT NULL REFERENCES nodes(node_id),
    travel_time  INTEGER NOT NULL CHECK (travel_time > 0),   -- 正整数分钟航时
    depth        REAL NOT NULL,                             -- 静水深度（米）
    clearance    REAL,                                     -- 桥底净空（米），可空
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (origin <> destination)
);

-- 潮位修订：base_revision 为 NULL 表示**物化修订**（全部逐分钟数据在
-- tide_values）；非空表示**差量修订**（相对基础修订的变化分钟在 tide_deltas，
-- 未改分钟沿基础链继承）。旧库升级时既有修订一律补为 NULL（物化），历史值
-- 原样保留在 tide_values。
CREATE TABLE IF NOT EXISTS tide_revisions (
    revision   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    note       TEXT,
    base_revision INTEGER REFERENCES tide_revisions(revision)
);

-- 物化修订的逐分钟全量数据（名义潮位 + 预测误差）。
CREATE TABLE IF NOT EXISTS tide_values (
    revision INTEGER NOT NULL REFERENCES tide_revisions(revision),
    minute   INTEGER NOT NULL,
    level    REAL NOT NULL,                             -- 名义潮位（米）
    error    REAL NOT NULL DEFAULT 0.0,                 -- 预测误差（米，非负）：真值落在 level±error
    PRIMARY KEY (revision, minute)
);

-- 差量修订的逐分钟变更：每行保存该分钟在**本修订下的最终** (level, error)。
-- 每次修订的持久化写入量只随本次改动分钟数增长，不再复制基础修订的全部分钟；
-- 任意历史修订沿基础链还原（物化根打底、差量自老到新逐层覆盖）。
CREATE TABLE IF NOT EXISTS tide_deltas (
    revision INTEGER NOT NULL REFERENCES tide_revisions(revision),
    minute   INTEGER NOT NULL,
    level    REAL NOT NULL,                             -- 本修订下该分钟的名义潮位（米）
    error    REAL NOT NULL DEFAULT 0.0,                 -- 本修订下该分钟的预测误差（米，非负）
    PRIMARY KEY (revision, minute)
);

-- 船舶档案：调度员按唯一船号管理。每次有效变更（吃水/水面以上高度/安全余量/
-- 启停状态任一实际变化）递增版本号；ship_versions 保存全部历史版本，当前版本
-- 即各船号的最大版本。档案只增不改——历史版本永不可变，供当前与历史查询。
CREATE TABLE IF NOT EXISTS ships (
    ship_no     TEXT PRIMARY KEY,                           -- 唯一船号
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS ship_versions (
    ship_no     TEXT NOT NULL REFERENCES ships(ship_no),
    version     INTEGER NOT NULL CHECK (version >= 1),      -- 自 1 起逐次递增
    draft       REAL NOT NULL CHECK (draft >= 0),           -- 非负吃水（米）
    height      REAL NOT NULL CHECK (height >= 0),          -- 非负水面以上高度（米）
    margin      REAL NOT NULL CHECK (margin >= 0),          -- 非负安全余量（米）
    enabled     INTEGER NOT NULL CHECK (enabled IN (0, 1)), -- 启停状态：1 启用 / 0 停用
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (ship_no, version)
);

-- 泊位：调度员为**现有节点**登记的靠泊资源。泊位编号全局唯一；每个泊位
-- 维护一组半开分钟开放日历区间 [start_minute, end_minute)（整组替换、
-- 互不交叠），船抵达后作业区间 [arrival, arrival+work) 必须完整落在某一
-- 开放区间内。泊位属性（所属节点）建成即固定；日历可随时整组替换，
-- 替换不影响已确认快照固化的泊位作业区间（见 confirmed_trips）。
CREATE TABLE IF NOT EXISTS berths (
    berth_id    TEXT PRIMARY KEY,                           -- 全局唯一泊位编号
    node_id     TEXT NOT NULL REFERENCES nodes(node_id),    -- 所属节点（建成固定）
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 泊位开放日历：半开分钟区间 [start_minute, end_minute)。同一泊位的区间
-- 互不半开交叠（首尾相接合法），服务层保证；整组替换（同 berth_id 先全删
-- 再插），任何校验失败回滚、旧日历原样保留。
CREATE TABLE IF NOT EXISTS berth_open_intervals (
    berth_id     TEXT NOT NULL REFERENCES berths(berth_id),
    start_minute INTEGER NOT NULL CHECK (start_minute >= 0),
    end_minute   INTEGER NOT NULL CHECK (end_minute > start_minute),  -- 半开止（不含）
    PRIMARY KEY (berth_id, start_minute)
);

CREATE TABLE IF NOT EXISTS confirmed_trips (
    trip_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    revision        INTEGER NOT NULL,                       -- 确认时所据修订号（快照）
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    start_minute    INTEGER NOT NULL,
    window_minutes  INTEGER NOT NULL,
    draft           REAL NOT NULL,
    height          REAL NOT NULL,
    margin          REAL NOT NULL,
    arrival_minute  INTEGER NOT NULL,
    itinerary       TEXT NOT NULL,                          -- JSON 快照
    ship_no         TEXT,                                   -- 档案引用（可空）：确认时所用船号
    ship_version    INTEGER,                                -- 档案引用（可空）：确认时所用档案版本
    berth_id        TEXT,                                   -- 泊位引用（可空）：指定泊位预排时确认的终点泊位
    work_minutes    INTEGER,                                -- 作业分钟（可空）：正整数；与 berth_id 成对
    work_start_minute INTEGER,                              -- 作业区间起点（可空）：抵达即开工时等于 arrival_minute
    work_end_minute   INTEGER,                              -- 作业区间止点（可空，不含）：work_start + work
    confirmed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batch_confirmations (
    idempotency_key TEXT PRIMARY KEY,                      -- 批次幂等键
    revision        INTEGER NOT NULL,                       -- 批次所据修订号
    content_hash    TEXT NOT NULL,                          -- 提交内容（不含幂等键）的规范化哈希
    result          TEXT NOT NULL,                          -- 首次成功结果 JSON（各行程号等）
    confirmed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 行程作废记录：每条已确认行程至多一条（trip_id 唯一）。confirmed_trips 中的
-- 原确认快照仍作为历史快照保留可查；作废只使该行程按固化航时计算的航段及
-- 狭窄水道组占用从后续调度中失效（load_occupancy 排除已作废行程）。
-- 重复作废不覆盖：服务层命中既有记录直接返回首次原因/时间，唯一约束兜底并发。
CREATE TABLE IF NOT EXISTS voided_trips (
    trip_id     INTEGER PRIMARY KEY REFERENCES confirmed_trips(trip_id),
    reason      TEXT NOT NULL,                              -- 非空作废原因（剥离首尾空白后至少 1 字符）
    voided_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 航段分时航时管制：半开分钟区间 [start_minute, end_minute) 内进入航段的
-- 船舶按 travel_time 计航时；未命中任何区间沿用 segments.travel_time 基础航时。
-- 整组替换（同 seg_id 先全删再插），区间互不交叠由服务层保证。
CREATE TABLE IF NOT EXISTS segment_travel_controls (
    seg_id        INTEGER NOT NULL REFERENCES segments(seg_id),
    start_minute  INTEGER NOT NULL CHECK (start_minute >= 0),
    end_minute    INTEGER NOT NULL CHECK (end_minute > start_minute),  -- 半开区间止（不含）
    travel_time   INTEGER NOT NULL CHECK (travel_time > 0),            -- 管制通行分钟（正整数）
    PRIMARY KEY (seg_id, start_minute)
);

-- 狭窄水道组：组建成后不可修改（无更新/删除接口）；每条航段至多属于一个组。
-- 组占用在规划时按当前组成员关系自快照原占用展开（见 services/groups.py）。
CREATE TABLE IF NOT EXISTS channel_groups (
    group_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    note        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS channel_group_members (
    group_id   INTEGER NOT NULL REFERENCES channel_groups(group_id),
    seg_id     INTEGER NOT NULL UNIQUE REFERENCES segments(seg_id),  -- 航段不得重复归组
    position   INTEGER NOT NULL,                                    -- 提交集合中的次序（0 起）
    PRIMARY KEY (group_id, seg_id)
);

-- 通行费费率版本：财务按航段整组提交半开分钟费率区间，每次提交生成一个
-- 全局不可变新版本（版本号自增）。新版本继承前一版本的全部航段区间，
-- 仅整组替换被提交航段（changed_seg_id）的区间；空组表示该航段此后未覆盖
-- 分钟费率为 0。版本一经生成永不修改，账单按其版本号永久固化。
CREATE TABLE IF NOT EXISTS toll_rate_versions (
    version         INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_seg_id  INTEGER NOT NULL REFERENCES segments(seg_id),  -- 本次替换费率的航段
    note            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 各费率版本下的全局费率区间快照（全量物化：生成新版本时从上一版本复制
-- 全部既有航段区间，再删除/重写 changed_seg_id 的部分）。费率为非负整数
-- 分/分钟；同版本同航段的半开区间互不交叠（服务层保证），未覆盖分钟费率 0。
CREATE TABLE IF NOT EXISTS toll_rates (
    version       INTEGER NOT NULL REFERENCES toll_rate_versions(version),
    seg_id        INTEGER NOT NULL REFERENCES segments(seg_id),
    start_minute  INTEGER NOT NULL CHECK (start_minute >= 0),
    end_minute    INTEGER NOT NULL CHECK (end_minute > start_minute),  -- 半开区间止（不含）
    rate_cents_per_minute INTEGER NOT NULL CHECK (rate_cents_per_minute >= 0),  -- 非负整数分/分钟
    PRIMARY KEY (version, seg_id, start_minute)
);

-- 通行费账单：每个已确认行程至多一张（trip_id 唯一）。账单在单个
-- BEGIN IMMEDIATE 事务内按当时最新费率版本与确认快照各段进入分钟固化，
-- 之后费率版本再如何变化都不改写旧账单。segments 为逐段计费结果 JSON：
-- 金额 = 该段固化航时 travel_time × 进入分钟(start_minute)命中区间费率，
-- 未覆盖分钟费率 0；total_cents 为各段金额之和（非负整数分）。
CREATE TABLE IF NOT EXISTS toll_bills (
    trip_id       INTEGER PRIMARY KEY REFERENCES confirmed_trips(trip_id),
    version       INTEGER NOT NULL REFERENCES toll_rate_versions(version),  -- 开单时固化的费率版本
    total_cents   INTEGER NOT NULL CHECK (total_cents >= 0),
    segments      TEXT NOT NULL,                                           -- 逐段计费结果 JSON
    billed_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 改期确认幂等记录：每个非空改期幂等键至多一条。原行程作废与新行程确认
-- 在同一 BEGIN IMMEDIATE 事务内完成，本记录在该事务末尾写入，保存首次
-- 结果（新旧行程号、复算行程、作废与冲销状态）。同键同内容重试（即使此后
-- 修订/占用/档案已变化、原行程已被本次改期作废）直接重放首次结果；同键
-- 异内容冲突。原确认快照与批次幂等记录均不被本流程改写。
CREATE TABLE IF NOT EXISTS trip_reschedules (
    idempotency_key TEXT PRIMARY KEY,                      -- 改期幂等键（非空）
    old_trip_id     INTEGER NOT NULL REFERENCES confirmed_trips(trip_id),
    target_revision INTEGER NOT NULL,                      -- 改期所据目标修订号
    content_hash    TEXT NOT NULL,                         -- 提交内容（不含幂等键）的规范化哈希
    result          TEXT NOT NULL,                         -- 首次成功结果 JSON（新行程号/复算行程/冲销状态等）
    confirmed_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 冲销单：作废已开单行程时在原作废事务内生成，每个账单至多一张
-- （bill_trip_id 唯一兜底并发）。等额冲销——逐段金额与总额取账单原值的
-- 相反数（账单为正、冲销为负，绝对值完全相等），费率版本引用沿用账单
-- 版本（旧账单不随费率变化，冲销同样固化）。重复作废不重复冲销。
CREATE TABLE IF NOT EXISTS toll_reversals (
    bill_trip_id  INTEGER PRIMARY KEY REFERENCES toll_bills(trip_id),
    version       INTEGER NOT NULL REFERENCES toll_rate_versions(version),
    total_cents   INTEGER NOT NULL CHECK (total_cents <= 0),               -- 非正整数分（与账单等额相反）
    segments      TEXT NOT NULL,                                           -- 逐段冲销结果 JSON（金额取反）
    reversed_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn():
    """请求级连接，业务代码自行控制事务（isolation_level=None）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(conn: sqlite3.Connection) -> None:
    """建表；空库且 SEED_DEMO 时写入演示数据。幂等。"""
    conn.executescript(SCHEMA)
    _migrate(conn)
    if config.SEED_DEMO and conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0:
        _seed_demo(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """旧版数据库平滑升级（全部 ALTER 在单个事务内完成：升级中断则整体
    回滚，重启后重跑本函数即可，不会留下半迁移状态；各步幂等可重入）：

    - 为 tide_values 补 error 列（既有行误差取 0）；
    - 为 tide_revisions 补 base_revision 列（既有修订为 NULL，即物化修订，
      历史逐分钟数据继续由 tide_values 原样提供；此后新修订按差量链式存储，
      见 services/tide.py；tide_deltas 表随上方 SCHEMA 幂等创建）；
    - 为 confirmed_trips 补 ship_no / ship_version 档案引用列（既有行程为
      NULL，表示直接提交参数确认、无档案引用）与 berth_id / work_minutes /
      work_start_minute / work_end_minute 泊位作业快照列（既有行程为 NULL，
      表示未指定泊位的旧确认，其请求与响应保持原样）。"""
    tide_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tide_values)").fetchall()}
    rev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(tide_revisions)").fetchall()}
    trip_cols = {r["name"] for r in conn.execute("PRAGMA table_info(confirmed_trips)").fetchall()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "error" not in tide_cols:
            conn.execute("ALTER TABLE tide_values ADD COLUMN error REAL NOT NULL DEFAULT 0.0")
        if "base_revision" not in rev_cols:
            conn.execute(
                "ALTER TABLE tide_revisions"
                " ADD COLUMN base_revision INTEGER REFERENCES tide_revisions(revision)"
            )
        if "ship_no" not in trip_cols:
            conn.execute("ALTER TABLE confirmed_trips ADD COLUMN ship_no TEXT")
        if "ship_version" not in trip_cols:
            conn.execute("ALTER TABLE confirmed_trips ADD COLUMN ship_version INTEGER")
        if "berth_id" not in trip_cols:
            conn.execute("ALTER TABLE confirmed_trips ADD COLUMN berth_id TEXT")
        if "work_minutes" not in trip_cols:
            conn.execute("ALTER TABLE confirmed_trips ADD COLUMN work_minutes INTEGER")
        if "work_start_minute" not in trip_cols:
            conn.execute("ALTER TABLE confirmed_trips ADD COLUMN work_start_minute INTEGER")
        if "work_end_minute" not in trip_cols:
            conn.execute("ALTER TABLE confirmed_trips ADD COLUMN work_end_minute INTEGER")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# 演示数据：4 个节点、3 条单向航段（其中一条带桥），修订 #1 含 0..1439 潮位
# ---------------------------------------------------------------------------

def _seed_demo(conn: sqlite3.Connection) -> None:
    import math

    cur = conn.execute("BEGIN IMMEDIATE")
    try:
        nodes = [
            ("A", "外锚地"),
            ("B", "内河节点B"),
            ("C", "桥区节点C"),
            ("D", "内港码头"),
        ]
        conn.executemany("INSERT INTO nodes(node_id, name) VALUES (?, ?)", nodes)

        # A->B：深水直航；B->C：带桥；B->D 与 C->D 构成可比较路径
        segments = [
            # origin, dest, 航时(分钟), 静水深度(m), 桥底净空(m)
            ("A", "B", 30, 6.0, None),
            ("B", "C", 40, 4.0, 8.0),
            ("C", "D", 20, 5.0, None),
            ("B", "D", 90, 3.5, None),
        ]
        conn.executemany(
            "INSERT INTO segments(origin, destination, travel_time, depth, clearance)"
            " VALUES (?, ?, ?, ?, ?)",
            segments,
        )

        rev = conn.execute(
            "INSERT INTO tide_revisions(note) VALUES (?)",
            ("初始潮位修订：周期 720 分钟正弦潮，振幅 1.5m，均值 2.0m",),
        ).lastrowid

        rows = [(rev, m, 2.0 + 1.5 * math.sin(2 * math.pi * m / 720.0)) for m in range(1440)]
        conn.executemany(
            "INSERT INTO tide_values(revision, minute, level) VALUES (?, ?, ?)", rows
        )

        # 通行费费率初始版本：A->B(seg 1) 分时段费率；其余航段未覆盖分钟费率为 0。
        # 财务可随后通过 PUT /api/segments/{seg_id}/toll-rates 整组替换生成新版本。
        conn.execute(
            "INSERT INTO toll_rate_versions(changed_seg_id, note)"
            " VALUES (1, '初始演示费率版本：A->B 航段分时段费率（分/分钟）')"
        )
        conn.executemany(
            "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute,"
            " rate_cents_per_minute) VALUES (1, 1, ?, ?, ?)",
            [(0, 360, 2), (360, 720, 1), (720, 1440, 2)],
        )

        # 泊位演示数据：为内港码头 D 登记一个全天开放泊位 D1；调度员可通过
        # POST /api/nodes/{node_id}/berths 为其他节点登记，并用
        # PUT /api/berths/{berth_id}/calendar 整组替换开放日历。
        conn.execute(
            "INSERT INTO berths(berth_id, node_id, note)"
            " VALUES ('D1', 'D', '内港码头 1 号泊位（全天开放）')"
        )
        conn.execute(
            "INSERT INTO berth_open_intervals(berth_id, start_minute, end_minute)"
            " VALUES ('D1', 0, 1440)"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
