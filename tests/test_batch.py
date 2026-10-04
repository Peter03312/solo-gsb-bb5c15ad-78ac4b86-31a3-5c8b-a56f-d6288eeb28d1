"""同潮位修订下批次预排 / 确认测试。

规则：
* 批次为按优先顺序排列的 2～5 艘船，整批共用一个潮位修订号；
* 预排只读不占位：后船避开已确认占用与本批前船拟占用，逐艘沿用
  最早抵达 + 同刻航段序列字典序择优；任一船无解给出首个失败序号；
* 确认在单事务内核订最新修订与当时占用，逐艘复算逐段比对：
  REVISION_STALE / PLAN_MISMATCH 均全批不保存；
* 成功一次写入全部不可变快照；幂等键：同键同内容重放原结果（即便
  修订已更新），同键异内容 BATCH_KEY_CONFLICT；
* 旧单船接口语义保持不变。
"""
import json

from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _ship(origin="A", destination="B", start=0, window=20,
          draft=1.0, height=0.0, margin=0.0):
    return {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "draft": draft, "height": height, "margin": margin,
    }


def _raw_insert_trip(rev, itinerary, origin="A", destination="B",
                     start=0, window=20, draft=1.0, height=0.0, margin=0.0):
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


def _batch_plan(client, ships, rev=None):
    body = {"ships": ships}
    if rev is not None:
        body["revision"] = rev
    r = client.post("/api/trips/batch/plan", json=body)
    return r


def _batch_confirm(client, plan, key, rev=None):
    body = {
        "ships": [
            {**{k: s[k] for k in ("origin", "destination", "start_minute",
                                  "window_minutes", "draft", "height", "margin")},
             "itinerary": s["itinerary"]}
            for s in plan["ships"]
        ],
        "revision": rev if rev is not None else plan["revision"],
        "idempotency_key": key,
    }
    return client.post("/api/trips/batch/confirm", json=body)


# ---------------------------------------------------------------------------
# 1. 预排：后船避开前船拟占用；只读不占位
# ---------------------------------------------------------------------------

