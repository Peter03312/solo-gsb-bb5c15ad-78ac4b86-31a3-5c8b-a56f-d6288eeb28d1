"""狭窄水道组测试。

规则：
* 调度员可将至少两条**现有**航段登记为同一狭窄水道组，提交航段编号集合，
  返回组号及成员；组建成后不可修改（无更新/解散），航段不得重复归组；
* 未知航段、提交含重复编号或冲突归组须拒绝且不留半成品；
* 船进入组内任一航段后，从进入分钟 d 到抵达前一分钟 d+t-1 独占整组；
  不同成员航段的通行也不得重叠；抵达分钟 d+t 可由下一船进入；
* 组建前的已确认快照按其原航段与固化航时计入新组占用，既有快照不改写、
  已有重叠也不清理；
* 单船与批次预排须避开组占用；确认时在原事务内按当前组关系与已确认及
  批内拟占用复算，不一致沿用 PLAN_MISMATCH 且整批回滚；
* 改期建议须避开其他快照的组占用并排除自身；原行程安全复核仍只判断潮位。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan(client, body):
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _create_group(client, seg_ids, note=None):
    body = {"segments": seg_ids}
    if note is not None:
        body["note"] = note
    return client.post("/api/channel-groups", json=body)


def _raw_insert_trip(rev, itinerary, origin="A", destination="D",
                     start=0, window=30, draft=1.0, height=0.0, margin=0.0):
    """直接写入已确认快照（绕过确认事务），用于构造组建前快照。"""
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, draft, height, margin,
         arrival, json.dumps(itinerary)),
    )
    conn.commit()
    conn.close()


def _two_segments_in_chain(client, t1=2, t2=3):
    """A->B(seg1) 与 B->C(seg2)，默认潮位全程可行。返回修订号。"""
    add_node(client, "A"); add_node(client, "B"); add_node(client, "C")
    add_segment(client, "A", "B", travel_time=t1, depth=99.0)
    add_segment(client, "B", "C", travel_time=t2, depth=99.0)
    return seed_initial_revision(_level(0, 29, 3.0))


# ---------------------------------------------------------------------------
# 1. 组建：返回组号及成员（按提交次序）；组号自增
# ---------------------------------------------------------------------------

def test_create_group_returns_id_and_members_in_submitted_order(client):
    rev = _two_segments_in_chain(client)
    r = _create_group(client, [2, 1], note="狭口")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["group_id"] >= 1
    assert data["note"] == "狭口"
    assert [m["seg_id"] for m in data["members"]] == [2, 1]
    assert [m["position"] for m in data["members"]] == [0, 1]
    assert data["members"][0]["origin"] == "B"

    # GET 列表与详情一致
    listing = client.get("/api/channel-groups").json()
    assert [g["group_id"] for g in listing] == [data["group_id"]]
    detail = client.get(f"/api/channel-groups/{data['group_id']}").json()
    assert [m["seg_id"] for m in detail["members"]] == [2, 1]


def test_independent_groups_autoincrement(client):
    _two_segments_in_chain(client)
    g1 = _create_group(client, [1, 2])
    assert g1.status_code == 201
    id1 = g1.json()["group_id"]

    # 全新两条航段组成第二组：组号自增、成员互不影响
    for n in ("D", "E", "F"):
        add_node(client, n)
    add_segment(client, "D", "E", travel_time=2, depth=99.0)
    add_segment(client, "E", "F", travel_time=2, depth=99.0)
    g2 = _create_group(client, [3, 4])
    assert g2.status_code == 201, g2.text
    assert g2.json()["group_id"] == id1 + 1
    assert [m["seg_id"] for m in g2.json()["members"]] == [3, 4]
    assert len(client.get("/api/channel-groups").json()) == 2


def test_get_unknown_group_404(client):
    _two_segments_in_chain(client)
    r = client.get("/api/channel-groups/999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "CHANNEL_GROUP_NOT_FOUND"


# ---------------------------------------------------------------------------
# 2. 拒绝：未知航段 / 重复编号 / 冲突归组 / 成员不足，且不留半成品
# ---------------------------------------------------------------------------

def test_create_group_unknown_segment_rejected(client):
    _two_segments_in_chain(client)
    r = _create_group(client, [1, 99])
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "SEGMENT_NOT_FOUND"
    assert err["details"]["unknown_seg_ids"] == [99]
    # 不留半成品：组表为空
    assert client.get("/api/channel-groups").json() == []


def test_create_group_duplicate_segment_in_submission(client):
    _two_segments_in_chain(client)
    r = _create_group(client, [1, 2, 1])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "DUPLICATE_SEGMENT_IN_GROUP"
    assert err["details"]["duplicates"] == [1]
    assert client.get("/api/channel-groups").json() == []


def test_create_group_segment_already_grouped_conflict(client):
    _two_segments_in_chain(client)
    assert _create_group(client, [1, 2]).status_code == 201
    # 再把其中一条与新航段成组 -> 冲突拒绝，新组不产生
    add_node(client, "D")
    add_segment(client, "C", "D", travel_time=2, depth=99.0)
    r = _create_group(client, [2, 3])
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SEGMENT_ALREADY_GROUPED"
    assert err["details"]["conflicts"][0]["seg_id"] == 2
    # 只有第一组，且成员不变（不可修改）
    groups = client.get("/api/channel-groups").json()
    assert len(groups) == 1
    assert sorted(m["seg_id"] for m in groups[0]["members"]) == [1, 2]
    # 全新两条航段仍可正常建第二组
    add_node(client, "E"); add_node(client, "F")
    add_segment(client, "D", "E", travel_time=2, depth=99.0)
    add_segment(client, "E", "F", travel_time=2, depth=99.0)
    r2 = _create_group(client, [4, 5])
    assert r2.status_code == 201
    assert r2.json()["group_id"] != groups[0]["group_id"]


def test_create_group_requires_at_least_two_segments(client):
    _two_segments_in_chain(client)
    assert _create_group(client, []).status_code == 422
    assert _create_group(client, [1]).status_code == 422
    assert client.get("/api/channel-groups").json() == []


# ---------------------------------------------------------------------------
# 3. 独占整组：进入组内任一航段，占用展开到全组；抵达分钟可进入
# ---------------------------------------------------------------------------

def test_plan_avoids_group_occupancy_on_other_member_segment(client):
    """同航路：船 0 走 seg1(A->B)，其占用展开到 seg2(B->C)；
    船 1 走 A->C，seg1 出发也须避开船 0 对 seg1 的直接占用。"""
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    assert _create_group(client, [1, 2]).status_code == 201

    # 船 0 只走 A->B：seg1 占 0,1
    p0 = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in p0["itinerary"]] == [0]
    r = client.post("/api/trips/confirm", json=p0)
    assert r.status_code == 201, r.text

    # 船 1 走 A->C：seg1 的 0,1 被直接占用，最早 2 进入（占 2,3）、4 抵 B；
    # seg2 首尾相接 4 出发（占 4,5,6）——船 0 的组展开仅封锁 0,1，4 可行，
    # 且第 2 分钟（船 0 抵达分钟）即可进入 seg1。
    p1 = _plan(client, {
        "origin": "A", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [(l["seg_id"], l["start_minute"]) for l in p1["itinerary"]] == [
        (1, 2), (2, 4),
    ]


def test_group_blocks_different_member_departure_without_direct_segment_overlap(client):
    """组的核心：快照只经过 seg1，却能封锁后船在 seg2 上的通行窗口。"""
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    # 组建前先确认一个只走 seg1 的快照：占 seg1 的 2,3（第 4 分钟抵达）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ], destination="B")
    assert _create_group(client, [1, 2]).status_code == 201

    # 船 1 起自 B 只走 seg2：seg2 自身无直接占用，但组展开封锁 2,3。
    # 0 进入占 0,1,2（2 冲突）、1 进入占 1,2,3（冲突）、2/3 进入冲突，
    # 4 进入占 4,5,6 可行——且第 4 分钟正是先船抵达分钟，允许进入。
    p = _plan(client, {
        "origin": "B", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [(l["seg_id"], l["start_minute"], l["end_minute"])
            for l in p["itinerary"]] == [(2, 4, 7)]


def test_without_group_snapshot_on_seg1_does_not_block_seg2(client):
    """无组对照：seg1 上的快照不影响 seg2。"""
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ], destination="B")
    # 不建组
    p = _plan(client, {
        "origin": "B", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in p["itinerary"]] == [0]


# ---------------------------------------------------------------------------
# 4. 组建前快照：按原航段与固化航时计入新组；快照不改写、重叠不清理
# ---------------------------------------------------------------------------

def test_pre_group_snapshot_counts_by_original_segment_and_fixed_time(client):
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    # 组建前快照只走 seg1、占 0,1（固化航时 2）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ], destination="B")
    assert _create_group(client, [1, 2]).status_code == 201

    # 快照内容不改写
    trips = client.get("/api/trips").json()
    assert trips[0]["itinerary"][0]["travel_time"] == 2

    # 组占用按固化航时展开：seg2 的 0,1 封闭；B->C 最早 2 进入（占 2,3,4）
    p = _plan(client, {
        "origin": "B", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in p["itinerary"]] == [2]


def test_pre_group_overlapping_snapshots_are_not_cleaned_and_still_block(client):
    """组建前两条快照在组内重叠（3..4）：原样保留，重叠分钟仍封闭全组。"""
    rev = _two_segments_in_chain(client, t1=2, t2=5)
    # 快照 A：seg1 占 3..7；快照 B：seg1 占 0..4（彼此在 3,4 重叠）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 3, "end_minute": 8, "travel_time": 5},
    ], destination="B")
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 5, "travel_time": 5},
    ], destination="B")
    assert _create_group(client, [1, 2]).status_code == 201

    # seg2（B->C，5 分钟）须避开 0..7：最早 8 进入（抵达分钟 8 开放）
    p = _plan(client, {
        "origin": "B", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in p["itinerary"]] == [8]
    # 两条快照原样保留
    assert len(client.get("/api/trips").json()) == 2


# ---------------------------------------------------------------------------
# 5. 批次预排/确认：批内拟占用按组展开；组建后旧预排确认整批回滚
# ---------------------------------------------------------------------------

def test_batch_plan_proposed_occupancy_expands_to_group(client):
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    assert _create_group(client, [1, 2]).status_code == 201

    ships = [
        # 船 0：A->B（seg1 0,1）；船 1：B->C（seg2），受组展开影响
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 30, "draft": 1.0, "height": 0.0, "margin": 0.0},
        {"origin": "B", "destination": "C", "start_minute": 0,
         "window_minutes": 30, "draft": 1.0, "height": 0.0, "margin": 0.0},
    ]
    r = client.post("/api/trips/batch/plan",
                    json={"revision": rev, "ships": ships})
    assert r.status_code == 200, r.text
    planned = r.json()
    assert [l["start_minute"] for l in planned["ships"][0]["itinerary"]] == [0]
    # seg2 被船 0 的拟占用（展开）封锁 0,1；最早 2 进入
    assert [(l["seg_id"], l["start_minute"])
            for l in planned["ships"][1]["itinerary"]] == [(2, 2)]


def test_batch_plan_readonly_does_not_occupy(client):
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    assert _create_group(client, [1, 2]).status_code == 201
    body = {
        "revision": rev,
        "ships": [
            {"origin": "B", "destination": "C", "start_minute": 0,
             "window_minutes": 30, "draft": 1.0, "height": 0.0, "margin": 0.0},
            {"origin": "B", "destination": "C", "start_minute": 0,
             "window_minutes": 30, "draft": 1.0, "height": 0.0, "margin": 0.0},
        ],
    }
    r1 = client.post("/api/trips/batch/plan", json=body).json()
    r2 = client.post("/api/trips/batch/plan", json=body).json()
    assert r1 == r2
    assert client.get("/api/trips").json() == []


def _batch_confirm_body(rev, planned, key):
    return {
        "revision": rev,
        "idempotency_key": key,
        "ships": [
            {
                "origin": s["origin"], "destination": s["destination"],
                "start_minute": s["start_minute"], "window_minutes": s["window_minutes"],
                "draft": s["draft"], "height": s["height"], "margin": s["margin"],
                "itinerary": s["itinerary"],
            }
            for s in planned["ships"]
        ],
    }


def test_batch_confirm_recomputes_with_new_group_and_rolls_back_entire_batch(client):
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 30, "draft": 1.0, "height": 0.0, "margin": 0.0},
        {"origin": "B", "destination": "C", "start_minute": 0,
         "window_minutes": 30, "draft": 1.0, "height": 0.0, "margin": 0.0},
    ]
    planned = client.post("/api/trips/batch/plan",
                          json={"revision": rev, "ships": ships}).json()
    # 无组时船 1 seg2 于 0 出发
    assert [l["start_minute"] for l in planned["ships"][1]["itinerary"]] == [0]

    # 预排之后、确认之前组建（1,2）：事务内按当前组关系复算 -> 不一致
    assert _create_group(client, [1, 2]).status_code == 201
    r = client.post("/api/trips/batch/confirm",
                    json=_batch_confirm_body(rev, planned, "batch-group-1"))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 1
    # 整批回滚：无任何快照
    assert client.get("/api/trips").json() == []

    # 重新预排（按组避开）后确认成功，两条快照都写入
    planned2 = client.post("/api/trips/batch/plan",
                           json={"revision": rev, "ships": ships}).json()
    assert [l["start_minute"] for l in planned2["ships"][1]["itinerary"]] == [2]
    r2 = client.post("/api/trips/batch/confirm",
                     json=_batch_confirm_body(rev, planned2, "batch-group-2"))
    assert r2.status_code == 201, r2.text
    assert len(client.get("/api/trips").json()) == 2


def test_single_confirm_recomputes_with_new_group_plan_mismatch(client):
    rev = _two_segments_in_chain(client, t1=2, t2=3)
    # B->C 的规划（无组，seg2 0 出发）
    body = {
        "origin": "B", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    }
    planned = _plan(client, body)
    assert [l["start_minute"] for l in planned["itinerary"]] == [0]

    # 先确认一条走 seg1 的快照，再组建：seg2 0..1 被组展开封锁
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ], destination="B")
    assert _create_group(client, [1, 2]).status_code == 201

    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json()  # 原有 raw 快照仍在


# ---------------------------------------------------------------------------
# 6. 改期建议避开其他快照的组占用并排除自身；原行程复核只看潮位
# ---------------------------------------------------------------------------

def test_review_tide_check_ignores_group_but_reschedule_avoids_group(client):
    rev = _two_segments_in_chain(client, t1=2, t2=2)
    # seg2 水深 4m：潮位 3m 时满足吃水 6+余量 0.5（4+3=7>=6.5），低潮 0 即违规
    conn = connect()
    conn.execute("UPDATE segments SET depth = 4.0 WHERE seg_id = 2")
    conn.commit()
    conn.close()
    ship = {"draft": 6.0, "height": 0.0, "margin": 0.5}
    # 先确认被复核行程：A->C，seg1 0,1 + seg2 2,3，4 抵达
    plan = _plan(client, {
        "origin": "A", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        **ship, "revision": rev,
    })
    assert [(l["seg_id"], l["start_minute"]) for l in plan["itinerary"]] == [
        (1, 0), (2, 2),
    ]
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201, r.text
    trip_id = r.json()["trip_id"]

    # 另一条船只走 seg1、占 4,5（与被复核行程不重叠）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 4, "end_minute": 6, "travel_time": 2},
    ], destination="B")
    assert _create_group(client, [1, 2]).status_code == 201

    # 潮位更正：让原 seg2 的第 2 分钟水深不足，触发改期
    cr = client.post("/api/tide/corrections", json={"corrections": {2: 0.0}})
    new_rev = cr.json()["revision"]

    rr = client.post(f"/api/trips/{trip_id}/review",
                     json={"target_revision": new_rev})
    assert rr.status_code == 200, rr.text
    data = rr.json()
    # 原行程复核只判潮位：seg2 第 2 分钟违规（与组占用无关）
    assert data["safe"] is False
    assert data["first_violation"]["seg_id"] == 2
    assert data["first_violation"]["minute"] == 2

    # 改期：seg1 无需变动（0,1 仍安全且不与自身冲突）；seg2 重算航时 2，
    # 须满足潮位（第 2 分钟低潮）且避开他船组占用（seg1 的 4,5 展开到 seg2）。
    # 候选：seg2 于 2 出发占 2,3 -> 低潮违规；3 出发占 3,4 -> 4 组占用；
    #       4 出发占 4,5 -> 组占用；5 出发占 5,6 -> 5 组占用；6 出发占 6,7 可行。
    resched = data["reschedule"]
    assert resched is not None
    assert [(l["seg_id"], l["start_minute"]) for l in resched["itinerary"]] == [
        (1, 0), (2, 6),
    ]
    # 只变动 seg2 一段
    assert resched["changed_leg_count"] == 1
    assert resched["changed_legs"][0]["seg_id"] == 2

    # 建议不占位、快照不改写
    same = client.get(f"/api/trips/{trip_id}").json()
    assert [(l["seg_id"], l["start_minute"]) for l in same["itinerary"]] == [
        (1, 0), (2, 2),
    ]


def test_reschedule_excludes_self_group_occupancy(client):
    """自身快照在组内的占用不挡自己的改期（排除自身）。"""
    rev = _two_segments_in_chain(client, t1=2, t2=2)
    conn = connect()
    conn.execute("UPDATE segments SET depth = 4.0 WHERE seg_id = 2")
    conn.commit()
    conn.close()
    plan = _plan(client, {
        "origin": "A", "destination": "C",
        "start_minute": 0, "window_minutes": 30,
        "draft": 6.0, "height": 0.0, "margin": 0.5, "revision": rev,
    })
    r = client.post("/api/trips/confirm", json=plan)
    trip_id = r.json()["trip_id"]
    assert _create_group(client, [1, 2]).status_code == 201

    # seg2 第 3 分钟低潮：原 seg2 占 2,3。改期候选 2 出发占 2,3 -> 3 低潮；
    # 3 出发占 3,4 -> 3 低潮；4 出发占 4,5 可行。自身原占用（seg1 0,1 展开、
    # seg2 2,3）不得挡自己，故 4 可达而非被迫继续后移。
    client.post("/api/tide/corrections", json={"corrections": {3: 0.0}})
    data = client.post(f"/api/trips/{trip_id}/review", json={}).json()
    assert data["safe"] is False
    resched = data["reschedule"]
    assert resched is not None, data
    assert [(l["seg_id"], l["start_minute"]) for l in resched["itinerary"]] == [
        (1, 0), (2, 4),
    ]
