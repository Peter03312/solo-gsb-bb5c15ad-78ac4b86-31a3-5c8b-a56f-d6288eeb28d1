"""潮位更正后的原行程复核与改期。

输入为已确认行程快照（行程号 trip_id）与目标潮位修订号：

1. **原航路复核**：保持快照中的航段编号顺序与原占用分钟，逐段逐分钟按目标
   修订的名义潮位与**误差带**校验水深（名义潮位−误差）与桥高（名义潮位+
   误差，收紧净空），给出原行程是否安全。**复核只检查潮位，不检查与其他
   已确认快照的占用重叠**（既有快照原样保留，其彼此重叠不构成风险）；
   不安全时返回首次违规（航段、分钟、水深/净空原因，以及按对应极值计算
   的实际可用水深/净空）。
2. **改期**：保持原航段编号顺序、船舶参数、原起始分钟与规划窗口不变，仅允许
   在节点重新等待（即只调整各段出发分钟，且出发必须与前段抵达首尾相接）。
   段一旦进入即连续占用 travel_time 分钟，窗口内占用的每一分钟均须满足
   水深/净空约束，且不得与**其他已确认快照**的航段占用重叠（本快照自身
   占用排除：建议不占位，快照不改写）。改期择优依次为：
     a. 相对快照，出发分钟发生变化的航段个数最少；
     b. 抵达分钟最早；
     c. 各段出发分钟序列字典序最小。
3. 窗口内无改期解时返回原行程风险与 no_solution 标记；本服务只读，绝不修改
   已确认快照。

算法（窗口 W ≤ 1440，快照段数 k ≤ W）：每段预处理每个进入分钟是否全程可行
（不可行分钟前缀和，O(1) 查询）；按段做时间展开 DP，状态为「完成前 i 段、
于某分钟抵达该段终点」的 (变化段数, 出发分钟序列)。对第 i 段，每个出发分钟 d
只需要所有抵达分钟 ≤ d 的最优前缀——可用「逐分钟最优前缀」O(W) 扫描完成，
总复杂度 O(k·W)。

剪枝依据：对同一 (i, 抵达分钟)，若 (c1, d1) 字典序小于 (c2, d2)（c 为变化段数，
d 为出发分钟序列），则追加同一后续航段后，变化段数同增 0/1、序列同追加同一
分钟，大小关系不变；故每状态只保留一个值不会丢失最优解。
"""
import json
from typing import Optional

from .. import config
from ..errors import ApiError, tide_missing, trip_not_found
from .berths import berth_free_at, load_berth_occupancy, load_calendar, work_fits_calendar
from .controls import effective_travel_time, load_controls
from .planning import (
    Segment,
    _build_departure_feasible,
    _load_segments,
    load_occupancy,
)


def review_and_reschedule(
    conn,
    trip_id: int,
    revision: Optional[int] = None,
) -> dict:
    """按目标修订复核已确认行程，必要时求改期行程。

    未知行程 -> TRIP_NOT_FOUND (404)
    目标修订不存在 -> TIDE_REVISION_NOT_FOUND (404)
    窗口内缺潮位 -> MISSING_TIDE_DATA (422)
    """
    ctx = _load_review_context(conn, trip_id, revision)

    # 4) 原航路复核：只按目标修订检查潮位（与其他快照的占用重叠不判风险）
    violation = _check_original(
        ctx["legs"], ctx["segs"], ctx["tide"],
        ctx["draft"], ctx["height"], ctx["margin"],
    )
    safe = violation is None

    result = {
        "trip_id": ctx["trip_id"],
        "snapshot_revision": ctx["snapshot_revision"],
        "target_revision": ctx["revision"],
        "origin": ctx["origin"],
        "destination": ctx["destination"],
        "start_minute": ctx["start"],
        "window_minutes": ctx["window"],
        "draft": ctx["draft"],
        "height": ctx["height"],
        "margin": ctx["margin"],
        "original_arrival_minute": ctx["arrival"],
        "safe": safe,
        "first_violation": violation,
        "reschedule": None,
        "no_solution": False,
        "original_itinerary": ctx["legs"],
        # 泊位快照（未指定泊位的旧确认为 None）；潮位复核本身仍只按原快照
        "berth_id": ctx["berth_id"],
        "work_minutes": ctx["work_minutes"],
        "work_start_minute": ctx["work_start_minute"],
        "work_end_minute": ctx["work_end_minute"],
    }

    # 5) 安全（含零航段）：原行程本身即最优改期（0 段变动），无需改期
    if safe:
        return result

    # 6) 不安全：在相同航段序列/参数/窗口下求改期行程。
    resched = solve_reschedule(conn, ctx)
    if resched is None:
        result["no_solution"] = True
    else:
        result["reschedule"] = resched
    return result


