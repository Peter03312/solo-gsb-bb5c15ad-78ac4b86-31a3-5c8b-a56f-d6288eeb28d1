"""潮位修订增量存储（写入量随改动分钟数增长）、非最新修订分叉、
祖先链还原与旧库原子升级测试。"""
import threading

from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _get_range(client, rev, lo=0, hi=19):
    g = client.get(f"/api/tide/revisions/{rev}",
                   params={"from_minute": lo, "to_minute": hi}).json()
    levels = {int(k): v for k, v in g["levels"].items()}
    errors = {int(k): v for k, v in g["errors"].items()}
    return levels, errors


def _correction(client, corrections, base=None, note=None):
    body = {"corrections": corrections}
    if base is not None:
        body["base_revision"] = base
    if note is not None:
        body["note"] = note
    return client.post("/api/tide/corrections", json=body)


def _errors(client, errors, base=None, note=None):
    body = {"errors": errors}
    if base is not None:
        body["base_revision"] = base
    if note is not None:
        body["note"] = note
    return client.post("/api/tide/error-revisions", json=body)


# ---------------------------------------------------------------------------
# 1. 写入量只随本次改动分钟数增长：不复制基础修订的任何分钟
# ---------------------------------------------------------------------------

def test_correction_writes_only_changed_minutes(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = _correction(client, {3: 1.0, 7: 2.0})
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]
    assert r.json()["base_revision"] == rev1
    assert r.json()["kind"] == "correction"

    conn = connect()
    try:
        # 新修订只有 2 行增量；tide_values 中没有新修订的任何全量复制行
        n_delta = conn.execute(
            "SELECT COUNT(*) FROM tide_deltas WHERE revision = ?", (rev2,)
        ).fetchone()[0]
        n_full = conn.execute(
            "SELECT COUNT(*) FROM tide_values WHERE revision = ?", (rev2,)
        ).fetchone()[0]
        assert n_delta == 2
        assert n_full == 0
        # 基础修订的 20 行全量数据原样保留、不被改写
        assert conn.execute(
            "SELECT COUNT(*) FROM tide_values WHERE revision = ?", (rev1,)
        ).fetchone()[0] == 20
    finally:
        conn.close()


def test_error_revision_writes_only_changed_minutes(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = _errors(client, {1: 0.2, 2: 0.3, 3: 0.4})
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]
    assert r.json()["base_revision"] == rev1
    assert r.json()["kind"] == "error"
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM tide_deltas WHERE revision = ?", (rev2,)
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM tide_values WHERE revision = ?", (rev2,)
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_long_chain_total_writes_grow_linearly(client):
    """100 个修订各改 1 分钟：增量总行数 = 100，与基修订分钟规模无关。"""
    seed_initial_revision(_level(0, 1439, 3.0))
    for m in range(100):
        r = _correction(client, {m: 4.0})
        assert r.status_code == 201, r.text
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) FROM tide_deltas").fetchone()[0]
        # 仅 100 行增量，而不是 100 * 1440
        assert n == 100
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 2. 分钟范围查询对任意历史修订还原完整名义潮位与误差
# ---------------------------------------------------------------------------

def test_range_query_reconstructs_full_values(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = _errors(client, {3: 0.5}).json()["revision"]
    rev3 = _correction(client, {5: 1.0}).json()["revision"]
    rev4 = _errors(client, {5: 0.25, 7: 0.9}).json()["revision"]

    # 历史修订 rev2：误差继承与覆盖并存，范围查询返回全部 20 分钟
    levels2, errors2 = _get_range(client, rev2)
    assert len(levels2) == 20 and len(errors2) == 20
    assert errors2[3] == 0.5 and errors2[4] == 0.0
    # rev3：名义更正第 5 分钟，误差继承 rev2（第 3 分钟 0.5 仍在）
    levels3, errors3 = _get_range(client, rev3)
    assert levels3[5] == 1.0 and levels3[3] == 3.0
    assert errors3[5] == 0.0 and errors3[3] == 0.5
    # rev4：第 5 分钟名义保持 1.0、误差 0.25；第 7 分钟误差 0.9；
    # 第 3 分钟误差仍继承 0.5
    levels4, errors4 = _get_range(client, rev4)
    assert levels4[5] == 1.0 and levels4[7] == 3.0
    assert errors4[5] == 0.25 and errors4[7] == 0.9 and errors4[3] == 0.5

    # 最老的基修订保持不可变
    levels1, errors1 = _get_range(client, rev1)
    assert all(v == 3.0 for v in levels1.values())
    assert all(v == 0.0 for v in errors1.values())


def test_range_query_partial_window(client):
    seed_initial_revision(_level(0, 19, 3.0))
    rev2 = _correction(client, {2: 1.0, 10: 2.0}).json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 2, "to_minute": 5}).json()
    levels = {int(k): v for k, v in g["levels"].items()}
    assert set(levels) == {2, 3, 4, 5}
    assert levels[2] == 1.0 and levels[3] == 3.0


def test_sparse_range_only_returns_known_minutes(client):
    """稀疏修订：范围内基修订没有的分钟不返回（规划/复核仍报缺潮位）。"""
    rev1 = seed_initial_revision(_level(0, 9, 3.0))
    rev2 = _correction(client, {3: 1.0}).json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    assert len(g["levels"]) == 10
    assert g["levels"]["3"] == 1.0


# ---------------------------------------------------------------------------
# 3. 允许从非最新修订分叉
# ---------------------------------------------------------------------------

def test_fork_from_non_latest_revision(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    # rev2：主线上的名义更正 + rev3 误差修订
    rev2 = _correction(client, {1: 1.0}, note="主线更正").json()["revision"]
    rev3 = _errors(client, {1: 0.8}).json()["revision"]
    # 从非最新的 rev1 分叉：rev4 不应看到 rev2/rev3 的任何改动
    r = _correction(client, {2: 2.5}, base=rev1, note="从 rev1 分叉")
    assert r.status_code == 201, r.text
    body = r.json()
    rev4 = body["revision"]
    assert body["base_revision"] == rev1
    assert rev4 > rev3  # 修订号仍是全局自增

    levels, errors = _get_range(client, rev4)
    assert levels[2] == 2.5
    assert levels[1] == 3.0          # rev2 对第 1 分钟的更正不可见
    assert errors[1] == 0.0          # rev3 对第 1 分钟的误差不可见
    assert errors[2] == 0.0          # 新分钟（rev1 已有该分钟 -> 继承 0）

    # 主线修订不受分叉影响
    levels3, errors3 = _get_range(client, rev3)
    assert levels3[1] == 1.0 and errors3[1] == 0.8
    assert levels3[2] == 3.0


def test_fork_correction_inherits_error_from_fork_base(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = _errors(client, {5: 0.4}).json()["revision"]
    rev3 = _correction(client, {6: 9.0}).json()["revision"]  # 主线另起改动
    # 从 rev2 分叉更正第 5 分钟：误差必须继承 rev2 的 0.4
    rev4 = _correction(client, {5: 2.0}, base=rev2).json()["revision"]
    _, errors4 = _get_range(client, rev4)
    assert errors4[5] == 0.4
    # 第 6 分钟的主线改动对分叉不可见
    levels4, _ = _get_range(client, rev4)
    assert levels4[6] == 3.0


def test_fork_error_revision_checks_base_chain(client):
    rev1 = seed_initial_revision(_level(0, 9, 3.0))           # 只含 0..9
    rev2 = _correction(client, {15: 1.0}).json()["revision"]  # 主线新增第 15 分钟
    # 从 rev1 分叉做误差修订：第 15 分钟在 rev1 链上不存在 -> MISSING_TIDE_DATA
    r = _errors(client, {15: 0.1}, base=rev1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MISSING_TIDE_DATA"
    # 基于 rev2（链上有第 15 分钟）则允许
    r2 = _errors(client, {15: 0.1}, base=rev2)
    assert r2.status_code == 201, r2.text
    levels, errors = _get_range(client, r2.json()["revision"], 0, 19)
    assert levels[15] == 1.0 and errors[15] == 0.1


def test_fork_unknown_base_still_rejected(client):
    seed_initial_revision(_level(0, 9, 3.0))
    r = _correction(client, {0: 1.0}, base=999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"
    r = _errors(client, {0: 0.1}, base=999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"


def test_correction_new_minute_error_zero(client):
    rev1 = seed_initial_revision(_level(0, 9, 3.0))  # 基修订只到 9
    # 更正引入基修订没有的第 20 分钟：新分钟误差为 0
    rev2 = _correction(client, {20: 5.0}).json()["revision"]
    levels, errors = _get_range(client, rev2, 0, 20)
    assert levels[20] == 5.0 and errors[20] == 0.0
    assert 10 not in levels  # 未引入也未继承的分钟仍缺失
    # 之后可对该分钟做误差修订（此时链上已存在）
    rev3 = _errors(client, {20: 0.3}, base=rev2).json()["revision"]
    _, errors3 = _get_range(client, rev3, 20, 20)
    assert errors3[20] == 0.3


# ---------------------------------------------------------------------------
# 4. 规划 / 复核按分叉修订读取一致数据
# ---------------------------------------------------------------------------

def test_plan_uses_forked_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision({0: 2.6, **_level(1, 19, 3.0)})
    # 主线：给第 0 分钟加误差使第 0 分钟不可行
    _errors(client, {0: 0.5})
    # 从 rev1 分叉：第 0 分钟误差不可见，仍可于 0 出发
    rev_fork = _correction(client, {5: 1.0}, base=rev1).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev_fork,
    })
    assert r.status_code == 200, r.text
    assert r.json()["itinerary"][0]["start_minute"] == 0


def test_review_against_forked_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    # 确认一个行程（最新修订 rev1）
    planned = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev1,
    }).json()
    trip = client.post("/api/trips/confirm", json=planned).json()
    # 主线上的误差修订使原行程不安全
    _errors(client, {0: 0.9, 1: 0.9})
    # 从 rev1 分叉的更正不含该误差 -> 原行程仍安全
    rev_fork = _correction(client, {10: 1.0}, base=rev1).json()["revision"]
    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    json={"target_revision": rev_fork})
    assert r.status_code == 200, r.text
    assert r.json()["safe"] is True


# ---------------------------------------------------------------------------
# 5. 并发修订串行生效（BEGIN IMMEDIATE 写锁）
# ---------------------------------------------------------------------------

def test_concurrent_corrections_serialize(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    results = []
    errors_box = []

    def worker(m):
        try:
            r = _correction(client, {m: 4.0}, note=f"并发更正 {m}")
            results.append((m, r.status_code, r.json()))
        except BaseException as exc:  # noqa: BLE001
            errors_box.append(exc)

    threads = [threading.Thread(target=worker, args=(m,)) for m in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors_box, errors_box
    assert len(results) == 8
    assert all(code == 201 for _, code, _ in results)
    new_revs = [body["revision"] for _, _, body in results]
    assert len(set(new_revs)) == 8  # 修订号不重号
    # 串行链最终还原包含全部改动
    latest = max(new_revs)
    levels, _ = _get_range(client, latest, 0, 19)
    for m in range(8):
        assert levels[m] == 4.0
    # 基修订不变
    base_levels, _ = _get_range(client, rev1, 0, 19)
    assert all(v == 3.0 for v in base_levels.values())


# ---------------------------------------------------------------------------
# 6. 旧库升级：修订号、历史值、已确认快照可用；重复/中断式启动不留半成品
# ---------------------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE tide_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')), note TEXT);
CREATE TABLE tide_values (revision INTEGER, minute INTEGER, level REAL,
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
    import sqlite3
    conn = sqlite3.connect(db_file)
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (1, '旧版修订')")
    conn.executemany("INSERT INTO tide_values VALUES (1, ?, ?)",
                     [(m, 2.0) for m in range(20)])
    conn.execute("INSERT INTO nodes(node_id) VALUES ('A')")
    conn.execute("INSERT INTO nodes(node_id) VALUES ('B')")
    conn.execute(
        "INSERT INTO segments(origin, destination, travel_time, depth, clearance)"
        " VALUES ('A', 'B', 2, 99.0, NULL)"
    )
    conn.execute(
        "INSERT INTO confirmed_trips(revision, origin, destination, start_minute,"
        " window_minutes, draft, height, margin, arrival_minute, itinerary)"
        " VALUES (1, 'A', 'B', 0, 20, 1.0, 0.0, 0.0, 2, ?)",
        ('[{"seg_id": 1, "origin": "A", "destination": "B", "start_minute": 0,'
         ' "end_minute": 2, "travel_time": 2}]',),
    )
    conn.commit()
    conn.close()


def test_legacy_upgrade_preserves_revisions_and_snapshot(tmp_path, monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    from app.main import app

    db_file = tmp_path / "legacy.db"
    _build_legacy_db(db_file)
    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "SEED_DEMO", False)

    conn = connect()
    init_db(conn)
    # 修订号、历史值保持
    row = conn.execute(
        "SELECT base_revision, kind FROM tide_revisions WHERE revision = 1"
    ).fetchone()
    assert row["base_revision"] is None and row["kind"] == "root"
    vals = conn.execute(
        "SELECT minute, level, error FROM tide_values WHERE revision = 1 ORDER BY minute"
    ).fetchall()
    assert [(r["minute"], r["level"], r["error"]) for r in vals] == \
        [(m, 2.0, 0.0) for m in range(20)]
    # 已确认快照仍可查
    assert conn.execute(
        "SELECT trip_id, revision FROM confirmed_trips WHERE trip_id = 1"
    ).fetchone()["revision"] == 1
    conn.close()

    with TestClient(app) as c:
        # 修订列表 / 分钟范围读取历史值
        revs = c.get("/api/tide/revisions").json()
        assert [r["revision"] for r in revs] == [1]
        g = c.get("/api/tide/revisions/1",
                  params={"from_minute": 0, "to_minute": 19}).json()
        assert len(g["levels"]) == 20 and g["levels"]["0"] == 2.0
        assert all(v == 0.0 for v in g["errors"].values())
        # 已确认行程快照可用
        trip = c.get("/api/trips/1").json()
        assert trip["revision"] == 1
        # 旧修订作为基修订可继续分叉，增量写入并正确还原
        r = c.post("/api/tide/corrections", json={"corrections": {3: 4.0},
                                                  "base_revision": 1})
        assert r.status_code == 201, r.text
        rev2 = r.json()["revision"]
        assert rev2 == 2 and r.json()["base_revision"] == 1
        g2 = c.get("/api/tide/revisions/2",
                   params={"from_minute": 0, "to_minute": 19}).json()
        assert g2["levels"]["3"] == 4.0 and g2["levels"]["4"] == 2.0
        # 复核旧快照（同样安全）
        rv = c.post("/api/trips/1/review", json={"target_revision": rev2})
        assert rv.status_code == 200, rv.text
        assert rv.json()["safe"] is True


def test_legacy_upgrade_restartable_and_idempotent(tmp_path, monkeypatch):
    """模拟升级中断（只完成部分 ALTER 即退出）：重启 init_db 必须把剩余
    步骤补完，且再次启动不重复迁移、不破坏数据。"""
    import sqlite3

    db_file = tmp_path / "halfmigrated.db"
    _build_legacy_db(db_file)

    # 手工完成迁移的前两步（模拟在 kind 列与 tide_deltas 建立之前中断）
    pre = sqlite3.connect(db_file)
    pre.execute("ALTER TABLE tide_values ADD COLUMN error REAL NOT NULL DEFAULT 0.0")
    pre.execute("ALTER TABLE tide_revisions ADD COLUMN base_revision INTEGER")
    pre.commit()
    pre.close()

    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "SEED_DEMO", False)

    conn = connect()
    init_db(conn)  # 必须从中断点续完且不报错
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(tide_revisions)").fetchall()}
    assert {"base_revision", "kind"} <= cols
    kind = conn.execute(
        "SELECT kind FROM tide_revisions WHERE revision = 1"
    ).fetchone()[0]
    assert kind == "root"
    n = conn.execute(
        "SELECT COUNT(*) FROM tide_deltas"
    ).fetchone()[0]
    assert n == 0

    # 再启动一次：完全幂等
    init_db(conn)
    vals = conn.execute(
        "SELECT COUNT(*) FROM tide_values WHERE revision = 1"
    ).fetchone()[0]
    assert vals == 20
    conn.close()

    # 新进程重新打开数据库也保持已迁移状态
    conn2 = connect()
    init_db(conn2)
    assert conn2.execute(
        "SELECT kind FROM tide_revisions WHERE revision = 1"
    ).fetchone()[0] == "root"
    conn2.close()
