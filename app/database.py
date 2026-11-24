"""SQLite 连接与建表。

三张业务表均以 (month, row_id) 为主键，保证“同月每表行号唯一”。
连接开启 WAL 与 busy_timeout，写事务统一使用 BEGIN IMMEDIATE，
使并发纠错在数据库层串行化，快照语义由事务隔离保证。
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

CREATE INDEX IF NOT EXISTS idx_dispatch_month_wo     ON dispatch(month, work_order);
CREATE INDEX IF NOT EXISTS idx_dispatch_month_proj   ON dispatch(month, project);
CREATE INDEX IF NOT EXISTS idx_attribution_month_proj ON attribution(month, project);
"""


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
    finally:
        conn.close()
