"""潮位修订的增量存储与沿修订链还原。

存储模型（写入量只随本次改动分钟数增长）：

- **基修订**（kind='root'，含旧库升级后的既有全量修订）：逐分钟全量
  (level, error) 存于 ``tide_values``，``base_revision`` 为空。
- **名义潮位更正 / 预测误差修订**（kind='correction' / 'error'）：不复制
  基础修订的任何分钟，只为本次列出的每个分钟向 ``tide_deltas`` 写一行
  该分钟在本修订**解析后**的完整 (level, error)，并以 ``base_revision``
  指向基础修订（允许从**非最新**修订分叉）。
- 读取修订 r 在某分钟范围的完整名义潮位与误差时，沿 ``base_revision``
  祖先链（含 r 自身）取每个分钟**最近修订**（链上深度最小者）的解析值；
  链上始终无该分钟即缺名义潮位（沿用 MISSING_TIDE_DATA 错误）。

因此：
- 名义更正只覆盖所列分钟的名义潮位；已有分钟的误差沿用基础修订，基础
  修订没有的新分钟误差取 0；
- 误差修订名义潮位不变、只改所列已有潮位分钟的误差（不能凭空新增分钟）；
- 规划、确认、批次、复核与改期读取同一修订号时经同一函数取数，保证一致。
"""
from typing import Optional

from ..errors import ApiError


def revision_exists(conn, revision: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM tide_revisions WHERE revision = ?", (revision,)
    ).fetchone() is not None


def latest_revision(conn) -> Optional[int]:
    row = conn.execute(
        "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
    ).fetchone()
    return None if row is None else row["revision"]


def resolve_base(conn, base_revision: Optional[int]) -> int:
    """解析基础修订号：显式指定须存在（允许指定非最新修订以分叉），否则取
    最新修订；系统中无任何修订或指定修订未知 -> TIDE_REVISION_NOT_FOUND。"""
    if base_revision is not None:
        base = base_revision
        if not revision_exists(conn, base):
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"基础修订不存在: {base}", {"revision": base})
        return base
    latest = latest_revision(conn)
    if latest is None:
        raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                       "系统中不存在任何潮位修订")
    return latest


# 沿 base_revision 祖先链（目标修订深度 0，逐层向基础修订加深）读取范围内
# 每个分钟在各层修订上的解析值；调用方按 depth 最小者去重即得本修订的
# 完整名义潮位与误差。基修订（tide_values）与增量修订（tide_deltas）的数据
# 用 UNION ALL 合并。修订链只随修订次数增长、与分钟规模无关。
_CHAIN_SQL = """
WITH RECURSIVE chain(rev, base_rev, depth) AS (
    SELECT revision, base_revision, 0 FROM tide_revisions WHERE revision = ?
    UNION ALL
    SELECT p.revision, p.base_revision, c.depth + 1
    FROM tide_revisions p JOIN chain c ON p.revision = c.base_rev
)
SELECT c.depth AS depth, v.minute AS minute, v.level AS level, v.error AS error
FROM chain c
JOIN tide_values v ON v.revision = c.rev
WHERE v.minute BETWEEN ? AND ?
UNION ALL
SELECT c.depth AS depth, d.minute AS minute, d.level AS level, d.error AS error
FROM chain c
JOIN tide_deltas d ON d.revision = c.rev
WHERE d.minute BETWEEN ? AND ?
"""


def load_tide(conn, revision: int, lo: int, hi: int
              ) -> dict[int, tuple[float, float]]:
    """还原修订 ``revision`` 在闭区间 [lo, hi] 内的 {minute: (level, error)}。

    每个分钟取祖先链上最近一次改动（depth 最小）的解析值；未知修订返回空
    映射（调用方通常已先做存在性校验）。修订内容不可变，结果与读取时刻无关。
    """
    best: dict[int, tuple[int, float, float]] = {}
    for r in conn.execute(_CHAIN_SQL, (revision, lo, hi, lo, hi)).fetchall():
        cur = best.get(r["minute"])
        if cur is None or r["depth"] < cur[0]:
            best[r["minute"]] = (r["depth"], r["level"], r["error"])
    return {m: (v[1], v[2]) for m, v in best.items()}


def minutes_present(conn, revision: int, minutes) -> set[int]:
    """给定分钟集合中，哪些分钟在 ``revision``（沿祖先链）已有名义潮位。"""
    if not minutes:
        return set()
    present = load_tide(conn, revision, min(minutes), max(minutes))
    return {m for m in minutes if m in present}


def create_correction(conn, corrections: dict[int, float],
                      base_revision: Optional[int],
                      note: Optional[str]) -> int:
    """名义潮位更正（调用方已持 BEGIN IMMEDIATE 事务）。

    只为所列分钟写入增量行：名义潮位取提交值；误差沿用基础修订该分钟的
    误差，基础修订没有该分钟（新分钟）时误差取 0。返回新修订号。
    """
    base = resolve_base(conn, base_revision)
    minutes = sorted(corrections)
    base_values = load_tide(conn, base, minutes[0], minutes[-1]) if minutes else {}

    cur = conn.execute(
        "INSERT INTO tide_revisions(note, base_revision, kind) VALUES (?, ?, 'correction')",
        (note or f"基于修订 #{base} 的名义潮位更正", base),
    )
    new_rev = cur.lastrowid
    # 每个所列分钟一行：持久化写入量 = 本次改动分钟数
    conn.executemany(
        "INSERT INTO tide_deltas(revision, minute, level, error) VALUES (?, ?, ?, ?)",
        [
            (new_rev, m, corrections[m],
             base_values[m][1] if m in base_values else 0.0)
            for m in minutes
        ],
    )
    return new_rev


def create_error_revision(conn, errors: dict[int, float],
                          base_revision: Optional[int],
                          note: Optional[str]) -> int:
    """预测误差修订（调用方已持 BEGIN IMMEDIATE 事务）。

    名义潮位不变；只允许提交基础修订（沿祖先链）已有名义潮位的分钟，
    否则抛 MISSING_TIDE_DATA（在插入新修订之前，不产生任何修订）。返回新修订号。
    """
    base = resolve_base(conn, base_revision)
    minutes = sorted(errors)
    base_values = load_tide(conn, base, minutes[0], minutes[-1]) if minutes else {}
    missing = [m for m in minutes if m not in base_values]
    if missing:
        raise ApiError(
            422, "MISSING_TIDE_DATA",
            f"基础修订 #{base} 中有 {len(missing)} 个分钟缺少名义潮位，"
            "误差修订不能为其指定误差",
            {"missing_minutes": missing[:20], "total_missing": len(missing),
             "base_revision": base},
        )

    cur = conn.execute(
        "INSERT INTO tide_revisions(note, base_revision, kind) VALUES (?, ?, 'error')",
        (note or f"基于修订 #{base} 的潮位预测误差修订", base),
    )
    new_rev = cur.lastrowid
    # 名义潮位沿用基础修订解析值，误差覆盖为提交值；写入量 = 本次改动分钟数
    conn.executemany(
        "INSERT INTO tide_deltas(revision, minute, level, error) VALUES (?, ?, ?, ?)",
        [(new_rev, m, base_values[m][0], errors[m]) for m in minutes],
    )
    return new_rev
