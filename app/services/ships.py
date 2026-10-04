"""船舶档案：调度员按唯一船号管理船舶参数与启停状态。

每份档案记录**非负吃水、水面以上高度、安全余量及启停状态**；每次**有效
变更**（四项中任一项实际发生变化）使版本号自 1 起递增，全部历史版本不可变、
可随时查询（当前版本即最大版本）。未实际改变任何字段的提交视为无效变更，
**不递增版本**（幂等返回当前状态）。

规划/确认集成：

* 单船与批次预排可用**船号代替三项参数**，按档案**当前启用版本**的参数规划
  （档案已停用 -> ``SHIP_DISABLED``；未知船号 -> ``SHIP_NOT_FOUND``），结果
  回传船号、版本及所用参数；
* 确认时在原事务内核对档案**仍启用且版本未变**（版本已变 ->
  ``SHIP_VERSION_STALE``），再按现有潮位、航时管制与占用复算；任一失效即
  拒绝且不保存（批次全退）；
* 已确认快照固化所用参数与档案引用（船号 + 版本），后续档案变更不改写历史，
  潮位复核与改期建议只读快照参数。

所有写路径（建档 / 变更）均在单个 ``BEGIN IMMEDIATE`` 事务内完成，任一失败
回滚、不留半成品；重复船号由主键兜底并发（``SHIP_EXISTS``）。
"""
import sqlite3
from typing import Optional

from ..errors import ApiError, ship_not_found


# ---- 读取 ---------------------------------------------------------------------

def _version_out(row) -> dict:
    return {
        "ship_no": row["ship_no"],
        "version": row["version"],
        "draft": row["draft"],
        "height": row["height"],
        "margin": row["margin"],
        "enabled": bool(row["enabled"]),
        "note": row["note"],
        "created_at": row["created_at"],
    }


def _current_row(conn, ship_no: str):
    """当前（最大）版本行；无该船号返回 None。"""
    return conn.execute(
        "SELECT ship_no, version, draft, height, margin, enabled, note, created_at"
        " FROM ship_versions WHERE ship_no = ?"
        " ORDER BY version DESC LIMIT 1",
        (ship_no,),
    ).fetchone()


def get_ship(conn, ship_no: str) -> dict:
    """读取档案当前版本。未知船号 -> 404 SHIP_NOT_FOUND。"""
    row = _current_row(conn, ship_no)
    if row is None:
        raise ship_not_found(ship_no)
    return _version_out(row)


def list_ships(conn) -> list[dict]:
    """列出全部档案的当前版本（按船号升序）。"""
    rows = conn.execute(
        "SELECT v.ship_no, v.version, v.draft, v.height, v.margin, v.enabled,"
        " v.note, v.created_at"
        " FROM ship_versions v"
        " JOIN (SELECT ship_no, MAX(version) AS mv FROM ship_versions"
        "       GROUP BY ship_no) latest"
        "   ON latest.ship_no = v.ship_no AND latest.mv = v.version"
        " ORDER BY v.ship_no"
    ).fetchall()
    return [_version_out(r) for r in rows]


def list_versions(conn, ship_no: str) -> list[dict]:
    """列出某船号的全部历史版本（按版本号升序）。未知船号 -> 404。"""
    if not conn.execute(
        "SELECT 1 FROM ships WHERE ship_no = ?", (ship_no,)
    ).fetchone():
        raise ship_not_found(ship_no)
    rows = conn.execute(
        "SELECT ship_no, version, draft, height, margin, enabled, note, created_at"
        " FROM ship_versions WHERE ship_no = ? ORDER BY version",
        (ship_no,),
    ).fetchall()
    return [_version_out(r) for r in rows]


def get_version(conn, ship_no: str, version: int) -> dict:
    """读取指定历史版本。未知船号 -> SHIP_NOT_FOUND；未知版本 ->
    SHIP_VERSION_NOT_FOUND。"""
    row = conn.execute(
        "SELECT ship_no, version, draft, height, margin, enabled, note, created_at"
        " FROM ship_versions WHERE ship_no = ? AND version = ?",
        (ship_no, version),
    ).fetchone()
    if row is None:
        if not conn.execute(
            "SELECT 1 FROM ships WHERE ship_no = ?", (ship_no,)
        ).fetchone():
            raise ship_not_found(ship_no)
        raise ApiError(
            404, "SHIP_VERSION_NOT_FOUND",
            f"船号 {ship_no} 不存在版本 #{version}",
            {"ship_no": ship_no, "version": version},
        )
    return _version_out(row)


# ---- 写入（建档 / 变更，均单事务、不留半成品） -----------------------------------

