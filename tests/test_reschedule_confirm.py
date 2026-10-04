"""不安全航次改期确认（原行程作废 + 新行程确认，单事务幂等）测试。"""
from app.database import connect

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _plan_confirm(client, rev, draft=1.0, height=0.0, margin=0.0,
                  start=0, window=20, origin="A", destination="B",
                  ship_no=None, extra=None):
    body = {
        "origin": origin, "destination": destination,
        "start_minute": start, "window_minutes": window,
        "revision": rev,
    }
    if ship_no is not None:
        body["ship_no"] = ship_no
    else:
        body.update({"draft": draft, "height": height, "margin": margin})
    if extra:
        body.update(extra)
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    planned = r.json()
    confirm_body = {
        "origin": body["origin"], "destination": body["destination"],
        "start_minute": body["start_minute"],
        "window_minutes": body["window_minutes"],
        "revision": rev,
        "itinerary": planned["itinerary"],
        "arrival_minute": planned["arrival_minute"],
    }
    # 保留泊位等可选字段；船号模式带船号/版本，直接参数模式带三项参数
    for k in ("berth_id", "work_minutes"):
        if planned.get(k) is not None:
            confirm_body[k] = planned[k]
    if ship_no is not None:
        confirm_body["ship_no"] = ship_no
        confirm_body["ship_version"] = planned["ship_version"]
    else:
        confirm_body.update({"draft": draft, "height": height, "margin": margin})
    r = client.post("/api/trips/confirm", json=confirm_body)
    assert r.status_code == 201, r.text
    return r.json()


def _make_unsafe(client, corrections):
    r = client.post("/api/tide/corrections",
                    json={"corrections": corrections})
    assert r.status_code == 201, r.text
    return r.json()["revision"]


def _review(client, trip_id, rev):
    r = client.post(f"/api/trips/{trip_id}/review",
                    json={"target_revision": rev})
    assert r.status_code == 200, r.text
    return r.json()


def _reschedule(client, trip_id, rev, departures, key="resched-key-1"):
    return client.post(
        f"/api/trips/{trip_id}/reschedule",
        json={"target_revision": rev,
              "departure_minutes": departures,
              "idempotency_key": key},
    )


def _do_reschedule(client, trip, rev, key="resched-key-1"):
    """复核取建议 -> 按建议逐段出发分钟确认改期；返回 (响应json, 复核json)。"""
    review = _review(client, trip["trip_id"], rev)
    assert review["safe"] is False
    assert review["reschedule"] is not None
    deps = [l["start_minute"] for l in review["reschedule"]["itinerary"]]
    r = _reschedule(client, trip["trip_id"], rev, deps, key)
    assert r.status_code == 201, r.text
    return r.json(), review


# ---------------------------------------------------------------------------
# 1. 基本成功路径：原行程不安全 -> 改期确认 -> 旧作废、新生效
# ---------------------------------------------------------------------------

def test_reschedule_confirm_happy_path(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in trip["itinerary"]] == [5]

    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    data, review = _do_reschedule(client, trip, rev2)

    rs = review["reschedule"]
    assert data["old_trip_id"] == trip["trip_id"]
    assert data["target_revision"] == rev2
    assert data["snapshot_revision"] == rev1
    assert data["replayed"] is False
    assert data["reversal_created"] is False
    assert data["reversal"] is None
    assert data["reschedule"] == rs
    assert data["reschedule"]["itinerary"][0]["start_minute"] == 8
    assert data["old_void"]["trip_id"] == trip["trip_id"]
    assert str(data["new_trip_id"]) in data["old_void"]["reason"]

    # 旧行程：快照仍可查、voided_at 非空
    old = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert old["voided_at"] is not None
    assert old["itinerary"] == trip["itinerary"]

    # 新行程：未作废、修订为目标修订、行程为复算结果
    new = client.get(f"/api/trips/{data['new_trip_id']}").json()
    assert new["voided_at"] is None
    assert new["revision"] == rev2
    assert new["itinerary"] == rs["itinerary"]
    assert new["arrival_minute"] == rs["arrival_minute"]

    # 作废记录可经既有接口查询，原因一致
    v = client.get(f"/api/trips/{trip['trip_id']}/void").json()
    assert v["reason"] == data["old_void"]["reason"]
    assert v["voided_at"] == data["old_void"]["voided_at"]