def test_batch_plan_ships_avoid_preceding_tentative_occupancy(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    r = _batch_plan(client, [_ship(), _ship()], rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["revision"] == rev
    assert [s["index"] for s in data["ships"]] == [0, 1]
    # 船 0：0 进入占 0,1；船 1 须避开拟占用，最早 2 进入
    assert [l["start_minute"] for l in data["ships"][0]["itinerary"]] == [0]
    assert [l["start_minute"] for l in data["ships"][1]["itinerary"]] == [2]
    assert data["ships"][1]["arrival_minute"] == 4

    # 预排不占位：没有任何已确认行程
    assert client.get("/api/trips").json() == []
    # 再次预排结果一致
    assert _batch_plan(client, [_ship(), _ship()], rev).json() == data


def test_batch_plan_three_ships_chain(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    data = _batch_plan(client, [_ship(), _ship(), _ship()], rev).json()
    assert [[l["start_minute"] for l in s["itinerary"]] for s in data["ships"]] == \
        [[0], [2], [4]]


def test_batch_plan_avoids_confirmed_and_tentative(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 已确认占用 0,1（2 抵达）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    data = _batch_plan(client, [_ship(), _ship()], rev).json()
    # 船 0 避开已确认：2 进入；船 1 再避开船 0 拟占用：4 进入
    assert [l["start_minute"] for l in data["ships"][0]["itinerary"]] == [2]
    assert [l["start_minute"] for l in data["ships"][1]["itinerary"]] == [4]


# ---------------------------------------------------------------------------
# 2. 任一船无解：指出首个失败序号
# ---------------------------------------------------------------------------

def test_batch_plan_first_failure_index(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    # 已确认占用 0,1（2 抵达）：船 0 最早 2 出发，船 1 本可 4 出发；
    # 再占 4..19 后船 1 窗口内无解，船 0 仍可 2 出发
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 4, "end_minute": 20, "travel_time": 16},
    ])
    r = _batch_plan(client, [_ship(), _ship()], rev)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NO_FEASIBLE_ITINERARY"
    assert err["details"]["ship_index"] == 1

    # 连 2,3 也占满 -> 首船即无解，ship_index=0
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    r = _batch_plan(client, [_ship(), _ship()], rev)
    assert r.status_code == 404
    assert r.json()["error"]["details"]["ship_index"] == 0


def test_batch_plan_unknown_node_indexed(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    r = _batch_plan(client, [_ship(), _ship(destination="ZZ")], rev)
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NODE_NOT_FOUND"
    assert err["details"]["ship_index"] == 1


def test_batch_plan_revision_defaults_to_latest(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    data = _batch_plan(client, [_ship(), _ship()]).json()
    assert data["revision"] == rev

    r = client.post("/api/tide/corrections",
                    json={"corrections": {5: 3.0}, "note": "n"})
    new_rev = r.json()["revision"]
    data = _batch_plan(client, [_ship(), _ship()]).json()
    assert data["revision"] == new_rev


# ---------------------------------------------------------------------------
# 3. 数量 / 字段不合法：VALIDATION_ERROR（422）
# ---------------------------------------------------------------------------

def test_batch_validation_ship_count(client):
    rev = seed_initial_revision(_level(0, 19, 3.0))
    for ships in ([_ship()], [_ship() for _ in range(6)]):
        r = client.post("/api/trips/batch/plan", json={"ships": ships, "revision": rev})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_batch_validation_bad_fields_and_key(client):
    rev = seed_initial_revision(_level(0, 19, 3.0))
    bad = _ship()
    bad["window_minutes"] = 0
    r = _batch_plan(client, [bad, _ship()], rev)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    # 确认缺幂等键 / revision 非 int -> 422
    r = client.post("/api/trips/batch/confirm", json={
        "ships": [], "revision": rev,
    })
    assert r.status_code == 422
    r = client.post("/api/trips/batch/confirm", json={
        "ships": [_ship(), _ship()], "revision": rev,
    })
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 4. 批次确认成功：一次保存全部快照；后船避开同批前船
# ---------------------------------------------------------------------------

def test_batch_confirm_saves_all_snapshots(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    r = _batch_confirm(client, plan, key="batch-1")
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["replayed"] is False
    assert data["revision"] == rev
    assert [t["index"] for t in data["trips"]] == [0, 1]
    assert [l["start_minute"] for l in data["trips"][0]["itinerary"]] == [0]
    assert [l["start_minute"] for l in data["trips"][1]["itinerary"]] == [2]
    ids = [t["trip_id"] for t in data["trips"]]
    assert ids[0] != ids[1]

    trips = client.get("/api/trips").json()
    assert {t["trip_id"] for t in trips} == set(ids)
    # 单船接口可读到各快照
    for tid in ids:
        assert client.get(f"/api/trips/{tid}").status_code == 200


def test_batch_confirm_occupancy_visible_to_later_planners(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    _batch_confirm(client, plan, key="b")

    # 批次后再单船规划：0..3 已被两船占满，最早 4 进入
    r = client.post("/api/trips/plan", json={**_ship(), "revision": rev})
    assert r.status_code == 200
    assert [l["start_minute"] for l in r.json()["itinerary"]] == [4]


# ---------------------------------------------------------------------------
# 5. PLAN_MISMATCH：占用变化 / 篡改提交行程；全批不保存
# ---------------------------------------------------------------------------

def test_batch_confirm_mismatch_on_occupancy_change(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    # 预排后、确认前，他船抢占船 0 的 0,1
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ])
    r = _batch_confirm(client, plan, key="bm")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 0
    # 全批不保存：只有抢占的那一条快照
    assert len(client.get("/api/trips").json()) == 1


def test_batch_confirm_mismatch_when_second_ship_blocks(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    # 抢占船 1 预排得到的 2,3（船 0 仍可在 0 出发）
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ])
    r = _batch_confirm(client, plan, key="bm2")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    # 船 0 复算仍为 0 出发；船 1 复算变为 4 出发，与提交不一致
    assert err["details"]["ship_index"] == 1
    assert len(client.get("/api/trips").json()) == 1


def test_batch_confirm_mismatch_on_tampered_itinerary(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    # 篡改船 1 的出发分钟（2 -> 3），其占用 3,4 与船 0 的拟占用无冲突但非最优
    plan["ships"][1]["itinerary"][0]["start_minute"] = 3
    plan["ships"][1]["itinerary"][0]["end_minute"] = 5
    r = _batch_confirm(client, plan, key="bt")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLAN_MISMATCH"
    assert client.get("/api/trips").json() == []


def test_batch_confirm_mismatch_no_solution_with_index(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 3, 3.0))  # 窗口只有 0..3 有潮位

    plan = _batch_plan(client, [_ship(window=4), _ship(window=4)], rev).json()
    assert [l["start_minute"] for l in plan["ships"][0]["itinerary"]] == [0]
    assert [l["start_minute"] for l in plan["ships"][1]["itinerary"]] == [2]
    # 抢占 0,1 与 2,3 -> 两船复算均无解；首个失败为船 0
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 0, "end_minute": 2, "travel_time": 2},
    ], window=4)
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 2, "end_minute": 4, "travel_time": 2},
    ], window=4)
    r = _batch_confirm(client, plan, key="bn", rev=rev)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["ship_index"] == 0
    assert err["details"]["reason"] == "no_feasible_itinerary"
    assert len(client.get("/api/trips").json()) == 2