def create_ship(conn, ship_no: str, draft: float, height: float, margin: float,
                enabled: bool, note: Optional[str] = None) -> dict:
    """建立船舶档案（初始版本 1）。重复船号 -> 409 SHIP_EXISTS。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        try:
            conn.execute("INSERT INTO ships(ship_no) VALUES (?)", (ship_no,))
        except sqlite3.IntegrityError:
            raise ApiError(
                409, "SHIP_EXISTS", f"船号已存在: {ship_no}，拒绝重复建档",
                {"ship_no": ship_no},
            )
        conn.execute(
            "INSERT INTO ship_versions"
            " (ship_no, version, draft, height, margin, enabled, note)"
            " VALUES (?, 1, ?, ?, ?, ?, ?)",
            (ship_no, draft, height, margin, 1 if enabled else 0, note),
        )
        row = _current_row(conn, ship_no)
        conn.execute("COMMIT")
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _version_out(row)


def update_ship(conn, ship_no: str, draft: Optional[float],
                height: Optional[float], margin: Optional[float],
                enabled: Optional[bool], note: Optional[str] = None
                ) -> tuple[dict, bool]:
    """变更档案：未给出的字段沿用当前值。返回 (最新状态, changed)。

    四项（吃水/水面以上高度/安全余量/启停状态）任一实际变化 -> 版本 +1，
    changed=True；全部与当前一致（含空提交）-> 不递增版本，changed=False。
    未知船号 -> 404 SHIP_NOT_FOUND。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _current_row(conn, ship_no)
        if row is None:
            raise ship_not_found(ship_no)
        new_draft = row["draft"] if draft is None else draft
        new_height = row["height"] if height is None else height
        new_margin = row["margin"] if margin is None else margin
        new_enabled = bool(row["enabled"]) if enabled is None else enabled

        changed = (
            new_draft != row["draft"]
            or new_height != row["height"]
            or new_margin != row["margin"]
            or new_enabled != bool(row["enabled"])
        )
        if changed:
            conn.execute(
                "INSERT INTO ship_versions"
                " (ship_no, version, draft, height, margin, enabled, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ship_no, row["version"] + 1, new_draft, new_height, new_margin,
                 1 if new_enabled else 0, note),
            )
            row = _current_row(conn, ship_no)
        conn.execute("COMMIT")
    except ApiError:
        conn.execute("ROLLBACK")
        raise
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _version_out(row), changed


# ---- 规划 / 确认集成 -------------------------------------------------------------

def resolve_for_plan(conn, ship_no: str) -> dict:
    """预排用档案解析：按**当前启用版本**取参数。

    未知船号 -> 404 SHIP_NOT_FOUND；档案已停用 -> 409 SHIP_DISABLED。
    返回 {ship_no, ship_version, draft, height, margin}。
    """
    cur = get_ship(conn, ship_no)
    if not cur["enabled"]:
        raise ApiError(
            409, "SHIP_DISABLED",
            f"船号 {ship_no} 的档案已停用（当前版本 #{cur['version']}），"
            "不能用于规划",
            {"ship_no": ship_no, "version": cur["version"]},
        )
    return {
        "ship_no": cur["ship_no"],
        "ship_version": cur["version"],
        "draft": cur["draft"],
        "height": cur["height"],
        "margin": cur["margin"],
    }


def resolve_for_confirm(conn, ship_no: str, ship_version: int) -> dict:
    """确认用档案核对：档案须**仍启用且版本未变**（在确认事务内调用）。

    未知船号 -> 404 SHIP_NOT_FOUND；已停用 -> 409 SHIP_DISABLED；
    当前版本与回填版本不一致 -> 409 SHIP_VERSION_STALE。
    """
    cur = get_ship(conn, ship_no)
    if not cur["enabled"]:
        raise ApiError(
            409, "SHIP_DISABLED",
            f"船号 {ship_no} 的档案已停用（当前版本 #{cur['version']}），"
            "确认失败",
            {"ship_no": ship_no, "version": cur["version"]},
        )
    if cur["version"] != ship_version:
        raise ApiError(
            409, "SHIP_VERSION_STALE",
            f"船号 {ship_no} 的档案已变更到版本 #{cur['version']}，"
            f"规划所据版本 #{ship_version} 已过期，请重新规划",
            {"ship_no": ship_no, "provided_version": ship_version,
             "current_version": cur["version"]},
        )
    return {
        "ship_no": cur["ship_no"],
        "ship_version": cur["version"],
        "draft": cur["draft"],
        "height": cur["height"],
        "margin": cur["margin"],
    }
