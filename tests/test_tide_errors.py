"""潮位预测误差修订（误差带核验）测试。"""
from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan_confirm(client, rev, draft=1.0, height=0.0, margin=0.0,
                  start=0, window=20, origin="A", destination="B"):
    body = {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": draft, "height": height, "margin": margin,
        "revision": rev,
    }
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    planned = r.json()
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 201, r.text
    return r.json()


def _submit_errors(client, errors, base=None, note=None):
    body = {"errors": errors}
    if base is not None:
        body["base_revision"] = base
    if note is not None:
        body["note"] = note
    return client.post("/api/tide/error-revisions", json=body)


# ---------------------------------------------------------------------------
# 1. 误差修订：返回新修订号；未列出分钟继承；名义潮位不变；旧修订误差 0
# ---------------------------------------------------------------------------

def test_error_revision_inherits_and_overrides(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))

    r = _submit_errors(client, {3: 0.5, 7: 1.25}, note="人工误差修订")
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]
    assert rev2 == rev1 + 1

    g = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    # 名义潮位全部保持 3.0
    assert {float(v) for v in g["levels"].values()} == {3.0}
    errs = {int(k): v for k, v in g["errors"].items()}
    assert errs[3] == 0.5
    assert errs[7] == 1.25
    # 未列出分钟继承基础修订误差（旧修订视为 0）
    assert errs[0] == 0.0
    assert errs[19] == 0.0

    # 旧修订仍为误差 0 且不可变
    g1 = client.get(f"/api/tide/revisions/{rev1}",
                    params={"from_minute": 0, "to_minute": 19}).json()
    assert {float(v) for v in g1["errors"].values()} == {0.0}


def test_error_revision_chain_inherits_previous_errors(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))

    rev2 = _submit_errors(client, {3: 0.5}).json()["revision"]
    # 第三版只改第 7 分钟，第 3 分钟误差应继承 rev2
    rev3 = _submit_errors(client, {7: 0.2}).json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    errs = {int(k): v for k, v in g["errors"].items()}
    assert errs[3] == 0.5
    assert errs[7] == 0.2
    assert errs[0] == 0.0


def test_error_revision_based_on_nominal_correction_inherits(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    # 名义潮位更正
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {5: 1.0}}).json()["revision"]
    # 误差修订基于 rev2：名义潮位（含被更正分钟）保持不变
    rev3 = _submit_errors(client, {5: 0.3}).json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    levels = {int(k): v for k, v in g["levels"].items()}
    errs = {int(k): v for k, v in g["errors"].items()}
    assert levels[5] == 1.0
    assert errs[5] == 0.3
    assert errs[4] == 0.0


def test_nominal_correction_inherits_errors(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    _submit_errors(client, {5: 0.4})
    # 名义潮位更正第 5 分钟：该分钟误差继承 0.4，不重置
    rev3 = client.post("/api/tide/corrections",
                       json={"corrections": {5: 2.0}}).json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    levels = {int(k): v for k, v in g["levels"].items()}
    errs = {int(k): v for k, v in g["errors"].items()}
    assert levels[5] == 2.0
    assert errs[5] == 0.4
    assert errs[6] == 0.0


# ---------------------------------------------------------------------------
# 2. 规划按误差带核验：水深名义−误差；净空名义+误差
# ---------------------------------------------------------------------------

def test_plan_depth_uses_level_minus_error(client):
    add_node(client, "A"); add_node(client, "B")
    # 深 4，吃水 6，余量 0.5 => 需要 level-error >= 2.5
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision({0: 2.6, **_level(1, 19, 3.0)})
    rev2 = _submit_errors(client, {0: 0.2}).json()["revision"]
    # rev1 下第 0 分钟可行（2.6>=2.5）；rev2 误差带 2.6-0.2=2.4 < 2.5，需等待
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev2,
    })
    assert r.status_code == 200
    leg = r.json()["itinerary"][0]
    assert leg["start_minute"] == 1
    assert leg["end_minute"] == 3


