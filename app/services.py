"""业务逻辑。

关键设计
--------
完整路径 = 工时 -> 分派(同工单) -> 归科(同项目) -> 预算科目。

路径核算只用一条集合式 GROUP BY SQL 完成，服务端不逐条展开路径
（不按工时逐行 JOIN、不在 Python 里追链），同科目不同路径仍按不同路径计数。

纠错在单条 BEGIN IMMEDIATE 事务内完成：撤除前先读已提交快照做校验，
撤除与供数变化同一事务提交；任一校验失败即 ROLLBACK，数据保持原状。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from .schemas import (
    BulkAttribution,
    BulkDispatch,
    BulkWorkHour,
)

class AppError(Exception):
    """业务错误，携带 HTTP 状态码与中文原因。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ---------------------------------------------------------------------------
# 集合式供数核算（单条 SQL，不逐行展开路径）
# ---------------------------------------------------------------------------
# LEFT JOIN 保证没有任何路径的工时也出现在结果里（path_count = 0）。
# COUNT(a.row_id) 同时完成两件事：
#   1. 归科缺失的连接结果为 NULL，不计入路径；
#   2. 同一工时有多条(分派,归科)组合时逐组合计数——即使预算科目相同，
#      也视为两条路径，path_count >= 2 即判重复扣留。
PATH_SQL = """
SELECT h.row_id                                  AS work_row_id,
       h.work_order                              AS work_order,
       h.minutes                                 AS minutes,
       COUNT(a.row_id)                           AS path_count,
       MIN(a.budget_subject)                     AS budget_subject
FROM work_hour h
LEFT JOIN dispatch d
       ON d.month = h.month AND d.work_order = h.work_order
LEFT JOIN attribution a
       ON a.month = d.month AND a.project = d.project
WHERE h.month = ?
GROUP BY h.row_id, h.work_order, h.minutes
ORDER BY h.row_id
"""


def _path_rows(conn: sqlite3.Connection, month: str) -> list[sqlite3.Row]:
    return list(conn.execute(PATH_SQL, (month,)).fetchall())


