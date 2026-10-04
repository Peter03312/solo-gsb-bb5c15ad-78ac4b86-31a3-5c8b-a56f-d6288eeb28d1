"""已确认行程作废（误确认撤销）测试。

规则：
* 凭行程号 + 非空作废原因作废，返回行程号、原因、作废时间；
* 作废记录按行程号查询：未知行程 TRIP_NOT_FOUND、未作废
  VOID_RECORD_NOT_FOUND、重复作废返回首次记录且不覆盖原因（replayed=true）；
* 已确认行程及原批次确认结果仍作为历史快照可查（GET /api/trips* 不变），
  作废只使其按固化航时计算的航段及狭窄水道组占用从后续调度失效；
* 单船/批次预排、确认复算、改期建议只避开仍有效的快照；
* 作废与确认在 BEGIN IMMEDIATE 事务中串行，不产生相互重叠的有效确认行程；
* 已作废批次行程的原幂等键重放仍返回首次确认结果，不重新产生占用；
* 原行程潮位安全复核仍按历史快照执行。
"""
import json
import threading

import pytest
from pydantic import ValidationError

from app.database import connect
from app.errors import ApiError
from app.main import confirm_trip, void_confirmed_trip
from app.schemas import ConfirmRequest, TripVoidRequest

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
    """直接写入确认快照（绕过确认事务）。"""
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


def _ab(client):
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    return seed_initial_revision(_level(0, 39, 3.0))


def _plan_body(rev, **over):
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# 1. 首次作废 / 重复作废 / 查询
# ---------------------------------------------------------------------------

def test_void_first_then_repeated_returns_first_record(client):
    rev = _ab(client)
    tid = _confirm(client, _plan(client, _plan_body(rev)))["trip_id"]

    r = client.post(f"/api/trips/{tid}/void", json={"reason": "调度员误确认"})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["trip_id"] == tid
    assert data["reason"] == "调度员误确认"
    assert data["voided_at"]
    assert data["replayed"] is False
    first_time = data["voided_at"]

    # 重复作废：不同原因也不覆盖，返回首次记录，HTTP 200 + replayed
    r2 = client.post(f"/api/trips/{tid}/void", json={"reason": "另一个原因"})
    assert r2.status_code == 200, r2.text
    again = r2.json()
    assert again["replayed"] is True
    assert again["reason"] == "调度员误确认"
    assert again["voided_at"] == first_time

    # 查询记录与首次一致
    g = client.get(f"/api/trips/{tid}/void")
    assert g.status_code == 200
    rec = g.json()
    assert set(rec.keys()) == {"trip_id", "reason", "voided_at"}
    assert rec == {"trip_id": tid, "reason": "调度员误确认",
                   "voided_at": first_time}


