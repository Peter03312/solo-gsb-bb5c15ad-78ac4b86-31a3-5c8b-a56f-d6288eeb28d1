"""潮位修订差量链式存储（本轮）测试。

- 每次修订的持久化写入量只随本次改动分钟数增长（tide_deltas 行数 == 改动
  分钟数，不再向 tide_values 复制基础修订全部分钟）；
- 任意历史修订的分钟范围查询沿基础链还原完整名义潮位与误差；
- 允许从非最新修订分叉：未改分钟继承指定基础修订；更正覆盖已有分钟时误差
  沿用基础修订、新分钟误差为 0；误差修订只能修改已有潮位的分钟；
- 旧库升级后修订号、历史值与已确认行程快照保持可用；迁移幂等可重入，
  中断重启不留半迁移状态；
- 规划/确认/批次/改期复核按同一修订读取一致数据；缺失分钟与未知修订沿用
  原错误；并发修订串行生效。
"""
import sqlite3
import threading

from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _get_range(client, rev, lo, hi):
    r = client.get(f"/api/tide/revisions/{rev}",
                   params={"from_minute": lo, "to_minute": hi})
    assert r.status_code == 200, r.text
    g = r.json()
    levels = {int(k): v for k, v in g["levels"].items()}
    errors = {int(k): v for k, v in g["errors"].items()}
    return levels, errors


def _delta_rows(rev):
    conn = connect()
    try:
        return conn.execute(
            "SELECT minute, level, error FROM tide_deltas WHERE revision = ?"
            " ORDER BY minute",
            (rev,),
        ).fetchall()
    finally:
        conn.close()


def _materialized_rows(rev):
    conn = connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM tide_values WHERE revision = ?",
            (rev,),
        ).fetchone()["c"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 1. 写入量随本次改动分钟数增长（不再复制基础修订全部分钟）
# ---------------------------------------------------------------------------

def test_correction_persists_only_changed_minutes(client):
    rev1 = seed_initial_revision(_level(0, 1439, 3.0))   # 1440 分钟物化基础

    r = client.post("/api/tide/corrections",
                    json={"corrections": {3: 1.5, 700: 2.5}})
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]

    # 只写 2 行差量；tide_values 中没有新修订的全量复制行
    rows = _delta_rows(rev2)
    assert [(row["minute"], row["level"], row["error"]) for row in rows] == \
        [(3, 1.5, 0.0), (700, 2.5, 0.0)]
    assert _materialized_rows(rev2) == 0
    assert _materialized_rows(rev1) == 1440   # 基础修订原样保留

    # 分钟范围查询仍还原完整数据
    levels, errors = _get_range(client, rev2, 0, 1439)
    assert len(levels) == 1440
    assert levels[3] == 1.5 and levels[700] == 2.5
    assert levels[0] == 3.0 and levels[1439] == 3.0
    assert set(errors.values()) == {0.0}


def test_error_revision_persists_only_changed_minutes(client):
    rev1 = seed_initial_revision(_level(0, 1439, 3.0))

    r = client.post("/api/tide/error-revisions",
                    json={"errors": {1: 0.1, 2: 0.2, 3: 0.3}})
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]

    rows = _delta_rows(rev2)
    assert [(row["minute"], row["level"], row["error"]) for row in rows] == \
        [(1, 3.0, 0.1), (2, 3.0, 0.2), (3, 3.0, 0.3)]
    assert _materialized_rows(rev2) == 0


