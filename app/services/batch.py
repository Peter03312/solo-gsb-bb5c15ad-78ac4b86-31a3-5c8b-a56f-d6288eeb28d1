"""同潮位修订下的批次预排与批次确认。

调度员把同一潮位修订下的 2～5 艘船按优先顺序作为一个批次：

* **预排**（只读，不占位）：逐艘沿用单船 `find_itinerary` 的最早抵达 +
  同刻航段编号序列字典序择优；排序靠后的船除避开**已确认占用**外，还须
  避开本批前船的**拟占用**。任一船无解即指出首个失败序号。
* **确认**（单个 `BEGIN IMMEDIATE` 事务）：核对修订是否仍为最新
  （过期 `REVISION_STALE`），在同一事务内读取当时已确认占用并逐艘复算、
  逐段比对（前船复算行程同样作为后船的占用）；任一船无解或不一致均
  `PLAN_MISMATCH`，**全批不保存**。通过后一次写入全部不可变行程快照，
  并以批次幂等键登记首次结果：同键同内容重试返回原结果（即使修订已
  更新），同键异内容 `BATCH_KEY_CONFLICT`。
"""
import hashlib
import json
import sqlite3
from typing import Iterable, Optional

from ..errors import ApiError
from .berths import load_berth_occupancy
from .groups import expand_group_occupancy, load_groups
from .planning import find_itinerary, load_occupancy
from .ships import resolve_for_confirm, resolve_for_plan


def _tag_ship_error(index: int, exc: ApiError) -> ApiError:
    """给单船层面的错误附加批次内序号（0 起）。"""
    details = dict(exc.details or {})
    details["ship_index"] = index
    return ApiError(exc.status_code, exc.code, exc.message, details)


def _resolve_plan_params(conn, ship) -> dict:
    """预排时解析一艘船的实际参数：船号 -> 档案当前启用版本；否则直接提交值。"""
    if ship.ship_no is not None:
        return resolve_for_plan(conn, ship.ship_no)
    return {
        "ship_no": None,
        "ship_version": None,
        "draft": ship.draft,
        "height": ship.height,
        "margin": ship.margin,
    }


def _resolve_confirm_params(conn, ship) -> dict:
    """确认时核对一艘船的档案（仍启用且版本未变）并取参数；直接提交参数原样返回。"""
    if ship.ship_no is not None:
        return resolve_for_confirm(conn, ship.ship_no, ship.ship_version)
    return {
        "ship_no": None,
        "ship_version": None,
        "draft": ship.draft,
        "height": ship.height,
        "margin": ship.margin,
    }


def _ship_kwargs(ship, params: dict) -> dict:
    return {
        "origin": ship.origin,
        "destination": ship.destination,
        "start_minute": ship.start_minute,
        "window_minutes": ship.window_minutes,
        "draft": params["draft"],
        "height": params["height"],
        "margin": params["margin"],
        "berth_id": ship.berth_id,
        "work_minutes": ship.work_minutes,
    }


def _add_berth_occupancy(blocked: dict[str, list[tuple[int, int]]],
                         result: dict) -> None:
    """把一艘船预排/复算所得泊位作业区间并入后船的泊位拟占用
    {berth_id: [(起, 止), ...]}（与已确认泊位占用同口径，半开、抵达即开工）。"""
    if result.get("berth_id") is None:
        return
    blocked.setdefault(result["berth_id"], []).append(
        (result["work_start_minute"], result["work_end_minute"])
    )
    blocked[result["berth_id"]].sort()


def _add_itinerary_occupancy(blocked: dict[int, set[int]], legs: Iterable[dict],
                             groups=None) -> None:
    """把一段行程（拟占用或已复算占用）并入 blocked：{seg_id: {绝对分钟}}。

    占用语义与 load_occupancy 一致：进入分钟 d 占用 d..d+travel_time-1，
    抵达分钟不占。groups 给定时按当前狭窄水道组关系把占用展开到同组全部
    成员（船进入组内任一航段即独占整组，批内后船同样受此约束）。
    """
    for leg in legs:
        d, t = leg["start_minute"], leg["travel_time"]
        blocked.setdefault(leg["seg_id"], set()).update(range(d, d + t))
    if groups is not None:
        seg_to_group, group_members = groups
        expand_group_occupancy(blocked, seg_to_group, group_members)


