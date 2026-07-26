"""
Database layer — SQLite (local dev) or PostgreSQL (cloud/Railway).
Set DATABASE_URL env var to use PostgreSQL. Falls back to SQLite.
"""
import os
import json
import uuid
import hashlib
import time
import logging
import threading
import datetime as _dt
from pathlib import Path

# ---------------------------------------------------------------------------
# Connection-lifecycle logging
# ---------------------------------------------------------------------------
# IMPORTANT: this _dlog is intentionally standalone (Python `logging` + a flat
# /tmp file) and must NEVER call back into get_db()/get_db_ctx(). The
# database-backed _dlog used elsewhere in the app (services/pipeline.py)
# writes its debug events through get_db_ctx() — if database.py's own
# connection logging did that too, a DB outage would recurse into itself
# (log the failure to open a connection by... opening a connection).
_db_logger = logging.getLogger("surgicalai.database")
_DB_DLOG_PATH = "/tmp/surgicalai_db_debug.jsonl"

# Tracks how many DB connections are currently open (best-effort, not
# thread-safe-strict, but good enough for leak *trend* detection in logs —
# a steadily climbing number across log lines means connections aren't
# being closed somewhere).
_open_conn_count = 0
_open_conn_lock = threading.Lock()
_LEAK_WARN_THRESHOLD = 20  # flag in logs if this many connections are open at once


def _dlog(event: str, **kwargs):
    """Lightweight, DB-independent debug log for connection lifecycle events.

    Writes to both the standard Python logger (shows up in Railway logs)
    and a flat /tmp file (fast, greppable fallback). Never raises — a
    logging failure must never break a real DB operation.
    """
    try:
        ts = _dt.datetime.utcnow().isoformat() + "Z"
        record = {"ts": ts, "event": event, **kwargs}
        _db_logger.info("[db] %s", json.dumps(record, default=str))
        try:
            with open(_DB_DLOG_PATH, "a") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass  # /tmp write is best-effort only; logger line above already fired
    except Exception:
        pass  # logging must never break a DB call

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = DATABASE_URL.startswith("postgres")

# Reserved workspace_path sentinel for GLOBAL (team-wide) project memory.
# Stored as a normal project_memory row under this key. Merged into EVERY chat
# prompt — every session, every user — alongside any per-session memory.
GLOBAL_MEMORY_KEY = "__global__"

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    print("🐘 Database: PostgreSQL")
else:
    import sqlite3
    DB_DIR = Path.home() / ".surgicalai"
    DB_PATH = DB_DIR / "surgicalai.db"
    print(f"🗄️  Database: SQLite at {Path.home() / '.surgicalai' / 'surgicalai.db'}")


# ---------------------------------------------------------------------------
# PostgreSQL compatibility adapter
# ---------------------------------------------------------------------------