def test_void_unknown_trip_returns_trip_not_found(client):
    rev = _ab(client)
    r = client.post("/api/trips/999/void", json={"reason": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"
    g = client.get("/api/trips/999/void")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_void_record_of_non_voided_trip_not_found(client):
    rev = _ab(client)
    tid = _confirm(client, _plan(client, _plan_body(rev)))["trip_id"]
    g = client.get(f"/api/trips/{tid}/void")
    assert g.status_code == 404
    assert g.json()["error"]["code"] == "VOID_RECORD_NOT_FOUND"


def test_void_requires_non_empty_reason(client):
    rev = _ab(client)
    tid = _confirm(client, _plan(client, _plan_body(rev)))["trip_id"]

    # 缺字段 / 空串：schema 层 422
    r = client.post(f"/api/trips/{tid}/void", json={})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post(f"/api/trips/{tid}/void", json={"reason": ""})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    # 纯空白：服务层 422 VALIDATION_ERROR
    r = client.post(f"/api/trips/{tid}/void", json={"reason": "   \t "})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    # 未作废成功（查询仍 not found）
    assert client.get(f"/api/trips/{tid}/void").status_code == 404

    # 首尾空白被剥离后保存
    r = client.post(f"/api/trips/{tid}/void",
                    json={"reason": "  误确认，撤销 \n"})
    assert r.status_code == 201
    assert r.json()["reason"] == "误确认，撤销"


# ---------------------------------------------------------------------------
# 2. 历史快照仍可查；作废只释放占用
# ---------------------------------------------------------------------------

def test_void_keeps_snapshot_and_frees_occupancy(client):
    rev = _ab(client)
    plan0 = _plan(client, _plan_body(rev))
    assert [l["start_minute"] for l in plan0["itinerary"]] == [0]
    tid = _confirm(client, plan0)["trip_id"]

    # 占用生效：后续规划最早第 2 分钟进入
    p_blocked = _plan(client, _plan_body(rev))
    assert [l["start_minute"] for l in p_blocked["itinerary"]] == [2]

    # 作废
    r = client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    assert r.status_code == 201

    # 历史快照原样可查，仅多了 voided_at 标识
    snap = client.get(f"/api/trips/{tid}").json()
    assert snap["arrival_minute"] == 2
    assert [l["start_minute"] for l in snap["itinerary"]] == [0]
    assert snap["confirmed_at"]
    assert snap["voided_at"] == r.json()["voided_at"]

    rows = client.get("/api/trips").json()
    assert [t["trip_id"] for t in rows] == [tid]
    assert rows[0]["voided_at"] is not None

    # 占用失效：第 0 分钟重新可规划、可确认
    p_free = _plan(client, _plan_body(rev))
    assert [l["start_minute"] for l in p_free["itinerary"]] == [0]
    t2 = _confirm(client, p_free)["trip_id"]
    assert t2 != tid
    # 新快照未作废，列表中 voided_at 为 null
    rows = client.get("/api/trips").json()
    flags = {t["trip_id"]: t["voided_at"] for t in rows}
    assert flags[tid] is not None and flags[t2] is None


def test_confirm_recalc_after_void_rejects_stale_submission(client):
    """作废后确认复算只避开仍有效的快照：按占用期最优行程回填会 PLAN_MISMATCH。"""
    rev = _ab(client)
    tid = _confirm(client, _plan(client, _plan_body(rev)))["trip_id"]
    # 占用期重新规划：最早第 2 分钟
    stale_plan = _plan(client, _plan_body(rev))
    assert [l["start_minute"] for l in stale_plan["itinerary"]] == [2]

    client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})

    # 占用释放后最优回到第 0 分钟：旧提交（第 2 分钟）复算不一致
    r = client.post("/api/trips/confirm", json=stale_plan)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    # 未新增快照
    assert [t["trip_id"] for t in client.get("/api/trips").json()] == [tid]


# ---------------------------------------------------------------------------
# 3. 狭窄水道组：作废释放整组占用；组建后也不回溯计入已作废快照
# ---------------------------------------------------------------------------

def test_void_frees_expanded_group_occupancy(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    r = client.post("/api/channel-groups", json={"segments": [s1, s2]})
    assert r.status_code == 201

    # 船经 s1 于 0 进入占 0,1 -> 整组封锁，B->C 最早第 2 分钟进入
    tid = _confirm(client, _plan(client, _plan_body(
        rev, origin="A", destination="B")))["trip_id"]
    bc = _plan(client, _plan_body(rev, origin="B", destination="C"))
    assert [l["start_minute"] for l in bc["itinerary"]] == [2]

    # 作废后整组展开占用同步失效
    assert client.post(f"/api/trips/{tid}/void",
                       json={"reason": "误确认"}).status_code == 201
    bc2 = _plan(client, _plan_body(rev, origin="B", destination="C"))
    assert [l["start_minute"] for l in bc2["itinerary"]] == [0]


def test_group_formed_after_void_does_not_include_voided_snapshot(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))

    # 先确认并作废：组建前作废的快照不应回溯计入新组占用
    tid = _confirm(client, _plan(client, _plan_body(
        rev, origin="A", destination="B")))["trip_id"]
    client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})

    r = client.post("/api/channel-groups", json={"segments": [s1, s2]})
    assert r.status_code == 201
    bc = _plan(client, _plan_body(rev, origin="B", destination="C"))
    # 已作废快照不展开到 s2：第 0 分钟即可进入
    assert [l["start_minute"] for l in bc["itinerary"]] == [0]


