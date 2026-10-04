"""船舶档案测试。

规则：
* 调度员按唯一船号管理档案：非负吃水、水面以上高度、安全余量及启停状态；
  有效变更递增版本，当前与历史版本可查；重复船号 / 非法参数明确拒绝且不留半成品；
* 单船与批次预排可用船号代替三项参数，按当前启用版本规划，结果回传船号、
  版本及所用参数；直接提交参数的既有请求保持兼容；混用两种输入须拒绝；
* 档案行程确认须回填船号、版本及行程：事务内核对档案仍启用且版本未变，
  再按现有潮位/管制/占用复算；失效拒绝且不保存，批次全退；
* 已确认快照固化所用参数及档案引用：复核/改期只用快照参数，后续档案变更
  不改写历史；作废与批次同键重放沿用旧语义。
"""
import json
import sqlite3

from app import config
from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _ab(client, depth=99.0):
    add_node(client, "A")
    add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=depth)
    return seed_initial_revision(_level(0, 39, 3.0))


def _create_ship(client, ship_no="SHIP-1", draft=1.0, height=0.5, margin=0.2,
                 enabled=True):
    r = client.post("/api/ships", json={
        "ship_no": ship_no, "draft": draft, "height": height,
        "margin": margin, "enabled": enabled,
    })
    assert r.status_code == 201, r.text
    return r.json()


def _plan_body(rev, **over):
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev,
    }
    body.update(over)
    return body


def _ship_plan_body(rev, ship_no="SHIP-1", **over):
    body = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "ship_no": ship_no, "revision": rev,
    }
    body.update(over)
    return body


