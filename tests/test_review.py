"""潮位更正后原行程复核与改期测试。"""
from app.database import connect

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


def _review(client, trip_id, target_revision=None):
    body = {} if target_revision is None else {"target_revision": target_revision}
    return client.post(f"/api/trips/{trip_id}/review", json=body)


# ---------------------------------------------------------------------------
# 1. 目标修订下原行程仍安全（含修订号相同）：safe=true，不给改期
# ---------------------------------------------------------------------------

def test_review_safe(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev)

    r = _review(client, trip["trip_id"], rev)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["safe"] is True
    assert data["first_violation"] is None
    assert data["reschedule"] is None
    assert data["no_solution"] is False
    assert data["snapshot_revision"] == rev
    assert data["target_revision"] == rev
    assert data["original_itinerary"] == trip["itinerary"]


def test_review_safe_after_irrelevant_correction(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)
    # 更正发生在行程占用分钟（0,1）之外
    r = client.post("/api/tide/corrections", json={"corrections": {15: 0.0}})
    rev2 = r.json()["revision"]

    data = _review(client, trip["trip_id"], rev2).json()
    assert data["safe"] is True
    assert data["target_revision"] == rev2
    assert data["reschedule"] is None


# ---------------------------------------------------------------------------
# 2. 不安全：首次违规给出航段、分钟与水深原因
# ---------------------------------------------------------------------------