def _norm_legs(legs) -> list[tuple]:
    """逐段比对用的规范化元组序列（与单船确认口径一致）。"""
    out = []
    for l in legs:
        l = l if isinstance(l, dict) else l.model_dump()
        out.append(
            (l["seg_id"], l["origin"], l["destination"],
             l["start_minute"], l["end_minute"], l["travel_time"])
        )
    return out


def _arrival_of(ship, legs) -> int:
    if not legs:
        return ship.start_minute
    last = legs[-1]
    return last["end_minute"] if isinstance(last, dict) else last.end_minute


def _resolve_revision(conn, revision: Optional[int]) -> int:
    """解析批次修订号：显式指定须存在，否则取最新。"""
    if revision is not None:
        row = conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND", f"潮位修订不存在: {revision}",
                {"revision": revision},
            )
        return revision
    row = conn.execute(
        "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                       "系统中不存在任何潮位修订，无法规划")
    return row["revision"]


# ---- 批次预排（只读） -------------------------------------------------------------

def plan_batch(conn, ships: list, revision: Optional[int]) -> dict:
    """逐艘规划；返回 {"revision": r, "ships": [find_itinerary 结果, ...]}。

    第 i 艘船的 blocked = 全部已确认占用 ∪ 本批第 0..i-1 艘船的拟占用。
    任一船规划失败（未知节点/缺潮位/无可行行程）时，错误 details 附带
    ship_index（首个失败序号，0 起）后原样抛出。
    """
    rev = _resolve_revision(conn, revision)

    # 已确认占用只取覆盖全批各船窗口的并集区间，一次读入后逐艘复用
    lo = min(s.start_minute for s in ships)
    hi = max(s.start_minute + s.window_minutes - 1 for s in ships)
    blocked = load_occupancy(conn, lo, hi)
    berth_blocked = load_berth_occupancy(conn, lo, hi)
    groups = load_groups(conn)

    results = []
    for i, ship in enumerate(ships):
        try:
            params = _resolve_plan_params(conn, ship)
            result = find_itinerary(
                conn, **_ship_kwargs(ship, params), revision=rev, blocked=blocked,
                berth_blocked=berth_blocked,
            )
        except ApiError as exc:
            raise _tag_ship_error(i, exc)
        _add_itinerary_occupancy(blocked, result["itinerary"], groups)
        _add_berth_occupancy(berth_blocked, result)
        # 按档案预排的船回传船号、版本及所用参数（直接提交参数则为 null）
        result["ship_no"] = params["ship_no"]
        result["ship_version"] = params["ship_version"]
        results.append({"index": i, **result})
    return {"revision": rev, "ships": results}


# ---- 批次确认（事务 + 幂等） -------------------------------------------------------

def content_hash(body) -> str:
    """提交内容（不含幂等键本身）的规范化 SHA-256。

    以 Pydantic 的规范化 JSON（紧凑分隔符、保留字段顺序）序列化
    revision 与 ships（含回填行程），作为同键异内容判定依据。
    """
    payload = body.model_dump(exclude={"idempotency_key"})
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _stale_error(provided: int, current: Optional[int]) -> ApiError:
    return ApiError(
        409,
        "REVISION_STALE",
        f"潮位已更新到修订 #{current}，所据修订 #{provided} 已过期，请重新预排",
        {"provided_revision": provided, "current_revision": current},
    )


def _store_idempotency_record(conn, key: str, revision: int,
                              digest: str, result: dict) -> bool:
    """登记幂等记录。键已存在时返回 False（由调用方判定重放/冲突）。"""
    try:
        conn.execute(
            "INSERT INTO batch_confirmations"
            " (idempotency_key, revision, content_hash, result)"
            " VALUES (?, ?, ?, ?)",
            (key, revision, digest, json.dumps(result, ensure_ascii=False)),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def _replay_or_conflict(conn, key: str, revision: int, digest: str) -> dict:
    """幂等键已存在时：同内容返回重放结果；异内容抛 BATCH_KEY_CONFLICT。"""
    row = conn.execute(
        "SELECT revision, content_hash, result FROM batch_confirmations"
        " WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row is None:
        # 理论上不会到这里（仅当并发竞争方在 ROLLBACK 后又删除），按冲突处理
        raise ApiError(409, "BATCH_KEY_CONFLICT",
                       "批次幂等键并发冲突，请换用新的幂等键重试",
                       {"idempotency_key": key})
    if row["content_hash"] != digest or row["revision"] != revision:
        raise ApiError(
            409, "BATCH_KEY_CONFLICT",
            "同一批次幂等键已用于不同的提交内容（或不同修订号），拒绝保存",
            {"idempotency_key": key, "existing_revision": row["revision"]},
        )
    return json.loads(row["result"])


def confirm_batch(conn, body) -> tuple[dict, bool]:
    """事务内确认整批。返回 (响应体, replayed)。

    流程（单事务 BEGIN IMMEDIATE）：
      0. 幂等键已存在且内容一致 -> 直接返回原结果（即使修订已更新）；
         内容不一致 -> BATCH_KEY_CONFLICT；
      1. 按船号提交的船核对档案仍启用且版本未变（SHIP_NOT_FOUND /
         SHIP_DISABLED / SHIP_VERSION_STALE，附 ship_index，全批回滚）；
      2. 修订须仍为最新，否则 REVISION_STALE；
      3. 读取当时全部已确认占用，逐艘复算：无解 -> PLAN_MISMATCH；
         与提交行程逐段不一致 -> PLAN_MISMATCH；前船复算行程并入后船占用；
      4. 全部通过后一次写入全部快照（固化所用参数与档案引用）+ 幂等记录后
         COMMIT，任一失败全批回滚。
    """
    key = body.idempotency_key
    digest = content_hash(body)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 0) 幂等重放优先于一切（修订过期也仍返回原结果）
        existing = conn.execute(
            "SELECT revision, content_hash, result FROM batch_confirmations"
            " WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != digest or existing["revision"] != body.revision:
                raise ApiError(
                    409, "BATCH_KEY_CONFLICT",
                    "同一批次幂等键已用于不同的提交内容（或不同修订号），拒绝保存",
                    {"idempotency_key": key, "existing_revision": existing["revision"]},
                )
            conn.execute("COMMIT")
            replay = json.loads(existing["result"])
            replay["replayed"] = True
            return replay, True

        # 1) 档案核对：按船号提交的船须仍启用且版本未变（任一失效全批回滚）
        ship_params: list[dict] = []
        for i, ship in enumerate(body.ships):
            try:
                ship_params.append(_resolve_confirm_params(conn, ship))
            except ApiError as exc:
                raise _tag_ship_error(i, exc)

        # 2) 修订是否仍为最新
        latest = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if latest is None or latest["revision"] != body.revision:
            raise _stale_error(body.revision, latest["revision"] if latest else None)

        # 3) 当时已确认占用（覆盖全批窗口的并集区间；航段与泊位分别加载）
        lo = min(s.start_minute for s in body.ships)
        hi = max(s.start_minute + s.window_minutes - 1 for s in body.ships)
        blocked = load_occupancy(conn, lo, hi)
        berth_blocked = load_berth_occupancy(conn, lo, hi)
        groups = load_groups(conn)

        trip_refs: list[dict] = []
        for i, ship in enumerate(body.ships):
            params = ship_params[i]
            try:
                recalculated = find_itinerary(
                    conn, **_ship_kwargs(ship, params),
                    revision=body.revision, blocked=blocked,
                    berth_blocked=berth_blocked,
                )
            except ApiError as exc:
                if exc.code == "NO_FEASIBLE_ITINERARY":
                    raise _plan_mismatch(
                        i, reason="no_feasible_itinerary",
                        message="占用或潮位已变化：该船窗口内已不存在可行行程"
                                "（含终点泊位开放/作业占用变化），整批确认失败，"
                                "全批不保存",
                    )
                raise _tag_ship_error(i, exc)

            submitted = _norm_legs(ship.itinerary)
            canonical = _norm_legs(recalculated["itinerary"])
            if submitted != canonical or _arrival_of(ship, ship.itinerary) != \
                    recalculated["arrival_minute"]:
                raise _plan_mismatch(
                    i, reason="itinerary_mismatch",
                    message="该船行程复核结果与所提交行程不一致（窗口/船舶参数/"
                            "潮位/航段占用/泊位开放日历或作业占用可能已变化），"
                            "整批确认失败，全批不保存",
                    recalculated=recalculated,
                )
            # 泊位作业区间亦须与复算结果一致（抵达已在上方比对；此处再比对
            # 泊位与作业分钟——作业区间完全由抵达+作业分钟决定）
            if ship.berth_id != recalculated["berth_id"] or \
                    ship.work_minutes != recalculated["work_minutes"]:
                raise _plan_mismatch(
                    i, reason="berth_mismatch",
                    message="该船泊位/作业分钟复核结果与提交不一致，整批确认失败，"
                            "全批不保存",
                    recalculated=recalculated,
                )

            # 4a) 先保存该船不可变快照（同一事务，失败整体回滚）；
            #     快照固化所用参数与档案引用（船号 + 版本，无档案则为 NULL），
            #     指定泊位时同时固化泊位编号与作业区间（抵达即开工）
            cur = conn.execute(
                "INSERT INTO confirmed_trips"
                " (revision, origin, destination, start_minute, window_minutes,"
                "  draft, height, margin, arrival_minute, itinerary,"
                "  ship_no, ship_version, berth_id, work_minutes,"
                "  work_start_minute, work_end_minute)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (body.revision, ship.origin, ship.destination, ship.start_minute,
                 ship.window_minutes, params["draft"], params["height"],
                 params["margin"],
                 recalculated["arrival_minute"],
                 json.dumps(recalculated["itinerary"]),
                 params["ship_no"], params["ship_version"],
                 recalculated["berth_id"], recalculated["work_minutes"],
                 recalculated["work_start_minute"],
                 recalculated["work_end_minute"]),
            )
            trip_refs.append({
                "index": i,
                "trip_id": cur.lastrowid,
                "revision": body.revision,
                "ship_no": params["ship_no"],
                "ship_version": params["ship_version"],
                "berth_id": recalculated["berth_id"],
                "work_minutes": recalculated["work_minutes"],
                "work_start_minute": recalculated["work_start_minute"],
                "work_end_minute": recalculated["work_end_minute"],
                "arrival_minute": recalculated["arrival_minute"],
                "itinerary": recalculated["itinerary"],
            })
            # 前船复算行程作为后船的当时占用（按当前组关系展开到整组；
            # 泊位作业区间并入泊位拟占用）
            _add_itinerary_occupancy(blocked, recalculated["itinerary"], groups)
            _add_berth_occupancy(berth_blocked, recalculated)

        result = {
            "idempotency_key": key,
            "revision": body.revision,
            "replayed": False,
            "trips": trip_refs,
        }

        # 3b) 登记幂等记录（防御并发：唯一键竞争则按重放/冲突判定）
        if not _store_idempotency_record(conn, key, body.revision, digest, result):
            conn.execute("ROLLBACK")
            replayed = _replay_or_conflict(conn, key, body.revision, digest)
            replayed["replayed"] = True
            return replayed, True

        conn.execute("COMMIT")
        return result, False
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _plan_mismatch(index: int, reason: str, message: str,
                   recalculated: Optional[dict] = None) -> ApiError:
    details = {"ship_index": index, "reason": reason}
    if recalculated is not None:
        details["recalculated"] = recalculated
    return ApiError(409, "PLAN_MISMATCH", message, details)