def test_old_occupancy_released_new_occupancy_active(client):
    # 改期后旧占用分钟（5,6,7）释放，新占用分钟（8,9,10）生效。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})

    # 深吃水船 t1：rev1 下最早 5 出发（占 5,6,7）
    t1 = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 浅吃水船（低潮可行）：rev1 下最早 0 出发（占 0,1,2），不挡后续验证
    shallow = _plan_confirm(client, rev1, draft=4.0, margin=0.0)
    assert [l["start_minute"] for l in shallow["itinerary"]] == [0]

    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    data, _ = _do_reschedule(client, t1, rev2)
    assert [l["start_minute"] for l in data["reschedule"]["itinerary"]] == [8]

    # 浅吃水船在 rev2 重新规划（须避开 0,1,2 既有占用与新行程的 8,9,10）：
    # 旧行程已作废 -> 5,6,7 不再封闭，最早 3 出发（占 3,4,5）；
    # 若旧占用未释放，3/4/5 都与旧 5..7 冲突，最早只能 11。
    plan = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 4.0, "height": 0.0, "margin": 0.0,
        "revision": rev2,
    }).json()
    assert plan["itinerary"][0]["start_minute"] == 3

    # 新占用 8,9,10 已生效：窗口起点 5（避开浅船 0..2）规划时，5,6,7 空闲
    # 但 8 出发与新行程重叠；5 出发占 5,6,7 可行 -> 最早 5
    plan2 = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 5, "window_minutes": 15,
        "draft": 4.0, "height": 0.0, "margin": 0.0,
        "revision": rev2,
    }).json()
    assert plan2["itinerary"][0]["start_minute"] == 5

    # 新行程自身未作废
    assert client.get(
        f"/api/trips/{data['new_trip_id']}").json()["voided_at"] is None
    # 旧行程已作废
    assert client.get(
        f"/api/trips/{t1['trip_id']}").json()["voided_at"] is not None


# ---------------------------------------------------------------------------
# 2. 幂等：同键同内容重放；异内容冲突；冲突即使修订再变化也先重放
# ---------------------------------------------------------------------------

def test_reschedule_idempotent_replay(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    first, _ = _do_reschedule(client, trip, rev2, key="k-uniq")
    deps = [8]

    # 再次又出现更新修订（rev3，且让原行程在 rev2 下的改期解也变化）
    rev3 = _make_unsafe(client, {m: 0.0 for m in range(5, 10)})

    # 同键同内容 -> 200 重放首次结果（新行程号不变、目标修订仍 rev2）
    r = _reschedule(client, trip["trip_id"], rev2, deps, key="k-uniq")
    assert r.status_code == 200, r.text
    replay = r.json()
    assert replay["replayed"] is True
    assert replay["new_trip_id"] == first["new_trip_id"]
    assert replay["old_trip_id"] == first["old_trip_id"]
    assert replay["target_revision"] == rev2
    assert replay["reschedule"] == first["reschedule"]

    # 不新增行程
    trips = client.get("/api/trips").json()
    assert len(trips) == 2

    # 同键但换成 rev3 -> 冲突（即使原行程已作废，也先按键判定冲突）
    r = _reschedule(client, trip["trip_id"], rev3, [10], key="k-uniq")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESCHEDULE_KEY_CONFLICT"


def test_reschedule_same_key_different_content_conflicts(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    _do_reschedule(client, trip, rev2, key="k-x")

    # 同键不同出发分钟（rev2 下 9 不是最优解，但内容指纹不同即冲突）
    r = _reschedule(client, trip["trip_id"], rev2, [9], key="k-x")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "RESCHEDULE_KEY_CONFLICT"
    assert err["details"]["existing_target_revision"] == rev2


def test_reschedule_key_scoped_per_key_not_trip(client):
    # 同键用于不同行程：内容指纹含 trip_id -> 冲突；不同键可独立成功
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    t1 = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 第二艘船：t1 占用 5..7 -> 第二艘最早 8 出发
    t2 = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in t2["itinerary"]] == [8]
    # 更正 5..8 低潮：t1(5..7) 与 t2(8..10) 在 rev2 下均不安全
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 9)})

    d1, _ = _do_reschedule(client, t1, rev2, key="shared")

    # 同键用于 t2（无论建议内容如何）-> 指纹含 trip_id，冲突
    r = _reschedule(client, t2["trip_id"], rev2, [11], key="shared")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESCHEDULE_KEY_CONFLICT"

    # 换新键即可成功（t1 新占用 8..10 -> t2 改期建议后移）
    review2 = _review(client, t2["trip_id"], rev2)
    assert review2["safe"] is False and review2["reschedule"] is not None
    deps2 = [l["start_minute"] for l in review2["reschedule"]["itinerary"]]
    r = _reschedule(client, t2["trip_id"], rev2, deps2, key="other")
    assert r.status_code == 201, r.text
    assert r.json()["new_trip_id"] != d1["new_trip_id"]