def test_repeated_small_revisions_write_volume_stays_small(client):
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    # 连续 10 次更正、每次只改 1 分钟：总差量行数 == 10（而非 10×1440）
    for i in range(10):
        r = client.post("/api/tide/corrections",
                        json={"corrections": {i: 1.0 + i * 0.1}})
        assert r.status_code == 201, r.text
        rev = r.json()["revision"]
    conn = connect()
    try:
        total = conn.execute("SELECT COUNT(*) AS c FROM tide_deltas").fetchone()["c"]
        copied = conn.execute(
            "SELECT COUNT(*) AS c FROM tide_values WHERE revision > 1"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert total == 10
    assert copied == 0
    # 最新修订还原：前 10 分钟被逐次更正，其余继承
    levels, _ = _get_range(client, rev, 0, 1439)
    assert len(levels) == 1440
    for i in range(10):
        assert abs(levels[i] - (1.0 + i * 0.1)) < 1e-9
    assert levels[10] == 3.0


# ---------------------------------------------------------------------------
# 2. 任意历史修订的分钟范围查询还原完整名义潮位与误差
# ---------------------------------------------------------------------------

def test_chain_reconstructs_every_historical_revision(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {5: 1.0}}).json()["revision"]
    rev3 = client.post("/api/tide/error-revisions",
                       json={"errors": {5: 0.4, 6: 0.2},
                             "base_revision": rev2}).json()["revision"]
    rev4 = client.post("/api/tide/corrections",
                       json={"corrections": {7: 9.0},
                             "base_revision": rev3}).json()["revision"]

    def expected(level_over=None, error_over=None):
        levels = _level(0, 19, 3.0)
        errors = _level(0, 19, 0.0)
        levels.update(level_over or {})
        errors.update(error_over or {})
        return levels, errors

    for rev, lo, eo in [
        (rev1, {}, {}),
        (rev2, {5: 1.0}, {}),
        (rev3, {5: 1.0}, {5: 0.4, 6: 0.2}),
        (rev4, {5: 1.0, 7: 9.0}, {5: 0.4, 6: 0.2}),
    ]:
        exp_levels, exp_errors = expected(lo, eo)
        levels, errors = _get_range(client, rev, 0, 19)
        assert levels == exp_levels, f"revision {rev} levels"
        assert errors == exp_errors, f"revision {rev} errors"

    # 子区间查询同样还原（不依赖整段读取）
    levels, errors = _get_range(client, rev4, 5, 7)
    assert levels == {5: 1.0, 6: 3.0, 7: 9.0}
    assert errors == {5: 0.4, 6: 0.2, 7: 0.0}


def test_revision_list_and_unknown_revision_unchanged(client):
    rev1 = seed_initial_revision(_level(0, 9, 3.0))
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {0: 1.0}}).json()["revision"]
    revs = client.get("/api/tide/revisions").json()
    assert [x["revision"] for x in revs] == [rev1, rev2]

    r = client.get("/api/tide/revisions/4242")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 3. 允许从非最新修订分叉；更正误差继承 / 新分钟误差 0；误差修订只改已有分钟
# ---------------------------------------------------------------------------

def test_fork_from_non_latest_revision(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {5: 9.9}}).json()["revision"]
    # 从 rev1（非最新）分叉：未改分钟继承 rev1，而非 rev2
    rev3 = client.post("/api/tide/corrections",
                       json={"corrections": {7: 1.0},
                             "base_revision": rev1}).json()["revision"]

    levels3, _ = _get_range(client, rev3, 0, 19)
    assert levels3[5] == 3.0     # 不继承 rev2 的更正
    assert levels3[7] == 1.0
    # rev2 不可变
    levels2, _ = _get_range(client, rev2, 0, 19)
    assert levels2[5] == 9.9 and levels2[7] == 3.0

    # 误差修订同样可从非最新修订分叉
    rev4 = client.post("/api/tide/error-revisions",
                       json={"errors": {5: 0.8},
                             "base_revision": rev1}).json()["revision"]
    levels4, errors4 = _get_range(client, rev4, 0, 19)
    assert levels4[5] == 3.0 and errors4[5] == 0.8
    assert errors4[7] == 0.0


def test_correction_inherits_error_for_existing_minute_zero_for_new(client):
    rev1 = seed_initial_revision(_level(0, 9, 3.0))
    rev2 = client.post("/api/tide/error-revisions",
                       json={"errors": {3: 0.5}}).json()["revision"]
    # 第 3 分钟已有潮位（更正后误差沿用 0.5）；第 100 分钟为新分钟（误差 0）
    rev3 = client.post("/api/tide/corrections",
                       json={"corrections": {3: 2.0, 100: 4.0},
                             "base_revision": rev2}).json()["revision"]
    levels, errors = _get_range(client, rev3, 0, 100)
    assert levels[3] == 2.0 and errors[3] == 0.5
    assert levels[100] == 4.0 and errors[100] == 0.0
    assert levels[4] == 3.0 and errors[4] == 0.0
    # 差量行内即最终值：已有分钟存继承误差，新分钟存 0
    rows = {row["minute"]: (row["level"], row["error"])
            for row in _delta_rows(rev3)}
    assert rows == {3: (2.0, 0.5), 100: (4.0, 0.0)}