class CompatRow(dict):
    """Dict-like row that also supports integer indexing (matches sqlite3.Row API)."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class CompatCursor:
    """Wraps a psycopg2 RealDictCursor to match the sqlite3 cursor interface."""
    def __init__(self, pg_cur):
        self._cur = pg_cur

    def fetchone(self):
        row = self._cur.fetchone()
        return CompatRow(row) if row is not None else None

    def fetchall(self):
        return [CompatRow(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        # Only valid after INSERT ... RETURNING id
        try:
            row = self._cur.fetchone()
            return row[0] if row else None
        except Exception:
            return None


class CompatConn:
    """
    sqlite3-API-compatible wrapper around a psycopg2 connection.
    Automatically converts:
      - ? placeholders → %s
      - INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
      - INSERT OR REPLACE INTO settings → INSERT ... ON CONFLICT (key) DO UPDATE
    """
    def __init__(self, url: str):
        self._conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params=()):
        sql = self._adapt(sql)
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
        except Exception:
            self._conn.rollback()
            raise
        return CompatCursor(cur)

    def _adapt(self, sql: str) -> str:
        """Translate SQLite-flavour SQL to PostgreSQL."""
        sql = sql.replace("?", "%s")
        if "INSERT OR IGNORE INTO" in sql:
            sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            sql = sql.rstrip() + "\nON CONFLICT DO NOTHING"
        elif "INSERT OR REPLACE INTO settings" in sql:
            sql = sql.replace("INSERT OR REPLACE INTO settings", "INSERT INTO settings")
            sql = sql.rstrip() + (
                "\nON CONFLICT (key) DO UPDATE SET"
                " value = EXCLUDED.value,"
                " updated_at = EXCLUDED.updated_at"
            )
        elif "INSERT OR REPLACE INTO change_history" in sql:
            sql = sql.replace("INSERT OR REPLACE INTO change_history", "INSERT INTO change_history")
            sql = sql.rstrip() + (
                "\nON CONFLICT (id) DO UPDATE SET"
                " file_path = EXCLUDED.file_path,"
                " symbol_path = EXCLUDED.symbol_path,"
                " original_code = EXCLUDED.original_code,"
                " new_code = EXCLUDED.new_code,"
                " applied = EXCLUDED.applied"
            )
        elif "INSERT OR REPLACE INTO" in sql:
            # Generic fallback for any INSERT OR REPLACE
            import re as _re
            m = _re.search(r"INSERT OR REPLACE INTO (\w+)", sql)
            if m:
                table = m.group(1)
                sql = sql.replace(f"INSERT OR REPLACE INTO {table}", f"INSERT INTO {table}")
                sql = sql.rstrip() + "\nON CONFLICT DO NOTHING"
        return sql

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_db_connection():
    """Alias for get_db() used by pipeline compliance + QA logging."""
    return get_db()


def get_db():
    """Open a new DB connection. Logs open/failure and tracks a live-count
    so leaks show up as a steadily rising number in the debug logs.

    NOTE: callers are responsible for closing this connection. Prefer
    get_db_ctx() (a `with` block) wherever possible — it closes
    automatically even on exception. Raw get_db() call sites that don't
    use try/finally are a known leak risk (see _dlog "db_open" trend).
    """
    global _open_conn_count
    t0 = time.monotonic()
    try:
        if USE_POSTGRES:
            conn = CompatConn(DATABASE_URL)
        else:
            DB_DIR.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
    except Exception as e:
        _dlog("db_open_failed", driver=("postgres" if USE_POSTGRES else "sqlite"),
              error=str(e), elapsed_ms=round((time.monotonic() - t0) * 1000, 1))
        raise

    with _open_conn_lock:
        _open_conn_count += 1
        current_count = _open_conn_count

    _dlog("db_open", driver=("postgres" if USE_POSTGRES else "sqlite"),
          open_connections=current_count, elapsed_ms=round((time.monotonic() - t0) * 1000, 1))
    if current_count >= _LEAK_WARN_THRESHOLD:
        _dlog("db_open_count_high_possible_leak", open_connections=current_count,
              threshold=_LEAK_WARN_THRESHOLD)
    return conn


def _note_conn_closed(reason: str, exc: Exception = None):
    """Shared bookkeeping for connection close — decrements the live count
    and logs whether this close happened after an exception in the caller's
    `with` block (that path is exactly what raw get_db()+close() call sites
    miss, since they never run cleanup on exception)."""
    global _open_conn_count
    with _open_conn_lock:
        _open_conn_count = max(0, _open_conn_count - 1)
        current_count = _open_conn_count
    if exc is not None:
        _dlog("db_close_after_exception", reason=reason, open_connections=current_count,
              exception_type=type(exc).__name__, exception_msg=str(exc))
    else:
        _dlog("db_close", reason=reason, open_connections=current_count)


from contextlib import contextmanager

@contextmanager
def get_db_ctx():
    """Context-managed DB connection — guarantees close() even on exception.

    Usage:
        with get_db_ctx() as conn:
            conn.execute(...)
            conn.commit()
        # conn.close() called automatically, even if an exception occurred.

    This is the preferred way to get a DB connection. It logs open/close
    (including whether close happened after an exception) so any future
    leak or crash-mid-query shows up clearly in the debug logs.
    """
    conn = get_db()
    exc_seen = None
    try:
        yield conn
    except Exception as e:
        exc_seen = e
        raise
    finally:
        try:
            conn.close()
        except Exception as close_err:
            _dlog("db_close_failed", error=str(close_err))
        _note_conn_closed(reason="get_db_ctx", exc=exc_seen)


def _ensure_session_files_indexes(executor):
    """Index `session_files` on session_id and enforce one row per filename.

    Works for both the sqlite cursor and the Postgres connection — both expose
    `.execute(sql)`.

    WHY
    ---
    1. `session_id` was unindexed, yet EVERY upload runs
       `SELECT SUM(LENGTH(content)) ... WHERE session_id = ?` to enforce the
       30 MB session cap. With no index that is a full-table scan per upload,
       so a burst of N uploads costs O(N * table_size).
    2. There was no uniqueness on `(session_id, filename)` even though every
       write path does a read-then-insert upsert keyed on exactly that pair.
       Concurrent uploads of the same filename can both miss the SELECT and
       both INSERT, leaving duplicate rows — after which "the" file id for a
       filename is ambiguous and edits can land on the stale row.

    SAFETY: the unique index is only created when the table currently holds no
    duplicates. If duplicates already exist we log and skip rather than
    deleting user data; the plain index (the performance fix) still lands.
    """
    try:
        executor.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_files_session "
            "ON session_files(session_id)"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[db] idx_session_files_session skipped: {e}")

    try:
        dupes = executor.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT session_id, filename FROM session_files"
            "  GROUP BY session_id, filename HAVING COUNT(*) > 1"
            ") d"
        ).fetchone()
        dupe_count = int((dupes[0] if dupes else 0) or 0)
    except Exception as e:  # noqa: BLE001
        print(f"[db] session_files duplicate check failed: {e}")
        return

    if dupe_count:
        print(
            f"[db] WARNING: {dupe_count} duplicate (session_id, filename) "
            f"group(s) in session_files — UNIQUE index NOT created. "
            f"Resolve the duplicates, then restart to enforce uniqueness."
        )
        return

    try:
        executor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_session_files_session_filename "
            "ON session_files(session_id, filename)"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[db] idx_session_files_session_filename skipped: {e}")


def init_db():
    if USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite()


# ---------------------------------------------------------------------------
# SQLite init
# ---------------------------------------------------------------------------

def _init_sqlite():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # Allow concurrent reads during writes; persists in DB file, set once at init
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New Chat',
            file_path TEXT,
            model TEXT DEFAULT 'gpt-4.1',
            session_summary TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            is_compacted INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS change_history (
            id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            symbol_path TEXT NOT NULL,
            original_code TEXT NOT NULL,
            new_code TEXT NOT NULL,
            applied INTEGER DEFAULT 0,
            session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pinned_context (
            id TEXT PRIMARY KEY,
            workspace_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            symbol_path TEXT,
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS project_memory (
            id TEXT PRIMARY KEY,
            workspace_path TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            mode TEXT DEFAULT 'chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_files (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            language TEXT DEFAULT 'plaintext',
            lines INTEGER DEFAULT 0,
            symbol_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            hashed_password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )
    """)
    # session_file_versions — full, append-only edit history per file.
    # Every time a file's content changes (apply, undo, restore) the PRIOR
    # content is snapshotted here first, so a file is always reversible to
    # any past state, not just the single most-recent one.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS session_file_versions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            file_id TEXT NOT NULL,
            content TEXT NOT NULL,
            lines INTEGER DEFAULT 0,
            symbol_count INTEGER DEFAULT 0,
            label TEXT DEFAULT 'Edit',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_session_file_versions_file
        ON session_file_versions(file_id, created_at)
    """)
    _ensure_session_files_indexes(cur)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_api_keys (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            key_type TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, key_type)
        )
    """)
    # Migration: add user_id to chat_sessions if missing
    existing_cols = [row[1] for row in cur.execute("PRAGMA table_info(chat_sessions)").fetchall()]
    if "user_id" not in existing_cols:
        cur.execute("ALTER TABLE chat_sessions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE SET NULL")
    if "session_summary" not in existing_cols:
        cur.execute("ALTER TABLE chat_sessions ADD COLUMN session_summary TEXT DEFAULT ''")

    # Migration: add is_compacted to chat_messages if missing
    cm_cols = [row[1] for row in cur.execute("PRAGMA table_info(chat_messages)").fetchall()]
    if "is_compacted" not in cm_cols:
        cur.execute("ALTER TABLE chat_messages ADD COLUMN is_compacted INTEGER DEFAULT 0")

    # Migration: add file_type to session_files if missing
    sf_cols = [row[1] for row in cur.execute("PRAGMA table_info(session_files)").fetchall()]
    if "file_type" not in sf_cols:
        cur.execute("ALTER TABLE session_files ADD COLUMN file_type TEXT DEFAULT 'code'")
    if "previous_content" not in sf_cols:
        cur.execute("ALTER TABLE session_files ADD COLUMN previous_content TEXT")
    if "updated_at" not in sf_cols:
        cur.execute("ALTER TABLE session_files ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    if "origin" not in sf_cols:
        # 'uploaded' (user-provided) | 'created' (AI-generated net-new file)
        cur.execute("ALTER TABLE session_files ADD COLUMN origin TEXT DEFAULT 'uploaded'")
    if "edited" not in sf_cols:
        cur.execute("ALTER TABLE session_files ADD COLUMN edited INTEGER DEFAULT 0")
    if "github_meta" not in sf_cols:
        cur.execute("ALTER TABLE session_files ADD COLUMN github_meta TEXT")
    if "github_pushed_at" not in sf_cols:
        cur.execute("ALTER TABLE session_files ADD COLUMN github_pushed_at TIMESTAMP")

    # Migration: add QA log + compliance log tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS qa_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            filename TEXT,
            symbol_name TEXT,
            verdict TEXT,
            qa_score INTEGER,
            issues_json TEXT,
            ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS compliance_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE,
            session_id TEXT,
            intent TEXT,
            steps_json TEXT,
            missing_steps TEXT,
            overall_pass INTEGER DEFAULT 1,
            ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # agent_tasks — agentic task list (plan → execute → track → cancel).
    # One row per task within an agentic run. status moves through:
    #   pending → running → done | blocked | cancelled | error
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            run_id TEXT,
            seq INTEGER DEFAULT 0,
            title TEXT NOT NULL,
            detail TEXT DEFAULT '',
            kind TEXT DEFAULT 'code',
            status TEXT DEFAULT 'pending',
            qa_score INTEGER,
            verdict TEXT,
            cancel_requested INTEGER DEFAULT 0,
            result_summary TEXT DEFAULT '',
            thinking TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add thinking to agent_tasks if missing (extended-thinking
    # transparency — each task persists the model's reasoning for the UI).
    at_cols = [row[1] for row in cur.execute("PRAGMA table_info(agent_tasks)").fetchall()]
    if at_cols and "thinking" not in at_cols:
        cur.execute("ALTER TABLE agent_tasks ADD COLUMN thinking TEXT DEFAULT ''")

    # debug_events — persistent pipeline debug log (survives deploys/restarts)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debug_events (
            id TEXT PRIMARY KEY,
            event TEXT NOT NULL,
            session_id TEXT DEFAULT '',
            user_id TEXT DEFAULT '',
            data TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # github_app_installations — one row per (user, installation). A user can
    # have multiple installations (e.g. personal account + one or more orgs).
    # permission_tier gates write access at the API layer (routers/github_app.py)
    # — read_only / read_comment / read_write. Legacy PAT flow (user_api_keys,
    # key_type='github') is completely separate and untouched.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS github_app_installations (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            installation_id TEXT NOT NULL,
            account_login TEXT DEFAULT '',
            permission_tier TEXT DEFAULT 'read_only',
            connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, installation_id)
        )
    """)

    _seed_defaults_sqlite(cur)
    _migrate_sqlite(cur)
    conn.commit()
    conn.close()
    print(f"✅ SQLite database initialized at {DB_PATH}")


def _migrate_sqlite(cur):
    """One-time data migrations that run on every startup (idempotent).
    Safe for Railway/Vercel — those DBs won't have gpt-5 seeded so this is a no-op there."""
    # Bug fix: gpt-5 was incorrectly seeded as the default architect model in early versions.
    # gpt-5 is real but extremely slow (30-40s) with no visible progress — users see a frozen UI.
    # Migrate any existing installs to gpt-4.1.
    cur.execute(
        "UPDATE settings SET value = 'gpt-4.1', updated_at = CURRENT_TIMESTAMP "
        "WHERE key = 'architect_model' AND value = 'gpt-5'"
    )
    if cur.rowcount:
        print("🔧 Migrated architect_model from gpt-5 → gpt-4.1")