# ---------------------------------------------------------------------------
# 4. 批次：作废其中一条；原幂等键重放仍返回首次结果且不产生占用
# ---------------------------------------------------------------------------

def test_batch_replay_after_void_returns_first_result_and_no_new_occupancy(client):
    add_node(client, "A")
    add_node(client, "B")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))

    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
    ]
    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": ships})
    assert plan.status_code == 200, plan.text
    body = {
        "revision": rev,
        "idempotency_key": "batch-void-demo-1",
        "ships": [
            {**ships[i], "itinerary": plan.json()["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    trips = r.json()["trips"]
    ids = [t["trip_id"] for t in trips]
    # s1 编号小，船 0 走 s1 第 0 分钟
    assert trips[0]["itinerary"][0]["seg_id"] == s1
    assert trips[0]["itinerary"][0]["start_minute"] == 0

    # 作废船 0（s1 占 0,1）
    t0 = ids[0]
    assert client.post(f"/api/trips/{t0}/void",
                       json={"reason": "批次误确认"}).status_code == 201

    # 原幂等键重放：仍 200/replayed、同行程号，不重新产生占用
    replay = client.post("/api/trips/batch/confirm", json=body)
    assert replay.status_code == 200
    rj = replay.json()
    assert rj["replayed"] is True
    assert [t["trip_id"] for t in rj["trips"]] == ids

    # s1 的 0,1 已随作废释放：新单船规划回到 s1 第 0 分钟
    p = _plan(client, _plan_body(rev))
    assert p["itinerary"][0]["seg_id"] == s1
    assert [l["start_minute"] for l in p["itinerary"]] == [0]

    # 作废前则会避开 s1 0,1：用船 1（s2 未作废）在场时对比——再规划第二艘
    # 仍须避开仍有效的船 1（s2 占 0,1），同刻字典序下 s1 空闲故取 s1@0
    p2 = _plan(client, _plan_body(rev))
    assert p2["itinerary"][0]["seg_id"] == s1
    assert p2["itinerary"][0]["start_minute"] == 0

    # 批次两条快照都仍在历史列表中（一条带 voided_at）
    rows = {t["trip_id"]: t for t in client.get("/api/trips").json()}
    assert set(rows) == set(ids)
    assert rows[t0]["voided_at"] is not None
    assert rows[ids[1]]["voided_at"] is None


def test_batch_confirm_avoids_only_valid_snapshots(client):
    """批次确认复算也只避开仍有效快照：作废后旧批次提交 PLAN_MISMATCH、整批不保存。"""
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))

    # 先占 0,1
    blocker = _confirm(client, _plan(client, _plan_body(rev)))["trip_id"]

    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0},
    ]
    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": ships}).json()
    assert [l["start_minute"] for l in plan["ships"][0]["itinerary"]] == [2]
    body = {
        "revision": rev, "idempotency_key": "batch-void-stale",
        "ships": [
            {**ships[i], "itinerary": plan["ships"][i]["itinerary"]}
            for i in range(2)
        ],
    }

    client.post(f"/api/trips/{blocker}/void", json={"reason": "误确认"})
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    # 整批不保存：列表仍只有（已作废的）blocker
    assert [t["trip_id"] for t in client.get("/api/trips").json()] == [blocker]


# ---------------------------------------------------------------------------
# 5. 原行程潮位安全复核仍按历史快照执行；改期只避开仍有效的他人快照
# ---------------------------------------------------------------------------

def test_review_of_voided_trip_uses_historical_snapshot(client):
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=99.0)
    rev = seed_initial_revision(_level(0, 39, 3.0))
    tid = _confirm(client, _plan(client, _plan_body(
        rev, window_minutes=20)))["trip_id"]
    client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})

    r = client.post(f"/api/trips/{tid}/review",
                    json={"target_revision": rev})
    assert r.status_code == 200
    data = r.json()
    assert data["safe"] is True
    assert data["reschedule"] is None
    assert [l["start_minute"] for l in data["original_itinerary"]] == [0]


