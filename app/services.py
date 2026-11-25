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

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .schemas import (
    BulkAttribution,
    BulkDispatch,
    BulkWorkHour,
    ShiftReportIn,
)

class AppError(Exception):
    """业务错误，携带 HTTP 状态码与中文原因。"""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


BEIJING_TZ = timezone(timedelta(hours=8))
_MINUTE_FMT = "%Y-%m-%dT%H:%M"
_MAX_SHIFT_MINUTES = 24 * 60


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
    """手工录入工时。

    与另外两张表不同：
    - 整月覆盖（replace=true）只删除 source='manual' 的手工工时，班次工时保留；
    - 入参行号只要有一个落在 shift_month_row 的班次保留行号上，整批退回，
      不删不写任何行。
    """
    _check_unique(payload.rows, "工时表")
    requested = [r.row_id for r in payload.rows]
    try:
        conn.execute("BEGIN IMMEDIATE")

        reserved = _reserved_row_ids(conn, month, requested)
        if reserved:
            raise AppError(
                409,
                f"行号 {sorted(reserved)} 是班次在 {month} 保留的工时行号，"
                "手工录入不得占用，整批已退回，未写入任何改动",
            )

        if payload.replace:
            # 只替换手工工时；班次工时（source='shift'）原样保留。
            conn.execute("DELETE FROM work_hour WHERE month = ? AND source = 'manual'", (month,))

        conn.executemany(
            "INSERT INTO work_hour (month, row_id, work_order, minutes, source) "
            "VALUES (?, ?, ?, ?, 'manual')",
            [(month, r.row_id, r.work_order, r.minutes) for r in payload.rows],
        )
        conn.execute("COMMIT")
    except AppError:
        _rollback_if_open(conn)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback_if_open(conn)
        raise AppError(409, f"工时表写入失败（行号与已有数据冲突或约束不满足）: {exc}")
    except sqlite3.OperationalError as exc:
        _rollback_if_open(conn)
        if _is_locked(exc):
            raise AppError(409, "数据库正被其他事务占用，请基于最新已提交数据重试")
        raise
    return {"month": month, "table": "work_hour", "inserted": len(payload.rows), "replace": payload.replace}


def _reserved_row_ids(conn: sqlite3.Connection, month: str, row_ids: list[int]) -> set[int]:
    """返回给定行号中已被班次保留的部分（含班次已移出该月但保留号仍在的情况）。"""
    if not row_ids:
        return set()
    marks = ", ".join("?" for _ in row_ids)
    cur = conn.execute(
        f"SELECT row_id FROM shift_month_row WHERE month = ? AND row_id IN ({marks})",
        (month, *row_ids),
    )
    return {int(r[0]) for r in cur.fetchall()}


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
    """测试辅助：清空某月业务数据。

    若该月当前存在班次拆分出的工时行，按需求拒绝清空（409）；
    班次保留行号表 shift_month_row 不在清空范围内，以保证“移出后补回仍用原号”。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")
        shift_count = conn.execute(
            "SELECT COUNT(*) FROM work_hour WHERE month = ? AND source = 'shift'",
            (month,),
        ).fetchone()[0]
        if shift_count:
            raise AppError(
                409,
                f"{month} 存在 {shift_count} 行班次工时，拒绝清空；"
                "请先用更新的修订把班次移出该月后再试",
            )
        for t in ("work_hour", "dispatch", "attribution", "correction_audit"):
            conn.execute(f"DELETE FROM {t} WHERE month = ?", (month,))
        conn.execute("COMMIT")
    except AppError:
        _rollback_if_open(conn)
        raise
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


# ---------------------------------------------------------------------------
# 班次报送：北京时间时刻解析、按月界拆分、修订乐观语义、固定行号
# ---------------------------------------------------------------------------
def _parse_beijing_minute(value: Any, field_label: str) -> datetime:
    """把入参时刻解析为北京时间的 naive datetime，并截断到分钟。

    - 字符串默认按北京时间解释；若带时区偏移（含 Z），换算到 UTC+8 后去时区；
    - 也容忍 datetime / Unix 时间戳（秒或毫秒）。
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool) or value is None:
        raise AppError(422, f"{field_label} 必须是北京时间时刻（YYYY-MM-DDTHH:MM）")
    elif isinstance(value, (int, float)):
        number = float(value)
        if number > 1e12:  # 毫秒时间戳
            number /= 1000.0
        try:
            dt = datetime.fromtimestamp(number, tz=timezone.utc).astimezone(BEIJING_TZ).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            raise AppError(422, f"{field_label} 时间戳无法解析: {value!r}")
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise AppError(422, f"{field_label} 不能为空")
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            dt = None
            for fmt in (
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y%m%d%H%M",
            ):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise AppError(
                    422,
                    f"{field_label} 时间格式无法解析: {value!r}，请使用北京时间 YYYY-MM-DDTHH:MM",
                )
        if dt.tzinfo is not None:
            dt = dt.astimezone(BEIJING_TZ).replace(tzinfo=None)
    else:
        raise AppError(422, f"{field_label} 类型不支持: {type(value).__name__}")

    return dt.replace(second=0, microsecond=0)