def _seed_defaults_sqlite(cur):
    defaults = {
        "architect_model": "gpt-4.1",
        "surgeon_model": "gpt-4.1",
        "openai_api_key": "",
        "temperature_architect": "0.3",
        "temperature_surgeon": "0.1",
        "confidence_threshold": "7",
        "auto_backup": "true",
        "theme": "dark",
        "font_size": "14",
        "workspace_path": str(Path.home()),
        "ollama_enabled": "false",
        "ollama_base_url": "http://localhost:11434",
        "ollama_model": "qwen2.5-coder:7b",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    _seed_templates_sqlite(cur)
    cur.execute("""
        DELETE FROM prompt_templates
        WHERE id NOT IN (SELECT MIN(id) FROM prompt_templates GROUP BY name)
    """)


def _seed_templates_sqlite(cur):
    templates = _default_templates()
    for det_id, name, prompt, mode in templates:
        cur.execute(
            "INSERT OR IGNORE INTO prompt_templates (id, name, prompt, mode) VALUES (?, ?, ?, ?)",
            (det_id, name, prompt, mode),
        )


# ---------------------------------------------------------------------------
# PostgreSQL init
# ---------------------------------------------------------------------------

def _init_postgres():
    conn = CompatConn(DATABASE_URL)
    try:
        # settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # chat_sessions
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New Chat',
                file_path TEXT,
                model TEXT DEFAULT 'gpt-4.1',
                user_id TEXT,
                session_summary TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # chat_messages
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                is_compacted INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
            )
        """)
        # change_history
        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_history (
                id TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                symbol_path TEXT NOT NULL,
                original_code TEXT NOT NULL,
                new_code TEXT NOT NULL,
                applied INTEGER DEFAULT 0,
                session_id TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # pinned_context
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pinned_context (
                id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL,
                file_path TEXT NOT NULL,
                symbol_path TEXT,
                label TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # project_memory
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_memory (
                id TEXT PRIMARY KEY,
                workspace_path TEXT NOT NULL,
                content TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # prompt_templates
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prompt_templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                prompt TEXT NOT NULL,
                mode TEXT DEFAULT 'chat',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # session_files
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_files (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                filename TEXT NOT NULL,
                content TEXT NOT NULL,
                language TEXT DEFAULT 'plaintext',
                lines INTEGER DEFAULT 0,
                symbol_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # users
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                email TEXT,
                hashed_password TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP
            )
        """)
        # session_file_versions — full, append-only edit history per file
        # (mirrors the sqlite table above; see comment there for rationale)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_file_versions (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                file_id TEXT NOT NULL,
                content TEXT NOT NULL,
                lines INTEGER DEFAULT 0,
                symbol_count INTEGER DEFAULT 0,
                label TEXT DEFAULT 'Edit',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_session_file_versions_file
            ON session_file_versions(file_id, created_at)
        """)
        _ensure_session_files_indexes(conn)
        # user_api_keys — encrypted API keys per user
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_api_keys (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                key_type TEXT NOT NULL,
                encrypted_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, key_type)
            )
        """)
        # Migration: add user_id column if missing (idempotent on PG)
        conn.execute("""
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS user_id TEXT
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS file_type TEXT DEFAULT 'code'
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS previous_content TEXT
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        """)
        conn.execute("""
            ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS session_summary TEXT DEFAULT ''
        """)
        conn.execute("""
            ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS is_compacted INTEGER DEFAULT 0
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS github_meta TEXT
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS github_pushed_at TIMESTAMP
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'uploaded'
        """)
        conn.execute("""
            ALTER TABLE session_files ADD COLUMN IF NOT EXISTS edited INTEGER DEFAULT 0
        """)
        # qa_log — proof QA ran on every Surgeon run (was missing on Postgres,
        # so the audit trail was silently empty in production)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS qa_log (
                id SERIAL PRIMARY KEY,
                session_id TEXT,
                filename TEXT,
                symbol_name TEXT,
                verdict TEXT,
                qa_score INTEGER,
                issues_json TEXT,
                ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # compliance_log — proves every required pipeline step ran
        conn.execute("""
            CREATE TABLE IF NOT EXISTS compliance_log (
                id SERIAL PRIMARY KEY,
                run_id TEXT UNIQUE,
                session_id TEXT,
                intent TEXT,
                steps_json TEXT,
                missing_steps TEXT,
                overall_pass INTEGER DEFAULT 1,
                ran_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # agent_tasks — agentic task list (plan → execute → track → cancel)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                run_id TEXT,
                seq INTEGER DEFAULT 0,
                title TEXT NOT NULL,
                detail TEXT DEFAULT '',
                kind TEXT DEFAULT 'code',
                status TEXT DEFAULT 'pending',
                qa_score INTEGER,
                verdict TEXT,
                cancel_requested INTEGER DEFAULT 0,
                result_summary TEXT DEFAULT '',
                thinking TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Migration: add thinking column for pre-existing deployments (idempotent on PG)
        conn.execute("""
            ALTER TABLE agent_tasks ADD COLUMN IF NOT EXISTS thinking TEXT DEFAULT ''
        """)
        # debug_events — persistent pipeline debug log (survives deploys/restarts)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS debug_events (
                id TEXT PRIMARY KEY,
                event TEXT NOT NULL,
                session_id TEXT DEFAULT '',
                user_id TEXT DEFAULT '',
                data TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_debug_events_created
            ON debug_events(created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_debug_events_session
            ON debug_events(session_id)
        """)
        # github_app_installations — one row per (user, installation). A user can
        # have multiple installations (e.g. personal account + one or more orgs).
        # permission_tier gates write access at the API layer (routers/github_app.py)
        # — read_only / read_comment / read_write. Legacy PAT flow (user_api_keys,
        # key_type='github') is completely separate and untouched.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS github_app_installations (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                installation_id TEXT NOT NULL,
                account_login TEXT DEFAULT '',
                permission_tier TEXT DEFAULT 'read_only',
                connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, installation_id)
            )
        """)
        conn.commit()

        _seed_defaults_postgres(conn)
        _migrate_postgres(conn)
        conn.commit()
    finally:
        conn.close()
    print("✅ PostgreSQL database initialized")


def _migrate_postgres(conn):
    """One-time idempotent data migrations for Postgres. Safe — no-op if value already correct."""
    # Bug fix: gpt-5 was incorrectly seeded as default architect_model in early versions.
    cur = conn.execute(
        "UPDATE settings SET value = 'gpt-4.1', updated_at = CURRENT_TIMESTAMP "
        "WHERE key = 'architect_model' AND value = 'gpt-5'"
    )
    if cur.rowcount:
        print("🔧 Migrated architect_model from gpt-5 → gpt-4.1")


def _seed_defaults_postgres(conn):
    defaults = {
        "architect_model": "gpt-4.1",
        "surgeon_model": "gpt-4.1",
        "openai_api_key": "",
        "temperature_architect": "0.3",
        "temperature_surgeon": "0.1",
        "confidence_threshold": "7",
        "auto_backup": "true",
        "theme": "dark",
        "font_size": "14",
        "workspace_path": "/tmp",
        "ollama_enabled": "false",
        "ollama_base_url": "http://localhost:11434",
        "ollama_model": "qwen2.5-coder:7b",
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )
    for det_id, name, prompt, mode in _default_templates():
        conn.execute(
            "INSERT OR IGNORE INTO prompt_templates (id, name, prompt, mode) VALUES (?, ?, ?, ?)",
            (det_id, name, prompt, mode),
        )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_templates():
    raw = [
        ("Add error handling", "Add comprehensive error handling to this function. Use try/except blocks with specific exception types. Log errors appropriately.", "surgical"),
        ("Write unit tests", "Write unit tests for this function/class. Cover happy path, edge cases, and error cases.", "chat"),
        ("Refactor for readability", "Refactor this code for readability and maintainability. Improve naming, reduce complexity, add docstrings.", "surgical"),
        ("Add logging", "Add appropriate logging throughout this code. Use the existing logging pattern in the codebase.", "surgical"),
        ("Optimize performance", "Analyze and optimize this code for performance. Identify bottlenecks and suggest improvements.", "chat"),
        ("Explain this code", "Explain what this code does in detail. Cover the logic, patterns used, and any potential issues.", "chat"),
        ("Add type hints", "Add comprehensive type hints to all functions and variables in this code.", "surgical"),
        ("Security review", "Review this code for security vulnerabilities. Check for injection, auth issues, data validation problems.", "chat"),
    ]
    return [
        (hashlib.md5(f"default:{name}".encode()).hexdigest(), name, prompt, mode)
        for name, prompt, mode in raw
    ]


# ---------------------------------------------------------------------------
# Settings helpers (used by routers)
# ---------------------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    try:
        with get_db_ctx() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    except Exception as e:
        # Never let a DB hiccup take down a setting lookup — degrade to
        # env var / default, but log it loudly so it's visible in Railway logs.
        _dlog("get_setting_db_error", key=key, error=str(e))
        row = None
    if row:
        return row["value"]
    # Fallback: check OS environment variable (UPPER_CASE convention)
    env_val = os.environ.get(key.upper(), os.environ.get(key, ""))
    return env_val if env_val else default


def set_setting(key: str, value: str):
    with get_db_ctx() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (key, value),
        )
        conn.commit()


def set_user_api_key(user_id: str, key_type: str, encrypted_value: str):
    """Store an encrypted API key for a user. Upserts."""
    key_id = hashlib.md5(f"{user_id}:{key_type}".encode()).hexdigest()
    with get_db_ctx() as conn:
        if USE_POSTGRES:
            conn.execute(
                """INSERT INTO user_api_keys (id, user_id, key_type, encrypted_value, updated_at)
                   VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (user_id, key_type) DO UPDATE SET
                     encrypted_value = EXCLUDED.encrypted_value,
                     updated_at = EXCLUDED.updated_at""",
                (key_id, user_id, key_type, encrypted_value),
            )
        else:
            conn.execute(
                """INSERT OR REPLACE INTO user_api_keys (id, user_id, key_type, encrypted_value, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (key_id, user_id, key_type, encrypted_value),
            )
        conn.commit()