def test_review_depth_violation_first_minute(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    # 快照：第 5 分钟进入，占用 5,6,7
    assert [l["start_minute"] for l in trip["itinerary"]] == [5]

    # 更正：第 6 分钟低潮（第 5、7 分钟仍可行）
    client.post("/api/tide/corrections", json={"corrections": {6: 0.0}})
    data = _review(client, trip["trip_id"]).json()

    assert data["safe"] is False
    v = data["first_violation"]
    assert v["leg_index"] == 0
    assert v["minute"] == 6
    assert v["reasons"] == ["depth"]
    assert v["tide_level"] == 0.0
    assert v["available_depth"] == 4.0
    assert v["required_depth"] == 6.5
    assert v["available_clearance"] is None  # 无桥


def test_review_clearance_violation(client):
    add_node(client, "A"); add_node(client, "B")
    # 净空 5m；船高 3m + 余量 1m => 潮位必须 <= 1m
    add_segment(client, "A", "B", travel_time=2, depth=99.0, clearance=5.0)
    rev1 = seed_initial_revision(_level(0, 19, 0.0))
    trip = _plan_confirm(client, rev1, height=3.0, margin=1.0)

    client.post("/api/tide/corrections", json={"corrections": {1: 3.0}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["minute"] == 1
    assert v["reasons"] == ["clearance"]
    assert v["available_clearance"] == 2.0
    assert v["required_clearance"] == 4.0


def test_review_first_violation_is_earliest_leg_and_minute(client):
    # 两段：第一段安全、第二段违规；且第二段首个违规分钟被正确定位
    for n in ["A", "B", "C"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    add_segment(client, "B", "C", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5, destination="C")
    # 快照：A->B 占 0,1；B->C 占 2,3,4
    assert [(l["seg_id"], l["start_minute"]) for l in trip["itinerary"]] == \
        [(1, 0), (2, 2)]

    # 更正 3、4 分钟低潮：第二段首次违规在第 3 分钟（第 2 分钟仍可行）
    client.post("/api/tide/corrections", json={"corrections": {3: 0.0, 4: 0.0}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    v = data["first_violation"]
    assert v["leg_index"] == 1
    assert v["seg_id"] == 2
    assert v["minute"] == 3
    assert v["reasons"] == ["depth"]


# ---------------------------------------------------------------------------
# 3. 改期：等待即可恢复，只变动后续必要航段
# ---------------------------------------------------------------------------

def test_reschedule_wait_pushes_trip(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert trip["arrival_minute"] == 8  # 5 进入，占 5,6,7

    # 更正 5..7 低潮，但 8 之后恢复：原行程违规，等待到 8 出发即可
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 8)}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    assert data["no_solution"] is False
    rs = data["reschedule"]
    assert rs["changed_leg_count"] == 1
    assert rs["itinerary"][0]["start_minute"] == 8
    assert rs["itinerary"][0]["end_minute"] == 11
    assert rs["arrival_minute"] == 11
    assert rs["total_wait_minutes"] == 8
    assert rs["changed_legs"] == [
        {"leg_index": 0, "seg_id": 1,
         "original_departure_minute": 5, "new_departure_minute": 8}
    ]


def test_reschedule_multiple_legs_only_necessary_changes(client):
    # A->B（航时2），B->C（航时2）；原快照 0 出发、2 出发，4 抵达。
    # 更正使第 0 分钟不可行、第 1 分钟恢复：等待 1 分钟后
    # 第一段 1 出发（变动），第二段 3 出发（原 2 -> 变动），但没有多余变动。
    for n in ["A", "B", "C"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    add_segment(client, "B", "C", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5, destination="C")

    client.post("/api/tide/corrections", json={"corrections": {0: 0.0}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    assert rs["changed_leg_count"] == 2
    assert [l["start_minute"] for l in rs["itinerary"]] == [1, 3]
    assert rs["arrival_minute"] == 5


# ---------------------------------------------------------------------------
# 4. 择优：先最少变动航段数（利用原等待吸收扰动，不级联推迟后续段）
# ---------------------------------------------------------------------------

def test_reschedule_minimize_changed_legs_absorbs_with_wait(client):
    # A->B（航时1，深6）；B->C（航时1，深4，仅第5分钟潮位足够）。
    # 原快照：s1 第0分钟出发，B 等待至第5分钟走 s2，6 抵达。
    # 更正使第0分钟低潮、第1分钟恢复：s1 推迟到1出发（变动），s2 仍在5出发
    # （原等待被吸收，不级联），变动段数=1，抵达仍为6。
    for n in ["A", "B", "C"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=1, depth=6.0)
    s2 = add_segment(client, "B", "C", travel_time=1, depth=4.0)
    levels = {0: 1.0, **_level(1, 4, 0.0), 5: 3.0, **_level(6, 19, 0.0)}
    rev1 = seed_initial_revision(levels)
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5,
                         destination="C", window=20)
    assert [l["seg_id"] for l in trip["itinerary"]] == [1, s2]
    assert [l["start_minute"] for l in trip["itinerary"]] == [0, 5]
    assert trip["arrival_minute"] == 6

    # 更正第0分钟低潮（s1 不可行）、第1分钟潮1.0（s1 可行、s2 仍不可行）
    client.post("/api/tide/corrections", json={"corrections": {0: 0.0, 1: 1.0}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    assert rs["changed_leg_count"] == 1
    assert [l["start_minute"] for l in rs["itinerary"]] == [1, 5]
    assert rs["arrival_minute"] == 6  # 抵达不变
    assert rs["changed_legs"] == [{
        "leg_index": 0, "seg_id": 1,
        "original_departure_minute": 0, "new_departure_minute": 1,
    }]


# ---------------------------------------------------------------------------
# 5. 择优：变动数相同时抵达最早；抵达再相同按出发分钟序列字典序
# ---------------------------------------------------------------------------

def test_reschedule_earliest_arrival_when_changed_count_ties(client):
    # 单段航时3，原快照第5分钟出发（5,6,7 占用）。
    # 更正后 5..8 低潮、9 起恢复：可行最早出发9 -> 抵达12。
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in trip["itinerary"]] == [5]

    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(5, 9)}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    assert rs["changed_leg_count"] == 1
    assert rs["itinerary"][0]["start_minute"] == 9
    assert rs["arrival_minute"] == 12


def test_reschedule_lexicographic_departures_when_arrival_ties(client):
    # 三段航时均为1：s1 A->B 深6（需潮位>=0.5），s2 B->C 深4（需>=2.5），
    # s3 C->D 深3（需>=4.0）。修订#1 全程潮位4.5，原快照 (0,1,2)，3 抵达。
    #
    # 修订#2 潮位：0->0.0，1/2/3->3.0，4->4.5，其余0.0。
    #   * s1 在0不可行（必变），最早1出发；
    #   * s1 最早1出发 => s2 最早2出发（原1，必变），s2 在2、3均可行；
    #   * s3 在3不可行（3+3=6<6.5）、在4可行 => 最早4出发（原2，必变）。
    # 故最少变动数=3；存在两个同为3变动、同抵达5的解：
    #   甲 (1,2,4)：在 C 等1分钟；乙 (1,3,4)：在 B 等1分钟。
    # 出发序列字典序 (1,2,4) < (1,3,4) -> 取甲。
    for n in ["A", "B", "C", "D"]:
        add_node(client, n)
    add_segment(client, "A", "B", travel_time=1, depth=6.0)
    add_segment(client, "B", "C", travel_time=1, depth=4.0)
    add_segment(client, "C", "D", travel_time=1, depth=3.0)
    rev1 = seed_initial_revision(_level(0, 19, 4.5))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5,
                         destination="D", window=20)
    assert [l["start_minute"] for l in trip["itinerary"]] == [0, 1, 2]
    assert trip["arrival_minute"] == 3

    corrections = {0: 0.0, 1: 3.0, 2: 3.0, 3: 3.0, 4: 4.5}
    corrections.update({m: 0.0 for m in range(5, 20)})
    client.post("/api/tide/corrections", json={"corrections": corrections})

    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    rs = data["reschedule"]
    assert rs["changed_leg_count"] == 3
    assert [l["start_minute"] for l in rs["itinerary"]] == [1, 2, 4]
    assert rs["arrival_minute"] == 5
    assert rs["total_wait_minutes"] == 2


# ---------------------------------------------------------------------------
# 6. 窗口内无改期解：返回原风险与 no_solution，快照不变
# ---------------------------------------------------------------------------

def test_reschedule_no_solution(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision({**_level(0, 4, 0.0), **_level(5, 19, 3.0)})
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)
    assert [l["start_minute"] for l in trip["itinerary"]] == [5]

    # 更正：全窗口低潮，任何出发都不可行
    client.post("/api/tide/corrections",
                json={"corrections": {m: 0.0 for m in range(0, 20)}})
    data = _review(client, trip["trip_id"]).json()
    assert data["safe"] is False
    assert data["no_solution"] is True
    assert data["reschedule"] is None
    assert data["first_violation"]["minute"] == 5

    # 快照不被修改
    g = client.get(f"/api/trips/{trip['trip_id']}").json()
    assert g["itinerary"] == trip["itinerary"]
    assert g["revision"] == rev1


# ---------------------------------------------------------------------------
# 7. 复核只读：任何情况都不产生/修改确认行程
# ---------------------------------------------------------------------------

def test_review_never_mutates_snapshot(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)

    client.post("/api/tide/corrections", json={"corrections": {0: 0.0, 1: 0.0}})
    _review(client, trip["trip_id"])
    trips = client.get("/api/trips").json()
    assert len(trips) == 1
    assert trips[0]["itinerary"] == trip["itinerary"]


# ---------------------------------------------------------------------------
# 8. 错误语义：未知行程 / 未知修订 / 缺潮位
# ---------------------------------------------------------------------------

def test_review_unknown_trip(client):
    r = _review(client, 9999)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TRIP_NOT_FOUND"
    assert r.json()["error"]["details"]["trip_id"] == 9999


def test_review_unknown_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev)

    r = _review(client, trip["trip_id"], 4242)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "TIDE_REVISION_NOT_FOUND"
    assert r.json()["error"]["details"]["revision"] == 4242


def test_review_missing_tide_uses_existing_semantics(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1)

    # 新建一个稀疏修订（基于 rev1 覆盖后不会稀疏；直接插修订模拟缺数据）
    conn = connect()
    rev2 = conn.execute(
        "INSERT INTO tide_revisions(note) VALUES (?)", ("稀疏修订",)
    ).lastrowid
    conn.executemany(
        "INSERT INTO tide_values(revision, minute, level) VALUES (?, ?, ?)",
        [(rev2, m, 3.0) for m in range(0, 10)],
    )
    conn.close()

    r = _review(client, trip["trip_id"], rev2)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "MISSING_TIDE_DATA"
    assert err["details"]["total_missing"] == 10


def test_review_defaults_to_latest_revision(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    trip = _plan_confirm(client, rev1, draft=6.0, margin=0.5)

    r = client.post("/api/tide/corrections",
                    json={"corrections": {0: 0.0, 1: 0.0}})
    rev2 = r.json()["revision"]

    # 不带 target_revision -> 取最新
    r = client.post(f"/api/trips/{trip['trip_id']}/review", json={})
    assert r.status_code == 200
    assert r.json()["target_revision"] == rev2

    # 空 body 也可以
    r = client.post(f"/api/trips/{trip['trip_id']}/review")
    assert r.status_code == 200
    assert r.json()["target_revision"] == rev2
