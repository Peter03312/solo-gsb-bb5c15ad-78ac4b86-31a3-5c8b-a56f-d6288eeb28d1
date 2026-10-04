"""不安全航次改期的确认执行（原行程作废 + 新行程确认，单事务幂等）。

调度员先经只读接口 ``POST /api/trips/{id}/review`` 取得复核建议，再凭
**已确认行程号、目标潮位修订号、复核建议中的逐段出发分钟与非空幂等键**
执行改期（``POST /api/trips/{id}/reschedule``）。整个状态变更在单个
``BEGIN IMMEDIATE`` 事务内完成：

0. **幂等重放优先于一切**：同键同内容（行程号 / 目标修订 / 逐段出发分钟
   一致）直接返回**首次结果**——即使此后目标修订已不再是最新、原行程已被
   本次改期作废、占用/管制/日历又发生变化，也不复算、不再确认；同键异内容
   （或不同目标修订）-> ``409 RESCHEDULE_KEY_CONFLICT``。
1. **原行程仍有效**：确认快照存在且未作废；未知行程 ->
   ``404 TRIP_NOT_FOUND``，已作废 -> ``409 TRIP_ALREADY_VOIDED``。
2. **档案核对**（仅当原快照固化了船号引用）：关联档案船仍须**启用且版本与
   快照一致**——档案停用 ``SHIP_DISABLED``、版本已变
   ``SHIP_VERSION_STALE``、未知船号 ``SHIP_NOT_FOUND``。复算船舶参数始终
   取自**快照**（吃水/水面以上高度/余量），不读取档案当前参数。
3. **目标修订为最新且存在**：未知修订 -> ``404 TIDE_REVISION_NOT_FOUND``；
   目标修订已过期（又出现更新修订）-> ``409 REVISION_STALE``。
4. **原快照在该修订下不安全**：与只读复核同一口径逐分钟按误差带校验；
   原行程仍安全 -> ``409 RESCHEDULE_NOT_NEEDED``（无需改期）。
5. **提交方案与复算一致**：按原航段编号顺序、快照船舶参数、原起始分钟、
   原窗口及快照泊位作业分钟，在**当前分时航时管制、狭窄水道组、泊位日历
   与其他有效占用（排除原行程）**下重算最优建议（择优：变动航段数最少 ->
   抵达最早 -> 出发分钟序列字典序最小）。窗口内无解 ->
   ``409 RESCHEDULE_NO_SOLUTION``；提交的逐段出发分钟段数不符
   ``422 VALIDATION_ERROR``，与复算最优出发分钟不一致 ->
   ``409 PLAN_MISMATCH``（details 回传复算结果）。
6. 通过后在同一事务内：
   * 插入**新确认快照**：固化目标修订号、快照船舶参数与档案引用、复算行程、
     原泊位与即时作业区间——新占用随即生效；
   * 插入原行程的**不可变作废记录**：旧占用（航段、狭窄水道组与泊位作业）
     立即失效；作废原因由系统按新旧行程号生成（该接口不接受自定义原因）；
   * 原账单若存在，在**同一事务内等额冲销一次**（沿用
     ``services/toll.py`` 的作废冲销语义，版本与金额取账单原值相反数；
     原行程未开单则无冲销）；重复执行幂等重放，绝不二次冲销。

新行程**待另行开单**（本接口不出账单）。响应给出新旧行程号、复算行程与
冲销状态。原确认快照、批次幂等记录及现有接口契约一律保持不变。
"""
import hashlib
import json
import sqlite3
from typing import Optional

from ..errors import ApiError
from .reschedule import _check_original, _load_review_context, solve_reschedule
from .toll import _load_reversal