def get_user_api_key(user_id: str, key_type: str) -> str:
    """Retrieve encrypted API key for a user. Returns empty string if not found."""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT encrypted_value FROM user_api_keys WHERE user_id = ? AND key_type = ?",
            (user_id, key_type),
        ).fetchone()
    return row["encrypted_value"] if row else ""


def save_github_app_installation(user_id: str, installation_id: str, account_login: str):
    """Upsert one (user_id, installation_id) link. Keeps whatever
    permission_tier was already set if this is a re-install; defaults to
    read_only for a brand new link (safest default)."""
    row_id = hashlib.md5(f"{user_id}:{installation_id}".encode()).hexdigest()
    with get_db_ctx() as conn:
        if USE_POSTGRES:
            conn.execute(
                """INSERT INTO github_app_installations (id, user_id, installation_id, account_login, updated_at)
                   VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP)
                   ON CONFLICT (user_id, installation_id) DO UPDATE SET
                     account_login = EXCLUDED.account_login,
                     updated_at = EXCLUDED.updated_at""",
                (row_id, user_id, installation_id, account_login),
            )
        else:
            conn.execute(
                """INSERT INTO github_app_installations (id, user_id, installation_id, account_login, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT (user_id, installation_id) DO UPDATE SET
                     account_login = excluded.account_login,
                     updated_at = excluded.updated_at""",
                (row_id, user_id, installation_id, account_login),
            )
        conn.commit()
    _dlog("github_app_installation_saved", user_id=user_id, installation_id=installation_id, account_login=account_login)


