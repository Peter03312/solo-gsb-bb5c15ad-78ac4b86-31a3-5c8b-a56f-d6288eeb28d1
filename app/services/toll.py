"""通行费费率版本与账单 / 冲销单。

费率版本（财务按航段提交）
--------------------------

财务以航段编号提交一组半开分钟费率区间 ``[start_minute, end_minute)`` 及
**非负整数**费率（分/分钟）：

* 每次成功提交生成一个**全局不可变**新版本（版本号自增）：继承前一版本的
  全部航段费率，仅把被提交航段的费率**整组替换**为本次区间（空组表示该航段
  此后未覆盖分钟费率为 0）；
* 组内区间交叠或航段未知 -> 整次拒绝、不生成版本（旧版本原样可查）；
* 某分钟未被任何区间覆盖 -> 费率为 0。

开单（凭行程号）
----------------

在单个 ``BEGIN IMMEDIATE`` 事务内：

1. 取**当时最新费率版本**，读取确认快照 ``confirmed_trips.itinerary``；
2. 每段按其**进入分钟**（快照的 ``start_minute``）在该版本中选费率，
   段金额 = 快照固化 ``travel_time`` × 费率（航时计费按快照固化，不随管制变化）；
3. 账单（版本号、逐段金额、整数分总额）一次落库。旧账单永不随新费率版本改变；
4. 每个行程只开一次：重复开单（含并发竞争）返回首次账单（``replayed=true``）；
5. 未知行程 -> ``404 TRIP_NOT_FOUND``；已作废且未开单 ->
   ``409 TRIP_ALREADY_VOIDED``；系统中尚无费率版本 ->
   ``409 TOLL_RATE_VERSION_NOT_FOUND``。

作废与冲销
----------

作废**已开单**行程时，在原作废事务（``services/voids.py`` 的同一事务）内生成
一张**等额冲销单**：逐段金额与总额取账单原值的相反数，费率版本沿用账单版本。
重复作废不重复冲销；账单与冲销单均可查。未开单行程的作废不生成冲销单。

并发：开单与作废都持有 ``BEGIN IMMEDIATE`` 写锁，SQLite 将二者串行化——
作废先提交则随后的开单读到作废标识而拒绝；开单先提交则随后的作废在同一事务
内读到账单并生成冲销。唯一约束（``toll_bills.trip_id`` /
``toll_reversals.bill_trip_id``）兜底并发竞争，保证不重复开单、不重复冲销。
"""
import json
import sqlite3

from ..errors import ApiError
from .controls import segment_exists


# ---- 费率区间校验 ----------------------------------------------------------------

def validate_rate_intervals(items) -> list[tuple[int, int, int]]:
    """校验提交的费率组，返回规范化后的升序三元组列表（可为空）。

    非法区间 -> 422 INVALID_TOLL_RATE_INTERVAL；
    组内半开区间交叠 -> 422 OVERLAPPING_TOLL_RATE_INTERVAL。
    费率允许为 0（免费时段）；起点非负、终点大于起点。
    """
    intervals: list[tuple[int, int, int]] = []
    for idx, it in enumerate(items):
        start = it["start_minute"]
        end = it["end_minute"]
        rate = it["rate_cents_per_minute"]
        # 布尔值是 int 的子类，财务整数口径（分钟、分）不接受；
        # 起点非负、终点大于起点、费率非负。
        if (isinstance(start, bool) or isinstance(end, bool)
                or isinstance(rate, bool)
                or start < 0 or end <= start or rate < 0):
            raise ApiError(
                422,
                "INVALID_TOLL_RATE_INTERVAL",
                f"第 {idx} 个费率区间非法：要求 start_minute>=0、"
                "end_minute>start_minute、rate_cents_per_minute 为非负整数（分/分钟）",
                {"index": idx, "start_minute": start, "end_minute": end,
                 "rate_cents_per_minute": rate},
            )
        intervals.append((start, end, rate))

    intervals.sort(key=lambda x: x[0])
    for i in range(1, len(intervals)):
        prev_start, prev_end, _ = intervals[i - 1]
        start, end, _ = intervals[i]
        if start < prev_end:
            raise ApiError(
                422,
                "OVERLAPPING_TOLL_RATE_INTERVAL",
                f"费率区间组内交叠：[{prev_start}, {prev_end}) 与 [{start}, {end})"
                " 相互重叠，拒绝替换",
                {"previous": {"start_minute": prev_start, "end_minute": prev_end},
                 "current": {"start_minute": start, "end_minute": end}},
            )
    return intervals