def test_plan_clearance_uses_level_plus_error(client):
    add_node(client, "A"); add_node(client, "B")
    # 净空 5；船高 3 + 余量 1 => 需要 level+error <= 1
    add_segment(client, "A", "B", travel_time=2, depth=99.0, clearance=5.0)
    rev1 = seed_initial_revision({**_level(0, 9, 0.9), **_level(10, 19, 0.0)})
    rev2 = _submit_errors(client, {m: 0.2 for m in range(0, 10)}).json()["revision"]
    # 名义 0.9 时 0.9+0.2=1.1 > 1，前 10 分钟均不可行，等到第 10 分钟（误差 0）
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 3.0, "margin": 1.0,
        "revision": rev2,
    })
    assert r.status_code == 200
    assert r.json()["itinerary"][0]["start_minute"] == 10


# ---------------------------------------------------------------------------
# 3. 确认：误差修订成为最新后，旧修订确认冲突
# ---------------------------------------------------------------------------

def test_confirm_conflicts_after_error_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    planned = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev1,
    }).json()

    _submit_errors(client, {0: 0.1})
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REVISION_STALE"
    assert client.get("/api/trips").json() == []


def test_confirm_on_error_revision_succeeds(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    seed_initial_revision({0: 2.6, **_level(1, 19, 3.0)})
    rev2 = _submit_errors(client, {0: 0.2}).json()["revision"]
    planned = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev2,
    }).json()
    assert planned["arrival_minute"] == 3
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 201, r.text
    assert r.json()["revision"] == rev2


# ---------------------------------------------------------------------------
# 4. 复核：按目标修订误差带复核原占用分钟；首次违规极值可用值；原规则改期
# ---------------------------------------------------------------------------

def test_review_depth_violation_with_error_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in trip["itinerary"]] == [5]

    # 名义潮位不变（3.0），仅给第 6 分钟加 0.6 误差：4+3-0.6=6.4 < 6.5
    rev2 = _submit_errors(client, {6: 0.6}).json()["revision"]
    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    json={"target_revision": rev2})
    data = r.json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["leg_index"] == 0
    assert v["minute"] == 6
    assert v["reasons"] == ["depth"]
    assert v["tide_level"] == 3.0
    assert v["tide_error"] == 0.6
    assert v["available_depth"] == 6.4          # 4 + 3 - 0.6
    assert v["required_depth"] == 6.5
    assert v["available_clearance"] is None
    # 快照不改写
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["revision"] == rev1


def test_review_clearance_violation_with_error_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0, clearance=5.0)
    rev1 = seed_initial_revision(_level(0, 19, 0.0))
    trip = _plan_confirm(client, rev1, height=3.0, margin=1.0)

    # 名义 0 不变，误差 1.1：5-0-1.1=3.9 < 4.0
    _submit_errors(client, {0: 1.1, 1: 1.1})
    data = client.post(f"/api/trips/{trip['trip_id']}/review",
                       json={}).json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["minute"] == 0
    assert v["reasons"] == ["clearance"]
    assert v["tide_level"] == 0.0
    assert v["tide_error"] == 1.1
    assert v["available_clearance"] == 3.9      # 5 - 0 - 1.1
    assert v["required_clearance"] == 4.0