# ---------------------------------------------------------------------------
# 6. REVISION_STALE：修订过期；全批不保存
# ---------------------------------------------------------------------------

def test_batch_confirm_revision_stale(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    # 新修订（哪怕内容完全相同）也使旧号过期
    r = client.post("/api/tide/corrections",
                    json={"corrections": {0: 3.0}, "note": "noop"})
    new_rev = r.json()["revision"]
    r = _batch_confirm(client, plan, key="stale-1")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "REVISION_STALE"
    assert err["details"]["provided_revision"] == rev
    assert err["details"]["current_revision"] == new_rev
    assert client.get("/api/trips").json() == []

    # 用新修订重新预排后可确认
    plan2 = _batch_plan(client, [_ship(), _ship()], new_rev).json()
    assert _batch_confirm(client, plan2, key="stale-2").status_code == 201


# ---------------------------------------------------------------------------
# 7. 幂等：同键同内容重放（即使修订已更新）；同键异内容冲突
# ---------------------------------------------------------------------------

def test_batch_idempotent_replay_same_content(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    first = _batch_confirm(client, plan, key="idem-1")
    assert first.status_code == 201
    first_data = first.json()
    first_ids = [t["trip_id"] for t in first_data["trips"]]
    assert len(client.get("/api/trips").json()) == 2

    # 修订更新后，同键同内容重试仍返回原结果（不重算、不新增）
    client.post("/api/tide/corrections",
                json={"corrections": {0: 2.5}, "note": "x"})
    r = _batch_confirm(client, plan, key="idem-1")
    assert r.status_code == 200
    replay = r.json()
    assert replay["replayed"] is True
    assert [t["trip_id"] for t in replay["trips"]] == first_ids
    assert len(client.get("/api/trips").json()) == 2

    # 占用变化也不影响重放：抢占后重试仍返回原结果
    _raw_insert_trip(rev, [
        {"seg_id": 1, "origin": "A", "destination": "B",
         "start_minute": 4, "end_minute": 6, "travel_time": 2},
    ])
    r = _batch_confirm(client, plan, key="idem-1")
    assert r.status_code == 200
    assert [t["trip_id"] for t in r.json()["trips"]] == first_ids
    assert len(client.get("/api/trips").json()) == 3


def test_batch_idempotency_same_key_different_content_conflict(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan = _batch_plan(client, [_ship(), _ship()], rev).json()
    assert _batch_confirm(client, plan, key="k").status_code == 201

    # 同键、改一艘船的参数 -> BATCH_KEY_CONFLICT
    plan2 = _batch_plan(client, [_ship(draft=2.0), _ship()], rev).json()
    r = _batch_confirm(client, plan2, key="k")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "BATCH_KEY_CONFLICT"
    assert err["details"]["idempotency_key"] == "k"
    # 冲突不新增快照
    assert len(client.get("/api/trips").json()) == 2

    # 同键、换修订号（提交行程恰好仍可行）-> 同样冲突
    client.post("/api/tide/corrections",
                json={"corrections": {m: 3.0 for m in range(0, 20)}, "note": "flat"})
    new_rev = rev + 1
    r = _batch_confirm(client, plan, key="k", rev=new_rev)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BATCH_KEY_CONFLICT"


def test_batch_distinct_keys_each_confirmed_once(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    plan_a = _batch_plan(client, [_ship(), _ship()], rev).json()
    assert _batch_confirm(client, plan_a, key="ka").status_code == 201
    # 同样内容、不同键：作为新批次重新复算，须避开首批占用（船 0 变为 4 出发）
    plan_b = _batch_plan(client, [_ship(), _ship()], rev).json()
    assert [l["start_minute"] for l in plan_b["ships"][0]["itinerary"]] == [4]
    r = _batch_confirm(client, plan_b, key="kb")
    assert r.status_code == 201
    assert len(client.get("/api/trips").json()) == 4


# ---------------------------------------------------------------------------
# 8. 5 艘船上限 & 不同航路互不干扰（同一航段上后船仍须避开前船）
# ---------------------------------------------------------------------------

def test_batch_five_ships_distinct_segments(client):
    for n in ["A", "B", "C"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "A", "C", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 29, 3.0))

    ships = [
        _ship(destination="B") if i % 2 == 0 else _ship(destination="C")
        for i in range(5)
    ]
    data = _batch_plan(client, ships, rev).json()
    assert len(data["ships"]) == 5
    # 优先顺序 B,C,B,C,B：
    #   s1 被三艘去 B 的船依次占 0,1 / 2,3 / 4,5 -> 出发 0/2/4
    #   s2 被两艘去 C 的船依次占 0,1 / 2,3       -> 出发 0/2
    # 两条航段互不干扰（各自独立计数）
    b_deps = [s["itinerary"][0]["start_minute"]
              for s in data["ships"] if s["destination"] == "B"]
    c_deps = [s["itinerary"][0]["start_minute"]
              for s in data["ships"] if s["destination"] == "C"]
    assert b_deps == [0, 2, 4]
    assert c_deps == [0, 2]
    assert all(s["itinerary"][0]["seg_id"] == s1
               for s in data["ships"] if s["destination"] == "B")
    assert all(s["itinerary"][0]["seg_id"] == s2
               for s in data["ships"] if s["destination"] == "C")


# ---------------------------------------------------------------------------
# 9. 旧单船接口语义保持
# ---------------------------------------------------------------------------

def test_legacy_single_ship_endpoints_unchanged(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))

    r = client.post("/api/trips/plan", json={**_ship(), "revision": rev})
    assert r.status_code == 200
    plan = r.json()
    assert [l["start_minute"] for l in plan["itinerary"]] == [0]
    r = client.post("/api/trips/confirm", json=plan)
    assert r.status_code == 201
    assert r.json()["trip_id"] >= 1

    # 单船确认不传 revision 仍为 422 VALIDATION_ERROR
    body = {k: v for k, v in plan.items() if k != "revision"}
    r = client.post("/api/trips/confirm", json=body)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 10. 正航时回路：批次预排 / 确认复算与单船一致，不剪较长同刻前缀
# ---------------------------------------------------------------------------

def test_batch_positive_time_loop_matches_single(client):
    for n in ["A", "B", "C", "D"]:
        add_node(client, n)
    s1 = add_segment(client, "A", "B", 1)
    s2 = add_segment(client, "B", "C", 1)
    s3 = add_segment(client, "C", "B", 1)
    s4 = add_segment(client, "B", "D", 1, depth=0.5)
    levels = {m: 0.0 for m in range(0, 3)}
    levels[3] = 3.0
    rev = seed_initial_revision(levels)

    ship = {
        "origin": "A", "destination": "D",
        "start_minute": 0, "window_minutes": 4,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
    }

    # 单船预排：(1,2,3,4)，分钟 4 抵达
    single = client.post("/api/trips/plan", json={**ship, "revision": rev})
    assert single.status_code == 200, single.text
    assert [l["seg_id"] for l in single.json()["itinerary"]] == [s1, s2, s3, s4]

    # 批次预排：同一艘船（2 艘下限），结果与单船一致
    other = {"origin": "A", "destination": "B",
             "start_minute": 0, "window_minutes": 4,
             "draft": 1.0, "height": 0.0, "margin": 0.0}
    r = _batch_plan(client, [ship, other], rev)
    assert r.status_code == 200, r.text
    plan = r.json()
    assert [l["seg_id"] for l in plan["ships"][0]["itinerary"]] == [s1, s2, s3, s4]
    assert plan["ships"][0]["arrival_minute"] == 4

    # 批次确认：复算与预排一致（前船拟占用不影响自身结论），快照按新结果固化
    r = _batch_confirm(client, plan, "loop-key-1", rev)
    assert r.status_code == 201, r.text
    confirmed = r.json()
    assert [l["seg_id"] for l in confirmed["trips"][0]["itinerary"]] == \
        [s1, s2, s3, s4]

    # 同键同内容重放：200 + replayed=true，结果不变
    r = _batch_confirm(client, plan, "loop-key-1", rev)
    assert r.status_code == 200
    assert r.json()["replayed"] is True
    assert [l["seg_id"] for l in r.json()["trips"][0]["itinerary"]] == \
        [s1, s2, s3, s4]
