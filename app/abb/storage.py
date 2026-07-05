"""SQLite persistence: the download audit log and the wanted-list pipeline.

One small file (LOG_DB_PATH), same schema as v1 — an existing database drops
in unchanged and still self-migrates via additive ALTER TABLEs. v2 opens it in
WAL mode with a busy timeout so the wanted worker thread and request threads
never trip over each other, and adds indexes for the /log queries.

When LOG_DB_PATH is empty the log is disabled and wanted rows fall back to an
in-memory dict (lost on restart, like v1).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger("abb.storage")


class Store:
    def __init__(self, config):
        self.config = config
        self.enabled = config.log_enabled
        self._lock = threading.Lock()
        self._wanted_mem: dict[int, dict] = {}   # fallback when the DB is off

    # --- plumbing -------------------------------------------------------------
    def _connect(self):
        conn = sqlite3.connect(self.config.log_db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def init(self):
        """Create tables/indexes if logging is enabled. Non-fatal on failure —
        the app keeps working, it just won't record."""
        if not self.enabled:
            log.info("download log disabled (LOG_DB_PATH empty)")
            return
        try:
            os.makedirs(os.path.dirname(self.config.log_db_path) or ".", exist_ok=True)
            with self._lock, self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        user TEXT NOT NULL,
                        title TEXT NOT NULL,
                        link TEXT,
                        infohash TEXT,
                        client TEXT,
                        route TEXT,
                        status TEXT NOT NULL,
                        detail TEXT
                    )
                """)
                self._migrate(conn, "downloads", ("batch_id", "batch_label"))
                conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_ts ON downloads(ts)")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS wanted (
                        hc_id INTEGER PRIMARY KEY,
                        title TEXT,
                        author TEXT,
                        slug TEXT,
                        status TEXT,
                        best_link TEXT,
                        best_title TEXT,
                        best_meta TEXT,
                        searched_at TEXT,
                        detail TEXT
                    )
                """)
                self._migrate(conn, "wanted", ("candidates", "verdict"))
                # In-app feature settings (see abb/settings.py). env_snapshot
                # is the env value seen at save time — the key to the
                # most-recently-set-wins precedence.
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT,
                        env_snapshot TEXT,
                        updated_at TEXT,
                        updated_by TEXT
                    )
                """)
            log.info("download log at %s", self.config.log_db_path)
        except Exception as e:
            self.enabled = False
            log.warning("download log unavailable (%s): %s", self.config.log_db_path, e)

    @staticmethod
    def _migrate(conn, table, columns):
        """Add columns introduced after a table's first release. ADD COLUMN is
        cheap and additive, so old databases migrate themselves on boot."""
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")

    # --- download log -----------------------------------------------------------
    def record_download(self, user, title, link, infohash, status, detail="",
                        batch_id=None, batch_label=None, route=""):
        """Append one log row. Swallows storage errors so a logging hiccup can
        never fail an otherwise-successful download."""
        if not self.enabled:
            return
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO downloads (ts, user, title, link, infohash, client, route,"
                    " status, detail, batch_id, batch_label)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), user, title,
                     link, infohash, self.config.download_client, route, status, detail or "",
                     batch_id, batch_label),
                )
        except Exception as e:
            log.warning("failed to write download log entry: %s", e)

    def fetch_download_log(self, user_filter=None, limit=500):
        if not self.enabled:
            return []
        try:
            with self._connect() as conn:
                if user_filter:
                    rows = conn.execute(
                        "SELECT * FROM downloads WHERE user = ? ORDER BY id DESC LIMIT ?",
                        (user_filter, limit)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM downloads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            log.warning("failed to read download log: %s", e)
            return []

    # --- wanted rows ------------------------------------------------------------
    def wanted_rows(self):
        if not self.enabled:
            with self._lock:
                return [dict(r) for r in self._wanted_mem.values()]
        with self._lock, self._connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM wanted").fetchall()]

    def wanted_upsert(self, row):
        """Merge `row` into the stored row for its hc_id (partial updates fine)."""
        if not self.enabled:
            with self._lock:
                self._wanted_mem[row["hc_id"]] = {**self._wanted_mem.get(row["hc_id"], {}), **row}
            return
        cols = ("hc_id", "title", "author", "slug", "status", "best_link", "best_title",
                "best_meta", "searched_at", "detail", "candidates", "verdict")
        with self._lock, self._connect() as conn:
            existing = conn.execute("SELECT * FROM wanted WHERE hc_id = ?",
                                    (row["hc_id"],)).fetchone()
            merged = {**(dict(existing) if existing else {}), **row}
            conn.execute(
                f"INSERT OR REPLACE INTO wanted ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                tuple(merged.get(c) for c in cols))

    # --- in-app settings ----------------------------------------------------------
    def settings_all(self):
        """{key: row dict} of stored overrides. {} when the store is disabled
        or not yet initialized — settings then simply don't apply."""
        if not self.enabled:
            return {}
        try:
            with self._lock, self._connect() as conn:
                return {r["key"]: dict(r)
                        for r in conn.execute("SELECT * FROM settings").fetchall()}
        except Exception as e:
            log.warning("failed to read settings: %s", e)
            return {}

    def settings_set(self, key, value, env_snapshot, user):
        if not self.enabled:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, env_snapshot, updated_at, updated_by)"
                " VALUES (?, ?, ?, ?, ?)",
                (key, value, env_snapshot,
                 datetime.now(timezone.utc).isoformat(timespec="seconds"), user))

    def settings_delete(self, key):
        if not self.enabled:
            return
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def wanted_delete_missing(self, keep_ids):
        """Drop rows the user removed from their Hardcover list."""
        if not self.enabled:
            with self._lock:
                for k in list(self._wanted_mem):
                    if k not in keep_ids:
                        del self._wanted_mem[k]
            return
        with self._lock, self._connect() as conn:
            for r in conn.execute("SELECT hc_id FROM wanted").fetchall():
                if r["hc_id"] not in keep_ids:
                    conn.execute("DELETE FROM wanted WHERE hc_id = ?", (r["hc_id"],))