def test_reschedule_after_other_trip_voided_uses_freed_slot(client):
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 39, 3.0)})

    # 本快照 5 进入占 5,6,7（吃水 6.5+余量 0.5）
    mine = _raw_insert_trip(
        rev,
        [{"seg_id": 1, "origin": "A", "destination": "B",
          "start_minute": 5, "end_minute": 8, "travel_time": 3}],
        draft=6.0, margin=0.5,
    )
    # 他船占 11,12,13
    other = _raw_insert_trip(
        rev,
        [{"seg_id": 1, "origin": "A", "destination": "B",
          "start_minute": 11, "end_minute": 14, "travel_time": 3}],
        draft=6.0, margin=0.5,
    )
    # 5..10 低潮 -> 原行程违规，改期本应避开他船，最早 14 出发
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 11)}})

    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    assert data["reschedule"]["itinerary"][0]["start_minute"] == 14

    # 作废他船：11..13 释放，改期提前到 11（复核仍按本行程历史快照）
    client.post(f"/api/trips/{other}/void", json={"reason": "误确认"})
    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["safe"] is False
    assert data["reschedule"]["itinerary"][0]["start_minute"] == 11

    # 本行程自身作废后复核结论不变（历史快照口径，他人作废占用同样已释放）
    client.post(f"/api/trips/{mine}/void", json={"reason": "误确认"})
    data = client.post(f"/api/trips/{mine}/review", json={}).json()
    assert data["reschedule"]["itinerary"][0]["start_minute"] == 11
    # 快照未被改写
    g = client.get(f"/api/trips/{mine}").json()
    assert [l["start_minute"] for l in g["itinerary"]] == [5]


# ---------------------------------------------------------------------------
# 6. 并发：作废与确认事务串行，不产生相互重叠的有效确认行程
# ---------------------------------------------------------------------------

def test_concurrent_void_and_confirm_never_overlap(client):
    n = 8
    add_node(client, "A")
    for i in range(n):
        add_node(client, f"B{i}")
        add_segment(client, "A", f"B{i}", travel_time=2, depth=99.0)
    segs = list(range(1, n + 1))
    rev = seed_initial_revision(_level(0, 63, 3.0))

    # 每对节点一条原始快照，占 4i,4i+1；确认提交与其完全重叠的 4i 出发
    raw_ids = []
    for i, seg in enumerate(segs):
        d = 4 * i
        raw_ids.append(_raw_insert_trip(
            rev,
            [{"seg_id": seg, "origin": "A", "destination": f"B{i}",
              "start_minute": d, "end_minute": d + 2, "travel_time": 2}],
            origin="A", destination=f"B{i}", window=64,
        ))

    outcomes = []
    outcomes_lock = threading.Lock()

    # 每对（作废原始快照 / 确认重叠新行程）用独立 barrier 同步并发
    for i, (tid, seg) in enumerate(zip(raw_ids, segs)):
        bar = threading.Barrier(2)
        tv = threading.Thread(target=_void_thread,
                              args=(tid, bar, outcomes, outcomes_lock))
        tc = threading.Thread(target=_confirm_thread,
                              args=(i, seg, tid, rev, bar, outcomes,
                                    outcomes_lock))
        tv.start()
        tc.start()
        tv.join()
        tc.join()

    # 作废全部成功；确认要么成功（原始快照必已先作废，返回新 trip_id），
    # 要么 PLAN_MISMATCH（确认先于作废，重叠提交不成立）
    assert sum(1 for o in outcomes if o[0] == "void") == n
    confirm_ok = [o for o in outcomes if o[0] == "confirm"]
    confirm_bad = [o for o in outcomes if o[0] == "confirm-err"]
    assert all(o[1] == "PLAN_MISMATCH" for o in confirm_bad)
    assert len(confirm_ok) + len(confirm_bad) == n
    new_ids = {o[2] for o in confirm_ok}

    # 核心不变量：任意两条仍有效（未作废）确认行程在同一航段上占用互不相交
    conn = connect()
    rows = conn.execute(
        "SELECT c.trip_id AS trip_id, c.itinerary AS itinerary FROM"
        " confirmed_trips c LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
        " WHERE v.trip_id IS NULL"
    ).fetchall()
    import json
    per_seg: dict[int, list[set]] = {}
    valid_ids = set()
    for row in rows:
        valid_ids.add(row["trip_id"])
        for leg in json.loads(row["itinerary"]):
            mins = set(range(leg["start_minute"],
                             leg["start_minute"] + leg["travel_time"]))
            per_seg.setdefault(leg["seg_id"], []).append(mins)
    conn.close()
    for seg_id, sets in per_seg.items():
        for a in range(len(sets)):
            for b in range(a + 1, len(sets)):
                assert not (sets[a] & sets[b]), (seg_id, sets[a], sets[b])

    # 串行顺序下：全部原始快照均已作废；成功确认产生的新行程仍有效
    conn = connect()
    still_valid = {
        r["trip_id"] for r in conn.execute(
            "SELECT c.trip_id AS trip_id FROM confirmed_trips c"
            " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
            " WHERE v.trip_id IS NULL").fetchall()
    }
    conn.close()
    assert not (set(raw_ids) & still_valid)
    assert new_ids <= still_valid
    assert len(valid_ids - set(raw_ids)) == len(confirm_ok)