def test_error_revision_only_existing_minutes_through_chain(client):
    rev1 = seed_initial_revision(_level(0, 9, 3.0))
    # 更正引入新分钟 100；误差修订沿链可改第 100 分钟
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {100: 5.0}}).json()["revision"]
    r = client.post("/api/tide/error-revisions",
                    json={"errors": {100: 0.3}, "base_revision": rev2})
    assert r.status_code == 201, r.text
    rev3 = r.json()["revision"]
    levels, errors = _get_range(client, rev3, 100, 100)
    assert levels[100] == 5.0 and errors[100] == 0.3

    # 第 101 分钟沿链仍无名义潮位 -> MISSING_TIDE_DATA，不产生新修订
    r = client.post("/api/tide/error-revisions",
                    json={"errors": {101: 0.3}, "base_revision": rev3})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["missing_minutes"] == [101]
    assert err["details"]["total_missing"] == 1
    assert err["details"]["base_revision"] == rev3
    revs = [x["revision"] for x in client.get("/api/tide/revisions").json()]
    assert revs == [rev1, rev2, rev3]


# ---------------------------------------------------------------------------
# 4. 规划 / 确认 / 批次 / 复核按同一修订读取一致数据；缺失分钟沿用原错误
# ---------------------------------------------------------------------------

def test_plan_confirm_review_on_delta_chain(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    # 差量误差修订：第 0 分钟有效潮位 3.0-0.6=2.4 < 2.5，需等待
    rev2 = client.post("/api/tide/error-revisions",
                       json={"errors": {0: 0.6}}).json()["revision"]
    planned = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev2,
    }).json()
    assert planned["itinerary"][0]["start_minute"] == 1
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 201, r.text
    trip = r.json()

    # 差量更正链（基于差量修订再更正）：把 1..2 分钟改为低潮
    rev3 = client.post("/api/tide/corrections",
                       json={"corrections": {1: 0.0, 2: 0.0},
                             "base_revision": rev2}).json()["revision"]
    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={"target_revision": rev3}).json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["minute"] == 1
    assert v["tide_level"] == 0.0
    assert v["tide_error"] == 0.0          # 第 1 分钟误差沿链继承为 0
    assert v["available_depth"] == 4.0
    # 快照不改写
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["revision"] == rev2


def test_batch_plan_and_confirm_on_delta_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {0: 3.5}}).json()["revision"]
    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
    ]
    r = client.post("/api/trips/batch/plan",
                    json={"revision": rev2, "ships": ships})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["revision"] == rev2
    # 后船避开前船拟占用：进入分钟分别为 0 与 2
    assert [s["itinerary"][0]["start_minute"] for s in plan["ships"]] == [0, 2]

    body = {
        "revision": rev2,
        "idempotency_key": "delta-batch-1",
        "ships": [{**ships[i], "itinerary": plan["ships"][i]["itinerary"]}
                  for i in range(2)],
    }
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    assert [t["arrival_minute"] for t in r.json()["trips"]] == [2, 4]


def test_plan_missing_minutes_on_delta_chain_still_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 9, 3.0))      # 只到第 9 分钟
    # 差量更正只补第 100 分钟，10..19 仍缺
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {100: 4.0}}).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev2,
    })
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["missing_minutes"][0] == 10
    assert err["details"]["total_missing"] == 10


# ---------------------------------------------------------------------------
# 5. 并发修订串行生效
# ---------------------------------------------------------------------------

