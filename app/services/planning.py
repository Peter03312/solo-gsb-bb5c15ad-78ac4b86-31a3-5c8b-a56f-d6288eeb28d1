"""潮汐通航行程规划核心算法。

时间轴为绝对"分钟"，与潮位表 minute 列一致。船舶可在节点的任意整分钟
等待；航段一旦进入即连续占用 travel_time 个分钟（进入分钟 d 占用
d, d+1, ..., d+t-1，于 d+t 抵达下一节点）。

约束（占用的每一分钟均须满足，按修订的预测误差带取保守极值）：
  * 深度：静水深度 + 名义潮位 − 误差 ≥ 吃水 + 安全余量（真值可能偏低）
  * 桥梁：桥底净空 − 名义潮位 − 误差 ≥ 水面以上高度 + 安全余量（真值可能偏高，有桥时）
  * 独占：同一航段同一分钟已被其他**已确认**行程占用时不可再进入（进入
    分钟 d 至抵达前一分钟 d+t-1 独占该航段；节点等待不占用航段）
等待不占用航段，不参与校验。

择优：最早抵达；同一抵达时刻按**完整**航段编号序列字典序（小者优先），
等待不产生航段编号、不计入序列。

算法：窗口 <= 1440 分钟，按时间展开三遍 DP：
  1. 正向可达 DP（只记录可达、不保留序列）确定终点最早可接受抵达分钟 T；
  2. 反向可达 DP：状态 (节点, 分钟) 能否仍在 T 抵达/开工，并为每条航段预
     处理"不早于当前分钟的下一个既可行、又能按时抵达的进入分钟"；
  3. 自起点贪心重建完整序列：每一步在"等待"（不追加编号）与各可行航段
     （追加该段编号）所导出的完整序列中取字典序最小者。同一条航段的多个
     可进入分钟里，最早者给出的后缀字典序必不更差（在同一节点，晚一刻的
     可行序列集合是早一刻的子集——等待不增加任何航段编号）。

不能沿用"同一 (节点, 分钟) 只留字典序最小前缀"的旧剪枝：所有航段航时均为
正整数，正航时回路会让长度不同的序列在同一 (节点, 分钟) 会合；当较短序列
恰是较长序列的前缀时，追加同一后缀后字典序大小关系会反转，旧剪枝会把全局
字典序最小的完整航路误删（例：1 A→B、2 B→C、3 C→B、4 B→D，各耗时 1 分
钟，4 仅分钟 3 可行；(1) 与 (1,2,3) 在 (B, 分钟3) 会合，旧法剪得 [1,4]，
正确答案为同刻抵达的 [1,2,3,4]）。
"""
import json
from dataclasses import dataclass
from typing import Optional

from .. import config
from ..errors import ApiError, no_itinerary, tide_missing, unknown_node
from .berths import (
    berth_free_at,
    load_berth_occupancy,
    resolve_plan_berth,
    work_fits_calendar,
)
from .controls import effective_travel_time, load_controls


@dataclass(frozen=True)
class Segment:
    seg_id: int
    origin: str
    destination: str
    travel_time: int
    depth: float
    clearance: Optional[float]


def _load_segments(conn) -> dict[int, Segment]:
    rows = conn.execute(
        "SELECT seg_id, origin, destination, travel_time, depth, clearance"
        " FROM segments ORDER BY seg_id"
    ).fetchall()
    return {
        r["seg_id"]: Segment(
            r["seg_id"], r["origin"], r["destination"],
            r["travel_time"], r["depth"], r["clearance"],
        )
        for r in rows
    }


def _load_tide(conn, revision: int, lo: int, hi: int) -> dict[int, tuple[float, float]]:
    """返回修订 revision 在 [lo, hi] 闭区间内的完整 {minute: (level, error)}。

    更正/误差修订为增量存储，数据沿 base_revision 祖先链还原（未改分钟继承
    基础修订），见 services/tides.py。
    """
    from .tides import load_tide
    return load_tide(conn, revision, lo, hi)


def _minute_ok(seg: Segment, tide: float, error: float,
               draft: float, height: float, margin: float) -> bool:
    """航段在某一分钟是否满足深度/净空约束（按误差带取保守极值）。"""
    eps = config.EPS
    if seg.depth + tide - error + eps < draft + margin:
        return False
    if seg.clearance is not None:
        if seg.clearance - tide - error + eps < height + margin:
            return False
    return True