# ---------------------------------------------------------------------------
# 3. 拒绝：未知行程 / 已作废 / 修订未知或过期 / 仍安全 / 无解 / 方案不一致
# ---------------------------------------------------------------------------

def test_reschedule_unknown_trip(client):
    r = _reschedule(client, 9999, 1, [0], key="k")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"


def test_reschedule_already_voided(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    r = client.post(f"/api/trips/{trip['trip_id']}/void",
                    json={"reason": "手工作废"})
    assert r.status_code == 201
    r = _reschedule(client, trip["trip_id"], rev1, [0], key="k")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "TRIP_ALREADY_VOIDED"


def test_reschedule_unknown_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)
    r = _reschedule(client, trip["trip_id"], 4242, [0], key="k")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"


def test_reschedule_stale_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    # 再来一个更新修订 rev3；拿着 rev2 执行 -> 过期
    rev3 = _make_unsafe(client, {m: 0.0 for m in range(5, 9)})
    r = _reschedule(client, trip["trip_id"], rev2, [9], key="k")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "REVISION_STALE"
    assert err["details"] == {"provided_revision": rev2,
                              "current_revision": rev3}
    # 状态未变
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None


def test_reschedule_still_safe_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)
    # 更正发生在行程占用之外，原行程仍安全
    rev2 = _make_unsafe(client, {15: 0.0})
    r = _reschedule(client, trip["trip_id"], rev2, [0], key="k")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESCHEDULE_NOT_NEEDED"
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None


def test_reschedule_no_solution(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(0, 20)})

    review = _review(client, trip["trip_id"], rev2)
    assert review["no_solution"] is True

    r = _reschedule(client, trip["trip_id"], rev2, [5], key="k")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "RESCHEDULE_NO_SOLUTION"
    assert err["details"]["first_violation"]["minute"] == 5
    # 状态未变
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None
    assert client.get("/api/trips").json().__len__() == 1


def test_reschedule_departure_count_mismatch(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})

    r = _reschedule(client, trip["trip_id"], rev2, [8, 9], key="k")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"] == {"trip_id": trip["trip_id"],
                              "provided": 2, "expected": 1}
    # 未消耗幂等键：随后同键同内容首次成功
    data, _ = _do_reschedule(client, trip, rev2, key="k")
    assert data["new_trip_id"]


def test_reschedule_plan_mismatch_wrong_departures(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    # 最优是 8；提交 9 -> 方案不一致（9 本身可行但不是最优建议）
    r = _reschedule(client, trip["trip_id"], rev2, [9], key="k")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "PLAN_MISMATCH"
    assert err["details"]["submitted_departure_minutes"] == [9]
    assert [l["start_minute"]
            for l in err["details"]["recalculated"]["itinerary"]] == [8]
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["voided_at"] is None

    # 失败不消耗幂等键
    data, _ = _do_reschedule(client, trip, rev2, key="k")
    assert data["old_trip_id"] == trip["trip_id"]


# ---------------------------------------------------------------------------
# 4. 船舶档案：停用 / 版本变化 / 档案删除均拒绝；参数取自快照
# ---------------------------------------------------------------------------

def test_reschedule_ship_disabled_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    client.post("/api/ships", json={
        "ship_no": "S1", "draft": 6.0, "height": 0.0, "margin": 0.5,
    })
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, ship_no="S1")
    assert trip["ship_no"] == "S1" and trip["ship_version"] == 1
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})

    r = client.put("/api/ships/S1", json={"enabled": False})
    assert r.status_code == 200
    review = _review(client, trip["trip_id"], rev2)
    deps = [l["start_minute"] for l in review["reschedule"]["itinerary"]]
    r = _reschedule(client, trip["trip_id"], rev2, deps, key="k")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIP_DISABLED"
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