def _split_by_month(start: datetime, end: datetime) -> list[tuple[str, int]]:
    """按北京时间月界拆分 [start, end)，返回 [(YYYY-MM, 分钟数)]，仅含正分钟月份。"""
    segments: list[tuple[str, int]] = []
    cur = start
    while cur < end:
        if cur.month == 12:
            next_boundary = datetime(cur.year + 1, 1, 1)
        else:
            next_boundary = datetime(cur.year, cur.month + 1, 1)
        seg_end = end if end < next_boundary else next_boundary
        minutes = int((seg_end - cur).total_seconds() // 60)
        if minutes > 0:
            segments.append((cur.strftime("%Y-%m"), minutes))
        cur = seg_end
    return segments


def _next_shift_row_id(conn: sqlite3.Connection, month: str) -> int:
    """新月分配行号：大于该月所有现存工时行与所有班次保留行号。"""
    return int(
        conn.execute(
            """
            SELECT COALESCE(MAX(rid), 0) + 1 FROM (
                SELECT row_id AS rid FROM work_hour WHERE month = ?
                UNION ALL
                SELECT row_id AS rid FROM shift_month_row WHERE month = ?
            )
            """,
            (month, month),
        ).fetchone()[0]
    )


def _build_shift_receipt(
    shift_value: Any,
    revision: int,
    work_order: str,
    start_dt: datetime,
    end_dt: datetime,
    lines: list[dict[str, Any]],
) -> dict[str, Any]:
    month_lines = [
        {
            "month": line["month"],
            "row_id": line["row_id"],
            "work_row_id": line["row_id"],
            "work_hour_row_id": line["row_id"],
            "row_no": line["row_id"],
            "minutes": line["minutes"],
            "work_minutes": line["minutes"],
            "duration_minutes": line["minutes"],
        }
        for line in lines
    ]
    receipt: dict[str, Any] = {
        "shift_id": shift_value,
        "shift_no": shift_value,
        "shift_number": shift_value,
        "revision": revision,
        "revision_no": revision,
        "work_order": work_order,
        "start_at": start_dt.strftime(_MINUTE_FMT),
        "end_at": end_dt.strftime(_MINUTE_FMT),
        "total_minutes": sum(item["minutes"] for item in month_lines),
        "months": month_lines,
        "by_month": {item["month"]: dict(item) for item in month_lines},
    }
    # 列表键别名，兼容不同调用方的命名习惯。
    for key in (
        "rows",
        "month_rows",
        "allocations",
        "entries",
        "items",
        "lines",
        "splits",
        "breakdown",
        "details",
        "month_minutes",
        "work_hours",
    ):
        receipt[key] = [dict(item) for item in month_lines]
    # 嵌套信封别名：响应体本身即回执，同时提供 data/receipt 副本。
    core = {k: v for k, v in receipt.items()}
    receipt["receipt"] = core
    receipt["data"] = dict(core)
    return receipt


def submit_shift(
    conn: sqlite3.Connection,
    payload: ShiftReportIn,
    path_shift_id: str | int | None = None,
) -> tuple[dict[str, Any], bool]:
    """报送/修订一个班次。

    返回 (回执, 是否为原样重放)。修订判定全部在 BEGIN IMMEDIATE 之后读取
    “写入前最新生效修订”完成，因此并发报送由 SQLite 写锁串行化。
    """
    shift_value: Any = payload.shift_id if payload.shift_id is not None else path_shift_id
    if shift_value is None or (isinstance(shift_value, str) and not shift_value.strip()):
        raise AppError(422, "必须提供班次号 shift_id")
    shift_key = str(shift_value).strip() if isinstance(shift_value, str) else str(shift_value)

    work_order = payload.work_order
    start_dt = _parse_beijing_minute(payload.start_at, "开始时刻 start_at")
    end_dt = _parse_beijing_minute(payload.end_at, "结束时刻 end_at")

    if end_dt <= start_dt:
        raise AppError(
            422,
            f"结束时刻 {end_dt.strftime(_MINUTE_FMT)} 必须晚于开始时刻 "
            f"{start_dt.strftime(_MINUTE_FMT)}，未做任何改动",
        )
    total_minutes = int((end_dt - start_dt).total_seconds() // 60)
    if total_minutes > _MAX_SHIFT_MINUTES:
        raise AppError(
            422,
            f"班次时长 {total_minutes} 分钟，超过最长 24 小时（1440 分钟）限制，未做任何改动",
        )

    start_minute = start_dt.strftime(_MINUTE_FMT)
    end_minute = end_dt.strftime(_MINUTE_FMT)
    segments = _split_by_month(start_dt, end_dt)

    receipt: dict[str, Any] | None = None
    replayed = False
    try:
        conn.execute("BEGIN IMMEDIATE")

        latest = conn.execute(
            "SELECT revision, work_order, start_minute, end_minute, receipt_json "
            "FROM shift_revision WHERE shift_id = ? ORDER BY revision DESC LIMIT 1",
            (shift_key,),
        ).fetchone()
        latest_revision = int(latest["revision"]) if latest is not None else None

        if latest_revision is not None and payload.revision < latest_revision:
            raise AppError(
                409,
                f"修订号 {payload.revision} 是旧号：班次 {shift_key} 最新生效修订为 "
                f"{latest_revision}；旧修订退回，原修订与工时保持不变",
            )

        same_content = bool(
            latest is not None
            and latest["work_order"] == work_order
            and latest["start_minute"] == start_minute
            and latest["end_minute"] == end_minute
        )

        if latest_revision is not None and payload.revision == latest_revision:
            if not same_content:
                raise AppError(
                    409,
                    f"修订号 {payload.revision} 已是班次 {shift_key} 的最新生效修订，"
                    "但报送内容（工单号/起止时刻）与原修订不一致；同号不得报送不同内容，"
                    "已退回，原修订与工时保持不变",
                )
            # 最新版原样重报：直接返回首次生效时存储的原回执。
            receipt = json.loads(latest["receipt_json"])
            replayed = True
        else:
            # 首次报送（任意正整数）或比最新生效修订更新的修订：
            # 取/建各月固定行号。
            lines: list[dict[str, Any]] = []
            for month, minutes in segments:
                mapping = conn.execute(
                    "SELECT row_id FROM shift_month_row WHERE shift_id = ? AND month = ?",
                    (shift_key, month),
                ).fetchone()
                if mapping is not None:
                    row_id = int(mapping["row_id"])  # 移出后补回：仍用原号
                else:
                    row_id = _next_shift_row_id(conn, month)
                    conn.execute(
                        "INSERT INTO shift_month_row (shift_id, month, row_id) VALUES (?, ?, ?)",
                        (shift_key, month, row_id),
                    )
                lines.append({"month": month, "row_id": row_id, "minutes": minutes})

            receipt = _build_shift_receipt(
                shift_value, payload.revision, work_order, start_dt, end_dt, lines
            )

            # 一次替换该班次在所有月份的工时行。
            conn.execute("DELETE FROM work_hour WHERE shift_id = ?", (shift_key,))
            conn.executemany(
                "INSERT INTO work_hour "
                "(month, row_id, work_order, minutes, source, shift_id) "
                "VALUES (?, ?, ?, ?, 'shift', ?)",
                [
                    (line["month"], line["row_id"], work_order, line["minutes"], shift_key)
                    for line in lines
                ],
            )
            conn.execute(
                "INSERT INTO shift_revision "
                "(shift_id, revision, work_order, start_minute, end_minute, receipt_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    shift_key,
                    payload.revision,
                    work_order,
                    start_minute,
                    end_minute,
                    json.dumps(receipt, ensure_ascii=False),
                ),
            )

        conn.execute("COMMIT")
    except AppError:
        _rollback_if_open(conn)
        raise
    except sqlite3.IntegrityError as exc:
        _rollback_if_open(conn)
        raise AppError(409, f"班次报送写入冲突，请基于最新已生效修订重试: {exc}")
    except sqlite3.OperationalError as exc:
        _rollback_if_open(conn)
        if _is_locked(exc):
            raise AppError(409, "数据库正被其他班次报送事务占用，请基于最新已生效修订重试")
        raise
    except Exception:
        _rollback_if_open(conn)
        raise

    assert receipt is not None
    return receipt, replayed


def get_shift(conn: sqlite3.Connection, shift_id_value: str | int) -> dict[str, Any]:
    shift_key = str(shift_id_value)
    latest = conn.execute(
        "SELECT receipt_json FROM shift_revision WHERE shift_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (shift_key,),
    ).fetchone()
    if latest is None:
        raise AppError(404, f"班次 {shift_id_value} 不存在或尚无生效修订")
    receipt = json.loads(latest["receipt_json"])
    revisions = conn.execute(
        "SELECT revision, work_order, start_minute AS start_at, end_minute AS end_at, created_at "
        "FROM shift_revision WHERE shift_id = ? ORDER BY revision",
        (shift_key,),
    ).fetchall()
    receipt["revisions"] = [dict(row) for row in revisions]
    return receipt


def list_shifts(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT r.shift_id, r.revision, r.receipt_json
        FROM shift_revision r
        JOIN (
            SELECT shift_id, MAX(revision) AS max_revision
            FROM shift_revision GROUP BY shift_id
        ) m ON m.shift_id = r.shift_id AND m.max_revision = r.revision
        ORDER BY r.shift_id
        """
    ).fetchall()
    shifts = []
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        shifts.append(
            {
                "shift_id": receipt.get("shift_id", row["shift_id"]),
                "revision": int(row["revision"]),
                "months": receipt.get("months", []),
            }
        )
    return {"shifts": shifts, "total": len(shifts)}
