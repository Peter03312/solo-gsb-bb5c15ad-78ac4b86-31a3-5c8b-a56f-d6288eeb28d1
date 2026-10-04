"""航段分时航时管制测试。

规则：
* 以航段编号提交一组半开分钟区间 [start, end) 及正整数通行分钟，整组替换
  旧管制并返回生效清单；空组恢复基础航时；
* 未知航段 SEGMENT_NOT_FOUND、非法区间 INVALID_CONTROL_INTERVAL、组内交叠
  OVERLAPPING_CONTROL_INTERVAL，失败不改旧管制（整组替换在事务内完成）；
* 船在**进入航段的分钟**选定航时，途中跨越管制边界也不变；未命中区间沿用
  航段基础航时；
* 单船规划/确认与同修订批次预排/确认按实际航时计算抵达、全程潮位/桥高与
  航段独占；确认须在原事务中用当时管制重算，变化导致无解或行程不一致按
  PLAN_MISMATCH 拒绝且不保存；批次全成全败、同键重放；
* 已确认快照的既有占用时长不随管制变化；潮位复核按原快照占用；发生改期时
  用当前管制求解。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _put_controls(client, seg_id, controls):
    return client.put(
        f"/api/segments/{seg_id}/travel-controls", json={"controls": controls}
    )


def _get_controls(client, seg_id):
    return client.get(f"/api/segments/{seg_id}/travel-controls")


def _plan(client, body):
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, plan):
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201, r.text
    return r.json()


def _raw_insert_trip(rev, itinerary, seg_id=1, origin="A", destination="B",
                     start=0, window=20):
    """直接写入已确认快照（绕过确认事务），占用时长以行程内 travel_time 为准。"""
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    cur = conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, 1.0, 0.0, 0.0,
         arrival, json.dumps(itinerary)),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trip_id


# ---------------------------------------------------------------------------
# 1. 管制维护：整组替换 / 查询 / 空组恢复 / 错误口径
# ---------------------------------------------------------------------------

def test_replace_controls_returns_effective_list(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=3, depth=99.0)

    r = _put_controls(client, seg, [
        {"start_minute": 10, "end_minute": 20, "travel_time": 7},
        {"start_minute": 0, "end_minute": 10, "travel_time": 2},
    ])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["seg_id"] == seg
    assert data["base_travel_time"] == 3
    # 生效清单按起点升序
    assert data["controls"] == [
        {"start_minute": 0, "end_minute": 10, "travel_time": 2},
        {"start_minute": 10, "end_minute": 20, "travel_time": 7},
    ]

    r = _get_controls(client, seg)
    assert r.status_code == 200
    assert r.json()["controls"] == [
        {"start_minute": 0, "end_minute": 10, "travel_time": 2},
        {"start_minute": 10, "end_minute": 20, "travel_time": 7},
    ]


def test_replace_is_whole_group(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=3, depth=99.0)
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 9}])
    r = _put_controls(client, seg, [
        {"start_minute": 100, "end_minute": 110, "travel_time": 4},
    ])
    assert [c["start_minute"] for c in r.json()["controls"]] == [100]


def test_empty_group_restores_base_travel_time(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=3, depth=99.0)
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 9}])
    r = _put_controls(client, seg, [])
    assert r.status_code == 200
    assert r.json()["controls"] == []
    assert _get_controls(client, seg).json()["controls"] == []


def test_unknown_segment_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=99.0)
    r = _put_controls(client, 999, [{"start_minute": 0, "end_minute": 5, "travel_time": 2}])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"

    r = _get_controls(client, 999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SEGMENT_NOT_FOUND"


def test_invalid_interval_keeps_old_controls(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=3, depth=99.0)
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 2}])

    bad_payloads = [
        [{"start_minute": 0, "end_minute": 0, "travel_time": 2}],   # end == start
        [{"start_minute": 3, "end_minute": 2, "travel_time": 2}],   # end < start
        [{"start_minute": 0, "end_minute": 5, "travel_time": 0}],   # 通行分钟非正
        [{"start_minute": -1, "end_minute": 5, "travel_time": 2}],  # 负分钟
    ]
    for payload in bad_payloads:
        r = _put_controls(client, seg, payload)
        assert r.status_code == 422, payload
        assert r.json()["error"]["code"] == "INVALID_CONTROL_INTERVAL"

    # 旧管制原样保留
    assert _get_controls(client, seg).json()["controls"] == [
        {"start_minute": 0, "end_minute": 5, "travel_time": 2},
    ]


def test_overlapping_intervals_rejected_and_kept(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=3, depth=99.0)
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 2}])

    # 半开交叠 [0,6) 与 [5,10) 在分钟 5 上交叠
    r = _put_controls(client, seg, [
        {"start_minute": 0, "end_minute": 6, "travel_time": 2},
        {"start_minute": 5, "end_minute": 10, "travel_time": 3},
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_CONTROL_INTERVAL"

    # 首尾相接的半开区间 [0,5) 与 [5,10) 合法
    r = _put_controls(client, seg, [
        {"start_minute": 5, "end_minute": 10, "travel_time": 3},
        {"start_minute": 0, "end_minute": 5, "travel_time": 2},
    ])
    assert r.status_code == 200, r.text
    assert [c["start_minute"] for c in r.json()["controls"]] == [0, 5]


# ---------------------------------------------------------------------------
# 2. 规划：进入分钟选航时 / 跨边界不变 / 未命中用基础航时
# ---------------------------------------------------------------------------

def test_plan_uses_control_travel_time_at_entry_minute(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 管制 [0,5) 内进入只需 2 分钟：第 0 分钟进入，第 2 分钟抵达
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 2}])
    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    assert plan["arrival_minute"] == 2
    assert plan["total_wait_minutes"] == 0
    assert plan["itinerary"] == [{
        "seg_id": seg, "origin": "A", "destination": "B",
        "start_minute": 0, "end_minute": 2, "travel_time": 2,
    }]


def test_plan_waits_for_fast_control_window(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 0..4 进入按基础 10 分钟（抵达 >=10）；5..9 进入按管制 2 分钟（最早抵达 7）
    _put_controls(client, seg, [{"start_minute": 5, "end_minute": 10, "travel_time": 2}])
    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    assert plan["itinerary"][0]["start_minute"] == 5
    assert plan["arrival_minute"] == 7
    assert plan["itinerary"][0]["travel_time"] == 2
    assert plan["total_wait_minutes"] == 5


def test_travel_time_fixed_after_entry_across_boundary(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    # [0,5) 内进入航时 6；既有快照占用该航段 0..3 分钟
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 6}])
    _raw_insert_trip(rev, [{
        "seg_id": seg, "origin": "A", "destination": "B",
        "start_minute": 0, "end_minute": 4, "travel_time": 4,
    }])

    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    leg = plan["itinerary"][0]
    # 第 4 分钟进入（命中管制 -> 航时 6），占 4..9，虽第 5 分钟已越出
    # [0,5) 边界，航时仍为 6，第 10 分钟抵达；第 5 分钟进入则按基础 10 分钟
    assert leg["start_minute"] == 4
    assert leg["travel_time"] == 6
    assert leg["end_minute"] == 10


def test_control_travel_exceeding_window_is_infeasible(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 9, 3.0))
    # 0..4 进入航时 10 -> 越出窗口（8 分钟）；5 起进入按基础 2 分钟
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 5, "travel_time": 10}])

    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 8, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    assert plan["itinerary"][0]["start_minute"] == 5
    assert plan["itinerary"][0]["travel_time"] == 2
    assert plan["arrival_minute"] == 7


def test_tide_checked_over_full_controlled_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=1, depth=4.0)
    # 0..2 进入航时 5；第 3 分钟低潮。吃水 6 + 余量 0.5 => 需要潮位 >=2.5
    levels = {**_level(0, 2, 3.0), **_level(3, 3, 0.0), **_level(4, 9, 3.0)}
    rev = seed_initial_revision(levels)
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 3, "travel_time": 5}])

    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 10, "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev,
    })
    # 0/1/2 进入均占满含低潮分钟 3 的 5 分钟 -> 不可行；3 进入本身低潮；
    # 4 进入已出管制区间 -> 基础航时 1，占分钟 4 可行，第 5 分钟抵达
    assert plan["itinerary"][0]["start_minute"] == 4
    assert plan["itinerary"][0]["travel_time"] == 1
    assert plan["arrival_minute"] == 5


def test_multi_leg_each_leg_uses_control_at_its_entry(client):
    for nid in ["A", "B", "C"]:
        add_node(client, nid)
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    _put_controls(client, s1, [{"start_minute": 0, "end_minute": 10, "travel_time": 3}])
    _put_controls(client, s2, [{"start_minute": 0, "end_minute": 10, "travel_time": 3}])

    plan = _plan(client, {
        "origin": "A", "destination": "C", "start_minute": 0,
        "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    # 第 0 分钟进 s1（航时 3）-> 第 3 分钟进 s2（命中管制航时 3）-> 6 抵达
    assert [(l["seg_id"], l["start_minute"], l["end_minute"], l["travel_time"])
            for l in plan["itinerary"]] == [(s1, 0, 3, 3), (s2, 3, 6, 3)]
    assert plan["arrival_minute"] == 6


# ---------------------------------------------------------------------------
# 3. 快照占用时长锁定 + 确认事务按当时管制重算
# ---------------------------------------------------------------------------

def test_confirmed_snapshot_occupancy_survives_control_change(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 40, "travel_time": 5}])

    plan1 = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 40, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    assert (plan1["arrival_minute"], plan1["itinerary"][0]["travel_time"]) == (5, 5)
    trip = _confirm(client, plan1)
    assert trip["itinerary"][0]["travel_time"] == 5

    # 管制改为航时 2：快照占用仍是 0..4（不随管制变化），新船最早第 5 分钟进入
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 40, "travel_time": 2}])
    plan2 = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 40, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    assert plan2["itinerary"][0]["start_minute"] == 5
    assert plan2["itinerary"][0]["travel_time"] == 2
    assert plan2["arrival_minute"] == 7

    # 用管制变化前的旧规划结果确认 -> PLAN_MISMATCH，不保存
    r = client.post("/api/trips/confirm", json=plan1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"

    # 新规划可正常确认
    trip2 = _confirm(client, plan2)
    assert trip2["arrival_minute"] == 7


def test_control_change_making_window_infeasible_is_mismatch(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 9, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 10, "travel_time": 2}])
    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 6, "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    assert plan["arrival_minute"] == 2

    # 管制航时加大到越出窗口
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 10, "travel_time": 20}])
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "PLAN_MISMATCH"
    assert body["error"]["details"]["reason"] == "no_feasible_itinerary"
    # 不保存任何快照
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 4. 批次预排 / 确认：实际航时、全成全败、同键重放
# ---------------------------------------------------------------------------

def test_batch_plan_and_confirm_with_controls(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 59, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 5}])

    body = {
        "revision": rev,
        "ships": [
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 60, "draft": 1.0, "height": 0.0, "margin": 0.0},
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 60, "draft": 1.0, "height": 0.0, "margin": 0.0},
        ],
    }
    r = client.post("/api/trips/batch/plan", json=body)
    assert r.status_code == 200, r.text
    bp = r.json()
    assert bp["ships"][0]["arrival_minute"] == 5     # 0 进入，占 0..4
    assert bp["ships"][1]["itinerary"][0]["start_minute"] == 5
    assert bp["ships"][1]["arrival_minute"] == 10

    confirm_body = {
        "revision": rev,
        "idempotency_key": "ctrl-batch-1",
        "ships": [
            {**body["ships"][i], "itinerary": bp["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 201, r.text
    trips = r.json()["trips"]
    assert [t["arrival_minute"] for t in trips] == [5, 10]

    # 管制变化后同键同内容重放：仍返回首次结果（不重算）
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 2}])
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert [t["arrival_minute"] for t in r.json()["trips"]] == [5, 10]


def test_batch_confirm_mismatch_on_control_change_saves_nothing(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    rev = seed_initial_revision(_level(0, 59, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 5}])

    body = {
        "revision": rev,
        "ships": [
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 60, "draft": 1.0, "height": 0.0, "margin": 0.0},
            {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 60, "draft": 1.0, "height": 0.0, "margin": 0.0},
        ],
    }
    bp = client.post("/api/trips/batch/plan", json=body).json()
    confirm_body = {
        "revision": rev,
        "idempotency_key": "ctrl-batch-2",
        "ships": [
            {**body["ships"][i], "itinerary": bp["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }

    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 2}])
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 0
    assert err["details"]["reason"] == "itinerary_mismatch"
    # 全批不保存（幂等记录也不登记，同键可在重新预排后成功使用）
    assert client.get("/api/trips").json() == []

    bp2 = client.post("/api/trips/batch/plan", json=body).json()
    confirm_body["ships"] = [
        {**body["ships"][i], "itinerary": bp2["ships"][i]["itinerary"]}
        for i in range(2)
    ]
    r = client.post("/api/trips/batch/confirm", json=confirm_body)
    assert r.status_code == 201, r.text
    assert [t["arrival_minute"] for t in r.json()["trips"]] == [2, 4]


# ---------------------------------------------------------------------------
# 5. 复核 / 改期：复核按快照占用；改期用当前管制
# ---------------------------------------------------------------------------

def test_review_uses_snapshot_occupancy_despite_control_change(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev = seed_initial_revision(_level(0, 59, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 5}])

    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 60, "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev,
    })
    assert plan["itinerary"][0]["travel_time"] == 5
    tid = _confirm(client, plan)["trip_id"]

    # 潮位更正：第 4 分钟低潮（快照 0 进入、航时 5，占 0..4）
    r = client.post("/api/tide/corrections", json={
        "base_revision": rev, "corrections": {"4": 0.0},
    })
    rev2 = r.json()["revision"]

    # 同时把管制改为航时 2 —— 复核仍按快照原占用 0..4，在分钟 4 判违规
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 2}])
    r = client.post(f"/api/trips/{tid}/review", json={"target_revision": rev2})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["minute"] == 4
    assert v["reasons"] == ["depth"]


def test_review_safe_when_only_control_changes(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev = seed_initial_revision(_level(0, 59, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 5}])
    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 60, "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev,
    })
    tid = _confirm(client, plan)["trip_id"]

    # 仅管制变化、潮位不变：快照不变，原行程安全
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 8}])
    data = client.post(f"/api/trips/{tid}/review", json={"target_revision": rev}).json()
    assert data["safe"] is True
    assert data["reschedule"] is None


def test_reschedule_solves_with_current_controls(client):
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev = seed_initial_revision(_level(0, 59, 3.0))
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 5}])
    plan = _plan(client, {
        "origin": "A", "destination": "B", "start_minute": 0,
        "window_minutes": 60, "draft": 6.0, "height": 0.0, "margin": 0.5,
        "revision": rev,
    })
    tid = _confirm(client, plan)["trip_id"]

    # 潮位更正：0..9 低潮，10 起恢复；按当时管制（航时 5）改期 ->
    # 10 进入占 10..14，15 抵达
    rev2 = client.post("/api/tide/corrections", json={
        "base_revision": rev, "corrections": {str(m): 0.0 for m in range(0, 10)},
    }).json()["revision"]
    data = client.post(f"/api/trips/{tid}/review",
                       json={"target_revision": rev2}).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    assert rs is not None
    assert rs["itinerary"][0]["start_minute"] == 10
    assert rs["itinerary"][0]["travel_time"] == 5
    assert rs["arrival_minute"] == 15
    assert rs["changed_leg_count"] == 1

    # 再把当前管制改为航时 2：改期重新求解 -> 10 进入、12 抵达
    _put_controls(client, seg, [{"start_minute": 0, "end_minute": 60, "travel_time": 2}])
    data = client.post(f"/api/trips/{tid}/review",
                       json={"target_revision": rev2}).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    assert rs["itinerary"][0]["start_minute"] == 10
    assert rs["itinerary"][0]["travel_time"] == 2
    assert rs["arrival_minute"] == 12

    # 快照始终未被改写
    snap = client.get(f"/api/trips/{tid}").json()
    assert snap["itinerary"][0]["travel_time"] == 5
    assert snap["arrival_minute"] == 5


# ---------------------------------------------------------------------------
# 正航时回路 × 分时航时：晚出发早到，三遍 DP 仍求最早抵达与字典序最优
# ---------------------------------------------------------------------------

def test_loop_with_time_dependent_travel_time(client):
    # 1 A->B：基础航时 5，分钟 0 进入命中管制航时 5（分钟5到 B）；
    #          分钟 1 进入航时 1（分钟2到 B，晚出发反而早到）。
    # 2 B->C、3 C->B 航时均 1；4 B->D 深水航时 1，仅分钟 2..3 可进入前
    # 先确保最早抵达 B 为分钟 2：若正向 DP 只按"首条路抵达"判最早，会误取
    # 分钟 5；正确做法是等待到分钟 1 再发，分钟 4 抵达 D = 1(1→2)+4(3→4)。
    for n in ["A", "B", "C", "D"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=5, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=1, depth=99.0)
    s3 = add_segment(client, "C", "B", travel_time=1, depth=99.0)
    s4 = add_segment(client, "B", "D", travel_time=1, depth=99.0)
    _put_controls(client, s1, [{"start_minute": 0, "end_minute": 1, "travel_time": 5},
                              {"start_minute": 1, "end_minute": 2, "travel_time": 1}])
    rev = seed_initial_revision(_level(0, 19, 3.0))

    data = _plan(client, {
        "origin": "A", "destination": "D",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev,
    })
    # 等待 1 分钟后以管制航时 1 出发；B 分钟 2 -> D 分钟 3
    assert data["arrival_minute"] == 3
    legs = data["itinerary"]
    assert [(l["seg_id"], l["start_minute"], l["end_minute"], l["travel_time"])
            for l in legs] == [
        (s1, 1, 2, 1),
        (s4, 2, 3, 1),
    ]
    assert data["total_wait_minutes"] == 1
