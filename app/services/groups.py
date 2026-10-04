"""狭窄水道组（narrow channel group）。

调度员可把**至少两条**现有航段登记为同一狭窄水道组。组建成后**不可修改**
（无更新/解散接口），航段**不得重复归组**（一条航段至多属于一个组）。

组占用语义：船进入组内任一航段后，从**进入分钟到抵达前一分钟**
（`d..d+t-1`，`t` 为该段固化的实际航时）**独占整组**——该行程该段的
占用会展开到组内**全部成员航段**的同一批分钟；因此不同成员航段的通行
也不得重叠，抵达分钟 `d+t` 可由下一船进入（不展开到该分钟）。

* **组建前**的已确认快照按其**原航段和固化航时**计入新组占用：只要其
  原行程经过组内任一航段，该段原占用即展开到全组；既有快照**不改写**，
  组建前已有的重叠也**不清理**（展开后彼此重叠的占用同样原样保留）。
* 单船/批次预排与确认复算、改期求解统一经
  `planning.load_occupancy` 获得展开后的组占用；原行程潮位安全复核
  仍只判断潮位，不读取占用。

登记失败（未知航段 / 提交含重复编号 / 成员已属于某组）一律拒绝，
单事务内不留下任何半成品组。
"""
import sqlite3

from ..errors import ApiError


def load_groups(conn) -> tuple[dict[int, int], dict[int, list[int]]]:
    """读取当前全部组关系。

    返回 (seg_to_group, group_members)：
      seg_to_group：{seg_id: group_id}；
      group_members：{group_id: [seg_id, ...]}（按提交次序 position 升序）。
    """
    rows = conn.execute(
        "SELECT group_id, seg_id, position FROM channel_group_members"
        " ORDER BY group_id, position"
    ).fetchall()
    seg_to_group: dict[int, int] = {}
    group_members: dict[int, list[int]] = {}
    for r in rows:
        seg_to_group[r["seg_id"]] = r["group_id"]
        group_members.setdefault(r["group_id"], []).append(r["seg_id"])
    return seg_to_group, group_members


def expand_group_occupancy(
    blocked: dict[int, set[int]],
    seg_to_group: dict[int, int],
    group_members: dict[int, list[int]],
) -> dict[int, set[int]]:
    """把组内任一航段的占用就地展开到同组全部成员航段（独占整组）。

    blocked 为 {seg_id: {绝对占用分钟}}。展开取闭包：每条已计入的占用
    分钟都同步到其所在组的每个成员航段；反复传播直到稳定。展开不改变
    占用区间（进入分钟到抵达前一分钟），故抵达分钟自然可由下一船进入。
    """
    pending = True
    while pending:
        pending = False
        for seg_id in list(blocked):
            gid = seg_to_group.get(seg_id)
            if gid is None:
                continue
            minutes = blocked[seg_id]
            if not minutes:
                continue
            for member in group_members[gid]:
                target = blocked.setdefault(member, set())
                if not minutes <= target:
                    target.update(minutes)
                    pending = True
    return blocked


