"""
Local SQLite database for settings, API keys, and chat history.
All data stays on your machine — nothing leaves locally.
"""
import sqlite3
import json
import os
import uuid
from pathlib import Path

DB_DIR = Path.home() / ".surgicalai"
DB_PATH = DB_DIR / "surgicalai.db"


def get_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()

    # Settings table (key-value store)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Chat sessions
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT 'New Chat',
            file_path TEXT,
            model TEXT DEFAULT 'gpt-4.1',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Chat messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
        )
    """)

    # Surgical change history
    cursor.execute("""
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

    # Pinned files/symbols per workspace
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pinned_context (
            id TEXT PRIMARY KEY,
            workspace_path TEXT NOT NULL,
            file_path TEXT NOT NULL,
            symbol_path TEXT,
            label TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Project memory/conventions per workspace
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS project_memory (
            id TEXT PRIMARY KEY,
            workspace_path TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Prompt templates
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            prompt TEXT NOT NULL,
            mode TEXT DEFAULT 'chat',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Per-session uploaded files
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS session_files (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            language TEXT DEFAULT 'plaintext',
            lines INTEGER DEFAULT 0,
            symbol_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Users (auth)
    cursor.execute("""
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

    # Add user_id to chat_sessions if missing (migration)
    existing_cols = [
        row[1]
        for row in cursor.execute("PRAGMA table_info(chat_sessions)").fetchall()
    ]
    if "user_id" not in existing_cols:
        cursor.execute(
            "ALTER TABLE chat_sessions ADD COLUMN user_id TEXT REFERENCES users(id) ON DELETE SET NULL"
        )

    # Insert defaults if not present
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
        cursor.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
        )

    # Insert default prompt templates
    default_templates = [
        ("Add error handling", "Add comprehensive error handling to this function. Use try/except blocks with specific exception types. Log errors appropriately.", "surgical"),
        ("Write unit tests", "Write unit tests for this function/class. Cover happy path, edge cases, and error cases.", "chat"),
        ("Refactor for readability", "Refactor this code for readability and maintainability. Improve naming, reduce complexity, add docstrings.", "surgical"),
        ("Add logging", "Add appropriate logging throughout this code. Use the existing logging pattern in the codebase.", "surgical"),
        ("Optimize performance", "Analyze and optimize this code for performance. Identify bottlenecks and suggest improvements.", "chat"),
        ("Explain this code", "Explain what this code does in detail. Cover the logic, patterns used, and any potential issues.", "chat"),
        ("Add type hints", "Add comprehensive type hints to all functions and variables in this code.", "surgical"),
        ("Security review", "Review this code for security vulnerabilities. Check for injection, auth issues, data validation problems.", "chat"),
    ]
    for name, prompt, mode in default_templates:
        # Use deterministic ID based on name so re-runs don't duplicate
        import hashlib
        det_id = hashlib.md5(f"default:{name}".encode()).hexdigest()
        cursor.execute(
            "INSERT OR IGNORE INTO prompt_templates (id, name, prompt, mode) VALUES (?, ?, ?, ?)",
            (det_id, name, prompt, mode)
        )

    # Dedup any templates that got duplicated from previous runs
    cursor.execute("""
        DELETE FROM prompt_templates
        WHERE id NOT IN (
            SELECT MIN(id) FROM prompt_templates GROUP BY name
        )
    """)

    conn.commit()
    conn.close()
    print(f"✅ Database initialized at {DB_PATH}")


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


def get_all_settings() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}