def test_concurrent_revisions_serialize(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    results, lock = [], threading.Lock()
    bar = threading.Barrier(2)

    def post(corrections):
        bar.wait()
        r = client.post("/api/tide/corrections", json={"corrections": corrections})
        with lock:
            results.append((r.status_code,
                            r.json().get("revision") if r.status_code == 201 else r.text))

    t1 = threading.Thread(target=post, args=({1: 1.0},))
    t2 = threading.Thread(target=post, args=({2: 2.0},))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 两次都成功，修订号连续（串行生效）
    assert sorted(code for code, _ in results) == [201, 201]
    revs = sorted(rev for _, rev in results)
    assert revs == [rev1 + 1, rev1 + 2]

    # 先生效的修订只含自己的改动；后生效的以先者为基，两处改动都可见
    levels_first, _ = _get_range(client, rev1 + 1, 0, 19)
    changed = {m for m in (1, 2) if levels_first[m] != 3.0}
    assert len(changed) == 1
    levels_second, _ = _get_range(client, rev1 + 2, 0, 19)
    assert levels_second[1] == 1.0 and levels_second[2] == 2.0

    # 每个修订各只持久化 1 行差量
    assert len(_delta_rows(rev1 + 1)) == 1
    assert len(_delta_rows(rev1 + 2)) == 1


# ---------------------------------------------------------------------------
# 6. 旧库升级：修订号 / 历史值 / 已确认快照保持可用；迁移幂等、可愈合中断
# ---------------------------------------------------------------------------

# 上一轮（全量复制时代）的库结构：tide_revisions 无 base_revision、无 tide_deltas，
# 每个修订在 tide_values 中存全部分钟
_LEGACY_SCHEMA = """
CREATE TABLE tide_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')), note TEXT);
CREATE TABLE tide_values (revision INTEGER NOT NULL, minute INTEGER NOT NULL,
 level REAL NOT NULL, error REAL NOT NULL DEFAULT 0.0,
 PRIMARY KEY (revision, minute));
CREATE TABLE nodes (node_id TEXT PRIMARY KEY, name TEXT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE segments (seg_id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT,
 destination TEXT, travel_time INTEGER, depth REAL, clearance REAL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')), CHECK(origin<>destination));
CREATE TABLE confirmed_trips (trip_id INTEGER PRIMARY KEY AUTOINCREMENT,
 revision INTEGER, origin TEXT, destination TEXT, start_minute INTEGER,
 window_minutes INTEGER, draft REAL, height REAL, margin REAL,
 arrival_minute INTEGER, itinerary TEXT,
 confirmed_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _build_legacy_db(db_file):
    """构造全量复制时代的旧库：rev1 全量 0..19；rev2 为 rev1 全量复制后
    改第 5 分钟潮位、第 6 分钟误差；另有一条按 rev2 确认的行程快照。"""
    conn = sqlite3.connect(db_file)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute("INSERT INTO nodes(node_id, name) VALUES ('A', '外锚地')")
    conn.execute("INSERT INTO nodes(node_id, name) VALUES ('B', '内港')")
    conn.execute("INSERT INTO segments(origin, destination, travel_time, depth)"
                 " VALUES ('A', 'B', 2, 4.0)")
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (1, '旧初始修订')")
    conn.executemany("INSERT INTO tide_values(revision, minute, level, error)"
                     " VALUES (1, ?, 3.0, 0.0)", [(m,) for m in range(20)])
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (2, '旧更正')")
    conn.executemany(
        "INSERT INTO tide_values(revision, minute, level, error) VALUES (2, ?, ?, ?)",
        [(m, 1.0 if m == 5 else 3.0, 0.7 if m == 6 else 0.0) for m in range(20)],
    )
    conn.execute(
        "INSERT INTO confirmed_trips(revision, origin, destination, start_minute,"
        " window_minutes, draft, height, margin, arrival_minute, itinerary)"
        " VALUES (2, 'A', 'B', 0, 20, 1.0, 0.0, 0.0, 2,"
        " '[{\"seg_id\": 1, \"origin\": \"A\", \"destination\": \"B\","
        " \"start_minute\": 0, \"end_minute\": 2, \"travel_time\": 2}]')"
    )
    conn.commit()
    conn.close()


def _open_upgraded_client(db_file, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "SEED_DEMO", False)
    return TestClient(app)


def test_legacy_db_upgrade_preserves_revisions_values_and_trips(
        tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    _build_legacy_db(db_file)

    with _open_upgraded_client(db_file, monkeypatch) as client:
        # 修订号与历史值保持可用（物化修订直接读 tide_values）
        assert [x["revision"] for x in client.get("/api/tide/revisions").json()] == [1, 2]
        levels1, errors1 = _get_range(client, 1, 0, 19)
        assert levels1 == _level(0, 19, 3.0) and set(errors1.values()) == {0.0}
        levels2, errors2 = _get_range(client, 2, 0, 19)
        assert levels2[5] == 1.0 and levels2[6] == 3.0
        assert errors2[6] == 0.7 and errors2[5] == 0.0

        # 已确认行程快照保持可用
        trip = client.get("/api/trips/1").json()
        assert trip["revision"] == 2
        assert trip["itinerary"][0]["seg_id"] == 1
        assert trip["arrival_minute"] == 2

        # 升级后新修订按差量落库，且基于旧物化修订正确还原
        r = client.post("/api/tide/corrections",
                        json={"corrections": {5: 2.0}, "base_revision": 2})
        assert r.status_code == 201, r.text
        rev3 = r.json()["revision"]
        assert rev3 == 3
        assert [(row["minute"], row["level"], row["error"])
                for row in _delta_rows(rev3)] == [(5, 2.0, 0.0)]
        assert _materialized_rows(rev3) == 0
        levels3, errors3 = _get_range(client, rev3, 0, 19)
        assert levels3[5] == 2.0 and errors3[6] == 0.7
        # 旧修订不可变
        levels2b, _ = _get_range(client, 2, 0, 19)
        assert levels2b[5] == 1.0

        # 升级后的库上复核历史行程（目标修订为差量修订）照常工作
        data = client.post("/api/trips/1/review",
                           json={"target_revision": rev3}).json()
        assert data["target_revision"] == rev3
        assert data["snapshot_revision"] == 2


def test_migration_idempotent_and_heals_interrupted_states(tmp_path, monkeypatch):
    from app import config

    # 状态一：升级在 SCHEMA 之后、列迁移之前中断（tide_deltas 已建，
    # base_revision 缺失）——重跑 init_db 必须愈合并保留历史值
    db1 = tmp_path / "partial1.db"
    _build_legacy_db(db1)
    conn = sqlite3.connect(db1)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS tide_deltas ("
        " revision INTEGER NOT NULL REFERENCES tide_revisions(revision),"
        " minute INTEGER NOT NULL, level REAL NOT NULL,"
        " error REAL NOT NULL DEFAULT 0.0, PRIMARY KEY (revision, minute));"
    )
    conn.commit(); conn.close()

    monkeypatch.setattr(config, "DB_PATH", str(db1))
    monkeypatch.setattr(config, "SEED_DEMO", False)
    conn = connect()
    init_db(conn)
    init_db(conn)   # 幂等：重复执行不报错
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tide_revisions)")}
    assert "base_revision" in cols
    rows = conn.execute(
        "SELECT minute, level, error FROM tide_values WHERE revision = 2"
        " ORDER BY minute").fetchall()
    assert [(r["minute"], r["level"], r["error"]) for r in rows][5] == (5, 1.0, 0.0)
    conn.close()

    # 状态二：升级在列迁移之后、建差量表之前中断（base_revision 已存在，
    # tide_deltas 缺失）——重跑 init_db 必须补表且不丢数据
    db2 = tmp_path / "partial2.db"
    _build_legacy_db(db2)
    conn = sqlite3.connect(db2)
    conn.execute("ALTER TABLE tide_revisions ADD COLUMN base_revision INTEGER"
                 " REFERENCES tide_revisions(revision)")
    conn.commit(); conn.close()

    monkeypatch.setattr(config, "DB_PATH", str(db2))
    conn = connect()
    init_db(conn)
    init_db(conn)
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "tide_deltas" in tables
    n = conn.execute("SELECT COUNT(*) AS c FROM tide_values").fetchone()["c"]
    assert n == 40   # 两个旧修订的全量历史行原样保留
    conn.close()

    # 状态三（全新升级）在 test_legacy_db_upgrade_* 中覆盖；此处再验证
    # 愈合后的库可继续接受差量修订
    with _open_upgraded_client(db2, monkeypatch) as client:
        r = client.post("/api/tide/error-revisions", json={"errors": {6: 0.9}})
        assert r.status_code == 201, r.text
        rev3 = r.json()["revision"]
        levels, errors = _get_range(client, rev3, 0, 19)
        assert levels[5] == 1.0 and errors[6] == 0.9