def load_occupancy(conn, lo: int, hi: int, exclude_trip_id: Optional[int] = None
                   ) -> dict[int, set[int]]:
    """读取 [lo, hi] 闭区间内全部已确认行程的航段占用（含狭窄水道组展开）。

    返回 {seg_id: {占用分钟, ...}}。每段从进入分钟 d 起占用
    d, d+1, …, d+travel_time-1（抵达分钟不占用）。既有快照即使彼此重叠也
    原样保留——它们就是后续规划必须避开的占用。
    exclude_trip_id 用于改期复核：建议须避开其他已确认快照，但排除自身。

    狭窄水道组：快照任一段若属于某组，该段原占用（按其固化航时）展开到
    组内全部成员航段的同一批分钟——船进入组内任一航段即独占整组。组建前
    的已确认快照同样按原航段与固化航时计入新组占用（快照不改写，组建前
    已有的重叠也不清理）。
    """
    from .groups import expand_group_occupancy, load_groups

    # 已作废行程的原确认快照仍保留在 confirmed_trips 中作为历史快照可查，
    # 但其按固化航时计算的航段/组占用不再阻挡后续调度，故 LEFT JOIN 排除。
    sql = (
        "SELECT c.itinerary AS itinerary FROM confirmed_trips c"
        " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
        " WHERE v.trip_id IS NULL"
    )
    params: list = []
    if exclude_trip_id is not None:
        sql += " AND c.trip_id <> ?"
        params.append(exclude_trip_id)
    occupied: dict[int, set[int]] = {}
    for row in conn.execute(sql, params).fetchall():
        for leg in json.loads(row["itinerary"]):
            d, t = leg["start_minute"], leg["travel_time"]
            end = d + t - 1
            if end < lo or d > hi:
                continue
            occupied.setdefault(leg["seg_id"], set()).update(
                range(max(lo, d), min(hi, end) + 1)
            )
    seg_to_group, group_members = load_groups(conn)
    expand_group_occupancy(occupied, seg_to_group, group_members)
    # 展开可能引入 [lo,hi] 之外航段的键，但其分钟来自区间内原占用，裁剪口径不变
    return occupied


def _build_departure_feasible(
    segs: list[Segment],
    tide: dict[int, float],
    start: int,
    window: int,
    draft: float,
    height: float,
    margin: float,
    blocked: Optional[dict[int, set[int]]] = None,
    controls: Optional[dict[int, list[tuple[int, int, int]]]] = None,
) -> tuple[dict[int, list[bool]], dict[int, list[int]]]:
    """每条航段每个可能进入分钟（窗口内局部下标）是否可行，及其实际航时。

    不可行原因有三：该进入分钟选用的实际航时使航程越出窗口、航段在该航程
    的某一分钟不满足潮位误差带水深/净空，或该航程与其他已确认行程的占用
    （blocked：{seg_id: {绝对分钟}}）重叠。航时在**进入分钟**按分时管制
    （controls：{seg_id: [(起, 止, 通行分钟), ...]}，半开区间）选定，途中
    跨越管制边界也不变；未命中区间沿用航段基础航时。用"不可行分钟"前缀
    和，令一次区间查询为 O(1)。

    返回 (feasible, travel_at)：
      feasible[seg_id][i]：于局部分钟 i（绝对分钟 start+i）进入是否可行；
      travel_at[seg_id][i]：于该分钟进入时的实际通行分钟（含不可行情形）。
    """
    blocked = blocked or {}
    controls = controls or {}
    feasible: dict[int, list[bool]] = {}
    travel_at: dict[int, list[int]] = {}
    horizon = start + window

    for seg in segs:
        seg_blocked = blocked.get(seg.seg_id, ())
        seg_controls = controls.get(seg.seg_id, ())
        # bad[i] 对应绝对分钟 start+i
        bad = [0] * window
        for i in range(window):
            m = start + i
            val = tide.get(m)
            if (val is None
                    or not _minute_ok(seg, val[0], val[1], draft, height, margin)
                    or m in seg_blocked):
                bad[i] = 1
        pref = [0] * (window + 1)
        for i in range(window):
            pref[i + 1] = pref[i] + bad[i]

        ok = [False] * (window + 1)   # 局部下标 d-start，0..window
        tt = [0] * (window + 1)       # 各进入分钟实际选用的通行分钟
        base_t = seg.travel_time
        for i in range(0, window + 1):
            dep = start + i
            t = effective_travel_time(base_t, seg_controls, dep)
            tt[i] = t
            if i + t > window:
                continue
            if pref[i + t] - pref[i] == 0:
                ok[i] = True
        feasible[seg.seg_id] = ok
        travel_at[seg.seg_id] = tt
    return feasible, travel_at