def _plan(client, body):
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _confirm(client, body):
    r = client.post("/api/trips/confirm", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _confirm_body(plan):
    """按规划响应构造确认请求：船号规划回填船号+版本，参数规划回填三项参数。"""
    body = {
        "revision": plan["revision"],
        "origin": plan["origin"], "destination": plan["destination"],
        "start_minute": plan["start_minute"],
        "window_minutes": plan["window_minutes"],
        "itinerary": plan["itinerary"],
    }
    if plan["ship_no"] is not None:
        body["ship_no"] = plan["ship_no"]
        body["ship_version"] = plan["ship_version"]
    else:
        body.update({"draft": plan["draft"], "height": plan["height"],
                     "margin": plan["margin"]})
    return body


# ---------------------------------------------------------------------------
# 1. 建档 / 查询 / 版本历史
# ---------------------------------------------------------------------------

def test_create_ship_and_query_current_and_versions(client):
    ship = _create_ship(client, draft=5.0, height=2.0, margin=0.5)
    assert ship["ship_no"] == "SHIP-1"
    assert ship["version"] == 1
    assert ship["draft"] == 5.0 and ship["height"] == 2.0 and ship["margin"] == 0.5
    assert ship["enabled"] is True
    assert ship["created_at"]

    # 当前版本查询
    cur = client.get("/api/ships/SHIP-1").json()
    assert cur == ship
    # 列表
    rows = client.get("/api/ships").json()
    assert [r["ship_no"] for r in rows] == ["SHIP-1"]
    # 版本历史
    vers = client.get("/api/ships/SHIP-1/versions").json()
    assert len(vers) == 1 and vers[0]["version"] == 1
    # 指定版本
    v1 = client.get("/api/ships/SHIP-1/versions/1").json()
    assert v1["version"] == 1 and v1["draft"] == 5.0


def test_create_ship_duplicate_rejected(client):
    _create_ship(client)
    r = client.post("/api/ships", json={
        "ship_no": "SHIP-1", "draft": 9.0, "height": 9.0, "margin": 9.0,
    })
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIP_EXISTS"
    # 不留半成品：原档案保持版本 1 与旧参数
    cur = client.get("/api/ships/SHIP-1").json()
    assert cur["version"] == 1 and cur["draft"] == 1.0
    assert len(client.get("/api/ships/SHIP-1/versions").json()) == 1


def test_create_ship_invalid_params_rejected(client):
    for field in ("draft", "height", "margin"):
        body = {"ship_no": "BAD", "draft": 1.0, "height": 1.0, "margin": 1.0}
        body[field] = -0.1
        r = client.post("/api/ships", json=body)
        assert r.status_code == 422, field
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    r = client.post("/api/ships", json={"ship_no": "", "draft": 1, "height": 1,
                                        "margin": 1})
    assert r.status_code == 422
    # 未留下任何档案
    assert client.get("/api/ships").json() == []


def test_ship_not_found_on_all_routes(client):
    assert client.get("/api/ships/NOPE").status_code == 404
    assert client.get("/api/ships/NOPE").json()["error"]["code"] == "SHIP_NOT_FOUND"
    assert client.get("/api/ships/NOPE/versions").status_code == 404
    assert client.get("/api/ships/NOPE/versions/1").status_code == 404
    r = client.put("/api/ships/NOPE", json={"draft": 1.0})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"


def test_update_ship_increments_version_and_keeps_history(client):
    _create_ship(client, draft=1.0, height=0.5, margin=0.2)

    # 部分字段变更：其余沿用当前值，版本 +1
    r = client.put("/api/ships/SHIP-1", json={"draft": 6.5})
    assert r.status_code == 200
    data = r.json()
    assert data["changed"] is True and data["version"] == 2
    assert data["draft"] == 6.5 and data["height"] == 0.5 and data["margin"] == 0.2

    # 停用：版本 +1
    r = client.put("/api/ships/SHIP-1", json={"enabled": False})
    assert r.json()["changed"] is True and r.json()["version"] == 3
    assert r.json()["enabled"] is False

    # 历史版本完整可查
    vers = client.get("/api/ships/SHIP-1/versions").json()
    assert [v["version"] for v in vers] == [1, 2, 3]
    assert vers[0]["draft"] == 1.0 and vers[0]["enabled"] is True
    assert vers[1]["draft"] == 6.5 and vers[1]["enabled"] is True
    assert vers[2]["enabled"] is False
    # 指定历史版本
    v1 = client.get("/api/ships/SHIP-1/versions/1").json()
    assert v1["draft"] == 1.0
    # 未知版本
    r = client.get("/api/ships/SHIP-1/versions/99")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIP_VERSION_NOT_FOUND"


def test_update_ship_noop_does_not_increment_version(client):
    _create_ship(client, draft=1.0, height=0.5, margin=0.2)
    # 提交与当前完全一致的值
    r = client.put("/api/ships/SHIP-1",
                   json={"draft": 1.0, "height": 0.5, "margin": 0.2,
                         "enabled": True})
    assert r.status_code == 200
    assert r.json()["changed"] is False and r.json()["version"] == 1
    # 空提交同样无效
    r = client.put("/api/ships/SHIP-1", json={})
    assert r.json()["changed"] is False and r.json()["version"] == 1
    assert len(client.get("/api/ships/SHIP-1/versions").json()) == 1


def test_update_ship_invalid_params_rejected(client):
    _create_ship(client)
    r = client.put("/api/ships/SHIP-1", json={"draft": -1.0})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 版本未变
    assert client.get("/api/ships/SHIP-1").json()["version"] == 1


# ---------------------------------------------------------------------------
# 2. 船号预排：代替三项参数，按当前启用版本；混用拒绝
# ---------------------------------------------------------------------------

def test_plan_by_ship_no_uses_current_enabled_version(client):
    rev = _ab(client)
    _create_ship(client, draft=6.0, height=1.0, margin=0.5)

    plan = _plan(client, _ship_plan_body(rev))
    assert plan["ship_no"] == "SHIP-1"
    assert plan["ship_version"] == 1
    # 回传所用参数（取自档案而非请求）
    assert plan["draft"] == 6.0 and plan["height"] == 1.0 and plan["margin"] == 0.5
    assert plan["arrival_minute"] == 2

    # 档案变更后，下一次预排按新版本参数
    client.put("/api/ships/SHIP-1", json={"draft": 2.0})
    plan2 = _plan(client, _ship_plan_body(rev))
    assert plan2["ship_version"] == 2 and plan2["draft"] == 2.0


def test_plan_by_ship_no_matches_direct_params_result(client):
    rev = _ab(client)
    _create_ship(client, draft=1.0, height=0.0, margin=0.0)
    by_ship = _plan(client, _ship_plan_body(rev))
    direct = _plan(client, _plan_body(rev))
    assert by_ship["itinerary"] == direct["itinerary"]
    assert by_ship["arrival_minute"] == direct["arrival_minute"]
    # 直接提交参数的响应保持兼容：无档案引用
    assert direct["ship_no"] is None and direct["ship_version"] is None


def test_plan_mixing_inputs_rejected(client):
    rev = _ab(client)
    _create_ship(client)
    # 船号 + 全部参数
    r = client.post("/api/trips/plan", json=_ship_plan_body(rev, draft=1.0,
                                                            height=0.0,
                                                            margin=0.0))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    # 船号 + 任一参数
    r = client.post("/api/trips/plan", json=_ship_plan_body(rev, draft=1.0))
    assert r.status_code == 422
    # 两者皆缺（参数不全且无船号）
    body = _plan_body(rev)
    del body["margin"]
    r = client.post("/api/trips/plan", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_plan_unknown_or_disabled_ship_rejected(client):
    rev = _ab(client)
    r = client.post("/api/trips/plan", json=_ship_plan_body(rev, ship_no="GHOST"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"

    _create_ship(client, enabled=False)
    r = client.post("/api/trips/plan", json=_ship_plan_body(rev))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIP_DISABLED"


# ---------------------------------------------------------------------------
# 3. 船号确认：回填船号+版本+行程；事务内核对档案仍启用且版本未变
# ---------------------------------------------------------------------------

def test_confirm_by_ship_no_freezes_params_and_reference(client):
    rev = _ab(client)
    _create_ship(client, draft=6.0, height=1.0, margin=0.5)
    plan = _plan(client, _ship_plan_body(rev))

    trip = _confirm(client, _confirm_body(plan))
    assert trip["ship_no"] == "SHIP-1" and trip["ship_version"] == 1
    assert trip["draft"] == 6.0 and trip["height"] == 1.0 and trip["margin"] == 0.5

    # 快照经查询接口原样可读（含档案引用）
    got = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert got["ship_no"] == "SHIP-1" and got["ship_version"] == 1
    rows = client.get("/api/trips").json()
    assert rows[0]["ship_no"] == "SHIP-1"


def test_confirm_request_validation_with_ship_no(client):
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))

    # 缺 ship_version
    body = _confirm_body(plan)
    del body["ship_version"]
    r = client.post("/api/trips/confirm", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    # 混用：船号 + 参数
    body = {**_confirm_body(plan), "draft": 1.0}
    r = client.post("/api/trips/confirm", json=body)
    assert r.status_code == 422

    # 只有 ship_version 没有船号
    body = _confirm_body(_plan(client, _plan_body(rev)))
    body["ship_version"] = 1
    r = client.post("/api/trips/confirm", json=body)
    assert r.status_code == 422

    # 均未保存
    assert client.get("/api/trips").json() == []


def test_confirm_ship_version_stale_rejected(client):
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))

    # 档案有效变更 -> 版本 2
    client.put("/api/ships/SHIP-1", json={"draft": 2.0})

    r = client.post("/api/trips/confirm", json=_confirm_body(plan))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SHIP_VERSION_STALE"
    assert err["details"]["provided_version"] == 1
    assert err["details"]["current_version"] == 2
    # 不保存
    assert client.get("/api/trips").json() == []

    # 重新预排后按新版本确认成功
    plan2 = _plan(client, _ship_plan_body(rev))
    assert plan2["ship_version"] == 2
    trip = _confirm(client, _confirm_body(plan2))
    assert trip["ship_version"] == 2 and trip["draft"] == 2.0


def test_confirm_ship_disabled_rejected(client):
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))

    client.put("/api/ships/SHIP-1", json={"enabled": False})
    r = client.post("/api/trips/confirm", json=_confirm_body(plan))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIP_DISABLED"
    assert client.get("/api/trips").json() == []