def _split_supply(rows: list[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    suppliable: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for r in rows:
        if r["path_count"] == 1:
            suppliable.append(
                {
                    "work_row_id": r["work_row_id"],
                    "work_order": r["work_order"],
                    "minutes": r["minutes"],
                    "budget_subject": r["budget_subject"],
                    "path_count": 1,
                }
            )
        else:
            withheld.append(
                {
                    "work_row_id": r["work_row_id"],
                    "work_order": r["work_order"],
                    "minutes": r["minutes"],
                    "path_count": r["path_count"],
                    "reason": "no_path" if r["path_count"] == 0 else "duplicate",
                }
            )
    return {"suppliable": suppliable, "withheld": withheld}


def query_supply(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    rows = _path_rows(conn, month)
    result = _split_supply(rows)
    return {"month": month, **result}


# ---------------------------------------------------------------------------
# 录入（整月覆盖或追加；同月行号唯一由主键兜底）
# ---------------------------------------------------------------------------
def _check_unique(rows: list[Any], label: str) -> None:
    ids = [r.row_id for r in rows]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise AppError(422, f"{label}内存在重复行号: {dup}，同月行号必须唯一")


def _bulk_upsert(
    conn: sqlite3.Connection,
    month: str,
    payload: Any,
    table: str,
    columns: tuple[str, ...],
    label: str,
) -> dict[str, Any]:
    _check_unique(payload.rows, label)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if payload.replace:
            conn.execute(f"DELETE FROM {table} WHERE month = ?", (month,))
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} (month, {', '.join(columns)}) "
            f"VALUES (?, {placeholders})"
        )
        conn.executemany(
            sql,
            [(month, *(getattr(r, c) for c in columns)) for r in payload.rows],
        )
        conn.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        raise AppError(409, f"{label}写入失败（行号与已有数据冲突或约束不满足）: {exc}")
    except sqlite3.OperationalError as exc:
        _rollback_if_open(conn)
        if _is_locked(exc):
            raise AppError(409, "数据库正被其他纠错事务占用，请基于最新已提交数据重试")
        raise
    return {"month": month, "table": table, "inserted": len(payload.rows), "replace": payload.replace}


def load_work_hours(conn: sqlite3.Connection, month: str, payload: BulkWorkHour) -> dict[str, Any]:
    return _bulk_upsert(conn, month, payload, "work_hour", ("row_id", "work_order", "minutes"), "工时表")


def load_dispatches(conn: sqlite3.Connection, month: str, payload: BulkDispatch) -> dict[str, Any]:
    return _bulk_upsert(conn, month, payload, "dispatch", ("row_id", "work_order", "project"), "分派表")


def load_attributions(conn: sqlite3.Connection, month: str, payload: BulkAttribution) -> dict[str, Any]:
    return _bulk_upsert(conn, month, payload, "attribution", ("row_id", "project", "budget_subject"), "归科表")


# ---------------------------------------------------------------------------
# 纠错：撤除一条分派/归科，撤除与供数变化一起生效
# ---------------------------------------------------------------------------
def _is_locked(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _rollback_if_open(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # 无活动事务


def _record_exists(conn: sqlite3.Connection, table: str, month: str, row_id: int) -> bool:
    cur = conn.execute(
        f"SELECT 1 FROM {table} WHERE month = ? AND row_id = ?", (month, row_id)
    )
    return cur.fetchone() is not None


def _target_path_ids(
    conn: sqlite3.Connection, month: str, work_row_id: int, table: str, remove_row_id: int
) -> int:
    """该待撤除记录落在目标工时完整路径上的条数（集合 SQL）。"""
    if table == "dispatch":
        sql = """
        SELECT COUNT(*)
        FROM work_hour h
        JOIN dispatch d ON d.month = h.month AND d.work_order = h.work_order
        JOIN attribution a ON a.month = d.month AND a.project = d.project
        WHERE h.month = ? AND h.row_id = ? AND d.row_id = ?
        """
    else:
        sql = """
        SELECT COUNT(*)
        FROM work_hour h
        JOIN dispatch d ON d.month = h.month AND d.work_order = h.work_order
        JOIN attribution a ON a.month = d.month AND a.project = d.project
        WHERE h.month = ? AND h.row_id = ? AND a.row_id = ?
        """
    return int(conn.execute(sql, (month, work_row_id, remove_row_id)).fetchone()[0])


def correct(
    conn: sqlite3.Connection, month: str, work_row_id: int, remove: str, remove_row_id: int
) -> dict[str, Any]:
    table = "dispatch" if remove == "dispatch" else "attribution"
    try:
        # 串行化：并发纠错以各自 BEGIN IMMEDIATE 前的已提交数据为准，
        # 后到者阻塞/失败，不会在同一份数据上各改各的。
        conn.execute("BEGIN IMMEDIATE")

        if not _record_exists(conn, "work_hour", month, work_row_id):
            raise AppError(404, f"工时行号 {work_row_id} 在 {month} 不存在，未做任何改动")
        if not _record_exists(conn, table, month, remove_row_id):
            raise AppError(404, f"{remove} 行号 {remove_row_id} 在 {month} 不存在，未做任何改动")

        # 撤除前快照（已提交数据 + 本事务尚未修改）
        before = {r["work_row_id"]: r for r in _path_rows(conn, month)}
        target_before = before.get(work_row_id)
        if target_before is None:  # 理论上不会发生，防御
            raise AppError(404, f"工时行号 {work_row_id} 无核算数据，未做任何改动")

        on_path = _target_path_ids(conn, month, work_row_id, table, remove_row_id)
        if on_path == 0:
            raise AppError(
                422,
                f"{remove} 行号 {remove_row_id} 不在工时行 {work_row_id} 的任何完整路径上，"
                "撤除它无法纠正该工时，未做任何改动",
            )

        before_count = target_before["path_count"]
        if before_count == 1:
            raise AppError(
                409,
                f"工时行 {work_row_id} 当前仅有 1 条完整路径、本就可供数，"
                "撤除会使其失去科目，拒绝执行，未做任何改动",
            )
        if before_count == 0:
            raise AppError(
                409,
                f"工时行 {work_row_id} 当前没有完整路径（无科目可归），"
                "撤除记录无法使其供数，拒绝执行，未做任何改动",
            )

        # 撤除 + 审计，与供数变化同一事务
        conn.execute(
            f"DELETE FROM {table} WHERE month = ? AND row_id = ?",
            (month, remove_row_id),
        )
        conn.execute(
            "INSERT INTO correction_audit (month, work_row_id, removed_table, removed_row_id) "
            "VALUES (?, ?, ?, ?)",
            (month, work_row_id, remove, remove_row_id),
        )

        # 撤除后重新集合核算
        after = {r["work_row_id"]: r for r in _path_rows(conn, month)}
        target_after = after[work_row_id]
        if target_after["path_count"] != 1:
            raise AppError(
                409,
                f"撤除后工时行 {work_row_id} 仍有 {target_after['path_count']} 条路径，"
                "不满足“恰好一条完整路径”，已回滚，数据保持撤除前状态",
            )

        # 原本可供数的工时必须全部保有科目
        broken = [
            wid
            for wid, r in before.items()
            if r["path_count"] == 1 and after[wid]["path_count"] != 1
        ]
        if broken:
            raise AppError(
                409,
                f"撤除会使原本可供数的工时行 {sorted(broken)} 失去科目，"
                "违反保科规则，已回滚，数据保持撤除前状态",
            )

        restored = sorted(
            wid
            for wid, r in before.items()
            if r["path_count"] >= 2 and after[wid]["path_count"] == 1
        )

        conn.execute("COMMIT")
    except AppError:
        _rollback_if_open(conn)
        raise
    except sqlite3.OperationalError as exc:
        _rollback_if_open(conn)
        if _is_locked(exc):
            raise AppError(409, "数据库正被其他纠错事务占用，请基于最新已提交数据重试")
        raise
    except Exception:
        _rollback_if_open(conn)
        raise

    final = _split_supply(list(after.values()))
    return {
        "month": month,
        "work_row_id": work_row_id,
        "removed": {"table": remove, "row_id": remove_row_id},
        "restored_work_row_ids": restored,
        **final,
    }


def reset_month(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    """测试辅助：清空某月全部数据。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        for t in ("work_hour", "dispatch", "attribution", "correction_audit"):
            conn.execute(f"DELETE FROM {t} WHERE month = ?", (month,))
        conn.execute("COMMIT")
    except sqlite3.OperationalError as exc:
        _rollback_if_open(conn)
        if _is_locked(exc):
            raise AppError(409, "数据库正被其他事务占用，请重试")
        raise
    return {"month": month, "cleared": True}


def list_audit(conn: sqlite3.Connection, month: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, month, work_row_id, removed_table, removed_row_id, created_at "
        "FROM correction_audit WHERE month = ? ORDER BY id",
        (month,),
    ).fetchall()
    return [dict(r) for r in rows]