def replace_segment_rates(conn, seg_id: int, items, note=None) -> dict:
    """整组替换某航段费率，继承其他航段，生成全局不可变新版本（单事务）。

    返回新版本完整视图（版本号、备注、全部航段费率区间）。
    未知航段 -> 404 SEGMENT_NOT_FOUND；区间非法/组内交叠 -> 422 且不出版本。
    """
    if not segment_exists(conn, seg_id):
        raise ApiError(404, "SEGMENT_NOT_FOUND",
                       f"未知航段: {seg_id}", {"seg_id": seg_id})

    intervals = validate_rate_intervals(items)

    try:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute(
            "SELECT version FROM toll_rate_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        prev_version = prev["version"] if prev is not None else None

        cur = conn.execute(
            "INSERT INTO toll_rate_versions(changed_seg_id, note) VALUES (?, ?)",
            (seg_id, note or (
                f"航段 {seg_id} 费率整组替换"
                + (f"（继承版本 #{prev_version}）" if prev_version is not None
                   else "（首个费率版本）")
            )),
        )
        new_version = cur.lastrowid

        # 全量物化：继承前一版本全部航段的费率区间（不可变版本独立可查），
        # 再以本次提交整组替换 changed_seg_id 航段。
        if prev_version is not None:
            conn.execute(
                "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute,"
                " rate_cents_per_minute)"
                " SELECT ?, seg_id, start_minute, end_minute, rate_cents_per_minute"
                " FROM toll_rates WHERE version = ?",
                (new_version, prev_version),
            )
        conn.execute(
            "DELETE FROM toll_rates WHERE version = ? AND seg_id = ?",
            (new_version, seg_id),
        )
        conn.executemany(
            "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute,"
            " rate_cents_per_minute) VALUES (?, ?, ?, ?, ?)",
            [(new_version, seg_id, s, e, r) for s, e, r in intervals],
        )
        out = _load_version(conn, new_version)
        conn.execute("COMMIT")
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return out


# ---- 费率版本查询 ----------------------------------------------------------------

def _load_version(conn, version: int) -> dict:
    """组装某版本的完整视图（元数据 + 全部航段费率区间）。未知版本抛 404。"""
    meta = conn.execute(
        "SELECT version, changed_seg_id, note, created_at"
        " FROM toll_rate_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if meta is None:
        raise ApiError(404, "TOLL_RATE_VERSION_NOT_FOUND",
                       f"费率版本不存在: {version}", {"version": version})
    rows = conn.execute(
        "SELECT seg_id, start_minute, end_minute, rate_cents_per_minute"
        " FROM toll_rates WHERE version = ? ORDER BY seg_id, start_minute",
        (version,),
    ).fetchall()
    return {
        "version": meta["version"],
        "changed_seg_id": meta["changed_seg_id"],
        "note": meta["note"],
        "created_at": meta["created_at"],
        "rates": [
            {"seg_id": r["seg_id"], "start_minute": r["start_minute"],
             "end_minute": r["end_minute"],
             "rate_cents_per_minute": r["rate_cents_per_minute"]}
            for r in rows
        ],
    }


def get_rate_version(conn, version: int) -> dict:
    return _load_version(conn, version)


def list_rate_versions(conn) -> list[dict]:
    """全部费率版本（按版本号升序），每个含其全部航段费率区间。"""
    versions = [r["version"] for r in conn.execute(
        "SELECT version FROM toll_rate_versions ORDER BY version"
    ).fetchall()]
    return [_load_version(conn, v) for v in versions]


def rate_at(intervals, minute: int) -> int:
    """按进入分钟选费率（分/分钟）；未覆盖任何区间返回 0。

    intervals 为按起点升序、互不交叠的半开区间 (start, end, rate)。
    """
    for start, end, rate in intervals:
        if start <= minute < end:
            return rate
        if minute < start:
            break
    return 0


# ---- 开单 ------------------------------------------------------------------------

def _load_trip(conn, trip_id: int):
    return conn.execute(
        "SELECT trip_id, itinerary FROM confirmed_trips WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()


def _is_voided(conn, trip_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM voided_trips WHERE trip_id = ?", (trip_id,)
    ).fetchone() is not None


def _load_rate_map(conn, version: int) -> dict[int, list[tuple[int, int, int]]]:
    rate_map: dict[int, list[tuple[int, int, int]]] = {}
    for r in conn.execute(
        "SELECT seg_id, start_minute, end_minute, rate_cents_per_minute"
        " FROM toll_rates WHERE version = ? ORDER BY seg_id, start_minute",
        (version,),
    ).fetchall():
        rate_map.setdefault(r["seg_id"], []).append(
            (r["start_minute"], r["end_minute"], r["rate_cents_per_minute"])
        )
    return rate_map


def _bill_segments(itinerary: list[dict],
                   rate_map: dict[int, list[tuple[int, int, int]]]) -> list[dict]:
    """按确认快照各段进入分钟计费（航时取快照固化值）。"""
    segments = []
    for idx, leg in enumerate(itinerary):
        entry = leg["start_minute"]
        travel_time = leg["travel_time"]
        rate = rate_at(rate_map.get(leg["seg_id"], ()), entry)
        segments.append({
            "leg_index": idx,
            "seg_id": leg["seg_id"],
            "origin": leg["origin"],
            "destination": leg["destination"],
            "entry_minute": entry,
            "travel_time": travel_time,
            "rate_cents_per_minute": rate,
            "amount_cents": rate * travel_time,
        })
    return segments


def _load_bill_row(conn, trip_id: int):
    return conn.execute(
        "SELECT trip_id, version, total_cents, segments, billed_at"
        " FROM toll_bills WHERE trip_id = ?",
        (trip_id,),
    ).fetchone()


def _load_reversal(conn, trip_id: int):
    row = conn.execute(
        "SELECT bill_trip_id, version, total_cents, segments, reversed_at"
        " FROM toll_reversals WHERE bill_trip_id = ?",
        (trip_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "trip_id": row["bill_trip_id"],
        "version": row["version"],
        "total_cents": row["total_cents"],
        "segments": json.loads(row["segments"]),
        "reversed_at": row["reversed_at"],
    }


def bill_out(conn, trip_id: int) -> dict:
    """组装账单视图（含冲销单，若有）。未知账单 -> 404 BILL_NOT_FOUND。"""
    row = _load_bill_row(conn, trip_id)
    if row is None:
        raise ApiError(404, "BILL_NOT_FOUND",
                       f"行程 {trip_id} 尚未开具通行费账单，账单不存在",
                       {"trip_id": trip_id})
    return {
        "trip_id": row["trip_id"],
        "version": row["version"],
        "total_cents": row["total_cents"],
        "segments": json.loads(row["segments"]),
        "billed_at": row["billed_at"],
        "reversal": _load_reversal(conn, trip_id),
    }


def issue_bill(conn, trip_id: int) -> tuple[dict, bool]:
    """为已确认行程开具通行费账单。返回 (账单视图, replayed)。

    首次开单返回 (账单, False)（HTTP 201）；重复开单（含并发竞争）返回
    **首次账单**与 True（HTTP 200）。未知行程 404 TRIP_NOT_FOUND；
    已作废且未开单 409 TRIP_ALREADY_VOIDED；尚无费率版本
    409 TOLL_RATE_VERSION_NOT_FOUND。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        trip = _load_trip(conn, trip_id)
        if trip is None:
            raise ApiError(404, "TRIP_NOT_FOUND",
                           f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})

        existing = _load_bill_row(conn, trip_id)
        if existing is not None:
            conn.execute("COMMIT")
            return bill_out(conn, trip_id), True

        # 已作废且未开单：拒绝（作废未开单行程不产生账单与冲销）
        if _is_voided(conn, trip_id):
            raise ApiError(
                409, "TRIP_ALREADY_VOIDED",
                f"行程 {trip_id} 已作废且未开单，拒绝开具通行费账单",
                {"trip_id": trip_id},
            )

        latest = conn.execute(
            "SELECT version FROM toll_rate_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if latest is None:
            raise ApiError(
                409, "TOLL_RATE_VERSION_NOT_FOUND",
                "系统中尚无任何通行费费率版本，无法开单；请先由财务提交费率",
            )
        version = latest["version"]

        itinerary = json.loads(trip["itinerary"])
        segments = _bill_segments(itinerary, _load_rate_map(conn, version))
        total = sum(s["amount_cents"] for s in segments)

        try:
            conn.execute(
                "INSERT INTO toll_bills(trip_id, version, total_cents, segments)"
                " VALUES (?, ?, ?, ?)",
                (trip_id, version, total, json.dumps(segments)),
            )
        except sqlite3.IntegrityError:
            # 兜底并发竞争（BEGIN IMMEDIATE 串行化下正常不会到达；SQLite 语句
            # 级失败不中止事务）：已有账单按重放返回，否则按已作废拒绝。
            if _load_bill_row(conn, trip_id) is not None:
                out = bill_out(conn, trip_id)
                conn.execute("COMMIT")
                return out, True
            raise ApiError(
                409, "TRIP_ALREADY_VOIDED",
                f"行程 {trip_id} 已作废且未开单，拒绝开具通行费账单",
                {"trip_id": trip_id},
            )

        out = bill_out(conn, trip_id)
        conn.execute("COMMIT")
        return out, False
    except ApiError:
        # 竞争分支可能已自行提交；仅当仍在事务中时回滚
        _safe_rollback(conn)
        raise
    except Exception:
        _safe_rollback(conn)
        raise


def _safe_rollback(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass


# ---- 冲销（由作废事务调用） ---------------------------------------------------------

def create_reversal_in_void(conn, trip_id: int) -> None:
    """在作废事务内为已开单行程生成等额冲销单；未开单则什么也不做。

    必须在调用方已持有的 ``BEGIN IMMEDIATE`` 作废事务内执行（不自行开关
    事务），保证作废与冲销原子提交。重复作废不进入本函数
    （``services/voids.py`` 在重放分支提前返回），唯一约束
    ``toll_reversals.bill_trip_id`` 兜底任何并发竞争。
    """
    bill = _load_bill_row(conn, trip_id)
    if bill is None:
        return
    if conn.execute(
        "SELECT 1 FROM toll_reversals WHERE bill_trip_id = ?", (trip_id,)
    ).fetchone() is not None:
        return

    segments = json.loads(bill["segments"])
    reversal_segments = [
        {**s, "rate_cents_per_minute": s["rate_cents_per_minute"],
         "amount_cents": -s["amount_cents"]}
        for s in segments
    ]
    total = -bill["total_cents"]
    conn.execute(
        "INSERT INTO toll_reversals(bill_trip_id, version, total_cents, segments)"
        " VALUES (?, ?, ?, ?)",
        (trip_id, bill["version"], total, json.dumps(reversal_segments)),
    )


def get_reversal(conn, trip_id: int) -> dict:
    """按行程号查询冲销单。

    未知行程 404 TRIP_NOT_FOUND；未开单 404 BILL_NOT_FOUND；
    已开单但未作废（无冲销）404 REVERSAL_NOT_FOUND。
    """
    if _load_trip(conn, trip_id) is None:
        raise ApiError(404, "TRIP_NOT_FOUND",
                       f"已确认行程不存在: {trip_id}", {"trip_id": trip_id})
    if _load_bill_row(conn, trip_id) is None:
        raise ApiError(404, "BILL_NOT_FOUND",
                       f"行程 {trip_id} 尚未开具通行费账单，无冲销单",
                       {"trip_id": trip_id})
    reversal = _load_reversal(conn, trip_id)
    if reversal is None:
        raise ApiError(
            404, "REVERSAL_NOT_FOUND",
            f"行程 {trip_id} 的账单尚未冲销（行程未作废或作废时未开单）",
            {"trip_id": trip_id},
        )
    return reversal


def list_bills(conn) -> list[dict]:
    """全部账单（按行程号升序），各附冲销单（若有）。"""
    ids = [r["trip_id"] for r in conn.execute(
        "SELECT trip_id FROM toll_bills ORDER BY trip_id"
    ).fetchall()]
    return [bill_out(conn, tid) for tid in ids]