def test_confirm_ship_noop_update_keeps_version_and_confirms(client):
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))
    # 无效变更（同值提交）不递增版本，确认不受影响
    r = client.put("/api/ships/SHIP-1", json={"draft": 1.0})
    assert r.json()["changed"] is False
    trip = _confirm(client, _confirm_body(plan))
    assert trip["ship_version"] == 1


def test_confirm_unknown_ship_rejected(client):
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))
    body = _confirm_body(plan)
    body["ship_no"] = "GHOST"
    r = client.post("/api/trips/confirm", json=body)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "SHIP_NOT_FOUND"


def test_confirm_ship_check_precedes_revision_stale(client):
    """事务内先核对档案再核订潮位修订：两者皆失效时报档案错误。"""
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))
    client.put("/api/ships/SHIP-1", json={"draft": 9.9})      # 版本 2
    client.post("/api/tide/corrections", json={"corrections": {"5": 3.0}})  # 新修订
    r = client.post("/api/trips/confirm", json=_confirm_body(plan))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "SHIP_VERSION_STALE"


def test_confirm_by_ship_revision_stale_still_applies(client):
    rev = _ab(client)
    _create_ship(client)
    plan = _plan(client, _ship_plan_body(rev))
    client.post("/api/tide/corrections", json={"corrections": {"5": 3.0}})
    r = client.post("/api/trips/confirm", json=_confirm_body(plan))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REVISION_STALE"


