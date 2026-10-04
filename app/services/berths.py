"""终点泊位：登记、开放日历与泊位占用。

调度员可为**现有节点**登记泊位（全局唯一泊位编号），并以一组互不重叠的
半开分钟区间 ``[start_minute, end_minute)`` **整组替换**该泊位的开放日历
（空组表示全程不开放）。

船在预排（单船或批次）时可指定**终点泊位**与**正整数作业分钟**：

* **抵达即开工**：作业区间为 ``[arrival, arrival + work_minutes)``；
* 作业区间必须**完整落在同一开放区间**内
  （``∃ [s, e): s <= arrival 且 arrival + work_minutes <= e``）；
* 且不与**有效确认行程**（未作废）在该泊位的固化作业区间半开重叠
  （``[work_start, work_end)``）；
* 批次中后船还须避开本批前船的**拟作业占用**。

未指定泊位时规划行为与旧版完全一致（不读取泊位、响应泊位字段为 null）。
日历可随时整组替换，但**不影响已确认快照**——确认快照固化泊位作业区间，
泊位占用按固化区间计算，日历变更不改历史；作废行程即释放其泊位作业占用，
历史快照仍可查。
"""
from typing import Optional

from ..errors import ApiError, berth_exists, berth_not_at_destination, berth_not_found, unknown_node


# ---- 登记 / 查询 -------------------------------------------------------------------

def create_berth(conn, node_id: str, berth_id: str, note=None) -> dict:
    """为现有节点登记泊位。未知节点 404 NODE_NOT_FOUND；泊位已存在
    409 BERTH_EXISTS。"""
    if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is None:
        raise unknown_node(node_id)
    if conn.execute("SELECT 1 FROM berths WHERE berth_id = ?", (berth_id,)).fetchone() is not None:
        raise berth_exists(berth_id)
    conn.execute(
        "INSERT INTO berths(berth_id, node_id, note) VALUES (?, ?, ?)",
        (berth_id, node_id, note),
    )
    return get_berth(conn, berth_id)


