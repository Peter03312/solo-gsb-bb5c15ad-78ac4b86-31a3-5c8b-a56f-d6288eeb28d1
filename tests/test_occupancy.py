"""已确认行程的航段独占占用测试。

规则：
* 每段行程从进入分钟到抵达前一分钟独占该航段（d..d+t-1）；
* 既有已确认快照即使彼此重叠也原样保留，后续规划/确认必须避开它们；
* 确认在同一事务内以最新潮位修订与当时占用重新计算，占用变化使提交行程
  不再最优时 PLAN_MISMATCH；
* 节点等待不占用航段；不同航段互不相干；
* 复核原快照仍只按目标修订检查潮位；改期建议避开其他快照占用但排除自身，
  建议不占位、快照不改写；窗口内无解沿用 no_solution。
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


def _confirm(client, plan):
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201, r.text
    return r.json()


def _raw_insert_trip(rev, itinerary, origin="A", destination="B",
                     start=0, window=20, draft=1.0, height=0.0, margin=0.0):
    """直接写入一个已确认快照（绕过确认事务），用于构造重叠占用。"""
    conn = connect()
    arrival = start if not itinerary else itinerary[-1]["end_minute"]
    cur = conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (rev, origin, destination, start, window, draft, height, margin,
         arrival, json.dumps(itinerary)),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trip_id


# ---------------------------------------------------------------------------
# 1. 规划避开已确认占用：等待到占用结束后进入
# ---------------------------------------------------------------------------

def test_plan_avoids_confirmed_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 既有快照：占 3,4（3 进入、5 抵达）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 3, "end_minute": 5, "travel_time": 2},
    ])

    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    # 0/1 可行 -> 0 进入本应抵达 2；但 3,4 只影响第 3 分钟起的进入，
    # 0 进入占 0,1 不冲突，故最早仍是 0 出发
    assert [l["start_minute"] for l in plan["itinerary"]] == [0]

    # 起始分钟推到 2：2 进入占 2,3（3 冲突）、3 进入占 3,4（冲突）、
    # 4 进入占 4,5（4 冲突），最早 5 进入
    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 2, "window_minutes": 18,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in plan["itinerary"]] == [5]
    assert plan["arrival_minute"] == 7


# ---------------------------------------------------------------------------
# 2. 抵达分钟不占用航段：后船可在前船抵达的同一分钟进入
# ---------------------------------------------------------------------------

def test_arrival_minute_is_not_occupied(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 前船占 0,1，2 抵达
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    # 0/1 进入均冲突，2 进入占 2,3 不冲突
    assert [l["start_minute"] for l in plan["itinerary"]] == [2]
    assert plan["arrival_minute"] == 4


# ---------------------------------------------------------------------------
# 3. 不同航段互不影响
# ---------------------------------------------------------------------------

def test_occupancy_is_per_segment(client):
    add_node(client, "A"); add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # s1 全程占满 0..19；s2 空闲
    _raw_insert_trip(rev, [
        {"seg_id": s1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 20, "travel_time": 20},
    ])
    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    # 无占用时 s1 编号小优先；s1 不可用后改走 s2（同到达时刻仍按序列择优）
    assert [l["seg_id"] for l in plan["itinerary"]] == [s2]
    assert plan["arrival_minute"] == 2


# ---------------------------------------------------------------------------
# 4. 多段：在节点等待，第二段避开占用
# ---------------------------------------------------------------------------

def test_multi_leg_wait_for_segment_occupancy(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    add_segment(client, "B", "C", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 另一艘船占 B->C 的第 3 分钟
    _raw_insert_trip(rev, [
        {"seg_id": 2, "origin": "B", "destination": "C",
         "start_minute": 3, "end_minute": 4, "travel_time": 1},
    ], origin="B", destination="C")

    plan = _plan(client, {
        "origin": "A", "destination": "C",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    # 第 1 段抵达 B 后，第 2 段 2 出发占 2,3（3 冲突）、3 出发占 3,4
    # （冲突），最早 4 出发占 4,5；总抵达 6。至于是"先走再在 B 等"还是
    # "在 A 等再走"，规划择优规则（最早抵达 + 航段序列）不区分，只断言
    # 占用约束相关事实：
    assert [l["seg_id"] for l in plan["itinerary"]] == [1, 2]
    assert plan["itinerary"][1]["start_minute"] == 4
    assert plan["arrival_minute"] == 6
    # 全程没有任何占用分钟与历史快照重叠
    for leg in plan["itinerary"]:
        mins = range(leg["start_minute"], leg["end_minute"])
        if leg["seg_id"] == 2:
            assert 3 not in mins


# ---------------------------------------------------------------------------
# 5. 既有快照彼此重叠也原样保留：重叠部分对后续规划仍然封闭
# ---------------------------------------------------------------------------

def test_overlapping_legacy_snapshots_both_block(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 两条彼此重叠的历史快照（占 0,1 与 1,2）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 1, "end_minute": 3, "travel_time": 2},
    ])
    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    # 0/1/2 进入均与既有占用重叠，最早 3 进入
    assert [l["start_minute"] for l in plan["itinerary"]] == [3]


# ---------------------------------------------------------------------------
# 6. 确认事务：以当时占用重算；占用变化后旧规划提交 -> PLAN_MISMATCH
# ---------------------------------------------------------------------------

def test_confirm_uses_current_occupancy_mismatch(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in plan["itinerary"]] == [0]

    # 另一艘船先确认占 0,1（同修订、彼此重叠也照存）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])

    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    # 冲突不保存：仍是那一条历史快照
    assert [t["trip_id"] for t in client.get("/api/trips").json()] == [1]

    # 重新规划避开占用后可确认（同一最新修订）
    replan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in replan["itinerary"]] == [2]
    trip2 = _confirm(client, replan)
    assert [l["start_minute"] for l in trip2["itinerary"]] == [2]
    assert len(client.get("/api/trips").json()) == 2


def test_confirm_mismatch_when_occupancy_makes_no_solution(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    # 窗口 20：先把潮位限制在 0..3（4 起进入即缺潮位），唯一可行进入为 0..2
    rev = seed_initial_revision(_level(0, 3, 3.0))

    plan = _plan(client, {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 4,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert [l["start_minute"] for l in plan["itinerary"]] == [0]

    # 历史快照占 0,1（另一条占 2,3 会导致 2 进入也冲突，窗口内无解）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])

    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json().__len__() == 2


# ---------------------------------------------------------------------------
# 7. 规划接口本身不占位（只读）：连续两次规划结果相同
# ---------------------------------------------------------------------------

def test_plan_is_read_only(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    }
    p1 = _plan(client, body)
    p2 = _plan(client, body)
    assert p1["itinerary"] == p2["itinerary"]
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 8. 复核原快照只看潮位：与其他快照占用重叠不判违规
# ---------------------------------------------------------------------------

def test_review_ignores_occupancy_overlap(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 本快照占 2,3；另有历史快照占 2,3（重叠）
    other = _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    mine = _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    data = client.post(f"/api/trips/{mine}/review",
                       json={"target_revision": rev}).json()
    # 潮位仍安全 -> safe=true，占用重叠不影响复核结论
    assert data["safe"] is True
    assert data["reschedule"] is None


# ---------------------------------------------------------------------------
# 9. 改期建议避开其他快照占用（含与自身重叠的历史快照，仅排除自身）
# ---------------------------------------------------------------------------

def test_reschedule_avoids_other_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    # 潮位：5..19 足够；本快照 5 进入占 5,6,7
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    mine = _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    # 潮位更正：5..10 低潮（原行程违规），11 起可行
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 11)}})
    # 另一艘船在第 11、12 分钟占用该航段（11 进入占 11,12,13）
    other = _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 11, "end_minute": 14, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    assert data["no_solution"] is False
    rs = data["reschedule"]
    # 若无占用：11 出发；避开 11,12,13 后最早 14 出发
    assert rs["changed_leg_count"] == 1
    assert rs["itinerary"][0]["start_minute"] == 14
    assert rs["arrival_minute"] == 17

    # 建议不占位：再复核一次结果相同；两条快照都不改写
    data2 = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data2["reschedule"]["itinerary"] == rs["itinerary"]
    trips = {t["trip_id"]: t for t in client.get("/api/trips").json()}
    assert [l["start_minute"] for l in trips[mine]["itinerary"]] == [5]
    assert [l["start_minute"] for l in trips[other]["itinerary"]] == [11]


# ---------------------------------------------------------------------------
# 10. 改期排除自身：允许落在"仅自身历史占用、潮位已恢复"的分钟
# ---------------------------------------------------------------------------

def test_reschedule_self_exclusion_allows_original_slot_when_only_self_blocks(client):
    # 单段：原快照 5 出发占 5,6,7。更正使 5 低潮 -> 原行程违规，
    # 5..10 低潮、11 起恢复。
    # 排除自身时：自身占用 5,6,7 本就因低潮不可行，11 可行；
    # 关键：当其他船不挡时，改期建议允许落在"仅自身历史占用、但潮位恢复"
    # 的分钟（8,9,10）——若错误地不排除自身，这些分钟仍会被挡。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    mine = _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    # 只把第 5 分钟改成低潮：原 5 出发违规；6 起潮位恢复。
    client.post("/api/tide/corrections", json={"corrections": {5: 0.0}})
    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    # 6 出发占 6,7,8——其中 6,7 曾是自身占用；排除自身后可行（最早）
    assert rs["itinerary"][0]["start_minute"] == 6
    assert rs["changed_leg_count"] == 1


# ---------------------------------------------------------------------------
# 11. 改期窗口内被他船占满：沿用现有 no_solution 结果
# ---------------------------------------------------------------------------

def test_reschedule_no_solution_due_to_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    mine = _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 5, "end_minute": 8, "travel_time": 3},
    ], draft=6.0, margin=0.5)

    # 潮位：5..7 低潮，8 起恢复
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 8)}})
    # 他船把 8..19 全部占满（任意 3 分钟进入都会撞上）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 8, "end_minute": 20, "travel_time": 12},
    ], draft=6.0, margin=0.5)

    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    assert data["reschedule"] is None
    assert data["no_solution"] is True
    # 快照不改写
    g = client.get(f"/api/trips/{mine}").json()
    assert [l["start_minute"] for l in g["itinerary"]] == [5]


# ---------------------------------------------------------------------------
# 12. 规划因占用窗口内无解：沿用现有 NO_FEASIBLE_ITINERARY
# ---------------------------------------------------------------------------

def test_plan_no_feasible_due_to_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 20, "travel_time": 20},
    ])
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    })
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_FEASIBLE_ITINERARY"