def test_reschedule_ship_version_stale_rejected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    client.post("/api/ships", json={
        "ship_no": "S1", "draft": 6.0, "height": 0.0, "margin": 0.5,
    })
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, ship_no="S1")
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})

    # 档案参数变化（版本 2）；新快照仍应按快照参数（draft=6.0）求解，
    # 但版本已变必须拒绝
    r = client.put("/api/ships/S1", json={"draft": 5.0})
    assert r.json()["version"] == 2
    review = _review(client, trip["trip_id"], rev2)
    deps = [l["start_minute"] for l in review["reschedule"]["itinerary"]]
    r = _reschedule(client, trip["trip_id"], rev2, deps, key="k")
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SHIP_VERSION_STALE"
    assert err["details"] == {"ship_no": "S1", "provided_version": 1,
                              "current_version": 2}
    assert client.get(f"/api/trips/{trip['trip_id']}").json()["voided_at"] is None


def test_reschedule_uses_snapshot_params_when_ship_unchanged(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    client.post("/api/ships", json={
        "ship_no": "S1", "draft": 6.0, "height": 0.0, "margin": 0.5,
    })
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, ship_no="S1")
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    data, _ = _do_reschedule(client, trip, rev2, key="k")
    new = client.get(f"/api/trips/{data['new_trip_id']}").json()
    # 新快照固化档案引用与快照参数
    assert new["ship_no"] == "S1"
    assert new["ship_version"] == 1
    assert new["draft"] == 6.0 and new["margin"] == 0.5


# ---------------------------------------------------------------------------
# 5. 原账单等额冲销一次；新行程待另行开单
# ---------------------------------------------------------------------------

def test_reschedule_reverses_bill_once_and_new_trip_billable(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)

    # 开单（需要费率版本）
    conn = connect()
    conn.execute("INSERT INTO toll_rate_versions(changed_seg_id, note)"
                 " VALUES (1, 'v')")
    conn.execute(
        "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute,"
        " rate_cents_per_minute) VALUES (1, 1, 0, 1440, 10)")
    conn.close()
    b = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert b.status_code == 201
    total = b.json()["total_cents"]
    assert total == 30  # 航时 3 × 费率 10

    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    data, _ = _do_reschedule(client, trip, rev2, key="k")
    assert data["reversal_created"] is True
    rev = data["reversal"]
    assert rev["trip_id"] == trip["trip_id"]
    assert rev["total_cents"] == -total
    assert rev["segments"][0]["amount_cents"] == -30
    assert rev["version"] == 1

    # 账单查询内嵌同一冲销
    bill = client.get(f"/api/trips/{trip['trip_id']}/bill").json()
    assert bill["reversal"]["total_cents"] == -total
    assert bill["reversal"]["reversed_at"] == rev["reversed_at"]

    # 冲销查询接口一致
    rr = client.get(f"/api/trips/{trip['trip_id']}/reversal").json()
    assert rr["total_cents"] == -total

    # 新行程尚未开单；可另行开单（不自动出账）
    assert client.get(
        f"/api/trips/{data['new_trip_id']}/bill").status_code == 404
    nb = client.post(f"/api/trips/{data['new_trip_id']}/bill")
    assert nb.status_code == 201
    new_itinerary = data["reschedule"]["itinerary"]
    assert nb.json()["total_cents"] == \
        sum(l["travel_time"] for l in new_itinerary) * 10

    # 重放不改冲销（仍为首次那张）
    deps = [l["start_minute"] for l in data["reschedule"]["itinerary"]]
    replay = _reschedule(client, trip["trip_id"], rev2, deps, key="k")
    assert replay.status_code == 200
    assert replay.json()["reversal"]["reversed_at"] == rev["reversed_at"]
    bills = client.get("/api/toll/bills").json()
    assert len(bills) == 2  # 旧账单 + 新行程账单；冲销不是账单


