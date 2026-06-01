"""
DataLab artifact storage.

Stores raw spreadsheet/CSV bytes (the ORIGINAL upload AND every edited version)
so the workbook can be round-tripped. Bytes are base64-encoded into a TEXT
column — this works identically on SQLite and the Postgres compat layer, with
no BYTEA param-binding edge cases.

Table: datalab_artifacts
  id            TEXT PK
  session_id    TEXT      owning chat session
  file_id       TEXT      links to session_files.id (the visible file row)
  filename      TEXT
  version       INTEGER   1 = original upload, 2+ = edits
  parent_id     TEXT      previous version's artifact id (NULL for v1)
  origin        TEXT      'uploaded' | 'edited' | 'created'
  mime          TEXT
  data_b64      TEXT      base64 of the raw bytes
  byte_size     INTEGER   decoded size (pre-base64)
  transform_sql TEXT      the SQL/spec that produced this version (audit/repro)
  created_at    TIMESTAMP

Schema creation is idempotent (CREATE TABLE IF NOT EXISTS) and lazy — the first
call ensures the table exists, so this lane never depends on init_db ordering.
"""
from __future__ import annotations

import base64
import uuid
from typing import Optional

from database import get_db, USE_POSTGRES
from .config import MAX_UPLOAD_BYTES, MAX_VERSIONS_PER_FILE

_schema_ready = False


def ensure_schema() -> None:
    """Create the datalab_artifacts table if absent. Idempotent + cached."""
    global _schema_ready
    if _schema_ready:
        return
    conn = get_db()
    try:
        if USE_POSTGRES:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS datalab_artifacts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    file_id TEXT,
                    filename TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    parent_id TEXT,
                    origin TEXT DEFAULT 'uploaded',
                    mime TEXT DEFAULT '',
                    data_b64 TEXT NOT NULL,
                    byte_size INTEGER DEFAULT 0,
                    transform_sql TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS datalab_artifacts (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    file_id TEXT,
                    filename TEXT NOT NULL,
                    version INTEGER DEFAULT 1,
                    parent_id TEXT,
                    origin TEXT DEFAULT 'uploaded',
                    mime TEXT DEFAULT '',
                    data_b64 TEXT NOT NULL,
                    byte_size INTEGER DEFAULT 0,
                    transform_sql TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        conn.commit()
        _schema_ready = True
    finally:
        conn.close()


class ArtifactTooLarge(ValueError):
    """Raised when a payload exceeds MAX_UPLOAD_BYTES."""


def save_artifact(
    *,
    session_id: str,
    filename: str,
    raw: bytes,
    origin: str = "uploaded",
    mime: str = "",
    file_id: Optional[str] = None,
    version: int = 1,
    parent_id: Optional[str] = None,
    transform_sql: str = "",
) -> str:
    """Persist raw bytes as a versioned artifact. Returns the new artifact id."""
    if raw is None:
        raise ValueError("raw bytes required")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ArtifactTooLarge(
            f"{len(raw)} bytes exceeds limit of {MAX_UPLOAD_BYTES} bytes"
        )
    ensure_schema()
    art_id = uuid.uuid4().hex
    data_b64 = base64.b64encode(raw).decode("ascii")
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO datalab_artifacts
               (id, session_id, file_id, filename, version, parent_id,
                origin, mime, data_b64, byte_size, transform_sql)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (art_id, session_id, file_id, filename, version, parent_id,
             origin, mime, data_b64, len(raw), transform_sql),
        )
        conn.commit()
    finally:
        conn.close()
    return art_id


def get_artifact_bytes(artifact_id: str) -> Optional[bytes]:
    """Return the decoded raw bytes for an artifact, or None if missing."""
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT data_b64 FROM datalab_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return base64.b64decode(row["data_b64"])


def get_artifact_meta(artifact_id: str) -> Optional[dict]:
    """Return artifact metadata (no bytes) or None."""
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT id, session_id, file_id, filename, version, parent_id,
                      origin, mime, byte_size, transform_sql, created_at
               FROM datalab_artifacts WHERE id = ?""",
            (artifact_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def latest_version_for_file(file_id: str) -> int:
    """Highest version number recorded for a logical file. 0 if none."""
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM datalab_artifacts WHERE file_id = ?",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or row["v"] is None:
        return 0
    return int(row["v"])


def artifact_id_for_file(file_id: str) -> Optional[str]:
    """The backing artifact id for a session_files row (newest if several)."""
    ensure_schema()
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM datalab_artifacts WHERE file_id = ? "
            "ORDER BY created_at DESC, version DESC LIMIT 1",
            (file_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def list_versions(file_id: str) -> list:
    """All artifact versions for a logical file, oldest first."""
    ensure_schema()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT id, filename, version, origin, byte_size, transform_sql, created_at
               FROM datalab_artifacts WHERE file_id = ? ORDER BY version ASC""",
            (file_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def next_version(file_id: str) -> int:
    """Compute the next version number, enforcing the chain cap."""
    cur = latest_version_for_file(file_id)
    if cur >= MAX_VERSIONS_PER_FILE:
        raise ValueError(f"version chain cap ({MAX_VERSIONS_PER_FILE}) reached")
    return cur + 1