def create_group(conn, seg_ids: list[int], note=None) -> dict:
    """把一组现有航段登记为新的狭窄水道组。

    至少两条成员（请求模型层面保证）；返回 {"group_id", "members", "note",
    "created_at"}，members 按提交次序给出。

    * 提交编号含重复 -> 422 DUPLICATE_SEGMENT_IN_GROUP；
    * 任一航段不存在 -> 404 SEGMENT_NOT_FOUND；
    * 任一航段已归组   -> 409 SEGMENT_ALREADY_GROUPED；
    任一失败均在事务内回滚，不留半成品；并发竞争由 UNIQUE(seg_id) 兜底。
    """
    # 1) 提交集合中的重复编号（内存校验，不入库）
    seen: set[int] = set()
    duplicates: list[int] = []
    for sid in seg_ids:
        if sid in seen and sid not in duplicates:
            duplicates.append(sid)
        seen.add(sid)
    if duplicates:
        raise ApiError(
            422, "DUPLICATE_SEGMENT_IN_GROUP",
            f"提交的航段编号存在重复: {duplicates}，组内每条航段只能出现一次",
            {"duplicates": duplicates},
        )

    # 2) 未知航段（一次性给出全部未知编号）
    placeholders = ",".join("?" for _ in seg_ids)
    known_rows = conn.execute(
        f"SELECT seg_id FROM segments WHERE seg_id IN ({placeholders})",
        seg_ids,
    ).fetchall()
    unknown = sorted(set(seg_ids) - {r["seg_id"] for r in known_rows})
    if unknown:
        raise ApiError(
            404, "SEGMENT_NOT_FOUND",
            f"未知航段: {unknown}，拒绝组建狭窄水道组",
            {"unknown_seg_ids": unknown},
        )

    # 3) 冲突归组：任一成员已属于某组
    grouped_rows = conn.execute(
        f"SELECT seg_id, group_id FROM channel_group_members"
        f" WHERE seg_id IN ({placeholders})",
        seg_ids,
    ).fetchall()
    if grouped_rows:
        conflicts = [
            {"seg_id": r["seg_id"], "group_id": r["group_id"]}
            for r in grouped_rows
        ]
        raise ApiError(
            409, "SEGMENT_ALREADY_GROUPED",
            "提交航段中已有航段归属其他狭窄水道组（组建成后不可修改、"
            "航段不得重复归组），拒绝组建",
            {"conflicts": conflicts},
        )

    # 4) 单事务写入组与全部成员；任一失败整体回滚（不留半成品）
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO channel_groups(note) VALUES (?)", (note,)
        )
        group_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO channel_group_members(group_id, seg_id, position)"
            " VALUES (?, ?, ?)",
            [(group_id, sid, pos) for pos, sid in enumerate(seg_ids)],
        )
        row = conn.execute(
            "SELECT group_id, note, created_at FROM channel_groups"
            " WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        # 并发竞争：他事务已抢先把某航段归组
        conn.execute("ROLLBACK")
        conflicts = [
            {"seg_id": sid, "group_id": gid}
            for sid, gid in load_groups(conn)[0].items() if sid in seen
        ]
        raise ApiError(
            409, "SEGMENT_ALREADY_GROUPED",
            "并发组建冲突：提交航段中已有航段被其他事务归组，拒绝组建",
            {"conflicts": conflicts},
        )
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {
        "group_id": group_id,
        "members": _member_details(conn, group_id),
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _member_details(conn, group_id: int) -> list[dict]:
    """按提交次序返回成员航段详情。"""
    rows = conn.execute(
        "SELECT m.seg_id AS seg_id, m.position AS position, s.origin AS origin,"
        " s.destination AS destination, s.travel_time AS travel_time,"
        " s.depth AS depth, s.clearance AS clearance"
        " FROM channel_group_members m JOIN segments s ON s.seg_id = m.seg_id"
        " WHERE m.group_id = ? ORDER BY m.position",
        (group_id,),
    ).fetchall()
    return [
        {
            "position": r["position"],
            "seg_id": r["seg_id"],
            "origin": r["origin"],
            "destination": r["destination"],
            "travel_time": r["travel_time"],
            "depth": r["depth"],
            "clearance": r["clearance"],
        }
        for r in rows
    ]


def get_group(conn, group_id: int) -> dict:
    """读取组号及成员（按提交次序）。未知组 -> 404 CHANNEL_GROUP_NOT_FOUND。"""
    row = conn.execute(
        "SELECT group_id, note, created_at FROM channel_groups WHERE group_id = ?",
        (group_id,),
    ).fetchone()
    if row is None:
        raise ApiError(
            404, "CHANNEL_GROUP_NOT_FOUND", f"狭窄水道组不存在: {group_id}",
            {"group_id": group_id},
        )
    return {
        "group_id": row["group_id"],
        "note": row["note"],
        "created_at": row["created_at"],
        "members": _member_details(conn, group_id),
    }


def list_groups(conn) -> list[dict]:
    """列出全部狭窄水道组（按组号升序）。"""
    rows = conn.execute(
        "SELECT group_id FROM channel_groups ORDER BY group_id"
    ).fetchall()
    return [get_group(conn, r["group_id"]) for r in rows]
