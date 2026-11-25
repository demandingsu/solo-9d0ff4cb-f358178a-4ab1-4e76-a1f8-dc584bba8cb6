"""SQLite 连接与建表。

三张业务表均以 (month, row_id) 为主键，保证“同月每表行号唯一”。
连接开启 WAL 与 busy_timeout，写事务统一使用 BEGIN IMMEDIATE，
使并发写入在数据库层串行化，快照语义由事务隔离保证。

班次功能新增：
- shift_revision：每个班次的全部修订（含规范化内容与原始回执 JSON）；
- shift_month_row：班次在某月分配到的固定行号（即使该修订已移出该月也保留，
  补回时仍用原号；同时作为“班次保留行号”的唯一依据）；
- work_hour 增加 source（manual/shift）与 shift_id，区分手工工时与班次工时。
"""

from __future__ import annotations

import os
import sqlite3

DB_PATH = os.getenv("APP_DB_PATH", "/data/maintenance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_hour (
    month      TEXT NOT NULL,
    row_id     INTEGER NOT NULL,
    work_order TEXT NOT NULL,
    minutes    INTEGER NOT NULL CHECK (minutes >= 0),
    source     TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'shift')),
    shift_id   TEXT,
    PRIMARY KEY (month, row_id)
);

CREATE TABLE IF NOT EXISTS dispatch (
    month      TEXT NOT NULL,
    row_id     INTEGER NOT NULL,
    work_order TEXT NOT NULL,
    project    TEXT NOT NULL,
    PRIMARY KEY (month, row_id)
);

CREATE TABLE IF NOT EXISTS attribution (
    month          TEXT NOT NULL,
    row_id         INTEGER NOT NULL,
    project        TEXT NOT NULL,
    budget_subject TEXT NOT NULL,
    PRIMARY KEY (month, row_id)
);

CREATE TABLE IF NOT EXISTS correction_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    month           TEXT NOT NULL,
    work_row_id     INTEGER NOT NULL,
    removed_table   TEXT NOT NULL CHECK (removed_table IN ('dispatch', 'attribution')),
    removed_row_id  INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shift_revision (
    shift_id     TEXT NOT NULL,
    revision     INTEGER NOT NULL CHECK (revision >= 1),
    work_order   TEXT NOT NULL,
    start_minute TEXT NOT NULL,
    end_minute   TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (shift_id, revision)
);

CREATE TABLE IF NOT EXISTS shift_month_row (
    shift_id TEXT NOT NULL,
    month    TEXT NOT NULL,
    row_id   INTEGER NOT NULL CHECK (row_id >= 1),
    PRIMARY KEY (shift_id, month),
    UNIQUE (month, row_id)
);

CREATE INDEX IF NOT EXISTS idx_dispatch_month_wo     ON dispatch(month, work_order);
CREATE INDEX IF NOT EXISTS idx_dispatch_month_proj   ON dispatch(month, project);
CREATE INDEX IF NOT EXISTS idx_attribution_month_proj ON attribution(month, project);
-- idx_work_hour_shift 由 _migrate() 在补列之后创建，避免旧库尚无 shift_id 列时建索引失败。
CREATE INDEX IF NOT EXISTS idx_shift_month_row_month ON shift_month_row(month);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """对旧版本已存在的库做幂等加列迁移。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(work_hour)").fetchall()}
    if "source" not in cols:
        # 旧数据全部视为手工工时；SQLite 的 ALTER ADD COLUMN 带常量默认值。
        conn.execute("ALTER TABLE work_hour ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if "shift_id" not in cols:
        conn.execute("ALTER TABLE work_hour ADD COLUMN shift_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_hour_shift ON work_hour(shift_id)")


def get_conn() -> sqlite3.Connection:
    """每个请求一条独立连接，autocommit 模式，事务显式 BEGIN/COMMIT。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    # check_same_thread=False：FastAPI 的同步依赖与端点可能落在线程池的不同线程上，
    # 而每条连接仅服务单个请求、不被多线程并发共享；写一致性由 BEGIN IMMEDIATE
    # 与 busy_timeout 保证，因此允许跨线程使用同一条连接。
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
    finally:
        conn.close()