def find_itinerary(
    conn,
    origin: str,
    destination: str,
    start_minute: int,
    window_minutes: int,
    draft: float,
    height: float,
    margin: float,
    revision: Optional[int] = None,
    blocked: Optional[dict[int, set[int]]] = None,
    berth_id: Optional[str] = None,
    work_minutes: Optional[int] = None,
    berth_blocked: Optional[dict[str, list[tuple[int, int]]]] = None,
) -> dict:
    """计算最早抵达行程。

    blocked 给出须避开的已确认航段占用 {seg_id: {绝对分钟}}；缺省为不避占
    （仅供不需要占用语义的内部调用）。

    泊位参数（berth_id 与 work_minutes 成对出现，二选一整体缺省）：船抵达
    终点即开工，作业区间 [arrival, arrival+work) 须完整落在该泊位同一开放
    区间、且不与 berth_blocked（{berth_id: [(起, 止), ...]}，半开占用）
    重叠。未指定泊位时求解与响应完全沿用旧语义。berth_blocked 缺省时由
    调用方语义决定：HTTP 单船/批次路径总是显式传入（含本批前船拟占用）。
    未知节点 -> NODE_NOT_FOUND (404)
    未知泊位 -> BERTH_NOT_FOUND (404)；泊位不属终点 -> BERTH_NOT_AT_DESTINATION (422)
    窗口内缺潮位 -> MISSING_TIDE_DATA (422)
    无可行行程 -> NO_FEASIBLE_ITINERARY (404)
    """
    # 1) 节点校验
    node_rows = conn.execute(
        "SELECT node_id FROM nodes WHERE node_id IN (?, ?)", (origin, destination)
    ).fetchall()
    known = {r["node_id"] for r in node_rows}
    if origin not in known:
        raise unknown_node(origin)
    if destination not in known:
        raise unknown_node(destination)

    # 2) 修订号（显式指定或取最新）
    if revision is None:
        row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND",
                "系统中不存在任何潮位修订，无法规划",
            )
        revision = row["revision"]
    else:
        row = conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "TIDE_REVISION_NOT_FOUND", f"潮位修订不存在: {revision}",
                {"revision": revision},
            )

    horizon = start_minute + window_minutes

    # 2b) 泊位参数解析（未知泊位 / 不属终点 / 非法作业分钟在此拒绝）
    berth = resolve_plan_berth(conn, berth_id, work_minutes, destination)
    berth_mode = berth is not None
    if berth_mode:
        berth_occupied = (berth_blocked or {}).get(berth["berth_id"], [])

    # 3) 潮位覆盖校验：占用分钟最晚到 horizon-1；泊位模式作业区间同样须落在
    #    窗口内（arrival+work <= horizon），故潮位仍按整个窗口逐分钟校验
    tide = _load_tide(conn, revision, start_minute, horizon - 1)
    missing = [m for m in range(start_minute, horizon) if m not in tide]
    if missing:
        raise tide_missing(missing)

    # 4) 起终点相同：零航段，立即抵达（泊位模式下须在起点=终点即时开工）
    if origin == destination:
        if berth_mode and not _berth_window_ok(
            start_minute, berth, berth_occupied, horizon
        ):
            raise no_itinerary(origin, destination)
        result = _result(
            revision, origin, destination, start_minute, window_minutes,
            draft, height, margin, arrival=start_minute, legs=[],
        )
        _attach_berth(result, berth, start_minute)
        return result

    segs = _load_segments(conn)
    seg_list = list(segs.values())
    out_segs: dict[str, list[Segment]] = {}
    for s in seg_list:
        out_segs.setdefault(s.origin, []).append(s)

    # 航段分时航时管制：航时在进入分钟选定（未命中区间用基础航时）
    controls = load_controls(conn, [s.seg_id for s in seg_list])
    feasible, travel_at = _build_departure_feasible(
        seg_list, tide, start_minute, window_minutes, draft, height, margin,
        blocked=blocked, controls=controls,
    )

    # 5) 时间展开三遍 DP（状态一律用局部分钟 0..n）
    #
    # 终点在两种模式下都是"吸收态"：船抵达终点即结束（泊位模式抵达即开工，
    # 不能在终点等待或再出发），故终点不进入正向可达集合，落入终点的航段
    # 只在 fresh_dest[抵达分钟] 标记；等待传播因此无需特判终点。
    n = window_minutes

    # ---- 第 1 遍：正向可达，求最早可接受抵达分钟 T -------------------------
    # reach[i]：局部分钟 i 船可能所在的非终点节点（等待自 i-1 原样延续，
    # 航段自 i 出发落入更晚分钟）；fresh_dest[i]：分钟 i 是否有航段新鲜抵达
    # 终点。分钟 i 开始时所有抵达 i 的路径必已在更早迭代展开（航时 ≥1），
    # 故分时航时下"晚出发反而早到"也仍按抵达分钟升序被发现：泊位模式取首个
    # 作业可行的新鲜抵达分钟，非泊位模式取首个新鲜抵达分钟。
    reach: list[set[str]] = [set() for _ in range(n + 1)]
    fresh_dest = [False] * (n + 1)
    reach[0].add(origin)
    arrival_local = None
    for i in range(n + 1):
        if i > 0:
            reach[i].update(reach[i - 1])
        if fresh_dest[i] and (
            not berth_mode
            or _berth_window_ok(start_minute + i, berth, berth_occupied, horizon)
        ):
            arrival_local = i
            break
        for v in tuple(reach[i]):
            for s in out_segs.get(v, ()):
                j = i + travel_at[s.seg_id][i]
                if j <= n and feasible[s.seg_id][i]:
                    if s.destination == destination:
                        fresh_dest[j] = True
                    else:
                        reach[j].add(s.destination)

    if arrival_local is None:
        raise no_itinerary(origin, destination)

    # ---- 第 2/3 遍：反向可达 + 同遍贪心重建完整序列 ------------------------
    # can[i]：{节点: 能否在局部分钟 i 起，仍恰于 T 抵达终点（泊位模式即 T
    # 开工）}。终点只在 (终点, T) 可接受——更早抵达不可等待到 T、更晚则不
    # 是最早抵达。
    # nxt[seg][i]：该航段不早于 i 的下一个"既全程可行、到达点状态仍能按时
    # 完成"的进入分钟（无则 -1）。按 i 倒序自 nxt[seg][i+1] O(1) 继承。
    # best[i][v]：can 为真时的最优完整序列及其本步选择：
    #   ("wait", None, i)   等待到 i+1（不追加航段编号）；
    #   ("seg", s, dep)     于 dep(≥i) 进入航段 s，落入 (s.dest, dep+t)。
    T = arrival_local
    nodes = {s.origin for s in seg_list}
    nodes.update(s.destination for s in seg_list)
    can: list[dict[str, bool]] = [dict() for _ in range(T + 1)]
    best: list[dict[str, tuple]] = [dict() for _ in range(T + 1)]
    nxt: dict[int, list[int]] = {
        s.seg_id: [-1] * (T + 1) for s in seg_list
    }
    best[T][destination] = ((), None)

    for i in range(T, -1, -1):
        can[i][destination] = (i == T)
        # 终点在 i<T 不可接受，故所有"过早到达"的航段选择在此被判否。
        if i < T:
            # 2a) 各航段下一个合格进入分钟（到达状态来自更晚分钟，已算好）
            for s in seg_list:
                j = i + travel_at[s.seg_id][i]
                if (j <= T and feasible[s.seg_id][i]
                        and can[j].get(s.destination, False)):
                    nxt[s.seg_id][i] = i
                else:
                    nxt[s.seg_id][i] = nxt[s.seg_id][i + 1]
            # 2b+3) 非终点节点：在等待与各可行航段导出的完整序列间取字典序
            #      最小者。同序列时优先航段（等待不产生编号，能走即不必等），
            #      保证重建确定性。
            for v in nodes:
                if v == destination:
                    continue
                best_key = None  # (完整序列, 优先级)，优先级 0=航段 1=等待
                choice = None
                if can[i + 1].get(v, False):
                    best_key = (best[i + 1][v][0], 1)
                    choice = ("wait", None, i)
                # out_segs 按 seg_id 升序（seg_list 取自 ORDER BY seg_id）
                for s in out_segs.get(v, ()):
                    dep = nxt[s.seg_id][i]
                    if dep < 0:
                        continue
                    j = dep + travel_at[s.seg_id][dep]
                    seq = (s.seg_id,) + best[j][s.destination][0]
                    key = (seq, 0)
                    if best_key is None or key < best_key:
                        best_key = key
                        choice = ("seg", s, dep)
                if choice is not None:
                    can[i][v] = True
                    best[i][v] = (best_key[0], choice)

    if not can[0].get(origin, False):
        # 理论不可达：正向已找到 T，反向必连通；保留防御性拒绝
        raise no_itinerary(origin, destination)

    # 3) 沿 best 重建航段：等待只推进分钟、不落航段；航段选择的进入分钟
    #    dep 可能晚于当前分钟（其间为节点等待，不计入序列）。
    legs: list[tuple[int, int, int]] = []
    v, i = origin, 0
    while not (v == destination and i == T):
        _, choice = best[i][v]
        kind, s, dep = choice
        if kind == "wait":
            i += 1
            continue
        j = dep + travel_at[s.seg_id][dep]
        legs.append((s.seg_id, dep, j))
        v, i = s.destination, j

    leg_dicts = [
        {
            "seg_id": sid,
            "origin": segs[sid].origin,
            "destination": segs[sid].destination,
            "start_minute": start_minute + pi,
            "end_minute": start_minute + ai,
            # 实际航时以进入分钟当时管制为准（可能与基础航时不同）
            "travel_time": travel_at[sid][pi],
        }
        for sid, pi, ai in legs
    ]
    result = _result(
        revision, origin, destination, start_minute, window_minutes,
        draft, height, margin, arrival=start_minute + arrival_local, legs=leg_dicts,
    )
    # 无论是否指定泊位都附带泊位字段（未指定时为 None，旧响应仅新增 null 字段）
    _attach_berth(result, berth if berth_mode else None,
                  start_minute + arrival_local)
    return result


