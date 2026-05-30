"""
Database layer — SQLite (local dev) or PostgreSQL (cloud/Railway).
Set DATABASE_URL env var to use PostgreSQL. Falls back to SQLite.
"""
import os
import json
import uuid
import hashlib
from pathlib import Path

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
    if USE_POSTGRES:
        return CompatConn(DATABASE_URL)
    else:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        return conn


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

    _seed_defaults_sqlite(cur)
    conn.commit()
    conn.close()
    print(f"✅ SQLite database initialized at {DB_PATH}")


def _seed_defaults_sqlite(cur):
    defaults = {
        "architect_model": "gpt-5",
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
        conn.commit()

        _seed_defaults_postgres(conn)
        conn.commit()
    finally:
        conn.close()
    print("✅ PostgreSQL database initialized")


def _seed_defaults_postgres(conn):
    defaults = {
        "architect_model": "gpt-5",
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
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
        (key, value),
    )
    conn.commit()
    conn.close()


def set_user_api_key(user_id: str, key_type: str, encrypted_value: str):
    """Store an encrypted API key for a user. Upserts."""
    conn = get_db()
    key_id = hashlib.md5(f"{user_id}:{key_type}".encode()).hexdigest()
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
    conn.close()


def get_user_api_key(user_id: str, key_type: str) -> str:
    """Retrieve encrypted API key for a user. Returns empty string if not found."""
    conn = get_db()
    row = conn.execute(
        "SELECT encrypted_value FROM user_api_keys WHERE user_id = ? AND key_type = ?",
        (user_id, key_type),
    ).fetchone()
    conn.close()
    return row["encrypted_value"] if row else ""


def get_all_settings() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}