# ---------------------------------------------------------------------------
# 4. 快照固化：后续档案变更不改写历史；复核/改期只用快照参数
# ---------------------------------------------------------------------------

def test_archive_change_does_not_rewrite_confirmed_snapshot(client):
    rev = _ab(client)
    _create_ship(client, draft=1.0, height=0.0, margin=0.0)
    trip = _confirm(client, _confirm_body(_plan(client, _ship_plan_body(rev))))
    tid = trip["trip_id"]

    # 档案后续变更（参数与启停），快照原样保留
    client.put("/api/ships/SHIP-1", json={"draft": 50.0, "enabled": False})
    got = client.get(f"/api/trips/{tid}").json()
    assert got["ship_no"] == "SHIP-1" and got["ship_version"] == 1
    assert got["draft"] == 1.0 and got["height"] == 0.0 and got["margin"] == 0.0


def test_review_uses_snapshot_params_not_current_archive(client):
    """档案改到会使原行程不可行的参数后，复核仍按快照参数判定安全。"""
    # 潮位 3.0、航段静水深度 4.0：吃水 1.0 + 余量 0 -> 可用 7.0 安全；
    # 若按档案变更后的吃水 50.0 判定则会违规——复核须仍用快照的 1.0。
    rev = _ab(client, depth=4.0)
    _create_ship(client, draft=1.0, height=0.0, margin=0.0)
    trip = _confirm(client, _confirm_body(_plan(client, _ship_plan_body(rev))))
    tid = trip["trip_id"]

    client.put("/api/ships/SHIP-1", json={"draft": 50.0})
    data = client.post(f"/api/trips/{tid}/review",
                       json={"target_revision": rev}).json()
    assert data["safe"] is True
    assert data["draft"] == 1.0  # 复核回显快照参数
    # 快照仍未被改写
    assert client.get(f"/api/trips/{tid}").json()["draft"] == 1.0


# ---------------------------------------------------------------------------
# 5. 批次：船号预排 / 确认；档案失效全批回滚；幂等重放沿用旧语义
# ---------------------------------------------------------------------------