def _berth_window_ok(arrival: int, berth: dict,
                     occupied: list[tuple[int, int]], horizon: int) -> bool:
    """抵达分钟 arrival 时能否在该泊位完成作业：作业区间不越规划窗口
    （arrival+work <= horizon）、完整落在同一开放区间且不与占用重叠。"""
    if arrival + berth["work_minutes"] > horizon:
        return False
    return (
        work_fits_calendar(arrival, berth["work_minutes"], berth["calendar"])
        and berth_free_at(arrival, berth["work_minutes"], occupied)
    )


def _attach_berth(result: dict, berth: Optional[dict], arrival: int) -> None:
    """把泊位作业结果附加到规划响应：抵达即开工，作业区间固化为
    [arrival, arrival + work_minutes)。未指定泊位时字段为 None。"""
    if berth is None:
        result["berth_id"] = None
        result["work_minutes"] = None
        result["work_start_minute"] = None
        result["work_end_minute"] = None
        return
    result["berth_id"] = berth["berth_id"]
    result["work_minutes"] = berth["work_minutes"]
    result["work_start_minute"] = arrival
    result["work_end_minute"] = arrival + berth["work_minutes"]


def _result(revision, origin, destination, start_minute, window_minutes,
            draft, height, margin, arrival, legs) -> dict:
    sailing = sum(l["travel_time"] for l in legs) if legs else 0
    return {
        "revision": revision,
        "origin": origin,
        "destination": destination,
        "start_minute": start_minute,
        "window_minutes": window_minutes,
        "draft": draft,
        "height": height,
        "margin": margin,
        "arrival_minute": arrival,
        "total_wait_minutes": arrival - start_minute - sailing,
        "itinerary": legs,
    }