def solve_reschedule(conn, ctx: dict) -> Optional[dict]:
    """按已加载的复核上下文求改期建议（无解返回 None）。

    占用须避开其他已确认快照，但排除本快照自身（建议不占位）；改期按
    **当前分时航时管制 / 狭窄水道组关系 / 泊位开放日历与其他有效占用**求解
    （已确认快照的既有占用时长不随管制变化，但新行程各段航时以进入分钟当前
    管制计）。原快照指定泊位时，建议须同时在原泊位找到可用即时作业区间。
    """
    blocked = load_occupancy(
        conn, ctx["start"], ctx["start"] + ctx["window"] - 1,
        exclude_trip_id=ctx["trip_id"],
    )
    controls = load_controls(conn, [leg["seg_id"] for leg in ctx["legs"]])
    return _reschedule(
        ctx["legs"], ctx["segs"], ctx["tide"],
        ctx["start"], ctx["window"],
        ctx["draft"], ctx["height"], ctx["margin"], ctx["revision"],
        blocked, controls,
        berth_id=ctx["berth_id"], work_minutes=ctx["work_minutes"],
        berth_calendar=ctx["berth_calendar"],
        berth_occupied=ctx["berth_occupied"],
    )


def _load_review_context(conn, trip_id: int, revision: Optional[int]) -> dict:
    """加载复核 / 改期确认共用的快照、修订潮位、航段与泊位上下文。

    未知行程 -> TRIP_NOT_FOUND (404)；目标修订不存在 ->
    TIDE_REVISION_NOT_FOUND (404)；窗口内缺潮位 -> MISSING_TIDE_DATA (422)。
    """
    # 1) 读取不可变快照（只读，不改写）
    row = conn.execute(
        "SELECT * FROM confirmed_trips WHERE trip_id = ?", (trip_id,)
    ).fetchone()
    if row is None:
        raise trip_not_found(trip_id)
    snap = dict(row)

    legs = json.loads(snap["itinerary"])
    start = snap["start_minute"]
    window = snap["window_minutes"]
    horizon = start + window
    draft, height, margin = snap["draft"], snap["height"], snap["margin"]

    # 泊位快照：原快照指定泊位时，改期建议必须同时在**原泊位**找到可用的
    # 即时作业区间（抵达即开工，按当前日历与当时占用，建议不预留）。
    berth_id = snap["berth_id"]
    work_minutes = snap["work_minutes"]
    berth_calendar = None
    berth_occupied = None
    if berth_id is not None:
        berth_calendar = load_calendar(conn, [berth_id]).get(berth_id, [])
        # 改期建议避开其他有效快照的泊位作业占用，排除自身（自身不挡自己）
        berth_occupied = load_berth_occupancy(
            conn, start, horizon - 1, exclude_trip_id=snap["trip_id"]
        ).get(berth_id, [])

    # 2) 目标修订号（显式指定或取最新），沿用规划接口的缺潮位/未知修订语义
    if revision is None:
        rev_row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if rev_row is None:
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           "系统中不存在任何潮位修订，无法复核")
        revision = rev_row["revision"]
    elif not conn.execute(
        "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
    ).fetchone():
        raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                       f"潮位修订不存在: {revision}", {"revision": revision})

    # 3) 全窗口潮位：任一分钟缺失即报错（与规划一致）
    tide_rows = conn.execute(
        "SELECT minute, level, error FROM tide_values"
        " WHERE revision = ? AND minute BETWEEN ? AND ?",
        (revision, start, horizon - 1),
    ).fetchall()
    tide = {r["minute"]: (r["level"], r["error"]) for r in tide_rows}
    missing = [m for m in range(start, horizon) if m not in tide]
    if missing:
        raise tide_missing(missing)

    segs = _load_segments(conn)

    return {
        "trip_id": snap["trip_id"],
        "snapshot_revision": snap["revision"],
        "revision": revision,
        "origin": snap["origin"],
        "destination": snap["destination"],
        "start": start,
        "window": window,
        "horizon": horizon,
        "draft": draft,
        "height": height,
        "margin": margin,
        "arrival": snap["arrival_minute"],
        "legs": legs,
        "tide": tide,
        "segs": segs,
        "berth_id": berth_id,
        "work_minutes": work_minutes,
        "work_start_minute": snap["work_start_minute"],
        "work_end_minute": snap["work_end_minute"],
        "berth_calendar": berth_calendar,
        "berth_occupied": berth_occupied,
        "snap": snap,
    }