def _void_thread(tid, bar, outcomes, lock):
    bar.wait()
    try:
        resp = void_confirmed_trip(tid, TripVoidRequest(reason="并发作废"))
        with lock:
            outcomes.append(("void", resp.status_code))
    except ApiError as exc:
        with lock:
            outcomes.append(("void-err", exc.code))


def _confirm_thread(i, seg, tid, rev, bar, outcomes, lock):
    d = 4 * i
    body = ConfirmRequest(
        origin="A", destination=f"B{i}", start_minute=0,
        window_minutes=64, draft=1.0, height=0.0, margin=0.0,
        revision=rev,
        itinerary=[{"seg_id": seg, "origin": "A", "destination": f"B{i}",
                    "start_minute": d, "end_minute": d + 2, "travel_time": 2}],
    )
    bar.wait()
    try:
        # 直接调用路由：成功返回 TripOut（非 HTTP 响应），即确认在作废之后
        result = confirm_trip(body)
        with lock:
            outcomes.append(("confirm", 201, getattr(result, "trip_id", None)))
    except ApiError as exc:
        # 确认在作废之前 -> 提交行程与当时最优重叠，PLAN_MISMATCH
        with lock:
            outcomes.append(("confirm-err", exc.code, tid))


# ---------------------------------------------------------------------------
# 7. 并发竞争：同一行程两个作废请求，恰好一条首次记录
# ---------------------------------------------------------------------------

def test_concurrent_double_void_single_record(client):
    rev = _ab(client)
    tid = _confirm(client, _plan(client, _plan_body(rev)))["trip_id"]
    bar = threading.Barrier(2)
    results = []

    def v(reason):
        bar.wait()
        resp = void_confirmed_trip(tid, TripVoidRequest(reason=reason))
        results.append((resp.status_code, json.loads(resp.body)))

    t1 = threading.Thread(target=v, args=("原因一",))
    t2 = threading.Thread(target=v, args=("原因二",))
    t1.start(); t2.start(); t1.join(); t2.join()

    # 一条 201 首次、一条 200 重放，两条响应内容一致（首次记录）
    codes = sorted(r[0] for r in results)
    assert codes == [200, 201]
    bodies = [r[1] for r in results]
    assert bodies[0]["trip_id"] == bodies[1]["trip_id"] == tid
    assert bodies[0]["voided_at"] == bodies[1]["voided_at"]
    assert bodies[0]["reason"] == bodies[1]["reason"]

    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM voided_trips WHERE trip_id = ?",
                     (tid,)).fetchone()[0]
    rec = conn.execute(
        "SELECT reason FROM voided_trips WHERE trip_id = ?", (tid,)
    ).fetchone()
    conn.close()
    assert n == 1
    assert rec["reason"] in ("原因一", "原因二")


def test_void_request_pydantic_model():
    with pytest.raises(ValidationError):
        TripVoidRequest(reason="")