def get_berth(conn, berth_id: str) -> dict:
    """读取泊位与其当前开放日历（按起点升序）。未知泊位 404 BERTH_NOT_FOUND。"""
    row = conn.execute(
        "SELECT berth_id, node_id, note, created_at FROM berths WHERE berth_id = ?",
        (berth_id,),
    ).fetchone()
    if row is None:
        raise berth_not_found(berth_id)
    intervals = load_calendar(conn, [berth_id]).get(berth_id, [])
    return {
        "berth_id": row["berth_id"],
        "node_id": row["node_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "calendar": [{"start_minute": s, "end_minute": e} for s, e in intervals],
    }


def list_berths(conn, node_id: Optional[str] = None) -> list[dict]:
    """列出泊位（按泊位编号升序），可按节点过滤。未知节点（过滤时）
    404 NODE_NOT_FOUND。"""
    if node_id is not None:
        if conn.execute("SELECT 1 FROM nodes WHERE node_id = ?", (node_id,)).fetchone() is None:
            raise unknown_node(node_id)
        rows = conn.execute(
            "SELECT berth_id, node_id, note, created_at FROM berths"
            " WHERE node_id = ? ORDER BY berth_id",
            (node_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT berth_id, node_id, note, created_at FROM berths ORDER BY berth_id"
        ).fetchall()
    calendars = load_calendar(conn, [r["berth_id"] for r in rows])
    return [
        {
            "berth_id": r["berth_id"],
            "node_id": r["node_id"],
            "note": r["note"],
            "created_at": r["created_at"],
            "calendar": [
                {"start_minute": s, "end_minute": e}
                for s, e in calendars.get(r["berth_id"], [])
            ],
        }
        for r in rows
    ]


# ---- 开放日历（整组替换） -----------------------------------------------------------

def validate_calendar(items) -> list[tuple[int, int]]:
    """校验提交的开放日历组，返回规范化后的升序 (start, end) 列表（可为空）。

    非法区间 -> 422 INVALID_BERTH_INTERVAL（起点非负、终点大于起点）；
    组内半开交叠 -> 422 OVERLAPPING_BERTH_INTERVAL（首尾相接合法）。
    """
    intervals: list[tuple[int, int]] = []
    for idx, it in enumerate(items):
        start = it["start_minute"]
        end = it["end_minute"]
        if start < 0 or end <= start:
            raise ApiError(
                422,
                "INVALID_BERTH_INTERVAL",
                f"第 {idx} 个开放区间非法：要求 start_minute>=0 且"
                " end_minute>start_minute",
                {"index": idx, "start_minute": start, "end_minute": end},
            )
        intervals.append((start, end))

    intervals.sort(key=lambda x: x[0])
    for i in range(1, len(intervals)):
        prev_start, prev_end = intervals[i - 1]
        start, end = intervals[i]
        if start < prev_end:
            raise ApiError(
                422,
                "OVERLAPPING_BERTH_INTERVAL",
                f"开放日历组内交叠：[{prev_start}, {prev_end}) 与 [{start}, {end})"
                " 相互重叠，拒绝替换",
                {"previous": {"start_minute": prev_start, "end_minute": prev_end},
                 "current": {"start_minute": start, "end_minute": end}},
            )
    return intervals


def load_calendar(conn, berth_ids) -> dict[str, list[tuple[int, int]]]:
    """读取给定泊位的开放日历，返回 {berth_id: [(start, end), ...]}（按起点升序）。"""
    berth_ids = list(berth_ids)
    if not berth_ids:
        return {}
    placeholders = ",".join("?" for _ in berth_ids)
    rows = conn.execute(
        f"SELECT berth_id, start_minute, end_minute FROM berth_open_intervals"
        f" WHERE berth_id IN ({placeholders})"
        f" ORDER BY berth_id, start_minute",
        berth_ids,
    ).fetchall()
    out: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        out.setdefault(r["berth_id"], []).append(
            (r["start_minute"], r["end_minute"])
        )
    return out


def replace_calendar(conn, berth_id: str, items) -> dict:
    """整组替换泊位开放日历（单事务，失败不改旧日历）。

    返回替换后的泊位视图。未知泊位 404 BERTH_NOT_FOUND；非法 / 交叠区间
    422，旧日历原样保留。日历变更不影响已确认快照的固化作业区间。
    """
    if conn.execute("SELECT 1 FROM berths WHERE berth_id = ?", (berth_id,)).fetchone() is None:
        raise berth_not_found(berth_id)
    intervals = validate_calendar(items)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM berth_open_intervals WHERE berth_id = ?", (berth_id,)
        )
        conn.executemany(
            "INSERT INTO berth_open_intervals(berth_id, start_minute, end_minute)"
            " VALUES (?, ?, ?)",
            [(berth_id, s, e) for s, e in intervals],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_berth(conn, berth_id)


# ---- 泊位解析与作业可行性 -----------------------------------------------------------

def resolve_plan_berth(conn, berth_id: Optional[str], work_minutes: Optional[int],
                       destination: str) -> Optional[dict]:
    """预排时解析泊位参数并校验其属于终点。

    泊位与作业分钟必须成对给出（schema 层已做互斥校验，此处防御性再查）。
    返回 {"berth_id", "node_id", "work_minutes", "calendar": [(s,e),...]}；
    未指定泊位返回 None。
    未知泊位 -> 404 BERTH_NOT_FOUND；泊位不属终点 -> 422 BERTH_NOT_AT_DESTINATION；
    作业分钟非正整数 -> 422 INVALID_WORK_MINUTES。
    """
    if berth_id is None and work_minutes is None:
        return None
    if berth_id is None or work_minutes is None:
        raise ApiError(
            422, "VALIDATION_ERROR",
            "泊位编号 berth_id 与作业分钟 work_minutes 必须成对出现",
            {"berth_id": berth_id, "work_minutes": work_minutes},
        )
    if not isinstance(work_minutes, int) or work_minutes <= 0:
        raise ApiError(
            422, "INVALID_WORK_MINUTES",
            "作业分钟 work_minutes 必须为正整数",
            {"berth_id": berth_id, "work_minutes": work_minutes},
        )
    row = conn.execute(
        "SELECT berth_id, node_id FROM berths WHERE berth_id = ?", (berth_id,)
    ).fetchone()
    if row is None:
        raise berth_not_found(berth_id)
    if row["node_id"] != destination:
        raise berth_not_at_destination(berth_id, row["node_id"], destination)
    calendar = load_calendar(conn, [berth_id]).get(berth_id, [])
    return {"berth_id": berth_id, "node_id": row["node_id"],
            "work_minutes": work_minutes, "calendar": calendar}


def work_fits_calendar(arrival: int, work_minutes: int,
                       calendar: list[tuple[int, int]]) -> bool:
    """作业区间 [arrival, arrival+work) 是否完整落在同一开放区间内。"""
    work_end = arrival + work_minutes
    for s, e in calendar:
        if s <= arrival and work_end <= e:
            return True
        if s > arrival:
            break  # 日历按起点升序，其后区间起点更晚，不可能再容纳
    return False


def load_berth_occupancy(conn, lo: int, hi: int, exclude_trip_id=None
                         ) -> dict[str, list[tuple[int, int]]]:
    """读取 [lo, hi] 闭区间内有效（未作废）确认行程的泊位作业占用。

    返回 {berth_id: [(work_start, work_end), ...]}（按起点升序）。占用取
    确认快照固化的作业区间；与 lo/hi 相交即纳入（调用方按半开重叠判定）。
    已作废行程的泊位占用同其航段占用一样即时释放；快照本身仍可查。
    exclude_trip_id 供改期建议排除自身快照。
    """
    sql = (
        "SELECT c.berth_id AS berth_id, c.work_start_minute AS ws,"
        " c.work_end_minute AS we FROM confirmed_trips c"
        " LEFT JOIN voided_trips v ON v.trip_id = c.trip_id"
        " WHERE v.trip_id IS NULL AND c.berth_id IS NOT NULL"
    )
    params: list = []
    if exclude_trip_id is not None:
        sql += " AND c.trip_id <> ?"
        params.append(exclude_trip_id)
    occupied: dict[str, list[tuple[int, int]]] = {}
    for r in conn.execute(sql, params).fetchall():
        ws, we = r["ws"], r["we"]
        if we - 1 < lo or ws > hi:
            continue
        occupied.setdefault(r["berth_id"], []).append((ws, we))
    for intervals in occupied.values():
        intervals.sort()
    return occupied


def berth_free_at(arrival: int, work_minutes: int,
                  occupied: list[tuple[int, int]]) -> bool:
    """作业区间 [arrival, arrival+work) 是否不与既有占用半开重叠。"""
    work_end = arrival + work_minutes
    for ws, we in occupied:
        if arrival < we and ws < work_end:
            return False
    return True