def _two_ships(rev, ship_no=None):
    """两艘同航路船：ship_no 给定时首艘按档案、次艘直接提交参数。"""
    first = {"origin": "A", "destination": "B", "start_minute": 0,
             "window_minutes": 20}
    if ship_no is not None:
        first["ship_no"] = ship_no
    else:
        first.update({"draft": 1.0, "height": 0.0, "margin": 0.0})
    second = {"origin": "A", "destination": "B", "start_minute": 0,
              "window_minutes": 20, "draft": 1.0, "height": 0.0, "margin": 0.0}
    return [first, second]


def _batch_confirm_body(rev, key, ships, plan):
    out = []
    for i, s in enumerate(ships):
        entry = dict(s)
        entry["itinerary"] = plan["ships"][i]["itinerary"]
        if plan["ships"][i]["ship_no"] is not None:
            entry["ship_version"] = plan["ships"][i]["ship_version"]
        out.append(entry)
    return {"revision": rev, "idempotency_key": key, "ships": out}


def test_batch_plan_and_confirm_by_ship_no(client):
    rev = _ab(client)
    _create_ship(client, draft=6.0, height=1.0, margin=0.5)
    ships = _two_ships(rev, ship_no="SHIP-1")

    r = client.post("/api/trips/batch/plan", json={"revision": rev, "ships": ships})
    assert r.status_code == 200, r.text
    plan = r.json()
    # 首艘按档案：回传船号、版本与所用参数；次艘直接参数：无档案引用
    s0, s1 = plan["ships"]
    assert s0["ship_no"] == "SHIP-1" and s0["ship_version"] == 1
    assert s0["draft"] == 6.0 and s0["margin"] == 0.5
    assert s1["ship_no"] is None and s1["ship_version"] is None
    # 后船避开前船拟占用
    assert s0["itinerary"][0]["start_minute"] == 0
    assert s1["itinerary"][0]["start_minute"] == 2

    body = _batch_confirm_body(rev, "ship-batch-1", ships, plan)
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 201, r.text
    trips = r.json()["trips"]
    assert trips[0]["ship_no"] == "SHIP-1" and trips[0]["ship_version"] == 1
    assert trips[1]["ship_no"] is None

    # 快照固化档案引用与所用参数
    got = client.get(f"/api/trips/{trips[0]['trip_id']}").json()
    assert got["ship_no"] == "SHIP-1" and got["ship_version"] == 1
    assert got["draft"] == 6.0

    # 同键同内容重放：沿用旧语义（即使档案其后变更）
    client.put("/api/ships/SHIP-1", json={"draft": 9.0})
    replay = client.post("/api/trips/batch/confirm", json=body)
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert [t["trip_id"] for t in replay.json()["trips"]] == \
        [t["trip_id"] for t in trips]


def test_batch_plan_ship_errors_carry_ship_index(client):
    rev = _ab(client)
    _create_ship(client, ship_no="OK-1")
    _create_ship(client, ship_no="OFF-1", enabled=False)
    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "ship_no": "OK-1"},
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "ship_no": "OFF-1"},
    ]
    r = client.post("/api/trips/batch/plan", json={"revision": rev, "ships": ships})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SHIP_DISABLED" and err["details"]["ship_index"] == 1

    ships[1]["ship_no"] = "GHOST"
    r = client.post("/api/trips/batch/plan", json={"revision": rev, "ships": ships})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "SHIP_NOT_FOUND" and err["details"]["ship_index"] == 1


def test_batch_plan_mixing_inputs_rejected(client):
    rev = _ab(client)
    _create_ship(client)
    ships = _two_ships(rev, ship_no="SHIP-1")
    ships[0]["draft"] = 1.0  # 混用
    r = client.post("/api/trips/batch/plan", json={"revision": rev, "ships": ships})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_batch_confirm_ship_version_stale_rolls_back_whole_batch(client):
    rev = _ab(client)
    _create_ship(client)
    ships = _two_ships(rev, ship_no="SHIP-1")
    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": ships}).json()
    body = _batch_confirm_body(rev, "ship-batch-stale", ships, plan)

    # 首艘档案在预排后变更 -> 版本过期，全批不保存
    client.put("/api/ships/SHIP-1", json={"margin": 0.9})
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SHIP_VERSION_STALE"
    assert err["details"]["ship_index"] == 0
    assert client.get("/api/trips").json() == []