def content_hash(trip_id: int, target_revision: int,
                 departure_minutes: list[int]) -> str:
    """提交内容（不含幂等键本身）的规范化 SHA-256。

    与批次确认同一口径：紧凑分隔符、保留字段顺序的规范化 JSON。
    """
    payload = {
        "trip_id": trip_id,
        "target_revision": target_revision,
        "departure_minutes": list(departure_minutes),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---- 内部小工具 ------------------------------------------------------------------

def _reversal_out(conn, trip_id: int) -> Optional[dict]:
    """读取行程账单的等额冲销单视图；无账单或无冲销返回 None。"""
    reversal = _load_reversal(conn, trip_id)
    if reversal is None:
        return None
    return reversal


def _void_reason(old_trip_id: int, new_trip_id: int, revision: int) -> str:
    """改期作废的系统原因（非空；作废记录仍可经既有作废查询接口读取）。"""
    return (f"改期至新行程 #{new_trip_id}（目标潮位修订 #{revision}），"
            f"原行程 #{old_trip_id} 由调度改期确认自动作废")


# ---- 主流程 ----------------------------------------------------------------------

def confirm_reschedule(conn, trip_id: int, body) -> tuple[dict, bool]:
    """执行不安全航次改期。返回 (响应体, replayed)。

    首次成功 (响应, False)（HTTP 201）；同键同内容重试返回首次结果
    (响应, True)（HTTP 200，即使修订此后又变化也先重放）。
    任何校验失败均抛出 ApiError，事务回滚、不改变任何状态。
    """
    key = body.idempotency_key
    target_revision = body.target_revision
    departure_minutes = list(body.departure_minutes)
    digest = content_hash(trip_id, target_revision, departure_minutes)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # 0) 幂等重放优先于一切（修订过期、原行程已作废也仍返回首次结果）
        existing = conn.execute(
            "SELECT target_revision, content_hash, result FROM trip_reschedules"
            " WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["content_hash"] != digest:
                raise ApiError(
                    409, "RESCHEDULE_KEY_CONFLICT",
                    "同一改期幂等键已用于不同的提交内容（不同行程号/目标修订/"
                    "逐段出发分钟），拒绝执行",
                    {"idempotency_key": key,
                     "existing_target_revision": existing["target_revision"]},
                )
            conn.execute("COMMIT")
            replay = json.loads(existing["result"])
            replay["replayed"] = True
            return replay, True

        # 1) 原行程快照存在且仍有效（未作废）
        snap_row = conn.execute(
            "SELECT * FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone()
        if snap_row is None:
            raise ApiError(404, "TRIP_NOT_FOUND",
                           f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})
        if conn.execute(
            "SELECT 1 FROM voided_trips WHERE trip_id = ?", (trip_id,)
        ).fetchone() is not None:
            raise ApiError(
                409, "TRIP_ALREADY_VOIDED",
                f"行程 {trip_id} 已作废，不能再执行改期确认",
                {"trip_id": trip_id},
            )

        # 2) 提交逐段出发分钟的段数须与快照航段数一致（内容在下步比对）
        legs = json.loads(snap_row["itinerary"])
        if len(departure_minutes) != len(legs):
            raise ApiError(
                422, "VALIDATION_ERROR",
                f"逐段出发分钟个数（{len(departure_minutes)}）必须与原快照航段数"
                f"（{len(legs)}）一致",
                {"trip_id": trip_id, "provided": len(departure_minutes),
                 "expected": len(legs)},
            )

        # 3) 档案核对（仅快照固化了船号引用时）：船仍启用且版本未变。
        #    复算参数始终用快照值；档案停用/版本已变/档案被删均拒绝。
        if snap_row["ship_no"] is not None:
            ver = conn.execute(
                "SELECT version, enabled FROM ship_versions"
                " WHERE ship_no = ? ORDER BY version DESC LIMIT 1",
                (snap_row["ship_no"],),
            ).fetchone()
            if ver is None:
                raise ApiError(
                    404, "SHIP_NOT_FOUND",
                    f"未知船号: {snap_row['ship_no']}（原快照关联档案已不存在），"
                    "改期确认失败",
                    {"ship_no": snap_row["ship_no"]},
                )
            if not ver["enabled"]:
                raise ApiError(
                    409, "SHIP_DISABLED",
                    f"船号 {snap_row['ship_no']} 的档案已停用"
                    f"（当前版本 #{ver['version']}），改期确认失败",
                    {"ship_no": snap_row["ship_no"], "version": ver["version"]},
                )
            if ver["version"] != snap_row["ship_version"]:
                raise ApiError(
                    409, "SHIP_VERSION_STALE",
                    f"船号 {snap_row['ship_no']} 的档案已变更到版本 "
                    f"#{ver['version']}，原快照所据版本 "
                    f"#{snap_row['ship_version']} 已过期，改期确认失败",
                    {"ship_no": snap_row["ship_no"],
                     "provided_version": snap_row["ship_version"],
                     "current_version": ver["version"]},
                )

        # 4) 目标修订存在且仍为最新（过期冲突，不重算、不改状态）
        if conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (target_revision,)
        ).fetchone() is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND",
                f"潮位修订不存在: {target_revision}",
                {"revision": target_revision},
            )
        latest = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if latest is None or latest["revision"] != target_revision:
            raise ApiError(
                409, "REVISION_STALE",
                f"潮位已更新到修订 #{latest['revision'] if latest else None}，"
                f"目标修订 #{target_revision} 已过期，请重新复核取建议",
                {"provided_revision": target_revision,
                 "current_revision": latest["revision"] if latest else None},
            )

        # 5) 加载复核上下文（全窗口潮位缺失 -> MISSING_TIDE_DATA），
        #    原快照在目标修订下必须不安全
        ctx = _load_review_context(conn, trip_id, target_revision)
        violation = _check_original(
            ctx["legs"], ctx["segs"], ctx["tide"],
            ctx["draft"], ctx["height"], ctx["margin"],
        )
        if violation is None:
            raise ApiError(
                409, "RESCHEDULE_NOT_NEEDED",
                f"原行程 #{trip_id} 在目标修订 #{target_revision} 下仍安全，"
                "无需改期，拒绝执行",
                {"trip_id": trip_id, "target_revision": target_revision},
            )

        # 6) 按当前管制 / 组 / 泊位日历与其他有效占用（排除原行程）重算最优建议
        resched = solve_reschedule(conn, ctx)
        if resched is None:
            raise ApiError(
                409, "RESCHEDULE_NO_SOLUTION",
                f"原行程 #{trip_id} 在目标修订 #{target_revision} 下不安全，且"
                "按当前航时管制、狭窄水道组、泊位日历与其他有效占用重算后窗口内"
                "已无改期解，拒绝执行",
                {"trip_id": trip_id, "target_revision": target_revision,
                 "first_violation": violation},
            )

        canonical_departures = [
            leg["start_minute"] for leg in resched["itinerary"]
        ]
        if departure_minutes != canonical_departures:
            raise ApiError(
                409, "PLAN_MISMATCH",
                "提交的逐段出发分钟与事务内复算的最优改期建议不一致"
                "（航时管制/狭窄水道组/泊位日历或其他有效占用可能已变化），"
                "改期确认失败",
                {"trip_id": trip_id, "target_revision": target_revision,
                 "submitted_departure_minutes": departure_minutes,
                 "recalculated": resched},
            )

        # 7) 写入新确认快照（固化目标修订、快照参数与档案引用、原泊位作业区间）
        cur = conn.execute(
            "INSERT INTO confirmed_trips"
            " (revision, origin, destination, start_minute, window_minutes,"
            "  draft, height, margin, arrival_minute, itinerary,"
            "  ship_no, ship_version, berth_id, work_minutes,"
            "  work_start_minute, work_end_minute)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_revision, ctx["origin"], ctx["destination"],
             ctx["start"], ctx["window"],
             ctx["draft"], ctx["height"], ctx["margin"],
             resched["arrival_minute"],
             json.dumps(resched["itinerary"]),
             snap_row["ship_no"], snap_row["ship_version"],
             resched["berth_id"], resched["work_minutes"],
             resched["work_start_minute"], resched["work_end_minute"]),
        )
        new_trip_id = cur.lastrowid

        # 8) 作废原行程（系统原因，不可变记录）；旧占用经 voided_trips 即时失效。
        #    唯一性由 voided_trips 主键兜底（本事务上方已校验未作废）。
        reason = _void_reason(trip_id, new_trip_id, target_revision)
        conn.execute(
            "INSERT INTO voided_trips(trip_id, reason) VALUES (?, ?)",
            (trip_id, reason),
        )
        void_row = conn.execute(
            "SELECT reason, voided_at FROM voided_trips WHERE trip_id = ?",
            (trip_id,),
        ).fetchone()

        # 9) 原账单若存在，在同一事务内等额冲销一次（未开单为空操作）。
        #    新行程不在此开单，待财务另行调用既有开单接口。
        from .toll import create_reversal_in_void
        create_reversal_in_void(conn, trip_id)
        reversal = _reversal_out(conn, trip_id)

        result = {
            "idempotency_key": key,
            "old_trip_id": trip_id,
            "new_trip_id": new_trip_id,
            "target_revision": target_revision,
            "snapshot_revision": ctx["snapshot_revision"],
            "replayed": False,
            "reschedule": resched,
            "old_void": {
                "trip_id": trip_id,
                "reason": void_row["reason"],
                "voided_at": void_row["voided_at"],
            },
            "reversal_created": reversal is not None,
            "reversal": reversal,
        }

        # 10) 幂等记录最后写入；唯一键竞争 -> 回滚后按首次记录重放/冲突
        try:
            conn.execute(
                "INSERT INTO trip_reschedules"
                " (idempotency_key, old_trip_id, target_revision,"
                "  content_hash, result)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, trip_id, target_revision, digest,
                 json.dumps(result, ensure_ascii=False)),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            row = conn.execute(
                "SELECT content_hash, result FROM trip_reschedules"
                " WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                raise ApiError(409, "RESCHEDULE_KEY_CONFLICT",
                               "改期幂等键并发冲突，请换用新的幂等键重试",
                               {"idempotency_key": key})
            if row["content_hash"] != digest:
                raise ApiError(
                    409, "RESCHEDULE_KEY_CONFLICT",
                    "同一改期幂等键已用于不同的提交内容，拒绝执行",
                    {"idempotency_key": key},
                )
            replay = json.loads(row["result"])
            replay["replayed"] = True
            return replay, True

        conn.execute("COMMIT")
        return result, False
    except ApiError:
        # 竞争分支可能已自行提交/回滚；仅当仍在事务中时回滚
        _safe_rollback(conn)
        raise
    except Exception:
        _safe_rollback(conn)
        raise


def _safe_rollback(conn) -> None:
    """回滚（并发竞争分支可能已自行回滚；忽略“无事务”错误）。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass
