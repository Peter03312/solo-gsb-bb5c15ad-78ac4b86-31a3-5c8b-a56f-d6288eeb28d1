"""航段分时航时管制。

调度员按航段维护一组半开分钟区间 ``[start_minute, end_minute)`` 及正整数
通行分钟：

* 船舶在**进入航段的分钟**选定航时——命中某区间即按该区间的 ``travel_time``
  计航时，途中跨越管制边界航时也不变；未命中任何区间沿用航段基础航时
  （``segments.travel_time``）；
* 以航段编号提交一组区间即**整组替换**该航段的旧管制（空组恢复基础航时），
  成功返回生效清单；
* 拒绝未知航段（``SEGMENT_NOT_FOUND``）、非法区间
  （``INVALID_CONTROL_INTERVAL``）与组内重叠
  （``OVERLAPPING_CONTROL_INTERVAL``），任何失败都不改旧管制。

存储表为 ``segment_travel_controls``；同 ``seg_id`` 的区间互不为半开交叠。
"""
from ..errors import ApiError


def segment_exists(conn, seg_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM segments WHERE seg_id = ?", (seg_id,)
    ).fetchone() is not None


def load_controls(conn, seg_ids=None) -> dict[int, list[tuple[int, int, int]]]:
    """读取管制区间，返回 {seg_id: [(start, end, travel_time), ...]}（按起点升序）。

    seg_ids 为 None 时读取全部航段。
    """
    if seg_ids is not None:
        seg_ids = list(seg_ids)
        if not seg_ids:
            return {}
        placeholders = ",".join("?" for _ in seg_ids)
        rows = conn.execute(
            f"SELECT seg_id, start_minute, end_minute, travel_time"
            f" FROM segment_travel_controls WHERE seg_id IN ({placeholders})"
            f" ORDER BY seg_id, start_minute",
            seg_ids,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT seg_id, start_minute, end_minute, travel_time"
            " FROM segment_travel_controls ORDER BY seg_id, start_minute"
        ).fetchall()
    controls: dict[int, list[tuple[int, int, int]]] = {}
    for r in rows:
        controls.setdefault(r["seg_id"], []).append(
            (r["start_minute"], r["end_minute"], r["travel_time"])
        )
    return controls


def effective_travel_time(base_travel_time: int,
                          intervals: list[tuple[int, int, int]],
                          minute: int) -> int:
    """船舶于 minute 进入航段时的实际航时。

    intervals 为该航段按起点升序、互不交叠的半开区间 (start, end, t)；
    未命中任何区间时沿用基础航时。线性扫描——单航段管制区间数量通常很少。
    """
    for start, end, travel_time in intervals:
        if start <= minute < end:
            return travel_time
        if minute < start:
            break
    return base_travel_time


def validate_intervals(items) -> list[tuple[int, int, int]]:
    """校验提交的管制组，返回规范化后的升序三元组列表（可为空）。

    非法区间 -> 422 INVALID_CONTROL_INTERVAL；
    组内半开区间交叠 -> 422 OVERLAPPING_CONTROL_INTERVAL。
    """
    intervals: list[tuple[int, int, int]] = []
    for idx, it in enumerate(items):
        start = it["start_minute"]
        end = it["end_minute"]
        travel_time = it["travel_time"]
        if start < 0 or end <= start or travel_time <= 0:
            raise ApiError(
                422,
                "INVALID_CONTROL_INTERVAL",
                f"第 {idx} 个管制区间非法：要求 start_minute>=0、"
                "end_minute>start_minute、travel_time 为正整数分钟",
                {"index": idx, "start_minute": start, "end_minute": end,
                 "travel_time": travel_time},
            )
        intervals.append((start, end, travel_time))

    intervals.sort(key=lambda x: x[0])
    for i in range(1, len(intervals)):
        prev_start, prev_end, _ = intervals[i - 1]
        start, end, _ = intervals[i]
        if start < prev_end:
            raise ApiError(
                422,
                "OVERLAPPING_CONTROL_INTERVAL",
                f"管制区间组内交叠：[{prev_start}, {prev_end}) 与 [{start}, {end})"
                " 相互重叠，拒绝替换",
                {"previous": {"start_minute": prev_start, "end_minute": prev_end},
                 "current": {"start_minute": start, "end_minute": end}},
            )
    return intervals


def replace_segment_controls(conn, seg_id: int,
                             items) -> list[tuple[int, int, int]]:
    """整组替换某航段的分时航时管制（单事务，失败不改旧管制）。

    返回替换后的生效清单（按起点升序）。未知航段 -> 404 SEGMENT_NOT_FOUND；
    空组 -> 删除全部旧管制，恢复基础航时。
    """
    if not segment_exists(conn, seg_id):
        raise ApiError(404, "SEGMENT_NOT_FOUND",
                       f"未知航段: {seg_id}", {"seg_id": seg_id})

    intervals = validate_intervals(items)

    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM segment_travel_controls WHERE seg_id = ?", (seg_id,)
        )
        conn.executemany(
            "INSERT INTO segment_travel_controls"
            " (seg_id, start_minute, end_minute, travel_time)"
            " VALUES (?, ?, ?, ?)",
            [(seg_id, s, e, t) for s, e, t in intervals],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return intervals