def test_batch_confirm_ship_disabled_rolls_back_whole_batch(client):
    rev = _ab(client)
    _create_ship(client, ship_no="S-1")
    _create_ship(client, ship_no="S-2")
    ships = [
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "ship_no": "S-1"},
        {"origin": "A", "destination": "B", "start_minute": 0,
         "window_minutes": 20, "ship_no": "S-2"},
    ]
    plan = client.post("/api/trips/batch/plan",
                       json={"revision": rev, "ships": ships}).json()
    body = _batch_confirm_body(rev, "ship-batch-disabled", ships, plan)

    client.put("/api/ships/S-2", json={"enabled": False})
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "SHIP_DISABLED" and err["details"]["ship_index"] == 1
    assert client.get("/api/trips").json() == []


# ---------------------------------------------------------------------------
# 6. 作废语义沿用：船号确认的行程作废后占用释放、快照保留
# ---------------------------------------------------------------------------

def test_void_of_ship_confirmed_trip_keeps_old_semantics(client):
    rev = _ab(client)
    _create_ship(client)
    trip = _confirm(client, _confirm_body(_plan(client, _ship_plan_body(rev))))
    tid = trip["trip_id"]

    r = client.post(f"/api/trips/{tid}/void", json={"reason": "误确认"})
    assert r.status_code == 201
    # 历史快照保留（含档案引用），占用释放
    got = client.get(f"/api/trips/{tid}").json()
    assert got["ship_no"] == "SHIP-1" and got["voided_at"] is not None
    again = _plan(client, _ship_plan_body(rev))
    assert again["itinerary"][0]["start_minute"] == 0


# ---------------------------------------------------------------------------
# 7. 旧库迁移：confirmed_trips 补档案引用列，既有行为 NULL
# ---------------------------------------------------------------------------

def test_migrate_old_db_adds_ship_reference_columns(tmp_path, monkeypatch):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE confirmed_trips (
            trip_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            revision        INTEGER NOT NULL,
            origin          TEXT NOT NULL,
            destination     TEXT NOT NULL,
            start_minute    INTEGER NOT NULL,
            window_minutes  INTEGER NOT NULL,
            draft           REAL NOT NULL,
            height          REAL NOT NULL,
            margin          REAL NOT NULL,
            arrival_minute  INTEGER NOT NULL,
            itinerary       TEXT NOT NULL,
            confirmed_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE tide_values (
            revision INTEGER NOT NULL,
            minute   INTEGER NOT NULL,
            level    REAL NOT NULL,
            PRIMARY KEY (revision, minute)
        );
        """
    )
    conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (1, 'A', 'B', 0, 20, 1.0, 0.0, 0.0, 2, '[]')"
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(config, "DB_PATH", str(db))
    conn2 = connect()
    init_db(conn2)
    cols = {r["name"] for r in conn2.execute(
        "PRAGMA table_info(confirmed_trips)").fetchall()}
    assert {"ship_no", "ship_version"} <= cols
    row = conn2.execute(
        "SELECT ship_no, ship_version FROM confirmed_trips").fetchone()
    assert row["ship_no"] is None and row["ship_version"] is None
    # 旧 tide_values 同样补回 error 列
    tcols = {r["name"] for r in conn2.execute(
        "PRAGMA table_info(tide_values)").fetchall()}
    assert "error" in tcols
    # 档案表已建
    conn2.execute("SELECT 1 FROM ships LIMIT 1").fetchall()
    conn2.execute("SELECT 1 FROM ship_versions LIMIT 1").fetchall()
    conn2.close()