def list_github_app_installations(user_id: str) -> list:
    """All installations linked to this user, each with its permission tier."""
    with get_db_ctx() as conn:
        rows = conn.execute(
            "SELECT installation_id, account_login, permission_tier, connected_at FROM github_app_installations WHERE user_id = ? ORDER BY connected_at DESC",
            (user_id,),
        ).fetchall()
    result = [
        {
            "installation_id": row["installation_id"] if hasattr(row, "__getitem__") else row[0],
            "account_login": row["account_login"] if hasattr(row, "__getitem__") else row[1],
            "permission_tier": row["permission_tier"] if hasattr(row, "__getitem__") else row[2],
            "connected_at": str(row["connected_at"] if hasattr(row, "__getitem__") else row[3]),
        }
        for row in rows
    ]
    _dlog("github_app_installations_listed", user_id=user_id, count=len(result))
    return result


def get_github_app_installation(user_id: str, installation_id: str) -> dict:
    """Single installation row for this user, or {} if not found/not theirs
    (this also acts as the ownership check — a user can never look up an
    installation_id that isn't linked to their own user_id)."""
    with get_db_ctx() as conn:
        row = conn.execute(
            "SELECT installation_id, account_login, permission_tier FROM github_app_installations WHERE user_id = ? AND installation_id = ?",
            (user_id, installation_id),
        ).fetchone()
    if not row:
        return {}
    return {
        "installation_id": row["installation_id"] if hasattr(row, "__getitem__") else row[0],
        "account_login": row["account_login"] if hasattr(row, "__getitem__") else row[1],
        "permission_tier": row["permission_tier"] if hasattr(row, "__getitem__") else row[2],
    }


def set_github_app_permission_tier(user_id: str, installation_id: str, tier: str):
    with get_db_ctx() as conn:
        conn.execute(
            "UPDATE github_app_installations SET permission_tier = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND installation_id = ?",
            (tier, user_id, installation_id),
        )
        conn.commit()
    _dlog("github_app_permission_tier_set", user_id=user_id, installation_id=installation_id, tier=tier)


def delete_github_app_installation(user_id: str, installation_id: str):
    with get_db_ctx() as conn:
        conn.execute(
            "DELETE FROM github_app_installations WHERE user_id = ? AND installation_id = ?",
            (user_id, installation_id),
        )
        conn.commit()
    _dlog("github_app_installation_deleted", user_id=user_id, installation_id=installation_id)


def get_all_settings() -> dict:
    with get_db_ctx() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {row["key"]: row["value"] for row in rows}