# ---------------------------------------------------------------------------
# 6. 当前航时管制 / 狭窄水道组 / 泊位日历与其他有效占用参与复算
# ---------------------------------------------------------------------------

def test_reschedule_respects_current_travel_controls(client):
    # 基础航时 3；修订#2 下 5..7 低潮、8 起可行。
    # 在复核前对航段设置管制：[0,20) 进入航时 4。
    # 复核建议与确认复算都应按航时 4：8 出发 -> 12 抵达，占用 8..11。
    add_node(client, "A"); add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5, window=20)
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})

    r = client.put(f"/api/segments/{seg}/travel-controls", json={
        "controls": [{"start_minute": 0, "end_minute": 1440,
                      "travel_time": 4}],
    })
    assert r.status_code == 200

    review = _review(client, trip["trip_id"], rev2)
    leg = review["reschedule"]["itinerary"][0]
    assert leg["travel_time"] == 4
    assert (leg["start_minute"], leg["end_minute"]) == (8, 12)

    data, _ = _do_reschedule(client, trip, rev2, key="k")
    new = client.get(f"/api/trips/{data['new_trip_id']}").json()
    assert new["itinerary"][0]["travel_time"] == 4
    assert new["arrival_minute"] == 12

    # 提交旧管制下的出发结果（航时3的口径）会因段数一致但分钟不一致而失败：
    # 先作废已发生无法重测；另起一 trip 验证管制在确认时再次复算
    # （通过 8 可行但 9..12 占用的情形已由 happy path 覆盖）。


def test_reschedule_avoids_other_valid_occupancy(client):
    # t1 原占用 5..7；改期解要避开另一艘**仍有效**船占用 8..10 -> 最早 11。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    t1 = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 直接插入一条占用 8,9,10 的有效确认快照（rev1 下第 8 分钟可行）
    t2 = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in t2["itinerary"]] == [8]
    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})

    review = _review(client, t1["trip_id"], rev2)
    assert [l["start_minute"]
            for l in review["reschedule"]["itinerary"]] == [11]
    data, _ = _do_reschedule(client, t1, rev2, key="k")
    assert data["reschedule"]["itinerary"][0]["start_minute"] == 11
    # t2 仍有效、未被改动
    assert client.get(f"/api/trips/{t2['trip_id']}").json()["voided_at"] is None


def test_reschedule_berth_snapshot_work_window(client):
    # 原快照指定泊位 D1、作业 3 分钟；收紧日历使改期抵达后无处作业 -> 无解。
    for n in ["A", "B"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    client.post("/api/nodes/B/berths", json={"berth_id": "D1"})
    client.put("/api/berths/D1/calendar", json={
        "intervals": [{"start_minute": 0, "end_minute": 1440}]})
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(
        client, rev1, draft=6.0, margin=0.5,
        extra={"berth_id": "D1", "work_minutes": 3})
    assert trip["berth_id"] == "D1"
    assert trip["work_start_minute"] == 8
    assert trip["work_end_minute"] == 11

    rev2 = _make_unsafe(client, {m: 0.0 for m in range(5, 8)})
    # 收紧日历：只保留 [11,13) 这段 2 分钟开放窗；作业 3 分钟在任何可行
    # 抵达分钟都放不进去，其后不开放 -> 泊位约束下无解
    client.put("/api/berths/D1/calendar", json={
        "intervals": [{"start_minute": 11, "end_minute": 13}]})
    review = _review(client, trip["trip_id"], rev2)
    assert review["no_solution"] is True
    r = _reschedule(client, trip["trip_id"], rev2, [11], key="k")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RESCHEDULE_NO_SOLUTION"

    # 恢复日历 -> 成功，新快照固化新作业区间
    client.put("/api/berths/D1/calendar", json={
        "intervals": [{"start_minute": 0, "end_minute": 1440}]})
    data, _ = _do_reschedule(client, trip, rev2, key="k2")
    assert data["reschedule"]["berth_id"] == "D1"
    assert data["reschedule"]["work_minutes"] == 3
    assert data["reschedule"]["work_start_minute"] == 11
    assert data["reschedule"]["work_end_minute"] == 14
    new = client.get(f"/api/trips/{data['new_trip_id']}").json()
    assert new["work_start_minute"] == 11 and new["work_end_minute"] == 14