def _violation_reasons(
    seg: Segment, level: float, error: float, draft, height, margin
) -> list[str]:
    """按误差带保守极值判定水深/净空是否违规。"""
    reasons = []
    eps = config.EPS
    if seg.depth + level - error + eps < draft + margin:
        reasons.append("depth")
    if seg.clearance is not None and seg.clearance - level - error + eps < height + margin:
        reasons.append("clearance")
    return reasons


def _check_original(legs, segs, tide, draft, height, margin) -> Optional[dict]:
    """按快照顺序逐段逐分钟复核原占用，返回首次违规（无违规返回 None）。"""
    for idx, leg in enumerate(legs):
        seg = segs[leg["seg_id"]]
        d, t = leg["start_minute"], leg["travel_time"]
        for m in range(d, d + t):
            val = tide.get(m)  # 全窗口已校验，缺失仅可能越出窗口（快照异常）
            if val is None:
                return {
                    "leg_index": idx,
                    "seg_id": seg.seg_id,
                    "origin": seg.origin,
                    "destination": seg.destination,
                    "minute": m,
                    "reasons": ["missing_tide"],
                    "tide_level": None,
                    "tide_error": None,
                    "depth": seg.depth,
                    "clearance": seg.clearance,
                    "required_depth": draft + margin,
                    "required_clearance": height + margin,
                    "available_depth": None,
                    "available_clearance": None,
                }
            level, error = val
            reasons = _violation_reasons(seg, level, error, draft, height, margin)
            if reasons:
                return {
                    "leg_index": idx,
                    "seg_id": seg.seg_id,
                    "origin": seg.origin,
                    "destination": seg.destination,
                    "minute": m,
                    "reasons": reasons,
                    "tide_level": level,
                    "tide_error": error,
                    "depth": seg.depth,
                    "clearance": seg.clearance,
                    "required_depth": draft + margin,
                    "required_clearance": height + margin,
                    # 可用值按误差带对应极值：水深用名义−误差，净空用名义+误差
                    "available_depth": seg.depth + level - error,
                    "available_clearance": (
                        seg.clearance - level - error
                        if seg.clearance is not None else None
                    ),
                }
    return None