def test_review_reschedule_uses_error_band(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert trip["arrival_minute"] == 8  # 5 进入，占 5,6,7

    # 误差 0.6 使 5..7 的有效潮位 2.4（需 2.5）；8 起误差 0，可行
    _submit_errors(client, {m: 0.6 for m in range(5, 8)})
    data = client.post(f"/api/trips/{trip['trip_id']}/review", json={}).json()
    assert data["safe"] is False
    assert data["no_solution"] is False
    rs = data["reschedule"]
    assert rs["revision"] == data["target_revision"]
    assert rs["changed_leg_count"] == 1
    assert rs["itinerary"][0]["start_minute"] == 8
    assert rs["arrival_minute"] == 11


def test_review_safe_when_band_still_covers(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 需要 2.5；3.0-0.4=2.6 >= 2.5，仍安全
    _submit_errors(client, {0: 0.4, 1: 0.4})
    data = client.post(f"/api/trips/{trip['trip_id']}/review", json={}).json()
    assert data["safe"] is True
    assert data["reschedule"] is None


# ---------------------------------------------------------------------------
# 5. 报错：未知基础修订；负误差；提交分钟在基础修订缺名义潮位
# ---------------------------------------------------------------------------

def test_error_revision_unknown_base(client):
    r = _submit_errors(client, {0: 0.1}, base=4242)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"
    assert r.json()["error"]["details"]["revision"] == 4242


def test_error_revision_negative_error_rejected(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _submit_errors(client, {3: -0.01})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_error_revision_negative_minute_rejected(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _submit_errors(client, {-1: 0.1})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_error_revision_empty_rejected(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _submit_errors(client, {})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_error_revision_missing_nominal_level(client):
    # 稀疏修订只含 0..9；对第 15 分钟提交误差 -> MISSING_TIDE_DATA
    rev = seed_initial_revision(_level(0, 9, 3.0))
    r = _submit_errors(client, {15: 0.1}, base=rev)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["total_missing"] == 1
    assert err["details"]["missing_minutes"] == [15]
    # 不产生新修订
    revs = client.get("/api/tide/revisions").json()
    assert [x["revision"] for x in revs] == [rev]


def test_error_revision_no_revisions_at_all(client):
    r = _submit_errors(client, {0: 0.1})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"


# ---------------------------------------------------------------------------
# 6. 规划/复核窗口内缺名义潮位仍报 MISSING_TIDE_DATA（误差不影响判定）
# ---------------------------------------------------------------------------

def test_plan_missing_nominal_level_with_error_column(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 9, 3.0))  # 只到 9
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev1,
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "MISSING_TIDE_DATA"


# ---------------------------------------------------------------------------
# 7. 修订不可变：误差修订不改动任何旧修订
# ---------------------------------------------------------------------------

def test_revisions_remain_immutable(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = _submit_errors(client, {0: 0.5}).json()["revision"]
    _submit_errors(client, {0: 0.9})
    _ = client.post("/api/tide/corrections", json={"corrections": {0: 1.1}})

    g1 = client.get(f"/api/tide/revisions/{rev1}",
                    params={"from_minute": 0, "to_minute": 0}).json()
    g2 = client.get(f"/api/tide/revisions/{rev2}",
                    params={"from_minute": 0, "to_minute": 0}).json()
    assert g1["levels"]["0"] == 3.0 and g1["errors"]["0"] == 0.0
    assert g2["levels"]["0"] == 3.0 and g2["errors"]["0"] == 0.5


# ---------------------------------------------------------------------------
# 8. 旧版数据库（tide_values 无 error 列）启动时平滑升级，旧行误差 0
# ---------------------------------------------------------------------------

def test_legacy_schema_migration(tmp_path, monkeypatch):
    import sqlite3

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.executescript(
        "CREATE TABLE tide_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')), note TEXT);"
        "CREATE TABLE tide_values (revision INTEGER, minute INTEGER, level REAL,"
        " PRIMARY KEY (revision, minute));"
        "CREATE TABLE nodes (node_id TEXT PRIMARY KEY, name TEXT,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
        "CREATE TABLE segments (seg_id INTEGER PRIMARY KEY AUTOINCREMENT, origin TEXT,"
        " destination TEXT, travel_time INTEGER, depth REAL, clearance REAL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')), CHECK(origin<>destination));"
        "CREATE TABLE confirmed_trips (trip_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " revision INTEGER, origin TEXT, destination TEXT, start_minute INTEGER,"
        " window_minutes INTEGER, draft REAL, height REAL, margin REAL,"
        " arrival_minute INTEGER, itinerary TEXT,"
        " confirmed_at TEXT NOT NULL DEFAULT (datetime('now')));"
    )
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (1, '旧版')")
    conn.executemany("INSERT INTO tide_values VALUES (1, ?, ?)",
                     [(m, 2.0) for m in range(10)])
    conn.commit()
    conn.close()

    monkeypatch.setenv("DB_PATH", str(db_file))
    from app import config
    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "SEED_DEMO", False)

    conn = connect()
    init_db(conn)  # 不应报错；补 error 列
    rows = conn.execute(
        "SELECT minute, level, error FROM tide_values WHERE revision = 1"
    ).fetchall()
    assert [(r["minute"], r["level"], r["error"]) for r in rows] == \
        [(m, 2.0, 0.0) for m in range(10)]
    conn.close()

