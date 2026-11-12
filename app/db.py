import sqlite3
from contextlib import contextmanager

from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def db_transaction():
    db = get_db()
    # 配额判定与写入必须在同一把写锁内完成，否则并发继续可能同时放行超配额。
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def _ensure_column(db, table: str, column: str, declaration: str) -> None:
    columns = {
        row["name"] for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_db(app):
    with app.app_context():
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS crawl_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                seed_path TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS crawl_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
                admitted_date TEXT,
                UNIQUE(job_id, item_key)
            );
            CREATE TABLE IF NOT EXISTS catalog_quotas (
                catalog_id TEXT PRIMARY KEY,
                daily_quota INTEGER NOT NULL CHECK (daily_quota >= 0)
            );
            CREATE TABLE IF NOT EXISTS queue_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES crawl_jobs(id) ON DELETE CASCADE,
                item_key TEXT NOT NULL,
                sku TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                is_priority INTEGER NOT NULL DEFAULT 0,
                priority_reason TEXT,
                priority_at TEXT,
                queued_at TEXT NOT NULL,
                queued_date TEXT NOT NULL,
                admitted_at TEXT,
                admitted_date TEXT,
                UNIQUE(job_id, item_key)
            );
            CREATE INDEX IF NOT EXISTS idx_queue_job_status
                ON queue_entries (job_id, status);
            """
        )
        # 旧库迁移：入正式结果的日期用于按服务端日期统计配额。
        _ensure_column(db, "crawl_records", "admitted_date", "TEXT")
        db.execute(
            """
            UPDATE crawl_records
            SET admitted_date = substr(fetched_at, 1, 10)
            WHERE admitted_date IS NULL
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_records_admitted ON crawl_records (admitted_date)"
        )
        db.commit()