def _reschedule(
    legs, segs, tide, start, window, draft, height, margin, revision,
    blocked=None, controls=None,
    berth_id=None, work_minutes=None, berth_calendar=None, berth_occupied=None,
) -> Optional[dict]:
    """固定航段序列的时间展开 DP，返回最优改期或 None。

    blocked 为须避开的其他已确认快照占用 {seg_id: {绝对分钟}}（本快照
    自身已排除）；controls 为当前分时航时管制，各段航时以**出发分钟**
    当时管制计（未命中区间用航段基础航时）。

    work_minutes 非 None（原快照指定泊位）时：每个候选抵达分钟 a 还须令
    作业区间 [a, a+work) 完整落在**原泊位当前日历**的同一开放区间且不与
    其他有效快照的泊位作业占用重叠（抵达即开工；建议不预留）。择优次序
    （变化段数 → 抵达最早 → 出发序列字典序）不变。
    """
    k = len(legs)
    if k == 0:
        return None  # 零航段始终安全，不会进入这里

    used = [segs[leg["seg_id"]] for leg in legs]
    original_deps = [leg["start_minute"] for leg in legs]

    # 每个（快照序中的）航段在各进入分钟是否全程可行（潮位误差带 + 占用），
    # 以及于该分钟进入时的实际航时（当前分时管制）
    feasible, travel_at = _build_departure_feasible(
        used, tide, start, window, draft, height, margin,
        blocked=blocked, controls=controls,
    )

    n = window
    # states[i][t-local]：完成前 i 段、于该分钟抵达第 i 段终点时的最优代价
    #   (相对快照变化段数, 各段出发分钟序列)
    states: list[dict[int, tuple]] = [dict() for _ in range(k + 1)]
    states[0][0] = (0, ())

    for i in range(k):
        seg = used[i]
        tt_arr = travel_at[seg.seg_id]
        cur = states[i]
        nxt = states[i + 1]

        # 按分钟递增维护「抵达分钟 <= 当前出发分钟」的最优前缀
        best_key = None  # (changes, departures)
        for d in range(0, n + 1):
            if d in cur:
                cand = cur[d]
                if best_key is None or cand < best_key:
                    best_key = cand
            if best_key is None:
                continue
            t = tt_arr[d]
            if d + t > n or not feasible[seg.seg_id][d]:
                continue
            changes, deps = best_key
            new_changes = changes + (1 if start + d != original_deps[i] else 0)
            key = (new_changes, deps + (start + d,))
            # 分时航时下不同出发分钟可能同分钟抵达：同一抵达状态只保留最优代价
            old = nxt.get(d + t)
            if old is None or key < old:
                nxt[d + t] = key

    # 泊位约束：候选抵达分钟必须能在原泊位即时开工（完整落在同一当前开放
    # 区间且不与其他有效快照作业占用半开重叠，作业区间不越窗口）。
    def _berth_ok(local_arr: int) -> bool:
        if work_minutes is None:
            return True
        arrival = start + local_arr
        if arrival + work_minutes > start + window:
            return False
        return (
            work_fits_calendar(arrival, work_minutes, berth_calendar or [])
            and berth_free_at(arrival, work_minutes, berth_occupied or [])
        )

    feasible_arrivals = {la: key for la, key in states[k].items() if _berth_ok(la)}
    if not feasible_arrivals:
        return None

    # 全局择优：变化段数最少 -> 抵达最早 -> 出发分钟序列字典序最小
    arrival_local, key = min(
        feasible_arrivals.items(), key=lambda kv: (kv[1][0], kv[0], kv[1][1])
    )
    changes, deps = key
    arrival = start + arrival_local

    itinerary = []
    for i, seg in enumerate(used):
        d = deps[i]
        t = effective_travel_time(
            seg.travel_time, controls.get(seg.seg_id, ()), d
        ) if controls else seg.travel_time
        itinerary.append({
            "seg_id": seg.seg_id,
            "origin": seg.origin,
            "destination": seg.destination,
            "start_minute": d,
            "end_minute": d + t,
            "travel_time": t,
        })

    sailing = sum(leg["travel_time"] for leg in itinerary)
    changed = [
        {"leg_index": i, "seg_id": used[i].seg_id,
         "original_departure_minute": original_deps[i],
         "new_departure_minute": deps[i]}
        for i in range(k) if deps[i] != original_deps[i]
    ]
    return {
        "revision": revision,
        "arrival_minute": arrival,
        "total_wait_minutes": arrival - start - sailing,
        "changed_leg_count": changes,
        "changed_legs": changed,
        "itinerary": itinerary,
        # 原泊位的即时作业区间（抵达即开工）；未指定泊位时为 None
        "berth_id": berth_id,
        "work_minutes": work_minutes,
        "work_start_minute": arrival if work_minutes is not None else None,
        "work_end_minute": arrival + work_minutes if work_minutes is not None else None,
    }
