import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 两侧调度同时确认/重放时串行等待，避免“database is locked”裸错
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None, immediate: bool = False):
    """immediate=True 时 BEGIN IMMEDIATE 先抢写锁：额度账册/闸口互斥改写用。"""
    conn = connect(db_path)
    try:
        if immediate:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
BEGIN;
CREATE TABLE IF NOT EXISTS backfill_windows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('草稿','已计划','执行中','已完成')),
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS backfill_slices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待重放','已落库','失败')),
  row_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(window_id, slice_date)
);

CREATE TABLE IF NOT EXISTS coverage_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  planned INTEGER NOT NULL,
  loaded INTEGER NOT NULL,
  missing INTEGER NOT NULL,
  quota INTEGER NOT NULL DEFAULT 0,
  poured INTEGER NOT NULL DEFAULT 0,
  excess INTEGER NOT NULL DEFAULT 0,
  gate_no TEXT,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 旁路配额账册：额度与已灌量同账册同行（同源），每窗一行
CREATE TABLE IF NOT EXISTS quota_ledger (
  window_id INTEGER PRIMARY KEY REFERENCES backfill_windows(id),
  quota INTEGER NOT NULL DEFAULT 0,
  poured INTEGER NOT NULL DEFAULT 0,
  gate_status TEXT NOT NULL DEFAULT '闭闸' CHECK (gate_status IN ('闭闸','开闸')),
  gate_no TEXT,
  gate_opened_by TEXT,
  gate_opened_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 目标表落库留证：一片只落一次
CREATE TABLE IF NOT EXISTS target_rows (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL REFERENCES backfill_windows(id),
  slice_id INTEGER NOT NULL REFERENCES backfill_slices(id),
  slice_date TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(slice_id)
);

-- 闸口/拒灌/双账事件留证（本地库）
CREATE TABLE IF NOT EXISTS quota_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  window_id INTEGER NOT NULL,
  slice_id INTEGER,
  kind TEXT NOT NULL CHECK (kind IN ('开闸','拒灌','双账拦截','额度调整')),
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
COMMIT;
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """老库补齐：coverage_reports 新列 + 每窗账册行。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(coverage_reports)")}
    for col, ddl in (
        ("quota", "INTEGER NOT NULL DEFAULT 0"),
        ("poured", "INTEGER NOT NULL DEFAULT 0"),
        ("excess", "INTEGER NOT NULL DEFAULT 0"),
        ("gate_no", "TEXT"),
    ):
        if col not in cols:
            conn.execute(f"ALTER TABLE coverage_reports ADD COLUMN {col} {ddl}")
    conn.execute(
        "INSERT OR IGNORE INTO quota_ledger(window_id, quota) "
        "SELECT id, ? FROM backfill_windows",
        (Config.DEFAULT_QUOTA,),
    )


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_orders_d','2026-11-10','2026-11-12','草稿')"
        )
        conn.execute(
            "INSERT INTO backfill_windows(table_name, start_date, end_date, status) "
            "VALUES ('dw_payments_d','2026-11-11','2026-11-11','草稿')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO quota_ledger(window_id, quota) "
            "SELECT id, ? FROM backfill_windows",
            (Config.DEFAULT_QUOTA,),
        )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
