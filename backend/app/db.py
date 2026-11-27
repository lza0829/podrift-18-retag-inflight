from __future__ import annotations
import os
import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

_MEMORY_DB = os.environ.get("PODRIFT_MEMORY_DB") == "1"


def connect() -> sqlite3.Connection:
    # timeout 即 busy_timeout：并发写线程在写锁上排队，而不是立刻抛 locked
    if _MEMORY_DB:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=10, uri=True)
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _MEMORY_DB:
        # 内存库不支持 WAL；文件库开 WAL 让读写并发、崩溃可恢复
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def db_write():
    """立即写事务：进事务即拿写锁，改标与在途启动据此串行化，杜绝半截脏标签。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS images (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          tag TEXT NOT NULL,
          pull_state TEXT NOT NULL,
          layers_done INTEGER NOT NULL DEFAULT 0,
          layers_total INTEGER NOT NULL DEFAULT 4,
          ready_at TEXT
        );
        CREATE TABLE IF NOT EXISTS pods (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          image_id INTEGER,
          run_state TEXT NOT NULL,
          sidecar TEXT,
          cpu_quota REAL NOT NULL DEFAULT 1.0,
          mem_quota_mb INTEGER NOT NULL DEFAULT 512,
          FOREIGN KEY(image_id) REFERENCES images(id)
        );
        CREATE TABLE IF NOT EXISTS start_orders (
          id INTEGER PRIMARY KEY,
          pod_id INTEGER NOT NULL,
          action TEXT NOT NULL,
          target_image_id INTEGER,
          status TEXT NOT NULL,
          note TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY(pod_id) REFERENCES pods(id)
        );
        CREATE TABLE IF NOT EXISTS probes (
          id INTEGER PRIMARY KEY,
          pod_id INTEGER NOT NULL UNIQUE,
          color TEXT NOT NULL,
          fail_streak INTEGER NOT NULL DEFAULT 0,
          updated_at TEXT,
          FOREIGN KEY(pod_id) REFERENCES pods(id)
        );
        CREATE TABLE IF NOT EXISTS quotas (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          cpu_used REAL NOT NULL,
          cpu_limit REAL NOT NULL,
          mem_used_mb INTEGER NOT NULL,
          mem_limit_mb INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS restart_policy (
          id INTEGER PRIMARY KEY CHECK (id = 1),
          mode TEXT NOT NULL,
          backoff_sec INTEGER NOT NULL DEFAULT 5
        );
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          ref TEXT,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        /* 镜像标签版本史：每次改标追加一行，images.tag 始终等于该表最大 version 的标签，
           全系统只有这一个在轨标签观感，禁止长期双观感。 */
        CREATE TABLE IF NOT EXISTS image_tags (
          image_id INTEGER NOT NULL,
          version INTEGER NOT NULL,
          tag TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (image_id, version),
          FOREIGN KEY(image_id) REFERENCES images(id)
        );

        /* 改标戳：一次改标一行，带全局单调 change_seq；跟标臂下 follow_order_ids 留下可核痕迹。 */
        CREATE TABLE IF NOT EXISTS tag_changes (
          id INTEGER PRIMARY KEY,
          change_seq INTEGER NOT NULL UNIQUE,
          image_id INTEGER NOT NULL,
          old_tag TEXT NOT NULL,
          new_tag TEXT NOT NULL,
          arm TEXT NOT NULL,
          follow_order_ids TEXT NOT NULL DEFAULT '',
          changed_at TEXT NOT NULL,
          FOREIGN KEY(image_id) REFERENCES images(id)
        );

        /* 对读屏延后桶：只追加账本。在途期间 held=1 的行被读屏扣在桶里，收口时放行，
           行本身永不删改，按 seq 即可确定性重放。 */
        CREATE TABLE IF NOT EXISTS align_bucket (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          at TEXT NOT NULL,
          pod_id INTEGER NOT NULL,
          order_id INTEGER,
          image_id INTEGER NOT NULL,
          event_type TEXT NOT NULL,
          arm TEXT NOT NULL,
          tag_shown TEXT NOT NULL,
          tag_locked TEXT,
          tag_track TEXT NOT NULL,
          ref_change_id INTEGER,
          held INTEGER NOT NULL DEFAULT 1,
          released_at TEXT,
          detail TEXT NOT NULL
        );
        """
    )
    # 旧库增量迁移：启停单补上在途/锁标/跟标字段
    _ensure_columns(
        conn,
        "start_orders",
        {
            "locked_image_id": "INTEGER",
            "locked_tag": "TEXT",
            "effective_tag": "TEXT",
            "follow_change_id": "INTEGER",
            "released_at": "TEXT",
        },
    )
