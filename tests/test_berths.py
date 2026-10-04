"""终点泊位与泊位作业区间测试。

规则：
* 调度员为现有节点登记泊位（全局唯一编号），以互不重叠的半开分钟区间
  整组替换开放日历；起点非负、终点大于起点，失败不改旧日历；
* 单船或批次预排可指定终点泊位 + 正整数作业分钟：抵达即开工，作业区间
  [arrival, arrival+work) 须完整落在同一开放区间且不与有效确认行程的
  泊位作业占用半开重叠；返回泊位与作业起止分钟；无解明确告知
  NO_FEASIBLE_ITINERARY；
* 泊位行程按最早抵达、同刻航段序列择优；未指定泊位的旧请求与响应不变；
* 确认在原事务内按当时日历与占用复算，日历/占用变化致不一致 PLAN_MISMATCH
  且不保存；批次避开前船拟占用，失败全批回滚，同键同内容重放原结果；
* 确认快照固化泊位作业区间，日历变更不改历史；作废释放泊位占用；
* 潮位复核仍按原快照；涉及泊位的改期建议须同时找到原泊位可用的即时作业
  区间（只建议不预留）；
* 未知节点/泊位、泊位不属终点、非法区间/作业分钟均拒绝。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan(client, body):
    return client.post("/api/trips/plan", json=body)


def _confirm(client, plan):
    return client.post("/api/trips/confirm", json=plan)


def _add_berth(client, node, bid, note=None):
    r = client.post(f"/api/nodes/{node}/berths",
                    json={"berth_id": bid, "note": note})
    assert r.status_code == 201, r.text
    return r.json()


def _put_calendar(client, bid, intervals):
    return client.put(f"/api/berths/{bid}/calendar",
                      json={"intervals": intervals})


def _base_plan(rev, origin="A", destination="B", start=0, window=200, **kw):
    body = {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": 6.0, "height": 1.0, "margin": 0.5,
        "revision": rev,
    }
    body.update(kw)
    return body


# ---- 登记与日历 -------------------------------------------------------------------

def test_register_berth_for_existing_node(client):
    add_node(client, "A")
    out = _add_berth(client, "A", "A1", note="锚地泊位")
    assert out == {
        "berth_id": "A1", "node_id": "A", "note": "锚地泊位",
        "created_at": out["created_at"], "calendar": [],
    }
    # GET 列表 / 详情
    assert client.get("/api/berths/A1").json()["berth_id"] == "A1"
    listing = client.get("/api/berths").json()
    assert [b["berth_id"] for b in listing] == ["A1"]
    assert client.get("/api/berths", params={"node": "A"}).json()[0]["berth_id"] == "A1"


def test_register_berth_errors(client):
    add_node(client, "A")
    _add_berth(client, "A", "A1")
    # 未知节点
    r = client.post("/api/nodes/Z/berths", json={"berth_id": "Z1"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NODE_NOT_FOUND"
    # 泊位编号重复
    r = client.post("/api/nodes/A/berths", json={"berth_id": "A1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "BERTH_EXISTS"
    # 列表过滤未知节点
    r = client.get("/api/berths", params={"node": "Z"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "NODE_NOT_FOUND"
    # 未知泊位
    assert client.get("/api/berths/NOPE").status_code == 404


def test_replace_calendar_validation_keeps_old(client):
    add_node(client, "A")
    _add_berth(client, "A", "A1")
    # 非法区间：start<0
    r = _put_calendar(client, "A1", [{"start_minute": -1, "end_minute": 10}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_BERTH_INTERVAL"
    # 非法区间：end<=start
    r = _put_calendar(client, "A1", [{"start_minute": 5, "end_minute": 5}])
    assert r.status_code == 422 and r.json()["error"]["code"] == "INVALID_BERTH_INTERVAL"
    # 组内交叠
    r = _put_calendar(client, "A1", [
        {"start_minute": 0, "end_minute": 100},
        {"start_minute": 50, "end_minute": 120},
    ])
    assert r.status_code == 422 and r.json()["error"]["code"] == "OVERLAPPING_BERTH_INTERVAL"
    # 全部失败 -> 初始空日历不变
    assert client.get("/api/berths/A1").json()["calendar"] == []

    # 合法替换成功（首尾相接合法）
    r = _put_calendar(client, "A1", [
        {"start_minute": 0, "end_minute": 100},
        {"start_minute": 100, "end_minute": 200},
    ])
    assert r.status_code == 200
    assert r.json()["calendar"] == [
        {"start_minute": 0, "end_minute": 100},
        {"start_minute": 100, "end_minute": 200},
    ]

    # 之后一次非法提交不改旧日历
    r = _put_calendar(client, "A1", [{"start_minute": 0, "end_minute": -5}])
    assert r.status_code == 422
    assert len(client.get("/api/berths/A1").json()["calendar"]) == 2

    # 未知泊位
    r = _put_calendar(client, "NOPE", [{"start_minute": 0, "end_minute": 10}])
    assert r.status_code == 404 and r.json()["error"]["code"] == "BERTH_NOT_FOUND"


# ---- 预排：开放日历与作业区间 -------------------------------------------------------

def _setup_simple(client):
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 399, 3.0))
    _add_berth(client, "B", "B1")
    return rev


def test_plan_with_berth_returns_work_window(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    r = _plan(client, _base_plan(rev, berth_id="B1", work_minutes=30))
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["arrival_minute"] == 10
    assert p["berth_id"] == "B1" and p["work_minutes"] == 30
    assert p["work_start_minute"] == 10 and p["work_end_minute"] == 40


def test_plan_without_berth_unchanged(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    r = _plan(client, _base_plan(rev))
    assert r.status_code == 200
    p = r.json()
    for f in ("berth_id", "work_minutes", "work_start_minute", "work_end_minute"):
        assert p[f] is None


def test_work_must_fit_within_single_open_interval(client):
    rev = _setup_simple(client)
    # 抵达最早 10，作业 30。开放区间 [0,20) 与 [20,1440)：船不能在终点等待，
    # 但可以在起点 A 等待——推迟到 10 出发、20 抵达，[20,50) 完整落在
    # [20,1440)（等待不允许跨两个开放区间拼作业）。
    _put_calendar(client, "B1", [
        {"start_minute": 0, "end_minute": 20},
        {"start_minute": 20, "end_minute": 1440},
    ])
    r = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=30))
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["arrival_minute"] == 20
    assert (p["work_start_minute"], p["work_end_minute"]) == (20, 50)

    # 作业 10 -> 最早 10 抵达，[10,20) 恰完整落在 [0,20)
    r = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=10))
    assert r.status_code == 200
    p = r.json()
    assert (p["work_start_minute"], p["work_end_minute"]) == (10, 20)

    # 只有 [0,20) 开放、作业 30：任何抵达都放不下 -> 无解
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 20}])
    r = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=30))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"

    # 区间刚好贴合：[10,40) 落在 [10,40)
    _put_calendar(client, "B1", [{"start_minute": 10, "end_minute": 40}])
    r = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=30))
    assert r.status_code == 200
    assert r.json()["arrival_minute"] == 10


def test_empty_calendar_means_no_solution(client):
    rev = _setup_simple(client)
    r = _plan(client, _base_plan(rev, berth_id="B1", work_minutes=5))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"


def test_work_window_must_fit_in_planning_window(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    # 窗口 30：抵达 10，作业 25 -> 35 > 30，越窗无解
    r = _plan(client, _base_plan(rev, window=30, berth_id="B1", work_minutes=25))
    assert r.status_code == 404
    r = _plan(client, _base_plan(rev, window=40, berth_id="B1", work_minutes=25))
    assert r.status_code == 200


def test_berth_request_validation(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    # 未知泊位
    r = _plan(client, _base_plan(rev, berth_id="NOPE", work_minutes=5))
    assert r.status_code == 404 and r.json()["error"]["code"] == "BERTH_NOT_FOUND"
    # 泊位不属终点（给 B 的泊位但终点为 A：用 B1 + destination A 无航段也必拒）
    add_node(client, "C")
    add_segment(client, "A", "C", travel_time=5, depth=99.0)
    _add_berth(client, "C", "C1")
    _put_calendar(client, "C1", [{"start_minute": 0, "end_minute": 1440}])
    r = _plan(client, _base_plan(rev, destination="C", berth_id="B1", work_minutes=5))
    assert r.status_code == 422 and r.json()["error"]["code"] == "BERTH_NOT_AT_DESTINATION"
    # 泊位与作业分钟须成对
    r = _plan(client, _base_plan(rev, berth_id="B1"))
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = _plan(client, _base_plan(rev, work_minutes=5))
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 作业分钟非正整数
    r = _plan(client, _base_plan(rev, berth_id="B1", work_minutes=0))
    assert r.status_code == 422


def test_multi_leg_berth_itinerary_is_in_travel_order(client):
    add_node(client, "A")
    add_node(client, "B")
    add_node(client, "C")
    add_segment(client, "A", "B", travel_time=10, depth=99.0)
    add_segment(client, "B", "C", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 399, 3.0))
    _add_berth(client, "C", "C1")
    _put_calendar(client, "C1", [{"start_minute": 0, "end_minute": 1440}])
    p = _plan(client, _base_plan(rev, destination="C", window=300,
                                 berth_id="C1", work_minutes=5)).json()
    assert [l["origin"] for l in p["itinerary"]] == ["A", "B"]
    assert [l["destination"] for l in p["itinerary"]] == ["B", "C"]
    # 首尾相接：前段抵达 = 后段出发；抵达即开工
    assert p["itinerary"][0]["end_minute"] == p["itinerary"][1]["start_minute"]
    assert p["arrival_minute"] == 20
    assert (p["work_start_minute"], p["work_end_minute"]) == (20, 25)


def test_arrival_tie_prefers_lexicographic_segment_sequence(client):
    add_node(client, "A")
    add_node(client, "B")
    add_node(client, "X")
    # 两条同航时路径 A->B 与 A->X->B，同时抵达
    s1 = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    add_segment(client, "A", "X", travel_time=5, depth=99.0)
    add_segment(client, "X", "B", travel_time=5, depth=99.0)
    rev = seed_initial_revision(_level(0, 399, 3.0))
    _add_berth(client, "B", "B1")
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    p = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=10)).json()
    # seg_id 较小的直航段 A->B 序列更优
    assert [leg["seg_id"] for leg in p["itinerary"]] == [s1]
    assert p["work_start_minute"] == 10


# ---- 确认：泊位占用、复算、快照固化 --------------------------------------------------

def test_confirm_snapshot_fixes_berth_work_window(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, _base_plan(rev, berth_id="B1", work_minutes=30)).json()
    r = _confirm(client, plan)
    assert r.status_code == 201, r.text
    trip = r.json()
    assert trip["berth_id"] == "B1"
    assert (trip["work_start_minute"], trip["work_end_minute"]) == (10, 40)
    # 列表接口同样回传
    listed = client.get("/api/trips").json()[0]
    assert listed["berth_id"] == "B1" and listed["work_minutes"] == 30


def test_confirmed_berth_occupancy_blocks_later_plan(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, _base_plan(rev, start_minute=0, window=300,
                                    berth_id="B1", work_minutes=30)).json()
    assert _confirm(client, plan).status_code == 201  # 占用 B1 [10,40)

    # 后船窗口 0..49：最早 10 抵达与 [10,40) 冲突；在起点等待后只能 40
    # 抵达，但作业 20 -> [40,60) 越窗 -> 无解（占用推迟 + 窗口边界共同封死）
    r = _plan(client, _base_plan(rev, start_minute=0, window=50,
                                 berth_id="B1", work_minutes=20))
    assert r.status_code == 404 and r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"

    # 窗口放宽后 40 抵达可行（作业 [40,60)），验证占用确实把抵达从 10 推到 40
    r = _plan(client, _base_plan(rev, start_minute=0, window=300,
                                 berth_id="B1", work_minutes=20))
    assert r.status_code == 200
    assert r.json()["work_start_minute"] == 40

    # 半开边界：后船 40 抵达（start=30，航时 10），作业 [40,50) 与
    # [10,40) 首尾相接，合法
    r = _plan(client, _base_plan(rev, start_minute=30, window=300,
                                 berth_id="B1", work_minutes=10))
    assert r.status_code == 200
    assert r.json()["work_start_minute"] == 40


def test_calendar_change_after_confirm_rejects_stale_confirmation(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 40}])
    plan = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=30)).json()
    assert plan["arrival_minute"] == 10

    # 确认前把开放日历收紧到 [30,1440)：[10,40) 不再完整落入，复算无解
    _put_calendar(client, "B1", [{"start_minute": 30, "end_minute": 1440}])
    r = _confirm(client, plan)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_MISMATCH"
    # 未保存
    assert client.get("/api/trips").json() == []


def test_calendar_change_after_confirm_keeps_history_and_occupancy(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=30)).json()
    assert _confirm(client, plan).status_code == 201  # 固化 [10,40)

    # 日历整体关闭：历史快照原样可查，且泊位占用仍按固化区间 [10,40) 封闭
    _put_calendar(client, "B1", [])
    trip = client.get("/api/trips").json()[0]
    assert (trip["work_start_minute"], trip["work_end_minute"]) == (10, 40)
    # 新预排按新日历：无开放区间 -> 无解
    r = _plan(client, _base_plan(rev, start_minute=100, window=300,
                                 berth_id="B1", work_minutes=5))
    assert r.status_code == 404


def test_void_releases_berth_occupancy(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    plan = _plan(client, _base_plan(rev, window=300, berth_id="B1", work_minutes=30)).json()
    tid = _confirm(client, plan).json()["trip_id"]
    # 占用 [10,40)：后船作业 30 时最早可行抵达被推到 40，窗口仅 40 时无解
    blocked = _plan(client, _base_plan(rev, window=40, berth_id="B1", work_minutes=30))
    assert blocked.status_code == 404
    # 作废后占用释放：同请求恢复可行（10 抵达、[10,40)）
    r = client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    assert r.status_code == 201
    again = _plan(client, _base_plan(rev, window=40, berth_id="B1", work_minutes=30))
    assert again.status_code == 200
    assert again.json()["work_start_minute"] == 10
    # 历史快照仍可查且带泊位字段
    trip = client.get(f"/api/trips/{tid}").json()
    assert trip["voided_at"] is not None and trip["berth_id"] == "B1"


# ---- 批次 --------------------------------------------------------------------------

def _two_ships(rev, bid="B1", work=10, window=300):
    return {
        "revision": rev,
        "ships": [
            {**_base_plan(rev, start_minute=0, window=window,
                          berth_id=bid, work_minutes=work)},
            {**_base_plan(rev, start_minute=0, window=window,
                          berth_id=bid, work_minutes=work)},
        ],
    }


def test_batch_plan_later_ship_avoids_prior_berth_occupancy(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    # 可串行的情形：前船作业 10（[10,20)），后船 start=10 可在 20 抵达
    body = {
        "revision": rev,
        "ships": [
            _base_plan(rev, start_minute=0, window=300, berth_id="B1", work_minutes=10),
            _base_plan(rev, start_minute=10, window=300, berth_id="B1", work_minutes=10),
        ],
    }
    r = client.post("/api/trips/batch/plan", json=body)
    assert r.status_code == 200, r.text
    ships = r.json()["ships"]
    assert ships[0]["work_start_minute"] == 10
    # 后船航程 10 分钟，10 出发 20 抵达恰好避开前船 [10,20)（半开首尾相接）
    assert ships[1]["arrival_minute"] == 20
    assert ships[1]["work_start_minute"] == 20


def test_batch_plan_conflict_reports_first_failing_index(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    # 窗口仅 25：前船 10 抵达作业 [10,20)；后船避占最早 20 抵达，作业
    # [20,30) 越窗 -> 无解，首个失败序号 1
    r = client.post("/api/trips/batch/plan", json=_two_ships(rev, work=10, window=25))
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_ITINERARY"
    assert err["details"]["ship_index"] == 1


def test_batch_confirm_rollback_on_berth_mismatch_and_replay(client):
    rev = _setup_simple(client)
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    body = {
        "revision": rev,
        "idempotency_key": "k1",
        "ships": [
            {**_base_plan(rev, start_minute=0, window=300, berth_id="B1", work_minutes=10)},
            {**_base_plan(rev, start_minute=10, window=300, berth_id="B1", work_minutes=10)},
        ],
    }
    pre = client.post("/api/trips/batch/plan",
                      json={"revision": rev, "ships": body["ships"]}).json()
    for s, p in zip(body["ships"], pre["ships"]):
        s["itinerary"] = p["itinerary"]

    # 确认前改动日历使两船作业区间均不再可行（关闭全部开放区间）
    _put_calendar(client, "B1", [])
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json() == []  # 全批回滚

    # 恢复日历后成功
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    first = r.json()
    assert [t["berth_id"] for t in first["trips"]] == ["B1", "B1"]

    # 同键同内容重放 -> 200 原结果（原行程号）
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 200 and r.json()["replayed"] is True
    assert [t["trip_id"] for t in r.json()["trips"]] == \
           [t["trip_id"] for t in first["trips"]]


# ---- 复核 / 改期建议 ---------------------------------------------------------------

def _raw_berth_trip(rev, bid, work, arrival, itinerary, start=0, window=300):
    conn = connect()
    cur = conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary,"
        "  berth_id, work_minutes, work_start_minute, work_end_minute)"
        " VALUES (?, 'A', 'B', ?, ?, 6.0, 1.0, 0.5, ?, ?,"
        "         ?, ?, ?, ?)",
        (rev, start, window, arrival, json.dumps(itinerary),
         bid, work, arrival, arrival + work),
    )
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid


def test_review_tide_check_uses_snapshot_and_reschedule_finds_berth_window(client):
    add_node(client, "A")
    add_node(client, "B")
    # 浅水航段：要求水深 6.5m，名义潮位 3m 时 4+3=7 可行，低潮 0 不可行
    seg = add_segment(client, "A", "B", travel_time=10, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 399, 3.0))
    _add_berth(client, "B", "B1")
    # 开放窗口分两段：[0,20)（容纳 10 分钟作业）与 [30,1440)
    _put_calendar(client, "B1", [
        {"start_minute": 0, "end_minute": 20},
        {"start_minute": 30, "end_minute": 1440},
    ])
    leg = {"seg_id": seg, "origin": "A", "destination": "B",
           "start_minute": 0, "end_minute": 10, "travel_time": 10}
    # 快照：0 出发 10 抵达，作业 [10,20)
    tid = _raw_berth_trip(rev1, "B1", 10, 10, [leg])

    # 潮位更正：第 0..9 分钟变浅，原航路不安全
    conn = connect()
    rev2 = conn.execute("INSERT INTO tide_revisions(note) VALUES (?)",
                        ("低潮更正",)).lastrowid
    conn.execute(
        "INSERT INTO tide_values(revision, minute, level)"
        " SELECT ?, minute, level FROM tide_values WHERE revision = 1",
        (rev2,),
    )
    conn.executemany(
        "UPDATE tide_values SET level = ? WHERE revision = ? AND minute = ?",
        [(0.0, rev2, m) for m in range(0, 10)],
    )
    conn.commit()
    conn.close()

    r = client.post(f"/api/trips/{tid}/review", json={"target_revision": rev2})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["safe"] is False
    assert out["berth_id"] == "B1"  # 复核响应带快照泊位
    resched = out["reschedule"]
    assert resched is not None and out["no_solution"] is False
    # 改期出发须等到 20（避开低潮航段 0..19 中至少 [0,20) 出发不可行），
    # 同时原泊位即时作业区间落在 [30,1440)：30 抵达、[30,40) 作业
    assert resched["arrival_minute"] >= 30
    assert resched["berth_id"] == "B1"
    assert resched["work_start_minute"] == resched["arrival_minute"]
    assert resched["work_end_minute"] == resched["arrival_minute"] + 10
    # 建议不预留：再查行程，快照不变
    trip = client.get(f"/api/trips/{tid}").json()
    assert trip["arrival_minute"] == 10


def test_review_no_solution_when_berth_cannot_fit(client):
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 399, 3.0))
    _add_berth(client, "B", "B1")
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 20}])
    leg = {"seg_id": seg, "origin": "A", "destination": "B",
           "start_minute": 0, "end_minute": 10, "travel_time": 10}
    tid = _raw_berth_trip(rev1, "B1", 10, 10, [leg])

    # 低潮更正迫使改期到 20 之后出发，但泊位日历只到 20，作业无落处
    conn = connect()
    rev2 = conn.execute("INSERT INTO tide_revisions(note) VALUES (?)",
                        ("低潮",)).lastrowid
    conn.execute(
        "INSERT INTO tide_values(revision, minute, level)"
        " SELECT ?, minute, level FROM tide_values WHERE revision = 1",
        (rev2,),
    )
    conn.executemany(
        "UPDATE tide_values SET level = ? WHERE revision = ? AND minute = ?",
        [(0.0, rev2, m) for m in range(0, 20)],
    )
    conn.commit()
    conn.close()

    r = client.post(f"/api/trips/{tid}/review", json={"target_revision": rev2})
    out = r.json()
    assert out["safe"] is False and out["reschedule"] is None
    assert out["no_solution"] is True


def test_review_of_safe_berth_trip_unchanged(client):
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 399, 3.0))
    _add_berth(client, "B", "B1")
    _put_calendar(client, "B1", [{"start_minute": 0, "end_minute": 1440}])
    leg = {"seg_id": seg, "origin": "A", "destination": "B",
           "start_minute": 0, "end_minute": 10, "travel_time": 10}
    tid = _raw_berth_trip(rev1, "B1", 10, 10, [leg])
    # 同修订复核：安全，无需改期（泊位日历变化与潮位安全无关）
    _put_calendar(client, "B1", [])
    out = client.post(f"/api/trips/{tid}/review").json()
    assert out["safe"] is True and out["reschedule"] is None


# ---- 正航时回路下的泊位模式择优 -----------------------------------------------------

def test_berth_positive_time_loop_keeps_longer_lex_prefix(client):
    # 同题面回路图：1 A->B、2 B->C、3 C->B、4 B->D，航时均 1；4 仅分钟 3 可行。
    # 泊位 D1 作业 1 分钟：(1,2,3,4) 分钟 4 抵达即开工，序列字典序优于 (1,4)。
    for n in ["A", "B", "C", "D"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", 1)
    s2 = add_segment(client, "B", "C", 1)
    s3 = add_segment(client, "C", "B", 1)
    s4 = add_segment(client, "B", "D", 1, depth=0.5)
    levels = {m: 0.0 for m in range(0, 5)}
    levels[3] = 3.0
    rev = seed_initial_revision(levels)
    _add_berth(client, "D", "D1")
    _put_calendar(client, "D1", [{"start_minute": 0, "end_minute": 1440}])

    body = {
        "origin": "A", "destination": "D",
        "start_minute": 0, "window_minutes": 5,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev, "berth_id": "D1", "work_minutes": 1,
    }
    r = _plan(client, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["arrival_minute"] == 4
    assert [l["seg_id"] for l in data["itinerary"]] == [s1, s2, s3, s4]
    assert data["work_start_minute"] == 4 and data["work_end_minute"] == 5

    # 确认复算一致
    r = _confirm(client, data)
    assert r.status_code == 201, r.text
    assert [l["seg_id"] for l in r.json()["itinerary"]] == [s1, s2, s3, s4]
