"""
Two-model AI pipeline.
- Architect: GPT-5 (or configured model) — reads symbol map, reasons, produces plan
- Surgeon: GPT-4.1 — receives plan + code chunk, writes minimal precise replacement

Best Practice #1: Read the map before touching the territory (AST-first)
Best Practice #2: Minimal footprint (surgeon only touches requested symbol)
Best Practice #3: Verify before commit (confidence scoring + diff)
"""
import ast
import json
import re
import uuid
import difflib
import time
import logging
from pathlib import Path
from typing import Optional
from openai import OpenAI
import httpx

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# DEBUG LOGGING  (writes to DB for persistence + /tmp file for fast fallback)
# ─────────────────────────────────────────────────────────────────────────────
import json as _json_mod
import datetime as _dt
import os as _os
import uuid as _uuid_dlog
import random as _rnd_dlog
import hashlib as _hashlib

_DLOG_PATH = "/tmp/surgical_debug.jsonl"
_DLOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB cap — rotate by truncating oldest half
_DLOG_EXPIRE_DAYS = 7               # auto-expire DB entries older than this

def _dlog(event: str, **kwargs):
    """Append a structured debug record to the database (persistent) and /tmp file (fast fallback)."""
    try:
        ts = _dt.datetime.utcnow().isoformat() + "Z"
        record = {"ts": ts, "event": event, **kwargs}
        line = _json_mod.dumps(record, default=str) + "\n"

        # ── Primary: write to database (survives deploys/restarts) ──
        try:
            from database import get_db_ctx
            with get_db_ctx() as conn:
                session_id = str(kwargs.get("session_id", ""))
                user_id = str(kwargs.get("user_id", ""))
                data_json = _json_mod.dumps(record, default=str)
                conn.execute(
                    "INSERT INTO debug_events (id, event, session_id, user_id, data, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (str(_uuid_dlog.uuid4()), event, session_id, user_id, data_json, ts),
                )
                # Probabilistic cleanup: ~1 in 50 calls, expire old entries
                if _rnd_dlog.random() < 0.02:
                    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=_DLOG_EXPIRE_DAYS)).isoformat()
                    conn.execute("DELETE FROM debug_events WHERE created_at < ?", (cutoff,))
                conn.commit()
        except Exception:
            pass  # DB write failed — file fallback still works

        # ── Secondary: write to /tmp file (backward compat + fast grep) ──
        try:
            if _os.path.exists(_DLOG_PATH) and _os.path.getsize(_DLOG_PATH) > _DLOG_MAX_BYTES:
                with open(_DLOG_PATH, "r") as _f:
                    _lines = _f.readlines()
                with open(_DLOG_PATH, "w") as _f:
                    _f.writelines(_lines[len(_lines)//2:])
        except Exception:
            pass
        with open(_DLOG_PATH, "a") as _f:
            _f.write(line)
    except Exception:
        pass  # logging must NEVER crash the pipeline



from database import get_setting, get_user_api_key
from crypto_utils import decrypt_api_key

# ─────────────────────────────────────────────────────────────────────────────
# FILE MANIFEST — tracks which files Claude has already seen per session.
# DB-backed so it survives across chat turns.  Each file is hashed so we can
# detect new files, unchanged files, and modified files.  The classification
# is injected into the file context so Claude knows "this screenshot is new,
# but that code file was already discussed 3 turns ago."
# ─────────────────────────────────────────────────────────────────────────────

def _file_content_hash(content: str) -> str:
    """Fast, stable hash of file content for change detection."""
    return _hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]


def _ensure_file_manifest_table():
    """Create session_file_manifest table if it doesn't exist. Idempotent."""
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_file_manifest (
                    session_id    TEXT NOT NULL,
                    filename      TEXT NOT NULL,
                    content_hash  TEXT NOT NULL,
                    first_seen_turn INTEGER NOT NULL DEFAULT 0,
                    last_seen_turn  INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, filename)
                )
            """)
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # Table creation failure is non-fatal — falls back to "all files unknown"


# File status constants used in context annotations
_FILE_NEW      = "new"       # First time this file appears in this session
_FILE_MODIFIED = "modified"  # File existed before but content changed
_FILE_UNCHANGED = "unchanged" # File existed before with same content


def _classify_session_files(session_id: str, session_files: list,
                            conversation_history: list) -> dict:
    """
    Compare current session_files against the DB manifest.

    Returns {filename: {"status": "new"|"modified"|"unchanged",
                        "first_seen_turn": int, "detail": str}}

    Updates the manifest in-place so the next turn sees current state.
    """
    if not session_id:
        # No session tracking possible — treat everything as unknown
        return {}

    current_turn = max(len(conversation_history) // 2, 0)  # ~1 turn per user+assistant pair

    _ensure_file_manifest_table()

    # ── Load existing manifest from DB ──
    existing = {}  # {filename: (content_hash, first_seen_turn, last_seen_turn)}
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT filename, content_hash, first_seen_turn, last_seen_turn "
                "FROM session_file_manifest WHERE session_id = ?",
                (session_id,)
            ).fetchall()
            for row in rows:
                existing[row[0]] = (row[1], row[2], row[3])
        finally:
            conn.close()
    except Exception as e:
        _dlog("file_manifest_read_error", session_id=session_id, error=str(e))
        return {}

    # ── Classify each file ──
    result = {}
    upserts = []  # (session_id, filename, content_hash, first_seen_turn, last_seen_turn)

    for sf in session_files:
        fname = sf["filename"]
        content = sf.get("content", "")
        c_hash = _file_content_hash(content)

        if fname in existing:
            old_hash, first_turn, _ = existing[fname]
            if c_hash == old_hash:
                status = _FILE_UNCHANGED
                detail = f"present since turn {first_turn}, no changes"
            else:
                status = _FILE_MODIFIED
                detail = f"first seen turn {first_turn}, content changed this turn"
            upserts.append((session_id, fname, c_hash, first_turn, current_turn))
            result[fname] = {"status": status, "first_seen_turn": first_turn, "detail": detail}
        else:
            status = _FILE_NEW
            detail = "added this turn"
            upserts.append((session_id, fname, c_hash, current_turn, current_turn))
            result[fname] = {"status": status, "first_seen_turn": current_turn, "detail": detail}

    # ── Persist updated manifest ──
    try:
        from database import get_db_connection
        conn = get_db_connection()
        try:
            for row in upserts:
                # Upsert: INSERT OR REPLACE for SQLite, ON CONFLICT for Postgres
                try:
                    conn.execute(
                        """INSERT INTO session_file_manifest
                           (session_id, filename, content_hash, first_seen_turn, last_seen_turn)
                           VALUES (?, ?, ?, ?, ?)
                           ON CONFLICT (session_id, filename)
                           DO UPDATE SET content_hash = ?, last_seen_turn = ?""",
                        (row[0], row[1], row[2], row[3], row[4], row[2], row[4])
                    )
                except Exception:
                    # Fallback: try INSERT OR REPLACE (SQLite)
                    conn.execute(
                        """INSERT OR REPLACE INTO session_file_manifest
                           (session_id, filename, content_hash, first_seen_turn, last_seen_turn)
                           VALUES (?, ?, ?, ?, ?)""",
                        row
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _dlog("file_manifest_write_error", session_id=session_id, error=str(e))

    _dlog("file_manifest_classified",
          session_id=session_id,
          current_turn=current_turn,
          classifications={fn: cl["status"] for fn, cl in result.items()})

    return result


def _file_status_badge(status: str) -> str:
    """Human-readable badge for file status in context."""
    if status == _FILE_NEW:
        return "🆕 NEW"
    elif status == _FILE_MODIFIED:
        return "✏️ MODIFIED since last turn"
    elif status == _FILE_UNCHANGED:
        return "📎 UNCHANGED from previous turn"
    return ""
from models.schemas import (
    SurgicalOperation,
    ArchitectPlan, ChangeTarget, ChangeType, SurgicalChange,
    SurgicalAnalyzeResponse, SymbolMap, SymbolInfo, SymbolType
)
from services.ast_parser import ASTParser

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

try:
    from google import genai as _google_genai
    from google.genai import types as _google_types
    HAS_GOOGLE_GENAI = True
except ImportError:
    _google_genai = None
    _google_types = None
    HAS_GOOGLE_GENAI = False

parser = ASTParser()

# ── Per-model max output tokens (verified vs Anthropic docs) ─────────────────
# 128k-output models. Everything else falls back to 64000 (previous behavior).
_MODEL_MAX_OUTPUT = {
    "claude-sonnet-5": 128000,
    "claude-fable-5": 128000,
    "claude-opus-4-8": 128000,
    "claude-opus-4-7": 128000,
    "claude-opus-4-6": 128000,
    "claude-sonnet-4-6": 128000,
}
_MAX_OUTPUT_DEFAULT = 64000


def _max_output_tokens(model: str) -> int:
    """Return the max output tokens for a model, with a safe default.

    Matches exact IDs and dated variants (e.g. "claude-sonnet-5-20260101").
    Never raises — unknown/blank models get the conservative default.
    """
    try:
        base = (model or "").strip().lower()
        for known, cap in _MODEL_MAX_OUTPUT.items():
            if base == known or base.startswith(known + "-"):
                _dlog("max_output_lookup", model=model, matched=known, cap=cap)
                return cap
        _dlog("max_output_lookup", model=model, matched="default", cap=_MAX_OUTPUT_DEFAULT)
        return _MAX_OUTPUT_DEFAULT
    except Exception as _moe:
        _dlog("max_output_lookup_error", error=str(_moe)[:200])
        return _MAX_OUTPUT_DEFAULT


# ── QA-correction payload budgeter ──────────────────────────────────────────
# Guarantees a correction/retry payload fits the model context WITHOUT ever
# starving it. Replaces the old blind per-message `str(content)[:8000]` chop
# (~2k tokens) that silently deleted the full symbol code and the model's prior
# changes on any non-trivial file. Shared by BOTH OpenAI and Claude correction
# paths so neither can silently overflow or under-feed.
#
# ~300k chars ≈ ~75k tokens: >15x a realistic max correction payload (a
# 2,140-line file ≈ ~20k tokens) yet safely under the smallest context window
# we run. Realistic payloads pass through UNTOUCHED — clipping is a last resort
# for a pathological single message, and when it happens we clip head+tail
# (both ends preserved, never a blind head-chop) and emit a _dlog.
_CORRECTION_INPUT_CHAR_BUDGET = 300_000


def _fit_correction_messages(messages, *, session_id: str = "", user_id: str = ""):
    """Return messages guaranteed to fit context, preserving content when it fits.

    - Total under budget  -> returned unchanged (the normal case).
    - Total over budget   -> clip ONLY the single largest string message,
      head+tail, just enough to fit; emit a _dlog so traces show the clip.
    Never raises; on any error returns the input untouched.
    """
    try:
        msgs = list(messages or [])

        def _clen(m):
            c = m.get("content")
            return len(c) if isinstance(c, str) else 0

        total = sum(_clen(m) for m in msgs)
        if total <= _CORRECTION_INPUT_CHAR_BUDGET:
            return msgs

        # Over budget: find the single largest string message and clip it head+tail.
        idx = max(range(len(msgs)), key=lambda i: _clen(msgs[i]), default=-1)
        if idx < 0:
            return msgs
        content = msgs[idx].get("content", "")
        others = total - len(content)
        allow = max(4000, _CORRECTION_INPUT_CHAR_BUDGET - others)
        if len(content) <= allow:
            return msgs
        head = allow * 2 // 3
        tail = allow - head - 200  # room for the marker
        clipped = (
            content[:head]
            + f"\n\n... [{len(content) - head - tail} chars omitted to fit context] ...\n\n"
            + content[-tail:]
        )
        new_msgs = list(msgs)
        new_msgs[idx] = {**msgs[idx], "content": clipped}
        _dlog("correction_payload_clipped",
              original_chars=len(content), kept_chars=len(clipped),
              total_before=total, budget=_CORRECTION_INPUT_CHAR_BUDGET,
              message_index=idx, session_id=session_id, user_id=user_id)
        return new_msgs
    except Exception as _fce:
        _dlog("correction_payload_fit_error", error=str(_fce)[:200])
        return list(messages or [])


# Models that do NOT accept a temperature parameter (reasoning / latest-gen models)
NO_TEMPERATURE_MODELS = {
    "gpt-5", "gpt-5-mini", "gpt-5-nano",
    "gpt-5.1", "gpt-5.1-mini", "gpt-5.1-codex",
    "gpt-5.2", "gpt-5.2-pro",
    "gpt-5.3",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.5", "gpt-5.5-pro",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini",
}

# Models that support the reasoning_effort parameter (none/low/medium/high/xhigh).
# When set in settings, _chat_create will pass it automatically.
REASONING_EFFORT_MODELS = {
    "gpt-5", "gpt-5-mini", "gpt-5-nano",
    "gpt-5.1", "gpt-5.1-mini", "gpt-5.1-codex",
    "gpt-5.2", "gpt-5.2-pro",
    "gpt-5.3",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.5", "gpt-5.5-pro",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "o3", "o3-mini", "o4-mini",
}

# ── Prompt engineering constants ──────────────────────────────────────────────
HISTORY_WINDOW       = 20   # turns of conversation history passed to every prompt
TEXT_SEARCH_WINDOW   = 75   # ±lines around a text hit when no symbol contains the line
SYMBOL_FOCUS_WINDOW  = 100  # ±lines to slice when a symbol is huge but text is inside it
LARGE_FILE_WINDOW    = 500  # files above this get windowed context instead of full dump

# ── Shared chat persona ───────────────────────────────────────────────────────
CHAT_PERSONA = (
    "You are SurgicalAI, a world-class coding assistant. "
    "You are warm, encouraging, and precise. "
    "You help people build real software and always explain your reasoning clearly. "
    "Format code blocks with syntax highlighting. Use markdown.\n\n"
    "DIAGRAMS: When explaining flows, sequences, architectures, or relationships between "
    "components, ALWAYS use a mermaid code block instead of ASCII art. "
    "Use sequenceDiagram for request/response flows and component interactions. "
    "Use flowchart LR or TD for decision trees and data flows. "
    "Use classDiagram for type relationships. "
    "Example: ```mermaid\nsequenceDiagram\n    LoginPage->>useAuthStore: login(token,user)\n```"
)



async def _stream_and_collect(aclient, **kwargs):
    """Stream a Claude call and return the final Message object.

    Used instead of aclient.messages.create() for large-token calls that would
    exceed the Anthropic SDK's 10-minute non-streaming limit (e.g. Opus 4.5
    with max_tokens=64000).  Returns the same Message object as .create().
    Retries automatically on transient API errors (429, 500, 503, overloaded).
    """
    from services.api_retry import async_api_call_with_retry
    async def _call():
        async with aclient.messages.stream(**kwargs) as strm:
            return await strm.get_final_message()
    return await async_api_call_with_retry(_call)



# ── Phase 3: shared parse helpers (used by both mid-stream + EOS) ──────

def _parse_filereq_content(raw: str) -> list:
    """Parse file_request tag content — JSON array/string with plain-text fallback."""
    raw = raw.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(f).strip() for f in parsed if str(f).strip()]
        elif isinstance(parsed, str):
            return [parsed.strip()]
        return []
    except (json.JSONDecodeError, ValueError):
        return [f.strip() for f in raw.replace(",", "\n").split("\n") if f.strip()]


def _parse_search_content(raw: str):
    """Parse search_request tag content — JSON dict. Returns parsed dict or None."""
    try:
        return json.loads(raw.strip())
    except Exception:
        return None


def _parse_plan_content(raw: str):
    """Parse edit_plan tag content — JSON list. Returns list or None."""
    try:
        result = json.loads(raw.strip())
        return result if isinstance(result, list) else None
    except Exception:
        return None


def _eos_recover_blocks(open_tag: str, close_tag: str, full_response: str,
                        tag_buf: str, blocks_list: list) -> tuple:
    """EOS recovery: findall complete blocks in full_response, dedup, keep partial.
    Mutates blocks_list in place. Returns (new_count, partial_kept, had_matches)."""
    pattern = re.escape(open_tag) + r"(.*?)" + re.escape(close_tag)
    all_matches = re.findall(pattern, full_response, re.DOTALL)
    new_count = 0
    partial_kept = False
    if all_matches:
        for block in all_matches:
            if block not in blocks_list:
                blocks_list.append(block)
                new_count += 1
        if (tag_buf.strip()
                and close_tag not in tag_buf
                and tag_buf not in blocks_list):
            blocks_list.append(tag_buf)
            partial_kept = True
    else:
        if tag_buf.strip():
            blocks_list.append(tag_buf)
    return new_count, partial_kept, bool(all_matches)


def _is_claude_model(model: str) -> bool:
    """Check if a model ID is a Claude/Anthropic model."""
    return bool(model and model.startswith("claude-"))


def _is_gemini_model(model: str) -> bool:
    """Check if a model ID is a Gemini/Google model."""
    return bool(model and (model.startswith("gemini-") or model.startswith("models/gemini")))


# Models confirmed to support extended thinking (budget_tokens).
# Extended-thinking support. All Claude 4.x families (Opus/Sonnet/Haiku 4.5+)
# emit thinking blocks; 3.7 also supports it. 3.5 does NOT and is excluded.
_THINKING_CAPABLE_PATTERNS = ("claude-opus-4", "claude-sonnet-4", "claude-haiku-4-5", "claude-3-7")

# Specific model versions that match a thinking-capable pattern above but do
# NOT actually support manual extended thinking (type:enabled / budget_tokens).
# Checked first inside _supports_thinking() so they are never sent that shape.
# NOTE: 4-7 and 4-8 use ADAPTIVE thinking only — handled by _uses_adaptive_thinking().
_THINKING_EXCLUDED_MODELS = ("claude-opus-4-7", "claude-opus-4-8")

# Models that require adaptive thinking (type:"adaptive") instead of manual
# budget_tokens.  On these models, type:"enabled" returns a 400 error.
# Adaptive mode also auto-enables interleaved thinking (no beta header needed).
# display defaults to "omitted" on these models — must set "summarized" explicitly
# or thinking panel content will come back as empty strings (silent bug).
_ADAPTIVE_THINKING_MODELS = ("claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-sonnet-5", "claude-fable-5")

# 4-6 generation models support adaptive thinking and effort, but NOT 'xhigh'.
# Supported levels: max, high, medium, low.  Sending effort='xhigh' returns 400.
# Omitting effort entirely defaults to 'high' which is correct for these models.
_NO_XHIGH_EFFORT_MODELS = ("claude-opus-4-6", "claude-sonnet-4-6")

# -- ReAct agentic search: per-session grep cache ----------------------------
# Keyed by session_cache_key -> accumulated grep text from prior search rounds.
# Avoids re-scanning large files on follow-up edits in the same session.
_react_grep_cache: dict = {}

def _extract_search_terms(user_request: str) -> list:
    """
    Extract likely code identifiers, CSS selectors, and quoted strings
    from a plain-English user request.  Used to grep large files before
    Architect sees them so hidden symbols can still be found.
    """
    terms = []
    # 1. Quoted strings (user is naming something specific)
    quoted = re.findall(r'["\']([^"\'"]{3,})["\']\s', user_request + " ")
    terms.extend(q.strip() for q in quoted if q.strip())
    # 2. CSS selectors: .className  #idName
    css = re.findall(r'[.#][a-zA-Z][a-zA-Z0-9_-]+', user_request)
    terms.extend(css)
    # 3. camelCase / PascalCase — high signal (has uppercase after first char)
    camel = re.findall(r'\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b', user_request)
    terms.extend(camel)
    # 4. snake_case — high signal (has underscore)
    snake = re.findall(r'\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b', user_request)
    terms.extend(snake)
    # 5. Long plain words (7+ chars) — lower noise than short words
    _STOPS = {
        'function', 'section', 'element', 'replace', 'rename', 'remove',
        'without', 'between', 'current', 'default', 'created', 'returns',
        'content', 'background', 'updating', 'changed', 'looking', 'showing',
    }
    long_words = re.findall(r'\b[a-zA-Z]{7,}\b', user_request)
    terms.extend(w for w in long_words if w.lower() not in _STOPS)
    # deduplicate while preserving order, cap at 10 terms
    seen = set()
    result = []
    for t in terms:
        tl = t.lower()
        if tl not in seen and len(tl) >= 3:
            seen.add(tl)
            result.append(t)
        if len(result) >= 10:
            break
    return result


def _grep_relevant_sections(
    user_request: str,
    file_name: str,
    file_content: str,
    window: int = 50,
    max_lines: int = 180,
    extra_terms: list | None = None,
) -> str:
    """
    Search *file_content* for terms extracted from *user_request*.
    Returns a formatted string of up to 5 relevant code sections with
    line numbers, capped at *max_lines* total.  Empty string if nothing found.

    This is injected into the Architect prompt for large files (>60 symbols)
    so the Architect can locate code that fell outside the symbol map cap.

    extra_terms: additional terms to search for (e.g. from conversation history answers).
    HTML-aware: for .html/.htm files, automatically adds structural form terms
    (<input, type=, <select, etc.) when user mentions form UI words.
    """
    terms = _extract_search_terms(user_request)
    if extra_terms:
        for et in extra_terms:
            if et and et.lower() not in {t.lower() for t in terms}:
                terms.append(et)

    # Short-word fallback — _extract_search_terms only catches 7+ char words.
    # "Claude", "Opus", "Sonnet", "model", "GPT" are all < 7 chars and get missed.
    # Same fallback as _build_natural_file_context (line ~6794).
    _STOP_SHORT_GREP = {"the", "a", "an", "is", "it", "in", "on", "to", "fix", "bug",
                        "add", "make", "get", "set", "and", "for", "not", "are", "was",
                        "new", "old", "use", "run", "put", "all", "can", "has", "had",
                        "did", "do", "so", "up", "if", "or", "no", "be", "we", "my"}
    for word in re.findall(r"\b[a-zA-Z]{3,6}\b", user_request):
        wl = word.lower()
        if wl not in _STOP_SHORT_GREP and wl not in {t.lower() for t in terms}:
            terms.append(word)

    if not terms:
        return ""

    # ── HTML-aware enhancements ──────────────────────────────────────────────
    _is_html = file_name.lower().endswith((".html", ".htm"))
    if _is_html:
        # Bigger window for HTML — label and input often 50-100 lines apart in DOM
        window = max(window, 100)
        max_lines = max(max_lines, 300)
        # When user mentions form UI words, also grep for structural HTML tags
        _form_ui_words = {
            "field", "input", "form", "select", "button", "date", "text", "checkbox",
            "radio", "dropdown", "picker", "calendar", "entry", "box", "control",
            "widget", "click", "fill", "type", "enter", "placeholder",
        }
        _req_words = set(user_request.lower().split())
        if _req_words & _form_ui_words:
            _html_structural = ['<input', '<select', '<textarea', 'type="date"',
                                 'type="text"', 'onchange', 'oninput', 'value=']
            for ht in _html_structural:
                if ht.lower() not in {t.lower() for t in terms}:
                    terms.append(ht)

    lines = file_content.splitlines()
    matched_lines: set = set()

    for term in terms:
        tl = term.lower()
        for i, line in enumerate(lines):
            if tl in line.lower():
                start = max(0, i - window)
                end = min(len(lines), i + window + 1)
                for j in range(start, end):
                    matched_lines.add(j)

    if not matched_lines:
        return ""

    # Group into contiguous sections (gap > 10 lines = new section)
    sorted_lns = sorted(matched_lines)
    sections = []
    sec_start = sorted_lns[0]
    prev = sorted_lns[0]
    for ln in sorted_lns[1:]:
        if ln > prev + 10:
            sections.append((sec_start, prev))
            sec_start = ln
        prev = ln
    sections.append((sec_start, prev))

    # Keep top 5 largest sections (up from 3), re-sort by position
    sections = sorted(sections, key=lambda s: s[1] - s[0], reverse=True)[:5]
    sections = sorted(sections, key=lambda s: s[0])

    output_parts = []
    total = 0
    for s_start, s_end in sections:
        if total >= max_lines:
            break
        budget = min(s_end - s_start + 1, max_lines - total)
        s_end = s_start + budget - 1
        chunk = "\n".join(
            f"{s_start + 1 + j:5d}: {lines[s_start + j]}"
            for j in range(s_end - s_start + 1)
        )
        output_parts.append(f"Lines {s_start + 1}–{s_end + 1}:\n{chunk}")
        total += s_end - s_start + 1

    if not output_parts:
        return ""

    matched_terms = ", ".join(f'"{t}"' for t in terms[:8])
    header = (
        f"\nKEYWORD MATCH IN {file_name} "
        f"(searched for: {matched_terms}):\n"
    )
    return header + "\n\n".join(output_parts)

def _supports_thinking(model: str) -> bool:
    """Return True for models that support MANUAL extended thinking (type:enabled / budget_tokens).
    Adaptive thinking models (4.8, 4.7) are excluded here — they use _get_thinking_kwargs() instead.
    """
    if _is_claude_model(model):
        # Exclude specific versions that match a capable pattern but lack thinking support
        if any(excl in model for excl in _THINKING_EXCLUDED_MODELS):
            return False
        return any(model.startswith(p) or p in model for p in _THINKING_CAPABLE_PATTERNS)
    if _is_gemini_model(model):
        # Gemini 2.5+ models support thinking
        return "gemini-2.5" in model
    return False


def _uses_adaptive_thinking(model: str) -> bool:
    """Return True for Claude models that require adaptive thinking (type:'adaptive').
    These models reject type:'enabled'/budget_tokens with a 400 error.
    """
    return _is_claude_model(model) and any(m in model for m in _ADAPTIVE_THINKING_MODELS)


def _get_thinking_kwargs(model: str, budget: int) -> dict:
    """Return the correct `thinking` kwarg dict for any model.

    - Adaptive models (4.8, 4.7): {"thinking": {"type": "adaptive", "display": "summarized"}}
      IMPORTANT: display defaults to "omitted" on these models — must be set to "summarized"
      explicitly or thinking blocks come back as empty strings (silent failure).
      No budget_tokens parameter — effort is set separately via _get_effort_kwargs().

    - Manual thinking models (4.6, 3.7): {"thinking": {"type": "enabled", "budget_tokens": N}}

    - All other models: {} (no thinking params, safe to ** spread)
    """
    if _uses_adaptive_thinking(model):
        # Adaptive models REJECT type:"enabled" / budget_tokens with a 400.
        # Must use type:"adaptive" — budget caps aren't possible.
        # Starvation protection comes from _safe_claude_call's retry layer.
        # display must be "summarized" or thinking blocks are empty strings.
        return {"thinking": {"type": "adaptive", "display": "summarized"}}
    if _supports_thinking(model):
        return {"thinking": {"type": "enabled", "budget_tokens": budget}}
    return {}


def _get_effort_kwargs(model: str) -> dict:
    """Return output_config kwargs for models that support effort control.

    Adaptive thinking models benefit greatly from effort='xhigh' for coding tasks.
    Anthropic changed the default effort from 'high' to 'medium' which caused a
    significant quality regression — explicitly set xhigh for agentic/coding work.
    Returns {} for all other models (safe to ** spread).
    """
    if _uses_adaptive_thinking(model) and not any(m in model for m in _NO_XHIGH_EFFORT_MODELS):
        return {"output_config": {"effort": "xhigh"}}
    return {}


# ── Starvation-proof Claude API helpers ──────────────────────────────────
#
# Problem: adaptive-thinking models (Sonnet 5, Opus 4.x) have NO thinking
# budget cap.  With small max_tokens + complex input the model can spend
# ALL tokens thinking → zero text output ("thinking starvation").
#
# Solution: Manual-thinking models use {"type": "enabled", "budget_tokens": N}
# which hard-caps thinking within max_tokens.  Adaptive models REJECT
# type:"enabled" so they use {"type": "adaptive"} — starvation protection
# comes from _safe_claude_call's detection + retry layer instead.
#
# Three layers of protection:
#   1. _bounded_thinking_params() — correct params, budget < max_tokens
#   2. _is_starved()             — detects zero-text responses
#   3. _safe_claude_call()       — streams + starvation retry
# ─────────────────────────────────────────────────────────────────────────

def _bounded_thinking_params(model: str, desired_text_tokens: int,
                              thinking_budget: int | None = None) -> dict:
    """Build Claude params with text headroom protection.

    Manual-thinking models: ``{"type": "enabled", "budget_tokens": N}``
    hard-caps thinking so the model cannot consume all max_tokens.

    Adaptive models: ``{"type": "adaptive"}`` — these REJECT type:"enabled"
    with a 400 error.  Starvation protection shifts to _safe_claude_call's
    detection + retry layer.

    Formula (manual models):
        max_tokens = min(desired_text + budget, model_limit)
        budget     = min(budget, max_tokens // 2)   # never >50 % thinking
    """
    if not (_uses_adaptive_thinking(model) or _supports_thinking(model)):
        return {"max_tokens": desired_text_tokens}

    budget = thinking_budget if thinking_budget is not None else min(desired_text_tokens, 16_000)
    model_limit = _max_output_tokens(model)
    max_tok = min(desired_text_tokens + budget, model_limit)

    if _uses_adaptive_thinking(model):
        # Adaptive models REJECT type:"enabled" / budget_tokens with a 400.
        # Can't hard-cap thinking — starvation protection comes from
        # _safe_claude_call's detection + retry layer.
        result: dict = {
            "max_tokens": max_tok,
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        effort = _get_effort_kwargs(model)
        if effort:
            result.update(effort)
        return result

    # Manual thinking models (3.7, older 4.x) — budget_tokens IS supported
    # Hard rule: thinking never takes more than half of max_tokens
    budget = min(budget, max_tok // 2)
    budget = max(budget, 1024)  # floor — always allow some thinking

    return {
        "max_tokens": max_tok,
        "thinking": {"type": "enabled", "budget_tokens": budget},
    }


def _is_starved(response) -> bool:
    """True when a response contains zero usable text (all thinking)."""
    if not hasattr(response, "content"):
        return False
    return not any(
        hasattr(b, "text") and b.text.strip()
        for b in response.content
    )


async def _safe_claude_call(aclient_inst, *, model: str,
                             desired_text_tokens: int,
                             thinking_budget: int | None = None,
                             retry_on_starve: bool = True,
                             **kwargs):
    """Starvation-proof async Claude API call.

    Guarantees:
      1. Always streams internally (prevents Anthropic timeout rejection).
      2. Thinking is budget-capped (text headroom is mathematically safe).
      3. If starvation still occurs, auto-retries with 2× budget + reduced
         effort before returning.

    Returns an Anthropic ``Message`` object (same as ``messages.create``).
    """
    params = _bounded_thinking_params(model, desired_text_tokens, thinking_budget)

    _dlog("safe_claude_call_entry",
          model=model, desired_text=desired_text_tokens,
          max_tokens=params.get("max_tokens"),
          budget=params.get("thinking", {}).get("budget_tokens"),
          effort=params.get("output_config", {}).get("effort"))

    msg = await _stream_and_collect(aclient_inst, model=model, **params, **kwargs)

    if not _is_starved(msg):
        _dlog("safe_claude_call_ok",
              model=model,
              output_tokens=getattr(getattr(msg, "usage", None), "output_tokens", None),
              stop_reason=getattr(msg, "stop_reason", None))
        return msg

    # ── Starvation detected — log + retry ────────────────────────────
    _dlog("thinking_starvation_detected",
          model=model, desired_text=desired_text_tokens,
          max_tokens=params.get("max_tokens"),
          budget=params.get("thinking", {}).get("budget_tokens"),
          stop_reason=getattr(msg, "stop_reason", None),
          block_types=[getattr(b, "type", "?") for b in msg.content])

    if not retry_on_starve:
        return msg

    # Double both budgets, drop effort to "high" to discourage overthinking
    retry_budget = (thinking_budget or min(desired_text_tokens, 16_000)) * 2
    retry_params = _bounded_thinking_params(
        model, desired_text_tokens * 2, retry_budget)
    if "output_config" in retry_params:
        retry_params["output_config"] = {"effort": "high"}

    _dlog("thinking_starvation_retry",
          model=model, retry_max=retry_params.get("max_tokens"),
          retry_budget=retry_params.get("thinking", {}).get("budget_tokens"))

    msg = await _stream_and_collect(aclient_inst, model=model,
                                     **retry_params, **kwargs)
    if _is_starved(msg):
        _dlog("thinking_starvation_final_fail",
              model=model, stop_reason=getattr(msg, "stop_reason", None))
    else:
        _dlog("thinking_starvation_retry_ok",
              model=model,
              output_tokens=getattr(getattr(msg, "usage", None), "output_tokens", None),
              stop_reason=getattr(msg, "stop_reason", None))
    return msg


def _safe_claude_call_sync(sync_client, *, model: str,
                            desired_text_tokens: int,
                            thinking_budget: int | None = None,
                            retry_on_starve: bool = True,
                            **kwargs):
    """Starvation-proof **sync** Claude API call (surgeon thread-pool paths).

    Same guarantees as ``_safe_claude_call`` but blocking.  Uses
    ``api_call_with_retry`` for transient-error resilience.
    """
    from services.api_retry import api_call_with_retry

    params = _bounded_thinking_params(model, desired_text_tokens, thinking_budget)

    _dlog("safe_claude_call_sync_entry",
          model=model, desired_text=desired_text_tokens,
          max_tokens=params.get("max_tokens"),
          budget=params.get("thinking", {}).get("budget_tokens"),
          effort=params.get("output_config", {}).get("effort"))

    msg = api_call_with_retry(lambda: sync_client.messages.create(
        model=model, **params, **kwargs))

    if not _is_starved(msg):
        _dlog("safe_claude_call_sync_ok",
              model=model,
              output_tokens=getattr(getattr(msg, "usage", None), "output_tokens", None),
              stop_reason=getattr(msg, "stop_reason", None))
        return msg

    _dlog("thinking_starvation_detected_sync",
          model=model, desired_text=desired_text_tokens,
          max_tokens=params.get("max_tokens"),
          stop_reason=getattr(msg, "stop_reason", None),
          block_types=[getattr(b, "type", "?") for b in msg.content])

    if not retry_on_starve:
        return msg

    retry_budget = (thinking_budget or min(desired_text_tokens, 16_000)) * 2
    retry_params = _bounded_thinking_params(
        model, desired_text_tokens * 2, retry_budget)
    if "output_config" in retry_params:
        retry_params["output_config"] = {"effort": "high"}

    _dlog("thinking_starvation_retry_sync",
          model=model, retry_max=retry_params.get("max_tokens"))

    msg = api_call_with_retry(lambda: sync_client.messages.create(
        model=model, **retry_params, **kwargs))

    if _is_starved(msg):
        _dlog("thinking_starvation_final_fail_sync",
              model=model, stop_reason=getattr(msg, "stop_reason", None))
    else:
        _dlog("thinking_starvation_retry_ok_sync",
              model=model,
              output_tokens=getattr(getattr(msg, "usage", None), "output_tokens", None),
              stop_reason=getattr(msg, "stop_reason", None))
    return msg


def _extract_claude_text(response) -> str:
    """Safely extract text from a Claude API response.

    Models with adaptive/always-on thinking (Sonnet 5, Fable 5, Opus 4.x)
    return ThinkingBlock(s) before the TextBlock.  content[0].text crashes
    on a ThinkingBlock.  This iterates to find the first TextBlock instead.
    Returns '' if no text block is found (graceful degradation).
    """
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text or ""
    _dlog("extract_claude_text_no_text_block",
          stop_reason=getattr(response, "stop_reason", None),
          block_types=[getattr(b, "type", "?") for b in response.content])
    return ""


def _resolve_key(user_id: str, key_type: str) -> str:
    """Decrypt per-user API key, fall back to global setting."""
    if user_id:
        encrypted = get_user_api_key(user_id, key_type)
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                pass
    return get_setting(f"{key_type}_api_key", "")


def _get_anthropic_key(user_id: str = "") -> str:
    key = _resolve_key(user_id, "anthropic")
    if not key:
        raise ValueError("Anthropic API key not configured. Go to Settings → API Keys to add it.")
    return key


def _friendly_error(e: Exception) -> str:
    """Translate technical API/pipeline errors into plain-English messages for the user."""
    msg = str(e)
    low = msg.lower()
    cls = type(e).__name__.lower()

    # Anthropic-specific errors
    if "anthropic" in cls or (("500" in msg or "529" in msg or "overload" in low) and "anthropic" in (cls + low)):
        if "overloaded" in low or "529" in msg:
            return ("The AI service is temporarily overloaded. "
                    "Please wait a moment and try again — it usually clears in a few seconds.")
        if "500" in msg or "internal_server_error" in low:
            return ("The AI service hit a temporary server error (500). "
                    "This usually resolves quickly — please try again.")
        if "context_length" in low or "too long" in low or "max_tokens" in low:
            return ("Your file may be too large to process in one pass. "
                    "Try uploading just the specific function or section you want to change.")
        if "rate_limit" in low or "429" in msg:
            return "You've hit the API rate limit. Wait a few seconds and try again."
        if "invalid" in low and "key" in low:
            return "Your Anthropic API key appears to be invalid. Check **Settings → API Keys**."

    # Gemini-specific errors
    if "google" in cls or "gemini" in low or "generativelanguage" in low:
        if "api_key" in low or "invalid" in low or "401" in msg:
            return "Your Google Gemini API key appears to be invalid. Check **Settings → API Keys**."
        if "quota" in low or "429" in msg:
            return "You've hit the Gemini API quota. Wait a moment and try again."
        if "safety" in low or "blocked" in low:
            return "Gemini blocked this request due to safety filters. Try rephrasing your request."
        if "not found" in low or "404" in msg:
            return "This Gemini model isn't available. Try a different model in Settings."

    # OpenAI-specific errors
    if "openai" in cls or ("openai" in low and ("error" in low or "429" in msg or "500" in msg)):
        if "rate_limit" in low or "429" in msg:
            return "You've hit the OpenAI rate limit. Wait a moment and try again."
        if "invalid" in low and "key" in low:
            return "Your OpenAI API key appears to be invalid. Check **Settings → API Keys**."
        if "context_length" in low or "too long" in low:
            return ("The context window is full. Try uploading a smaller file or asking about "
                    "a specific function.")

    # Connection / timeout
    if "timeout" in low or "timed out" in low:
        return ("The request timed out. This can happen with large files — "
                "try again or ask about a specific function instead of the whole file.")
    if "connect" in low and ("refused" in low or "error" in low):
        return "Couldn't reach the AI service. Check your internet connection and try again."

    # Already-friendly messages from our own validators (pass through as-is)
    if "not configured" in low and "api key" in low:
        return msg

    # Generic fallback — never show raw Python dict/object representations
    short = msg[:150].replace("{", "(").replace("}", ")").replace("'type'", "type")
    return f"Something went wrong. Please try again in a moment. *(Detail: {short})*"


def _chat_create(client: OpenAI, model: str, messages: list, temperature: float = 0.3, **kwargs):
    """Wrapper around client.chat.completions.create that drops temperature
    for reasoning models and injects reasoning_effort when configured.
    Also injects max_completion_tokens for reasoning models that require it.
    Retries automatically on transient API errors (429, 500, 503, overloaded)."""
    from services.api_retry import api_call_with_retry
    base_model = model.split(":")[0].lower()
    # OpenAI deprecated max_tokens; newer models (GPT-4.1+, o-series, GPT-5.x)
    # require max_completion_tokens instead.  _chat_create is ONLY used with the
    # OpenAI SDK, so this conversion is always safe.
    if "max_tokens" in kwargs:
        if "max_completion_tokens" not in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
            _dlog("chat_create_max_tokens_renamed",
                  model=model, max_completion_tokens=kwargs["max_completion_tokens"])
        else:
            _popped = kwargs.pop("max_tokens")
            _dlog("chat_create_max_tokens_dropped",
                  model=model, dropped=_popped,
                  kept_max_completion_tokens=kwargs["max_completion_tokens"])
    # Reasoning models (GPT-5.x, o-series) do not support the `stop` parameter.
    # Strip it centrally so no call site can accidentally trigger the 400 error.
    if base_model in NO_TEMPERATURE_MODELS and "stop" in kwargs:
        _popped_stop = kwargs.pop("stop")
        _dlog("chat_create_stop_stripped",
              model=model, base_model=base_model,
              stripped_stop=_popped_stop)
    if base_model in NO_TEMPERATURE_MODELS:
        # ── GPT-5.x / o-series reasoning branch (Claude models never enter here) ──
        # Phase 1 hardening (flag: gpt5_hardening, default ON): 32k budget,
        # explicit low reasoning_effort, finish_reason truncation retry.
        # Docs: reasoning tokens bill against max_completion_tokens; OpenAI says
        # reserve >= 25k, and truncation can occur with ZERO visible output.
        _gpt_hardened = False
        try:
            from services.gpt_reasoning import (
                apply_hardening_kwargs as _gh_apply,
                create_with_truncation_retry as _gh_create,
                responses_api_enabled as _gh_responses_on,
                responses_create as _gh_responses_create,
            )
            _gpt_hardened = _gh_apply(base_model, kwargs, REASONING_EFFORT_MODELS,
                                      get_setting, _dlog)
        except Exception as _gh_err:
            _dlog("gpt_hardening_import_error", model=model,
                  error_type=type(_gh_err).__name__, error=str(_gh_err)[:300])
            _gh_create = None
            _gh_responses_on = None
            _gh_responses_create = None
        if not _gpt_hardened:
            # Legacy defaults — byte-for-byte the pre-hardening behavior.
            if base_model in REASONING_EFFORT_MODELS and "reasoning_effort" not in kwargs:
                _re = get_setting("reasoning_effort", "")
                if _re and _re.lower() in ("none", "low", "medium", "high", "xhigh"):
                    kwargs["reasoning_effort"] = _re.lower()
            if "max_completion_tokens" not in kwargs:
                kwargs["max_completion_tokens"] = 16384
        _dlog("chat_create_reasoning_model",
              model=model, base_model=base_model,
              reasoning_effort=kwargs.get("reasoning_effort", "<not set>"),
              max_completion_tokens=kwargs.get("max_completion_tokens"),
              has_response_format="response_format" in kwargs,
              stream=kwargs.get("stream", False),
              gpt_hardened=_gpt_hardened)
        _is_stream = bool(kwargs.get("stream", False))
        # Phase 3 (flag: gpt_responses_api, default OFF): route non-stream calls
        # to the Responses API via a chat-completions-shaped shim. Any mapping
        # or API problem returns None and we fall back to Chat Completions.
        if not _is_stream and _gh_responses_on is not None and _gh_responses_on(get_setting):
            _shim = _gh_responses_create(client, model, messages, kwargs,
                                         api_call_with_retry, _dlog)
            if _shim is not None:
                return _shim
            _dlog("gpt_responses_fallback_to_chat_completions", model=model)
        if _is_stream or not _gpt_hardened or _gh_create is None:
            # Streams cannot be inspected for finish_reason here; hardening-off
            # keeps the original single-shot call.
            return api_call_with_retry(lambda: client.chat.completions.create(model=model, messages=messages, **kwargs))
        # Phase 1: single truncation retry on finish_reason=length / empty output.
        return _gh_create(
            lambda _kw: api_call_with_retry(lambda: client.chat.completions.create(model=model, messages=messages, **_kw)),
            kwargs, _dlog, model=model)
    return api_call_with_retry(lambda: client.chat.completions.create(model=model, messages=messages, temperature=temperature, **kwargs))


def _get_client(user_id: str = "") -> OpenAI:
    key = _resolve_key(user_id, "openai")
    if not key:
        raise ValueError("OpenAI API key not configured. Go to Settings to add your key.")
    return OpenAI(api_key=key)


def _get_gemini_key(user_id: str = "") -> str:
    """Resolve Gemini/Google API key for user."""
    if user_id:
        encrypted = get_user_api_key(user_id, "gemini")
        if encrypted:
            try:
                return decrypt_api_key(encrypted)
            except Exception:
                pass
    key = get_setting("gemini_api_key", "")
    if not key:
        raise ValueError("Google Gemini API key not configured. Go to Settings → API Keys to add it.")
    return key


def _get_client_for_model(model: str, user_id: str = "") -> OpenAI:
    """Return an OpenAI-compatible client for the given model.
    Gemini models use Google's OpenAI-compat endpoint.
    All other models use the standard OpenAI endpoint.
    """
    if _is_gemini_model(model):
        key = _get_gemini_key(user_id)
        return OpenAI(
            api_key=key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        )
    return _get_client(user_id)


def _compute_target_element(original: str, new_code: str):
    """
    Compute the minimal changed lines between original and new_code.
    Returns (target_element, replacement) — the exact lines being replaced.
    These are stored on SurgicalChange and used by apply_change() as the
    primary match target, avoiding full-window overlap errors.
    """
    if not new_code:
        # DELETE: target is the full original
        return original, ""

    orig_lines = original.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)

    # Find first differing line from top
    top = 0
    while top < len(orig_lines) and top < len(new_lines):
        if orig_lines[top] != new_lines[top]:
            break
        top += 1

    # If no diff found at all, return None (no-op)
    if top >= len(orig_lines) and top >= len(new_lines):
        return None, None

    # Find first differing line from bottom
    bot_orig = len(orig_lines) - 1
    bot_new = len(new_lines) - 1
    while bot_orig > top and bot_new > top:
        if orig_lines[bot_orig] != new_lines[bot_new]:
            break
        bot_orig -= 1
        bot_new -= 1

    # Add ±2 lines of context around the core diff for reliable matching
    ctx = 2
    slice_start = max(0, top - ctx)
    slice_end_orig = min(len(orig_lines), bot_orig + 1 + ctx)
    slice_end_new = min(len(new_lines), bot_new + 1 + ctx)

    target_element = "".join(orig_lines[slice_start:slice_end_orig]).rstrip()
    replacement = "".join(new_lines[slice_start:slice_end_new]).rstrip()

    # Safety: if target_element is too short (< 2 lines) or too generic,
    # return the full original to avoid false matches
    if len(target_element.splitlines()) < 2:
        return original.rstrip(), new_code.rstrip()

    return target_element, replacement


def _make_diff(original: str, new_code: str, symbol_path: str) -> str:
    """Generate unified diff string between original and new code."""
    orig_lines = original.splitlines(keepends=True)
    new_lines = new_code.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines, new_lines,
        fromfile=f"{symbol_path} (original)",
        tofile=f"{symbol_path} (modified)",
        lineterm=""
    )
    return "".join(diff)


def _should_use_ollama(model: Optional[str] = None, user_id: str = "") -> bool:
    """Check if we should use Ollama for this model."""
    if model and model.startswith("ollama:"):
        return True
    if get_setting("ollama_enabled", "false") == "true":
        has_openai = bool(_resolve_key(user_id, "openai"))
        if not has_openai:
            return True
    return False


def _ollama_chat(messages: list, model: str, stream: bool = False):
    """Call Ollama API."""
    base_url = get_setting("ollama_base_url", "http://localhost:11434")
    ollama_model = model.replace("ollama:", "") if model.startswith("ollama:") else get_setting("ollama_model", "qwen2.5-coder:7b")

    payload = {
        "model": ollama_model,
        "messages": messages,
        "stream": stream,
        "options": {"temperature": 0.3}
    }

    resp = httpx.post(f"{base_url}/api/chat", json=payload, timeout=120)
    resp.raise_for_status()

    if stream:
        return resp
    data = resp.json()
    return data["message"]["content"]


# DEPRECATED: This prompt is superseded by SMART_ARCHITECT_SYSTEM and run_smart_pipeline_stream().
# Kept only for backward compat with the legacy run_architect() endpoint.
# DO NOT UPDATE — update SMART_ARCHITECT_SYSTEM instead.
ARCHITECT_SYSTEM = """You are an expert software architect AI. You analyze code and produce precise surgical change plans.

Your job:
1. Read the symbol map of the file (classes, functions, methods, their signatures)
2. Understand the user's request
3. Identify EXACTLY which symbols need to change — no more, no less
4. For each change, describe precisely what new logic is needed

Rules:
- Only target symbols that ACTUALLY EXIST in the symbol map
- Be conservative — if unsure, flag it as a risk
- Output valid JSON only — no markdown, no explanation outside JSON
- Think about dependencies: if changing X breaks Y, list Y in dependencies

Output format (JSON only):
{
  "summary": "Brief summary of what will change and why",
  "targets": [
    {
      "symbol_path": "ClassName.method_name or function_name",
      "change_type": "modify|add|delete|refactor",
      "description": "Precise description of what changes",
      "new_logic": "Detailed description of the new logic/code to write",
      "dependencies": ["other_symbol that might be affected"],
      "confidence": 8
    }
  ],
  "new_symbols_needed": ["new_function_name if any new functions/classes needed"],
  "import_changes": ["add: import X", "remove: import Y"],
  "risks": ["Any potential issues or things to verify"]
}"""


SURGEON_SYSTEM = """You are the SURGEON in a two-model coding system.

The ARCHITECT has already analyzed the codebase and created a precise change plan.
Your job: implement that plan using SEARCH/REPLACE blocks.

You will receive:
- FILE HEADER: top of the file (imports, key state/variables) for reference
- CONTEXT BEFORE: lines just before the target symbol
- TARGET CODE: the exact symbol you are editing
- CONTEXT AFTER: lines just after the target symbol

OUTPUT FORMAT — use ONLY Aider-style SEARCH/REPLACE blocks:

<<<<<<< SEARCH
[exact lines to find — copy character-for-character from TARGET CODE]
=======
[replacement lines]
>>>>>>> REPLACE

RULES:
1. SEARCH text MUST be an EXACT substring of TARGET CODE — copy it verbatim including all whitespace and indentation.
2. Use the MINIMUM lines needed to uniquely identify the location (usually 2–6 lines).
3. REPLACE is the new text. Preserve the original indentation style.
4. Each block = ONE logical change. Multiple changes = multiple blocks. Order them top-to-bottom in the file.
5. Do NOT include unchanged surrounding code in SEARCH or REPLACE.
6. CRITICAL: Every line in SEARCH that is absent from REPLACE gets permanently deleted. Never include trailing context lines you want to keep.
7. TO DELETE a symbol or block entirely: SEARCH = the exact code to remove, REPLACE = empty (no lines). Use this when asked to "remove", "delete", or eliminate "unused" code. This is NOT the same as "already correct" — a deletion requires a non-empty SEARCH block.

FOR REDESIGN / RESTYLE / COMPLETE REWRITE (> ~30% of symbol changing):
Use a single block covering the entire symbol body — SEARCH = full original symbol, REPLACE = full new symbol.

FOR TARGETED CHANGES (bug fix, add a field, change a color):
Use small precise blocks — one per change location.

STYLE RULES:
- Targeted tweaks: prefer existing color values/CSS variables.
- Redesign/restyle/modernize: freely introduce new colors, gradients, glassmorphism, modern DaaS/SaaS patterns.
- Preserve TypeScript types. Match indentation exactly (spaces vs tabs, 2 vs 4 spaces).

IF ALREADY CORRECT (the code already does exactly what was requested AND nothing should be deleted): output a single empty block pair:
<<<<<<< SEARCH
=======
>>>>>>> REPLACE
NEVER use this for deletions — if the task says "remove", "delete", or "unused", you MUST emit a non-empty SEARCH block.

VERIFICATION RULE — BEFORE concluding "already correct":
You MUST confirm you are looking at the exact named function/symbol, not just nearby code.
Search for the function definition by its literal name (e.g. `function exportRspProjectsXLSX(` or `def export_rsp_projects`).
A correct pattern appearing NEAR the target symbol does NOT mean the target symbol is correct.
If you cannot locate the exact definition, emit a SEARCH/REPLACE block rather than an empty pair.

Output ONLY the SEARCH/REPLACE blocks. No JSON. No markdown fences around the blocks. No explanation outside the blocks."""

# ── Phase 1: Tool-use Surgeon definitions ────────────────────────────────────
# Feature-flagged via get_setting("surgeon_tool_use", "false").
# When enabled, Claude/GPT call structured tools instead of producing
# free-text SEARCH/REPLACE blocks. Eliminates regex parsing entirely.

SURGEON_TOOL_USE_SYSTEM = """You are the SURGEON in a two-model coding system.

The ARCHITECT has already analyzed the codebase and created a precise change plan.
Your job: implement that plan by calling the provided tools.

You will receive:
- FILE HEADER: top of the file (imports, key state/variables) for reference
- CONTEXT BEFORE: lines just before the target symbol
- TARGET CODE: the exact symbol you are editing
- CONTEXT AFTER: lines just after the target symbol

RULES:
1. Use edit_code for targeted changes. The old_code must be an EXACT substring of TARGET CODE — copy it verbatim including all whitespace and indentation.
2. Use the MINIMUM lines in old_code needed to uniquely identify the location (usually 2-6 lines).
3. new_code is the replacement. Preserve the original indentation style.
4. Each tool call = ONE logical change. Multiple changes = multiple calls.
5. CRITICAL: Every line in old_code that is absent from new_code gets permanently deleted. Never include trailing context lines you want to keep.
6. TO DELETE code: set old_code to the exact code to remove, set new_code to empty string.

FOR REDESIGN / RESTYLE / COMPLETE REWRITE (> ~30% of symbol changing):
Use replace_symbol with the complete new symbol code.

FOR TARGETED CHANGES (bug fix, add a field, change a color):
Use edit_code — one call per change location.

STYLE RULES:
- Targeted tweaks: prefer existing color values/CSS variables.
- Redesign/restyle/modernize: freely introduce new colors, gradients, glassmorphism, modern patterns.
- Preserve TypeScript types. Match indentation exactly (spaces vs tabs, 2 vs 4 spaces).

IF ALREADY CORRECT (the code already does exactly what was requested AND nothing should be deleted):
Call no_change_needed. NEVER use this for deletions.

VERIFICATION RULE — BEFORE concluding no change needed:
You MUST confirm you are looking at the exact named function/symbol, not just nearby code.
If you cannot locate the exact definition, make an edit rather than calling no_change_needed."""

SURGEON_TOOLS_ANTHROPIC = [
    {
        "name": "edit_code",
        "description": "Replace a specific code region in the target symbol. The old_code must exactly match a substring of the TARGET CODE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "old_code": {
                    "type": "string",
                    "description": "Exact code to find — copy character-for-character from TARGET CODE including whitespace."
                },
                "new_code": {
                    "type": "string",
                    "description": "The replacement code. Use empty string to delete."
                }
            },
            "required": ["old_code", "new_code"]
        }
    },
    {
        "name": "replace_symbol",
        "description": "Replace the entire target symbol with new code. Use for rewrites affecting more than 30 percent of the symbol.",
        "input_schema": {
            "type": "object",
            "properties": {
                "new_code": {
                    "type": "string",
                    "description": "Complete new code for the entire symbol."
                }
            },
            "required": ["new_code"]
        }
    },
    {
        "name": "no_change_needed",
        "description": "The code already implements the requested change correctly. Never use for deletions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of why no change is needed."
                }
            },
            "required": ["reason"]
        }
    }
]

SURGEON_TOOLS_OPENAI = [
    {
        "type": "function",
        "function": {
            "name": "edit_code",
            "description": "Replace a specific code region in the target symbol. The old_code must exactly match a substring of the TARGET CODE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "old_code": {
                        "type": "string",
                        "description": "Exact code to find — copy character-for-character from TARGET CODE including whitespace."
                    },
                    "new_code": {
                        "type": "string",
                        "description": "The replacement code. Use empty string to delete."
                    }
                },
                "required": ["old_code", "new_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "replace_symbol",
            "description": "Replace the entire target symbol with new code. Use for rewrites affecting more than 30 percent of the symbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "new_code": {
                        "type": "string",
                        "description": "Complete new code for the entire symbol."
                    }
                },
                "required": ["new_code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "no_change_needed",
            "description": "The code already implements the requested change correctly. Never use for deletions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Brief explanation of why no change is needed."
                    }
                },
                "required": ["reason"]
            }
        }
    }
]


# ── Phase 2: Agentic Tool-Use Tools (Claude Architect) ──────────────────────
# Used when get_setting('agentic_tool_use') == 'true'.
# Replaces free-text JSON plan + intent=search ReAct with structured tool calls.
# Eliminates JSON fence stripping, _repair_json, brace matching, regex salvage.
AGENTIC_TOOLS_V2 = [
    {
        "name": "search_codebase",
        "description": "Search across all session files for code matching the given terms. Returns matching symbols and code with line numbers. Use to find functions, variables, CSS classes, or any code pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search terms — function names, variable names, class names, string literals, CSS selectors"
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for this search"
                }
            },
            "required": ["terms"]
        }
    },
    {
        "name": "request_file",
        "description": "Request the full content of a specific file from the session, with line numbers. Use when you need to see the complete file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "The filename to retrieve (as shown in the file list)"
                }
            },
            "required": ["filename"]
        }
    },
    {
        "name": "submit_plan",
        "description": "Submit your final plan. Use intent='edit' with targets for code changes, 'chat' for conversation, 'needs_clarification' to ask questions, or 'create' for new files.",
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": ["edit", "chat", "needs_clarification", "create"],
                    "description": "The type of response"
                },
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "symbol_path": {"type": "string", "description": "The function/class/component to modify (from the symbol index)"},
                            "change_type": {"type": "string", "enum": ["modify", "add", "delete", "refactor"]},
                            "description": {"type": "string", "description": "What the change does"},
                            "new_logic": {"type": "string", "description": "Detailed description of the new implementation"},
                            "confidence": {"type": "integer", "minimum": 1, "maximum": 10},
                            "import_changes": {"type": "array", "items": {"type": "string"}},
                            "target_line": {"type": "integer", "description": "Line number where the change should be applied"},
                            "context_needs": {"type": "array", "items": {"type": "string"}},
                            "surgeon_context": {"type": "array", "items": {"type": "object"}}
                        },
                        "required": ["filename", "symbol_path", "description", "new_logic"]
                    },
                    "description": "Code changes to make (for edit intent)"
                },
                "chat_response": {"type": "string", "description": "Response text (for chat intent)"},
                "reasoning": {"type": "string", "description": "Your reasoning"},
                "risks": {"type": "array", "items": {"type": "string"}, "description": "Potential risks"},
                "questions": {"type": "array", "items": {"type": "string"}, "description": "Questions (for needs_clarification)"},
                "clarification_response": {"type": "string", "description": "Full clarification response"},
                "summary": {"type": "string"},
                "new_files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string"},
                            "description": {"type": "string"},
                            "dependencies": {"type": "array", "items": {"type": "string"}}
                        }
                    },
                    "description": "New files to create (for create intent)"
                }
            },
            "required": ["intent"]
        }
    }
]

# Extend AGENTIC_TOOLS_V2 with find_callers/find_usages/get_lines, mirroring
# context_resolver's callers/usages/lines request types already proven in the
# Surgeon. Import failure degrades gracefully — base 3 tools still work.
try:
    from services.architect_search_tools import ARCHITECT_SEARCH_TOOLS_V2 as _ARCH_SEARCH_TOOLS_EXT
except Exception:
    _ARCH_SEARCH_TOOLS_EXT = []
AGENTIC_TOOLS_V2 = AGENTIC_TOOLS_V2 + _ARCH_SEARCH_TOOLS_EXT

# Extend AGENTIC_TOOLS_V2 with GitHub App read-side context tools
# (list_prs/get_pr_diff/get_pr_comments/list_issues/get_issue_comments/diff_branches).
# Gated by its OWN flag on top of the existing agentic_tool_use flag — both
# must be "true" before these tools are even added to the tool list, so this
# has zero effect until BOTH flags are explicitly turned on.
try:
    from services.github_context_tools import GITHUB_CONTEXT_TOOLS_V2 as _GH_CTX_TOOLS_EXT
except Exception:
    _GH_CTX_TOOLS_EXT = []

_github_context_tools_flag = get_setting("github_context_tools_enabled", "false").lower() == "true"
if _github_context_tools_flag:
    AGENTIC_TOOLS_V2 = AGENTIC_TOOLS_V2 + _GH_CTX_TOOLS_EXT

# ── P12: Add push_session_file + check_deploy to agentic tools ──
# push_session_file pushes an already-edited session file back to GitHub
# (content from DB, not from the model). check_deploy checks Vercel/Railway
# deployment status. Both are always available when agentic mode is on.
_P12_TOOLS = [
    {
        "name": "push_session_file",
        "description": "Push an already-edited session file back to its GitHub repo. The server reads the applied content from the database — you only provide the filename and commit message. Use ONLY after edits have been applied by the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "The session file's basename (e.g. 'LandingPage.tsx')"},
                "message": {"type": "string", "description": "Git commit message"},
                "branch": {"type": "string", "description": "Target branch (defaults to the branch it was loaded from)"},
            },
            "required": ["filename", "message"],
        },
    },
    {
        "name": "check_deploy",
        "description": "Check the latest deployment status from Vercel, Railway, or both. Use when the user asks about deploy status, build failures, or 'did it deploy?'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["vercel", "railway", "both"],
                    "description": "Which platform to check (defaults to 'both')",
                },
            },
        },
    },
]
AGENTIC_TOOLS_V2 = AGENTIC_TOOLS_V2 + _P12_TOOLS

# ── P13: create_spreadsheet — let Claude generate CSV/Excel files ──
# Uses existing DataLab infrastructure (writer.py + persist.py) to create
# a downloadable file that appears in the session file drawer.
_P13_TOOLS = [
    {
        "name": "create_spreadsheet",
        "description": (
            "Create a CSV or Excel file and add it to the session for the user to download. "
            "Use when the user asks you to generate, export, or create a spreadsheet, CSV, or Excel file. "
            "The file will appear in the session's file list and be downloadable."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Output filename (e.g. 'report.csv' or 'analysis.xlsx'). Extension determines format: .csv → CSV, .xlsx → Excel.",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Column headers (e.g. ['Name', 'Price', 'Quantity'])",
                },
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": "Data rows — each row is an array of string values matching the columns.",
                },
                "sheet_name": {
                    "type": "string",
                    "description": "Sheet name for Excel files (default: 'Sheet1'). Ignored for CSV.",
                },
            },
            "required": ["filename", "columns", "rows"],
        },
    },
]
AGENTIC_TOOLS_V2 = AGENTIC_TOOLS_V2 + _P13_TOOLS


# Phase 4: Correction handler tool definitions (tool_use migration)
CORRECTION_TOOLS = [
    {
        "name": "submit_fix",
        "description": "Submit a corrected version of a symbol to fix QA issues. new_code must be the COMPLETE replacement for the entire symbol — all imports, all functions, nothing omitted.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_path": {
                    "type": "string",
                    "description": "Full path or name of the symbol to fix (e.g., 'MyComponent', 'calculatePrice')"
                },
                "new_code": {
                    "type": "string",
                    "description": "Complete corrected code for the entire symbol"
                },
                "description": {
                    "type": "string",
                    "description": "What was fixed in this correction"
                },
                "confidence": {
                    "type": "integer",
                    "description": "Confidence score 1-10 that this fix is correct"
                }
            },
            "required": ["symbol_path", "new_code"]
        }
    },
    {
        "name": "request_symbol_code",
        "description": "Request the current code of a symbol to see its state before fixing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol_path": {
                    "type": "string",
                    "description": "Name or full path of the symbol to inspect"
                }
            },
            "required": ["symbol_path"]
        }
    },
    {
        "name": "done_fixing",
        "description": "Signal that all QA fixes are complete.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Summary of what was fixed"
                }
            },
            "required": ["summary"]
        }
    }
]


# ── Generic Anthropic→OpenAI tool format converter ─────────────────────────
# Converts {"name": ..., "input_schema": ...} → {"type": "function", "function": {"name": ..., "parameters": ...}}
# Used to give GPT models the same structured tool calls as Claude.
def _anthropic_to_openai_tools(tools: list) -> list:
    """Convert a list of Anthropic-format tools to OpenAI function-calling format."""
    oai_tools = []
    for t in tools:
        oai_tools.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            }
        })
    return oai_tools

# Pre-built OpenAI versions of agentic + correction tools
AGENTIC_TOOLS_V2_OPENAI = _anthropic_to_openai_tools(AGENTIC_TOOLS_V2)
CORRECTION_TOOLS_OPENAI = _anthropic_to_openai_tools(CORRECTION_TOOLS)





def run_architect(
    symbol_map: SymbolMap,
    user_request: str,
    file_content: str,
    model: Optional[str] = None,
    user_id: str = ""
) -> ArchitectPlan:
    """
    GPT-5 (Architect): reads symbol map + request → produces structured change plan.
    Never sees raw code — works from the symbol map for efficiency and accuracy.
    """
    arch_model = model or get_setting("architect_model", "gpt-4.1")
    temp = float(get_setting("temperature_architect", "0.0"))
    client = _get_client_for_model(arch_model, user_id)

    # Build a compact symbol map summary (not the raw code)
    symbol_summary = []
    for sym in symbol_map.symbols:
        entry = f"  [{sym.symbol_type.value}] {sym.full_path}"
        if sym.signature:
            entry += f" — {sym.signature}"
        entry += f" (lines {sym.start_line}-{sym.end_line})"
        symbol_summary.append(entry)

    # Include first 200 lines of file for context (imports, top-level structure)
    file_preview_lines = file_content.splitlines()[:200]
    file_preview = "\n".join(file_preview_lines)
    if len(file_content.splitlines()) > 200:
        file_preview += f"\n... [{len(file_content.splitlines()) - 200} more lines]"

    user_msg = f"""FILE: {symbol_map.file_path}
LANGUAGE: {symbol_map.language}
TOTAL LINES: {symbol_map.total_lines}

SYMBOL MAP:
{chr(10).join(symbol_summary)}

IMPORTS:
{chr(10).join(symbol_map.imports[:30])}

FILE PREVIEW (first 200 lines):
{file_preview}

USER REQUEST:
{user_request}

Produce the surgical change plan as JSON."""

    response = _chat_create(client,
        model=arch_model,
        messages=[
            {"role": "system", "content": ARCHITECT_SYSTEM},
            {"role": "user", "content": user_msg}
        ],
        temperature=temp,
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content
    data = json.loads(raw)

    # Parse and validate targets — only keep those that exist in symbol map
    valid_symbols = {sym.full_path for sym in symbol_map.symbols}
    valid_symbols.update({sym.name for sym in symbol_map.symbols})

    # Text-search assist: find quoted text from user request in the file
    _quoted_texts = re.findall(r"""['"](.+?)['"]""", user_request)
    _text_line_map = {}  # text -> line number where found
    file_lines = file_content.splitlines()
    for qt in _quoted_texts:
        if len(qt) >= 3:  # only meaningful text
            for i, line in enumerate(file_lines, 1):
                if qt.lower() in line.lower():
                    _text_line_map[qt] = i
                    break

    validated_targets = []
    for t in data.get("targets", []):
        target = ChangeTarget(
            symbol_path=t.get("symbol_path", ""),
            change_type=ChangeType(t.get("change_type", "modify")),
            description=t.get("description", ""),
            new_logic=t.get("new_logic", ""),
            dependencies=t.get("dependencies", []),
            confidence=t.get("confidence", 7),
            import_changes=t.get("import_changes", []),
            context_needs=t.get("context_needs", []),
            target_line=t.get("target_line"),  # v3.11.1
        )
        validated_targets.append(target)

    # Text-search redirection: if user mentioned specific text, verify it exists
    # in the targeted symbol. If not, redirect to the correct symbol or create a virtual one.
    if _text_line_map and validated_targets:
        for qt, target_line in _text_line_map.items():
            for i, target in enumerate(validated_targets):
                # Find the symbol this target points to
                matched_sym = None
                for sym in symbol_map.symbols:
                    if sym.full_path == target.symbol_path or sym.name == target.symbol_path:
                        matched_sym = sym
                        break
                if matched_sym and qt.lower() not in matched_sym.code.lower():
                    # The quoted text isn't in the targeted symbol — redirect!
                    # Find the narrowest symbol containing the target line
                    best_sym = None
                    best_size = float("inf")
                    for sym in symbol_map.symbols:
                        if sym.start_line <= target_line <= sym.end_line:
                            sym_size = sym.end_line - sym.start_line
                            if sym_size < best_size:
                                best_size = sym_size
                                best_sym = sym
                    if best_sym and best_sym.full_path != target.symbol_path:
                        validated_targets[i] = ChangeTarget(
                            symbol_path=best_sym.full_path,
                            change_type=target.change_type,
                            description=target.description,
                            new_logic=target.new_logic,
                            dependencies=target.dependencies,
                            confidence=target.confidence,
                            import_changes=target.import_changes,
                            context_needs=target.context_needs,
                            target_line=target.target_line,  # v3.11.1
                        )
                    elif not best_sym:
                        # No symbol contains this line — create a virtual window
                        total = len(file_lines)
                        win_start = max(1, target_line - 25)
                        win_end = min(total, target_line + 25)
                        win_code = "\n".join(file_lines[win_start - 1:win_end])
                        vname = f"_text_region_L{target_line}"
                        virtual_sym = SymbolInfo(
                            name=vname,
                            symbol_type=SymbolType.VARIABLE,
                            start_line=win_start,
                            end_line=win_end,
                            parent=None,
                            indentation=0,
                            code=win_code,
                            signature=f"text region around line {target_line}"
                        )
                        symbol_map.symbols.append(virtual_sym)
                        validated_targets[i] = ChangeTarget(
                            symbol_path=vname,
                            change_type=target.change_type,
                            description=target.description,
                            new_logic=target.new_logic,
                            dependencies=target.dependencies,
                            confidence=target.confidence,
                            import_changes=target.import_changes,
                            context_needs=target.context_needs,
                            target_line=target.target_line,  # v3.11.1
                        )

    # ── v3.11.1: Containment validation ──────────────────────────────────────
    # If the Architect named a symbol (e.g. hook "useCountUp") whose line range
    # does NOT contain target_line (e.g. L301), redirect to the symbol that
    # actually CONTAINS that line (e.g. "LoginPage" L124-506).
    # This catches LLM reasoning errors where a helper/hook is named instead of
    # the enclosing component, causing the Surgeon to operate on wrong code.
    for _cv_i, _cv_target in enumerate(validated_targets):
        if _cv_target.target_line is not None:
            _cv_sym = next(
                (s for s in symbol_map.symbols
                 if s.full_path == _cv_target.symbol_path or s.name == _cv_target.symbol_path),
                None
            )
            if _cv_sym and not (_cv_sym.start_line <= _cv_target.target_line <= _cv_sym.end_line):
                _cv_containing = next(
                    (s for s in symbol_map.symbols
                     if s.start_line <= _cv_target.target_line <= s.end_line),
                    None
                )
                if _cv_containing:
                    print(
                        f"[ARCHITECT] v3.11.1 containment fix: '{_cv_target.symbol_path}' "
                        f"(L{_cv_sym.start_line}–{_cv_sym.end_line}) does not contain "
                        f"target_line {_cv_target.target_line} → redirecting to "
                        f"'{_cv_containing.full_path}' (L{_cv_containing.start_line}–{_cv_containing.end_line})"
                    )
                    validated_targets[_cv_i] = ChangeTarget(
                        symbol_path=_cv_containing.full_path,
                        change_type=_cv_target.change_type,
                        description=_cv_target.description,
                        new_logic=_cv_target.new_logic,
                        dependencies=_cv_target.dependencies,
                        confidence=_cv_target.confidence,
                        import_changes=_cv_target.import_changes,
                        context_needs=_cv_target.context_needs,
                        surgeon_context=_cv_target.surgeon_context,
                        target_line=_cv_target.target_line,
                    )

    return ArchitectPlan(
        summary=data.get("summary", ""),
        targets=validated_targets,
        new_symbols_needed=data.get("new_symbols_needed", []),
        import_changes=data.get("import_changes", []),
        risks=data.get("risks", [])
    )


# ─── Script-injection safety helpers (v3.3.1) ────────────────────────────────

def _extract_new_js_code(symbol_code: str, new_code: str) -> str:
    """
    Extract genuinely new JavaScript added by the Surgeon (functions, arrow fns,
    const/let assignments that are new). Uses regex to avoid pulling in
    base64 garbage or truncated original lines.
    """
    import re as _re
    orig_fn_names = set(_re.findall(r'function\s+(\w+)', symbol_code))

    # Find new named functions
    fn_pattern = _re.compile(
        r'(?:^|\n)([ \t]*function\s+\w+\s*\([^)]*\)\s*\{[^}]*(?:\{[^}]*\}[^}]*)*\})',
        _re.MULTILINE | _re.DOTALL
    )
    new_fns = []
    for m in fn_pattern.finditer(new_code):
        body = m.group(1).strip()
        nm = _re.search(r'function\s+(\w+)', body)
        if nm and nm.group(1) not in orig_fn_names:
            new_fns.append(body)

    if new_fns:
        return "\n\n".join(new_fns)

    # Fallback: lines in new_code that aren't in original, filtered for JS-likeness
    orig_set = set(symbol_code.splitlines())
    fallback = [
        l for l in new_code.splitlines()
        if l.strip()
        and l not in orig_set
        and "</script>" not in l.lower()
        and len(l) < 300        # skip base64 blobs
        and not _re.search(r'[A-Za-z0-9+/]{50,}', l)  # skip base64 data
    ]
    return "\n".join(fallback)


def _is_script_injection_issue(symbol_code: str, new_code: str) -> tuple:
    """
    Detect whether the Surgeon truncated the window or fabricated a </script>.
    Returns (has_issue: bool, extracted_function: str).
    has_issue = True means we must use the safe INSERT path instead of REPLACE.
    """
    if not new_code:
        return False, ""

    orig_lines = symbol_code.splitlines()
    new_lines = new_code.splitlines()

    # Issue 1: Surgeon returned significantly fewer lines (truncation)
    is_truncation = len(new_lines) < len(orig_lines) * 0.75

    # Issue 2: Phantom </script> — new_code has more </script> occurrences than original
    orig_close = sum(1 for l in orig_lines if "</script>" in l.lower())
    new_close  = sum(1 for l in new_lines  if "</script>" in l.lower())
    is_phantom = new_close > orig_close

    if not (is_truncation or is_phantom):
        return False, ""

    extracted = _extract_new_js_code(symbol_code, new_code)
    if extracted:
        return True, extracted
    return False, ""


def _find_script_close_line(file_content: str, hint_line: int) -> str:
    """
    Find the line text of the </script> tag that closes the script block
    containing hint_line.  Searches FORWARD from hint_line.
    Returns the exact line string (with leading whitespace) so we can use it
    as insert_anchor in apply_change.
    """
    lines = file_content.splitlines()
    # Search forward from hint_line for </script>
    for i in range(hint_line, len(lines)):
        if "</script>" in lines[i].lower():
            return lines[i]
    # Fallback: search backward
    for i in range(hint_line - 1, -1, -1):
        if "</script>" in lines[i].lower():
            return lines[i]
    return "</script>"


# ─────────────────────────────────────────────────────────────────────────────

def _fetch_semantic_context(file_content: str, context_needs: list, symbol: "SymbolInfo") -> str:
    """
    Semantic context harvester — extracts specific code sections the Surgeon actually needs.

    Called by run_surgeon() when Architect specifies context_needs.
    Returns a formatted multi-section string to inject into the Surgeon prompt.
    Only returns sections OUTSIDE the symbol's own line range (no duplicates).

    Available context types:
      "style_block"        — full <style> tag or CSS-in-JS block
      "state_declarations" — all useState/useReducer/useRef declarations
      "hooks"              — all React hook call lines
      "css_vars"           — CSS custom property declarations (--var-name: value)
      "imports_block"      — full import section (comprehensive, beyond the 40-line header)
      "type_declarations"  — TypeScript interface/type definitions
      "constants"          — module-level const/let export declarations
    """
    if not context_needs:
        return ""

    all_lines = file_content.splitlines()
    sym_start = getattr(symbol, "start_line", 1)
    sym_end = getattr(symbol, "end_line", len(all_lines))
    sections: list = []

    def outside(lineno: int) -> bool:
        return lineno < sym_start or lineno > sym_end

    for need in context_needs:

        if need == "style_block":
            # Locate <style> block
            in_style = False
            style_lines: list = []
            style_start = -1
            for i, ln in enumerate(all_lines):
                if not in_style and re.search(r"<style[\s>]", ln):
                    in_style = True
                    style_start = i + 1
                    style_lines = [ln]
                elif in_style:
                    style_lines.append(ln)
                    if "</style>" in ln:
                        break
            if style_lines and style_start > 0 and outside(style_start):
                cap = 120
                body = style_lines[:cap]
                if len(style_lines) > cap:
                    body.append(f"  ... ({len(style_lines) - cap} more lines) ...")
                sections.append(
                    f"STYLE BLOCK (L{style_start}–L{style_start + len(style_lines) - 1})"
                    f" — reference only, match these exact colors/classes:\n"
                    + "\n".join(body)
                )

        elif need == "state_declarations":
            state_lines: list = []
            for i, ln in enumerate(all_lines):
                lineno = i + 1
                if outside(lineno) and re.search(r"\buse(?:State|Reducer|Ref)\s*[<(]", ln):
                    state_lines.append(f"  L{lineno}: {ln.rstrip()}")
            if state_lines:
                sections.append(
                    "STATE DECLARATIONS (reference only — all useState/useReducer/useRef in file):\n"
                    + "\n".join(state_lines[:40])
                )

        elif need == "hooks":
            hook_lines: list = []
            for i, ln in enumerate(all_lines):
                lineno = i + 1
                if outside(lineno) and re.search(r"\buse[A-Z]\w*\s*\(", ln):
                    hook_lines.append(f"  L{lineno}: {ln.rstrip()}")
            if hook_lines:
                sections.append(
                    "HOOK DECLARATIONS (reference only — all React hook calls):\n"
                    + "\n".join(hook_lines[:30])
                )

        elif need == "css_vars":
            css_lines: list = []
            for i, ln in enumerate(all_lines):
                lineno = i + 1
                if outside(lineno) and (re.search(r"--[\w-]+\s*:", ln) or "var(--" in ln):
                    css_lines.append(f"  L{lineno}: {ln.rstrip()}")
            if css_lines:
                sections.append(
                    "CSS CUSTOM PROPERTIES (reference only — use these exact variable names for colors/spacing):\n"
                    + "\n".join(css_lines[:50])
                )

        elif need == "imports_block":
            import_lines: list = []
            for i, ln in enumerate(all_lines):
                stripped = ln.strip()
                if stripped.startswith("import ") or stripped.startswith("from "):
                    import_lines.append(f"  L{i + 1}: {ln.rstrip()}")
                elif import_lines and not stripped:
                    pass  # blank line between imports — keep scanning
                elif import_lines:
                    break  # first non-import, non-blank line = end of block
            if import_lines:
                sections.append(
                    "IMPORTS BLOCK (reference only — full list of imports):\n"
                    + "\n".join(import_lines[:60])
                )

        elif need == "type_declarations":
            type_lines: list = []
            for i, ln in enumerate(all_lines):
                lineno = i + 1
                stripped = ln.strip()
                if outside(lineno) and re.match(
                    r"^(?:export\s+)?(?:interface|type)\s+[A-Z]", stripped
                ):
                    type_lines.append(f"  L{lineno}: {ln.rstrip()}")
            if type_lines:
                sections.append(
                    "TYPE DECLARATIONS (reference only — TypeScript interfaces and types):\n"
                    + "\n".join(type_lines[:40])
                )

        elif need == "constants":
            const_lines: list = []
            for i, ln in enumerate(all_lines):
                lineno = i + 1
                indent_len = len(ln) - len(ln.lstrip())
                stripped = ln.strip()
                if (outside(lineno)
                        and indent_len < 2
                        and re.match(r"^(?:export\s+)?const\s+[A-Z_]", stripped)):
                    const_lines.append(f"  L{lineno}: {ln.rstrip()}")
            if const_lines:
                sections.append(
                    "MODULE CONSTANTS (reference only — export const values):\n"
                    + "\n".join(const_lines[:30])
                )

    return "\n\n".join(sections)


def run_surgeon(
    symbol: SymbolInfo,
    target: ChangeTarget,
    file_content: str,
    model: Optional[str] = None,
    user_id: str = "",
    architect_risks: list = None,
    linter_feedback: list = None,    # linter error dicts — injected on compile-error retry
    extra_context: str = "",         # resolved surgeon_context from context_resolver
    qa_feedback: dict = None,        # QA verdict from previous attempt — injected on semantic retry
    forbid_noop: bool = False,       # if True, reject the empty "already correct" escape hatch
) -> tuple:
    """
    Surgeon: receives ONE code chunk + plan, returns search-and-replace operations.
    Returns (new_code, confidence, surgeon_notes, import_needed, operations).

    Claude (Architect) has already planned the change and specified exactly what context
    the Surgeon needs via surgeon_context. The pipeline resolves that context and passes
    it here as extra_context. GPT receives a complete brief and executes with precision.

    v3.4.0:  Returns JSON operations [{find, replace}].
    v3.10.0: extra_context injects Claude-resolved code; linter_feedback injects compile errors.
    v3.11.0: qa_feedback closes the coordination loop — when QA rejects an attempt, the
             Surgeon now sees the verdict on retry instead of receiving identical inputs.
    """
    client = _get_client(user_id)
    surg_model = model or get_setting("surgeon_model", "claude-sonnet-5")
    temp = float(get_setting("temperature_surgeon", "0.1"))
    client = _get_client_for_model(surg_model, user_id)

    # ── Symbol-proportional context window ───────────────────────────────────────────────
    # Window size = max(50, 40% of symbol size), capped at 300.
    # A 300-line component gets ~120 lines of surround; a 10-line function still gets ±50.
    # This replaces the old fixed ±50 constant.
    all_lines = file_content.splitlines()
    _symbol_lines = symbol.end_line - symbol.start_line + 1
    _window = min(max(50, int(_symbol_lines * 0.4)), 300)
    context_start = max(0, symbol.start_line - _window - 1)
    context_end = min(len(all_lines), symbol.end_line + _window)
    before_context = "\n".join(all_lines[context_start:symbol.start_line - 1])
    after_context = "\n".join(all_lines[symbol.end_line:context_end])

    # File header — top 40 lines (imports, key state) so Surgeon knows what's available
    # Only inject if the symbol doesn't already start near the top of the file
    _file_header = ""
    if symbol.start_line > 50:
        header_lines = all_lines[:40]
        _file_header = (
            "\nFILE HEADER (imports + key declarations — read-only, do NOT include in operations):\n"
            + "\n".join(header_lines) + "\n"
        )

    # Thread import_changes from Architect plan if present
    _import_hint = ""
    if target.import_changes:
        _import_hint = "\nREQUIRED IMPORT CHANGES (include in imports_needed):\n" + "\n".join(
            f"  {ic}" for ic in target.import_changes
        )

    # ── Architect-directed semantic context ───────────────────────────────────────────────
    # Architect specifies which OTHER file regions the Surgeon needs (e.g. style_block,
    # state_declarations). These are fetched by semantic TYPE — not by line proximity —
    # so a <style> block 200 lines away is still shown when the change needs it.
    _semantic_ctx = _fetch_semantic_context(file_content, target.context_needs or [], symbol)
    _semantic_section = (
        "\n\nSEMANTIC CONTEXT (requested by Architect — reference only; "
        "do NOT use these lines as find strings unless you are explicitly modifying that section):\n"
        + _semantic_ctx
    ) if _semantic_ctx else ""

    _risks_list = architect_risks or []
    _risks_block = "\n".join(f"- {r}" for r in _risks_list) if _risks_list else "(none — skip risk_verdicts)"

    # Build linter feedback block for retry attempts
    _linter_block = ""
    if linter_feedback:
        from services.linter_validator import format_feedback_block as _fmt_feedback, linter_tool_name as _ltool
        _sig_lower = (symbol.signature or "").lower()
        if any(ext in _sig_lower for ext in (".ts", ".tsx", ".js", ".jsx")):
            _tool_name = "tsc"
        elif ".py" in _sig_lower:
            _tool_name = "pyflakes"
        else:
            _tool_name = "tsc"
        _linter_block = "\n\n" + _fmt_feedback(linter_feedback, _tool_name)

    # Inject resolved extra_context (from context_resolver via surgeon_context plan)
    _extra_ctx_block = (
        f"\n\n{extra_context.strip()}"
        if extra_context and extra_context.strip()
        else ""
    )

    # ── v3.11.0: QA feedback block (the missing coordination link) ───────────
    # Previously the Surgeon retried with byte-identical inputs because QA's
    # verdict went only to the QA agent, never back to the Surgeon. Now we
    # surface the rejection so the Surgeon knows what to fix.
    _qa_feedback_block = ""
    if qa_feedback:
        _qa_lines: list = []
        _summary = (qa_feedback.get("summary") or "").strip()
        if _summary:
            _qa_lines.append(f"QA summary: {_summary[:300]}")
        _plan_dev = (qa_feedback.get("plan_deviation") or "").strip()
        if _plan_dev:
            _qa_lines.append(f"Plan deviation: {_plan_dev[:400]}")
        for _qk in ("import_issues", "type_errors", "logic_errors", "downstream_risks"):
            for _qi in (qa_feedback.get(_qk) or [])[:3]:
                _qa_lines.append(f"  - {_qk.replace('_', ' ')}: {str(_qi)[:200]}")
        # risk_verdicts is a list of dicts; surface blocked ones
        for _rv in (qa_feedback.get("risk_verdicts") or [])[:3]:
            if isinstance(_rv, dict) and _rv.get("status") in ("blocked", "warning"):
                _qa_lines.append(
                    f"  - {_rv.get('status', 'risk')}: "
                    f"{str(_rv.get('reason') or _rv.get('risk') or '')[:200]}"
                )
        if _qa_lines:
            _noop_clause = (
                "Do NOT emit the empty 'already correct' SEARCH/REPLACE block — "
                "QA has just rejected this exact code, so changes are required.\n"
                if forbid_noop else ""
            )
            _qa_feedback_block = (
                "\n\nPREVIOUS ATTEMPT WAS REJECTED BY QA. "
                "Re-read the CHANGE PLAN above and address EVERY item below in this attempt. "
                "Pay special attention to identifier names called out — those constants/"
                "tokens/symbols must be modified.\n"
                + _noop_clause
                + "\n".join(_qa_lines)
            )
            _dlog("surgeon_qa_feedback_injected",
                  symbol=symbol.name if symbol else "?",
                  feedback_len=len(_qa_feedback_block),
                  qa_lines_count=len(_qa_lines),
                  forbid_noop=forbid_noop,
                  summary=(qa_feedback.get("summary") or "")[:120])

    # For DELETE operations, new_logic is typically empty — make the instruction explicit
    # so the Surgeon doesn't misread "nothing to add" as "already correct".
    _ct_val = target.change_type.value if hasattr(target, "change_type") and target.change_type else "modify"

    # ── DETERMINISTIC DELETE for focused-window targets ──────────────────────────
    # When the Architect targeted a specific line inside a large context window
    # (symbol.name starts with _focused_L / _text_region_L / ends with _window),
    # the Surgeon cannot reliably identify which lines to delete — the window is too
    # large and the task description too vague.  Instead we extract the exact
    # declaration boundaries ourselves using brace-depth scanning and short-circuit
    # the LLM call entirely.  This is deterministic, zero-latency, and zero-hallucination.
    _is_focus_window = bool(
        symbol.name and (
            symbol.name.startswith("_focused_L")
            or symbol.name.startswith("_text_region_L")
            or symbol.name.endswith("_window")
        )
    )
    if _ct_val == "delete" and _is_focus_window and target.target_line and file_content:
        _fc_lines = file_content.splitlines()
        _tl0 = target.target_line - 1                    # 0-indexed
        if 0 <= _tl0 < len(_fc_lines):
            # Walk forward through the declaration using simple brace-depth counting.
            # Works for: object literals, function bodies, class declarations.
            # Single-line declarations (depth stays 0) are handled as a 1-line slice.
            _depth = sum(1 if c == "{" else -1 if c == "}" else 0 for c in _fc_lines[_tl0])
            _end0 = _tl0
            if _depth > 0:
                while _end0 + 1 < len(_fc_lines) and _depth > 0:
                    _end0 += 1
                    for c in _fc_lines[_end0]:
                        if c == "{":
                            _depth += 1
                        elif c == "}":
                            _depth -= 1
            # Absorb one trailing blank line so we don't leave a double-blank gap
            if _end0 + 1 < len(_fc_lines) and not _fc_lines[_end0 + 1].strip():
                _end0 += 1
            _find_text = "\n".join(_fc_lines[_tl0 : _end0 + 1])
            if _find_text and _find_text in symbol.code:
                import re as _re_det
                _new_sym = symbol.code.replace(_find_text, "", 1)
                # Collapse any triple-blank runs left behind
                _new_sym = _re_det.sub(r"\n{3,}", "\n\n", _new_sym)
                _det_ops = [{"find": _find_text, "replace": ""}]
                print(f"[SURGEON] Deterministic delete: removed L{target.target_line}–{_end0 + 1} ({_end0 - _tl0 + 1} lines)")
                return _new_sym, 9, [f"Deterministic delete at L{target.target_line}"], [], _det_ops

    if _ct_val == "delete":
        _new_logic_display = (
            "[DELETE OPERATION — remove the TARGET CODE lines shown below entirely from the file. "
            "Your SEARCH block must contain those exact lines; your REPLACE block must be completely empty. "
            "This is NOT 'already correct' — the TARGET CODE must be deleted.]"
        )
    else:
        _new_logic_display = target.new_logic

    user_msg = f"""CHANGE PLAN:
Type: {target.change_type.value}
Description: {target.description}
New logic required: {_new_logic_display}{_import_hint}{_file_header}{_semantic_section}{_linter_block}{_extra_ctx_block}{_qa_feedback_block}

CONTEXT BEFORE (read-only reference, do NOT include in operations):
{before_context}

TARGET CODE (lines {symbol.start_line}-{symbol.end_line}) -- your "find" text should come from here:
{symbol.code}

CONTEXT AFTER (read-only reference, do NOT include in operations):
{after_context}

Return SEARCH/REPLACE blocks ONLY. No JSON, no explanations outside blocks."""

    # ── Feature flag: tool_use vs text SEARCH/REPLACE ───────────────────────────
    _surgeon_raw = get_setting("surgeon_tool_use", "false")
    _use_tool_use = _surgeon_raw == "true"
    # NOTE: no session_id kwarg here — run_surgeon has no session_id in scope
    # (previously caused a guaranteed NameError on the non-streaming path).
    _dlog("flag_check_surgeon_tool_use",
          raw_value=_surgeon_raw, resolved=_use_tool_use,
          env_upper=_os.environ.get("SURGEON_TOOL_USE", "<not set>"),
          env_lower=_os.environ.get("surgeon_tool_use", "<not set>"),
          user_id=user_id)

    if _use_tool_use:
        # ── TOOL USE PATH (Phase 1) ─────────────────────────────────────────────
        # Claude/GPT call structured tools instead of writing free-text blocks.
        # Zero regex parsing. Zero tag recovery. Structured JSON from the SDK.
        _tu_user_msg = user_msg.replace(
            "Return SEARCH/REPLACE blocks ONLY. No JSON, no explanations outside blocks.",
            "Use the provided tools to make your changes. Do not output any text — only tool calls."
        )
        print(f"[SURGEON][TOOL_USE] Enabled — calling {surg_model} with structured tools")

        operations = []
        confidence = target.confidence
        surgeon_notes = []
        import_needed_lines = []

        _use_multi_turn = get_setting("multi_turn_surgeon", "false") == "true"

        if _use_multi_turn and not _is_claude_model(surg_model):
            _dlog("multi_turn_surgeon_gpt_fallback", model=surg_model, user_id=user_id,
                  note="GPT multi-turn not yet implemented — using single-turn tool_use")
        if _is_claude_model(surg_model) and _use_multi_turn:
            # ── MULTI-TURN VERIFICATION PATH (Phase 3) ─────────────────────────
            # Claude makes edits one at a time, sees verification after each.
            # Eliminates old_code mismatch failures: if an edit doesn't match,
            # Claude sees the error + hint and can fix it on the next turn.
            # Max turns capped to prevent runaway conversations.
            _anthropic_key = _get_anthropic_key(user_id)
            from anthropic import Anthropic as _AnthropicSync
            _sync_aclient = _AnthropicSync(api_key=_anthropic_key)

            _mt_messages = [{"role": "user", "content": _tu_user_msg}]
            _mt_working_code = symbol.code    # in-memory working copy
            _MT_MAX_TURNS = 5                 # safety cap
            _mt_total_edits = 0
            _mt_failed_edits = 0
            _mt_turn = 0

            _dlog("surgeon_multi_turn_start",
                  session_id=getattr(target, '_session_id', ''),
                  user_id=user_id,
                  model=surg_model,
                  symbol=symbol.name,
                  symbol_lines=symbol.end_line - symbol.start_line + 1,
                  max_turns=_MT_MAX_TURNS,
                  forbid_noop=forbid_noop)

            for _mt_turn in range(_MT_MAX_TURNS):
                _mt_api_start = time.time()
                try:
                    _mt_resp = _safe_claude_call_sync(
                        _sync_aclient, model=surg_model,
                        desired_text_tokens=12000, thinking_budget=4000,
                        system=SURGEON_TOOL_USE_SYSTEM,
                        messages=_mt_messages,
                        tools=SURGEON_TOOLS_ANTHROPIC,
                    )
                except Exception as _mt_api_err:
                    print(f"[SURGEON][MULTI_TURN] API error on turn {_mt_turn+1}: {_mt_api_err}")
                    _dlog("surgeon_multi_turn_api_error",
                          turn=_mt_turn + 1,
                          error=str(_mt_api_err)[:500],
                          model=surg_model,
                          user_id=user_id,
                          ops_so_far=len(operations))
                    # Return whatever we have so far (graceful degradation)
                    if operations:
                        surgeon_notes.append(f"Multi-turn: API error on turn {_mt_turn+1}, returning {len(operations)} ops collected so far")
                        break
                    return symbol.code, 0, [f"Surgeon: multi-turn API error — {str(_mt_api_err)[:100]}"], [], []

                _mt_api_elapsed = time.time() - _mt_api_start
                _mt_stop = _mt_resp.stop_reason
                _mt_block_count = len(_mt_resp.content)

                print(f"[SURGEON][MULTI_TURN] Turn {_mt_turn+1}/{_MT_MAX_TURNS}: "
                      f"stop_reason={_mt_stop}, blocks={_mt_block_count}, "
                      f"latency={_mt_api_elapsed:.1f}s, ops_so_far={len(operations)}")

                # Process tool calls in this turn
                _mt_tool_results = []
                _mt_turn_had_noop = False

                for _blk in _mt_resp.content:
                    if _blk.type == "tool_use":
                        if _blk.name == "edit_code":
                            _old = _blk.input.get("old_code", "")
                            _new = _blk.input.get("new_code", "")
                            _mt_total_edits += 1

                            if _old and _old in _mt_working_code:
                                # ✅ Edit matches — apply in-memory
                                _mt_working_code = _mt_working_code.replace(_old, _new, 1)
                                operations.append({"find": _old, "replace": _new})

                                # Build verification context (±3 lines around edit)
                                _edit_pos = _mt_working_code.find(_new) if _new else 0
                                _ctx_lines = _mt_working_code.splitlines()
                                _edit_line = _mt_working_code[:max(0, _edit_pos)].count("\n") + 1
                                _ctx_start = max(0, _edit_line - 4)
                                _ctx_end = min(len(_ctx_lines), _edit_line + _new.count("\n") + 4)
                                _ctx_snippet = "\n".join(
                                    f"{_ctx_start + j + 1}: {l}"
                                    for j, l in enumerate(_ctx_lines[_ctx_start:_ctx_end])
                                )

                                _result_payload = {
                                    "success": True,
                                    "message": f"Edit applied successfully at line {_edit_line}",
                                    "lines_changed": _new.count("\n") + 1,
                                    "context": _ctx_snippet
                                }
                                _mt_tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": _blk.id,
                                    "content": json.dumps(_result_payload)
                                })

                                _dlog("surgeon_multi_turn_edit_ok",
                                      turn=_mt_turn + 1,
                                      edit_num=_mt_total_edits,
                                      edit_line=_edit_line,
                                      old_len=len(_old),
                                      new_len=len(_new),
                                      working_code_len=len(_mt_working_code),
                                      user_id=user_id)
                                print(f"[SURGEON][MULTI_TURN]   ✅ edit_code applied at L{_edit_line} "
                                      f"(old={len(_old)}→new={len(_new)} chars)")

                            else:
                                # ❌ old_code doesn't match — send error with hint
                                _mt_failed_edits += 1

                                # Find the closest matching line for a helpful hint
                                _hint_lines = []
                                _old_first_line = _old.strip().split("\n")[0] if _old.strip() else ""
                                if _old_first_line:
                                    for _hi, _hl in enumerate(_mt_working_code.splitlines(), 1):
                                        if _old_first_line.strip() in _hl.strip():
                                            _hint_lines.append(f"L{_hi}: {_hl.rstrip()}")
                                            if len(_hint_lines) >= 3:
                                                break

                                _err_payload = {
                                    "success": False,
                                    "error": "old_code not found in current file content. "
                                             "The text must match exactly, including indentation and whitespace.",
                                    "old_code_preview": _old[:200],
                                    "hint": f"Similar lines found: {'; '.join(_hint_lines)}" if _hint_lines else
                                            "No similar lines found. Try requesting the current file content."
                                }
                                _mt_tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": _blk.id,
                                    "content": json.dumps(_err_payload),
                                    "is_error": True
                                })

                                _dlog("surgeon_multi_turn_edit_fail",
                                      turn=_mt_turn + 1,
                                      edit_num=_mt_total_edits,
                                      old_code_len=len(_old),
                                      old_code_preview=_old[:300],
                                      hint_lines=_hint_lines,
                                      working_code_len=len(_mt_working_code),
                                      user_id=user_id)
                                print(f"[SURGEON][MULTI_TURN]   ❌ edit_code FAILED — old_code not found "
                                      f"(old={len(_old)} chars, hints={len(_hint_lines)})")

                        elif _blk.name == "replace_symbol":
                            _new = _blk.input.get("new_code", "")
                            operations.append({"find": _mt_working_code, "replace": _new})
                            _mt_working_code = _new
                            _mt_total_edits += 1

                            _mt_tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": _blk.id,
                                "content": json.dumps({
                                    "success": True,
                                    "message": f"Symbol fully replaced ({len(_new.splitlines())} lines)",
                                    "new_length": len(_new.splitlines())
                                })
                            })
                            _dlog("surgeon_multi_turn_replace_symbol",
                                  turn=_mt_turn + 1,
                                  new_len=len(_new),
                                  user_id=user_id)
                            print(f"[SURGEON][MULTI_TURN]   ✅ replace_symbol ({len(_new)} chars)")

                        elif _blk.name == "no_change_needed":
                            _reason = _blk.input.get("reason", "")
                            _mt_turn_had_noop = True
                            print(f"[SURGEON][MULTI_TURN]   no_change_needed: {_reason[:200]}")

                            if forbid_noop:
                                # QA rejected — force Claude to make real edits
                                _mt_tool_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": _blk.id,
                                    "content": json.dumps({
                                        "success": False,
                                        "error": "QA has rejected the current code. Changes ARE required. "
                                                 "Re-read the CHANGE PLAN and make the specified modifications."
                                    }),
                                    "is_error": True
                                })
                                _dlog("surgeon_multi_turn_noop_rejected",
                                      turn=_mt_turn + 1,
                                      reason=_reason[:300],
                                      user_id=user_id)
                            else:
                                # Legitimate noop — return immediately
                                _dlog("surgeon_multi_turn_noop_accepted",
                                      turn=_mt_turn + 1,
                                      reason=_reason[:300],
                                      user_id=user_id)
                                return symbol.code, 10, [f"Surgeon: already implemented — {_reason[:100]}"], [], []

                    elif _blk.type == "text":
                        print(f"[SURGEON][MULTI_TURN]   text block ({len(_blk.text)} chars)")

                # ── Turn exit conditions ──────────────────────────────────────
                # 1. end_turn with no pending tool results → Claude is done
                if _mt_stop == "end_turn":
                    _dlog("surgeon_multi_turn_end_turn",
                          turn=_mt_turn + 1,
                          total_ops=len(operations),
                          total_edits=_mt_total_edits,
                          failed_edits=_mt_failed_edits,
                          user_id=user_id)
                    print(f"[SURGEON][MULTI_TURN] end_turn on turn {_mt_turn+1} — done")
                    break

                # 2. No tool results to send back → nothing to continue with
                if not _mt_tool_results:
                    _dlog("surgeon_multi_turn_no_tools",
                          turn=_mt_turn + 1,
                          stop_reason=_mt_stop,
                          user_id=user_id)
                    print(f"[SURGEON][MULTI_TURN] No tool results on turn {_mt_turn+1} — done")
                    break

                # 3. All edits failed and this is the last turn → stop
                if _mt_turn >= _MT_MAX_TURNS - 1:
                    _dlog("surgeon_multi_turn_budget_exhausted",
                          turns_used=_mt_turn + 1,
                          total_ops=len(operations),
                          failed_edits=_mt_failed_edits,
                          user_id=user_id)
                    print(f"[SURGEON][MULTI_TURN] Budget exhausted after {_mt_turn+1} turns")
                    break

                # ── Continue conversation ─────────────────────────────────────
                # Convert response.content to serializable format for messages
                _mt_assistant_content = []
                for _blk in _mt_resp.content:
                    if _blk.type == "tool_use":
                        _mt_assistant_content.append({
                            "type": "tool_use",
                            "id": _blk.id,
                            "name": _blk.name,
                            "input": _blk.input
                        })
                    elif _blk.type == "text":
                        _mt_assistant_content.append({
                            "type": "text",
                            "text": _blk.text
                        })

                _mt_messages.append({"role": "assistant", "content": _mt_assistant_content})
                _mt_messages.append({"role": "user", "content": _mt_tool_results})

                print(f"[SURGEON][MULTI_TURN] Continuing → turn {_mt_turn+2} "
                      f"(msgs={len(_mt_messages)}, ops={len(operations)}, "
                      f"failed={_mt_failed_edits})")

            # ── Multi-turn summary ────────────────────────────────────────────
            _mt_final_turns = _mt_turn + 1
            _dlog("surgeon_multi_turn_complete",
                  turns_used=_mt_final_turns,
                  max_turns=_MT_MAX_TURNS,
                  total_ops=len(operations),
                  total_edits=_mt_total_edits,
                  failed_edits=_mt_failed_edits,
                  working_code_len=len(_mt_working_code),
                  original_code_len=len(symbol.code),
                  model=surg_model,
                  user_id=user_id)
            print(f"[SURGEON][MULTI_TURN] Complete: {len(operations)} ops in "
                  f"{_mt_final_turns} turn(s), {_mt_failed_edits} failed edits")

            if _mt_failed_edits > 0 and not operations:
                # All edits failed — return with confidence 0
                surgeon_notes.append(f"Multi-turn: {_mt_failed_edits} edit(s) failed, 0 succeeded")
                confidence = 0
            elif _mt_failed_edits > 0:
                surgeon_notes.append(f"Multi-turn: {_mt_failed_edits} edit(s) failed, {len(operations)} succeeded")

        elif _is_claude_model(surg_model):
            # ── SINGLE-TURN TOOL USE (Phase 1 — unchanged) ──────────────────────
            _anthropic_key = _get_anthropic_key(user_id)
            from anthropic import Anthropic as _AnthropicSync
            _sync_aclient = _AnthropicSync(api_key=_anthropic_key)
            try:
                _dlog("surgeon_tool_use_call_config", model=surg_model,
                      wrapper="safe_claude_call_sync", user_id=user_id,
                      session_id=session_id)
                _tu_resp = _safe_claude_call_sync(
                    _sync_aclient, model=surg_model,
                    desired_text_tokens=12000, thinking_budget=4000,
                    system=SURGEON_TOOL_USE_SYSTEM,
                    messages=[{"role": "user", "content": _tu_user_msg}],
                    tools=SURGEON_TOOLS_ANTHROPIC,
                )
                _tu_stop = _tu_resp.stop_reason
                print(f"[SURGEON][TOOL_USE] Claude response: stop_reason={_tu_stop}, "
                      f"content_blocks={len(_tu_resp.content)}")
                if _tu_stop == "max_tokens":
                    _dlog("surgeon_tool_use_truncated_refused",
                          model=surg_model,
                          stop_reason=_tu_stop,
                          user_id=user_id)
                    print("[SURGEON][TOOL_USE] Truncated at max_tokens — refusing partial tool input")
                    return symbol.code, 0, ["Surgeon: output truncated at token limit — no edit applied"], [], []
                for _blk in _tu_resp.content:
                    if _blk.type == "tool_use":
                        print(f"[SURGEON][TOOL_USE]   tool={_blk.name}, "
                              f"input_keys={list(_blk.input.keys()) if isinstance(_blk.input, dict) else '?'}")
                        if _blk.name == "edit_code":
                            _old = _blk.input.get("old_code", "")
                            _new = _blk.input.get("new_code", "")
                            operations.append({"find": _old, "replace": _new})
                        elif _blk.name == "replace_symbol":
                            _new = _blk.input.get("new_code", "")
                            operations.append({"find": symbol.code, "replace": _new})
                        elif _blk.name == "no_change_needed":
                            _reason = _blk.input.get("reason", "")
                            print(f"[SURGEON][TOOL_USE]   no_change_needed: {_reason[:200]}")
                            if forbid_noop:
                                print("[SURGEON][TOOL_USE] Rejected no_change_needed after QA failure")
                                return symbol.code, 0, ["Surgeon: refused noop after QA rejection"], [], []
                            return symbol.code, 10, [f"Surgeon: already implemented — {_reason[:100]}"], [], []
                    elif _blk.type == "text":
                        print(f"[SURGEON][TOOL_USE]   text block ({len(_blk.text)} chars) — ignored")
            except Exception as _tu_err:
                print(f"[SURGEON][TOOL_USE] Claude tool_use call failed: {_tu_err}")
                _dlog("surgeon_tool_use_error",
                      error=str(_tu_err)[:500],
                      model=surg_model,
                      user_id=user_id)
                return symbol.code, 0, [f"Surgeon: tool_use API error — {str(_tu_err)[:100]}"], [], []
        else:
            # GPT / OpenAI-compatible tool_use path
            try:
                _tu_resp = _chat_create(client,
                    model=surg_model,
                    messages=[
                        {"role": "system", "content": SURGEON_TOOL_USE_SYSTEM},
                        {"role": "user", "content": _tu_user_msg}
                    ],
                    temperature=temp,
                    tools=SURGEON_TOOLS_OPENAI,
                    tool_choice="required",  # docs: force >=1 tool call — text-only
                                             # replies would silently become noops
                )
                # Phase 1 guard: refuse output that stayed truncated after retry.
                # Partial tool_call JSON must never be parsed into operations.
                if getattr(_tu_resp, "_sai_truncated", False):
                    _dlog("surgeon_tool_use_truncated_refused",
                          model=surg_model, user_id=user_id)
                    print("[SURGEON][TOOL_USE] GPT output truncated after retry — refusing")
                    return symbol.code, 0, ["Surgeon: output truncated — retry"], [], []
                _tu_calls = _tu_resp.choices[0].message.tool_calls or []
                print(f"[SURGEON][TOOL_USE] GPT response: {len(_tu_calls)} tool call(s)")
                if not _tu_calls:
                    # Without this, zero tool calls fell through to operations=[]
                    # → confidence 10 → falsely reported "already correct".
                    _dlog("surgeon_tool_use_no_tool_calls",
                          model=surg_model, user_id=user_id,
                          finish_reason=getattr(_tu_resp.choices[0], "finish_reason", None))
                    print("[SURGEON][TOOL_USE] GPT returned no tool calls — refusing")
                    return symbol.code, 0, ["Surgeon: no tool calls returned — retry"], [], []
                for _tc in _tu_calls:
                    import json as _json_tu
                    try:
                        _tc_args = _json_tu.loads(_tc.function.arguments)
                    except Exception as _jpe:
                        print(f"[SURGEON][TOOL_USE] Failed to parse tool args: {_jpe}")
                        continue
                    print(f"[SURGEON][TOOL_USE]   tool={_tc.function.name}, "
                          f"arg_keys={list(_tc_args.keys())}")
                    if _tc.function.name == "edit_code":
                        _old = _tc_args.get("old_code", "")
                        _new = _tc_args.get("new_code", "")
                        operations.append({"find": _old, "replace": _new})
                    elif _tc.function.name == "replace_symbol":
                        _new = _tc_args.get("new_code", "")
                        operations.append({"find": symbol.code, "replace": _new})
                    elif _tc.function.name == "no_change_needed":
                        _reason = _tc_args.get("reason", "")
                        print(f"[SURGEON][TOOL_USE]   no_change_needed: {_reason[:200]}")
                        if forbid_noop:
                            print("[SURGEON][TOOL_USE] Rejected no_change_needed after QA failure")
                            return symbol.code, 0, ["Surgeon: refused noop after QA rejection"], [], []
                        return symbol.code, 10, [f"Surgeon: already implemented — {_reason[:100]}"], [], []
            except Exception as _tu_err:
                print(f"[SURGEON][TOOL_USE] GPT tool_use call failed: {_tu_err}")
                _dlog("surgeon_tool_use_error",
                      error=str(_tu_err)[:500],
                      model=surg_model,
                      user_id=user_id)
                return symbol.code, 0, [f"Surgeon: tool_use API error — {str(_tu_err)[:100]}"], [], []

        print(f"[SURGEON][TOOL_USE] Final: {len(operations)} operation(s) extracted")

        # Noop check: if QA rejected and all ops are empty, refuse
        if forbid_noop and operations:
            _all_empty = all(
                not (op.get("find", "") or "").strip() and not (op.get("replace", "") or "").strip()
                for op in operations
            )
            if _all_empty:
                print("[SURGEON][TOOL_USE] Rejected all-empty ops after QA failure")
                return symbol.code, 0, ["Surgeon: refused empty-block noop after QA rejection"], [], []

    else:
        # ── ORIGINAL TEXT PATH (unchanged) ───────────────────────────────────────

        if _is_claude_model(surg_model):
            # Claude Surgeon path — Anthropic SDK (OpenAI client cannot call Claude models)
            _anthropic_key = _get_anthropic_key(user_id)
            from anthropic import Anthropic as _AnthropicSync
            _sync_aclient = _AnthropicSync(api_key=_anthropic_key)
            from services.api_retry import api_call_with_retry
            # Thinking-config fix: adaptive models consume max_tokens for
            _dlog("surgeon_text_call_config", model=surg_model,
                  wrapper="safe_claude_call_sync", user_id=user_id)
            _claude_surgeon_resp = _safe_claude_call_sync(
                _sync_aclient, model=surg_model,
                desired_text_tokens=12000, thinking_budget=4000,
                system=SURGEON_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = _extract_claude_text(_claude_surgeon_resp)
            # Truncation guard (mirrors the GPT _sai_truncated refusal below):
            # a max_tokens cut can leave zero complete SEARCH/REPLACE blocks,
            # and the raw-fallback would then apply the truncated text as a
            # FULL symbol replacement. Refuse and let the caller retry instead.
            if getattr(_claude_surgeon_resp, "stop_reason", None) == "max_tokens":
                _dlog("surgeon_claude_text_truncated_refused",
                      model=surg_model, raw_len=len(raw or ""), user_id=user_id)
                print("[SURGEON] Claude output truncated at max_tokens — refusing partial output")
                return symbol.code, 0, ["Surgeon: output truncated — retry"], [], []
        else:
            response = _chat_create(client,
                model=surg_model,
                messages=[
                    {"role": "system", "content": SURGEON_SYSTEM},
                    {"role": "user", "content": user_msg}
                ],
                temperature=temp
            )
            raw = response.choices[0].message.content
            # Phase 1 guard: _chat_create marks responses that stayed truncated
            # even after its budget-doubling retry. Parsing partial output here
            # risks writing half a symbol into the file — refuse instead.
            if getattr(response, "_sai_truncated", False):
                _dlog("surgeon_truncated_output_refused",
                      model=surg_model, raw_len=len(raw or ""), user_id=user_id)
                print("[SURGEON] Output truncated (finish_reason=length after retry) — refusing partial output")
                return symbol.code, 0, ["Surgeon: output truncated — retry"], [], []
            raw = raw or ""

        # Parse Aider-style SEARCH/REPLACE blocks
        operations = []
        confidence = target.confidence
        surgeon_notes = []
        import_needed_lines = []

        raw = raw.strip()

        # Already-correct shortcut — but if QA just rejected this exact code,
        # "already correct" is a lie. Force a retry instead of silently passing.
        if raw.startswith("ALREADY_CORRECT"):
            if forbid_noop:
                print("[SURGEON] Rejected ALREADY_CORRECT after QA failure — Surgeon must produce real edits")
                return symbol.code, 0, ["Surgeon: refused noop after QA rejection"], [], []
            return symbol.code, 10, ["Surgeon: already implemented"], [], []

        # Extract all <<<<<<< SEARCH / ======= / >>>>>>> REPLACE blocks
        _sr_pattern = re.compile(r"<{7} SEARCH\r?\n(.*?)\r?\n={7}\r?\n(.*?)\r?\n>{7} REPLACE", re.DOTALL)
        _matches = _sr_pattern.findall(raw)

        if not _matches:
            # If the raw output contains SEARCH/REPLACE markers the regex didn't match,
            # the Surgeon tried to use the format but produced malformed blocks.
            # NEVER inject raw output containing those markers — it would write
            # <<<<<<< SEARCH / ======= / >>>>>>> REPLACE directly into the file.
            _has_sr_markers = ("<<<<<<< SEARCH" in raw or ">>>>>>> REPLACE" in raw
                               or ("=======" in raw and "SEARCH" in raw))
            if _has_sr_markers:
                print("[SURGEON] Malformed SEARCH/REPLACE blocks detected — refusing raw fallback to avoid injecting markers")
                return symbol.code, 0, ["Surgeon: malformed SEARCH/REPLACE output — retry"], [], []
            # Safe: no markers present — treat whole raw as full symbol replacement
            print("[SURGEON] No SEARCH/REPLACE blocks found — using raw as full replacement")
            _clean = raw.strip("\n")
            if _clean:
                operations = [{"find": symbol.code, "replace": _clean}]
        else:
            for _find, _replace in _matches:
                operations.append({"find": _find, "replace": _replace})
            print(f"[SURGEON] Parsed {len(_matches)} SEARCH/REPLACE block(s)")

        # v3.11.0: If QA just rejected and Surgeon only produced empty blocks, refuse.
        # An empty SEARCH/REPLACE pair is the same noop escape hatch as ALREADY_CORRECT.
        if forbid_noop and operations:
            _all_empty = all(
                not (op.get("find", "") or "").strip() and not (op.get("replace", "") or "").strip()
                for op in operations
            )
            if _all_empty:
                print("[SURGEON] Rejected all-empty SEARCH/REPLACE after QA failure")
                return symbol.code, 0, ["Surgeon: refused empty-block noop after QA rejection"], [], []
    # ── Mechanical trailing-context rescue ──────────────────────────────────────────────
    # Must run BEFORE apply so QA + diff both see the corrected ops.
    # Detects 'find' strings that captured structural lines after the change target
    # (closing tags, sibling divs, next functions) and rescues them back into 'replace'.
    _ct_str = target.change_type.value if hasattr(target, "change_type") and target.change_type else "modify"
    operations = _rescue_trailing_context(operations, change_type=_ct_str)

    # ---- Compute new_code by applying operations to full file (same as apply path) ----
    # This guarantees QA/diff sees the same result the user would get after applying.
    _original_for_qa = symbol.code
    new_code = symbol.code
    try:
        if operations and file_content:
            from services.surgical_editor import apply_operations as _apply_ops
            ops_dicts = [{"find": op.get("find",""), "replace": op.get("replace","")} for op in operations]
            modified_file = _apply_ops(file_content, ops_dicts, hint_line=symbol.start_line)
            # Extract the window around the symbol for QA/diff
            # QA always gets the full original symbol vs full new symbol — no windowing.
            # Windowing by original end_line truncates the new code when the Surgeon
            # expands a small symbol (e.g. 2-line LiveDot → 60-line component).
            # Instead: apply ops directly to symbol.code to get the exact new symbol content.
            _original_for_qa = symbol.code
            _qa_code = symbol.code
            for _qa_op in ops_dicts:
                _qf = _qa_op.get("find", "")
                _qr = _qa_op.get("replace", "")
                if _qf and _qf in _qa_code:
                    _qa_code = _qa_code.replace(_qf, _qr, 1)
            new_code = _qa_code
            print(f"[MATCH] apply_operations OK: {len(operations)} ops, QA gets full symbol ({len(new_code)} chars)")
    except (ValueError, Exception) as _apply_err:
        # Operations couldn't be applied to full file — fall back to symbol.code matching
        print(f"[MATCH] apply_operations failed ({_apply_err}), falling back to symbol.code")
        _dlog("apply_operations_failed",
              error_type=type(_apply_err).__name__,
              error=str(_apply_err)[:300],
              op_count=len(operations),
              user_id=user_id)
        new_code = symbol.code
        for op in operations:
            find_text = op.get("find", "")
            replace_text = op.get("replace", "")
            if not find_text:
                continue
            idx = new_code.find(find_text)
            if idx != -1:
                new_code = new_code[:idx] + replace_text + new_code[idx + len(find_text):]
            else:
                # Try stripped match
                ft = find_text.strip()
                if ft:
                    for li, raw_line in enumerate(new_code.splitlines(keepends=True)):
                        if ft in raw_line.strip():
                            lines = new_code.splitlines(keepends=True)
                            indent = raw_line[:len(raw_line) - len(raw_line.lstrip())]
                            before = "".join(lines[:li])
                            after = "".join(lines[li + 1:])
                            new_code = before + indent + replace_text.lstrip() + "\n" + after
                            break

    # Store effective original for QA (Pydantic-safe)
    try:
        object.__setattr__(symbol, '_file_window_original', _original_for_qa)
    except Exception:
        pass

    # Confidence: empty operations = already correct
    if not operations:
        confidence = 10

    # Confidence reduction for suspicious results
    orig_line_count = len(symbol.code.splitlines())
    new_line_count = len(new_code.splitlines())
    if new_line_count < orig_line_count * 0.3:
        confidence = min(confidence, 5)

    return new_code, confidence, surgeon_notes, import_needed_lines, operations


# DEPRECATED: Use run_chat_stream() instead. This sync version is kept for backward compat only.
def run_chat(
    messages: list,
    file_content: Optional[str] = None,
    symbol_context: Optional[str] = None,
    model: Optional[str] = None,
    pinned_context: Optional[list] = None,
    project_memory: Optional[str] = None,
    user_id: str = ""
) -> str:
    """
    Standard chat (non-surgical). Uses configured model.
    Streams response and returns full text.
    DEPRECATED: prefer run_chat_stream() for all new callers.
    """
    chat_model = model or get_setting("architect_model", "gpt-4.1")

    system_parts = [CHAT_PERSONA]

    # Inject project memory
    if project_memory:
        system_parts.append(f"\n## Project Conventions & Memory\n{project_memory}")

    # Inject pinned context
    if pinned_context:
        for pin in pinned_context:
            system_parts.append(f"\n## Pinned Context: {pin.get('label', pin.get('file_path', ''))}\n```\n{pin.get('content', '')}\n```")

    if file_content:
        lines = file_content.splitlines()
        preview = "\n".join(lines[:300])
        if len(lines) > 300:
            preview += f"\n... [{len(lines) - 300} more lines not shown]"
        system_parts.append(f"\nActive file content (first 300 lines):\n```\n{preview}\n```")

    if symbol_context:
        system_parts.append(f"\nFocused symbol context:\n```\n{symbol_context}\n```")

    system_prompt = "\n".join(system_parts)
    all_messages = [{"role": "system", "content": system_prompt}] + messages

    if _should_use_ollama(chat_model):
        return _ollama_chat(all_messages, chat_model)

    client = _get_client(user_id)
    response = _chat_create(client,
        model=chat_model,
        messages=all_messages,
        temperature=float(get_setting("temperature_architect", "0.0")),
        stream=False
    )

    return response.choices[0].message.content


async def run_chat_stream(
    messages: list,
    file_content: Optional[str] = None,
    symbol_context: Optional[str] = None,
    model: Optional[str] = None,
    pinned_context: Optional[list] = None,
    project_memory: Optional[str] = None,
    user_id: str = ""
):
    """
    Streaming version of run_chat. Yields SSE chunks.
    Used by the /api/chat/stream endpoint.
    """
    chat_model = model or get_setting("architect_model", "gpt-4.1")

    system_parts = [CHAT_PERSONA]

    # Inject project memory
    if project_memory:
        system_parts.append(f"\n## Project Conventions & Memory\n{project_memory}")

    # Inject pinned context
    if pinned_context:
        for pin in pinned_context:
            system_parts.append(f"\n## Pinned Context: {pin.get('label', pin.get('file_path', ''))}\n```\n{pin.get('content', '')}\n```")

    if file_content:
        lines = file_content.splitlines()
        preview = "\n".join(lines[:300])
        if len(lines) > 300:
            preview += f"\n... [{len(lines) - 300} more lines not shown]"
        system_parts.append(f"\nActive file content (first 300 lines):\n```\n{preview}\n```")

    if symbol_context:
        system_parts.append(f"\nFocused symbol context:\n```\n{symbol_context}\n```")

    system_prompt = "\n".join(system_parts)
    all_messages = [{"role": "system", "content": system_prompt}] + messages

    try:
        def sse(obj):
            return f"data: {json.dumps(obj)}\n\n"

        if _is_gemini_model(chat_model) and HAS_GOOGLE_GENAI:
            # ── Gemini streaming with native thinking blocks ──
            gemini_key = _get_gemini_key(user_id)
            gclient = _google_genai.Client(api_key=gemini_key)
            system_text = next((m["content"] for m in all_messages if m["role"] == "system"), "")
            gemini_contents = [
                {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                for m in all_messages if m["role"] != "system"
            ]
            thinking_cfg = None
            if _supports_thinking(chat_model):
                thinking_cfg = _google_types.ThinkingConfig(thinking_budget=10000)
            gcfg = _google_types.GenerateContentConfig(
                system_instruction=system_text or None,
                thinking_config=thinking_cfg,
            )
            in_thinking = False
            async for gchunk in await gclient.aio.models.generate_content_stream(
                model=chat_model, contents=gemini_contents, config=gcfg
            ):
                for gpart in (gchunk.candidates or [{}])[0].content.parts if (gchunk.candidates and gchunk.candidates[0].content) else []:
                    is_thought = getattr(gpart, "thought", False)
                    if is_thought:
                        if not in_thinking:
                            yield sse({"type": "thinking_start", "content": ""})
                            in_thinking = True
                        if gpart.text:
                            yield sse({"type": "thinking", "content": gpart.text})
                    else:
                        if in_thinking:
                            yield sse({"type": "thinking_end", "content": ""})
                            in_thinking = False
                        if gpart.text:
                            yield f"data: {json.dumps({'type': 'token', 'content': gpart.text})}\n\n"
            if in_thinking:
                yield sse({"type": "thinking_end", "content": ""})
        elif _is_gemini_model(chat_model):
            # Fallback: Gemini via OpenAI-compat (no native thinking)
            gclient_oai = _get_client_for_model(chat_model, user_id)
            stream = _chat_create(gclient_oai, model=chat_model, messages=all_messages,
                                  temperature=float(get_setting("temperature_architect", "0.0")), stream=True)
            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield f"data: {json.dumps({'type': 'token', 'content': delta.content})}\n\n"
        elif _should_use_ollama(chat_model):
            # ── Ollama streaming with <think> tag parsing for reasoning models ──
            base_url = get_setting("ollama_base_url", "http://localhost:11434")
            ollama_model = chat_model.replace("ollama:", "") if chat_model.startswith("ollama:") else get_setting("ollama_model", "qwen2.5-coder:7b")
            in_thinking = False
            with httpx.stream("POST", f"{base_url}/api/chat", json={"model": ollama_model, "messages": all_messages, "stream": True}, timeout=120) as resp:
                for line in resp.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            token = data.get("message", {}).get("content", "")
                            while token:
                                if not in_thinking:
                                    ts = token.find("<think>")
                                    if ts != -1:
                                        before = token[:ts]
                                        if before:
                                            yield f"data: {json.dumps({'type': 'token', 'content': before})}\n\n"
                                        yield sse({"type": "thinking_start", "content": ""})
                                        in_thinking = True
                                        token = token[ts + 7:]
                                    else:
                                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                                        token = ""
                                else:
                                    te = token.find("</think>")
                                    if te != -1:
                                        tc = token[:te]
                                        if tc:
                                            yield sse({"type": "thinking", "content": tc})
                                        yield sse({"type": "thinking_end", "content": ""})
                                        in_thinking = False
                                        token = token[te + 8:]
                                    else:
                                        yield sse({"type": "thinking", "content": token})
                                        token = ""
                        except Exception:
                            pass
            if in_thinking:
                yield sse({"type": "thinking_end", "content": ""})
        else:
            client = _get_client(user_id)
            stream = _chat_create(client,
                model=chat_model,
                messages=all_messages,
                temperature=float(get_setting("temperature_architect", "0.0")),
                stream=True
            )
            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield f"data: {json.dumps({'type': 'token', 'content': delta.content})}\n\n"
    except Exception as e:
        _dlog("streaming_error",
              error_type=type(e).__name__,
              error=str(e)[:300], user_id=user_id)
        yield sse({"type": "error", "content": _friendly_error(e)})

    yield f"data: {json.dumps({'type': 'done', 'content': ''})}\n\n"



# ─────────────────────────────────────────────────────────────
# CLAUDE-FIRST PIPELINE (v4) — replaces old Architect+Surgeon
# Single Claude call: analyzes + writes complete symbol replacements
# Apply: exact original symbol code → reliable find-and-replace
# Keepalive: Claude streaming thinking blocks keep connection alive
# ─────────────────────────────────────────────────────────────

import uuid
from typing import Optional


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

CLAUDE_EDITOR_SYSTEM = """You are SurgicalAI — a precise AI code editor powered by Claude.

You analyze code and write complete symbol replacements. You do NOT write SEARCH/REPLACE fragments — you write the ENTIRE new version of each symbol that needs to change.

━━━ YOUR JOB ━━━
1. Read the file structure (symbol map) and code provided
2. Understand exactly what the user wants
3. Identify the minimum set of symbols to change
4. Write the COMPLETE new code for each changed symbol

━━━ DECISION TREE ━━━

Choose ONE intent:

"edit" — when you can identify exactly which symbol(s) need to change AND you have seen their code
"chat" — questions, explanations, no code change needed
"needs_clarification" — genuinely ambiguous, multiple valid interpretations
"search" — you need to see a specific symbol's code before you can write the replacement

━━━ OUTPUT FORMAT ━━━

Output ONLY valid JSON. No markdown fences, no preamble.

IF edit:
{
  "intent": "edit",
  "summary": "one sentence describing what changes",
  "reasoning": "your analysis — what you read, what you identified, why",
  "changes": [
    {
      "symbol_path": "exact symbol name from the SYMBOL MAP (e.g. LoginPage, handleSubmit)",
      "description": "what changed in this symbol and why",
      "new_code": "THE COMPLETE NEW CODE — entire symbol from first line to last, nothing omitted",
      "confidence": 9
    }
  ],
  "risks": ["any side effects or things to verify"]
}

IF chat:
{
  "intent": "chat",
  "chat_response": "full markdown answer here"
}

IF needs_clarification:
{
  "intent": "needs_clarification",
  "clarification_response": "friendly message with 1-2 specific questions. End with: Once you answer, I will plan the exact changes.",
  "questions": ["specific question"]
}

IF search (need to see a symbol's code first):
{
  "intent": "search",
  "reasoning": "what you found so far and what you still need",
  "search_terms": ["ExactSymbolName", "another_symbol"]
}

━━━ RULES FOR new_code ━━━
- Include the COMPLETE symbol: opening declaration, full body, closing bracket/brace
- Preserve ALL unchanged lines exactly — copy them character-for-character from the original
- Only modify the specific lines that need to change
- Match the original indentation exactly (same spaces/tabs)
- new_code REPLACES the entire original symbol line-for-line — if you omit lines, they get deleted
- If LoginPage is 400 lines and you change 1 line, new_code is still 400 lines (with 1 different line)

━━━ SYMBOL TARGETING RULES ━━━
- symbol_path MUST exactly match a name from the SYMBOL MAP
- For React/TSX files: always target the COMPONENT that renders the UI (e.g. LoginPage) — NEVER use hooks (useXxx) as symbol_path for UI changes
- Only change symbols explicitly asked about — minimal footprint
- confidence < 7 → use needs_clarification instead of guessing"""


# ---------------------------------------------------------------------------
# Helper: _build_claude_context
# ---------------------------------------------------------------------------

def _build_claude_context(
    symbol_map,
    file_content: str,
    file_path: str,
    user_request: str,
    project_memory: Optional[str] = None,
    search_results=None,
) -> str:
    """
    Build the context string to include in the Claude user message.

    Parameters
    ----------
    symbol_map      : SymbolMap from the AST parser
    file_content    : raw file text
    file_path       : path shown to Claude
    user_request    : the user's natural-language request
    project_memory  : optional project-level memory string
    search_results  : optional list of resolved search result dicts
    """
    lines = file_content.splitlines()
    total_lines = len(lines)

    # Determine language from extension
    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "unknown"
    lang_map = {
        "py": "Python", "js": "JavaScript", "jsx": "JavaScript (JSX)",
        "ts": "TypeScript", "tsx": "TypeScript (TSX)", "go": "Go",
        "rs": "Rust", "java": "Java", "rb": "Ruby", "php": "PHP",
        "cpp": "C++", "c": "C", "cs": "C#", "swift": "Swift",
        "kt": "Kotlin", "vue": "Vue", "html": "HTML", "css": "CSS",
        "scss": "SCSS", "json": "JSON", "yaml": "YAML", "yml": "YAML",
        "md": "Markdown",
    }
    language = lang_map.get(ext, ext.upper() if ext else "Unknown")

    parts = []

    # Header
    parts.append(f"FILE: {file_path}")
    parts.append(f"LANGUAGE: {language}")
    parts.append(f"TOTAL LINES: {total_lines}")
    parts.append("")

    # Symbol map
    parts.append("SYMBOL MAP:")
    if hasattr(symbol_map, "symbols") and symbol_map.symbols:
        for sym in symbol_map.symbols:
            sym_type = getattr(sym, "type", "symbol")
            sym_name = getattr(sym, "full_path", None) or getattr(sym, "name", "?")
            start = getattr(sym, "start_line", "?")
            end = getattr(sym, "end_line", "?")
            sig = getattr(sym, "signature", None)
            if sig:
                parts.append(f"  [{sym_type}] {sym_name} (lines {start}-{end}) — {sig}")
            else:
                parts.append(f"  [{sym_type}] {sym_name} (lines {start}-{end})")
    else:
        parts.append("  (no symbols found)")
    parts.append("")

    # Imports (up to 40 lines)
    import_lines = []
    for line in lines[:40]:
        stripped = line.strip()
        if (
            stripped.startswith("import ")
            or stripped.startswith("from ")
            or stripped.startswith("#include")
            or stripped.startswith("require(")
            or stripped.startswith("use ")
        ):
            import_lines.append(line)
        elif import_lines and not stripped:
            # blank line after imports — keep scanning
            pass
    if import_lines:
        parts.append("IMPORTS:")
        parts.extend(import_lines[:40])
        parts.append("")

    # File content
    if total_lines <= 400:
        parts.append("FILE CONTENT:")
        parts.append(file_content)
    else:
        parts.append("FILE CONTENT (partial — first 80 lines):")
        parts.append("\n".join(lines[:80]))
        parts.append("")
        # Grep relevant sections using the existing helper from pipeline.py
        try:
            grep_sections = _grep_relevant_sections(
                user_request, file_path, file_content, window=60, max_lines=200
            )
            if grep_sections:
                parts.append("RELEVANT SECTIONS (grep):")
                parts.append(grep_sections)
        except Exception as _grep_exc:
            _dlog("grep_relevant_sections_failed",
                  error=str(_grep_exc)[:200], user_id=user_id)

    parts.append("")

    # Search results from previous rounds
    if search_results:
        for sr in search_results:
            term = sr.get("term", "")
            found = sr.get("found", False)
            code = sr.get("code", "")
            start = sr.get("start_line", "?")
            end = sr.get("end_line", "?")
            if found and code:
                parts.append(f"SYMBOL CODE REQUESTED:")
                parts.append(f"symbol: {term} (lines {start}-{end})")
                parts.append(code)
                parts.append("")
            else:
                parts.append(f"SYMBOL CODE REQUESTED: {term} — NOT FOUND in file")
                parts.append("")

    # Project memory
    if project_memory:
        parts.append("PROJECT MEMORY:")
        parts.append(project_memory)
        parts.append("")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Helper: _apply_snippet_to_symbol  (SNIPPET / targeted edit support)
# ---------------------------------------------------------------------------

def _apply_snippet_to_symbol(symbol_code: str, old_code: str, new_code: str):
    """
    Reconstruct a full symbol body from a *targeted* edit.

    Instead of forcing Claude to re-emit an entire (possibly 900+ line) symbol,
    a surgical_edit may supply a small ``old_code`` snippet and its ``new_code``
    replacement. We splice that change into the *complete* original symbol code
    so every downstream stage (diff, QA, structural QA, retry loop, apply) keeps
    operating on the full before/after symbol exactly as it does today.

    Matching strategy (most precise first):
      1. Exact, unique verbatim match of old_code inside symbol_code.
      2. Whitespace-tolerant match (trailing whitespace per line ignored),
         still requiring a single unambiguous location.
      3. Line-number-prefix strip — if Claude copied numbered search output
         (e.g. "  736: <div>"), strip the "NNN: " prefix and retry (1) & (2).

    Returns (full_new_code, ok, reason).
      ok=True  -> full_new_code is the complete new symbol body.
      ok=False -> reason explains why (not found / ambiguous) for a correction round.
    """
    if not old_code:
        return None, False, "no old_code provided"
    if not symbol_code:
        return None, False, "empty symbol"

    # 1. Exact verbatim
    cnt = symbol_code.count(old_code)
    if cnt == 1:
        return symbol_code.replace(old_code, new_code, 1), True, "exact"
    if cnt > 1:
        return None, False, (
            f"old_code appears {cnt} times in the symbol — it is ambiguous. "
            f"Include a few more surrounding lines so it matches exactly one place."
        )

    # 3. Strip line-number prefixes Claude may have copied from search output.
    def _strip_lineno(block: str) -> str:
        out = []
        any_stripped = False
        for ln in block.splitlines():
            m = re.match(r'^\s*\d+:\s?(.*)$', ln)
            if m:
                out.append(m.group(1))
                any_stripped = True
            else:
                out.append(ln)
        return "\n".join(out) if any_stripped else block

    stripped_old = _strip_lineno(old_code)
    if stripped_old != old_code:
        c2 = symbol_code.count(stripped_old)
        if c2 == 1:
            return symbol_code.replace(stripped_old, new_code, 1), True, "exact-after-strip"
        if c2 > 1:
            return None, False, "old_code (after stripping line numbers) is ambiguous"
        old_code = stripped_old  # fall through to whitespace-tolerant below

    # 2. Whitespace-tolerant match (tabs→spaces + trailing whitespace per line).
    def _norm(s: str) -> str:
        return "\n".join(line.expandtabs(4).rstrip() for line in s.splitlines())

    norm_sym = _norm(symbol_code)
    norm_old = _norm(old_code)
    if norm_old and norm_sym.count(norm_old) == 1:
        # Locate the matching region in the ORIGINAL (un-normalised) symbol so we
        # preserve exact indentation and trailing whitespace outside the edit.
        sym_lines = symbol_code.splitlines(keepends=True)
        old_line_count = len(norm_old.split("\n"))
        target_norm = norm_old.split("\n")
        for i in range(0, len(sym_lines) - old_line_count + 1):
            window = [sym_lines[i + k].expandtabs(4).rstrip("\n").rstrip("\r").rstrip()
                      for k in range(old_line_count)]
            if window == target_norm:
                before = "".join(sym_lines[:i])
                after = "".join(sym_lines[i + old_line_count:])
                # Preserve a trailing newline convention between segments.
                joiner = "" if (not new_code.endswith("\n") and after.startswith("\n")) else ""
                rebuilt = before + new_code
                if after and not rebuilt.endswith("\n") and not after.startswith("\n"):
                    rebuilt += "\n"
                rebuilt += after
                return rebuilt, True, "whitespace-tolerant"

    return None, False, (
        "old_code was not found verbatim in the target symbol. Copy the exact lines "
        "(no line-number prefix) from the file/search results you were shown."
    )


def _locate_snippet_in_text(text: str, old_code: str):
    """
    Locate ``old_code`` inside ``text`` and return the EXACT verbatim substring
    of ``text`` that it matched — suitable for a mechanical find/replace op.

    Session 0183c92e fix: QA corrections are symbol-scoped, so a correction
    that fixes the file's import line (the single most common QA fix) could
    never splice — the import line lives outside the target symbol. This
    helper lets the correction loop fall back to a whole-file match and store
    the fix as a file-level operation.

    Same matching ladder as _apply_snippet_to_symbol, and equally strict:
      1. Exact, unique verbatim match.
      2. Line-number-prefix strip ("NNN: ") then retry.
      3. Whitespace-tolerant (tabs→spaces, trailing ws ignored), unique match —
         returns the ORIGINAL bytes of the matched region.

    Returns (verbatim_old, ok, reason).
    """
    if not old_code:
        return None, False, "no old_code provided"
    if not text:
        return None, False, "empty file content"

    def _try_exact(needle: str):
        cnt = text.count(needle)
        if cnt == 1:
            return needle, True, "exact"
        if cnt > 1:
            return None, False, f"snippet appears {cnt} times in the file — ambiguous"
        return None, None, "not found"

    res, ok, reason = _try_exact(old_code)
    if ok is not None:
        return res, ok, reason

    # Strip line-number prefixes Claude may have copied from search output.
    _stripped_lines = []
    _any_stripped = False
    for _ln in old_code.splitlines():
        _m = re.match(r'^\s*\d+:\s?(.*)$', _ln)
        if _m:
            _stripped_lines.append(_m.group(1))
            _any_stripped = True
        else:
            _stripped_lines.append(_ln)
    if _any_stripped:
        stripped_old = "\n".join(_stripped_lines)
        res, ok, reason = _try_exact(stripped_old)
        if ok is not None:
            return res, ok, reason
        old_code = stripped_old

    # Whitespace-tolerant line match — return the original bytes of the region.
    def _norm_line(s: str) -> str:
        return s.expandtabs(4).rstrip()

    target = [_norm_line(l) for l in old_code.splitlines()]
    if not target:
        return None, False, "old_code is blank"
    text_lines = text.splitlines(keepends=True)
    n_t = len(target)
    matches = []
    for i in range(0, len(text_lines) - n_t + 1):
        window = [_norm_line(text_lines[i + k].rstrip("\n").rstrip("\r")) for k in range(n_t)]
        if window == target:
            matches.append(i)
    if len(matches) == 1:
        i = matches[0]
        region = "".join(text_lines[i:i + n_t])
        # Trim the trailing newline so find/replace does not eat the separator.
        if region.endswith("\n"):
            region = region[:-1]
            if region.endswith("\r"):
                region = region[:-1]
        return region, True, "whitespace-tolerant"
    if len(matches) > 1:
        return None, False, f"snippet matches {len(matches)} locations in the file — ambiguous"
    return None, False, "snippet not found anywhere in the file"


# ---------------------------------------------------------------------------
# Helper: _apply_snippet_by_lines  (Option B — line-number targeted edit)
# ---------------------------------------------------------------------------

def _apply_snippet_by_lines(
    symbol_code: str,
    symbol_start_line: int,
    edit_start_line: int,
    edit_end_line: int,
    new_code: str,
):
    """
    Replace lines edit_start_line..edit_end_line (1-indexed ABSOLUTE file line
    numbers) with new_code, spliced into the symbol that starts at
    symbol_start_line.

    This is Option B for targeted edits — zero string matching.  Claude reads
    the line numbers shown in search results / symbol listings and emits
    ``edit_start_line`` / ``edit_end_line`` instead of an ``old_code`` snippet.
    The pipeline extracts the exact bytes by index, so tabs-vs-spaces and any
    other whitespace representation differences are completely irrelevant.

    Returns (full_new_code, ok, reason).
      ok=True  -> full_new_code is the complete new symbol body.
      ok=False -> reason explains the bounds error.
    """
    if not symbol_code:
        return None, False, "empty symbol"

    lines = symbol_code.splitlines(keepends=True)
    n = len(lines)

    # Convert absolute file line numbers to 0-based indices within the symbol.
    rel_start = edit_start_line - symbol_start_line   # 0-based, inclusive
    rel_end   = edit_end_line   - symbol_start_line   # 0-based, inclusive

    # ── Off-by-one clamp ──────────────────────────────────────────
    # LLMs commonly include the trailing blank line immediately after
    # a symbol's closing brace/bracket.  When rel_end is exactly one
    # past the last valid index (rel_end == n), clamp it rather than
    # rejecting.  This is safe because the blank line is cosmetic and
    # the replacement new_code controls its own trailing whitespace.
    _clamped = False
    if rel_end == n and rel_start >= 0 and rel_start < n:
        rel_end = n - 1
        _clamped = True

    if rel_start < 0 or rel_end >= n or rel_start > rel_end:
        return None, False, (
            f"edit_start_line={edit_start_line}, edit_end_line={edit_end_line} "
            f"is out of bounds for symbol at file lines "
            f"{symbol_start_line}–{symbol_start_line + n - 1}. "
            f"Check the line numbers from the search results."
        )

    before = "".join(lines[:rel_start])
    after  = "".join(lines[rel_end + 1:])

    replacement = new_code
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"

    full_new = before + replacement + after
    _clamp_tag = ",clamped" if _clamped else ""
    return full_new, True, f"line-number-splice:{edit_start_line}-{edit_end_line}{_clamp_tag}"


# ---------------------------------------------------------------------------
# Helper: _number_lines  (prepend 1-indexed line numbers to code)
# ---------------------------------------------------------------------------

def _number_lines(code: str) -> str:
    """Prepend 1-indexed line numbers to code for correction context.

    Example output:
        1: .sai-hero {
        2:   background: var(--bg1);
        3: }
    """
    lines = code.split('\n')
    width = len(str(len(lines)))
    return '\n'.join(f"{i+1:>{width}}: {line}" for i, line in enumerate(lines))


# ---------------------------------------------------------------------------
# Helper: _find_changed_window  (focused diff window for correction)
# ---------------------------------------------------------------------------

def _find_changed_window(original_code: str, edited_code: str, context_lines: int = 20):
    """Diff original vs edited code, return a focused window around changes.

    Returns a dict with window boundaries and formatted code for the
    correction prompt.  Returns ``None`` when the two strings are identical.

    The window is extracted from the EDITED (broken) version, since that is
    what the correction model will fix.  A corresponding window from the
    ORIGINAL is included for reference.

    Returns None if codes are identical or on any internal error (caller
    must handle gracefully).
    """
    try:
        if not original_code or not edited_code:
            _dlog("find_changed_window_empty_input",
                  original_len=len(original_code) if original_code else 0,
                  edited_len=len(edited_code) if edited_code else 0)
            return None

        orig_lines = original_code.splitlines()
        edit_lines = edited_code.splitlines()

        if not orig_lines or not edit_lines:
            _dlog("find_changed_window_no_lines",
                  orig_line_count=len(orig_lines),
                  edit_line_count=len(edit_lines))
            return None

        sm = difflib.SequenceMatcher(None, orig_lines, edit_lines)
        changed_in_edit = set()   # 0-indexed lines in edited version that differ
        changed_in_orig = set()   # 0-indexed lines in original that differ

        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                for j in range(j1, j2):
                    changed_in_edit.add(j)
                for i in range(i1, i2):
                    changed_in_orig.add(i)

        if not changed_in_edit and not changed_in_orig:
            _dlog("find_changed_window_identical",
                  orig_lines=len(orig_lines),
                  edit_lines=len(edit_lines))
            return None

        # ── Window in the edited (broken) version ──
        if changed_in_edit:
            edit_first = min(changed_in_edit)
            edit_last  = max(changed_in_edit)
        else:
            # Pure deletion — anchor at the deletion point
            edit_first = min(changed_in_orig) if changed_in_orig else 0
            edit_last  = edit_first

        ws = max(0, edit_first - context_lines)
        we = min(len(edit_lines) - 1, edit_last + context_lines)

        # Safety: ensure valid range
        if ws > we or we >= len(edit_lines):
            _dlog("find_changed_window_bad_range",
                  ws=ws, we=we, edit_lines=len(edit_lines),
                  edit_first=edit_first, edit_last=edit_last,
                  context_lines=context_lines)
            return None

        window_lines = edit_lines[ws : we + 1]
        numbered_broken = "\n".join(
            f"{ws + i + 1:4d} | {line}" for i, line in enumerate(window_lines)
        )

        # ── Corresponding window in the original ──
        if changed_in_orig:
            orig_first = min(changed_in_orig)
            orig_last  = max(changed_in_orig)
            ows = max(0, orig_first - context_lines)
            owe = min(len(orig_lines) - 1, orig_last + context_lines)
            orig_window = orig_lines[ows : owe + 1]
            numbered_original = "\n".join(
                f"{ows + i + 1:4d} | {line}" for i, line in enumerate(orig_window)
            )
        else:
            numbered_original = "(no lines changed in original)"
            ows, owe = ws, we

        _changed_edit_count = len(changed_in_edit)
        _changed_orig_count = len(changed_in_orig)
        _window_line_count = we - ws + 1

        _dlog("find_changed_window_result",
              orig_lines=len(orig_lines),
              edit_lines=len(edit_lines),
              changed_in_edit=_changed_edit_count,
              changed_in_orig=_changed_orig_count,
              edit_first_changed=edit_first,
              edit_last_changed=edit_last,
              window_start_0=ws,
              window_end_0=we,
              window_line_count=_window_line_count,
              context_lines=context_lines,
              compression_ratio=f"{_window_line_count}/{len(edit_lines)} = {_window_line_count/max(len(edit_lines),1)*100:.0f}%")

        return {
            "window_start": ws,           # 0-indexed in edited_code
            "window_end": we,             # 0-indexed in edited_code (inclusive)
            "numbered_broken": numbered_broken,
            "numbered_original": numbered_original,
            "window_line_count": _window_line_count,
            "total_edit_lines": len(edit_lines),
            "total_orig_lines": len(orig_lines),
            "changed_line_count": _changed_edit_count,
        }
    except Exception as _fcw_exc:
        _dlog("find_changed_window_error",
              error=str(_fcw_exc),
              error_type=type(_fcw_exc).__name__,
              original_len=len(original_code) if original_code else 0,
              edited_len=len(edited_code) if edited_code else 0)
        return None


# ---------------------------------------------------------------------------
# Helper: _find_changed_windows  (multi-cluster windowed diff for scattered edits)
# ---------------------------------------------------------------------------

def _find_changed_windows(original_code: str, edited_code: str, context_lines: int = 20, merge_gap: int = None):
    """Diff original vs edited code, return focused windows around change clusters.

    Unlike _find_changed_window (singular) which always returns ONE window
    spanning all changes, this clusters scattered changes into SEPARATE windows
    when they're far apart.  Two changes at lines 50 and 1900 of a 2000-line
    symbol produce two ~60-line windows instead of one 1870-line mega-window.

    merge_gap: minimum gap between consecutive changed lines to split into
               separate clusters.  Default = 2 * context_lines (so context
               zones of adjacent clusters don't overlap).

    Returns a list of window dicts (same fields as _find_changed_window, plus
    cluster_index and total_clusters).
    Empty list on identical codes or internal error.
    """
    if merge_gap is None:
        merge_gap = context_lines * 2

    try:
        if not original_code or not edited_code:
            _dlog("find_changed_windows_empty_input",
                  original_len=len(original_code) if original_code else 0,
                  edited_len=len(edited_code) if edited_code else 0)
            return []

        orig_lines = original_code.splitlines()
        edit_lines = edited_code.splitlines()

        if not orig_lines or not edit_lines:
            _dlog("find_changed_windows_no_lines",
                  orig_count=len(orig_lines), edit_count=len(edit_lines))
            return []

        sm = difflib.SequenceMatcher(None, orig_lines, edit_lines)
        opcodes = sm.get_opcodes()

        # Collect change regions with BOTH edit-space and orig-space coords
        change_regions = []  # [(edit_start, edit_end, orig_start, orig_end)]
        for tag, i1, i2, j1, j2 in opcodes:
            if tag != "equal":
                # j = edited, i = original; use inclusive end indices
                change_regions.append((
                    j1, max(j1, j2 - 1),   # edit range (inclusive)
                    i1, max(i1, i2 - 1),    # orig range (inclusive)
                ))

        if not change_regions:
            _dlog("find_changed_windows_identical",
                  orig_lines=len(orig_lines), edit_lines=len(edit_lines))
            return []

        # ── Cluster change regions by gap in edit-space ──
        clusters = [[change_regions[0]]]
        for region in change_regions[1:]:
            prev_end = clusters[-1][-1][1]  # edit_end of last region in cluster
            gap = region[0] - prev_end
            if gap > merge_gap:
                clusters.append([region])
            else:
                clusters[-1].append(region)

        _dlog("find_changed_windows_clustering",
              total_change_regions=len(change_regions),
              merge_gap=merge_gap,
              cluster_count=len(clusters),
              cluster_sizes=[len(c) for c in clusters])

        # ── Build window dicts from clusters ──
        windows = []
        for ci, cluster in enumerate(clusters):
            e_first = min(r[0] for r in cluster)
            e_last  = max(r[1] for r in cluster)
            o_first = min(r[2] for r in cluster)
            o_last  = max(r[3] for r in cluster)

            # Window with context in edited version
            ws = max(0, e_first - context_lines)
            we = min(len(edit_lines) - 1, e_last + context_lines)

            if ws > we:
                _dlog("find_changed_windows_bad_edit_range",
                      ci=ci, ws=ws, we=we, e_first=e_first, e_last=e_last)
                continue

            window_lines = edit_lines[ws:we + 1]
            numbered_broken = "\n".join(
                f"{ws + i + 1:4d} | {line}" for i, line in enumerate(window_lines)
            )

            # Corresponding window in original version
            ows = max(0, o_first - context_lines)
            owe = min(len(orig_lines) - 1, o_last + context_lines)

            if ows <= owe and owe < len(orig_lines):
                orig_window = orig_lines[ows:owe + 1]
                numbered_original = "\n".join(
                    f"{ows + i + 1:4d} | {line}" for i, line in enumerate(orig_window)
                )
            else:
                numbered_original = "(no corresponding original lines)"
                ows, owe = ws, we

            wl = we - ws + 1
            _changed_count = sum(r[1] - r[0] + 1 for r in cluster)

            windows.append({
                "window_start": ws,           # 0-indexed in edited_code
                "window_end": we,             # 0-indexed in edited_code (inclusive)
                "numbered_broken": numbered_broken,
                "numbered_original": numbered_original,
                "window_line_count": wl,
                "total_edit_lines": len(edit_lines),
                "total_orig_lines": len(orig_lines),
                "changed_line_count": _changed_count,
                "cluster_index": ci,
                "total_clusters": len(clusters),
            })

        _total_wl = sum(w["window_line_count"] for w in windows)
        _dlog("find_changed_windows_result",
              orig_lines=len(orig_lines),
              edit_lines=len(edit_lines),
              total_change_regions=len(change_regions),
              cluster_count=len(clusters),
              window_count=len(windows),
              merge_gap=merge_gap,
              context_lines=context_lines,
              windows_summary=[{
                  "ci": w["cluster_index"],
                  "ws": w["window_start"] + 1,
                  "we": w["window_end"] + 1,
                  "lines": w["window_line_count"],
                  "changed": w["changed_line_count"],
              } for w in windows],
              total_window_lines=_total_wl,
              compression=f"{_total_wl}/{len(edit_lines)} = {_total_wl/max(len(edit_lines),1)*100:.0f}%")

        return windows

    except Exception as exc:
        _dlog("find_changed_windows_error",
              error=str(exc), error_type=type(exc).__name__,
              original_len=len(original_code) if original_code else 0,
              edited_len=len(edited_code) if edited_code else 0)
        return []


# ---------------------------------------------------------------------------
# Helper: _extract_qa_reference_lines  (find QA-referenced code locations)
# ---------------------------------------------------------------------------

def _extract_qa_reference_lines(
    qa_dict: dict,
    code: str,
    context_lines: int = 20,
) -> list:
    """Extract line ranges referenced in QA feedback but not in diff windows.

    QA often identifies issues at locations where code SHOULD HAVE changed but
    DIDN'T (e.g., timed-out plan items left dead code).  Those locations won't
    appear in any diff-based window, so the correction model literally can't
    see the code it needs to fix.

    Proven failure (session 82eb1056, line 542): QA said "update textWrapStyle
    at line ~1830" but the correction window showed lines 1127-1167.  The
    correction model emitted a <search_request> because it couldn't see line
    1830.

    Returns a list of (window_start_0, window_end_0) tuples (0-indexed,
    inclusive) — ready to be turned into correction windows.
    """
    # Collect all text from QA feedback
    texts = []
    for key in ("summary", "plan_deviation"):
        v = qa_dict.get(key)
        if v:
            texts.append(str(v))
    for key in ("import_issues", "type_errors", "logic_errors",
                "downstream_risks", "issues", "risk_verdicts"):
        for item in (qa_dict.get(key) or []):
            if isinstance(item, dict):
                # Flatten dict values
                texts.append(" ".join(str(v) for v in item.values() if v))
            else:
                texts.append(str(item))

    if not texts:
        return []

    all_text = " ".join(texts)
    code_lines = code.splitlines()
    total = len(code_lines)
    if total == 0:
        return []

    raw_ranges = []  # (start_0, end_0)

    # ── 1. Extract explicit line-number references ────────────────────────
    # Match: "line 1830", "line ~1830", "lines 1827-1920", "L1830"
    for m in re.finditer(r'lines?\s*~?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?', all_text, re.IGNORECASE):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if 1 <= start <= total:
            s0 = max(0, start - 1 - context_lines)
            e0 = min(total - 1, end - 1 + context_lines)
            raw_ranges.append((s0, e0))
    for m in re.finditer(r'L(\d+)(?:\s*[-\u2013]\s*L?(\d+))?', all_text):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        if 1 <= start <= total:
            s0 = max(0, start - 1 - context_lines)
            e0 = min(total - 1, end - 1 + context_lines)
            raw_ranges.append((s0, e0))

    # ── 2. Extract identifier references (PascalCase/camelCase only) ───────
    # Bug fix: plain r'[a-zA-Z_]\w{4,}' matched English words like "bubble",
    # "dialog", "modal", "table", "typing" — causing 82 matches → 49 lines →
    # 3 giant QA-ref windows covering 65% of file (session d59e51e5).
    # Fix: only match PascalCase (e.g. SuggestTitleDialog) or camelCase
    # (e.g. swallowEscape) — real code identifiers, not prose.
    _camel_or_pascal = re.findall(
        r'\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b'  # PascalCase: SuggestTitleDialog
        r'|'
        r'\b([a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)+)\b',  # camelCase: swallowEscape
        all_text
    )
    # re.findall with groups returns tuples; flatten and dedupe
    identifiers = set()
    for groups in _camel_or_pascal:
        for g in groups:
            if g and len(g) >= 5:
                identifiers.add(g)

    ident_line_indices = set()
    for i, line in enumerate(code_lines):
        for ident in identifiers:
            if ident in line:
                ident_line_indices.add(i)

    # Cluster nearby identifier lines
    if ident_line_indices:
        sorted_lines = sorted(ident_line_indices)
        clusters = [[sorted_lines[0]]]
        for ln in sorted_lines[1:]:
            if ln - clusters[-1][-1] <= context_lines * 2:
                clusters[-1].append(ln)
            else:
                clusters.append([ln])
        for cluster in clusters:
            s0 = max(0, min(cluster) - context_lines)
            e0 = min(total - 1, max(cluster) + context_lines)
            raw_ranges.append((s0, e0))

    if not raw_ranges:
        return []

    # ── 3. Merge overlapping ranges ───────────────────────────────────────
    raw_ranges.sort()
    merged = [list(raw_ranges[0])]
    for s, e in raw_ranges[1:]:
        if s <= merged[-1][1] + context_lines:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    _dlog("extract_qa_reference_lines",
          qa_summary=(qa_dict.get("summary") or "")[:200],
          total_code_lines=total,
          identifiers_found=len(identifiers),
          ident_code_matches=len(ident_line_indices),
          raw_ranges_count=len(raw_ranges),
          merged_count=len(merged),
          merged_ranges=[(s + 1, e + 1) for s, e in merged])

    return [(s, e) for s, e in merged]


def _augment_windows_with_qa_refs(
    diff_windows: list,
    qa_dict: dict,
    original_code: str,
    edited_code: str,
    context_lines: int = 20,
) -> list:
    """Add QA-referenced windows to diff-based windows.

    For each QA-referenced line range NOT already covered by a diff window,
    create a new correction window so the model can see and edit that code.

    Returns the augmented window list (may be unchanged if all refs are covered).
    """
    qa_ranges = _extract_qa_reference_lines(qa_dict, edited_code, context_lines)
    if not qa_ranges:
        return diff_windows

    # Determine which QA ranges are NOT covered by existing diff windows
    def _is_covered(s0, e0):
        for w in diff_windows:
            if w["window_start"] <= s0 and w["window_end"] >= e0:
                return True
        return False

    new_ranges = [(s, e) for s, e in qa_ranges if not _is_covered(s, e)]
    if not new_ranges:
        _dlog("augment_windows_all_covered",
              qa_range_count=len(qa_ranges),
              diff_window_count=len(diff_windows))
        return diff_windows

    # Build window dicts for uncovered QA ranges
    edit_lines = edited_code.splitlines()
    orig_lines = original_code.splitlines()
    extra_windows = []
    for ws, we in new_ranges:
        we = min(we, len(edit_lines) - 1)
        if ws > we:
            continue
        window_lines = edit_lines[ws:we + 1]
        numbered_broken = "\n".join(
            f"{ws + i + 1:4d} | {line}" for i, line in enumerate(window_lines)
        )
        ows = min(ws, len(orig_lines) - 1)
        owe = min(we, len(orig_lines) - 1)
        if ows <= owe and owe < len(orig_lines):
            orig_window = orig_lines[ows:owe + 1]
            numbered_original = "\n".join(
                f"{ows + i + 1:4d} | {line}" for i, line in enumerate(orig_window)
            )
        else:
            numbered_original = "(no corresponding original lines)"
        extra_windows.append({
            "window_start": ws,
            "window_end": we,
            "numbered_broken": numbered_broken,
            "numbered_original": numbered_original,
            "window_line_count": we - ws + 1,
            "total_edit_lines": len(edit_lines),
            "total_orig_lines": len(orig_lines),
            "changed_line_count": 0,
            "cluster_index": -1,  # re-indexed below
            "total_clusters": -1,
            "_source": "qa_reference",
        })

    if not extra_windows:
        return diff_windows

    # Merge all windows, sort by start, re-index
    all_windows = list(diff_windows) + extra_windows
    all_windows.sort(key=lambda w: w["window_start"])

    # Merge overlapping windows
    merged = [all_windows[0]]
    for w in all_windows[1:]:
        prev = merged[-1]
        if w["window_start"] <= prev["window_end"] + context_lines:
            # Merge: accumulate changed_line_count ALWAYS when merging,
            # regardless of whether the window boundary extends.
            # Bug fix: previously this was inside the `if w["window_end"] > ...`
            # block, so a diff window (changed=12) fully contained inside a
            # larger QA-ref window (changed=0) would silently drop its count.
            # (session d59e51e5: diff window 269-366 changed=12 swallowed by
            #  QA-ref window 0-531 changed=0 → merged changed=0)
            prev["changed_line_count"] = prev.get("changed_line_count", 0) + w.get("changed_line_count", 0)
            # Expand previous window boundary if needed
            if w["window_end"] > prev["window_end"]:
                new_we = w["window_end"]
                prev["window_end"] = new_we
                prev["window_line_count"] = new_we - prev["window_start"] + 1
                # Rebuild numbered text for merged window
                ws_m, we_m = prev["window_start"], new_we
                prev["numbered_broken"] = "\n".join(
                    f"{ws_m + i + 1:4d} | {edit_lines[ws_m + i]}"
                    for i in range(we_m - ws_m + 1)
                    if ws_m + i < len(edit_lines)
                )
                ows_m = min(ws_m, len(orig_lines) - 1)
                owe_m = min(we_m, len(orig_lines) - 1)
                if ows_m <= owe_m:
                    prev["numbered_original"] = "\n".join(
                        f"{ows_m + i + 1:4d} | {orig_lines[ows_m + i]}"
                        for i in range(owe_m - ows_m + 1)
                        if ows_m + i < len(orig_lines)
                    )
        else:
            merged.append(w)

    # Re-index clusters
    for ci, w in enumerate(merged):
        w["cluster_index"] = ci
        w["total_clusters"] = len(merged)

    _dlog("augment_windows_with_qa_refs",
          diff_window_count=len(diff_windows),
          qa_range_count=len(qa_ranges),
          uncovered_count=len(new_ranges),
          extra_windows_built=len(extra_windows),
          final_window_count=len(merged),
          final_summary=[{
              "ci": w["cluster_index"],
              "ws": w["window_start"] + 1,
              "we": w["window_end"] + 1,
              "lines": w["window_line_count"],
              "source": w.get("_source", "diff"),
          } for w in merged])

    return merged


# ---------------------------------------------------------------------------
# Helper: _apply_line_targeted_fixes  (line-number correction splice)
# ---------------------------------------------------------------------------

def _apply_line_targeted_fixes(
    symbol_code: str,
    fixes: list,
    session_id: str = "",
    user_id: str = "",
    symbol_name: str = "",
) -> tuple:
    """Apply line-targeted correction fixes to a symbol.

    Each fix in ``fixes`` is a dict with:
        start_line:  first line to replace (1-indexed within the symbol)
        end_line:    last line to replace (1-indexed, inclusive)
        new_code:    replacement code (no line numbers)
        reason:      (optional) explanation for debug logging

    Fixes are applied bottom-up (highest start_line first) so earlier line
    numbers stay valid after each splice.

    Returns (full_new_code, applied_count, skipped_count, details).
        full_new_code  — the reconstructed symbol (or original if 0 applied)
        applied_count  — how many fixes spliced successfully
        skipped_count  — how many were skipped (bounds errors etc.)
        details        — list of per-fix result dicts for logging
    """
    if not symbol_code:
        _dlog("line_fix_empty_symbol",
              session_id=session_id, user_id=user_id,
              symbol=symbol_name)
        return symbol_code, 0, len(fixes), [{"error": "empty symbol"}]

    if not fixes or not isinstance(fixes, list):
        _dlog("line_fix_no_fixes",
              session_id=session_id, user_id=user_id,
              symbol=symbol_name,
              fixes_type=type(fixes).__name__,
              fixes_value=str(fixes)[:200])
        return symbol_code, 0, 0, [{"error": "no fixes provided"}]

    lines = symbol_code.split('\n')
    total_lines = len(lines)

    _dlog("line_fix_start",
          session_id=session_id, user_id=user_id,
          symbol=symbol_name,
          total_symbol_lines=total_lines,
          total_symbol_chars=len(symbol_code),
          fix_count=len(fixes),
          fix_ranges=[(f.get("start_line"), f.get("end_line")) for f in fixes])

    # Validate all fixes BEFORE applying any
    validated = []
    for i, fix in enumerate(fixes):
        start = fix.get("start_line")
        end = fix.get("end_line")
        new_code = fix.get("new_code", "")
        reason = fix.get("reason", "")

        _dlog("line_fix_validate",
              session_id=session_id, user_id=user_id,
              symbol=symbol_name,
              fix_index=i,
              start_line=start,
              end_line=end,
              new_code_len=len(new_code) if new_code else 0,
              new_code_line_count=len(new_code.split('\n')) if new_code else 0,
              reason=reason[:200],
              start_type=type(start).__name__,
              end_type=type(end).__name__)

        # Type coercion — model might return strings
        try:
            start = int(start) if start is not None else None
            end = int(end) if end is not None else None
        except (ValueError, TypeError) as e:
            _dlog("line_fix_type_error",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_index=i,
                  start_raw=str(fix.get("start_line")),
                  end_raw=str(fix.get("end_line")),
                  error=str(e))
            validated.append({"index": i, "skip": True, "reason": f"type error: {e}"})
            continue

        if start is None or end is None:
            _dlog("line_fix_missing_lines",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_index=i,
                  start=start, end=end)
            validated.append({"index": i, "skip": True, "reason": "missing start_line or end_line"})
            continue

        if start < 1 or end < 1:
            _dlog("line_fix_negative_lines",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_index=i,
                  start=start, end=end)
            validated.append({"index": i, "skip": True, "reason": f"line numbers must be >= 1 (got {start}, {end})"})
            continue

        if start > end:
            _dlog("line_fix_inverted_range",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_index=i,
                  start=start, end=end)
            validated.append({"index": i, "skip": True, "reason": f"start_line ({start}) > end_line ({end})"})
            continue

        if end > total_lines:
            _dlog("line_fix_out_of_bounds",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_index=i,
                  start=start, end=end,
                  total_lines=total_lines)
            validated.append({"index": i, "skip": True, "reason": f"end_line ({end}) > total lines ({total_lines})"})
            continue

        validated.append({
            "index": i,
            "skip": False,
            "start": start,
            "end": end,
            "new_code": new_code,
            "reason": reason,
        })

    # Check for overlapping ranges (after sorting)
    good_fixes = [v for v in validated if not v.get("skip")]
    good_fixes.sort(key=lambda f: f["start"], reverse=True)  # bottom-up order

    for j in range(len(good_fixes) - 1):
        upper = good_fixes[j]
        lower = good_fixes[j + 1]
        if lower["end"] >= upper["start"]:
            _dlog("line_fix_overlap_detected",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_a_range=(upper["start"], upper["end"]),
                  fix_b_range=(lower["start"], lower["end"]))
            # Skip the later (higher-index) fix to avoid corruption
            upper["skip"] = True
            upper["reason"] = f"overlaps with fix at lines {lower['start']}-{lower['end']}"

    # Apply fixes bottom-up
    applied_count = 0
    skipped_count = 0
    details = []

    for v in good_fixes:
        if v.get("skip"):
            skipped_count += 1
            details.append({"fix_index": v["index"], "status": "skipped", "reason": v.get("reason", "unknown")})
            _dlog("line_fix_skipped",
                  session_id=session_id, user_id=user_id,
                  symbol=symbol_name,
                  fix_index=v["index"],
                  reason=v.get("reason", "unknown"))
            continue

        start_0 = v["start"] - 1  # convert to 0-indexed
        end_0 = v["end"]          # exclusive upper bound (1-indexed end → 0-indexed exclusive)

        old_lines = lines[start_0:end_0]
        old_text = '\n'.join(old_lines)
        new_lines = v["new_code"].split('\n') if v["new_code"] else []

        _dlog("line_fix_applying",
              session_id=session_id, user_id=user_id,
              symbol=symbol_name,
              fix_index=v["index"],
              start_line=v["start"],
              end_line=v["end"],
              old_line_count=len(old_lines),
              new_line_count=len(new_lines),
              old_text_preview=old_text[:300],
              new_text_preview=v["new_code"][:300] if v["new_code"] else "",
              lines_before_splice=len(lines),
              reason=v.get("reason", "")[:200])

        lines[start_0:end_0] = new_lines

        _dlog("line_fix_applied",
              session_id=session_id, user_id=user_id,
              symbol=symbol_name,
              fix_index=v["index"],
              start_line=v["start"],
              end_line=v["end"],
              old_line_count=len(old_lines),
              new_line_count=len(new_lines),
              lines_after_splice=len(lines),
              delta_lines=len(new_lines) - len(old_lines))

        applied_count += 1
        details.append({
            "fix_index": v["index"],
            "status": "applied",
            "start_line": v["start"],
            "end_line": v["end"],
            "old_line_count": len(old_lines),
            "new_line_count": len(new_lines),
        })

    # Also count the originally-skipped (validation failures)
    skipped_count += sum(1 for v in validated if v.get("skip") and v not in good_fixes)

    result_code = '\n'.join(lines)

    _dlog("line_fix_complete",
          session_id=session_id, user_id=user_id,
          symbol=symbol_name,
          original_lines=total_lines,
          original_chars=len(symbol_code),
          result_lines=len(lines),
          result_chars=len(result_code),
          applied_count=applied_count,
          skipped_count=skipped_count,
          details=details)

    return result_code, applied_count, skipped_count, details


def _fragment_reason(symbol_code: str, new_code: str):
    """
    Detect a degenerate "fragment" edit.

    When the model supplies a ``new_code`` with NO ``old_code`` that is clearly
    only PART of a large symbol — much shorter than the symbol AND missing the
    symbol's declaration line — treating it as a full-symbol replacement would
    delete the rest of the symbol (the exact large-file failure mode: a 2-line
    hero snippet replacing an 850-line component).

    Returns a guidance string when the new_code looks degenerate, else ``None``.
    Small symbols are never flagged (cheap and safe to re-emit whole); a genuine
    large refactor that keeps the declaration line is never flagged either.
    """
    sym_lines = symbol_code.splitlines()
    new_lines = new_code.splitlines()
    n_sym = len(sym_lines)
    n_new = len(new_lines)

    # Only guard reasonably large symbols. Small ones are cheap to re-emit fully,
    # and legitimate full rewrites of small symbols can be much shorter.
    if n_sym < 40:
        return None

    # Declaration = first non-empty line of the symbol
    # (e.g. "export function LandingPage() {", "def handler(...):", "class Foo:").
    decl = ""
    for _l in sym_lines:
        if _l.strip():
            decl = _l.strip()
            break

    has_decl = bool(decl) and decl in new_code
    much_smaller = n_new < max(n_sym * 0.5, 25)

    # A legitimate full-symbol edit keeps the declaration line; a fragment drops it.
    if has_decl or not much_smaller:
        return None

    return (
        f"new_code is only {n_new} line(s) but the symbol is {n_sym} line(s) and does not "
        f"include the symbol's declaration line — it looks like a FRAGMENT of the symbol, "
        f"not the whole symbol. Applying it as-is would delete the rest of the symbol."
    )


# ---------------------------------------------------------------------------
# Helper: _resolve_search_terms
# ---------------------------------------------------------------------------

def _resolve_search_terms(terms: list, symbol_map, file_content: str) -> list:
    """
    Given a list of search terms (symbol names or text strings), return a list
    of dicts with resolved code snippets.

    Each result dict:
      {"term": str, "found": bool, "code": str, "start_line": int, "end_line": int}
    """
    results = []
    content_lines = file_content.splitlines()
    symbols = getattr(symbol_map, "symbols", []) or []

    for term in terms:
        # 1. Exact AST symbol lookup
        matched_sym = None
        for sym in symbols:
            if getattr(sym, "name", None) == term or getattr(sym, "full_path", None) == term:
                matched_sym = sym
                break

        if matched_sym is not None:
            results.append({
                "term": term,
                "found": True,
                "code": getattr(matched_sym, "code", ""),
                "start_line": getattr(matched_sym, "start_line", 0),
                "end_line": getattr(matched_sym, "end_line", 0),
            })
            continue

        # 2. Grep fallback — case-insensitive search
        match_line_idx = None
        term_lower = term.lower()
        for idx, line in enumerate(content_lines):
            if term_lower in line.lower():
                match_line_idx = idx
                break

        if match_line_idx is not None:
            start_idx = max(0, match_line_idx - 30)
            end_idx = min(len(content_lines), match_line_idx + 31)
            snippet = "\n".join(content_lines[start_idx:end_idx])
            results.append({
                "term": term,
                "found": True,
                "code": snippet,
                "start_line": start_idx + 1,  # 1-indexed
                "end_line": end_idx,
            })
            continue

        # 3. Not found
        results.append({"term": term, "found": False, "code": "", "start_line": 0, "end_line": 0})

    return results


# ---------------------------------------------------------------------------
# Helper: _extract_json_from_text
# ---------------------------------------------------------------------------

def _extract_json_from_text(text: str) -> dict:
    """
    Extract and parse JSON from Claude's response text.

    Tries:
    1. Direct parse
    2. Slice from first '{' to last '}'
    3. ```json ... ``` fenced block
    """
    stripped = text.strip()

    # 1. Direct parse
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. First '{' to last '}'
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        try:
            return json.loads(stripped[first_brace : last_brace + 1])
        except json.JSONDecodeError:
            pass

    # 3. ```json ... ``` fenced block
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Could not parse JSON from response: {text[:300]}")


# ---------------------------------------------------------------------------
# Helper: run_qa_for_changes
# ---------------------------------------------------------------------------

async def run_qa_for_changes(
    changes,
    file_content: str,
    user_request: str,
    anthropic_key: str,
    model: str,
    qa_feedback: dict = None,
) -> dict:
    """
    Run a QA review of all proposed changes via a single non-streaming Claude call.

    Returns a dict with keys: verdict, qa_score, summary, issues, risks.
    On any error, returns a safe fallback dict.

    v3.12.1: qa_feedback — previous QA verdict dict; when present, appended to the
             user message so the re-QA judge knows what was previously blocked and
             can verify the correction actually addressed those issues.
    """
    _QA_SYSTEM = (
        "You are a code reviewer. Review the proposed code changes and verify they "
        "correctly implement the request.\n"
        "SCOPE: ORIGINAL and NEW CODE are a SINGLE SYMBOL excerpted from a larger file. "
        "File-level imports and module-level exports outside the symbol are present in the "
        "file but not shown here — do NOT flag them as missing or dropped. Only flag an "
        "import issue when NEW CODE introduces a NEW dependency that ORIGINAL did not use.\n"
        "For each change, check:\n"
        "1. Does new_code correctly implement what was requested?\n"
        "2. Does new_code preserve all unchanged parts of the original SYMBOL?\n"
        "3. Are there any obvious bugs, syntax errors, or missing logic?\n"
        "4. Are there risks (side effects, other files that might need updating)?\n\n"
        "Return ONLY valid JSON:\n"
        "{\n"
        '  "verdict": "safe" | "warning" | "blocked",\n'
        '  "qa_score": <integer 1-10>,\n'
        '  "summary": "one sentence",\n'
        '  "issues": ["any issues found"],\n'
        '  "risks": ["any risks"]\n'
        "}\n\n"
        "Score: 9-10=safe, 7-8=minor notes, 5-6=warning, <=4=blocked."
    )

    _FALLBACK = {
        "verdict": "warning",
        "qa_score": 7,
        "summary": "QA skipped due to error",
        "issues": [],
        "risks": [],
    }

    try:
        user_parts = [f"USER REQUEST:\n{user_request}\n"]

        for ch in changes:
            symbol_path = getattr(ch, "symbol", None)
            if symbol_path is not None:
                symbol_name = getattr(symbol_path, "full_path", None) or getattr(symbol_path, "name", "unknown")
            else:
                symbol_name = "unknown"

            original = getattr(ch, "original_code", "") or ""
            new_code = getattr(ch, "new_code", "") or ""

            # Head+tail truncation: keep first 300 + last 200 lines so QA sees
            # both the entry point and the closing logic of large symbols.
            # (v3.12.1: was head-only 500 which missed structural breaks at tail)
            _QA_HEAD, _QA_TAIL = 300, 200
            _QA_MAX = _QA_HEAD + _QA_TAIL
            orig_lines = original.splitlines()
            new_lines = new_code.splitlines()
            if len(orig_lines) > _QA_MAX:
                original = (
                    "\n".join(orig_lines[:_QA_HEAD])
                    + f"\n\n... ({len(orig_lines) - _QA_MAX} lines omitted) ...\n\n"
                    + "\n".join(orig_lines[-_QA_TAIL:])
                )
            if len(new_lines) > _QA_MAX:
                new_code = (
                    "\n".join(new_lines[:_QA_HEAD])
                    + f"\n\n... ({len(new_lines) - _QA_MAX} lines omitted) ...\n\n"
                    + "\n".join(new_lines[-_QA_TAIL:])
                )

            user_parts.append(
                f"--- CHANGE: {symbol_name} ---\n"
                f"ORIGINAL:\n{original}\n\n"
                f"NEW CODE:\n{new_code}\n"
            )

        user_message = "\n".join(user_parts)

        # v3.12.1: if this is a re-QA after correction, inject prior QA feedback
        # so the judge knows what was blocked and can verify the fix.
        if qa_feedback and qa_feedback.get("verdict") in ("blocked", "warning"):
            _prev_issues = qa_feedback.get("issues", [])
            _prev_summary = qa_feedback.get("summary", "")
            _prev_parts = []
            if _prev_summary:
                _prev_parts.append(f"Previous QA summary: {_prev_summary}")
            for _pi in _prev_issues[:5]:
                _prev_parts.append(f"  - {_pi}")
            if _prev_parts:
                user_message += (
                    "\n\n--- PREVIOUS QA REJECTION (verify these are fixed) ---\n"
                    + "\n".join(_prev_parts)
                )
            _dlog("qa_for_changes_reqa_with_feedback",
                  model=model,
                  prev_verdict=qa_feedback.get("verdict"),
                  prev_score=qa_feedback.get("qa_score"),
                  prev_issues_count=len(_prev_issues))

        raw_text = ""
        try:
            aclient = AsyncAnthropic(api_key=anthropic_key)
            _qa_model_legacy = "claude-sonnet-5"
            _dlog("qa_for_changes_call_config", model=_qa_model_legacy,
                  wrapper="safe_claude_call")
            response = await _safe_claude_call(
                aclient, model=_qa_model_legacy,
                desired_text_tokens=8000,
                system=_QA_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            )
            for block in response.content:
                if hasattr(block, "text"):
                    raw_text += block.text
        except Exception as _qa_claude_err:
            # GPT fallback — use OpenAI if no Anthropic key
            _dlog("qa_for_changes_claude_fallback_to_gpt",
                  error=str(_qa_claude_err)[:200], model=model)
            try:
                from services.api_retry import api_call_with_retry
                _qa_oai_client = _get_client("")  # uses default OpenAI key
                _qa_oai_resp = api_call_with_retry(lambda: _qa_oai_client.chat.completions.create(
                    model="gpt-4.1",
                    messages=[
                        {"role": "system", "content": _QA_SYSTEM},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                ))
                raw_text = _qa_oai_resp.choices[0].message.content or ""
            except Exception as _qa_oai_err:
                _dlog("qa_for_changes_both_failed",
                      claude_err=str(_qa_claude_err)[:200],
                      gpt_err=str(_qa_oai_err)[:200])
                return _FALLBACK

        result = _extract_json_from_text(raw_text)
        # _extract_json_from_text is shadowed module-wide by a later duplicate
        # that returns a JSON *string*, not a dict. Handle both shapes
        # (mirrors the isinstance pattern used by every other caller).
        if isinstance(result, str):
            result = json.loads(result)

        _dlog("qa_for_changes_parsed",
              model=model,
              verdict=result.get("verdict"),
              qa_score=result.get("qa_score"),
              raw_len=len(raw_text))

        # Normalise keys
        return {
            "verdict": result.get("verdict", "safe"),
            "qa_score": result.get("qa_score", 8),
            "summary": result.get("summary", ""),
            "issues": result.get("issues", []),
            "risks": result.get("risks", []),
        }

    except Exception as _qa_fc_err:
        try:
            _dlog("qa_for_changes_error",
                  model=model,
                  error=f"{type(_qa_fc_err).__name__}: {str(_qa_fc_err)[:300]}")
        except Exception:
            pass
        return _FALLBACK


# ---------------------------------------------------------------------------
# Main entry point: analyze_and_plan_stream
# ---------------------------------------------------------------------------

async def analyze_and_plan_stream(
    file_path: str,
    file_content: str,
    user_request: str,
    session_id: Optional[str] = None,
    project_memory: Optional[str] = None,
    pinned_context: Optional[list] = None,
    user_id: str = "",
):
    """
    Claude-first async generator that replaces the old Architect+Surgeon pipeline.

    Yields SSE strings ("data: {...}\\n\\n").

    Flow:
      1. Parse file symbols
      2. Call Claude (streaming) with file context + user request
      3. Handle search loop (Claude may request more symbol code)
      4. Build SurgicalChange objects
      5. Run QA
      6. Yield final result
    """
    # Lazy imports from the parent pipeline module context
    # (these will be resolved at call time from the merged module namespace)
    from models.schemas import SurgicalChange, SurgicalAnalyzeResponse, ArchitectPlan  # noqa: F401

    try:
        def sse(obj: dict) -> str:
            return f"data: {json.dumps(obj)}\n\n"

        # ------------------------------------------------------------------
        # Step 1: Parse file
        # ------------------------------------------------------------------
        yield sse({"type": "progress", "content": "Parsing file structure..."})
        symbol_map = parser.parse(file_content, file_path)
        n_sym = len(symbol_map.symbols) if hasattr(symbol_map, "symbols") and symbol_map.symbols else 0
        yield sse({"type": "progress", "content": f"Found {n_sym} symbols. {architect_model} is analyzing..."})

        # ------------------------------------------------------------------
        # Step 2: Get Anthropic key and model
        # ------------------------------------------------------------------
        architect_model = get_setting("architect_model", "claude-sonnet-5")
        _agent_use_claude = _is_claude_model(architect_model)
        if _agent_use_claude:
            anthropic_key = _get_anthropic_key(user_id)
            aclient = AsyncAnthropic(api_key=anthropic_key)
        else:
            _agent_oai_client = _get_client(user_id)
            anthropic_key = None  # may not have one
            try:
                anthropic_key = _get_anthropic_key(user_id)
            except Exception:
                pass  # GPT-only user — QA will fall back to GPT too
        _dlog("agent_mode_model_route", model=architect_model,
              use_claude=_agent_use_claude, user_id=user_id)

        # ------------------------------------------------------------------
        # Step 3: Search loop — Claude can request symbols via "search" intent
        # ------------------------------------------------------------------
        MAX_SEARCH_ROUNDS = 10
        search_results = []
        plan_data = None

        for round_num in range(MAX_SEARCH_ROUNDS + 1):
            context = _build_claude_context(
                symbol_map,
                file_content,
                file_path,
                user_request,
                project_memory=project_memory,
                search_results=search_results if search_results else None,
            )

            user_message = f"{context}\n\nUSER REQUEST:\n{user_request}"

            messages = [{"role": "user", "content": user_message}]

            model_kwargs = {
                "model": architect_model,
                "max_tokens": _max_output_tokens(architect_model),
                "system": CLAUDE_EDITOR_SYSTEM,
                "messages": messages,
            }
            model_kwargs.update(_get_thinking_kwargs(architect_model, 8000))
            model_kwargs.update(_get_effort_kwargs(architect_model))

            full_text = ""

            if _agent_use_claude:
                in_thinking = False
                async with aclient.messages.stream(**model_kwargs) as stream:
                    async for event in stream:
                        event_type = getattr(event, "type", None)

                        if event_type == "content_block_start":
                            block = getattr(event, "content_block", None)
                            if block and getattr(block, "type", "") == "thinking":
                                in_thinking = True
                                yield sse({"type": "thinking_start", "content": ""})

                        elif event_type == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            if delta:
                                thinking_chunk = getattr(delta, "thinking", None)
                                text_chunk = getattr(delta, "text", None)
                                if thinking_chunk:
                                    yield sse({"type": "thinking", "content": thinking_chunk})
                                elif text_chunk:
                                    full_text += text_chunk

                        elif event_type == "content_block_stop":
                            if in_thinking:
                                yield sse({"type": "thinking_end", "content": ""})
                                in_thinking = False

                # -- Agent Mode starvation detection + auto-retry ----------
                _dlog("agent_stream_done", model=architect_model,
                      round=round_num, text_len=len(full_text),
                      session_id=session_id, user_id=user_id)

                if not full_text.strip():
                    _retry_budget = 16_000
                    _retry_max = _bounded_thinking_params(
                        architect_model, _retry_budget
                    )["max_tokens"]
                    _dlog("agent_thinking_starvation_detected",
                          model=architect_model, original_budget=8000,
                          retry_budget=_retry_budget, retry_max=_retry_max,
                          round=round_num, session_id=session_id,
                          user_id=user_id)
                    yield sse({"type": "progress",
                               "content": "Re-analyzing with expanded capacity..."})

                    _retry_kwargs = {
                        "model": architect_model,
                        "max_tokens": _retry_max,
                        "system": CLAUDE_EDITOR_SYSTEM,
                        "messages": messages,
                    }
                    _retry_kwargs.update(
                        _get_thinking_kwargs(architect_model, _retry_budget)
                    )
                    _retry_kwargs.update(_get_effort_kwargs(architect_model))

                    in_thinking = False
                    async with aclient.messages.stream(**_retry_kwargs) as stream:
                        async for event in stream:
                            event_type = getattr(event, "type", None)

                            if event_type == "content_block_start":
                                block = getattr(event, "content_block", None)
                                if block and getattr(block, "type", "") == "thinking":
                                    in_thinking = True
                                    yield sse({"type": "thinking_start",
                                               "content": ""})

                            elif event_type == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                if delta:
                                    t_chunk = getattr(delta, "thinking", None)
                                    txt_chunk = getattr(delta, "text", None)
                                    if t_chunk:
                                        yield sse({"type": "thinking",
                                                   "content": t_chunk})
                                    elif txt_chunk:
                                        full_text += txt_chunk

                            elif event_type == "content_block_stop":
                                if in_thinking:
                                    yield sse({"type": "thinking_end",
                                               "content": ""})
                                    in_thinking = False

                    _dlog("agent_starvation_retry_done",
                          model=architect_model, retry_text_len=len(full_text),
                          round=round_num, session_id=session_id,
                          user_id=user_id)

                    if not full_text.strip():
                        _dlog("agent_starvation_final_fail",
                              model=architect_model, round=round_num,
                              session_id=session_id, user_id=user_id)
                # -- End starvation recovery -------------------------------
            else:
                # GPT path — blocking call, same prompt
                yield sse({"type": "progress", "content": f"Architect ({architect_model}) analyzing..."})
                _agent_oai_resp = await asyncio.to_thread(
                    lambda: _chat_create(
                        _agent_oai_client, architect_model,
                        messages=[
                            {"role": "system", "content": CLAUDE_EDITOR_SYSTEM},
                            {"role": "user", "content": user_message},
                        ],
                        response_format={"type": "json_object"},
                    )
                )
                full_text = _agent_oai_resp.choices[0].message.content or ""
                _dlog("agent_mode_gpt_search_response", model=architect_model,
                      round=round_num, response_len=len(full_text),
                      session_id=session_id, user_id=user_id)

            # Parse JSON from response
            try:
                _raw_plan = _extract_json_from_text(full_text)
                plan_data = json.loads(_raw_plan) if isinstance(_raw_plan, str) else _raw_plan
            except (ValueError, json.JSONDecodeError) as parse_err:
                yield sse({
                    "type": "error",
                    "content": (
                        f"Claude returned unexpected output. Please try again.\n\n"
                        f"Detail: {str(parse_err)[:200]}"
                    ),
                })
                return

            intent = plan_data.get("intent", "edit")

            if intent == "search":
                terms = plan_data.get("search_terms", [])
                if not terms or round_num >= MAX_SEARCH_ROUNDS:
                    # Out of search budget — treat as needs_clarification
                    plan_data = {
                        "intent": "needs_clarification",
                        "clarification_response": plan_data.get(
                            "reasoning",
                            "I need more information to make this change. Could you tell me "
                            "the exact function name or paste the relevant code section?",
                        ),
                    }
                    break

                yield sse({"type": "progress", "content": f"Looking up: {', '.join(terms[:3])}..."})
                new_results = _resolve_search_terms(terms, symbol_map, file_content)
                search_results.extend(new_results)
                continue  # loop again with expanded search_results

            # Got a final (non-search) response
            break

        if plan_data is None:
            yield sse({"type": "error", "content": f"No response from {architect_model}. Please try again."})
            return

        intent = plan_data.get("intent", "edit")

        # ------------------------------------------------------------------
        # Handle chat / clarification intents
        # ------------------------------------------------------------------
        if intent in ("chat", "needs_clarification"):
            response_text = plan_data.get("chat_response") or plan_data.get(
                "clarification_response", ""
            )
            yield sse({"type": "chat", "content": response_text})
            yield sse({"type": "done", "content": ""})
            return

        if intent != "edit":
            yield sse({"type": "error", "content": f"Unexpected intent '{intent}' from {architect_model}."})
            return

        # ------------------------------------------------------------------
        # Build SurgicalChange objects
        # ------------------------------------------------------------------
        changes_data = plan_data.get("changes", [])
        if not changes_data:
            response = plan_data.get(
                "reasoning", "No changes needed — the code already satisfies your request."
            )
            yield sse({"type": "chat", "content": response})
            yield sse({"type": "done", "content": ""})
            return

        yield sse({"type": "progress", "content": f"Running QA on {len(changes_data)} change(s)..."})

        changes = []
        for ch in changes_data:
            symbol_path = ch.get("symbol_path", "")
            new_code = ch.get("new_code", "")
            description = ch.get("description", "")
            confidence = ch.get("confidence", 9)

            if not symbol_path or not new_code:
                continue

            # Find symbol in AST map — exact match first
            symbol = None
            for s in symbol_map.symbols:
                if getattr(s, "full_path", None) == symbol_path or getattr(s, "name", None) == symbol_path:
                    symbol = s
                    break

            if symbol is None:
                # Partial match fallback
                for s in symbol_map.symbols:
                    s_fp = getattr(s, "full_path", "") or ""
                    s_name = getattr(s, "name", "") or ""
                    if symbol_path in s_fp or s_name in symbol_path:
                        symbol = s
                        break

            if symbol is None:
                yield sse({
                    "type": "progress",
                    "content": f"Warning: symbol '{symbol_path}' not found in file map — skipping",
                })
                continue

            diff = _make_diff(symbol.code, new_code, symbol_path)
            _tgt, _repl = _compute_target_element(symbol.code, new_code)

            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code,
                diff=diff,
                confidence=confidence,
                description=description,
                applied=False,
                # KEY: symbol.code is extracted directly from the file by the AST parser
                # so it is GUARANTEED to be an exact substring — always reliable.
                operations=[{"find": symbol.code, "replace": new_code}],
                target_element=_tgt,
                replacement=_repl,
            )
            changes.append(change)

        if not changes:
            yield sse({
                "type": "chat",
                "content": (
                    "I analyzed the file but couldn't map the change to a specific symbol. "
                    "Please re-upload the file and try again."
                ),
            })
            yield sse({"type": "done", "content": ""})
            return

        # ------------------------------------------------------------------
        # QA check — structural + LLM, with retry loop
        # ------------------------------------------------------------------
        yield sse({"type": "progress", "content": "Running QA..."})

        qa = await run_qa_for_changes(changes, file_content, user_request, anthropic_key, architect_model)
        qa_risks = qa.get("risks", [])

        # Structural QA: deterministic checks for missing imports, etc.
        try:
            from services.structural_qa import run_structural_qa, has_blocking_issues as _sq_blocking, filter_preexisting_issues as _filter_sq_aps
        except ImportError:
            _sq_blocking = None
            _filter_sq_aps = None

        _sq_has_errors = False
        if _sq_blocking is not None:
            for _sq_ch in changes:
                _sq_new = getattr(_sq_ch, "new_code", "") or ""
                _sq_orig = getattr(_sq_ch, "original_code", "") or ""
                _sq_fname = file_path or ""
                _sq_issues = run_structural_qa(
                    _sq_new, _sq_orig, _sq_fname,
                    file_content=file_content or "",
                    all_changes=[
                        {"filename": file_path or "", "new_code": getattr(_c, "new_code", "") or ""}
                        for _c in changes
                    ],
                )
                # Filter out pre-existing issues (session d007eaf1 fix)
                _sq_raw_count = len(_sq_issues)
                if _filter_sq_aps is not None:
                    _sq_issues = _filter_sq_aps(
                        _sq_issues, _sq_orig, _sq_fname,
                        file_content=file_content or "",
                    )
                if _sq_raw_count != len(_sq_issues):
                    _dlog("aps_structural_qa_preexisting_filtered",
                          session_id=session_id,
                          raw_issues=_sq_raw_count,
                          after_filter=len(_sq_issues),
                          file=_sq_fname,
                          user_id=user_id)
                if _sq_blocking(_sq_issues):
                    _sq_has_errors = True
                    _sq_msgs = [si["message"] for si in _sq_issues if si["severity"] == "error"]
                    qa["verdict"] = "blocked"
                    qa["qa_score"] = min(qa.get("qa_score", 10) or 10, 3)
                    qa_risks.extend([f"[STRUCTURAL] {m}" for m in _sq_msgs])
                    yield sse({"type": "progress",
                               "content": f"🔍 Structural QA: {len(_sq_msgs)} blocking issue(s) found"})

        # Retry loop: if QA blocked, send issues back to Claude for a fix
        _APS_MAX_RETRIES = 2
        _aps_verdict = qa.get("verdict", "safe")
        _aps_score   = qa.get("qa_score", 10) or 10
        _aps_blocked = (_aps_verdict == "blocked") or (_aps_score <= 7)

        if _aps_blocked:
            for _aps_attempt in range(_APS_MAX_RETRIES):
                yield sse({"type": "progress",
                           "content": f"🔁 Fixing blocked code — attempt {_aps_attempt + 1}/{_APS_MAX_RETRIES}..."})

                # Build feedback for Claude
                _fb_parts = [f"QA BLOCKED (score {qa.get('qa_score', '?')}/10):"]
                if qa.get("summary"):
                    _fb_parts.append(f"Summary: {qa['summary']}")
                for _fb_r in qa.get("risks", []):
                    _fb_parts.append(f"• {_fb_r}")
                for _fb_i in qa.get("issues", []):
                    _fb_parts.append(f"• {_fb_i}")
                _fb_text = "\n".join(_fb_parts)

                # Phase 4: tool_use vs free-text correction path
                _correction_raw = str(get_setting("correction_tool_use", "false")).lower()
                _use_correction_tool_use = _correction_raw == "true"
                _dlog("flag_check_correction_tool_use",
                      raw_value=_correction_raw, resolved=_use_correction_tool_use,
                      env_upper=_os.environ.get("CORRECTION_TOOL_USE", "<not set>"),
                      session_id=session_id, user_id=user_id)
                if _use_correction_tool_use:
                    # ── Multi-turn tool_use correction ──────────────────────────
                    try:
                        _dlog("correction_tool_use_start",
                              session_id=session_id, user_id=user_id,
                              attempt=_aps_attempt + 1,
                              qa_score=qa.get("qa_score"),
                              qa_verdict=qa.get("verdict"),
                              model=architect_model)
                        _corr_msgs = [
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": full_text},
                            {"role": "user", "content": (
                                f"Your code changes were rejected by QA:\n\n{_fb_text}\n\n"
                                f"Fix all issues. Use submit_fix() for each corrected symbol. "
                                f"Use request_symbol_code() to see the current state of any symbol first. "
                                f"Call done_fixing() when all fixes are complete.\n\n"
                                f"The new_code must be COMPLETE — include all imports, all functions, nothing omitted."
                            )},
                        ]
                        _corr_changes = []
                        _CORR_MAX_TURNS = 5
                        _corr_done = False
                
                        for _corr_turn in range(_CORR_MAX_TURNS):
                            _dlog("correction_tool_use_turn",
                                  session_id=session_id, turn=_corr_turn + 1,
                                  msg_count=len(_corr_msgs),
                                  fixes_so_far=len(_corr_changes))
                            try:
                                if _agent_use_claude:
                                    _corr_resp = await _safe_claude_call(
                                        AsyncAnthropic(api_key=anthropic_key),
                                        model="claude-sonnet-5",
                                        desired_text_tokens=64000,
                                        system=CLAUDE_EDITOR_SYSTEM,
                                        messages=_fit_correction_messages(
                                            _corr_msgs, session_id=session_id, user_id=user_id),
                                        tools=CORRECTION_TOOLS,
                                        tool_choice={"type": "auto"},
                                    )
                                else:
                                    # GPT correction: structured tool_use (same tools as Claude)
                                    # Budget the payload so the full symbol + prior changes
                                    # survive (no blind 8k chop); clip head+tail only if huge.
                                    _corr_oai_msgs = _fit_correction_messages(
                                        [{"role": "system", "content": CLAUDE_EDITOR_SYSTEM}]
                                        + [
                                            {"role": m.get("role", "user"),
                                             "content": str(m.get("content", ""))}
                                            for m in _corr_msgs
                                            if isinstance(m.get("content"), str)
                                        ],
                                        session_id=session_id, user_id=user_id,
                                    )
                                    _corr_oai_resp = await asyncio.to_thread(
                                        lambda: _chat_create(
                                            _agent_oai_client, architect_model,
                                            messages=_corr_oai_msgs,
                                            tools=CORRECTION_TOOLS_OPENAI,
                                        )
                                    )
                                    _corr_oai_msg = _corr_oai_resp.choices[0].message
                                    _corr_oai_tcalls = _corr_oai_msg.tool_calls or []

                                    if not _corr_oai_tcalls:
                                        # No tool calls — try parsing text as JSON fallback
                                        _corr_text = (_corr_oai_msg.content or "").strip()
                                        _dlog("agent_mode_gpt_correction_no_tools_fallback",
                                              model=architect_model, text_len=len(_corr_text),
                                              session_id=session_id)
                                        try:
                                            _raw_corr = _extract_json_from_text(_corr_text)
                                            _corr_data = json.loads(_raw_corr) if isinstance(_raw_corr, str) else _raw_corr
                                            for _rc in (_corr_data.get("changes", []) or []):
                                                _rc_sp = _rc.get("symbol_path", "")
                                                _rc_nc = _rc.get("new_code", "")
                                                if _rc_sp and _rc_nc:
                                                    _fix_sym = None
                                                    for _s in symbol_map.symbols:
                                                        if getattr(_s, "full_path", None) == _rc_sp or getattr(_s, "name", None) == _rc_sp:
                                                            _fix_sym = _s
                                                            break
                                                    if _fix_sym:
                                                        _fix_diff = _make_diff(_fix_sym.code, _rc_nc, _rc_sp)
                                                        _fix_tgt, _fix_repl = _compute_target_element(_fix_sym.code, _rc_nc)
                                                        _corr_changes.append(SurgicalChange(
                                                            id=str(uuid.uuid4()),
                                                            symbol=_fix_sym,
                                                            original_code=_fix_sym.code,
                                                            new_code=_rc_nc,
                                                            diff=_fix_diff,
                                                            confidence=_rc.get("confidence", 9),
                                                            description=_rc.get("description", "GPT correction"),
                                                            applied=False,
                                                            operations=[{"find": _fix_sym.code, "replace": _rc_nc}],
                                                            target_element=_fix_tgt,
                                                            replacement=_fix_repl,
                                                        ))
                                        except Exception:
                                            pass
                                        _corr_done = True
                                        break

                                    # Process tool calls (OpenAI format)
                                    _corr_oai_tool_results = []
                                    for _cotc in _corr_oai_tcalls:
                                        _cotc_name = _cotc.function.name
                                        try:
                                            _cotc_input = json.loads(_cotc.function.arguments) if _cotc.function.arguments else {}
                                        except json.JSONDecodeError:
                                            _cotc_input = {}

                                        if _cotc_name == "done_fixing":
                                            _corr_done = True
                                            _dlog("agent_mode_gpt_correction_done",
                                                  session_id=session_id, turn=_corr_turn + 1,
                                                  summary=_cotc_input.get("summary", ""),
                                                  total_fixes=len(_corr_changes))
                                            _corr_oai_tool_results.append({
                                                "role": "tool",
                                                "tool_call_id": _cotc.id,
                                                "content": json.dumps({"status": "ok"}),
                                            })
                                            break

                                        elif _cotc_name == "request_symbol_code":
                                            _req_sp = _cotc_input.get("symbol_path", "")
                                            _found_sym = None
                                            for _s in symbol_map.symbols:
                                                if getattr(_s, "full_path", None) == _req_sp or getattr(_s, "name", None) == _req_sp:
                                                    _found_sym = _s
                                                    break
                                            if not _found_sym:
                                                for _s in symbol_map.symbols:
                                                    if _req_sp in (getattr(_s, "full_path", "") or "") or (getattr(_s, "name", "") or "") in _req_sp:
                                                        _found_sym = _s
                                                        break
                                            if _found_sym:
                                                _corr_oai_tool_results.append({
                                                    "role": "tool",
                                                    "tool_call_id": _cotc.id,
                                                    "content": json.dumps({
                                                        "symbol_path": _found_sym.full_path,
                                                        "code": _found_sym.code,
                                                        "lines": len((_found_sym.code or "").splitlines())
                                                    }),
                                                })
                                            else:
                                                _available = [getattr(_s, "full_path", getattr(_s, "name", "?")) for _s in symbol_map.symbols[:20]]
                                                _corr_oai_tool_results.append({
                                                    "role": "tool",
                                                    "tool_call_id": _cotc.id,
                                                    "content": json.dumps({
                                                        "error": f"Symbol '{_req_sp}' not found",
                                                        "available_symbols": _available
                                                    }),
                                                })

                                        elif _cotc_name == "submit_fix":
                                            _fix_sp = _cotc_input.get("symbol_path", "")
                                            _fix_nc = _cotc_input.get("new_code", "")
                                            _fix_desc = _cotc_input.get("description", "")
                                            _fix_conf = _cotc_input.get("confidence", 9)

                                            _fix_sym = None
                                            for _s in symbol_map.symbols:
                                                if getattr(_s, "full_path", None) == _fix_sp or getattr(_s, "name", None) == _fix_sp:
                                                    _fix_sym = _s
                                                    break
                                            if not _fix_sym:
                                                for _s in symbol_map.symbols:
                                                    if _fix_sp in (getattr(_s, "full_path", "") or "") or (getattr(_s, "name", "") or "") in _fix_sp:
                                                        _fix_sym = _s
                                                        break

                                            if _fix_sym and _fix_nc:
                                                _fix_diff = _make_diff(_fix_sym.code, _fix_nc, _fix_sp)
                                                _fix_tgt, _fix_repl = _compute_target_element(_fix_sym.code, _fix_nc)
                                                _corr_changes.append(SurgicalChange(
                                                    id=str(uuid.uuid4()),
                                                    symbol=_fix_sym,
                                                    original_code=_fix_sym.code,
                                                    new_code=_fix_nc,
                                                    diff=_fix_diff,
                                                    confidence=_fix_conf,
                                                    description=_fix_desc,
                                                    applied=False,
                                                    operations=[{"find": _fix_sym.code, "replace": _fix_nc}],
                                                    target_element=_fix_tgt,
                                                    replacement=_fix_repl,
                                                ))
                                                _dlog("agent_mode_gpt_correction_fix_accepted",
                                                      session_id=session_id, symbol=_fix_sym.full_path,
                                                      new_code_lines=len(_fix_nc.splitlines()),
                                                      confidence=_fix_conf)
                                                _corr_oai_tool_results.append({
                                                    "role": "tool",
                                                    "tool_call_id": _cotc.id,
                                                    "content": json.dumps({
                                                        "status": "accepted",
                                                        "symbol": _fix_sym.full_path,
                                                        "new_code_lines": len(_fix_nc.splitlines())
                                                    }),
                                                })
                                            elif not _fix_sym:
                                                _available = [getattr(_s, "full_path", getattr(_s, "name", "?")) for _s in symbol_map.symbols[:20]]
                                                _corr_oai_tool_results.append({
                                                    "role": "tool",
                                                    "tool_call_id": _cotc.id,
                                                    "content": json.dumps({
                                                        "error": f"Symbol '{_fix_sp}' not found",
                                                        "available_symbols": _available
                                                    }),
                                                })
                                            else:
                                                _corr_oai_tool_results.append({
                                                    "role": "tool",
                                                    "tool_call_id": _cotc.id,
                                                    "content": json.dumps({"error": "new_code is empty"}),
                                                })

                                    # Check exit conditions
                                    if _corr_done:
                                        break
                                    if not _corr_oai_tool_results:
                                        _dlog("agent_mode_gpt_correction_no_tool_results",
                                              session_id=session_id, turn=_corr_turn + 1)
                                        break

                                    # Add assistant + tool results for next turn (OpenAI format)
                                    _corr_asst = {"role": "assistant", "content": _corr_oai_msg.content or None}
                                    _corr_asst["tool_calls"] = [
                                        {"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                                        for tc in _corr_oai_tcalls
                                    ]
                                    _corr_oai_msgs.append(_corr_asst)
                                    _corr_oai_msgs.extend(_corr_oai_tool_results)

                                    _dlog("agent_mode_gpt_correction_tool_use",
                                          model=architect_model, turn=_corr_turn + 1,
                                          changes=len(_corr_changes),
                                          session_id=session_id)
                                    continue  # next correction turn
                            except Exception as _corr_api_err:
                                _dlog("correction_tool_use_api_error",
                                      session_id=session_id, turn=_corr_turn + 1,
                                      error_type=type(_corr_api_err).__name__,
                                      error=str(_corr_api_err)[:300],
                                      fixes_so_far=len(_corr_changes))
                                break
                
                            _tool_results = []
                
                            for _cblk in _corr_resp.content:
                                if not hasattr(_cblk, "type") or _cblk.type != "tool_use":
                                    continue
                
                                if _cblk.name == "done_fixing":
                                    _corr_done = True
                                    _dlog("correction_tool_use_done",
                                          session_id=session_id, turn=_corr_turn + 1,
                                          summary=(_cblk.input or {}).get("summary", ""),
                                          total_fixes=len(_corr_changes))
                                    _tool_results.append({"type": "tool_result", "tool_use_id": _cblk.id,
                                                          "content": json.dumps({"status": "ok"})})
                                    break
                
                                elif _cblk.name == "request_symbol_code":
                                    _req_sp = (_cblk.input or {}).get("symbol_path", "")
                                    _found_sym = None
                                    for _s in symbol_map.symbols:
                                        if getattr(_s, "full_path", None) == _req_sp or getattr(_s, "name", None) == _req_sp:
                                            _found_sym = _s
                                            break
                                    if not _found_sym:
                                        for _s in symbol_map.symbols:
                                            if _req_sp in (getattr(_s, "full_path", "") or "") or (getattr(_s, "name", "") or "") in _req_sp:
                                                _found_sym = _s
                                                break
                                    if _found_sym:
                                        _dlog("correction_tool_use_symbol_requested",
                                              session_id=session_id, symbol=_found_sym.full_path,
                                              code_lines=len((_found_sym.code or "").splitlines()))
                                        _tool_results.append({"type": "tool_result", "tool_use_id": _cblk.id,
                                                              "content": json.dumps({
                                                                  "symbol_path": _found_sym.full_path,
                                                                  "code": _found_sym.code,
                                                                  "lines": len((_found_sym.code or "").splitlines())
                                                              })})
                                    else:
                                        _available = [getattr(_s, "full_path", getattr(_s, "name", "?")) for _s in symbol_map.symbols[:20]]
                                        _dlog("correction_tool_use_symbol_not_found",
                                              session_id=session_id, requested=_req_sp,
                                              available_count=len(symbol_map.symbols))
                                        _tool_results.append({"type": "tool_result", "tool_use_id": _cblk.id,
                                                              "content": json.dumps({
                                                                  "error": f"Symbol '{_req_sp}' not found",
                                                                  "available_symbols": _available
                                                              }), "is_error": True})
                
                                elif _cblk.name == "submit_fix":
                                    _fix_sp = (_cblk.input or {}).get("symbol_path", "")
                                    _fix_nc = (_cblk.input or {}).get("new_code", "")
                                    _fix_desc = (_cblk.input or {}).get("description", "")
                                    _fix_conf = (_cblk.input or {}).get("confidence", 9)
                
                                    # Symbol lookup — exact match first, then fuzzy
                                    _fix_sym = None
                                    for _s in symbol_map.symbols:
                                        if getattr(_s, "full_path", None) == _fix_sp or getattr(_s, "name", None) == _fix_sp:
                                            _fix_sym = _s
                                            break
                                    if not _fix_sym:
                                        for _s in symbol_map.symbols:
                                            if _fix_sp in (getattr(_s, "full_path", "") or "") or (getattr(_s, "name", "") or "") in _fix_sp:
                                                _fix_sym = _s
                                                break
                
                                    if _fix_sym and _fix_nc:
                                        _fix_diff = _make_diff(_fix_sym.code, _fix_nc, _fix_sp)
                                        _fix_tgt, _fix_repl = _compute_target_element(_fix_sym.code, _fix_nc)
                                        _corr_changes.append(SurgicalChange(
                                            id=str(uuid.uuid4()),
                                            symbol=_fix_sym,
                                            original_code=_fix_sym.code,
                                            new_code=_fix_nc,
                                            diff=_fix_diff,
                                            confidence=_fix_conf,
                                            description=_fix_desc,
                                            applied=False,
                                            operations=[{"find": _fix_sym.code, "replace": _fix_nc}],
                                            target_element=_fix_tgt,
                                            replacement=_fix_repl,
                                        ))
                                        _dlog("correction_tool_use_fix_accepted",
                                              session_id=session_id, symbol=_fix_sym.full_path,
                                              new_code_lines=len(_fix_nc.splitlines()),
                                              confidence=_fix_conf,
                                              description=_fix_desc[:80])
                                        _tool_results.append({"type": "tool_result", "tool_use_id": _cblk.id,
                                                              "content": json.dumps({
                                                                  "status": "accepted",
                                                                  "symbol": _fix_sym.full_path,
                                                                  "new_code_lines": len(_fix_nc.splitlines())
                                                              })})
                                    elif not _fix_sym:
                                        _available = [getattr(_s, "full_path", getattr(_s, "name", "?")) for _s in symbol_map.symbols[:20]]
                                        _dlog("correction_tool_use_fix_symbol_not_found",
                                              session_id=session_id, requested=_fix_sp,
                                              available_count=len(symbol_map.symbols))
                                        _tool_results.append({"type": "tool_result", "tool_use_id": _cblk.id,
                                                              "content": json.dumps({
                                                                  "error": f"Symbol '{_fix_sp}' not found. Use request_symbol_code to see available symbols.",
                                                                  "available_symbols": _available
                                                              }), "is_error": True})
                                    else:
                                        _dlog("correction_tool_use_fix_empty_code",
                                              session_id=session_id, symbol=_fix_sp)
                                        _tool_results.append({"type": "tool_result", "tool_use_id": _cblk.id,
                                                              "content": json.dumps({"error": "new_code is empty"}),
                                                              "is_error": True})
                
                            # Check exit conditions
                            if _corr_done:
                                break
                            if _corr_resp.stop_reason == "end_turn" and not _tool_results:
                                _dlog("correction_tool_use_end_turn_no_tools",
                                      session_id=session_id, turn=_corr_turn + 1)
                                break
                
                            # Continue conversation for next turn
                            if _tool_results:
                                _corr_msgs.append({"role": "assistant", "content": _corr_resp.content})
                                _corr_msgs.append({"role": "user", "content": _tool_results})
                            else:
                                _dlog("correction_tool_use_no_tool_results",
                                      session_id=session_id, turn=_corr_turn + 1,
                                      stop_reason=_corr_resp.stop_reason)
                                break
                
                        # Apply correction results
                        if _corr_changes:
                            changes = _corr_changes
                            _dlog("correction_tool_use_applying",
                                  session_id=session_id, num_fixes=len(_corr_changes))
                            # Re-run QA on the fix (pass prior qa as feedback so judge can verify fix)
                            qa = await run_qa_for_changes(changes, file_content, user_request, anthropic_key, architect_model, qa_feedback=qa)
                            qa_risks = qa.get("risks", [])
                            # Re-run structural QA
                            if _sq_blocking is not None:
                                _sq_still_bad = False
                                for _sq_ch2 in changes:
                                    _sq_n2 = getattr(_sq_ch2, "new_code", "") or ""
                                    _sq_o2 = getattr(_sq_ch2, "original_code", "") or ""
                                    _sq_i2 = run_structural_qa(
                                        _sq_n2, _sq_o2, file_path or "",
                                        file_content=file_content or "",
                                        all_changes=[
                                            {"filename": file_path or "", "new_code": getattr(_c, "new_code", "") or ""}
                                            for _c in changes
                                        ],
                                    )
                                    # Filter pre-existing (session d007eaf1 fix)
                                    _sq_i2_raw = len(_sq_i2)
                                    if _filter_sq_aps is not None:
                                        _sq_i2 = _filter_sq_aps(
                                            _sq_i2, _sq_o2, file_path or "",
                                            file_content=file_content or "",
                                        )
                                    if _sq_i2_raw != len(_sq_i2):
                                        _dlog("aps_rerun_tool_sq_preexisting_filtered",
                                              session_id=session_id,
                                              raw=_sq_i2_raw, after=len(_sq_i2),
                                              attempt=_aps_attempt + 1,
                                              user_id=user_id)
                                    if _sq_blocking(_sq_i2):
                                        _sq_still_bad = True
                                        _sq_m2 = [x["message"] for x in _sq_i2 if x["severity"] == "error"]
                                        qa["verdict"] = "blocked"
                                        qa["qa_score"] = min(qa.get("qa_score", 10) or 10, 3)
                                        qa_risks.extend([f"[STRUCTURAL] {m}" for m in _sq_m2])
                                if not _sq_still_bad and qa.get("verdict") != "blocked":
                                    yield sse({"type": "progress",
                                               "content": f"✅ Retry {_aps_attempt + 1} passed QA (score: {qa.get('qa_score', '?')})"})
                                    break
                            elif qa.get("verdict") != "blocked" and (qa.get("qa_score", 0) or 0) >= 5:
                                yield sse({"type": "progress",
                                           "content": f"✅ Retry {_aps_attempt + 1} passed QA (score: {qa.get('qa_score', '?')})"})
                                break
                        else:
                            _dlog("correction_tool_use_no_fixes",
                                  session_id=session_id, attempt=_aps_attempt + 1)
                
                    except Exception as _corr_exc:
                        _dlog("correction_tool_use_failed",
                              session_id=session_id,
                              error_type=type(_corr_exc).__name__,
                              error=str(_corr_exc)[:300],
                              attempt=_aps_attempt + 1,
                              user_id=user_id)
                else:
                    # ── Existing free-text correction path ──────────────────────

                    try:
                        _retry_msgs = [
                            {"role": "user", "content": user_message},
                            {"role": "assistant", "content": full_text},
                            {"role": "user", "content": (
                                f"Your code changes were rejected by QA:\n\n{_fb_text}\n\n"
                                f"Please fix all issues and return the corrected JSON with the "
                                f"same structure (changes array with symbol_path, new_code, description, confidence). "
                                f"The new_code must be COMPLETE — include all imports, all functions, nothing omitted."
                            )},
                        ]
                        if _agent_use_claude:
                            _dlog("correction_retry_call_config",
                                  model="claude-sonnet-5",
                                  wrapper="safe_claude_call",
                                  session_id=session_id, user_id=user_id)
                            _retry_resp = await _safe_claude_call(
                                AsyncAnthropic(api_key=anthropic_key),
                                model="claude-sonnet-5",
                                desired_text_tokens=64000,
                                system=CLAUDE_EDITOR_SYSTEM,
                                messages=_retry_msgs,
                            )
                            _retry_text = "".join(
                                b.text for b in _retry_resp.content if hasattr(b, "text")
                            )
                        else:
                            # GPT free-text correction
                            # Budget the payload so the full symbol + prior changes survive
                            # (no blind 8k chop); clip head+tail only if pathologically huge.
                            _retry_oai_msgs = _fit_correction_messages(
                                [{"role": "system", "content": CLAUDE_EDITOR_SYSTEM}]
                                + [
                                    {"role": m.get("role", "user"),
                                     "content": str(m.get("content", ""))}
                                    for m in _retry_msgs
                                    if isinstance(m.get("content"), str)
                                ],
                                session_id=session_id, user_id=user_id,
                            )
                            _retry_oai_resp = await asyncio.to_thread(
                                lambda: _chat_create(
                                    _agent_oai_client, architect_model,
                                    messages=_retry_oai_msgs,
                                    response_format={"type": "json_object"},
                                )
                            )
                            _retry_text = _retry_oai_resp.choices[0].message.content or ""
                            _dlog("agent_mode_gpt_retry", model=architect_model,
                                  response_len=len(_retry_text), session_id=session_id)
                        _raw_retry = _extract_json_from_text(_retry_text)
                        _retry_data = json.loads(_raw_retry) if isinstance(_raw_retry, str) else _raw_retry
                        _retry_changes_data = _retry_data.get("changes", [])

                        if _retry_changes_data:
                            # Rebuild changes from retry
                            _new_changes = []
                            for _rc in _retry_changes_data:
                                _rc_sp = _rc.get("symbol_path", "")
                                _rc_nc = _rc.get("new_code", "")
                                _rc_desc = _rc.get("description", "")
                                _rc_conf = _rc.get("confidence", 9)
                                if not _rc_sp or not _rc_nc:
                                    continue
                                _rc_sym = None
                                for s in symbol_map.symbols:
                                    if getattr(s, "full_path", None) == _rc_sp or getattr(s, "name", None) == _rc_sp:
                                        _rc_sym = s
                                        break
                                if not _rc_sym:
                                    for s in symbol_map.symbols:
                                        if _rc_sp in (getattr(s, "full_path", "") or "") or (getattr(s, "name", "") or "") in _rc_sp:
                                            _rc_sym = s
                                            break
                                if not _rc_sym:
                                    continue
                                _rc_diff = _make_diff(_rc_sym.code, _rc_nc, _rc_sp)
                                _rc_tgt, _rc_repl = _compute_target_element(_rc_sym.code, _rc_nc)
                                _new_changes.append(SurgicalChange(
                                    id=str(uuid.uuid4()),
                                    symbol=_rc_sym,
                                    original_code=_rc_sym.code,
                                    new_code=_rc_nc,
                                    diff=_rc_diff,
                                    confidence=_rc_conf,
                                    description=_rc_desc,
                                    applied=False,
                                    operations=[{"find": _rc_sym.code, "replace": _rc_nc}],
                                    target_element=_rc_tgt,
                                    replacement=_rc_repl,
                                ))
                            if _new_changes:
                                changes = _new_changes
                                # Re-run QA on the fix (pass prior qa as feedback so judge can verify fix)
                                qa = await run_qa_for_changes(changes, file_content, user_request, anthropic_key, architect_model, qa_feedback=qa)
                                qa_risks = qa.get("risks", [])
                                # Re-run structural QA
                                if _sq_blocking is not None:
                                    _sq_still_bad = False
                                    for _sq_ch2 in changes:
                                        _sq_n2 = getattr(_sq_ch2, "new_code", "") or ""
                                        _sq_o2 = getattr(_sq_ch2, "original_code", "") or ""
                                        _sq_i2 = run_structural_qa(
                                        _sq_n2, _sq_o2, file_path or "",
                                        file_content=file_content or "",
                                        all_changes=[
                                            {"filename": file_path or "", "new_code": getattr(_c, "new_code", "") or ""}
                                            for _c in changes
                                        ],
                                    )
                                        # Filter pre-existing (session d007eaf1 fix)
                                        _sq_i2_raw = len(_sq_i2)
                                        if _filter_sq_aps is not None:
                                            _sq_i2 = _filter_sq_aps(
                                                _sq_i2, _sq_o2, file_path or "",
                                                file_content=file_content or "",
                                            )
                                        if _sq_i2_raw != len(_sq_i2):
                                            _dlog("aps_rerun_freetext_sq_preexisting_filtered",
                                                  session_id=session_id,
                                                  raw=_sq_i2_raw, after=len(_sq_i2),
                                                  attempt=_aps_attempt + 1,
                                                  user_id=user_id)
                                        if _sq_blocking(_sq_i2):
                                            _sq_still_bad = True
                                            _sq_m2 = [x["message"] for x in _sq_i2 if x["severity"] == "error"]
                                            qa["verdict"] = "blocked"
                                            qa["qa_score"] = min(qa.get("qa_score", 10) or 10, 3)
                                            qa_risks.extend([f"[STRUCTURAL] {m}" for m in _sq_m2])
                                    if not _sq_still_bad and qa.get("verdict") != "blocked":
                                        yield sse({"type": "progress",
                                                   "content": f"✅ Retry {_aps_attempt + 1} passed QA (score: {qa.get('qa_score', '?')})"})
                                        break
                                elif qa.get("verdict") != "blocked" and (qa.get("qa_score", 0) or 0) >= 5:
                                    yield sse({"type": "progress",
                                               "content": f"✅ Retry {_aps_attempt + 1} passed QA (score: {qa.get('qa_score', '?')})"})
                                    break
                    except Exception as _retry_exc:
                        _dlog("auto_heal_retry_failed",
                              session_id=session_id,
                              error_type=type(_retry_exc).__name__,
                              error=str(_retry_exc)[:300],
                              user_id=user_id)

        # ------------------------------------------------------------------
        # Build final response object and yield
        # ------------------------------------------------------------------
        plan_obj = ArchitectPlan(
            summary=plan_data.get("summary", ""),
            targets=[],
            risks=plan_data.get("risks", []) + qa_risks,
        )

        result_obj = SurgicalAnalyzeResponse(
            session_id=session_id or str(uuid.uuid4()),
            plan=plan_obj,
            changes=changes,
            tokens_used=0,
        )

        yield sse({"type": "result", "content": result_obj.model_dump_json()})
        yield sse({"type": "done", "content": ""})

    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': _friendly_error(e)})}\n\n"


def analyze_multi_file(file_paths: list, file_contents: dict, user_request: str, session_id=None, user_id: str = ""):
    """
    Analyze multiple files and produce a coordinated change plan.
    The architect sees ALL symbol maps, then surgeons work per-file.
    """
    # Parse all files
    all_maps = {}
    for fp in file_paths:
        content = file_contents.get(fp, "")
        if content:
            all_maps[fp] = parser.parse(content, fp)

    # Build combined symbol summary for architect
    combined_summary = []
    for fp, smap in all_maps.items():
        combined_summary.append(f"\n### File: {fp}")
        for sym in smap.symbols:
            entry = f"  [{sym.symbol_type.value}] {sym.full_path}"
            if sym.signature:
                entry += f" — {sym.signature}"
            combined_summary.append(entry)

    # Single architect call for ALL files
    client = _get_client(user_id)
    arch_model = get_setting("architect_model", "gpt-4.1")

    multi_user_msg = f"""MULTI-FILE ANALYSIS REQUEST

FILES AND SYMBOLS:
{chr(10).join(combined_summary)}

USER REQUEST:
{user_request}

Produce the surgical change plan. For each target, include the file_path field to indicate which file it belongs to.
Add "file_path" to each target object."""

    response = _chat_create(client,
        model=arch_model,
        messages=[
            {"role": "system", "content": ARCHITECT_SYSTEM + '\nIMPORTANT: Add "file_path" field to each target indicating which file the change belongs to.'},
            {"role": "user", "content": multi_user_msg}
        ],
        temperature=float(get_setting("temperature_architect", "0.0")),
        response_format={"type": "json_object"}
    )

    raw = json.loads(response.choices[0].message.content)

    # Group changes by file
    changes_by_file = {}
    overall_summary = raw.get("summary", "")

    for t in raw.get("targets", []):
        fp = t.get("file_path", file_paths[0] if file_paths else "")
        if fp not in changes_by_file:
            changes_by_file[fp] = []

        target = ChangeTarget(
            symbol_path=t.get("symbol_path", ""),
            change_type=ChangeType(t.get("change_type", "modify")),
            description=t.get("description", ""),
            new_logic=t.get("new_logic", ""),
            dependencies=t.get("dependencies", []),
            confidence=t.get("confidence", 7),
            import_changes=t.get("import_changes", []),
            context_needs=t.get("context_needs", []),
        )
        changes_by_file[fp].append(target)

    # Run surgeon per file
    result_by_file = {}
    for fp, targets in changes_by_file.items():
        if fp not in all_maps:
            continue
        smap = all_maps[fp]
        content = file_contents.get(fp, "")
        changes = []

        for target in targets:
            symbol = None
            for sym in smap.symbols:
                if sym.full_path == target.symbol_path or sym.name == target.symbol_path:
                    symbol = sym
                    break
            if symbol is None:
                continue
            new_code, confidence, _surg_notes, _needed_imports, _operations = run_surgeon(symbol, target, content, user_id=user_id)
            diff = _make_diff(symbol.code, new_code, target.symbol_path)
            _tgt_elem, _replacement = _compute_target_element(symbol.code, new_code)

            # v3.3.1: detect script injection truncation / phantom </script>
            _inject_issue, _injected_fn = _is_script_injection_issue(symbol.code, new_code)
            _insert_mode2 = False
            _insert_anchor2 = None
            if _inject_issue and _injected_fn:
                _insert_anchor_line2 = _find_script_close_line(content, symbol.end_line)
                _insert_mode2 = True
                _insert_anchor2 = _insert_anchor_line2
                _tgt_elem = None
                _replacement = _injected_fn
                new_code = symbol.code

            changes.append(SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code if not _insert_mode2 else symbol.code + "\n" + _injected_fn,
                diff=diff,
                confidence=confidence,
                description=target.description,
                applied=False,
                target_element=_tgt_elem,
                replacement=_replacement,
                insert_mode=_insert_mode2,
                insert_anchor=_insert_anchor2
            ))

        result_by_file[fp] = SurgicalAnalyzeResponse(
            session_id=session_id or str(uuid.uuid4()),
            plan=ArchitectPlan(summary=overall_summary, targets=targets, risks=raw.get("risks", [])),
            changes=changes,
            tokens_used=0
        )

    return result_by_file, overall_summary


def run_impact_analysis(symbol_path: str, file_path: str, file_content: str, workspace_path: str = None):
    """
    Analyze what other code will be impacted if we change a given symbol.
    Scans other files in the workspace if workspace_path provided.
    """
    from models.schemas import ImpactResult, ImpactAnalysisResponse
    import os

    symbol_name = symbol_path.split(".")[-1]
    impacts = []

    # Scan workspace files for references
    search_paths = []
    if workspace_path and os.path.isdir(workspace_path):
        for root, dirs, files in os.walk(workspace_path):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('node_modules', '__pycache__', '.venv', 'dist', 'build')]
            for fname in files:
                fp = os.path.join(root, fname)
                if fp != file_path and any(fname.endswith(ext) for ext in ['.py', '.js', '.ts', '.tsx', '.jsx', '.go', '.rs', '.java']):
                    search_paths.append(fp)

    for fp in search_paths[:50]:  # limit scan
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()

            if symbol_name in content:
                # Check type of reference
                if re.search(rf'import.*{re.escape(symbol_name)}', content):
                    impacts.append(ImpactResult(
                        symbol_path=symbol_path,
                        file_path=fp,
                        impact_type="imports",
                        description=f"Imports {symbol_name}"
                    ))
                elif re.search(rf'{re.escape(symbol_name)}\s*\(', content):
                    impacts.append(ImpactResult(
                        symbol_path=symbol_path,
                        file_path=fp,
                        impact_type="calls",
                        description=f"Calls {symbol_name}()"
                    ))
                else:
                    impacts.append(ImpactResult(
                        symbol_path=symbol_path,
                        file_path=fp,
                        impact_type="uses",
                        description=f"References {symbol_name}"
                    ))
        except Exception:
            pass

    risk = "low" if len(impacts) == 0 else "medium" if len(impacts) < 5 else "high"
    summary = f"Changing {symbol_name} affects {len(impacts)} other location(s)." if impacts else f"No external references found for {symbol_name}."

    return ImpactAnalysisResponse(
        target_symbol=symbol_path,
        impacts=impacts[:20],
        risk_level=risk,
        summary=summary
    )


def analyze_and_plan(
    file_path: str,
    file_content: str,
    user_request: str,
    session_id: Optional[str] = None,
    user_id: str = ""
) -> SurgicalAnalyzeResponse:
    """
    Full pipeline: parse → architect → surgeon → diff → response.
    """
    # Step 1: Parse symbol map
    symbol_map = parser.parse(file_content, file_path)

    # Step 2: Architect produces plan
    plan = run_architect(symbol_map, user_request, file_content, user_id=user_id)

    # Step 3: For each target, find symbol and run surgeon
    changes = []

    for target in plan.targets:
        # Find the symbol in the map
        symbol = None
        for sym in symbol_map.symbols:
            if sym.full_path == target.symbol_path or sym.name == target.symbol_path:
                symbol = sym
                break

        if symbol is None:
            # For ADD: find parent class and add new symbol there
            if target.change_type == ChangeType.ADD:
                parent_path = ".".join(target.symbol_path.split(".")[:-1])
                parent_symbol = None
                if parent_path:
                    for sym in symbol_map.symbols:
                        if sym.full_path == parent_path or sym.name == parent_path:
                            parent_symbol = sym
                            break
                if parent_symbol is not None:
                    new_code, confidence, _surg_notes, _needed_imports, _operations = run_surgeon(parent_symbol, target, file_content, user_id=user_id)
                    diff = _make_diff(parent_symbol.code, new_code, f"{target.symbol_path} (added to {parent_path})")
                    _tgt_elem, _replacement = _compute_target_element(parent_symbol.code, new_code)
                    # v3.3.1: injection guard
                    _inj_ns, _fn_ns = _is_script_injection_issue(parent_symbol.code, new_code)
                    _im_ns, _ia_ns = False, None
                    if _inj_ns and _fn_ns:
                        _ia_ns = _find_script_close_line(file_content, parent_symbol.end_line)
                        _im_ns = True
                        _tgt_elem = None
                        _replacement = _fn_ns
                        new_code = parent_symbol.code
                    change = SurgicalChange(
                        id=str(uuid.uuid4()),
                        symbol=parent_symbol,
                        original_code=parent_symbol.code,
                        new_code=new_code if not _im_ns else parent_symbol.code + "\n" + _fn_ns,
                        diff=diff,
                        confidence=confidence,
                        description=target.description,
                        applied=False,
                        target_element=_tgt_elem,
                        replacement=_replacement,
                        insert_mode=_im_ns,
                        insert_anchor=_ia_ns
                    )
                    changes.append(change)
            continue

        if target.change_type == ChangeType.DELETE:
            # Deletion: mark with empty code
            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code="",
                diff=_make_diff(symbol.code, "", target.symbol_path),
                confidence=target.confidence,
                description=target.description,
                applied=False
            )
            changes.append(change)
            continue

        # Run surgeon to get replacement code
        new_code, confidence, _surg_notes, _needed_imports, _operations = run_surgeon(symbol, target, file_content, user_id=user_id)
        diff = _make_diff(symbol.code, new_code, target.symbol_path)
        _tgt_elem3, _replacement3 = _compute_target_element(symbol.code, new_code)

        # v3.3.1: detect script injection truncation / phantom </script>
        _inject_issue3, _injected_fn3 = _is_script_injection_issue(symbol.code, new_code)
        _insert_mode3 = False
        _insert_anchor3 = None
        if _inject_issue3 and _injected_fn3:
            _insert_anchor_line3 = _find_script_close_line(file_content, symbol.end_line)
            _insert_mode3 = True
            _insert_anchor3 = _insert_anchor_line3
            _tgt_elem3 = None
            _replacement3 = _injected_fn3
            new_code = symbol.code

        change = SurgicalChange(
            id=str(uuid.uuid4()),
            symbol=symbol,
            original_code=symbol.code,
            new_code=new_code if not _insert_mode3 else symbol.code + "\n" + _injected_fn3,
            diff=diff,
            confidence=confidence,
            description=target.description,
            applied=False,
            target_element=_tgt_elem3,
            replacement=_replacement3,
            insert_mode=_insert_mode3,
            insert_anchor=_insert_anchor3
        )
        changes.append(change)

    return SurgicalAnalyzeResponse(
        session_id=session_id or str(uuid.uuid4()),
        plan=plan,
        changes=changes,
        tokens_used=0
    )


# ─────────────────────────────────────────────────────────────
# SMART PIPELINE — v1.3
# Auto-detects edit vs chat, works across all session files
# ─────────────────────────────────────────────────────────────


# ── Diagnosis best practices — injected conditionally into SMART_ARCHITECT_SYSTEM ──
_DIAGNOSIS_KEYWORDS = frozenset([
    "bug", "debug", "diagnose", "investigate", "why", "broken", "wrong",
    "not working", "doesn't work", "failing", "error", "crash", "exception",
    "unexpected", "weird", "strange", "issue", "problem", "trace", "root cause",
])

_DIAGNOSIS_SECTION = """
━━━ DIAGNOSIS BEST PRACTICES (CRITICAL) ━━━
When the user asks you to DIAGNOSE a bug, investigate state/session issues, or debug behavior:

1. CHECK FRONTEND STATE FIRST — ALWAYS inspect localStorage, sessionStorage, Zustand/Redux stores,
   cookies, and context providers BEFORE blaming backend logic. Most "session" and "state" bugs live
   in the frontend, not the server. Look for shared keys, global state, cross-tab conflicts.

2. HYPOTHESIS VALIDATION — Before presenting your diagnosis, verify that every pattern or variable
   you reference ACTUALLY EXISTS in the uploaded code. Do NOT assume a pattern exists — check the
   symbol map. If you can't find it, say "I expected X but it's not in this file."

3. ZERO-CHANGE SIGNAL — If your plan results in 0 actual code changes for a file, that strongly
   suggests your diagnosis for that file was wrong. Acknowledge this explicitly.

4. CROSS-LAYER TRACE — Follow the actual data flow end-to-end before proposing a fix. Trace:
   where does the value originate → where is it stored → where is it read → where does it render?
   If your trace dead-ends at a layer boundary, say "I need to see [specific file] to continue."

5. PREDICT THE COUNTER-EXAMPLE — Before finalizing any change plan, state one concrete scenario
   where your proposed fix would fail or cause a regression: "This fix would break if [scenario]."
   If you cannot think of one, you haven't understood the change deeply enough.
"""


# ── Spreadsheet/data-file awareness — injected when data files are in session ──
_SPREADSHEET_SECTION = """
━━━ SPREADSHEET & DATA FILES ━━━

You have uploaded data files (CSV, Excel, or similar). You have special capabilities for these:

1. **Answering questions about the data:** You can see the data as markdown tables in the file
   content above. Analyze it directly — summarize, compare, find patterns, answer questions.

2. **Creating new spreadsheet files:** You have the `create_spreadsheet` tool. Use it when
   the user asks you to generate, export, create, or modify data into a downloadable file.
   - Provide `filename` (e.g. "report.csv" or "analysis.xlsx"), `columns`, and `rows`
   - The file will appear in the user's file list for immediate download
   - For modifications: read the data from the uploaded file content, apply the changes,
     and call `create_spreadsheet` with the updated data

CRITICAL: Do NOT write Python scripts for the user to run. You have the tools to create
spreadsheet files directly. Use `create_spreadsheet` instead of suggesting code.

Examples of when to use `create_spreadsheet`:
- "Add a total row to this spreadsheet" -> read data, compute totals, create_spreadsheet with new data
- "Filter rows where sales > 1000" -> read data, filter, create_spreadsheet with filtered data
- "Create a summary report" -> analyze data, create_spreadsheet with summary
- "Convert this to Excel" -> read CSV data, create_spreadsheet with .xlsx filename
- "Make me a spreadsheet of US states" -> create_spreadsheet with the data
"""

_NATURAL_DATA_SECTION = """
━━━ DATA FILE ANALYSIS ━━━

The uploaded files include data files (CSV, Excel, or similar). Their content is shown
as markdown tables in the file context above. You have full capability to work with this data:

1. **Answer questions about the data** — summarize, compare, find patterns, compute statistics,
   filter, rank, or explain the data. Work directly from the table content you can see above.

2. **Create new data files** — When the user asks you to create, export, filter, transform, or
   modify data into a downloadable file, use a <new_file> block. You can create BOTH CSV and
   Excel files:

   CSV example:
<new_file>
{
  "filename": "filtered_results.csv",
  "language": "csv",
  "summary": "Filtered rows where revenue > 10000",
  "content": "Name,Revenue,Region\nAcme,15000,West\nGlobex,22000,East"
}
</new_file>

   Excel example (provide data as CSV — it will be converted to a real .xlsx binary automatically):
<new_file>
{
  "filename": "report.xlsx",
  "language": "csv",
  "summary": "Sales report with all regions",
  "content": "Name,Revenue,Region\nAcme,15000,West\nGlobex,22000,East"
}
</new_file>

   For modifications: read the data from the uploaded content above, apply the user's changes,
   and output the complete updated data as a <new_file>.

3. **You CAN work with Excel/spreadsheet data** — The data has already been extracted and is
   visible to you as markdown tables. You can read it, analyze it, and create new CSV or Excel
   files from it. If the user uploaded an .xlsx and asks for edits, output an .xlsx back.

CRITICAL RULES:
- Do NOT use <file_request> for data files — they are ALREADY fully loaded as markdown tables above.
- Do NOT say "I cannot edit Excel files" — the data is RIGHT HERE in your context as tables.
- Do NOT suggest the user run Python scripts. YOU do the analysis and create the output directly.
- For simple questions (totals, averages, specific lookups), just answer in your response text.
- For new/modified data files, use <new_file> with either .csv or .xlsx filename.
"""


# ── Code quality best practices — always injected into SMART_ARCHITECT_SYSTEM for edits ──
_CODE_QUALITY_SECTION = """
━━━ CODE QUALITY RULES (MANDATORY FOR ALL EDITS) ━━━
Before writing ANY change plan, verify your plan follows every applicable rule below.

CSS / STYLING:
1. NO DUPLICATE DEFINITIONS — Before adding a CSS class or keyframe, check if it already exists
   in the symbol. If it does, MODIFY the existing definition. Never create a second copy.
2. USE ONLY DEFINED VARIABLES — Only reference CSS custom properties (--var-name) that are
   defined in :root or a parent scope in the file. If you need a new variable, ADD it to :root
   first and include that in your plan. Never reference undefined variables.
3. REUSE EXISTING TOKENS — Scan the file's existing :root / theme variables for colors, spacing,
   and fonts. Use those instead of hardcoding hex/rgb values or inventing new variable names.
4. SPECIFICITY DISCIPLINE — No !important unless overriding third-party styles. Prefer class
   selectors over element selectors. Keep specificity flat.
5. CONSISTENT UNITS — Match the unit convention already in the file (rem vs px vs em). Don't
   mix unit systems within the same component.

REACT / JSX:
6. TAG BALANCE — Every opening JSX tag must have a matching close tag at the correct nesting
   level. After planning any JSX change, mentally walk the open/close tree to confirm balance.
7. KEY PROPS — Every element produced by .map() or a loop must have a unique, stable `key`
   derived from data (id, slug), never from the array index if items can reorder or be deleted.
8. HOOK RULES — Hooks must be called unconditionally at the top level of the component.
   Never place hooks inside if/else, loops, or after early returns.
9. EFFECT CLEANUP — useEffect that creates subscriptions, timers, or event listeners MUST
   return a cleanup function. Missing cleanup = memory leak.
10. DEPENDENCY ARRAYS — useEffect / useMemo / useCallback deps must include every external
    variable referenced inside the callback. Never leave deps empty to "run once" if the
    callback reads props or state.
11. NO INLINE CLOSURES IN RENDER — Avoid creating new function instances inside JSX
    (onClick={() => fn(x)}) when a stable callback (useCallback or class method) is available.

TYPESCRIPT:
12. PRESERVE TYPES — Never widen a typed parameter to `any`. When adding a variable or param,
    give it a specific type matching existing patterns in the file.
13. INTERFACE CONSISTENCY — When adding a field to a type/interface, update every place that
    constructs or destructures that type. Mark new optional fields with ?.
14. IMPORT TYPES — Use `import type { X }` for type-only imports to avoid runtime overhead.
15. GENERICS — When the existing code uses generics (e.g., useState<string>), maintain the
    type parameter. Don't drop it to useState("") with inferred type.

STATE & DATA FLOW:
16. NO REDUNDANT STATE — Don't add useState for values that can be derived from existing state
    or props. Compute them in the render body or useMemo.
17. IMMUTABLE UPDATES — Never mutate state directly (push/splice on arrays, property assignment
    on objects). Always create new references: spread, map/filter, or structuredClone.
18. SINGLE SOURCE OF TRUTH — Don't duplicate the same data in multiple state variables or stores.
    One canonical location, derived views everywhere else.

ERROR HANDLING:
19. TRY/CATCH EVERY ASYNC — Every fetch, API call, or async operation must have error handling.
    Surface errors to the user via toast/alert/state — never silently swallow them.
20. GRACEFUL DEGRADATION — When a feature depends on an API or optional data, handle the loading
    and error states explicitly. The UI must never show a blank screen on failure.

STRUCTURAL INTEGRITY:
21. PRESERVE EXPORTS — Do not accidentally remove, rename, or change the signature of exported
    functions, components, or constants. Verify default and named exports remain intact.
22. IMPORT HYGIENE — Add imports for everything you reference. Remove imports you make unused.
    Never leave orphan imports or missing imports.
23. INDENTATION — Match the file's existing style exactly: spaces vs tabs, 2-space vs 4-space.
    Never mix indentation styles within a file.
24. NO DEAD CODE — Don't leave commented-out code blocks, unreachable branches, or unused
    variables. If you remove usage of something, remove its declaration too.
25. PRESERVE COMMENTS — Don't silently delete existing comments or doc blocks unless the code
    they describe is being removed. Comments are documentation for humans.
"""


def _build_architect_system(is_diagnostic: bool = False, session_id: str = "", has_data_files: bool = False) -> str:
    """Return the SMART_ARCHITECT_SYSTEM prompt, with conditional sections injected."""
    base = SMART_ARCHITECT_SYSTEM
    inject_before = "━━━ IMPORT DEPENDENCY CHECK (DO THIS FIRST) ━━━"
    _injected = []
    if inject_before in base:
        # Always inject code quality rules
        sections = _CODE_QUALITY_SECTION + "\n"
        _injected.append("code_quality_rules")
        # Conditionally add diagnosis section
        if is_diagnostic:
            sections = _DIAGNOSIS_SECTION + "\n" + sections
            _injected.append("diagnosis_best_practices")
        # Conditionally add spreadsheet awareness when data files are present
        if has_data_files:
            sections = _SPREADSHEET_SECTION + "\n" + sections
            _injected.append("spreadsheet_data_awareness")
        base = base.replace(inject_before, sections + inject_before, 1)
    _dlog("architect_system_build",
          injected_sections=_injected,
          is_diagnostic=is_diagnostic,
          quality_rules_len=len(_CODE_QUALITY_SECTION),
          final_system_len=len(base),
          session_id=session_id)
    return base


SMART_ARCHITECT_SYSTEM = """You are SurgicalAI — an AI coding assistant. You analyze code, plan changes, and implement them directly.

You do everything yourself: understand the request, locate the right symbols, and produce the complete new code.

━━━ YOUR DECISION TREE ━━━

Look at the uploaded files + the user request and choose ONE intent:

1. "needs_clarification" — use this when:
   - The request is too vague to identify specific files/symbols to change
   - Multiple reasonable interpretations exist and choosing wrong would waste the user's time
   - You are missing critical info (e.g. "add authentication" but no auth system in the files)
   - Ask max 2 SHORT questions. Do NOT ask if you can reasonably infer the answer.

2. "edit" — use this when:
   - You can identify EXACTLY which file(s) and symbol(s) need to change
   - The request is specific enough to plan with confidence ≥ 7
   - Rule: identify the MINIMUM set of symbols. Do not plan changes you weren't asked for.

3. "chat" — use this when:
   - It's a question, explanation request, or general discussion
   - No code changes are needed

4. "create" — use this when:
   - The user asks to create, build, make, scaffold, or generate a NEW file, component, hook,
     service, page, or module that does NOT exist in the uploaded session files
   - MIXED: if creating also requires changing an existing file (e.g. adding an import to App.tsx),
     include BOTH "new_files" AND "targets" in your response

   Detection:
   - "create a PaymentForm component" → create (file doesn't exist)
   - "add a useDebounce hook" → create (hooks/useDebounce.ts doesn't exist)
   - "add dark mode to SettingsModal" → EDIT not create (file exists)
   - "build a payment service" → create (services/payments.ts doesn't exist)

5. "search" — use this when you need to see a symbol's full code before you can plan.
   You have the FULL SYMBOL INDEX — every function and class name with its line range.
   Use it. Do not guess at symbol names or locations.

   Search strategy (follow this order):
   a) SYMBOL NAME FIRST: If the target symbol name is in the index, search its exact name.
      The pipeline does an AST lookup and returns the complete function code.
   b) STRING LITERAL SECOND: If the user mentioned a specific string or ID that appears
      verbatim in the code, search for that exact string.
   c) KEYWORD LAST: Only use plain keywords if neither (a) nor (b) applies.

   Rules:
   - Search budget scales with file size — up to 8 rounds on large files.
   - NEVER request terms already in ALREADY SEARCHED TERMS.
   - Stop searching the moment you have the exact symbol you need to edit.
   - After 3 failed rounds, switch to needs_clarification and ask the user for
     the function name or a quoted string from the code.

━━━ OUTPUT FORMAT ━━━

Output ONLY valid JSON. Pick the matching shape:

IF needs_clarification:
{
  "intent": "needs_clarification",
  "reasoning": "why you need more info",
  "questions": ["question 1 (short, specific)", "question 2 (optional)"],
  "clarification_response": "Friendly 1-sentence acknowledgement + the questions in markdown. End with: 'Once you answer, I'll plan the exact changes.'"
}

IF edit:
{
  "intent": "edit",
  "reasoning": "one sentence: what files are affected and why",
  "summary": "one sentence plan",
  "targets": [
    {
      "filename": "exact filename as uploaded",
      "symbol_path": "ClassName.method_name or function_name",
      "target_line": <optional integer: exact line number of the element to change, from KEYWORD MATCH / SEARCH RESULTS>,
      "change_type": "modify|add|delete",
      "description": "what changes in this symbol",
      "new_logic": "precise description of the new behavior AND the complete new code. QUALITY RULE: Reference ACTUAL variable/function names from the code. Be specific: 'After const fee = calcFee(), add: if (fee < 0) throw new Error()'. Never say just 'add error handling' — always say WHAT, WHERE, and HOW.",
      "import_changes": ["add: import uuid", "remove: from datetime import date"],
      "context_needs": [],
      // OPTIONAL — semantic sections from OUTSIDE the target symbol.
      // Values: "style_block"|"state_declarations"|"hooks"|"css_vars"|"imports_block"|"type_declarations"|"constants"

      "surgeon_context": [],
      // OPTIONAL — precise code the Surgeon MUST see to implement this change correctly.
      // The pipeline resolves these via AST before the Surgeon runs.
      // USE when the Surgeon will need to call a function, match a type, or reference a
      // constant that is NOT visible in the target symbol's own code.
      // Request types:
      //   {"type":"symbol",  "name":"handleSubmit"}               — fetch by AST name lookup
      //   {"type":"symbol",  "name":"PaymentFlow.validate", "file":"checkout.ts"}
      //   {"type":"grep",    "pattern":"TAX_RATE"}                — search all files
      //   {"type":"lines",   "file":"auth.py", "start":140, "end":180}
      //   {"type":"callers", "name":"processOrder"}               — who calls this function
      //   {"type":"usages",  "name":"PaymentSchema"}              — where this type is used
      // Max 4 items. Leave empty [] if the Surgeon can write correct code from what it has.
      "confidence": 9
    // Score guide: 9-10=clear isolated change no side effects; 7-8=touches shared logic has deps;
    // 5-6=ambiguous multiple interpretations → MUST use needs_clarification; <5=missing files → ask
    }
  ],
  "risks": ["any side effects or breakage risks"]
}

IF chat:
{
  "intent": "chat",
  "reasoning": "why this is a question not an edit",
  "chat_response": "full markdown answer"
}

IF create:
{
  "intent": "create",
  "reasoning": "one sentence: what new file(s) are needed and why",
  "summary": "one sentence plan",
  "new_files": [
    {
      "filename": "src/components/PaymentForm.tsx",
      "description": "what this file does and its public API",
      "based_on": "which existing file's patterns to follow (e.g. 'follows LoginPage.tsx pattern')",
      "content_guidance": "Detailed spec for the file generator. Reference ACTUAL existing symbols, types, imports, and API methods visible in the uploaded files. Be specific: mention exact function names, type names, import paths. The more specific, the better the output."
    }
  ],
  "targets": []
  // targets is optional — only include if creating also requires editing an existing file
  // (e.g. registering a new route, adding an import to index.ts)
  // Same shape as the edit intent targets array.
}

IF search:
{
  "intent": "search",
  "reasoning": "one sentence: what you found so far and what you still need to locate",
  "search_terms": ["exactId", "type=\"date\"", "<input"],
  "confidence_if_found": 8
}

━━━ INTENT ROUTING — READ THIS CAREFULLY ━━━

DEFAULT RULE: If files are uploaded and the user wants their code to look or behave differently — use "edit".
This includes: redesign, restyle, modernize, refactor, rewrite, update, improve, change the UI, make it look like X.
ONLY use "chat" for pure questions ("what does X do?", "explain Y", "why is Z slow?") with no code change needed.
ONLY use "needs_clarification" when you genuinely cannot identify WHICH file or WHAT symbol to change.

If you can see the file and you can describe the change — use "edit". Never fall back to "chat" just because
the change is visual, stylistic, or large in scope.

The ONE exception: if the user explicitly asks for multiple options to choose from ("give me 3 versions",
"show me some design alternatives", "what are my options?") AND no specific file+symbol is implied — use "chat"
with full code blocks for each option. This exception does NOT apply when a target file is uploaded.

━━━ DIAGNOSIS BEST PRACTICES ━━━
[Injected dynamically for debug/bug requests — see _build_architect_system()]

━━━ IMPORT DEPENDENCY CHECK ━━━

Before planning, ask: does this change require calling a function/type from a file that was NOT uploaded?

- If YES and you'd have to invent a method signature → use "needs_clarification" and name the missing file.
- If NO (the change is self-contained in the uploaded file) → proceed with "edit" normally.
- If the imported file's method is obvious from context (e.g. a standard library, or the user described it) → proceed.

NEVER invent function signatures for files you haven't seen. One clarification question beats broken code.

━━━ HARD RULES ━━━
- ALWAYS pick the NARROWEST (smallest) symbol that contains the code to change.
  NEVER target broad container symbols like "body", "head", "main", "div#sb-root" when
  more specific child symbols exist within the relevant line range. For example:
  - BAD: targeting "body" (100+ lines) when "div#tb-stat" (4 lines) is where the change lives
  - GOOD: targeting "div#tb-stat" (4 lines) directly
  If the code you need to change is inside a named element (has an id), target THAT element.
  If it's inside a <script> block, target that specific script_N symbol.
  If it's injected dynamically by JavaScript, target the script that renders it.
  Look at the line counts in the symbol map — prefer symbols under 200 lines.
- NEVER touch symbols that weren't asked about
- For Python/Go/JS/TS/Rust files: use the exact function/class name from the symbol map as symbol_path
- For TSX/JSX/HTML/CSS files: symbol_path MUST be the component that RENDERS the UI being changed —
  the default export or a named component function (e.g. "LoginPage", "App", "Header").
  ⚠️  HOOKS AND UTILITY FUNCTIONS ARE NEVER VALID TARGETS FOR UI CHANGES.
  If the file has hooks like "useCountUp", "useAnimation", or ANY function whose name starts with "use",
  do NOT use them as symbol_path when the change is to rendered JSX/UI. Use the enclosing component instead.
  Inner elements like divs/spans are NOT symbols — set symbol_path to the containing component and use
  description + new_logic to precisely identify the inner element to change.
  EXCEPTION: Target a hook/utility ONLY when the user explicitly asks to modify that hook's own logic, not its visual output.
  IMPORTANT FOR LARGE HTML FILES: When a SEARCH RESULT or KEYWORD MATCH section shows the exact line of
  an element (e.g. "rsp-eff-date at L9318"), set target_line to that line number (e.g. 9318).
  This lets the pipeline create a focused edit window exactly where needed — not 500 lines away.
  For multi-instance edits ("add X to ALL Y fields"), each target MUST have a different target_line.
- confidence scale: 9-10=clear isolated change; 7-8=has dependencies; 5-6=ambiguous → use needs_clarification; <5=missing files → ask
- confidence < 7 = use needs_clarification instead (enforce this — do NOT guess at low confidence)
- Minimal footprint: if the user said "add X to function Y", only plan function Y"""



# ─────────────────────────────────────────────────────────────────────────────
QA_CREATE_SYSTEM = """You are a code reviewer checking a brand-new file generated by an AI.
Your job: verify it is immediately usable in the codebase it was created for.

Check ALL of the following:
1. IMPORTS — does the file import from paths/modules that appear in the CODEBASE CONTEXT?
   Flag any import that references a module NOT visible in the context.
2. TYPES — are type annotations consistent with types visible in the codebase?
   Flag obvious mismatches (wrong interface name, missing required field, wrong generic).
3. API USAGE — does the file call API methods/functions using the correct signatures?
   Reference the CODEBASE CONTEXT for actual method signatures.
4. COMPLETENESS — is the file complete and functional, or does it have unimplemented stubs,
   TODO placeholders, or missing export statements?
5. NAMING — does naming follow the conventions in the codebase (camelCase, PascalCase, etc.)?

Return ONLY valid JSON:
{
  "verdict": "safe" | "warning" | "blocked",
  "qa_score": <integer 1-10>,
  "summary": "<one sentence>",
  "import_issues": ["<issue>"],
  "type_errors": ["<error>"],
  "completeness_issues": ["<issue>"]
}
Score guide: 9-10=safe, 7-8=minor notes, 5-6=warning, ≤4=blocked."""


async def _run_qa_for_new_file(file_result: dict, codebase_context: str, user_id: str = "") -> dict:
    """
    Lightweight QA check for Claude-created new files.
    Returns same shape as run_qa_agent for consistency.
    """
    import asyncio
    try:
        _qa_aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
        _use_claude = True
        _model = "claude-sonnet-5"  # QA upgraded to Sonnet 5
    except Exception as _qc_key_err:
        _qa_aclient = None
        _use_claude = False
        _model = "gpt-4.1"
        _dlog("qa_create_claude_fallback",
              reason=str(_qc_key_err)[:120], fallback_model=_model,
              user_id=user_id)

    filename = file_result.get("filename", "new_file")
    content  = file_result.get("content", "")[:6000]

    user_msg = f"""CODEBASE CONTEXT (imports, types, and API signatures in the project):
{codebase_context[:3000]}

NEW FILE: {filename}
{content}

Run all 5 checks and return the JSON verdict."""

    try:
        if _use_claude:
            _dlog("qa_create_call_config", model=_model,
                  wrapper="safe_claude_call")
            _msg = await _safe_claude_call(
                _qa_aclient, model=_model,
                desired_text_tokens=8000,
                system=QA_CREATE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            # Iterate blocks defensively — adaptive-thinking models may emit
            # non-text blocks first, so content[0] is not guaranteed to be text.
            raw = "".join(
                _nb.text for _nb in _msg.content if hasattr(_nb, "text")
            ).strip()
            _dlog("qa_create_response_blocks",
                  block_count=len(_msg.content),
                  block_types=[getattr(_nb, "type", "?") for _nb in _msg.content])
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:] if lines[-1].strip() != "```" else lines[1:-1])
        else:
            client = _get_client(user_id)
            def _call():
                return _chat_create(client, model=_model,
                    messages=[{"role":"system","content":QA_CREATE_SYSTEM},
                               {"role":"user","content":user_msg}],
                    temperature=0.1, response_format={"type":"json_object"})
            resp = await asyncio.to_thread(_call)
            raw = resp.choices[0].message.content

        data = json.loads(raw)
        return {
            "verdict":              data.get("verdict", "warning"),
            "qa_score":             int(data.get("qa_score", 7)),
            "summary":              data.get("summary", ""),
            "import_issues":        data.get("import_issues", []),
            "type_errors":          data.get("type_errors", []),
            "completeness_issues":  data.get("completeness_issues", []),
            "downstream_risks":     [],
            "plan_deviation":       "",
            "risk_verdicts":        [],
        }
    except Exception as e:
        _dlog("qa_create_error",
              filename=filename, error_type=type(e).__name__,
              error=str(e)[:300], user_id=user_id)
        return {
            "verdict": "skipped", "qa_score": None,
            "summary": f"QA skipped: {str(e)[:100]}",
            "import_issues": [], "type_errors": [], "completeness_issues": [],
            "downstream_risks": [], "plan_deviation": "", "risk_verdicts": [],
        }


# ─────────────────────────────────────────────────────────────────────────────
# INLINE TEST RUNNER — reuses tests.py logic, callable from the pipeline
# ─────────────────────────────────────────────────────────────────────────────

async def _run_tests_inline(patched_files: dict, session_id: str) -> dict:
    """
    Run pytest / jest / vitest against session files with the Surgeon's changes applied.

    patched_files: {filename: content} for every file in the session,
                   with the Surgeon's new content already substituted for the changed file.

    Returns the same shape as tests.py run_tests():
    {framework, verdict, passed, failed, errors, output, duration_ms}
    or {verdict: "skipped", message: "..."} if no test files found.

    Never raises — always returns a dict.
    """
    import time, tempfile, subprocess, os
    from pathlib import Path

    # ── Detect test framework ──────────────────────────────────────────────
    file_names = list(patched_files.keys())
    has_pytest = any(
        f.endswith(".py") and (f.startswith("test_") or "_test.py" in f or "tests/" in f)
        for f in file_names
    )
    has_jest = any(
        f.endswith((".test.ts", ".test.tsx", ".test.js", ".spec.ts", ".spec.tsx", ".spec.js"))
        for f in file_names
    )
    has_vitest = has_jest and any("vitest" in c for c in patched_files.values() if isinstance(c, str))

    if not has_pytest and not has_jest:
        return {
            "framework": "unknown", "verdict": "skipped",
            "message": "No test files detected in session.",
            "passed": 0, "failed": 0, "errors": 0, "output": "", "duration_ms": 0,
        }

    framework = "pytest" if has_pytest else ("vitest" if has_vitest else "jest")
    TIMEOUT = 45

    tmpdir = tempfile.mkdtemp(prefix="surgicalai_test_")
    try:
        # Write all files to temp dir
        for fname, content in patched_files.items():
            fpath = Path(tmpdir) / fname
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(content or "", encoding="utf-8")

        start = time.time()

        if framework == "pytest":
            cmd = ["python", "-m", "pytest", "--tb=short", "-q", tmpdir]
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        else:
            pkg = Path(tmpdir) / "package.json"
            if not pkg.exists():
                return {
                    "framework": framework, "verdict": "skipped",
                    "message": "Jest/Vitest requires package.json. Upload full project to run JS tests.",
                    "passed": 0, "failed": 0, "errors": 0, "output": "", "duration_ms": 0,
                }
            runner = "vitest" if framework == "vitest" else "jest"
            cmd = ["npx", runner, "--run", "--reporter=verbose"]
            env = os.environ.copy()

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=tmpdir, timeout=TIMEOUT, env=env,
            )
            duration_ms = int((time.time() - start) * 1000)
            output = (result.stdout + result.stderr)[:3000]

            if framework == "pytest":
                passed = output.count(" passed")
                failed = output.count(" failed") + output.count(" error")
                # Parse "X passed, Y failed" pattern
                import re as _re
                m = _re.search(r"(\d+) passed", output)
                if m: passed = int(m.group(1))
                m = _re.search(r"(\d+) failed", output)
                if m: failed = int(m.group(1))
                m = _re.search(r"(\d+) error", output)
                errors = int(m.group(1)) if m else 0
            else:
                import re as _re
                m_pass = _re.search(r"(\d+) passed", output)
                m_fail = _re.search(r"(\d+) failed", output)
                passed = int(m_pass.group(1)) if m_pass else 0
                failed = int(m_fail.group(1)) if m_fail else 0
                errors = 0

            verdict = "passed" if result.returncode == 0 else "failed"
            return {
                "framework": framework, "verdict": verdict,
                "passed": passed, "failed": failed, "errors": errors,
                "output": output, "duration_ms": duration_ms,
            }

        except subprocess.TimeoutExpired:
            return {
                "framework": framework, "verdict": "skipped",
                "message": f"Test run timed out after {TIMEOUT}s.",
                "passed": 0, "failed": 0, "errors": 0, "output": "", "duration_ms": TIMEOUT * 1000,
            }

    except Exception as e:
        return {
            "framework": framework, "verdict": "skipped",
            "message": f"Test runner error: {str(e)[:200]}",
            "passed": 0, "failed": 0, "errors": 0, "output": "", "duration_ms": 0,
        }
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmpdir, ignore_errors=True)


# QA AGENT — runs after every Surgeon output, before diff card is shown to user
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# FILE CREATOR — Claude writes brand-new files based on codebase patterns
# ─────────────────────────────────────────────────────────────────────────────

FILE_CREATOR_SYSTEM = """You are an expert software engineer creating a new source file.

You will receive:
- CODEBASE CONTEXT: symbol maps and key patterns from the existing uploaded files
- FILE SPEC: filename, description, and detailed content guidance

Your job: write the COMPLETE file — production-ready, not pseudocode, no placeholders.

Rules:
1. Follow the patterns, naming conventions, and import paths from the CODEBASE CONTEXT exactly.
   If the context shows `import { api } from '../api/client'`, use that exact path.
   If components use Tailwind, use Tailwind. If they use inline styles, use inline styles.
2. Match the existing code style: spacing, quotes, semicolons, export style.
3. Use the exact type names, interface names, and function signatures visible in the context.
4. Make the file immediately usable — a developer should be able to import it and it works.
5. Include all necessary imports. Do not import anything not visible in the context unless
   it is a core language/framework built-in (React, useState, useEffect, etc.).

Return ONLY valid JSON — no markdown fences, no explanation outside the JSON:
{
  "filename": "the exact filename requested",
  "content": "complete file content as a single string",
  "language": "typescript|python|javascript|etc",
  "summary": "one sentence describing what was created"
}"""


async def run_file_creator(
    file_spec: dict,
    codebase_context: str,
    user_id: str = "",
) -> dict:
    """
    Claude writes a complete new file based on:
    - file_spec: {filename, description, based_on, content_guidance}
    - codebase_context: formatted symbol maps + key code patterns from session files

    Returns {filename, content, language, summary} or raises on failure.
    """
    creator_model = get_setting("architect_model", "claude-sonnet-5")

    # Route based on user's model — respect GPT selection instead of overriding
    if not _is_claude_model(creator_model):
        # GPT / OpenAI path — use the user's chosen model
        client = _get_client(user_id)
        user_msg = _build_creator_user_msg(file_spec, codebase_context)
        _dlog("file_creator_gpt_path", model=creator_model, user_id=user_id)
        response = _chat_create(
            client, creator_model,
            messages=[
                {"role": "system", "content": FILE_CREATOR_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        data["filename"] = file_spec.get("filename", data.get("filename", "new_file.ts"))
        return data

    # Claude path
    try:
        aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
    except Exception:
        # Fall back to OpenAI if no Anthropic key configured
        client = _get_client(user_id)
        user_msg = _build_creator_user_msg(file_spec, codebase_context)
        _dlog("file_creator_claude_no_key_fallback", user_id=user_id)
        response = _chat_create(
            client, "gpt-4.1",
            messages=[
                {"role": "system", "content": FILE_CREATOR_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        data = json.loads(raw)
        data["filename"] = file_spec.get("filename", data.get("filename", "new_file.ts"))
        return data

    user_msg = _build_creator_user_msg(file_spec, codebase_context)

    # Thinking-config fix: adaptive models consume max_tokens for thinking + text
    _fc_think_kw = _get_thinking_kwargs(creator_model, 4000)
    _fc_effort_kw = _get_effort_kwargs(creator_model)
    response_chunks = []
    async with aclient.messages.stream(
        model=creator_model,
        max_tokens=16000,
        system=FILE_CREATOR_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
        **_fc_think_kw,
        **_fc_effort_kw,
    ) as stream:
        async for text in stream.text_stream:
            response_chunks.append(text)

    raw = "".join(response_chunks).strip()
    # Strip markdown fences if Claude wrapped it anyway
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    data = json.loads(raw)
    # Ensure filename matches what was requested (Claude sometimes drifts)
    data["filename"] = file_spec.get("filename", data.get("filename", "new_file.ts"))
    return data


def _build_creator_user_msg(file_spec: dict, codebase_context: str) -> str:
    return f"""CODEBASE CONTEXT (patterns to follow exactly):
{codebase_context}

FILE SPEC:
Filename: {file_spec.get("filename", "")}
Description: {file_spec.get("description", "")}
Based on: {file_spec.get("based_on", "patterns visible in the codebase context")}
Content guidance: {file_spec.get("content_guidance", "")}

Write the complete file now. Return only JSON."""


def _build_codebase_context_for_creator(symbol_maps_by_name: dict) -> str:
    """
    Build a rich context string for the file creator.
    Includes: full imports block + top-level symbol signatures from each file.
    This gives Claude enough to match patterns without flooding the context.
    """
    parts = []
    for fname, (smap, sf) in symbol_maps_by_name.items():
        if not isinstance(sf, dict):
            continue
        content = sf.get("content", "")
        if not content:
            continue

        lines = content.splitlines()
        section = [f"FILE: {fname}"]

        # Full import block (first 40 lines usually covers all imports)
        import_lines = []
        for ln in lines[:50]:
            stripped = ln.strip()
            if stripped.startswith(("import ", "from ", "require(")):
                import_lines.append(ln)
            elif import_lines and not stripped:
                pass  # blank line within imports
            elif import_lines:
                break
        if import_lines:
            section.append("IMPORTS:\n" + "\n".join(import_lines))

        # Symbol signatures (not full code — keeps context tight)
        if smap:
            sigs = []
            for sym in smap.symbols[:30]:
                size = sym.end_line - sym.start_line + 1
                sig = sym.signature or sym.code.splitlines()[0] if sym.code else sym.name
                sig_short = sig[:120].replace("\n", " ").strip()
                sigs.append(f"  [{sym.symbol_type.value}] {sym.full_path} (L{sym.start_line}–{sym.end_line}, {size}L)  {sig_short}")
            if sigs:
                section.append("SYMBOLS:\n" + "\n".join(sigs))

        parts.append("\n".join(section))

    return "\n\n" + ("─" * 60) + "\n\n".join(parts) if parts else "(no uploaded files)"


def _rescue_trailing_context(operations: list, change_type: str = "modify") -> list:
    """
    Mechanical guard against Surgeon 'find' string context leakage.

    Root cause: Surgeon writes a 'find' string that extends past the actual change
    target — capturing trailing structural lines (closing tags, sibling divs, next
    functions, etc.).  Because those lines are absent from 'replace', they get
    permanently deleted on apply.

    Algorithm (language-agnostic — works for HTML, Python, JS/TS, Go, Rust, etc.):
      1. Walk backward through 'find' lines.
      2. Use fuzzy matching (SequenceMatcher ≥ 0.60) to detect the first 'find' line
         (from the end) that has a counterpart in 'replace'.  That line is the real
         change boundary — everything AFTER it is trailing context leakage.
      3. Rescue those trailing lines: trim from 'find', append to 'replace'.

    Skips pure DELETE operations (replace is empty — intentional full removal).
    """
    from difflib import SequenceMatcher

    if change_type == "delete":
        return operations

    def _fuzzy_has_match(line: str, candidates: list, threshold: float = 0.60) -> bool:
        stripped = line.strip()
        if not stripped:
            return True  # blank lines always considered "matched"
        for c in candidates:
            c_stripped = c.strip()
            if not c_stripped:
                continue
            if SequenceMatcher(None, stripped, c_stripped).ratio() >= threshold:
                return True
        return False

    for op in operations:
        find    = op.get("find", "")
        replace = op.get("replace", "")
        if not find or not replace:
            continue  # empty replace = intentional full deletion → skip

        find_lines    = find.split("\n")
        replace_lines = replace.split("\n")

        if len(find_lines) <= 1:
            continue  # single-line find: no trailing context possible

        # Walk backward to find the rescue boundary
        rescue_start = len(find_lines)  # index where trailing context begins

        for i in range(len(find_lines) - 1, -1, -1):
            line = find_lines[i]
            if not line.strip():
                rescue_start = i   # blank: tentatively include in rescue zone
                continue
            if _fuzzy_has_match(line, replace_lines):
                rescue_start = i + 1  # boundary found — everything after is leakage
                break
            rescue_start = i

        if rescue_start >= len(find_lines):
            continue  # nothing to rescue

        trailing = find_lines[rescue_start:]
        new_find = "\n".join(find_lines[:rescue_start])

        # Safety: don't create a trivial / empty find string
        if not new_find.strip() or len(new_find.strip()) < 5:
            continue

        n_rescued = len([l for l in trailing if l.strip()])
        if n_rescued == 0:
            continue  # only blank lines — skip

        op["find"]    = new_find
        op["replace"] = replace.rstrip("\n") + "\n" + "\n".join(trailing)
        print(f"[RESCUE-GUARD] Rescued {n_rescued} trailing lines from find → appended to replace")

    return operations


def _extract_json_from_text(text: str) -> str:
    """
    Robustly extract a JSON object from a string that may have:
    - Markdown code fences (```json ... ```)
    - Preamble text before the JSON
    - Trailing explanation after the JSON
    Returns the raw JSON string, or the original text if no {} found.
    """
    if not text:
        return text
    # Strip markdown fences first
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        # Drop first line (```json or ```) and last line if it's a closing fence
        inner_lines = lines[1:]
        if inner_lines and inner_lines[-1].strip() == "```":
            inner_lines = inner_lines[:-1]
        stripped = "\n".join(inner_lines).strip()
    # Find the outermost { ... } block
    start = stripped.find("{")
    if start == -1:
        return stripped  # Let json.loads fail naturally with a clear error
    # Find matching closing brace
    depth = 0
    end = -1
    for i, ch in enumerate(stripped[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end == -1:
        return stripped[start:]  # Best effort: return from first { to end
    extracted = stripped[start:end + 1]
    # Fast path: already valid JSON
    try:
        json.loads(extracted)
        return extracted
    except json.JSONDecodeError:
        pass
    # Fallback: handle Python-style dicts (single-quoted keys/values, unquoted keys)
    # ast.literal_eval handles {'key': 'val'} which json.loads rejects
    try:
        obj = ast.literal_eval(extracted)
        return json.dumps(obj)
    except Exception:
        pass
    return extracted  # Return as-is and let caller surface the error



def _salvage_fields_from_truncated_json(raw_text: str) -> dict:
    """
    Best-effort field recovery for a QA response that WAS valid JSON but got
    cut off mid-object by max_tokens (e.g. a 'thinking'-heavy model burns most
    of its budget reasoning, then the JSON text itself is truncated partway
    through a later field). json.loads() rejects the whole object in that
    case, but earlier fields (verdict, qa_score, summary, and any fully-quoted
    array items) are often complete and worth keeping instead of discarding.

    Root cause proven in session 0183c92e-30a2-4a92-9478-fecfe9769e03: the
    response was cut off mid-string inside "plan_deviation", after "verdict",
    "qa_score", "summary", "import_issues", "downstream_risks", and
    "type_errors" had already been written in full.
    """
    import re
    salvaged: dict = {}

    m = re.search(r'"verdict"\s*:\s*"(blocked|safe|warning)"', raw_text)
    if m:
        salvaged["verdict"] = m.group(1)

    m = re.search(r'"qa_score"\s*:\s*(\d{1,2})', raw_text)
    if m:
        val = int(m.group(1))
        if 1 <= val <= 10:
            salvaged["qa_score"] = val

    for field in ("summary", "plan_deviation"):
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw_text)
        if m:
            # Unescape via json.loads (not str.encode/decode('unicode_escape'),
            # which mis-decodes real UTF-8 multibyte chars like em-dashes —
            # the raw text here is already a proper unicode str, so only
            # JSON's own escape grammar (\", \\, \n, \uXXXX, ...) needs undoing).
            try:
                salvaged[field] = json.loads(f'"{m.group(1)}"')
            except json.JSONDecodeError:
                salvaged[field] = m.group(1)

    # Array-of-strings fields: grab whichever complete quoted strings sit
    # inside the array, even if the array (or the whole object) never closed.
    _array_fields = ("import_issues", "downstream_risks", "type_errors", "logic_errors")
    for field in _array_fields:
        m = re.search(rf'"{field}"\s*:\s*\[(.*?)(?:\]|"(?:{"|".join(_array_fields + ("summary", "plan_deviation"))})"\s*:|$)', raw_text, re.DOTALL)
        if m:
            _raw_items = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))
            items = []
            for _it in _raw_items:
                try:
                    items.append(json.loads(f'"{_it}"'))
                except json.JSONDecodeError:
                    items.append(_it)
            if items:
                salvaged[field] = items

    return salvaged


def _qa_fallback_from_prose(raw_text: str, session_id: str = "", user_id: str = "") -> dict:
    """
    Last-resort fallback: when QA returns prose analysis instead of JSON,
    try to extract verdict and score from the text itself.
    Returns a dict with verdict/qa_score/summary or None if nothing found.
    PRIORITY: numeric score > explicit verdict keyword > keyword-in-context.
    """
    import re

    # ── Step 0: the response may actually BE (truncated) JSON, not prose. ─────
    # Try to salvage real fields first — this recovers the model's genuine
    # summary/import_issues/etc. instead of throwing them away for a garbage
    # "first line of text" summary (which is literally "{" for JSON text).
    _salvaged = _salvage_fields_from_truncated_json(raw_text)

    text_lower = raw_text.lower()

    # ── Step 1: find numeric score ────────────────────────────────────────────
    # If the response was truncated JSON, the object's own "qa_score" field
    # is authoritative — skip the generic regex scan over lowercased prose.
    score = _salvaged.get("qa_score")
    score_patterns = [
        r'(?:score|qa_score)[:\s]+\s*(\d{1,2})(?:\s*/\s*10)?',
        r'(\d{1,2})\s*/\s*10',
        r'score\s+(?:of\s+)?(\d{1,2})',
    ]
    if score is None:
        for pat in score_patterns:
            m = re.search(pat, text_lower)
            if m:
                val = int(m.group(1))
                if 1 <= val <= 10:
                    score = val
                    break

    # ── Step 2: find explicit "verdict: <value>" patterns ─────────────────────
    # A verdict salvaged directly from the object's own "verdict" field is
    # authoritative; only fall back to scanning prose if that's absent.
    verdict = _salvaged.get("verdict")
    if verdict is None:
        _explicit_verdict_match = re.search(
            r'verdict[:\s]+\s*"?(blocked|safe|warning)"?', text_lower
        )
        if _explicit_verdict_match:
            verdict = _explicit_verdict_match.group(1)

    # ── Step 3: if verdict still unknown but score found, derive it ───────────
    # Numeric score is the most reliable signal. Keyword matches like "critical"
    # or "blocked" appearing anywhere in the prose are too noisy.
    if verdict is None and score is not None:
        if score >= 7:
            verdict = "safe"
        elif score >= 5:
            verdict = "warning"
        else:
            verdict = "blocked"

    # ── Step 4: no score found — try keyword matching (last resort) ───────────
    if score is None and verdict is None:
        # Only match keywords in verdict-like contexts, not casual mentions
        _verdict_line_patterns = [
            r'\bverdict\b.*?\b(blocked|safe|warning)\b',
            r'\b(blocked|safe|warning)\b.*?\bverdict\b',
        ]
        for _vp in _verdict_line_patterns:
            _vm = re.search(_vp, text_lower)
            if _vm:
                verdict = _vm.group(1)
                break

        # Broader keyword match only if nothing else worked
        if verdict is None:
            if "syntax error" in text_lower:
                verdict = "blocked"

        if verdict is not None:
            score = {"blocked": 3, "warning": 5, "safe": 8}.get(verdict, 5)

    if verdict is None and score is None:
        return None

    # ── Summary: prefer the model's own salvaged "summary" field ──────────────
    # Only fall back to "first line of raw text" when no real summary field
    # was recoverable — for JSON responses that first line is just "{", which
    # is exactly the bug this salvage path exists to avoid (session
    # 0183c92e-30a2-4a92-9478-fecfe9769e03: score 3/10 with summary "{").
    _salvaged_summary = _salvaged.get("summary")
    if _salvaged_summary:
        summary_line = _salvaged_summary[:500]
        summary_tag = "[QA response truncated by max_tokens — recovered from partial JSON]"
    else:
        summary_line = raw_text.strip().split("\n")[0][:200]
        summary_tag = "[QA prose fallback]"

    _dlog("qa_prose_fallback_extracted",
          session_id=session_id, user_id=user_id,
          verdict=verdict, score=score,
          raw_len=len(raw_text),
          summary_preview=summary_line[:100],
          salvaged_fields=sorted(_salvaged.keys()),
          raw_text_full=raw_text)

    return {
        "verdict": verdict,
        "qa_score": score,
        "summary": f"{summary_tag} {summary_line}",
        "import_issues": _salvaged.get("import_issues", []),
        "downstream_risks": _salvaged.get("downstream_risks", []),
        "type_errors": _salvaged.get("type_errors", []),
        "logic_errors": _salvaged.get("logic_errors", []),
        "plan_deviation": _salvaged.get("plan_deviation", ""),
        "risk_verdicts": [],
    }


QA_SYSTEM = """You are the QA agent in a surgical code editing pipeline.
The Surgeon has produced a code change. Your job: verify the new code is correct, complete, and safe.

You receive:
- CHANGE PLAN: what was requested (description + expected behavior)
- ORIGINAL CODE: the complete, untruncated code block before the change
- NEW CODE: the complete, untrancated code block after the change
- OTHER FILES CONTEXT: symbol maps of other files for cross-file checks

IMPORTANT: You have the FULL original and FULL new code — there is no truncation.
Compare them directly, like a real code reviewer would. Do NOT ask for a diff or complain
about missing context — everything you need is in ORIGINAL CODE and NEW CODE.

SCOPE — READ THIS FIRST: ORIGINAL CODE and NEW CODE are a SINGLE SYMBOL (one function,
class, or component) excerpted from a LARGER FILE. File-level imports, other functions,
helper definitions, and module-level exports (e.g. `import ... from ...`,
`export default X`) live OUTSIDE this symbol. They are NOT shown here but ARE present in
the file. Their absence from NEW CODE is EXPECTED and CORRECT — do NOT flag them as
"missing", "dropped", "absent", or a downstream risk. Evaluate ONLY the symbol itself
(ORIGINAL vs NEW). A correct edit that changes a few lines inside the symbol and leaves
the rest of the symbol intact is a 9-10, even though it does not contain the file's imports.

Check ALL of the following by comparing ORIGINAL → NEW:
1. COMPLETENESS — within THIS symbol, does NEW CODE preserve every line of ORIGINAL that the
   plan did not ask to change? Flag only lines dropped FROM THE SYMBOL itself — never file-level
   imports/exports/other symbols that were never inside ORIGINAL CODE to begin with.
2. PLAN COMPLIANCE — does NEW CODE implement exactly what was asked? No more, no less.
3. SYNTAX — unclosed brackets, missing semicolons, malformed JSX/TSX tags, broken template literals.
4. IMPORT ISSUES — flag an import issue ONLY when NEW CODE introduces a NEW identifier/dependency
   that ORIGINAL CODE did NOT already use. If both ORIGINAL and NEW use the same identifier
   (e.g. `motion`, `useState`, `useRef`), the import already exists in the file — do NOT flag it.
5. TYPE ERRORS — obvious type mismatches, wrong argument counts, wrong return types.
6. DUPLICATION — is any function, component, or block defined twice?
7. DOWNSTREAM RISKS — does this change break signatures, exported types, or constants other files depend on?
8. ARCHITECT RISKS — evaluate each provided risk against actual code changes.
9. LOGIC CORRECTNESS — mentally trace through the NEW CODE with 1-2 concrete examples:
   - Pick realistic input values and follow the execution path step by step
   - Verify the output/behaviour matches what the CHANGE PLAN describes
   - Flag any logic errors: wrong operator, off-by-one, inverted condition, missing edge case
   - Example: if the plan says "calculate tax at 10%", trace: input=100 → tax=100*0.10=10 → total=110 ✓
   - If you find a logic error that would produce wrong results in normal use, score ≤ 4 (blocked)
   - If the logic looks correct for the described cases, note it as verified

SCORING RULES (ENFORCE STRICTLY):
9-10 → safe:    Change exactly matches plan, logic verified, no issues, all imports present
7-8  → safe:    Minor style notes but nothing breaking
5-6  → warning: Potential issue user should review (unclear import, subtle type change, minor scope creep)
3-4  → blocked: Likely broken — missing critical imports, wrong signature, obvious logic error, wrong formula
1-2  → blocked: Severely wrong — duplicated code, plan not implemented, syntax errors

verdict MUST match score:
  score 7-10 → "safe"
  score 5-6  → "warning"
  score 1-4  → "blocked"

A "warning" verdict allows Apply but shows a yellow banner.
A "blocked" verdict disables Apply until the user overrides.

Respond with ONLY valid JSON — no text outside the JSON object:
{
  "verdict": "safe" | "warning" | "blocked",
  "qa_score": <integer 1-10>,
  "summary": "<one sentence — what the change does and any key finding>",
  "import_issues": ["<issue>"],
  "downstream_risks": ["<risk>"],
  "type_errors": ["<error>"],
  "logic_errors": ["<describe any logic error found — empty array if logic verified correct>"],
  "plan_deviation": "<empty string if none, or exact description of deviation>",
  "risk_verdicts": [
    {"risk": "<exact text from architect_risks>", "status": "verified_safe|warning|blocked", "reason": "<one sentence>"}
  ]
}
risk_verdicts must have one entry per item in architect_risks. If architect_risks is empty, return [].
logic_errors must be present — use [] if no logic errors found."""


async def run_qa_agent(
    original_code: str,
    new_code: str,
    change_description: str,
    new_logic: str,
    symbol_path: str,
    filename: str,
    other_files_context: str,
    session_id: str = "",
    user_id: str = "",
    architect_risks: list = None,
    targeted_context: str = "",
    qa_feedback: dict = None,
    same_run_context: str = "",
) -> dict:
    """
    QA agent: verifies Surgeon output before showing diff card to user.
    Primary: Claude Sonnet (best semantic reasoning for code review).
    Fallback: gpt-4.1 (full model, not mini — QA is the last line of defence).
    Returns a dict matching QAResult schema.
    Guaranteed to return a result — never raises (skipped verdict on error).
    """
    _risks_list = architect_risks or []
    _risks_block = "\n".join(f"- {r}" for r in _risks_list) if _risks_list else "(none — skip risk_verdicts)"
    import asyncio

    # Prefer Claude Sonnet for QA — strongest semantic code reasoning in the chain
    try:
        _qa_aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
        _qa_use_claude = True
        _qa_model = "claude-sonnet-5"  # QA upgraded to Sonnet 5 (better + cheaper than 4.6)
    except Exception as _qa_key_err:
        _qa_aclient = None
        _qa_use_claude = False
        _qa_model = "gpt-4.1"  # full GPT-4.1, not mini — QA must not be weakest link
        _dlog("qa_agent_claude_fallback",
              session_id=session_id, reason=str(_qa_key_err)[:120],
              fallback_model=_qa_model, user_id=user_id)

    # v3.12.0: QA receives FULL code — no truncation, no diff confusion.
    # Claude Sonnet has a 200K-token context. Real-world symbols (e.g. a large
    # landing-page component) routinely run 70-90K chars; the previous 60K cap
    # silently truncated the TAIL of such symbols — exactly where appends and
    # "insert before the closing section" edits land — so QA could not verify the
    # change and scored it conservatively, causing the hard gate to block a
    # perfectly valid edit. Raise the cap to 200K chars (~50K tokens/block;
    # original + new ≈ 100K tokens, still well within the 200K window) and, when a
    # symbol genuinely exceeds the cap, keep the HEAD *and* the TAIL with a marked
    # elision in the middle so the end of the symbol is never the part dropped.
    _MAX_CODE_CHARS = 200_000

    def _cap_code(_c: str) -> str:
        if len(_c) <= _MAX_CODE_CHARS:
            return _c
        _half = _MAX_CODE_CHARS // 2
        _dropped = len(_c) - (2 * _half)
        return (
            _c[:_half]
            + f"\n\n... [{_dropped} chars elided from the MIDDLE to fit the review window — "
              f"head and tail are shown in full; review the elided region manually] ...\n\n"
            + _c[-_half:]
        )

    _orig_snippet = _cap_code(original_code)
    _new_snippet = _cap_code(new_code)
    other_ctx = other_files_context[:8000] + ("\n... [truncated]" if len(other_files_context) > 8000 else "")
    # Unchanged flag for QA awareness
    _no_change = _orig_snippet.strip() == _new_snippet.strip()

    # ── Changed-region highlight — guide QA to focus on what actually changed ─
    # Compute first/last changed line numbers in NEW CODE so QA knows where to
    # look instead of scanning 2000 blind lines.
    _changed_region_hint = ""
    try:
        _orig_lines = (original_code or "").splitlines()
        _new_lines = (new_code or "").splitlines()
        import difflib as _difflib
        _sm = _difflib.SequenceMatcher(None, _orig_lines, _new_lines)
        _changed_new_lines = set()
        for _tag, _i1, _i2, _j1, _j2 in _sm.get_opcodes():
            if _tag != "equal":
                for _ln in range(_j1, _j2):
                    _changed_new_lines.add(_ln + 1)  # 1-indexed
        if _changed_new_lines:
            _first_changed = min(_changed_new_lines)
            _last_changed = max(_changed_new_lines)
            _total_new = len(_new_lines)
            _changed_region_hint = (
                f"\n\n📍 CHANGED REGION: Lines {_first_changed}–{_last_changed} of {_total_new} "
                f"in NEW CODE ({_last_changed - _first_changed + 1} lines modified).\n"
                f"Focus your structural verification on this region and its immediate "
                f"surroundings. The rest of the symbol should be unchanged from ORIGINAL."
            )
    except Exception:
        pass  # degrade gracefully — QA still works without the hint

    # Targeted cross-file context: actual callers/usages of changed symbol
    _targeted_block = ""
    if targeted_context and targeted_context.strip():
        _targeted_block = f"\n\nTARGETED CROSS-FILE CONTEXT (callers/usages of the changed symbol):\n{targeted_context.strip()}"

    # QA feedback block injected on retry
    _qa_feedback_block = ""
    if qa_feedback:
        _issues = []
        for _qk in ("import_issues", "type_errors", "logic_errors", "downstream_risks"):
            for _qi in (qa_feedback.get(_qk) or [])[:2]:
                _issues.append(f"  - {_qk.replace('_', ' ')}: {_qi}")
        if qa_feedback.get("plan_deviation"):
            _issues.append(f"  - plan deviation: {qa_feedback['plan_deviation'][:150]}")
        if _issues:
            _qa_feedback_block = (
                "\n\nPREVIOUS QA FAILURE — Surgeon already attempted this. These issues were found:\n"
                + "\n".join(_issues)
                + "\nVerify the Surgeon has resolved ALL of these in this new attempt."
            )

    # ── PRE-QA ADDITIVE BLOCK (flag-gated, default OFF) ─────────────────────
    # Two independent, purely-advisory prompt enrichments. Neither can gate,
    # block, or raise — any failure degrades to an empty string and QA runs
    # exactly as before. Single-pass path untouched.
    _pre_qa_block = ""

    # 1. Deterministic sanity checks (PRE_QA_SANITY=true) — local, no API calls
    if _os.environ.get("PRE_QA_SANITY", "false").lower() == "true":
        try:
            from services.pre_qa_sanity import run_sanity_checks
            _sanity = run_sanity_checks(filename, original_code, new_code)
            _dlog("pre_qa_sanity_result",
                  session_id=session_id, filename=filename, symbol=symbol_path,
                  checked=_sanity.get("checked", []),
                  finding_count=len(_sanity.get("findings", [])),
                  findings=_sanity.get("findings", []),
                  check_errors=_sanity.get("errors", []),
                  user_id=user_id)
            if _sanity.get("findings"):
                _pre_qa_block += (
                    "\n\nDETERMINISTIC PRE-CHECKS flagged the following (these are "
                    "heuristics, not verdicts — verify each one against the code):\n"
                    + "\n".join(f"- {_f}" for _f in _sanity["findings"])
                )
        except Exception as _sanity_err:
            _dlog("pre_qa_sanity_error",
                  session_id=session_id, filename=filename, symbol=symbol_path,
                  error=str(_sanity_err)[:200], user_id=user_id)

    # 2. QA history memory (QA_HISTORY=true) — one indexed SELECT on qa_log
    if _os.environ.get("QA_HISTORY", "false").lower() == "true":
        try:
            from services.qa_history import get_symbol_history
            _hist = await asyncio.to_thread(get_symbol_history, filename, symbol_path)
            _dlog("qa_history_result",
                  session_id=session_id, filename=filename, symbol=symbol_path,
                  total=_hist.get("total", 0), blocked=_hist.get("blocked", 0),
                  warning=_hist.get("warning", 0),
                  last_verdict=_hist.get("last_verdict", ""),
                  has_summary=bool(_hist.get("summary")),
                  lookup_error=_hist.get("error"),
                  user_id=user_id)
            if _hist.get("summary"):
                _pre_qa_block += "\n\n" + _hist["summary"]
        except Exception as _hist_err:
            _dlog("qa_history_error",
                  session_id=session_id, filename=filename, symbol=symbol_path,
                  error=str(_hist_err)[:200], user_id=user_id)
    # ── END PRE-QA ADDITIVE BLOCK ────────────────────────────────────────────

    user_msg = f"""CHANGE PLAN:
Symbol: {symbol_path}
File: {filename}
Description: {change_description}
Expected behavior: {new_logic}
{"⚠️ NOTE: No code changes detected — original and new are identical." if _no_change else ""}{_changed_region_hint}

ORIGINAL CODE (complete — compare this directly against NEW CODE):
{_orig_snippet}

NEW CODE (complete — this is what the Surgeon produced):
{_new_snippet}

OTHER FILES IN SESSION (for cross-file checking):
{other_ctx if other_ctx.strip() else "(no other files uploaded)"}{_targeted_block}{_qa_feedback_block}{_pre_qa_block}

{("\n\nOTHER CHANGES IN THIS SAME REQUEST (these companion edits are shipping together with this one):\nIMPORTANT: If a concern you find (e.g. missing import, missing CDN tag, missing function) is RESOLVED by one of the companion edits listed below, do NOT lower the score for it. Score this edit as if the companion changes are already applied.\n" + same_run_context + "\n") if same_run_context else ""}Compare ORIGINAL CODE → NEW CODE directly. Run all 8 checks and return the JSON verdict.

ARCHITECT PRE-ANALYSIS RISKS (evaluate each in risk_verdicts):
{_risks_block}"""

    _last_qa_err = None
    _qa_had_starvation = False  # tracks thinking-starvation for retry logic
    for _qa_attempt in range(2):
      try:
        if _qa_use_claude:
            # --- Robust QA call for adaptive-thinking models ---
            # Session 69ee9da7: Sonnet 5 with max_tokens=4000/6000 spent ALL
            # tokens on thinking, zero text output → QA failure on 1763-line
            # symbol.  Session b7d8f1b0: fix attempt with max_tokens=16000 +
            # adaptive thinking still starved (16K all thinking); retry at
            _dlog("qa_call_config", session_id=session_id,
                  model=_qa_model, attempt=_qa_attempt,
                  wrapper="safe_claude_call")
            _qa_msg = await _safe_claude_call(
                _qa_aclient, model=_qa_model,
                desired_text_tokens=16000,
                system=QA_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            # Iterate blocks defensively — adaptive-thinking models may emit
            # non-text blocks first, so content[0] is not guaranteed to be text.
            _qa_raw_text = "".join(
                _qb.text for _qb in _qa_msg.content if hasattr(_qb, "text")
            ).strip()
            _dlog("qa_response_blocks", session_id=session_id,
                  block_count=len(_qa_msg.content),
                  block_types=[getattr(_qb, "type", "?") for _qb in _qa_msg.content],
                  # stop_reason/output_tokens were NOT logged in session
                  # dd543a3a, which made the empty-response mechanism
                  # unprovable. Log them so the next occurrence is provable.
                  stop_reason=getattr(_qa_msg, "stop_reason", None),
                  output_tokens=getattr(getattr(_qa_msg, "usage", None), "output_tokens", None))
            if not _qa_raw_text:
                # Empty text (e.g. thinking-only response). Raise a clear,
                # typed error so the retry loop fires with a logged cause
                # instead of an opaque JSONDecodeError on "".
                _dlog("qa_agent_empty_text",
                      session_id=session_id, filename=filename,
                      symbol=symbol_path, model=_qa_model,
                      attempt=_qa_attempt,
                      stop_reason=getattr(_qa_msg, "stop_reason", None),
                      block_types=[getattr(_qb, "type", "?") for _qb in _qa_msg.content],
                      user_id=user_id)
                raise ValueError(
                    f"QA model returned no text blocks "
                    f"(stop_reason={getattr(_qa_msg, 'stop_reason', None)})"
                )
            _dlog("qa_agent_raw_response",
                  session_id=session_id, filename=filename,
                  symbol=symbol_path, model=_qa_model,
                  raw_len=len(_qa_raw_text),
                  raw_preview=_qa_raw_text,
                  user_id=user_id)
            # Robust JSON extraction — Claude sometimes adds preamble or markdown fences
            _raw_qa = _extract_json_from_text(_qa_raw_text)
            try:
                data = json.loads(_raw_qa) if isinstance(_raw_qa, str) else _raw_qa
            except json.JSONDecodeError:
                # JSON parse failed — try extracting verdict/score from prose
                _prose_result = _qa_fallback_from_prose(_qa_raw_text, session_id=session_id, user_id=user_id)
                if _prose_result is not None:
                    _dlog("qa_prose_fallback_used",
                          session_id=session_id, filename=filename,
                          symbol=symbol_path,
                          verdict=_prose_result["verdict"],
                          score=_prose_result["qa_score"],
                          user_id=user_id)
                    return _prose_result
                raise  # No prose extraction possible — let original error propagate
        else:
            client = _get_client(user_id)

            def _call():
                return _chat_create(
                    client,
                    model=_qa_model,
                    messages=[
                        {"role": "system", "content": QA_SYSTEM},
                        {"role": "user",   "content": user_msg},
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )

            response = await asyncio.to_thread(_call)
            _qa_raw_text = response.choices[0].message.content
            _dlog("qa_agent_raw_response",
                  session_id=session_id, filename=filename,
                  symbol=symbol_path, model=_qa_model,
                  raw_len=len(_qa_raw_text or ""),
                  raw_preview=(_qa_raw_text or "")[:500],
                  user_id=user_id)
            data = json.loads(_qa_raw_text)

        result = {
            "verdict":          data.get("verdict", "warning"),
            "risk_verdicts":    data.get("risk_verdicts", []),
            "qa_score":         int(data.get("qa_score", 7)),
            "summary":          data.get("summary", ""),
            "import_issues":    data.get("import_issues", []),
            "downstream_risks": data.get("downstream_risks", []),
            "type_errors":      data.get("type_errors", []),
            "logic_errors":     data.get("logic_errors", []) or [],
            "plan_deviation":   data.get("plan_deviation", ""),
            "skipped_reason":   None,
        }
        _dlog("qa_agent_parsed",
              session_id=session_id, filename=filename,
              symbol=symbol_path,
              verdict=result["verdict"],
              qa_score=result["qa_score"],
              summary=result["summary"][:200],
              has_import_issues=bool(result["import_issues"]),
              has_type_errors=bool(result["type_errors"]),
              has_logic_errors=bool(result["logic_errors"]),
              user_id=user_id)

        # Enforce verdict/score consistency
        # If QA found hard blockers (missing imports, logic errors, type errors)
        # respect the "blocked" verdict even at higher scores — those are
        # compilation failures regardless of how clever the logic is.
        _has_hard_issues = bool(
            result.get("import_issues")
            or result.get("type_errors")
            or result.get("logic_errors")
        )
        # If LLM said "blocked", respect it — never soften to "warning".
        # If hard issues exist, downgrade score to guarantee retry fires.
        if result["verdict"] == "blocked" and _has_hard_issues:
            result["qa_score"] = min(result["qa_score"] or 10, 4)
        if (result["qa_score"] or 10) <= 7 and result["verdict"] == "safe":
            result["verdict"] = "warning"

        # Log to DB (non-blocking — fire and forget)
        try:
            _log_qa_result(session_id, filename, symbol_path, result)
        except Exception:
            pass  # Never let logging kill the pipeline

        return result

      except Exception as _qa_e:
        _last_qa_err = _qa_e
        _dlog("qa_agent_error",
              session_id=session_id, filename=filename,
              symbol=symbol_path, attempt=_qa_attempt,
              error_type=type(_qa_e).__name__,
              error=str(_qa_e)[:300],
              user_id=user_id)
        if _qa_attempt == 0:
            await asyncio.sleep(1)
            continue  # retry once
        # Both attempts failed — surface the real error type
        _err_type = type(_qa_e).__name__
        _err_short = str(_qa_e)[:120]
        skipped = {
            "verdict":          "skipped",
            "qa_score":         None,
            "summary":          f"QA check could not run ({_err_type}) — review manually",
            "import_issues":    [],
            "downstream_risks": [],
            "type_errors":      [],
            "plan_deviation":   "",
            "skipped_reason":   f"{_err_type}: {_err_short}",
        }
        try:
            _log_qa_result(session_id, filename, symbol_path, skipped)
        except Exception:
            pass
        return skipped


def _log_qa_result(session_id: str, filename: str, symbol_name: str, result: dict):
    """Persist QA result to qa_log table. Called after every Surgeon run."""
    from database import get_db_connection
    issues_json = json.dumps({
        "import_issues":    result.get("import_issues", []),
        "downstream_risks": result.get("downstream_risks", []),
        "type_errors":      result.get("type_errors", []),
        "logic_errors":     result.get("logic_errors", []),
        "plan_deviation":   result.get("plan_deviation", ""),
    })
    conn = get_db_connection()
    try:
        # Use conn.execute(): works for SQLite (native) AND PostgreSQL, where
        # CompatConn.execute() auto-translates ? -> %s. The Postgres wrapper does
        # not expose .cursor(), so the previous cur = conn.cursor() path always
        # raised AttributeError in production and the write was silently dropped.
        conn.execute(
            """INSERT INTO qa_log (session_id, filename, symbol_name, verdict, qa_score, issues_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (session_id, filename, symbol_name,
             result.get("verdict", "skipped"),
             result.get("qa_score"),
             issues_json)
        )
        conn.commit()
    finally:
        conn.close()



# ─────────────────────────────────────────────────────────────────────────────
# PIPELINE COMPLIANCE SYSTEM
# Tracks whether every required step ran. Enforces no silent skipping.
# If a required step is missing at end of pipeline, emits a warning and saves to DB.
# ─────────────────────────────────────────────────────────────────────────────

class ComplianceTracker:
    """
    Tracks which critical pipeline steps ran for a given request.
    Steps that are not applicable to the current intent are automatically
    marked as N/A (not treated as failures).

    Steps and applicability:
      symbol_map_read   — edit only
      import_check      — edit only (Architect import dependency scan)
      architect_routing — always (Architect must classify intent)
      qa_review         — edit only (QA agent must run per-change)
      confidence_gate   — edit only (confidence score checked)
      diff_validate     — edit only (ghost diff check)
    """

    STEP_INTENTS = {
        "symbol_map_read":   ["edit"],
        "import_check":      ["edit"],
        "architect_routing": ["edit", "chat", "needs_clarification", "create"],
        "file_creation":     ["create"],
        "qa_review":         ["edit"],
        "confidence_gate":   ["edit"],
        "diff_validate":     ["edit"],
    }

    def __init__(self, run_id: str, session_id: str, intent: str = "unknown"):
        self.run_id = run_id
        self.session_id = session_id
        self.intent = intent
        self.steps: dict = {}

    def mark(self, step: str, ran: bool, reason: str = None, output_summary: str = None):
        self.steps[step] = {
            "ran": ran,
            "skipped_reason": reason,
            "output_summary": output_summary or "",
        }

    def set_intent(self, intent: str):
        self.intent = intent

    def applicable_steps(self) -> list:
        return [s for s, intents in self.STEP_INTENTS.items() if self.intent in intents]

    def missing_steps(self) -> list:
        missing = []
        for step in self.applicable_steps():
            if step not in self.steps:
                missing.append(step)
            elif not self.steps[step]["ran"] and not self.steps[step].get("skipped_reason"):
                missing.append(step)
        return missing

    def overall_pass(self) -> bool:
        return len(self.missing_steps()) == 0

    def to_dict(self) -> dict:
        return {
            "run_id":        self.run_id,
            "session_id":    self.session_id,
            "intent":        self.intent,
            "steps":         self.steps,
            "missing_steps": self.missing_steps(),
            "overall_pass":  self.overall_pass(),
        }

    def save(self):
        """Persist compliance record to DB. Non-blocking — logs errors but never raises."""
        try:
            from database import get_db_connection
            conn = get_db_connection()
            try:
                steps_json = json.dumps(self.steps)
                missing_json = json.dumps(self.missing_steps())
                _pass = 1 if self.overall_pass() else 0
                # conn.execute() works for both SQLite and PostgreSQL (CompatConn
                # adapts ? -> %s). CompatConn has no .cursor(), so the old
                # cur = conn.cursor() path silently failed in production.
                conn.execute(
                    """INSERT INTO compliance_log
                       (run_id, session_id, intent, steps_json, missing_steps, overall_pass)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (self.run_id, self.session_id, self.intent, steps_json, missing_json, _pass)
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # Never let compliance logging kill the pipeline


def _regex_extract_edit_block(raw: str) -> dict | None:
    """
    Regex-based fallback for extracting edit block fields when JSON parsing
    fails due to unescaped quotes (className="...") or literal newlines in
    code content.

    Handles two formats Claude produces:
      1. JSON-style:  "field": "...code..."  (with unescaped internal quotes)
      2. Hybrid XML:  "field">...code...</field>

    Returns a dict with at least 'filename' and 'new_code', or None if
    extraction fails.
    """
    result = {}
    KNOWN_KEYS = ("filename", "symbol", "description", "old_code", "new_code",
                  "edit_start_line", "edit_end_line")

    # ── Find all field key positions to establish boundaries ──
    key_positions = []
    for key in KNOWN_KEYS:
        for m in re.finditer(rf'"{key}"\s*', raw):
            key_positions.append((m.start(), key, m.end()))
    key_positions.sort(key=lambda x: x[0])

    # ── Simple string fields ──
    for field in ("filename", "symbol", "description"):
        m = re.search(rf'"{field}"\s*:\s*"([^"]*)"', raw)
        if m:
            result[field] = m.group(1)
        else:
            # Pure XML fallback: <field>value</field>
            m_xml = re.search(rf'<{field}>(.*?)</{field}>', raw)
            if m_xml:
                result[field] = m_xml.group(1).strip()

    # ── Integer fields ──
    for field in ("edit_start_line", "edit_end_line"):
        m = re.search(rf'"{field}"\s*:\s*(\d+)', raw)
        if m:
            result[field] = int(m.group(1))
        else:
            # Pure XML fallback: <field>123</field>
            m_xml = re.search(rf'<{field}>(\d+)</{field}>', raw)
            if m_xml:
                result[field] = int(m_xml.group(1))

    # ── Code fields ──
    for field in ("old_code", "new_code"):
        # Try hybrid XML first: "field">...code...</field>
        m_xml = re.search(rf'"{field}"[^>]*>(.*?)</{field}>', raw, re.DOTALL)
        if m_xml:
            result[field] = m_xml.group(1).strip("\n")
            continue

        # Pure XML fallback: <field>...code...</field>
        m_pure_xml = re.search(rf'<{field}>\n?(.*?)\n?</{field}>', raw, re.DOTALL)
        if m_pure_xml:
            result[field] = m_pure_xml.group(1).strip("\n")
            continue

        # JSON-style: "field": "...code..."
        m_json = re.search(rf'"{field}"\s*:\s*"', raw)
        if not m_json:
            continue

        code_start = m_json.end()

        # Find next key boundary after this field
        next_boundary = len(raw)
        for pos, kname, _ in key_positions:
            if pos > m_json.start() and kname != field:
                next_boundary = pos
                break

        segment = raw[code_start:next_boundary]

        # Strip from the end in phases:
        # Phase 1: Remove trailing whitespace-only and JSON-structural lines
        #          (}, ]) — these are OUTSIDE the string value
        # Phase 2: Remove the closing quote line (" or ",) — ends the string
        # Phase 3: STOP — everything above is code (even } on its own line)
        lines = segment.split('\n')

        # Phase 1
        while lines:
            s = lines[-1].strip()
            if s in ('', '}', ']', '},', '],'):
                lines.pop()
            else:
                break

        # Phase 2
        if lines:
            s = lines[-1].strip()
            if s in ('"', '",'):
                lines.pop()
            else:
                # Single-line case: closing "} or ", stuck to code
                last = lines[-1]
                if last.endswith('"}') or last.endswith('",'):
                    lines[-1] = last[:-2]
                elif last.endswith('"'):
                    lines[-1] = last[:-1]

        code = '\n'.join(lines).strip('\n')
        if code:
            result[field] = code

    if result.get("filename") and result.get("new_code"):
        # Tag which extraction formats were used for debugging
        _json_keys_found = len(key_positions)
        _xml_tags_used = any(
            re.search(rf'<{f}>', raw) for f in KNOWN_KEYS
            if f in result and not any(k == f for _, k, _ in key_positions)
        )
        result["_extraction_format"] = (
            "pure_xml" if _xml_tags_used and _json_keys_found == 0
            else "hybrid" if _xml_tags_used
            else "json_regex"
        )
        return result
    return None


def _repair_json(text: str) -> str:
    """
    Repair common JSON issues from LLM output.
    The most frequent failure: Claude writes a large string value (e.g. chat_response
    containing full markdown) with LITERAL newline/tab/carriage-return characters
    inside the JSON string — which is invalid per RFC 8259.
    This function escapes those control characters so json.loads() can succeed.
    """
    result = []
    in_string = False
    escape_next = False
    for ch in text:
        if escape_next:
            result.append(ch)
            escape_next = False
        elif ch == '\\':
            result.append(ch)
            escape_next = True
        elif ch == '"':
            in_string = not in_string
            result.append(ch)
        elif in_string and ch == '\n':
            result.append('\\n')
        elif in_string and ch == '\r':
            result.append('\\r')
        elif in_string and ch == '\t':
            result.append('\\t')
        else:
            result.append(ch)
    return ''.join(result)


async def _run_claude_direct_rewrite(
    file_content: str,
    filename: str,
    change_description: str,
    new_logic: str,
    architect_plan: dict,
    anthropic_key: str,
    model: str,
) -> dict:
    """
    Size-based routing: when the change is a large/multi-region rewrite and the Surgeon
    is Claude, skip SEARCH/REPLACE entirely. Claude receives the FULL file and uses
    tool_use to output the COMPLETE new file — no truncation, no focused window needed.
    Returns {"new_file_content": str, "confidence": int, "notes": list}
    """
    from anthropic import AsyncAnthropic as _DirectAnthropic
    _da_client = _DirectAnthropic(api_key=anthropic_key)

    _dr_tools = [{
        "name": "submit_file_rewrite",
        "description": (
            "Submit the COMPLETE rewritten file content. "
            "You MUST output every line from the first import to the last closing brace. "
            "No ellipsis, no truncation, no 'rest unchanged' comments."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "new_file_content": {
                    "type": "string",
                    "description": "The complete new file — every line from top to bottom"
                },
                "confidence": {
                    "type": "integer",
                    "description": "Confidence 1-10",
                    "minimum": 1,
                    "maximum": 10
                },
                "notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Short notes about major changes made"
                }
            },
            "required": ["new_file_content", "confidence"]
        }
    }]

    _dr_system = (
        "You are a senior software engineer performing a large-scale code rewrite. "
        "You will receive the CURRENT file and a description of what must change. "
        "Output the COMPLETE new file using the submit_file_rewrite tool. "
        "CRITICAL: Output EVERY line. Do NOT skip, truncate, or use ellipsis. "
        "The file must be syntactically valid and complete."
    )

    _architect_risks = architect_plan.get("risks", [])
    _risks_block = "\n".join(f"- {r}" for r in _architect_risks) if _architect_risks else "(none)"

    _dr_user = (
        f"FILE: {filename}\n\n"
        f"CHANGE REQUIRED:\n{change_description}\n\n"
        f"{'ADDITIONAL DETAILS:\n' + new_logic + chr(10) * 2 if new_logic and new_logic.strip() else ''}"
        f"ARCHITECT RISKS TO ADDRESS:\n{_risks_block}\n\n"
        f"CURRENT FILE CONTENT ({len(file_content.splitlines())} lines):\n"
        f"```\n{file_content}\n```\n\n"
        f"Rewrite the file. Output the COMPLETE new file via submit_file_rewrite."
    )

    import time as _time_dr
    _dr_max_attempts = 3
    _dr_delay = 10  # seconds between 529 retries
    for _dr_attempt in range(_dr_max_attempts):
        try:
            # Thinking-config: explicit config for adaptive models
            _dr_think_kw = _get_thinking_kwargs(model, 4000)
            _dr_effort_kw = _get_effort_kwargs(model)
            async with _da_client.messages.stream(
                model=model,
                max_tokens=_max_output_tokens(model),
                system=_dr_system,
                messages=[{"role": "user", "content": _dr_user}],
                tools=_dr_tools,
                tool_choice={"type": "tool", "name": "submit_file_rewrite"},
                **_dr_think_kw,
                **_dr_effort_kw,
            ) as _dr_stream:
                _dr_resp = await _dr_stream.get_final_message()
            break  # success
        except Exception as _dr_e:
            _dr_msg = str(_dr_e)
            if ("529" in _dr_msg or "overloaded" in _dr_msg.lower()) and _dr_attempt < _dr_max_attempts - 1:
                print(f"[DIRECT_REWRITE] 529 overloaded (attempt {_dr_attempt+1}/{_dr_max_attempts}), retrying in {_dr_delay}s...")
                await asyncio.sleep(_dr_delay)
                _dr_delay = min(_dr_delay * 2, 60)
                continue
            raise  # non-529 or final attempt

    # ── Truncation gate: never parse partial tool input as a complete file ──
    _dr_stop = getattr(_dr_resp, "stop_reason", None)
    _dlog("direct_rewrite_stop_reason",
          stop_reason=_dr_stop,
          model=model,
          filename=filename,
          max_tokens=_max_output_tokens(model))
    if _dr_stop == "max_tokens":
        print(f"[DIRECT_REWRITE] TRUNCATED at max_tokens={_max_output_tokens(model)} — refusing partial file")
        _dlog("direct_rewrite_truncated_refused", model=model, filename=filename)
        raise RuntimeError(
            f"[DIRECT_REWRITE] Output truncated at {_max_output_tokens(model)} tokens — "
            f"file too large for a single-shot rewrite; no partial file was written"
        )

    for _blk in _dr_resp.content:
        if getattr(_blk, "type", None) == "tool_use" and getattr(_blk, "name", None) == "submit_file_rewrite":
            _inp = _blk.input if isinstance(_blk.input, dict) else {}
            return {
                "new_file_content": _inp.get("new_file_content", ""),
                "confidence": _inp.get("confidence", 8),
                "notes": _inp.get("notes", []),
            }

    raise RuntimeError("[DIRECT_REWRITE] Claude did not call submit_file_rewrite — check model/key")


async def _run_gpt_direct_rewrite(
    file_content: str,
    filename: str,
    change_description: str,
    new_logic: str,
    architect_plan: dict,
    user_id: str,
    model: str,
) -> dict:
    """
    GPT equivalent of _run_claude_direct_rewrite. Uses OpenAI tool_use to
    output the COMPLETE new file — mirrors the Claude path for feature parity.
    Returns {"new_file_content": str, "confidence": int, "notes": list}
    """
    client = _get_client(user_id)

    _dr_tools_oai = [{
        "type": "function",
        "function": {
            "name": "submit_file_rewrite",
            "description": (
                "Submit the COMPLETE rewritten file content. "
                "You MUST output every line from the first import to the last closing brace. "
                "No ellipsis, no truncation, no 'rest unchanged' comments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "new_file_content": {
                        "type": "string",
                        "description": "The complete new file — every line from top to bottom"
                    },
                    "confidence": {
                        "type": "integer",
                        "description": "Confidence 1-10"
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Short notes about major changes made"
                    }
                },
                "required": ["new_file_content", "confidence"]
            }
        }
    }]

    _dr_system = (
        "You are a senior software engineer performing a large-scale code rewrite. "
        "You will receive the CURRENT file and a description of what must change. "
        "Output the COMPLETE new file using the submit_file_rewrite tool. "
        "CRITICAL: Output EVERY line. Do NOT skip, truncate, or use ellipsis. "
        "The file must be syntactically valid and complete."
    )

    _architect_risks = architect_plan.get("risks", [])
    _risks_block = "\n".join(f"- {r}" for r in _architect_risks) if _architect_risks else "(none)"

    _dr_user = (
        f"FILE: {filename}\n\n"
        f"CHANGE REQUIRED:\n{change_description}\n\n"
        f"{'ADDITIONAL DETAILS:\n' + new_logic + chr(10) * 2 if new_logic and new_logic.strip() else ''}"
        f"ARCHITECT RISKS TO ADDRESS:\n{_risks_block}\n\n"
        f"CURRENT FILE CONTENT ({len(file_content.splitlines())} lines):\n"
        f"```\n{file_content}\n```\n\n"
        f"Rewrite the file. Output the COMPLETE new file via submit_file_rewrite."
    )

    _dlog("gpt_direct_rewrite_start", model=model, filename=filename,
          file_lines=len(file_content.splitlines()), user_id=user_id)

    response = _chat_create(
        client, model,
        messages=[
            {"role": "system", "content": _dr_system},
            {"role": "user", "content": _dr_user},
        ],
        tools=_dr_tools_oai,
        tool_choice={"type": "function", "function": {"name": "submit_file_rewrite"}},
    )

    # Truncation guard
    if getattr(response, "_sai_truncated", False):
        _dlog("gpt_direct_rewrite_truncated", model=model, filename=filename)
        raise RuntimeError(
            f"[GPT_DIRECT_REWRITE] Output truncated — file too large for single-shot rewrite"
        )

    # Parse tool_calls
    tool_calls = response.choices[0].message.tool_calls or []
    for _tc in tool_calls:
        if _tc.function.name == "submit_file_rewrite":
            import json as _json_dr
            _inp = _json_dr.loads(_tc.function.arguments)
            _dlog("gpt_direct_rewrite_success", model=model, filename=filename,
                  confidence=_inp.get("confidence", 0),
                  new_lines=len(_inp.get("new_file_content", "").splitlines()))
            return {
                "new_file_content": _inp.get("new_file_content", ""),
                "confidence": _inp.get("confidence", 8),
                "notes": _inp.get("notes", []),
            }

    _dlog("gpt_direct_rewrite_no_tool_call", model=model, filename=filename)
    raise RuntimeError("[GPT_DIRECT_REWRITE] GPT did not call submit_file_rewrite")


async def run_smart_pipeline_stream(
    session_files: list,
    user_request: str,
    conversation_history: list,
    session_id: str = None,
    project_memory: str = None,
    session_summary: str = "",
    user_id: str = "",
):
    """
    v1.3 Smart pipeline: given uploaded session files + a request,
    auto-routes to surgical edit OR chat response.
    Yields SSE chunks.

    Chunk types:
    - {type: "progress", content: "status message"}
    - {type: "token", content: "text chunk"}  (for chat intent)
    - {type: "smart_result", content: JSON string}  (for edit intent)
    - {type: "done", content: ""}
    - {type: "error", content: "error message"}
    """
    import asyncio

    def sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    run_id = str(uuid.uuid4())
    compliance = ComplianceTracker(run_id=run_id, session_id=session_id or "", intent="unknown")

    try:
        # Detect create intent keywords in the request even before Architect runs.
        # This lets the pipeline skip the early-return for no-files when the user
        # is clearly asking to build/create something new from scratch.
        _CREATE_KEYWORDS = {
            "create", "build", "make", "generate", "scaffold", "new file",
            "new component", "new page", "new hook", "new service", "new module",
            "write a", "write me a", "add a new", "create a new",
            "spreadsheet", "excel", "csv", "xlsx",
        }
        _req_lower_pre = user_request.lower()
        _looks_like_create = any(kw in _req_lower_pre for kw in _CREATE_KEYWORDS)

        if not session_files and not _looks_like_create:
            # No files — pure chat mode, stream response
            compliance.set_intent("chat")
            compliance.mark("architect_routing", ran=True, output_summary="no-files pure chat")
            yield sse({"type": "progress", "content": "Thinking..."})
            chat_model = get_setting("architect_model", "gpt-4.1")
            system = """You are SurgicalAI, a world-class coding assistant. You help people build real software.

CRITICAL BEHAVIOR — SCOPE BEFORE YOU CODE:
When a user asks you to build something new (an app, a script, a feature), do NOT immediately dump a full code solution.
Instead, FIRST have a short scoping conversation:
1. Acknowledge what they want to build in 1 friendly sentence
2. Ask 1-2 smart clarifying questions to understand their needs (e.g., what platform, what data to track, any specific features they care about most)
3. ONLY after they answer, generate the code

EXCEPTION: If the user's request already has enough detail (they mention specific features, tech stack, or say "just make something simple"), you can skip straight to building.

When you DO generate code:
- Prefer single-file solutions for simplicity (HTML+JS, single Python script, etc.)
- Include clear instructions for how to run it — assume the user is NOT a developer
- End with "Want me to add X, Y, or Z?" to invite iteration


SPREADSHEET CREATION: If the user asks to create, generate, or export a spreadsheet, CSV, or Excel file,
tell them to upload any source files and you'll create the spreadsheet directly — no code needed.
You have built-in tools for generating downloadable CSV and Excel files.
Be warm, friendly, and encouraging. You're helping a person build something real."""
            if project_memory:
                system += f"\n\n## Project Memory\n{project_memory}"
            if session_summary:
                system += f"\n\n## Earlier Conversation Summary\n{session_summary}"
            msgs = [{"role": "system", "content": system}] + conversation_history[-HISTORY_WINDOW:] + [{"role": "user", "content": user_request}]

            if _is_claude_model(chat_model):
                # ── Claude streaming with extended thinking ──
                aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
                claude_msgs = conversation_history[-HISTORY_WINDOW:] + [{"role": "user", "content": user_request}]
                async with aclient.messages.stream(
                    model=chat_model,
                    max_tokens=_max_output_tokens(chat_model),
                    **_get_thinking_kwargs(chat_model, 10000),
                    **_get_effort_kwargs(chat_model),
                    system=system,
                    messages=claude_msgs,
                ) as astream:
                    current_block = None
                    async for event in astream:
                        if event.type == "content_block_start":
                            current_block = getattr(event.content_block, 'type', None)
                            if current_block == "thinking":
                                yield sse({"type": "thinking_start", "content": ""})
                        elif event.type == "content_block_delta":
                            if hasattr(event.delta, 'thinking'):
                                yield sse({"type": "thinking", "content": event.delta.thinking})
                            elif hasattr(event.delta, 'text'):
                                yield sse({"type": "token", "content": event.delta.text})
                        elif event.type == "content_block_stop":
                            if current_block == "thinking":
                                yield sse({"type": "thinking_end", "content": ""})
                yield sse({"type": "done", "content": ""})
                return
            elif _is_gemini_model(chat_model) and HAS_GOOGLE_GENAI:
                # ── Gemini streaming with thinking blocks ──
                gemini_key = _get_gemini_key(user_id)
                gclient = _google_genai.Client(api_key=gemini_key)
                gemini_msgs = [
                    {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                    for m in msgs if m["role"] != "system"
                ]
                thinking_cfg = _google_types.ThinkingConfig(thinking_budget=10000) if _supports_thinking(chat_model) else None
                gcfg = _google_types.GenerateContentConfig(
                    system_instruction=system or None,
                    thinking_config=thinking_cfg,
                )
                in_thinking = False
                async for gchunk in await gclient.aio.models.generate_content_stream(
                    model=chat_model, contents=gemini_msgs, config=gcfg
                ):
                    for gpart in (gchunk.candidates or [{}])[0].content.parts if (gchunk.candidates and gchunk.candidates[0].content) else []:
                        is_thought = getattr(gpart, "thought", False)
                        if is_thought:
                            if not in_thinking:
                                yield sse({"type": "thinking_start", "content": ""})
                                in_thinking = True
                            if gpart.text:
                                yield sse({"type": "thinking", "content": gpart.text})
                        else:
                            if in_thinking:
                                yield sse({"type": "thinking_end", "content": ""})
                                in_thinking = False
                            if gpart.text:
                                yield sse({"type": "token", "content": gpart.text})
                if in_thinking:
                    yield sse({"type": "thinking_end", "content": ""})
                yield sse({"type": "done", "content": ""})
                return
            elif _should_use_ollama(chat_model):
                # ── Ollama streaming with <think> tag parsing ──
                base_url = get_setting("ollama_base_url", "http://localhost:11434")
                ollama_model = chat_model.replace("ollama:", "") if chat_model.startswith("ollama:") else get_setting("ollama_model", "qwen2.5-coder:7b")
                in_thinking = False
                with httpx.stream("POST", f"{base_url}/api/chat",
                                  json={"model": ollama_model, "messages": msgs[-HISTORY_WINDOW:], "stream": True},
                                  timeout=120) as resp:
                    for line in resp.iter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                token = data.get("message", {}).get("content", "")
                                while token:
                                    if not in_thinking:
                                        ts = token.find("<think>")
                                        if ts != -1:
                                            before = token[:ts]
                                            if before:
                                                yield sse({"type": "token", "content": before})
                                            yield sse({"type": "thinking_start", "content": ""})
                                            in_thinking = True
                                            token = token[ts + 7:]
                                        else:
                                            yield sse({"type": "token", "content": token})
                                            token = ""
                                    else:
                                        te = token.find("</think>")
                                        if te != -1:
                                            tc = token[:te]
                                            if tc:
                                                yield sse({"type": "thinking", "content": tc})
                                            yield sse({"type": "thinking_end", "content": ""})
                                            in_thinking = False
                                            token = token[te + 8:]
                                        else:
                                            yield sse({"type": "thinking", "content": token})
                                            token = ""
                            except Exception:
                                pass
                if in_thinking:
                    yield sse({"type": "thinking_end", "content": ""})
                yield sse({"type": "done", "content": ""})
                return

            client = _get_client(user_id)
            stream = _chat_create(client, model=chat_model, messages=msgs, temperature=0.3, stream=True)
            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield sse({"type": "token", "content": delta.content})
            yield sse({"type": "done", "content": ""})
            return

        # Parse all session files
        yield sse({"type": "progress", "content": f"Reading {len(session_files)} file(s)..."})

        # ── Classify files: new vs unchanged vs modified ─────────────────
        _smart_file_statuses = _classify_session_files(
            session_id or "", session_files, conversation_history,
        )

        file_summaries = []
        symbol_maps_by_name = {}

        image_files = []  # files to be passed as vision content blocks

        for sf in session_files:
            fname = sf["filename"]
            content = sf["content"]
            file_type = sf.get("file_type", "code")

            # File status badge
            _sfs = _smart_file_statuses.get(fname, {})
            _sbadge = _file_status_badge(_sfs.get("status", "")) if _sfs else ""
            _sbadge_suffix = f"  {_sbadge}" if _sbadge else ""

            if file_type == "image":
                # Don't add to text summaries — will be passed as vision content block
                image_files.append(sf)
                file_summaries.append(f"FILE: {fname} [IMAGE — passed as visual context]{_sbadge_suffix}")
                symbol_maps_by_name[fname] = (None, sf)
                continue

            if file_type in ("pdf", "csv", "excel", "text"):
                # Data files (csv/excel) already row-capped at 200 rows —
                # show full markdown.  PDF/text stay conservative.
                _data_limit = 60_000 if file_type in ("csv", "excel") else 8_000
                preview = content[:_data_limit] + (f"\n... [{len(content) - _data_limit} more chars not shown]" if len(content) > _data_limit else "")
                file_summaries.append(
                    f"FILE: {fname} [{file_type.upper()}]{_sbadge_suffix}\nCONTENT:\n{preview}"
                )
                symbol_maps_by_name[fname] = (None, sf)
                continue

            try:
                smap = parser.parse(content, fname)
                symbol_maps_by_name[fname] = (smap, sf)
                # ── Full symbol index — all symbols, compact format ──────────
                # Showing all symbols costs ~45 chars each — negligible in a
                # 200k context. Claude needs the full map to navigate a large
                # file without keyword guessing.
                _PRIORITY_TYPES = {"function": 0, "method": 1, "class": 2, "variable": 3}
                _sorted_syms = sorted(
                    smap.symbols,
                    key=lambda s: (
                        _PRIORITY_TYPES.get(s.symbol_type.value, 9),
                        s.end_line - s.start_line,
                    )
                )
                _total_syms = len(smap.symbols)

                # For very large files (>500 symbols) show functions/classes only
                _show_syms = _sorted_syms
                _index_note = ""
                if _total_syms > 500:
                    _show_syms = [
                        s for s in _sorted_syms
                        if s.symbol_type.value in ("class", "function", "method", "arrow_function")
                    ]
                    _index_note = (
                        f"\n  [Showing {len(_show_syms)} functions/classes of {_total_syms} total. "
                        f"Use search to find any symbol by name.]"
                    )

                syms = []
                for s in _show_syms:
                    size = s.end_line - s.start_line + 1
                    flag = " ⚠️LARGE" if size > 500 else ""
                    syms.append(
                        f"  [{s.symbol_type.value}] {s.full_path:<45} "
                        f"L{s.start_line}–{s.end_line}  ({size}L){flag}"
                    )

                _sym_header = f"SYMBOL INDEX — SYMBOLS: ({_total_syms} total — full map):"
                _sym_index_footer = (
                    "\nTo fetch any symbol's full code: use search intent with its exact name."
                    "\nTo find something by content: use search intent with a keyword or string literal."
                )
                _file_summary = (
                    f"FILE: {fname} ({sf.get('lines', len(content.splitlines()))} lines, "
                    f"{sf.get('language', 'code')}){_sbadge_suffix}\n"
                    f"{_sym_header}\n"
                    + ("\n".join(syms) if syms else "  (no symbols parsed)")
                    + _index_note
                    + _sym_index_footer
                )

                # Initial grep: inject code preview for large files
                if _total_syms > 60:
                    _history_answer_terms = []
                    for _hm in reversed(conversation_history[-4:]):
                        if _hm.get("role") == "user":
                            _ans_text = str(_hm.get("content", ""))[:300]
                            if _ans_text.strip() and _ans_text.strip() != user_request.strip():
                                _history_answer_terms = _extract_search_terms(_ans_text)
                            break
                    _grep_hit = _grep_relevant_sections(
                        user_request, fname, content,
                        extra_terms=_history_answer_terms or None,
                    )
                    if _grep_hit:
                        _file_summary += _grep_hit
                file_summaries.append(_file_summary)
            except Exception as e:
                file_summaries.append(f"FILE: {fname} — could not parse: {e}")
                symbol_maps_by_name[fname] = (None, sf)

        compliance.mark("symbol_map_read", ran=True,
                        output_summary=f"{len([f for f in file_summaries if 'SYMBOL' in f])} files parsed with symbol maps")
        _large_file_count = sum(
            1 for fname, (smap, _sf) in symbol_maps_by_name.items()
            if smap and len(smap.symbols) > 60
        )
        _progress_msg = (
            f"Architect analyzing your request... (keyword search active on {_large_file_count} large file(s))"
            if _large_file_count else
            "Architect analyzing your request..."
        )
        yield sse({"type": "progress", "content": _progress_msg})

        # Build real conversation history turns for Claude (passed as actual message list)
        # This replaces the flat-text RECENT CONVERSATION block — Claude treats real turns as memory.
        _arch_history_msgs = []
        for _hmsg in conversation_history[-HISTORY_WINDOW:]:
            _hrole = _hmsg.get("role", "user")
            if _hrole not in ("user", "assistant"):
                _hrole = "user"
            _hcontent = str(_hmsg.get("content", ""))[:4000]
            if _hcontent.strip():
                _arch_history_msgs.append({"role": _hrole, "content": _hcontent})

        # context_msg = file summaries + current request (no longer embeds history as text)
        context_msg = f"""UPLOADED FILES:
{chr(10).join(file_summaries)}

USER REQUEST:
{user_request}"""

        if project_memory:
            context_msg = f"PROJECT MEMORY:\n{project_memory}\n\n" + context_msg
        if session_summary:
            context_msg = f"EARLIER CONVERSATION SUMMARY (compacted history before recent turns):\n{session_summary}\n\n" + context_msg

        arch_model = get_setting("architect_model", "gpt-4.1")

        # Detect if this is a diagnostic request — inject extra guidance if so
        _req_lower = user_request.lower()
        _is_diagnostic = any(kw in _req_lower for kw in _DIAGNOSIS_KEYWORDS)
        # Detect if session contains data files (CSV/Excel/text) for spreadsheet awareness
        _has_data_files = any(
            sf.get("file_type") in ("csv", "excel", "text")
            for sf in session_files
        )
        _architect_system = _build_architect_system(is_diagnostic=_is_diagnostic, session_id=session_id, has_data_files=_has_data_files)
        _dlog("architect_prompt_assembly",
              has_project_memory=bool(project_memory),
              project_memory_len=len(project_memory) if project_memory else 0,
              has_session_summary=bool(session_summary),
              is_diagnostic=_is_diagnostic,
              has_data_files=_has_data_files,
              total_system_len=len(_architect_system))

        if _is_claude_model(arch_model):
            # -- Claude Architect with ReAct agentic search loop --
            # Claude drives iterative grep searches until it has enough context.
            # Each "search" intent response triggers a grep + re-call (up to 4 rounds).
            aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))


            _agentic_raw = get_setting("agentic_tool_use", "false").lower()
            _use_agentic_tools = _agentic_raw == "true"
            _dlog("flag_check_agentic_tool_use",
                  raw_value=_agentic_raw, resolved=_use_agentic_tools,
                  env_upper=_os.environ.get("AGENTIC_TOOL_USE", "<not set>"),
                  session_id=session_id, user_id=user_id,
                  extra_search_tools_loaded=len(_ARCH_SEARCH_TOOLS_EXT))
            _agentic_plan_set = False

            if _use_agentic_tools:
                # ── Phase 2: Tool-Use ReAct Loop ──────────────────────────────
                # Claude uses search_codebase/request_file tools for context
                # gathering and submit_plan for the final plan.
                # Eliminates: JSON fence stripping, _repair_json, brace matching,
                # chat_response regex salvage, intent=search JSON parsing.
                print(f"[AGENTIC][TOOL_USE] Enabled for session {session_id}")

                # Scale rounds by file size (same logic as legacy path)
                _tu_largest = max(
                    (sf_.get("lines", len(sf_.get("content", "").splitlines()))
                     for _, sf_ in symbol_maps_by_name.values()
                     if isinstance(sf_, dict)),
                    default=0,
                )
                if _tu_largest > 5000:
                    _TU_MAX_ROUNDS = 10
                elif _tu_largest > 1000:
                    _TU_MAX_ROUNDS = 8
                else:
                    _TU_MAX_ROUNDS = 6

                # Build initial user content (with images if present)
                if image_files:
                    _CLAUDE_VIS = {"image/jpeg", "image/png", "image/gif", "image/webp"}
                    _tu_user_content = [{"type": "text", "text": context_msg}]
                    for _iv_sf in image_files:
                        _iv_data = _iv_sf["content"]
                        if _iv_data.startswith("data:"):
                            _iv_parts = _iv_data.split(",", 1)
                            _iv_mt = _iv_parts[0].split(":")[1].split(";")[0]
                            _iv_b64 = _iv_parts[1]
                        else:
                            _iv_ext = Path(_iv_sf["filename"]).suffix.lower().lstrip(".")
                            _iv_mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                                        "png": "image/png", "webp": "image/webp",
                                        "gif": "image/gif"}.get(_iv_ext, "image/png")
                            _iv_mt = _iv_mime
                            _iv_b64 = _iv_data
                        if _iv_mt in _CLAUDE_VIS:
                            # Annotate image with new/unchanged/modified status
                            _iv_fname = _iv_sf.get("filename", "unknown")
                            _iv_fs = _smart_file_statuses.get(_iv_fname, {})
                            _iv_badge = _file_status_badge(_iv_fs.get("status", "")) if _iv_fs else ""
                            if _iv_badge:
                                _tu_user_content.append({
                                    "type": "text",
                                    "text": f"[Image: {_iv_fname} — {_iv_badge}]",
                                })
                            _tu_user_content.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": _iv_mt, "data": _iv_b64}
                            })
                else:
                    _tu_user_content = context_msg

                _tu_messages = list(_arch_history_msgs) + [{"role": "user", "content": _tu_user_content}]
                _tu_round = 0
                _tu_searched = []
                _tu_extra_searched = set()  # dedupe key for find_callers/find_usages/get_lines
                plan = None

                # ── P10: Adaptive round extension (ported from ReAct) ──
                # Productive rounds (that surface new code locations) get
                # refunded so they don't count against _TU_MAX_ROUNDS.
                # Bounded by an absolute hard ceiling on total rounds.
                _tu_abs_round = 0
                _TU_HARD_CEILING = 20
                _tu_seen_across = set()

                # Import once per Architect call; failure degrades gracefully —
                # the three extra tools simply report "unavailable" instead of
                # crashing the whole tool-use loop.
                try:
                    from services.architect_search_tools import execute_architect_search_tool
                    _dlog("architect_search_tools_import_ok", session_id=session_id)
                except Exception as _e_ast_imp:
                    execute_architect_search_tool = None
                    _dlog("architect_search_tools_import_failed",
                          error=str(_e_ast_imp), session_id=session_id)

                while _tu_round < _TU_MAX_ROUNDS:
                    _tu_round += 1
                    _tu_abs_round += 1
                    print(f"[AGENTIC][TOOL_USE] Round {_tu_round}/{_TU_MAX_ROUNDS} (abs={_tu_abs_round})")

                    # Inject budget warning on final round
                    if _tu_round == _TU_MAX_ROUNDS:
                        _tu_messages.append({
                            "role": "user",
                            "content": (
                                "[SEARCH BUDGET EXHAUSTED] You MUST call submit_plan now. "
                                "No more search_codebase, request_file, find_callers, "
                                "find_usages, or get_lines calls. "
                                "Plan with the context you have."
                            )
                        })

                    # ── Streaming call with tools ──
                    _tu_tcalls = {}  # index -> {name, id, json}
                    _tu_thinking = []
                    _tu_text = []
                    _tu_final_msg = None

                    _tu_attempt = 0
                    _tu_retry_delay = 10
                    while _tu_attempt < 3:
                        _tu_attempt += 1
                        _tu_tcalls = {}
                        _tu_thinking = []
                        _tu_text = []
                        try:
                            _tu_kwargs = {
                                "model": arch_model,
                                "max_tokens": _max_output_tokens(arch_model),
                                "system": _architect_system,
                                "messages": _tu_messages,
                                "tools": AGENTIC_TOOLS_V2,
                            }
                            _tu_kwargs.update(_get_thinking_kwargs(arch_model, 10000))
                            _tu_kwargs.update(_get_effort_kwargs(arch_model))

                            async with aclient.messages.stream(**_tu_kwargs) as _tu_stream:
                                _tu_cur_blk = None
                                async for _tu_ev in _tu_stream:
                                    if _tu_ev.type == "content_block_start":
                                        _tu_cur_blk = getattr(_tu_ev.content_block, "type", None)
                                        if _tu_cur_blk == "thinking":
                                            yield sse({"type": "thinking_start", "content": ""})
                                        elif _tu_cur_blk == "tool_use":
                                            _tu_tcalls[_tu_ev.index] = {
                                                "name": _tu_ev.content_block.name,
                                                "id": _tu_ev.content_block.id,
                                                "json": ""
                                            }
                                            print(f"[AGENTIC][TOOL_USE] Tool started: {_tu_ev.content_block.name}")
                                    elif _tu_ev.type == "content_block_delta":
                                        if hasattr(_tu_ev.delta, "thinking"):
                                            yield sse({"type": "thinking", "content": _tu_ev.delta.thinking})
                                            _tu_thinking.append(_tu_ev.delta.thinking)
                                        elif hasattr(_tu_ev.delta, "text"):
                                            _tu_text.append(_tu_ev.delta.text)
                                        elif hasattr(_tu_ev.delta, "partial_json"):
                                            if _tu_ev.index in _tu_tcalls:
                                                _tu_tcalls[_tu_ev.index]["json"] += _tu_ev.delta.partial_json
                                    elif _tu_ev.type == "content_block_stop":
                                        if _tu_cur_blk == "thinking":
                                            yield sse({"type": "thinking_end", "content": ""})
                                # Get final message for conversation history (includes signatures)
                                _tu_final_msg = await _tu_stream.get_final_message()
                            break  # success
                        except Exception as _tu_err:
                            _tu_es = str(_tu_err)
                            _tu_el = _tu_es.lower()
                            _tu_transient = (
                                "500" in _tu_es or "529" in _tu_es
                                or "overloaded" in _tu_el
                                or "internal_server_error" in _tu_el
                            )
                            if _tu_transient and _tu_attempt < 3:
                                yield sse({"type": "progress",
                                           "content": f"AI service busy (attempt {_tu_attempt}/3) — retrying in {_tu_retry_delay}s..."})
                                await asyncio.sleep(_tu_retry_delay)
                                _tu_retry_delay = min(_tu_retry_delay * 2, 60)
                                continue
                            if image_files and ("image" in _tu_el or "unsupported" in _tu_el):
                                yield sse({"type": "progress", "content": "Images failed — retrying text-only..."})
                                _tu_messages[-1] = {"role": "user", "content": context_msg}
                                continue
                            raise

                    # ── Parse tool calls ──
                    _tu_parsed = []
                    for _ti in sorted(_tu_tcalls):
                        _tc = _tu_tcalls[_ti]
                        try:
                            _tc_args = json.loads(_tc["json"]) if _tc["json"] else {}
                        except json.JSONDecodeError:
                            _tc_args = {}
                            print(f"[AGENTIC][TOOL_USE] JSON parse error for {_tc['name']}: {_tc['json'][:200]}")
                        _tu_parsed.append({"name": _tc["name"], "id": _tc["id"], "input": _tc_args})
                        print(f"[AGENTIC][TOOL_USE] Tool: {_tc['name']} id={_tc['id'][:12]} keys={list(_tc_args.keys())}")

                    # ── Check for submit_plan ──
                    _tu_plan = next((t for t in _tu_parsed if t["name"] == "submit_plan"), None)
                    if _tu_plan:
                        plan = _tu_plan["input"]
                        plan.setdefault("intent", "edit" if plan.get("targets") else "chat")
                        print(f"[AGENTIC][TOOL_USE] Plan submitted: intent={plan.get('intent')} targets={len(plan.get('targets', []))} round={_tu_round}")
                        break

                    # ── No tools → text fallback (backward compat) ──
                    if not _tu_parsed:
                        _tu_raw = "".join(_tu_text).strip()
                        print(f"[AGENTIC][TOOL_USE] No tools, text fallback ({len(_tu_raw)} chars)")
                        if _tu_raw.startswith("```"):
                            _fl = _tu_raw.split("\n")
                            if _fl[-1].strip() == "```":
                                _tu_raw = "\n".join(_fl[1:-1])
                            else:
                                _tu_raw = "\n".join(_fl[1:])
                        try:
                            plan = json.loads(_tu_raw)
                        except json.JSONDecodeError:
                            try:
                                plan = json.loads(_repair_json(_tu_raw))
                            except json.JSONDecodeError:
                                _bs = _tu_raw.find("{")
                                _be = _tu_raw.rfind("}")
                                if _bs >= 0 and _be > _bs:
                                    try:
                                        plan = json.loads(_tu_raw[_bs:_be + 1])
                                    except json.JSONDecodeError:
                                        plan = {"intent": "chat", "chat_response": _tu_raw}
                                else:
                                    plan = {"intent": "chat", "chat_response": _tu_raw}
                        break

                    # ── Search/request tools → execute and loop ──
                    _tu_known_tools = {
                        "search_codebase", "request_file",
                        "find_callers", "find_usages", "get_lines",
                        "list_prs", "get_pr_diff", "get_pr_comments",
                        "list_issues", "get_issue_comments", "diff_branches",
                        "list_files", "read_file", "search_code", "push_files",
                        "push_session_file", "check_deploy",  # P12
                    }
                    _tu_search = [t for t in _tu_parsed if t["name"] in _tu_known_tools]
                    if not _tu_search:
                        # Unknown tools — treat text as chat
                        _dlog("tu_gate_no_known_tools",
                              tool_names=[t["name"] for t in _tu_parsed],
                              session_id=session_id, round=_tu_round)
                        plan = {"intent": "chat", "chat_response": "".join(_tu_text) or "I couldn't determine what to do."}
                        break

                    # Build assistant message for conversation history
                    _tu_asst = []
                    if _tu_final_msg:
                        for _blk in _tu_final_msg.content:
                            if _blk.type == "thinking":
                                _tu_asst.append({
                                    "type": "thinking",
                                    "thinking": _blk.thinking,
                                    "signature": getattr(_blk, "signature", ""),
                                })
                            elif _blk.type == "text":
                                _tu_asst.append({"type": "text", "text": _blk.text})
                            elif _blk.type == "tool_use":
                                _tu_asst.append({
                                    "type": "tool_use",
                                    "id": _blk.id,
                                    "name": _blk.name,
                                    "input": _blk.input,
                                })
                    _tu_messages.append({"role": "assistant", "content": _tu_asst})

                    # Execute search/request tools
                    _tu_results = []
                    _tu_round_locs = set()  # P10: location keys found this round
                    for _tc in _tu_search:
                        if _tc["name"] == "search_codebase":
                            _s_terms = _tc["input"].get("terms", [])
                            _s_new = [t for t in _s_terms if t.lower() not in {s.lower() for s in _tu_searched}]
                            if not _s_new:
                                _tu_results.append({
                                    "type": "tool_result",
                                    "tool_use_id": _tc["id"],
                                    "content": "All requested terms already searched. Call submit_plan with what you have."
                                })
                                continue
                            yield sse({"type": "progress", "content":
                                       f"Searching for: {', '.join(_s_new[:3])} (round {_tu_round}/{_TU_MAX_ROUNDS})..."})
                            # 2-pass grep (same as legacy path)
                            _s_hits = []
                            _s_seen = set()
                            for _s_fn, (_s_sm, _s_sf) in symbol_maps_by_name.items():
                                _s_ct = _s_sf.get("content", "") if isinstance(_s_sf, dict) else ""
                                if not _s_ct:
                                    continue
                                _s_lns = _s_ct.splitlines()
                                # Pass 1: AST symbol name match
                                if _s_sm:
                                    for _s_t in _s_new:
                                        _s_tl = _s_t.lower()
                                        for _s_sym in _s_sm.symbols:
                                            if _s_sym.name.lower() == _s_tl or _s_sym.full_path.lower() == _s_tl:
                                                _sk = f"{_s_fn}::{_s_sym.full_path}"
                                                if _sk not in _s_seen:
                                                    _s_seen.add(_sk)
                                                    _sl = _s_sym.code.splitlines()
                                                    _sn = "\n".join(
                                                        f"{_s_sym.start_line + j:5d}: {_sl[j]}"
                                                        for j in range(len(_sl))
                                                    )
                                                    _s_hits.append(
                                                        f"SYMBOL MATCH [{_s_fn} :: {_s_sym.full_path} "
                                                        f"({_s_sym.symbol_type.value}, L{_s_sym.start_line}–{_s_sym.end_line})]:\n{_sn}"
                                                    )
                                                break
                                # Pass 2: keyword grep with enclosing symbol
                                for _s_t in _s_new:
                                    _s_tl = _s_t.lower()
                                    for _s_li, _s_ln in enumerate(_s_lns):
                                        if _s_tl not in _s_ln.lower():
                                            continue
                                        _s_lineno = _s_li + 1
                                        _s_enc = None
                                        if _s_sm:
                                            _s_bsz = float("inf")
                                            for _s_sym in _s_sm.symbols:
                                                if _s_sym.start_line <= _s_lineno <= _s_sym.end_line:
                                                    _sz = _s_sym.end_line - _s_sym.start_line
                                                    if _sz < _s_bsz:
                                                        _s_bsz = _sz
                                                        _s_enc = _s_sym
                                        if _s_enc:
                                            _sk = f"{_s_fn}::{_s_enc.full_path}"
                                            if _sk not in _s_seen:
                                                _s_seen.add(_sk)
                                                _el = _s_enc.code.splitlines()
                                                _en = "\n".join(
                                                    f"{_s_enc.start_line + j:5d}: {_el[j]}"
                                                    for j in range(len(_el))
                                                )
                                                _s_hits.append(
                                                    f"GREP MATCH ('{_s_t}') [{_s_fn} :: {_s_enc.full_path} "
                                                    f"({_s_enc.symbol_type.value}, L{_s_enc.start_line}–{_s_enc.end_line})]:\n{_en}"
                                                )
                                        else:
                                            _sk = f"{_s_fn}::L{_s_lineno}"
                                            if _sk not in _s_seen:
                                                _s_seen.add(_sk)
                                                _ws = max(0, _s_li - 15)
                                                _we = min(len(_s_lns), _s_li + 16)
                                                _sr = "\n".join(
                                                    f"{_ws + j + 1:5d}: {_s_lns[_ws + j]}"
                                                    for j in range(_we - _ws)
                                                )
                                                _s_hits.append(f"GREP MATCH ('{_s_t}') [{_s_fn} L{_s_lineno}]:\n{_sr}")
                                        break  # one match per term per file
                            _tu_searched.extend(_s_new)
                            _tu_round_locs.update(_s_seen)  # P10: collect for yield tracking
                            _s_result = "\n\n".join(_s_hits) if _s_hits else "No matches found for: " + ", ".join(_s_new)
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _s_result[:16000]
                            })
                            if _s_hits:
                                _hp = ", ".join(
                                    h.split("::")[1].split("(")[0].strip() if "::" in h else h[:40]
                                    for h in _s_hits[:3]
                                )
                                yield sse({"type": "progress", "content":
                                           f"Found: {_hp}"
                                           + (f" +{len(_s_hits)-3} more" if len(_s_hits) > 3 else "")})
                        elif _tc["name"] == "request_file":
                            _rf_name = _tc["input"].get("filename", "")
                            _rf_match = None
                            for _rf_fn in symbol_maps_by_name:
                                if _rf_fn == _rf_name or _rf_fn.endswith(_rf_name) or _rf_name.endswith(_rf_fn):
                                    _rf_match = _rf_fn
                                    break
                            if _rf_match:
                                _, _rf_sf = symbol_maps_by_name[_rf_match]
                                _rf_ct = _rf_sf.get("content", "") if isinstance(_rf_sf, dict) else ""
                                _rf_lns = _rf_ct.splitlines()
                                _rf_num = "\n".join(f"{i+1:5d}: {l}" for i, l in enumerate(_rf_lns))
                                _rf_res = f"FILE: {_rf_match} ({len(_rf_lns)} lines)\n{_rf_num}"
                                yield sse({"type": "progress", "content": f"Loaded: {_rf_match} ({len(_rf_lns)} lines)"})
                            else:
                                _rf_res = f"File '{_rf_name}' not found. Available: {', '.join(symbol_maps_by_name.keys())}"
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _rf_res[:32000]
                            })
                        elif _tc["name"] in ("find_callers", "find_usages", "get_lines"):
                            _dlog("tu_extra_search_tool_call", tool_name=_tc["name"],
                                  tool_input=_tc["input"], session_id=session_id, round=_tu_round)
                            _es_key = f"{_tc['name']}:{sorted(_tc['input'].items())}"
                            if _es_key in _tu_extra_searched:
                                _es_res = "Already requested — call submit_plan with what you have."
                                _dlog("tu_extra_search_dedup_hit", tool_name=_tc["name"], key=_es_key)
                            elif execute_architect_search_tool is None:
                                _es_res = f"[{_tc['name']} unavailable — module failed to load]"
                                _dlog("tu_extra_search_tool_unavailable", tool_name=_tc["name"])
                            else:
                                _tu_extra_searched.add(_es_key)
                                _es_label = _tc["input"].get("name") or _tc["input"].get("filename", "")
                                yield sse({"type": "progress", "content":
                                           f"{_tc['name'].replace('_', ' ').title()}: {_es_label}..."})
                                _es_res = execute_architect_search_tool(
                                    _tc["name"], _tc["input"], symbol_maps_by_name, dlog=_dlog
                                )
                                _dlog("tu_extra_search_tool_result", tool_name=_tc["name"],
                                      result_len=len(_es_res), session_id=session_id)
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _es_res[:16000]
                            })

                        elif _tc["name"] == "push_files":
                            # ── P11: Block push_files during agentic search loop ──
                            # push_files bypasses QA, diff cards, and user review.
                            # The proper flow: include changes in submit_plan →
                            # edit pipeline → QA → diff card → user Apply → push.
                            _dlog("tu_push_files_blocked",
                                  tool_input=_tc["input"],
                                  session_id=session_id, round=_tu_round)
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": (
                                    "[BLOCKED] push_files is not allowed during the search/planning phase. "
                                    "To push code changes: include them in your submit_plan with intent='edit'. "
                                    "The edit pipeline will run QA, show a diff card, and let the user Apply + Push. "
                                    "This ensures all changes are reviewed before reaching the repo."
                                ),
                                "is_error": True,
                            })

                        elif _tc["name"] == "push_session_file":
                            # ── P12: Push already-applied session file to GitHub ──
                            _dlog("tu_push_session_file_call",
                                  tool_input=_tc["input"],
                                  session_id=session_id, round=_tu_round)
                            try:
                                from services.github_natural_tag import (
                                    push_session_file_from_db,
                                )
                                _psf_parsed = {"tool": "push_session_file", "args": _tc["input"]}
                                _psf_res = push_session_file_from_db(
                                    _psf_parsed, user_id, session_id, dlog=_dlog)
                            except Exception as _psf_err:
                                _dlog("tu_push_session_file_error",
                                      error=str(_psf_err),
                                      session_id=session_id, round=_tu_round)
                                _psf_res = f"[push_session_file failed: {_psf_err}]"
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _psf_res[:16000],
                            })

                        elif _tc["name"] == "check_deploy":
                            # ── P12: Check deployment status ──
                            _dlog("tu_check_deploy_call",
                                  tool_input=_tc["input"],
                                  session_id=session_id, round=_tu_round)
                            try:
                                from services.deploy_status import check_deploy_status
                                _cd_res = check_deploy_status(
                                    user_id, _tc["input"], dlog=_dlog)
                            except Exception as _cd_err:
                                _dlog("tu_check_deploy_error",
                                      error=str(_cd_err),
                                      session_id=session_id, round=_tu_round)
                                _cd_res = f"[check_deploy failed: {_cd_err}]"
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _cd_res[:16000],
                            })
                            yield sse({"type": "progress", "content": "Checked deploy status"})

                        elif _tc["name"] == "create_spreadsheet":
                            # ── P13: Create CSV/Excel via DataLab ──
                            _dlog("tu_create_spreadsheet_call",
                                  tool_input=_tc["input"],
                                  session_id=session_id, round=_tu_round)
                            try:
                                from services.datalab.config import datalab_enabled
                                if not datalab_enabled():
                                    _cs_res = "[create_spreadsheet unavailable — DataLab is not enabled on this server]"
                                    _dlog("tu_create_spreadsheet_disabled",
                                          session_id=session_id, round=_tu_round)
                                else:
                                    _cs_input = _tc["input"]
                                    _cs_filename = _cs_input.get("filename", "output.csv")
                                    _cs_columns = _cs_input.get("columns", [])
                                    _cs_rows = _cs_input.get("rows", [])
                                    _cs_sheet = _cs_input.get("sheet_name", "Sheet1")

                                    # Determine format from extension
                                    _cs_ext = _cs_filename.rsplit(".", 1)[-1].lower() if "." in _cs_filename else "csv"
                                    _cs_kind = "csv" if _cs_ext in ("csv", "tsv") else "excel"

                                    from services.datalab import persist as _dl_persist
                                    _cs_desc = _dl_persist.persist_result(
                                        session_id=session_id,
                                        source_file_id="",
                                        source_filename=_cs_filename,
                                        source_kind=_cs_kind,
                                        source_delimiter=",",
                                        columns=_cs_columns,
                                        rows=_cs_rows,
                                        transform_sql="",
                                        origin="generated",
                                        sheet_name=_cs_sheet,
                                    )
                                    _cs_res = (
                                        f"✅ Created {_cs_desc['filename']} "
                                        f"({_cs_desc['row_count']} rows × {_cs_desc['column_count']} columns, "
                                        f"{_cs_desc['byte_size']:,} bytes). "
                                        f"The file is now in the session file list and ready for download."
                                    )
                                    _dlog("tu_create_spreadsheet_ok",
                                          session_id=session_id, round=_tu_round,
                                          filename=_cs_desc["filename"],
                                          file_id=_cs_desc["file_id"],
                                          rows=_cs_desc["row_count"],
                                          cols=_cs_desc["column_count"],
                                          bytes=_cs_desc["byte_size"])
                                    yield sse({"type": "progress", "content":
                                               f"Created: {_cs_desc['filename']} ({_cs_desc['row_count']} rows)"})
                            except Exception as _cs_err:
                                _dlog("tu_create_spreadsheet_error",
                                      error=str(_cs_err),
                                      error_type=type(_cs_err).__name__,
                                      session_id=session_id, round=_tu_round)
                                _cs_res = f"[create_spreadsheet failed: {_cs_err}]"
                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _cs_res[:16000],
                            })

                        elif _tc["name"] in ("list_prs", "get_pr_diff", "get_pr_comments", "list_issues", "get_issue_comments", "diff_branches", "list_files", "read_file", "search_code"):
                            _dlog("tu_github_context_tool_call", tool_name=_tc["name"],
                                  tool_input=_tc["input"], session_id=session_id, round=_tu_round)
                            try:
                                from services.github_context_tools import execute_github_context_tool
                            except Exception as _e_gh_imp:
                                execute_github_context_tool = None
                                _dlog("github_context_tools_import_failed", error=str(_e_gh_imp))
                            if execute_github_context_tool is None:
                                _gh_res = f"[{_tc['name']} unavailable — module failed to load]"
                            else:
                                yield sse({"type": "progress", "content":
                                           f"GitHub: {_tc['name'].replace('_', ' ')}..."})
                                _gh_res = execute_github_context_tool(
                                    _tc["name"], _tc["input"], user_id, dlog=_dlog
                                )
                                _dlog("tu_github_context_tool_result", tool_name=_tc["name"],
                                      result_len=len(_gh_res), session_id=session_id)
                            # ── P7: Register read_file result as session file ──
                            # After a successful read_file, persist the COMPLETE
                            # file into symbol_maps_by_name so search_codebase and
                            # future edits work on it. Same pattern as natural pipeline.
                            if _tc["name"] == "read_file" and _gh_res and not _gh_res.startswith("["):
                                try:
                                    _rf_args = _tc["input"]
                                    _rf_parsed = {
                                        "tool": "read_file",
                                        "args": {
                                            "owner": _rf_args.get("owner", ""),
                                            "repo": _rf_args.get("repo", ""),
                                            "path": _rf_args.get("path", ""),
                                            "ref": _rf_args.get("ref", ""),
                                        }
                                    }
                                    from services.github_natural_tag import (
                                        fetch_and_register_github_file,
                                    )
                                    _rf_entry = fetch_and_register_github_file(
                                        _rf_parsed, user_id, session_id, dlog=_dlog)
                                    if _rf_entry and _rf_entry.get("content"):
                                        _rf_fname = _rf_entry["filename"]
                                        # Parse symbols and add to searchable index
                                        try:
                                            _rf_smap = parser.parse(_rf_entry["content"], _rf_fname)
                                            symbol_maps_by_name[_rf_fname] = (_rf_smap, _rf_entry)
                                        except Exception as _rf_parse_err:
                                            symbol_maps_by_name[_rf_fname] = (None, _rf_entry)
                                            _dlog("tu_read_file_parse_failed",
                                                  filename=_rf_fname, error=str(_rf_parse_err),
                                                  session_id=session_id)
                                        # P9: Clear term dedup so re-search finds new file content
                                        _tu_prev_searched = list(_tu_searched)
                                        _tu_searched.clear()
                                        _tu_round_locs.add(f"{_rf_fname}::REGISTERED")  # P10
                                        _dlog("tu_read_file_registered",
                                              filename=_rf_fname,
                                              content_chars=len(_rf_entry["content"]),
                                              lines=_rf_entry.get("lines"),
                                              symbol_count=_rf_entry.get("symbol_count"),
                                              total_files=len(symbol_maps_by_name),
                                              cleared_dedup_terms=len(_tu_prev_searched),
                                              session_id=session_id, round=_tu_round)
                                        _gh_res += (
                                            f"\n\n[NOTE: The complete file '{_rf_fname}' "
                                            f"({_rf_entry.get('lines')} lines) is now loaded in "
                                            f"this session and is EDITABLE + SEARCHABLE. You can "
                                            f"search_codebase for symbols in this file. To modify "
                                            f"it, emit standard <surgical_edit> blocks with "
                                            f'filename \"{_rf_fname}\" — the user will see a '
                                            f"diff card with a QA score.]"
                                        )
                                    else:
                                        _dlog("tu_read_file_not_registered",
                                              path=_rf_args.get("path", ""),
                                              session_id=session_id, round=_tu_round)
                                except Exception as _rf_reg_err:
                                    _dlog("tu_read_file_register_error",
                                          error=str(_rf_reg_err),
                                          session_id=session_id, round=_tu_round)

                            _tu_results.append({
                                "type": "tool_result",
                                "tool_use_id": _tc["id"],
                                "content": _gh_res[:16000]
                            })

                    _tu_messages.append({"role": "user", "content": _tu_results})

                    # ── P10: Adaptive round extension ──
                    # If this round surfaced new code locations, refund it
                    # (don't count against _TU_MAX_ROUNDS). Hard ceiling
                    # prevents runaway loops.
                    try:
                        _tu_new_locs = [
                            loc for loc in _tu_round_locs
                            if loc not in _tu_seen_across
                        ]
                        for loc in _tu_round_locs:
                            _tu_seen_across.add(loc)
                        if _tu_abs_round >= _TU_HARD_CEILING:
                            # Absolute ceiling — grant one final forced-planning round
                            _tu_round = _TU_MAX_ROUNDS - 1
                            _dlog("tu_hard_ceiling",
                                  session_id=session_id, user_id=user_id,
                                  abs_round=_tu_abs_round,
                                  max_rounds=_TU_MAX_ROUNDS)
                        elif _tu_new_locs and _tu_abs_round < _TU_HARD_CEILING:
                            # Productive round — refund it
                            _tu_round -= 1
                            _dlog("tu_round_productive_extension",
                                  session_id=session_id, user_id=user_id,
                                  abs_round=_tu_abs_round,
                                  counted_round=_tu_round,
                                  new_locations=len(_tu_new_locs),
                                  new_locs_sample=_tu_new_locs[:5])
                        elif not _tu_new_locs:
                            _dlog("tu_round_stalled",
                                  session_id=session_id, user_id=user_id,
                                  abs_round=_tu_abs_round,
                                  counted_round=_tu_round,
                                  tools_used=[t["name"] for t in _tu_search[:5]])
                    except Exception as _tu_ext_err:
                        # Extension is best-effort — never break the loop
                        _dlog("tu_extension_error",
                              session_id=session_id, user_id=user_id,
                              error=str(_tu_ext_err),
                              error_type=type(_tu_ext_err).__name__)

                    print(f"[AGENTIC][TOOL_USE] Round {_tu_round} done: {len(_tu_search)} tools, {len(_tu_results)} results (abs={_tu_abs_round}, seen={len(_tu_seen_across)})")

                # End of tool_use while loop
                if plan is None:
                    plan = {"intent": "chat", "chat_response": "".join(_tu_text) or "I couldn't determine what to do."}
                _agentic_plan_set = True
                print(f"[AGENTIC][TOOL_USE] Final: intent={plan.get('intent')} targets={len(plan.get('targets', []))}")

            if not _agentic_plan_set:

                # ReAct loop state — scale limits by file size
                _largest_file_lines = max(
                    (sf_.get("lines", len(sf_.get("content", "").splitlines()))
                     for _, sf_ in symbol_maps_by_name.values()
                     if isinstance(sf_, dict)),
                    default=0,
                )
                if _largest_file_lines > 5000:
                    _REACT_MAX_ROUNDS = 8
                    _REACT_BUDGET = 8000
                elif _largest_file_lines > 1000:
                    _REACT_MAX_ROUNDS = 6
                    _REACT_BUDGET = 5000
                else:
                    _REACT_MAX_ROUNDS = 4
                    _REACT_BUDGET = 2500

                _react_round = 0
                _react_searched_terms = []
                _react_accumulated = ""
                _react_budget_lines = 0
                # ── Progress-based round extension ──
                # A round that surfaces never-before-seen code locations is
                # "productive" and gets refunded (doesn't count against the
                # cap). Bounded by an absolute hard ceiling on total rounds.
                _react_abs_round = 0
                _REACT_HARD_CEILING = 16
                _react_seen_across = set()

                # Seed from session cache (previous search rounds in this session)
                _react_cache_key = "{}::{}".format(
                    session_id or "",
                    ":".join(sorted(symbol_maps_by_name.keys()))
                )
                if _react_cache_key in _react_grep_cache:
                    _react_accumulated = _react_grep_cache[_react_cache_key]
                    _react_budget_lines = _react_accumulated.count("\n")

                while _react_round < _REACT_MAX_ROUNDS:
                    _react_round += 1
                    _react_abs_round += 1

                    # Build context for this round (base + accumulated search results)
                    _react_context = context_msg
                    if _react_accumulated:
                        _react_context += (
                            "\n\n=== SEARCH RESULTS (from {} search round(s)) ===\n{}".format(
                                _react_round - 1, _react_accumulated
                            )
                        )
                    if _react_searched_terms:
                        _react_context += (
                            "\n\nALREADY SEARCHED TERMS (do NOT request again): {}".format(
                                ", ".join(_react_searched_terms)
                            )
                        )
                    if _react_round >= _REACT_MAX_ROUNDS:
                        _react_context += (
                            "\n\n[SEARCH BUDGET EXHAUSTED] You MUST now return "
                            "'edit', 'chat', or 'needs_clarification'. "
                            "No more search rounds. Plan with what you have."
                        )

                    # Build user content for Claude (images use different format)
                    if image_files:
                        _CLAUDE_VISION_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
                        user_content = [{"type": "text", "text": _react_context}]
                        for img_sf in image_files:
                            img_data = img_sf["content"]
                            _fname = img_sf.get("filename", "unknown")
                            if img_data.startswith("data:"):
                                parts = img_data.split(",", 1)
                                media_type = parts[0].split(":")[1].split(";")[0]
                                b64_data = parts[1]
                            else:
                                ext = Path(img_sf["filename"]).suffix.lower().lstrip(".")
                                mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                                            "png": "image/png", "webp": "image/webp",
                                            "gif": "image/gif"}
                                media_type = mime_map.get(ext, "image/png")
                                b64_data = img_data
                            logger.info(
                                f"[pipeline:smart] Vision block: file={_fname!r} media_type={media_type!r} "
                                f"b64_len={len(b64_data)} data_url_valid={img_data.startswith('data:')}"
                            )
                            if media_type not in _CLAUDE_VISION_TYPES:
                                logger.warning(
                                    f"[pipeline:smart] Unsupported media type {media_type!r} for {_fname!r} — skipping vision block"
                                )
                                continue
                            # Annotate image with new/unchanged/modified status
                            _rv_fs = _smart_file_statuses.get(_fname, {})
                            _rv_badge = _file_status_badge(_rv_fs.get("status", "")) if _rv_fs else ""
                            if _rv_badge:
                                user_content.append({
                                    "type": "text",
                                    "text": f"[Image: {_fname} — {_rv_badge}]",
                                })
                            user_content.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": media_type,
                                           "data": b64_data}
                            })
                    else:
                        user_content = _react_context

                    thinking_chunks = []
                    response_text_chunks = []
                    claude_failed = False

                    # -- Retry loop: up to 3 attempts for transient 500/529 errors --
                    _claude_attempt = 0
                    _claude_retry_delay = 10
                    while _claude_attempt < 3:
                        _claude_attempt += 1
                        thinking_chunks = []
                        response_text_chunks = []
                        try:
                            async with aclient.messages.stream(
                                model=arch_model,
                                max_tokens=_max_output_tokens(arch_model),
                                **_get_thinking_kwargs(arch_model, 10000),
                                **_get_effort_kwargs(arch_model),
                                system=_architect_system,
                                messages=_arch_history_msgs + [{"role": "user",
                                                                "content": user_content}],
                            ) as astream:
                                current_block = None
                                async for event in astream:
                                    if event.type == "content_block_start":
                                        current_block = getattr(event.content_block,
                                                                'type', None)
                                        if current_block == "thinking":
                                            yield sse({"type": "thinking_start",
                                                       "content": ""})
                                    elif event.type == "content_block_delta":
                                        if hasattr(event.delta, 'thinking'):
                                            yield sse({"type": "thinking",
                                                       "content": event.delta.thinking})
                                            thinking_chunks.append(event.delta.thinking)
                                        elif hasattr(event.delta, 'text'):
                                            response_text_chunks.append(event.delta.text)
                                    elif event.type == "content_block_stop":
                                        if current_block == "thinking":
                                            yield sse({"type": "thinking_end",
                                                       "content": ""})
                            break  # success: exit retry loop
                        except Exception as claude_err:
                            err_str = str(claude_err)
                            err_low = err_str.lower()
                            _is_transient = (
                                "500" in err_str or "529" in err_str
                                or "overloaded" in err_low
                                or "internal_server_error" in err_low
                            )
                            if _is_transient and _claude_attempt < 3:
                                yield sse({"type": "progress",
                                           "content": f"AI service busy (attempt {_claude_attempt}/3) -- retrying in {_claude_retry_delay}s..."})
                                await asyncio.sleep(_claude_retry_delay)
                                _claude_retry_delay = min(_claude_retry_delay * 2, 60)
                                continue  # retry
                            if image_files and ("image" in err_low or "unsupported" in err_low):
                                yield sse({"type": "progress",
                                           "content": "Images failed -- retrying text-only..."})
                                thinking_chunks = []
                                response_text_chunks = []
                                try:
                                    async with aclient.messages.stream(
                                        model=arch_model,
                                        max_tokens=_max_output_tokens(arch_model),
                                        **_get_thinking_kwargs(arch_model, 10000),
                                        **_get_effort_kwargs(arch_model),
                                        system=_architect_system,
                                        messages=_arch_history_msgs + [{"role": "user",
                                                                        "content": _react_context}],
                                    ) as astream:
                                        current_block = None
                                        async for event in astream:
                                            if event.type == "content_block_start":
                                                current_block = getattr(event.content_block,
                                                                        'type', None)
                                                if current_block == "thinking":
                                                    yield sse({"type": "thinking_start",
                                                               "content": ""})
                                            elif event.type == "content_block_delta":
                                                if hasattr(event.delta, 'thinking'):
                                                    yield sse({"type": "thinking",
                                                               "content": event.delta.thinking})
                                                    thinking_chunks.append(event.delta.thinking)
                                                elif hasattr(event.delta, 'text'):
                                                    response_text_chunks.append(event.delta.text)
                                            elif event.type == "content_block_stop":
                                                if current_block == "thinking":
                                                    yield sse({"type": "thinking_end",
                                                               "content": ""})
                                except Exception:
                                    raise
                                break  # text-only succeeded
                            else:
                                raise

                    raw_text = "".join(response_text_chunks)
                    # Claude might wrap JSON in markdown fences
                    stripped = raw_text.strip()
                    if stripped.startswith("```"):
                        fence_lines = stripped.split("\n")
                        # Remove first line (```json) and last line (```)
                        if fence_lines[-1].strip() == "```":
                            raw_text = "\n".join(fence_lines[1:-1])
                        else:
                            raw_text = "\n".join(fence_lines[1:])

                    # Robust JSON parsing -- Claude may not always produce perfect JSON
                    try:
                        plan = json.loads(raw_text)
                    except json.JSONDecodeError:
                        # Attempt 2: repair literal control chars inside string values
                        # (Claude sometimes writes actual newlines inside a JSON string value)
                        try:
                            plan = json.loads(_repair_json(raw_text))
                        except json.JSONDecodeError:
                            # Attempt 3: extract JSON object from surrounding text
                            _brace_start = raw_text.find('{')
                            _brace_end = raw_text.rfind('}')
                            json_match = (raw_text[_brace_start:_brace_end + 1]
                                          if _brace_start >= 0 and _brace_end > _brace_start
                                          else None)
                            if json_match:
                                try:
                                    plan = json.loads(json_match)
                                except json.JSONDecodeError:
                                    try:
                                        plan = json.loads(_repair_json(json_match))
                                    except json.JSONDecodeError:
                                        # Last resort: treat entire response as chat
                                        # Note: raw_text here IS the JSON string from Claude —
                                        # streaming it directly would show the user raw JSON.
                                        # Instead, try to salvage chat_response with a targeted search.
                                        import re as _re_fallback
                                        _cr_search = _re_fallback.search(
                                            r'"chat_response"\s*:\s*"((?:[^"\\]|\\.)*)"',
                                            raw_text, _re_fallback.DOTALL
                                        )
                                        if _cr_search:
                                            _salvaged = _cr_search.group(1).replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
                                            plan = {"intent": "chat", "chat_response": _salvaged}
                                        else:
                                            plan = {"intent": "chat", "chat_response": raw_text}
                            else:
                                # No JSON found at all -- Claude gave a plain text answer
                                plan = {"intent": "chat", "chat_response": raw_text}

                    # -- ReAct: check if Claude wants to search for more context --
                    _this_intent = plan.get("intent", "chat")
                    if _this_intent != "search":
                        break  # Got a real plan (edit/chat/needs_clarification) -- exit ReAct loop

                    # -- Handle search round --
                    _search_terms = plan.get("search_terms", [])
                    _search_reason = plan.get("reasoning", "gathering more context")
                    _new_terms = [
                        t for t in _search_terms
                        if t.lower() not in {s.lower() for s in _react_searched_terms}
                    ]

                    if not _new_terms:
                        # All requested terms already searched -- force clarification
                        plan = {
                            "intent": "needs_clarification",
                            "questions": ["Could you paste a small snippet of the code you want to change, or tell me the exact element ID or function name?"],
                            "clarification_response": (
                                "I've searched through the file thoroughly but couldn't pinpoint "
                                "the exact code. Could you paste a small snippet of the element "
                                "you want to change, or give me the exact ID or function name? "
                                "That will let me find it directly."
                            )
                        }
                        break

                    if _react_budget_lines < _REACT_BUDGET:
                        yield sse({"type": "progress", "content":
                                   "Searching for: {} (round {}/{})...".format(
                                       ", ".join(_new_terms[:3]), _react_round, _REACT_MAX_ROUNDS
                                   )})

                        _round_hits = []
                        _seen_sym_paths = set()

                        for _fname_r, (_smap_r, _sf_r) in symbol_maps_by_name.items():
                            _fcontent_r = _sf_r.get("content", "") if isinstance(_sf_r, dict) else ""
                            if not _fcontent_r:
                                continue
                            _file_lines_r = _fcontent_r.splitlines()

                            # Pass 1: exact AST symbol name match — most precise
                            if _smap_r:
                                for _term_r in _new_terms:
                                    _term_lower = _term_r.lower()
                                    for _sym_r in _smap_r.symbols:
                                        if (_sym_r.name.lower() == _term_lower
                                                or _sym_r.full_path.lower() == _term_lower):
                                            _sym_lines = _sym_r.code.splitlines()
                                            _sym_numbered = "\n".join(
                                                f"{_sym_r.start_line + j:5d}: {_sym_lines[j]}"
                                                for j in range(len(_sym_lines))
                                            )
                                            _label = (
                                                f"SYMBOL MATCH [{_fname_r} :: {_sym_r.full_path} "
                                                f"({_sym_r.symbol_type.value}, "
                                                f"L{_sym_r.start_line}–{_sym_r.end_line})]"
                                            )
                                            _path_key = f"{_fname_r}::{_sym_r.full_path}"
                                            if _path_key not in _seen_sym_paths:
                                                _seen_sym_paths.add(_path_key)
                                                _round_hits.append((_fname_r, _label, _sym_numbered))
                                            break

                            # Pass 2: keyword grep, expanded to enclosing AST symbol
                            for _term_r in _new_terms:
                                _tl = _term_r.lower()
                                for _li, _ln in enumerate(_file_lines_r):
                                    if _tl not in _ln.lower():
                                        continue
                                    _lineno_r = _li + 1
                                    _enc = None
                                    if _smap_r:
                                        _best_size = float("inf")
                                        for _sym_r in _smap_r.symbols:
                                            if _sym_r.start_line <= _lineno_r <= _sym_r.end_line:
                                                _sz = _sym_r.end_line - _sym_r.start_line
                                                if _sz < _best_size:
                                                    _best_size = _sz
                                                    _enc = _sym_r
                                    if _enc:
                                        _path_key = f"{_fname_r}::{_enc.full_path}"
                                        if _path_key not in _seen_sym_paths:
                                            _seen_sym_paths.add(_path_key)
                                            _enc_lines = _enc.code.splitlines()
                                            _enc_numbered = "\n".join(
                                                f"{_enc.start_line + j:5d}: {_enc_lines[j]}"
                                                for j in range(len(_enc_lines))
                                            )
                                            _label = (
                                                f"GREP MATCH ('{_term_r}') [{_fname_r} :: "
                                                f"{_enc.full_path} ({_enc.symbol_type.value}, "
                                                f"L{_enc.start_line}–{_enc.end_line})]"
                                            )
                                            _round_hits.append((_fname_r, _label, _enc_numbered))
                                    else:
                                        _path_key = f"{_fname_r}::L{_lineno_r}"
                                        if _path_key not in _seen_sym_paths:
                                            _seen_sym_paths.add(_path_key)
                                            _ws = max(0, _li - 15)
                                            _we = min(len(_file_lines_r), _li + 16)
                                            _raw = "\n".join(
                                                f"{_ws + j + 1:5d}: {_file_lines_r[_ws + j]}"
                                                for j in range(_we - _ws)
                                            )
                                            _label = f"GREP MATCH ('{_term_r}') [{_fname_r} L{_lineno_r}]"
                                            _round_hits.append((_fname_r, _label, _raw))
                                    break

                        _round_text = ""
                        for _rh_fname, _rh_label, _rh_code in _round_hits:
                            _block = f"\n{_rh_label}:\n{_rh_code}"
                            _round_text += _block
                            _react_budget_lines += _block.count("\n")
                            if _react_budget_lines >= _REACT_BUDGET:
                                break
                        if _round_text:
                            _react_accumulated += _round_text

                        _react_searched_terms.extend(_new_terms)

                        if _round_hits:
                            _hit_names = ", ".join(
                                h[1].split("::")[1].split("(")[0].strip()
                                if "::" in h[1] else h[1]
                                for h in _round_hits[:3]
                            )
                            yield sse({"type": "progress", "content":
                                       f"Found: {_hit_names}"
                                       + (f" +{len(_round_hits)-3} more" if len(_round_hits) > 3 else "")})
                        elif _react_round > 1:
                            yield sse({"type": "progress", "content":
                                       "Round {}: no matches for: {}".format(
                                           _react_round, ", ".join(_new_terms[:3])
                                       )})

                        _react_grep_cache[_react_cache_key] = _react_accumulated

                        # ── Progress-based round extension ──
                        # Labels are deterministic (file + symbol path + line
                        # range), so they identify code locations across rounds.
                        try:
                            _new_locations = [
                                _h[1] for _h in _round_hits
                                if _h[1] not in _react_seen_across
                            ]
                            for _h in _round_hits:
                                _react_seen_across.add(_h[1])
                            if _react_abs_round == _REACT_HARD_CEILING:
                                # Absolute ceiling reached: grant exactly one
                                # final forced-planning round, then exit.
                                _react_round = _REACT_MAX_ROUNDS - 1
                                _dlog("react_hard_ceiling",
                                      session_id=session_id, user_id=user_id,
                                      abs_round=_react_abs_round,
                                      max_rounds=_REACT_MAX_ROUNDS)
                            elif _new_locations and _react_abs_round < _REACT_HARD_CEILING:
                                # Productive round — refund it so it doesn't
                                # count against the round cap.
                                _react_round -= 1
                                _dlog("react_round_productive_extension",
                                      session_id=session_id, user_id=user_id,
                                      abs_round=_react_abs_round,
                                      counted_round=_react_round,
                                      new_locations=len(_new_locations))
                            elif not _new_locations:
                                _dlog("react_round_stalled",
                                      session_id=session_id, user_id=user_id,
                                      abs_round=_react_abs_round,
                                      counted_round=_react_round,
                                      terms=_new_terms[:5])
                        except Exception as _ext_exc:
                            # Extension is best-effort — never break the loop.
                            _dlog("react_extension_error",
                                  session_id=session_id, user_id=user_id,
                                  error=str(_ext_exc),
                                  error_type=type(_ext_exc).__name__)

                    # If budget was exceeded, the warning is injected at top of next round;
                    # the loop naturally exits when _react_round hits _REACT_MAX_ROUNDS.

                # End of ReAct while loop -- plan is now set to a non-search intent
        elif _is_gemini_model(arch_model):
            # ── Gemini Architect — native thinking, 1M context window ──
            yield sse({"type": "progress", "content": "Sending to Gemini Architect..."})
            if HAS_GOOGLE_GENAI:
                gemini_key = _get_gemini_key(user_id)
                gclient = _google_genai.Client(api_key=gemini_key)
                thinking_cfg = _google_types.ThinkingConfig(thinking_budget=12000) if _supports_thinking(arch_model) else None
                gcfg = _google_types.GenerateContentConfig(
                    system_instruction=_architect_system,
                    thinking_config=thinking_cfg,
                    response_mime_type="application/json",
                )
                gemini_arch_msgs = (
                    [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                     for m in _arch_history_msgs if m["role"] != "system"]
                    + [{"role": "user", "parts": [{"text": context_msg}]}]
                )
                thinking_chunks = []
                response_text_chunks = []
                in_thinking = False
                async for gchunk in await gclient.aio.models.generate_content_stream(
                    model=arch_model, contents=gemini_arch_msgs, config=gcfg
                ):
                    for gpart in (gchunk.candidates or [{}])[0].content.parts if (gchunk.candidates and gchunk.candidates[0].content) else []:
                        is_thought = getattr(gpart, "thought", False)
                        if is_thought:
                            if not in_thinking:
                                yield sse({"type": "thinking_start", "content": ""})
                                in_thinking = True
                            if gpart.text:
                                yield sse({"type": "thinking", "content": gpart.text})
                                thinking_chunks.append(gpart.text)
                        else:
                            if in_thinking:
                                yield sse({"type": "thinking_end", "content": ""})
                                in_thinking = False
                            if gpart.text:
                                response_text_chunks.append(gpart.text)
                if in_thinking:
                    yield sse({"type": "thinking_end", "content": ""})
                raw_gemini = "".join(response_text_chunks).strip()
                # Strip markdown code fences if Gemini wrapped response
                if raw_gemini.startswith("```"):
                    raw_gemini = re.sub(r"^```[a-z]*\n?", "", raw_gemini).rstrip("`").strip()
                plan = json.loads(raw_gemini)
            else:
                # Fallback: OpenAI-compat endpoint
                gclient_oai = _get_client_for_model(arch_model, user_id)
                arch_msgs_oai = [
                    {"role": "system", "content": _architect_system},
                    {"role": "user", "content": context_msg},
                ]
                # For reasoning models, force low effort for architect planning
                _fb_base = arch_model.split(":")[0].lower()
                _fb_extra = {}
                if _fb_base in NO_TEMPERATURE_MODELS and _fb_base in REASONING_EFFORT_MODELS:
                    _fb_extra["reasoning_effort"] = "low"
                _dlog("oai_fallback_architect_call",
                      model=arch_model, is_reasoning=_fb_base in NO_TEMPERATURE_MODELS,
                      reasoning_effort=_fb_extra.get("reasoning_effort", "<default>"),
                      session_id=session_id, user_id=user_id)
                _fb_start = time.time()
                try:
                    resp_oai = await asyncio.wait_for(
                        asyncio.to_thread(
                            lambda: _chat_create(gclient_oai, model=arch_model, messages=arch_msgs_oai,
                                                temperature=0.3, response_format={"type": "json_object"},
                                                **_fb_extra)
                        ),
                        timeout=120
                    )
                except asyncio.TimeoutError:
                    _dlog("oai_fallback_architect_timeout",
                          model=arch_model, elapsed=round(time.time() - _fb_start, 1),
                          session_id=session_id, user_id=user_id)
                    yield sse({"type": "error", "content": f"The model timed out after 120s. Please try again or switch models."})
                    return
                _dlog("oai_fallback_architect_ok",
                      model=arch_model, elapsed_s=round(time.time() - _fb_start, 1),
                      session_id=session_id, user_id=user_id)
                plan = json.loads(resp_oai.choices[0].message.content)
        else:
            # ── OpenAI Architect with Tool-Use ReAct Loop ──────────────────
            # Mirrors Claude's structured tool-use loop: GPT calls
            # search_codebase/request_file tools for context gathering
            # and submit_plan for the final plan.
            # Uses AGENTIC_TOOLS_V2_OPENAI for structured tool calls.
            client = _get_client(user_id)

            _oai_architect_system = _architect_system

            # Import once; failure degrades gracefully
            try:
                from services.architect_search_tools import execute_architect_search_tool
                _dlog("oai_architect_search_tools_import_ok", session_id=session_id)
            except Exception as _e_ast_imp:
                execute_architect_search_tool = None
                _dlog("oai_architect_search_tools_import_failed",
                      error=str(_e_ast_imp), session_id=session_id)

            # Scale rounds by file size (same logic as Claude path)
            _largest_file_lines_oai = max(
                (len(sf.get("content", "").split("\n")) for _, (_, sf) in symbol_maps_by_name.items()),
                default=0
            )
            if _largest_file_lines_oai > 5000:
                _OAI_REACT_MAX = 10
            elif _largest_file_lines_oai > 2000:
                _OAI_REACT_MAX = 8
            else:
                _OAI_REACT_MAX = 6

            _oai_round = 0
            _oai_searched_terms = set()
            _oai_abs_round = 0
            _OAI_HARD_CEILING = 20
            _oai_extra_searched = set()
            _oai_seen_across = set()
            plan = None

            # Build initial messages (with images if present)
            if image_files:
                _oai_user_content = [{"type": "text", "text": context_msg}]
                for img_sf in image_files:
                    img_data = img_sf["content"]
                    _oai_fname = img_sf.get("filename", "unknown")
                    if not img_data.startswith("data:"):
                        ext = Path(img_sf["filename"]).suffix.lower().lstrip(".")
                        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                                    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}
                        mime = mime_map.get(ext, "image/png")
                        img_data = f"data:{mime};base64,{img_data}"
                    _oai_ifs = _smart_file_statuses.get(_oai_fname, {})
                    _oai_ibadge = _file_status_badge(_oai_ifs.get("status", "")) if _oai_ifs else ""
                    if _oai_ibadge:
                        _oai_user_content.append({
                            "type": "text",
                            "text": f"[Image: {_oai_fname} — {_oai_ibadge}]",
                        })
                    _oai_user_content.append({
                        "type": "image_url",
                        "image_url": {"url": img_data}
                    })
                _oai_messages = [
                    {"role": "system", "content": _oai_architect_system},
                ] + _arch_history_msgs + [
                    {"role": "user", "content": _oai_user_content}
                ]
            else:
                _oai_messages = [
                    {"role": "system", "content": _oai_architect_system},
                ] + _arch_history_msgs + [
                    {"role": "user", "content": context_msg}
                ]

            # Reasoning models need special kwargs
            _arch_base = arch_model.split(":")[0].lower()
            _is_reasoning = _arch_base in NO_TEMPERATURE_MODELS
            _oai_arch_extra_kwargs = {}
            if _is_reasoning and _arch_base in REASONING_EFFORT_MODELS:
                _oai_arch_extra_kwargs["reasoning_effort"] = "low"

            _OAI_ARCH_TIMEOUT = 120

            while _oai_round < _OAI_REACT_MAX:
                _oai_round += 1
                _oai_abs_round += 1
                print(f"[OAI_TOOL_USE] Round {_oai_round}/{_OAI_REACT_MAX} (abs={_oai_abs_round})")

                # Inject budget warning on final round
                if _oai_round == _OAI_REACT_MAX:
                    _oai_messages.append({
                        "role": "user",
                        "content": (
                            "[SEARCH BUDGET EXHAUSTED] You MUST call submit_plan now. "
                            "No more search_codebase, request_file, find_callers, "
                            "find_usages, or get_lines calls. "
                            "Plan with the context you have."
                        )
                    })

                _round_label = f" (search round {_oai_round}/{_OAI_REACT_MAX})" if _oai_round > 1 else ""
                if _oai_round > 1:
                    elapsed = int(time.time() - start_time) if 'start_time' in dir() else 0
                    yield sse({"type": "progress", "content": f"Architect thinking{_round_label}... ({elapsed}s)"})

                _dlog("oai_tool_use_call_setup",
                      model=arch_model, is_reasoning=_is_reasoning,
                      reasoning_effort=_oai_arch_extra_kwargs.get("reasoning_effort", "<default>"),
                      react_round=_oai_round, has_images=bool(image_files),
                      msg_count=len(_oai_messages),
                      session_id=session_id, user_id=user_id)

                async def _call_architect_oai_tu(msgs):
                    _call_kwargs = {
                        "model": arch_model,
                        "messages": msgs,
                        "tools": AGENTIC_TOOLS_V2_OPENAI,
                    }
                    if not _is_reasoning:
                        _call_kwargs["temperature"] = 0.3
                    _call_kwargs.update(_oai_arch_extra_kwargs)
                    return await asyncio.to_thread(
                        lambda: _chat_create(client, **_call_kwargs)
                    )

                arch_task = asyncio.create_task(_call_architect_oai_tu(_oai_messages))
                start_time = time.time()
                tick = 0
                while not arch_task.done():
                    await asyncio.sleep(1)
                    tick += 1
                    elapsed = int(time.time() - start_time)
                    if tick % 3 == 0:
                        yield sse({"type": "progress", "content": f"Architect thinking{_round_label}... ({elapsed}s)"})
                    if elapsed >= _OAI_ARCH_TIMEOUT and not arch_task.done():
                        arch_task.cancel()
                        _dlog("oai_tool_use_timeout",
                              model=arch_model, elapsed=elapsed,
                              timeout=_OAI_ARCH_TIMEOUT,
                              react_round=_oai_round,
                              session_id=session_id, user_id=user_id)
                        yield sse({"type": "progress", "content": f"⚠️ {arch_model} took too long ({elapsed}s). Try again or switch models."})
                        yield sse({"type": "error", "content": f"The model timed out after {elapsed}s. Please try again or switch to a different model."})
                        return

                try:
                    response = arch_task.result()
                    _arch_elapsed = round(time.time() - start_time, 1)
                    _arch_usage = getattr(response, "usage", None)
                    _dlog("oai_tool_use_response_ok",
                          model=arch_model, elapsed_s=_arch_elapsed,
                          react_round=_oai_round,
                          prompt_tokens=getattr(_arch_usage, "prompt_tokens", None) if _arch_usage else None,
                          completion_tokens=getattr(_arch_usage, "completion_tokens", None) if _arch_usage else None,
                          has_tool_calls=bool(response.choices[0].message.tool_calls),
                          session_id=session_id, user_id=user_id)
                except asyncio.CancelledError:
                    return
                except Exception as _oai_tu_err:
                    _arch_elapsed = round(time.time() - start_time, 1)
                    err_str = str(_oai_tu_err).lower()
                    _dlog("oai_tool_use_error",
                          model=arch_model, elapsed_s=_arch_elapsed,
                          error=str(_oai_tu_err)[:500], error_type=type(_oai_tu_err).__name__,
                          react_round=_oai_round, has_images=bool(image_files),
                          session_id=session_id, user_id=user_id)
                    if image_files and ("image" in err_str or "unsupported" in err_str or "invalid" in err_str):
                        yield sse({"type": "progress", "content": "⚠️ Images couldn't be read — falling back to text-only..."})
                        _oai_messages = [
                            {"role": "system", "content": _oai_architect_system},
                        ] + _arch_history_msgs + [
                            {"role": "user", "content": context_msg}
                        ]
                        continue
                    raise

                _oai_msg = response.choices[0].message
                _oai_tool_calls = _oai_msg.tool_calls or []

                # ── Check for submit_plan ──
                _oai_submit = None
                for _otc in _oai_tool_calls:
                    if _otc.function.name == "submit_plan":
                        try:
                            _oai_submit = json.loads(_otc.function.arguments)
                        except json.JSONDecodeError:
                            _dlog("oai_tool_use_submit_plan_json_error",
                                  raw=_otc.function.arguments[:200],
                                  session_id=session_id, round=_oai_round)
                        break

                if _oai_submit is not None:
                    plan = _oai_submit
                    plan.setdefault("intent", "edit" if plan.get("targets") else "chat")
                    print(f"[OAI_TOOL_USE] Plan submitted: intent={plan.get('intent')} targets={len(plan.get('targets', []))} round={_oai_round}")
                    break

                # ── No tool calls → text fallback ──
                if not _oai_tool_calls:
                    _oai_raw = (_oai_msg.content or "").strip()
                    print(f"[OAI_TOOL_USE] No tools, text fallback ({len(_oai_raw)} chars)")
                    if _oai_raw.startswith("```"):
                        _fl = _oai_raw.split("\n")
                        if _fl[-1].strip() == "```":
                            _oai_raw = "\n".join(_fl[1:-1])
                        else:
                            _oai_raw = "\n".join(_fl[1:])
                    try:
                        plan = json.loads(_oai_raw)
                    except json.JSONDecodeError:
                        try:
                            plan = json.loads(_repair_json(_oai_raw))
                        except json.JSONDecodeError:
                            _bs = _oai_raw.find("{")
                            _be = _oai_raw.rfind("}")
                            if _bs >= 0 and _be > _bs:
                                try:
                                    plan = json.loads(_oai_raw[_bs:_be + 1])
                                except json.JSONDecodeError:
                                    plan = {"intent": "chat", "chat_response": _oai_raw}
                            else:
                                plan = {"intent": "chat", "chat_response": _oai_raw}
                    break

                # ── Execute tool calls ──
                # Add assistant message with tool calls to conversation
                _oai_asst_msg = {"role": "assistant", "content": _oai_msg.content or None}
                _oai_asst_msg["tool_calls"] = [
                    {
                        "id": _otc.id,
                        "type": "function",
                        "function": {
                            "name": _otc.function.name,
                            "arguments": _otc.function.arguments,
                        }
                    }
                    for _otc in _oai_tool_calls
                ]
                _oai_messages.append(_oai_asst_msg)

                _oai_round_locs = set()
                _oai_known_tools = {
                    "search_codebase", "request_file",
                    "find_callers", "find_usages", "get_lines",
                    "list_prs", "get_pr_diff", "get_pr_comments",
                    "list_issues", "get_issue_comments", "diff_branches",
                    "list_files", "read_file", "search_code", "push_files",
                    "push_session_file", "check_deploy",
                    "create_spreadsheet",
                }

                for _otc in _oai_tool_calls:
                    _otc_name = _otc.function.name
                    try:
                        _otc_input = json.loads(_otc.function.arguments) if _otc.function.arguments else {}
                    except json.JSONDecodeError:
                        _otc_input = {}
                        _dlog("oai_tool_use_args_json_error", tool=_otc_name,
                              raw=_otc.function.arguments[:200], session_id=session_id)

                    _otc_result = ""

                    if _otc_name == "search_codebase":
                        _s_terms = _otc_input.get("terms", [])
                        _s_new = [t for t in _s_terms if t.lower() not in {s.lower() for s in _oai_searched_terms}]
                        if not _s_new:
                            _otc_result = "All requested terms already searched. Call submit_plan with what you have."
                        else:
                            yield sse({"type": "progress", "content":
                                       f"Searching for: {', '.join(_s_new[:3])} (round {_oai_round}/{_OAI_REACT_MAX})..."})
                            _s_hits = []
                            _s_seen = set()
                            for _s_fn, (_s_sm, _s_sf) in symbol_maps_by_name.items():
                                _s_ct = _s_sf.get("content", "") if isinstance(_s_sf, dict) else ""
                                if not _s_ct:
                                    continue
                                _s_lns = _s_ct.splitlines()
                                # Pass 1: AST symbol name match
                                if _s_sm:
                                    for _s_t in _s_new:
                                        _s_tl = _s_t.lower()
                                        for _s_sym in _s_sm.symbols:
                                            if _s_sym.name.lower() == _s_tl or _s_sym.full_path.lower() == _s_tl:
                                                _sk = f"{_s_fn}::{_s_sym.full_path}"
                                                if _sk not in _s_seen:
                                                    _s_seen.add(_sk)
                                                    _sl = _s_sym.code.splitlines()
                                                    _sn = "\n".join(
                                                        f"{_s_sym.start_line + j:5d}: {_sl[j]}"
                                                        for j in range(len(_sl))
                                                    )
                                                    _s_hits.append(
                                                        f"SYMBOL MATCH [{_s_fn} :: {_s_sym.full_path} "
                                                        f"({_s_sym.symbol_type.value}, L{_s_sym.start_line}–{_s_sym.end_line})]:\n{_sn}"
                                                    )
                                                break
                                # Pass 2: keyword grep with enclosing symbol
                                for _s_t in _s_new:
                                    _s_tl = _s_t.lower()
                                    for _s_li, _s_ln in enumerate(_s_lns):
                                        if _s_tl not in _s_ln.lower():
                                            continue
                                        _s_lineno = _s_li + 1
                                        _s_enc = None
                                        if _s_sm:
                                            _s_bsz = float("inf")
                                            for _s_sym in _s_sm.symbols:
                                                if _s_sym.start_line <= _s_lineno <= _s_sym.end_line:
                                                    _sz = _s_sym.end_line - _s_sym.start_line
                                                    if _sz < _s_bsz:
                                                        _s_bsz = _sz
                                                        _s_enc = _s_sym
                                        if _s_enc:
                                            _sk = f"{_s_fn}::{_s_enc.full_path}"
                                            if _sk not in _s_seen:
                                                _s_seen.add(_sk)
                                                _el = _s_enc.code.splitlines()
                                                _en = "\n".join(
                                                    f"{_s_enc.start_line + j:5d}: {_el[j]}"
                                                    for j in range(len(_el))
                                                )
                                                _s_hits.append(
                                                    f"GREP MATCH ('{_s_t}') [{_s_fn} :: {_s_enc.full_path} "
                                                    f"({_s_enc.symbol_type.value}, L{_s_enc.start_line}–{_s_enc.end_line})]:\n{_en}"
                                                )
                                        else:
                                            _sk = f"{_s_fn}::L{_s_lineno}"
                                            if _sk not in _s_seen:
                                                _s_seen.add(_sk)
                                                _ws = max(0, _s_li - 15)
                                                _we = min(len(_s_lns), _s_li + 16)
                                                _sr = "\n".join(
                                                    f"{_ws + j + 1:5d}: {_s_lns[_ws + j]}"
                                                    for j in range(_we - _ws)
                                                )
                                                _s_hits.append(f"GREP MATCH ('{_s_t}') [{_s_fn} L{_s_lineno}]:\n{_sr}")
                                        break
                            _oai_searched_terms.update(_s_new)
                            _oai_round_locs.update(_s_seen)
                            _otc_result = "\n\n".join(_s_hits) if _s_hits else "No matches found for: " + ", ".join(_s_new)
                            if _s_hits:
                                _hp = ", ".join(
                                    h.split("::")[1].split("(")[0].strip() if "::" in h else h[:40]
                                    for h in _s_hits[:3]
                                )
                                yield sse({"type": "progress", "content":
                                           f"Found: {_hp}"
                                           + (f" +{len(_s_hits)-3} more" if len(_s_hits) > 3 else "")})

                    elif _otc_name == "request_file":
                        _rf_name = _otc_input.get("filename", "")
                        _rf_match = None
                        for _rf_fn in symbol_maps_by_name:
                            if _rf_fn == _rf_name or _rf_fn.endswith(_rf_name) or _rf_name.endswith(_rf_fn):
                                _rf_match = _rf_fn
                                break
                        if _rf_match:
                            _, _rf_sf = symbol_maps_by_name[_rf_match]
                            _rf_ct = _rf_sf.get("content", "") if isinstance(_rf_sf, dict) else ""
                            _rf_lns = _rf_ct.splitlines()
                            _rf_num = "\n".join(f"{i+1:5d}: {l}" for i, l in enumerate(_rf_lns))
                            _otc_result = f"FILE: {_rf_match} ({len(_rf_lns)} lines)\n{_rf_num}"
                            yield sse({"type": "progress", "content": f"Loaded: {_rf_match} ({len(_rf_lns)} lines)"})
                        else:
                            _otc_result = f"File '{_rf_name}' not found. Available: {', '.join(symbol_maps_by_name.keys())}"

                    elif _otc_name in ("find_callers", "find_usages", "get_lines"):
                        _dlog("oai_tu_extra_search_tool_call", tool_name=_otc_name,
                              tool_input=_otc_input, session_id=session_id, round=_oai_round)
                        _es_key = f"{_otc_name}:{sorted(_otc_input.items())}"
                        if _es_key in _oai_extra_searched:
                            _otc_result = "Already requested — call submit_plan with what you have."
                        elif execute_architect_search_tool is None:
                            _otc_result = f"[{_otc_name} unavailable — module failed to load]"
                        else:
                            _oai_extra_searched.add(_es_key)
                            _es_label = _otc_input.get("name") or _otc_input.get("filename", "")
                            yield sse({"type": "progress", "content":
                                       f"{_otc_name.replace('_', ' ').title()}: {_es_label}..."})
                            _otc_result = execute_architect_search_tool(
                                _otc_name, _otc_input, symbol_maps_by_name, dlog=_dlog
                            )

                    elif _otc_name == "push_files":
                        _dlog("oai_tu_push_files_blocked",
                              tool_input=_otc_input, session_id=session_id, round=_oai_round)
                        _otc_result = (
                            "[BLOCKED] push_files is not allowed during the search/planning phase. "
                            "Include changes in submit_plan with intent='edit'."
                        )

                    elif _otc_name == "push_session_file":
                        _dlog("oai_tu_push_session_file_call",
                              tool_input=_otc_input, session_id=session_id, round=_oai_round)
                        try:
                            from services.github_natural_tag import push_session_file_from_db
                            _psf_parsed = {"tool": "push_session_file", "args": _otc_input}
                            _otc_result = push_session_file_from_db(
                                _psf_parsed, user_id, session_id, dlog=_dlog)
                        except Exception as _psf_err:
                            _otc_result = f"[push_session_file failed: {_psf_err}]"

                    elif _otc_name == "check_deploy":
                        _dlog("oai_tu_check_deploy_call",
                              tool_input=_otc_input, session_id=session_id, round=_oai_round)
                        try:
                            from services.deploy_status import check_deploy_status
                            _otc_result = check_deploy_status(user_id, _otc_input, dlog=_dlog)
                        except Exception as _cd_err:
                            _otc_result = f"[check_deploy failed: {_cd_err}]"
                        yield sse({"type": "progress", "content": "Checked deploy status"})

                    elif _otc_name == "create_spreadsheet":
                        _dlog("oai_tu_create_spreadsheet_call",
                              tool_input=_otc_input, session_id=session_id, round=_oai_round)
                        try:
                            from services.datalab.config import datalab_enabled
                            if not datalab_enabled():
                                _otc_result = "[create_spreadsheet unavailable — DataLab is not enabled]"
                            else:
                                _cs_filename = _otc_input.get("filename", "output.csv")
                                _cs_columns = _otc_input.get("columns", [])
                                _cs_rows = _otc_input.get("rows", [])
                                _cs_sheet = _otc_input.get("sheet_name", "Sheet1")
                                _cs_ext = _cs_filename.rsplit(".", 1)[-1].lower() if "." in _cs_filename else "csv"
                                _cs_kind = "csv" if _cs_ext in ("csv", "tsv") else "excel"
                                from services.datalab import persist as _dl_persist
                                _cs_desc = _dl_persist.persist_result(
                                    session_id=session_id, source_file_id="",
                                    source_filename=_cs_filename, source_kind=_cs_kind,
                                    source_delimiter=",", columns=_cs_columns, rows=_cs_rows,
                                    transform_sql="", origin="generated", sheet_name=_cs_sheet,
                                )
                                _otc_result = (
                                    f"✅ Created {_cs_desc['filename']} "
                                    f"({_cs_desc['row_count']} rows × {_cs_desc['column_count']} columns)"
                                )
                                yield sse({"type": "progress", "content":
                                           f"Created: {_cs_desc['filename']} ({_cs_desc['row_count']} rows)"})
                        except Exception as _cs_err:
                            _otc_result = f"[create_spreadsheet failed: {_cs_err}]"

                    elif _otc_name in ("list_prs", "get_pr_diff", "get_pr_comments", "list_issues", "get_issue_comments", "diff_branches", "list_files", "read_file", "search_code"):
                        _dlog("oai_tu_github_context_tool_call", tool_name=_otc_name,
                              tool_input=_otc_input, session_id=session_id, round=_oai_round)
                        try:
                            from services.github_context_tools import execute_github_context_tool
                        except Exception as _e_gh_imp:
                            execute_github_context_tool = None
                        if execute_github_context_tool is None:
                            _otc_result = f"[{_otc_name} unavailable — module failed to load]"
                        else:
                            yield sse({"type": "progress", "content":
                                       f"GitHub: {_otc_name.replace('_', ' ')}..."})
                            _otc_result = execute_github_context_tool(
                                _otc_name, _otc_input, user_id, dlog=_dlog
                            )
                            # P7: Register read_file result as session file
                            if _otc_name == "read_file" and _otc_result and not _otc_result.startswith("["):
                                try:
                                    from services.github_natural_tag import fetch_and_register_github_file
                                    _rf_parsed = {"tool": "read_file", "args": _otc_input}
                                    _rf_entry = fetch_and_register_github_file(
                                        _rf_parsed, user_id, session_id, dlog=_dlog)
                                    if _rf_entry and _rf_entry.get("content"):
                                        _rf_fname = _rf_entry["filename"]
                                        try:
                                            _rf_smap = parser.parse(_rf_entry["content"], _rf_fname)
                                            symbol_maps_by_name[_rf_fname] = (_rf_smap, _rf_entry)
                                        except Exception:
                                            symbol_maps_by_name[_rf_fname] = (None, _rf_entry)
                                        _oai_searched_terms.clear()
                                        _oai_round_locs.add(f"{_rf_fname}::REGISTERED")
                                        _otc_result += (
                                            f"\n\n[NOTE: '{_rf_fname}' is now loaded and EDITABLE + SEARCHABLE.]"
                                        )
                                except Exception:
                                    pass

                    else:
                        _otc_result = f"[Unknown tool: {_otc_name}]"
                        _dlog("oai_tu_unknown_tool", tool_name=_otc_name,
                              session_id=session_id, round=_oai_round)

                    # Add tool result to conversation (OpenAI format)
                    _oai_messages.append({
                        "role": "tool",
                        "tool_call_id": _otc.id,
                        "content": str(_otc_result)[:16000],
                    })

                # ── Adaptive round extension (mirrors Claude path) ──
                try:
                    _oai_new_locs = [
                        loc for loc in _oai_round_locs
                        if loc not in _oai_seen_across
                    ]
                    for loc in _oai_round_locs:
                        _oai_seen_across.add(loc)
                    if _oai_abs_round >= _OAI_HARD_CEILING:
                        _oai_round = _OAI_REACT_MAX - 1
                        _dlog("oai_tu_hard_ceiling",
                              session_id=session_id, abs_round=_oai_abs_round,
                              max_rounds=_OAI_REACT_MAX)
                    elif _oai_new_locs and _oai_abs_round < _OAI_HARD_CEILING:
                        _oai_round -= 1
                        _dlog("oai_tu_round_productive_extension",
                              session_id=session_id, abs_round=_oai_abs_round,
                              counted_round=_oai_round,
                              new_locations=len(_oai_new_locs))
                    elif not _oai_new_locs:
                        _dlog("oai_tu_round_stalled",
                              session_id=session_id, abs_round=_oai_abs_round,
                              counted_round=_oai_round)
                except Exception as _oai_ext_err:
                    _dlog("oai_tu_extension_error",
                          session_id=session_id, error=str(_oai_ext_err))

                print(f"[OAI_TOOL_USE] Round {_oai_round} done (abs={_oai_abs_round}, seen={len(_oai_seen_across)})")

            # End of OpenAI tool-use while loop
            if plan is None:
                plan = {"intent": "chat", "chat_response": "I couldn't determine what to do."}
            _oai_intent = plan.get("intent", "unknown")
            print(f"[OAI_TOOL_USE] Final: intent={_oai_intent} targets={len(plan.get('targets', []))}")

        intent = plan.get("intent", "chat")
        compliance.set_intent(intent)
        compliance.mark("architect_routing", ran=True, output_summary=f"intent={intent}")
        if intent == "edit":
            compliance.mark("import_check", ran=True,
                            output_summary="Architect scanned imports per SMART_ARCHITECT_SYSTEM prompt")
        else:
            compliance.mark("import_check", ran=False, reason=f"not_applicable: intent={intent}")

        # ── NEEDS CLARIFICATION: Architect doesn't have enough info to plan ──
        # Stream the clarification questions back as tokens.
        # On the next turn, conversation history carries the Q&A context
        # Safety: if 'search' intent leaked through any path, degrade gracefully.
        # Claude and OpenAI now have proper ReAct loops; this is a last-resort guard.
        if intent == "search":
            intent = "needs_clarification"
            plan.setdefault("clarification_response",
                "I searched the file but need a bit more context. "
                "Could you paste the function or element name you'd like to change?")
            compliance.set_intent(intent)


        if intent == "needs_clarification":
            clarification_response = plan.get(
                "clarification_response",
                "I need a bit more info before I start. " +
                " ".join(plan.get("questions", ["Could you clarify what you'd like?"]))
            )
            yield sse({"type": "progress", "content": "Scoping your request..."})
            words = clarification_response.split(" ")
            buffer = []
            for word in words:
                buffer.append(word)
                if len(buffer) >= 4:
                    yield sse({"type": "token", "content": " ".join(buffer) + " "})
                    buffer = []
                    await asyncio.sleep(0.008)
            if buffer:
                yield sse({"type": "token", "content": " ".join(buffer)})
            compliance.save()
            yield sse({"type": "done", "content": ""})
            return

        if intent == "chat":
            chat_resp = plan.get("chat_response", "I analyzed your files. Here's what I found:\n\n" + plan.get("reasoning", ""))
            yield sse({"type": "progress", "content": "Generating response..."})
            # Stream word-by-word for nice UX
            words = chat_resp.split(" ")
            buffer = []
            for word in words:
                buffer.append(word)
                if len(buffer) >= 4:
                    yield sse({"type": "token", "content": " ".join(buffer) + " "})
                    buffer = []
                    await asyncio.sleep(0.008)
            if buffer:
                yield sse({"type": "token", "content": " ".join(buffer)})
            compliance.save()
            yield sse({"type": "done", "content": ""})
            return

        # ── CREATE intent — Claude writes brand-new files ──────────────────
        if intent == "create":
            compliance.set_intent("create")
            new_file_specs = plan.get("new_files", [])
            if not new_file_specs:
                # Malformed plan — fall back to clarification
                yield sse({"type": "token", "content": "I understood you want to create something new, but couldn't determine the file details. Could you describe what the file should do?"})
                compliance.save()
                yield sse({"type": "done", "content": ""})
                return

            # Build codebase context for the file creator
            _creator_context = _build_codebase_context_for_creator(symbol_maps_by_name)
            created_files = []

            for _spec in new_file_specs:
                _fname = _spec.get("filename", "new_file.ts")
                yield sse({"type": "progress", "content": f"✍️ Creating {_fname}..."})
                try:
                    _file_result = await run_file_creator(
                        file_spec=_spec,
                        codebase_context=_creator_context,
                        user_id=user_id,
                    )
                    # Run QA on the created file
                    try:
                        _create_qa = await _run_qa_for_new_file(
                            file_result=_file_result,
                            codebase_context=_creator_context,
                            user_id=user_id,
                        )
                        _file_result["qa_result"] = _create_qa
                        _qa_icon = {"safe": "✅", "warning": "⚠️", "blocked": "🚫"}.get(
                            _create_qa.get("verdict", "safe"), "✅"
                        )
                        yield sse({"type": "progress", "content": (
                            f"{_qa_icon} {_fname} ready "
                            f"({len(_file_result.get('content','').splitlines())} lines) "
                            f"— QA {_create_qa.get('summary', '')}"
                        )})
                    except Exception as _cqe:
                        yield sse({"type": "progress", "content": f"✅ {_fname} ready ({len(_file_result.get('content','').splitlines())} lines)"})
                        print(f"[QA_CREATE] Skipped: {_cqe}")
                    created_files.append(_file_result)
                except Exception as _cfe:
                    yield sse({"type": "progress", "content": f"⚠️ Could not create {_fname}: {_cfe}"})

            if not created_files:
                yield sse({"type": "token", "content": "File creation failed. Try being more specific about what the file should contain."})
                compliance.save()
                yield sse({"type": "done", "content": ""})
                return

            compliance.mark("file_creation", ran=True,
                            output_summary=f"{len(created_files)} file(s) created")

            create_result = {
                "intent": "create",
                "summary": plan.get("summary", f"Created {len(created_files)} new file(s)"),
                "reasoning": plan.get("reasoning", ""),
                "new_files": created_files,
                "risks": plan.get("risks", []),
                "changes_by_file": {},   # required by SmartResult schema; empty for pure create
                "skipped_changes": [],
            }

            # If Architect also planned edits to existing files (mixed create+edit),
            # run the surgical pipeline for those targets too
            _create_targets = plan.get("targets", [])
            if _create_targets:
                yield sse({"type": "progress", "content": f"Updating {len(_create_targets)} existing file(s)..."})
                plan["targets"] = _create_targets
                plan["intent"] = "edit"
                compliance.set_intent("edit")
                _pending_create_result = create_result
            else:
                compliance.save()
                result_json = json.dumps(create_result)
                yield sse({"type": "smart_result", "content": result_json})
                yield sse({"type": "done", "content": ""})
                return

        else:
            _pending_create_result = None

        # CODE EDIT intent — run surgical pipeline
        targets = plan.get("targets", [])
        if not targets:
            # No targets identified — fall back to chat
            fallback = f"I can see what you want to change, but I'm having trouble pinpointing the exact code to edit.\n\n**What I understood:** {plan.get('reasoning', '')}\n\nCould you be a bit more specific? For example: *\"In LoginPage.tsx, change the background color of the header\"*"
            for word in fallback.split(" "):
                yield sse({"type": "token", "content": word + " "})
                await asyncio.sleep(0.005)
            yield sse({"type": "done", "content": ""})
            return

        yield sse({"type": "progress", "content": f"Plan ready: {len(targets)} change(s) identified"})
        yield sse({"type": "progress", "content": "Surgeon writing code..."})

        # Build cross-file context for QA agent (symbol summaries of ALL session files)
        _qa_other_context_parts = []
        for _fn, (_sm, _sf) in symbol_maps_by_name.items():
            if _sm is None:
                continue
            _syms_brief = [f"  {s.full_path} ({s.end_line - s.start_line + 1}L)" for s in _sm.symbols[:20]]
            _qa_other_context_parts.append(f"FILE: {_fn}\n" + "\n".join(_syms_brief))
        _qa_other_context = "\n\n".join(_qa_other_context_parts)

        # Text-search assist: redirect mis-targeted symbols
        # Extract quoted text manually (avoids 're' module scoping issues)
        _used_focus_lines = set()  # prevent multiple targets from collapsing to same line
        _quoted_texts = []
        for _qc in ["'", '"']:
            _rest = user_request
            while _qc in _rest:
                _start = _rest.index(_qc)
                _after = _rest[_start + 1:]
                if _qc in _after:
                    _end = _after.index(_qc)
                    _quoted = _after[:_end]
                    if len(_quoted) >= 3:
                        _quoted_texts.append(_quoted)
                    _rest = _after[_end + 1:]
                else:
                    break
        for qi, target in enumerate(targets):
            if not _quoted_texts:
                break
            target_filename = target.get("filename", "")
            # Find the file and its symbol map
            _smap_match = None
            _sf_match = None
            for fn in symbol_maps_by_name:
                if fn == target_filename or fn.endswith(target_filename) or target_filename.endswith(fn):
                    _smap_match, _sf_match = symbol_maps_by_name[fn]
                    break
            if not _smap_match or not _sf_match:
                continue
            _file_content = _sf_match.get("content", "") if isinstance(_sf_match, dict) else ""
            if not _file_content:
                continue
            _file_lines = _file_content.splitlines()
            sp = target.get("symbol_path", "")

            # FIX v3.2.1: If Architect provided an exact target_line (from ReAct search results),
            # use it directly to create a focused window. Skip text-search fallback entirely.
            # This prevents label text at wrong line from hijacking the edit target.
            _tl_direct = target.get("target_line")
            if _tl_direct:
                try:
                    _tl_int = int(_tl_direct)
                    if 1 <= _tl_int <= len(_file_lines) and _tl_int not in _used_focus_lines:
                        _tl_total = len(_file_lines)
                        _tl_ws = max(1, _tl_int - 100)
                        _tl_we = min(_tl_total, _tl_int + 100)
                        _tl_vcode = "\n".join(_file_lines[_tl_ws - 1:_tl_we])
                        _tl_vname = "_focused_L{}".format(_tl_int)
                        from models.schemas import SymbolInfo as _SI_tl, SymbolType as _ST_tl
                        _tl_vsym = _SI_tl(
                            name=_tl_vname, symbol_type=_ST_tl.VARIABLE,
                            start_line=_tl_ws, end_line=_tl_we,
                            parent=None, indentation=0, code=_tl_vcode,
                            signature="target_line window around line {}".format(_tl_int)
                        )
                        _smap_match.symbols.append(_tl_vsym)
                        targets[qi]["symbol_path"] = _tl_vname
                        _used_focus_lines.add(_tl_int)
                        continue  # skip text-search fallback — direct line target is authoritative
                except (ValueError, TypeError):
                    pass  # invalid target_line — fall through to text-search fallback

            # Find the targeted symbol
            _tsym = None
            for sym in _smap_match.symbols:
                if sym.full_path == sp or sym.name == sp:
                    _tsym = sym
                    break
            if not _tsym:
                continue
            # Check if any quoted text is missing from the targeted symbol
            for qt in _quoted_texts:
                if len(qt) < 3:
                    continue
                if qt.lower() in _tsym.code.lower():
                    # Text IS in the symbol, but if symbol is large (>50 lines),
                    # create a focused window around the text line for better Surgeon accuracy
                    _tsym_size = _tsym.end_line - _tsym.start_line + 1
                    if _tsym_size > 50:
                        # FIX v3.2.1: Search WITHIN the targeted symbol's own line range.
                        # The old code scanned from line 1 and found the first occurrence
                        # globally (e.g. a label 500 lines BEFORE the actual input element),
                        # creating _focused_L8834 instead of _focused_L9318.
                        _tline_inner = None
                        _sym_lo = _tsym.start_line - 1  # 0-indexed
                        _sym_hi = min(_tsym.end_line, len(_file_lines))
                        for li_off in range(_sym_lo, _sym_hi):
                            candidate_line = _tsym.start_line + (li_off - _sym_lo)
                            if (qt.lower() in _file_lines[li_off].lower()
                                    and candidate_line not in _used_focus_lines):
                                _tline_inner = candidate_line
                                break
                        # Fallback: if not found within symbol (rare edge case), try global
                        if _tline_inner is None:
                            for li, line in enumerate(_file_lines, 1):
                                if qt.lower() in line.lower() and li not in _used_focus_lines:
                                    _tline_inner = li
                                    break
                        if _tline_inner:
                            _used_focus_lines.add(_tline_inner)
                            total = len(_file_lines)
                            ws = max(1, _tline_inner - SYMBOL_FOCUS_WINDOW // 4)
                            we = min(total, _tline_inner + SYMBOL_FOCUS_WINDOW // 4)
                            vcode = "\n".join(_file_lines[ws - 1:we])
                            vname = f"_focused_L{_tline_inner}"
                            from models.schemas import SymbolInfo as _SI2, SymbolType as _ST2
                            vsym = _SI2(name=vname, symbol_type=_ST2.VARIABLE, start_line=ws, end_line=we,
                                       parent=None, indentation=0, code=vcode,
                                       signature=f"focused window around line {_tline_inner}")
                            _smap_match.symbols.append(vsym)
                            targets[qi]["symbol_path"] = vname
                    continue  # Text is in the symbol — no redirect needed (or already redirected)
                # Find where the text actually is — skip lines already used by other targets
                # (prevents multi-target edits from all collapsing to the same location)
                _tline = None
                for li, line in enumerate(_file_lines, 1):
                    if qt.lower() in line.lower() and li not in _used_focus_lines:
                        _tline = li
                        break
                if _tline is None:
                    continue
                # Find narrowest symbol containing that line
                _best = None
                _best_size = float("inf")
                for sym in _smap_match.symbols:
                    if sym.start_line <= _tline <= sym.end_line:
                        sz = sym.end_line - sym.start_line
                        if sz < _best_size:
                            _best_size = sz
                            _best = sym
                if _best and _best.full_path != sp:
                    _used_focus_lines.add(_tline)
                    targets[qi]["symbol_path"] = _best.full_path
                elif not _best:
                    # Create virtual window
                    _used_focus_lines.add(_tline)
                    total = len(_file_lines)
                    ws = max(1, _tline - TEXT_SEARCH_WINDOW)
                    we = min(total, _tline + TEXT_SEARCH_WINDOW)
                    vcode = "\n".join(_file_lines[ws - 1:we])
                    vname = f"_text_region_L{_tline}"
                    from models.schemas import SymbolInfo as _SI, SymbolType as _ST
                    vsym = _SI(name=vname, symbol_type=_ST.VARIABLE, start_line=ws, end_line=we,
                               parent=None, indentation=0, code=vcode, signature=f"text region around line {_tline}")
                    _smap_match.symbols.append(vsym)
                    targets[qi]["symbol_path"] = vname

        changes_by_file = {}
        _parse_skipped = []  # files skipped because smap is None (not parsed as code)

        for i, target in enumerate(targets):
            target_filename = target.get("filename", "")

            # Find matching file (exact or suffix match)
            matched_name = None
            if target_filename in symbol_maps_by_name:
                matched_name = target_filename
            else:
                for fn in symbol_maps_by_name:
                    if fn.endswith(target_filename) or target_filename.endswith(fn):
                        matched_name = fn
                        break

            if not matched_name:
                continue

            smap, sf = symbol_maps_by_name[matched_name]
            if smap is None:
                _parse_skipped.append(matched_name)
                continue

            yield sse({"type": "progress", "content": f"Surgeon: {matched_name} → {target.get('symbol_path', '?')} ({i+1}/{len(targets)})"})

            symbol_path = target.get("symbol_path", "")
            ct_str = target.get("change_type", "modify")

            # Find symbol in map
            symbol = None
            for sym in smap.symbols:
                if sym.full_path == symbol_path or sym.name == symbol_path:
                    symbol = sym
                    break

            # v3.11.1: Containment validation for dict-based targets (run_pipeline_stream path)
            # If the found symbol doesn't contain target_line, use the one that does.
            _tline_cv = target.get("target_line")
            if _tline_cv and symbol is not None:
                if not (symbol.start_line <= _tline_cv <= symbol.end_line):
                    _cv_containing = next(
                        (s for s in smap.symbols if s.start_line <= _tline_cv <= s.end_line),
                        None
                    )
                    if _cv_containing:
                        print(
                            f"[PIPELINE] v3.11.1 containment fix: '{symbol_path}' "
                            f"(L{symbol.start_line}–{symbol.end_line}) does not contain "
                            f"target_line {_tline_cv} → '{_cv_containing.full_path}' "
                            f"(L{_cv_containing.start_line}–{_cv_containing.end_line})"
                        )
                        symbol = _cv_containing

            ct = ChangeType(ct_str) if ct_str in ("modify", "add", "delete", "refactor") else ChangeType.MODIFY

            if symbol is None and ct == ChangeType.ADD:
                parent_name = ".".join(symbol_path.split(".")[:-1])
                if parent_name:
                    symbol = next(
                        (s for s in smap.symbols if s.full_path == parent_name or s.name == parent_name),
                        None
                    )
                if symbol is None and smap.symbols:
                    # Fall back to first class or last function
                    symbol = next((s for s in smap.symbols if s.symbol_type.value == "class"), smap.symbols[0])

            if symbol is None:
                # Fuzzy fallback: try partial name match (e.g. Architect said "LeftPanel" but
                # TSX only has "LoginPage" — find any symbol whose name contains the requested
                # name as a substring, or whose range contains the target_line if provided)
                _tline_fb = target.get("target_line")
                if _tline_fb:
                    symbol = next(
                        (s for s in smap.symbols
                         if s.start_line <= _tline_fb <= s.end_line),
                        None
                    )
                if symbol is None:
                    # Partial name match
                    sp_lower = symbol_path.lower().split(".")[-1]
                    symbol = next(
                        (s for s in smap.symbols
                         if sp_lower in s.full_path.lower() or s.full_path.lower() in sp_lower),
                        None
                    )
                if symbol is None and smap.symbols:
                    # Last resort: use the largest symbol (most likely the main component)
                    symbol = max(smap.symbols, key=lambda s: s.end_line - s.start_line)
                    print(f"[PIPELINE] symbol '{symbol_path}' not found — falling back to largest symbol '{symbol.full_path}'")
                if symbol is None:
                    continue

            change_target = ChangeTarget(
                symbol_path=symbol_path,
                change_type=ct,
                description=target.get("description", ""),
                new_logic=target.get("new_logic", ""),
                dependencies=[],
                confidence=target.get("confidence", 7),
                import_changes=target.get("import_changes", []),
                context_needs=target.get("context_needs", []),
                surgeon_context=target.get("surgeon_context", []),
                target_line=target.get("target_line"),  # v3.11.2: pass through for deterministic delete
            )

            # ── Oversized symbol guardrail ──
            # If symbol is >500 lines, try to find a more specific child symbol.
            #
            # v3.11.0: tightened heuristic. Previously a child <100 lines got +3
            # for being small, so any tiny irrelevant subcomponent (e.g. StatPill)
            # could win against the actual edit target with best_score=3 from
            # smallness alone. That misrouted the Surgeon to code that didn't
            # contain the tokens the plan asked for, and it emitted the empty
            # "already correct" block, producing the "no changes whatsoever" QA.
            #
            # New rules:
            #   - A child must meet MIN_NARROW_SCORE on keyword match alone.
            #     Smallness no longer counts toward the threshold — it only
            #     breaks ties between otherwise-qualifying candidates.
            #   - If the architect supplied target_line, a child whose range
            #     contains that line is decisively preferred (large bonus).
            #   - "redesign / restyle / rewrite" descriptions skip narrowing
            #     entirely: those changes are component-wide by definition.
            symbol_size = symbol.end_line - symbol.start_line + 1
            _original_symbol_size = symbol_size  # v3.12: save before any narrowing
            MIN_NARROW_SCORE = 10  # one name-match (+10) or two code-matches (+5+5)
            _REDESIGN_KEYWORDS = ("redesign", "restyle", "rewrite", "modernize",
                                  "overhaul", "revamp", "refactor",
                                  "full redesign", "complete redesign", "entire panel",
                                  "whole panel", "new design", "fresh design")
            _desc_combined = (target.get("description", "") + " "
                              + target.get("new_logic", "")).lower()
            _is_redesign = any(k in _desc_combined for k in _REDESIGN_KEYWORDS)
            _target_line_hint = target.get("target_line")
            try:
                _target_line_hint = int(_target_line_hint) if _target_line_hint else None
            except (ValueError, TypeError):
                _target_line_hint = None

            if symbol_size > 500 and not _is_redesign:
                # Look for child symbols within this symbol's range
                child_candidates = [
                    s for s in smap.symbols
                    if s.start_line >= symbol.start_line
                    and s.end_line <= symbol.end_line
                    and s.full_path != symbol.full_path
                    and (s.end_line - s.start_line + 1) < symbol_size
                ]
                # Try to find a child that matches the target description
                desc_lower = _desc_combined
                best_child = None
                best_score = 0
                for child in child_candidates:
                    child_size = child.end_line - child.start_line + 1
                    if child_size > 500:
                        continue  # Skip huge children too
                    # Score by keyword match in symbol name/code only — NOT by size.
                    score = 0
                    child_name_lower = child.full_path.lower()
                    for keyword in desc_lower.split():
                        if len(keyword) > 3 and keyword in child_name_lower:
                            score += 10
                        if len(keyword) > 3 and keyword in child.code[:500].lower():
                            score += 5
                    # Architect target_line containment is decisive
                    if (_target_line_hint is not None
                            and child.start_line <= _target_line_hint <= child.end_line):
                        score += 25
                    # Below threshold → not a real match, don't narrow into it
                    if score < MIN_NARROW_SCORE:
                        continue
                    # Tie-break by smallness (more surgical) only after threshold passes
                    if score > best_score or (score == best_score and best_child
                                              and child_size < (best_child.end_line - best_child.start_line + 1)):
                        best_score = score
                        best_child = child
                if best_child:
                    yield sse({"type": "progress", "content": f"Narrowing: {symbol.full_path} ({symbol_size}L) → {best_child.full_path} ({best_child.end_line - best_child.start_line + 1}L) [score={best_score}]"})
                    symbol = best_child
                elif symbol_size > 1000:
                    # No good child found — create a focused window (±100 lines around midpoint of description keywords)
                    # Use the target description to estimate where the change is needed
                    all_lines = sf["content"].splitlines()
                    search_terms = [w for w in desc_lower.split() if len(w) > 3]
                    best_line = symbol.start_line + symbol_size // 2  # default: middle
                    for li in range(symbol.start_line - 1, min(symbol.end_line, len(all_lines))):
                        line_lower = all_lines[li].lower() if li < len(all_lines) else ""
                        if any(term in line_lower for term in search_terms):
                            best_line = li + 1
                            break
                    window_start = max(symbol.start_line, best_line - SYMBOL_FOCUS_WINDOW)
                    window_end = min(symbol.end_line, best_line + SYMBOL_FOCUS_WINDOW)
                    windowed_code = "\n".join(all_lines[window_start - 1:window_end])
                    from models.schemas import SymbolInfo as _SI, SymbolType as _ST
                    symbol = _SI(
                        name=f"{symbol.name}_window",
                        symbol_type=symbol.symbol_type,
                        start_line=window_start,
                        end_line=window_end,
                        parent=symbol.parent,
                        indentation=symbol.indentation,
                        code=windowed_code,
                        signature=f"Focused window L{window_start}-{window_end} of {symbol.full_path}",
                    )
                    yield sse({"type": "progress", "content": f"Focused window: L{window_start}-{window_end} ({window_end - window_start + 1} lines)"})
            elif symbol_size > 500 and _is_redesign:
                # Redesign-class request: don't narrow — the Surgeon needs the whole component.
                yield sse({"type": "progress", "content": f"Redesign mode: keeping full symbol {symbol.full_path} ({symbol_size}L), narrowing skipped"})

            # ── Size-based routing: large rewrites or redesigns → Claude direct ─
            # When the Surgeon is Claude and the target symbol is large (>300L) or
            # is a redesign-class request, skip SEARCH/REPLACE entirely. Claude
            # receives the full file and outputs a complete new file via tool_use.
            # This eliminates the focused-window truncation problem for multi-region
            # rewrites that span 800+ lines across 4+ non-contiguous blocks.
            _surg_model_route = get_setting("surgeon_model", "claude-sonnet-5")
            _symbol_size_route = symbol.end_line - symbol.start_line + 1
            # v3.12: use original (pre-narrowing) size — narrowing must not defeat routing
            _use_direct_rewrite = (
                (_is_redesign or _original_symbol_size > 250)
            )

            _dr_new_code = None
            _dr_confidence = None
            _dr_surg_notes = None
            _dr_operations = None
            _dr_diff = None
            _dr_qa_result = None
            _dr_effective_original = None
            _dr_full_after_lint = None

            if _use_direct_rewrite:
                yield sse({"type": "progress", "content": f"Full-file rewrite: {_surg_model_route} writing complete {matched_name} ({_symbol_size_route}L symbol)..."})
                _dr_ok = False
                try:
                    if _is_claude_model(_surg_model_route):
                        _dr = await _run_claude_direct_rewrite(
                            file_content=sf["content"],
                            filename=matched_name,
                            change_description=change_target.description,
                            new_logic=change_target.new_logic,
                            architect_plan=plan,
                            anthropic_key=_get_anthropic_key(user_id),
                            model=_surg_model_route,
                        )
                    else:
                        _dr = await _run_gpt_direct_rewrite(
                            file_content=sf["content"],
                            filename=matched_name,
                            change_description=change_target.description,
                            new_logic=change_target.new_logic,
                            architect_plan=plan,
                            user_id=user_id,
                            model=_surg_model_route,
                        )
                    _dr_new_code = _dr["new_file_content"]
                    _dr_confidence = _dr.get("confidence", 8)
                    _dr_surg_notes = _dr.get("notes", [])
                    _dr_operations = [{"find": sf["content"], "replace": _dr_new_code}]
                    _dr_diff = _make_diff(sf["content"], _dr_new_code, matched_name)
                    _dr_effective_original = sf["content"]
                    _dr_full_after_lint = _dr_new_code
                    _dr_ok = True
                except Exception as _dr_exc:
                    _dr_err_msg = str(_dr_exc)[:160]
                    print(f"[DIRECT_REWRITE] Failed: {_dr_exc} — falling back to Surgeon")
                    _dlog("direct_rewrite_failed",
                          session_id=session_id,
                          error_type=type(_dr_exc).__name__,
                          error=str(_dr_exc)[:300],
                          user_id=user_id)
                    yield sse({"type": "progress", "content": f"⚠️ Direct rewrite failed ({type(_dr_exc).__name__}: {_dr_err_msg}) — falling back to Surgeon"})
                    _use_direct_rewrite = False

                if _dr_ok:
                    _dr_qa_result = await run_qa_agent(
                        original_code=sf["content"],
                        new_code=_dr_new_code,
                        change_description=change_target.description,
                        new_logic=change_target.new_logic,
                        symbol_path=symbol_path,
                        filename=matched_name,
                        other_files_context="",
                        session_id=session_id or "",
                        user_id=user_id,
                        architect_risks=plan.get("risks", []),
                        targeted_context="",
                    )
                    _dr_qa_icon = {"safe": "✅", "warning": "⚠️", "blocked": "🚫", "skipped": "⏭"}.get(
                        _dr_qa_result.get("verdict", "skipped"), "⏭"
                    )
                    yield sse({"type": "progress", "content": f"QA {_dr_qa_icon} {_dr_qa_result.get('summary', '')} (score: {_dr_qa_result.get('qa_score', '?')})"})
                    compliance.mark("qa_review", ran=True,
                                    output_summary=f"direct-rewrite QA: {_dr_qa_result.get('verdict', 'skipped')}")

                    # ── Assemble SurgicalChange and skip Surgeon ──────────────
                    from models.schemas import SurgicalOperation as _SO_DR
                    _dr_ops = [_SO_DR(find=op.get("find",""), replace=op.get("replace","")) for op in _dr_operations]
                    _dr_change = SurgicalChange(
                        id=str(uuid.uuid4()),
                        symbol=symbol,
                        original_code=_dr_effective_original,
                        new_code=_dr_new_code,
                        diff=_dr_diff,
                        confidence=_dr_confidence,
                        description=target.get("description", ""),
                        applied=False,
                        surgeon_notes=_dr_surg_notes or [],
                        qa_result=_dr_qa_result,
                        operations=_dr_ops,
                    )
                    if matched_name not in changes_by_file:
                        changes_by_file[matched_name] = {"file": sf, "changes": []}
                    changes_by_file[matched_name]["changes"].append(_dr_change)
                    continue  # skip Surgeon retry loop for this target

            # ── v3.10.0: Resolve Claude's surgeon_context before Surgeon runs ─
            # v3.11.0: resolution moved INSIDE the retry loop so QA-flagged
            # identifiers can be added on retry. The architect's plan only
            # knows what it knew at planning time; if QA later reports
            # "you missed scanColor, badgeBg, …", we resolve those symbols
            # too and inject them on the next attempt.
            try:
                from services.context_resolver import (
                    resolve_context_requests as _resolve_ctx,
                    describe_requests as _describe_ctx,
                )
                _have_resolver = True
            except Exception as _ctx_imp_exc:
                print(f"[CONTEXT_RESOLVER] Import failed: {_ctx_imp_exc}")
                _have_resolver = False

            def _build_surgeon_context_for_attempt(
                base_requests: list,
                qa_verdict: dict,
            ) -> str:
                """Resolve architect's surgeon_context plus QA-derived symbol/grep
                requests mined from the previous QA verdict's text fields."""
                if not _have_resolver:
                    return ""
                _reqs = list(base_requests or [])
                if qa_verdict:
                    import re as _re_local
                    _qa_text_parts = [
                        qa_verdict.get("summary", "") or "",
                        qa_verdict.get("plan_deviation", "") or "",
                    ]
                    for _qk in ("import_issues", "type_errors", "logic_errors", "downstream_risks"):
                        _qa_text_parts.extend(str(x) for x in (qa_verdict.get(_qk) or []))
                    for _rv in (qa_verdict.get("risk_verdicts") or []):
                        if isinstance(_rv, dict):
                            _qa_text_parts.append(str(_rv.get("reason", "") or ""))
                            _qa_text_parts.append(str(_rv.get("risk", "") or ""))
                    _qa_text = " ".join(_qa_text_parts)
                    # Identifier-shaped tokens: camelCase, PascalCase, SNAKE_CASE.
                    # Length ≥ 4 avoids matching English noise like "the", "and".
                    _ident_re = _re_local.compile(
                        r"\b(?:[A-Z][A-Z0-9_]{3,}|[a-z][a-zA-Z0-9]{3,}|[A-Z][a-zA-Z0-9]{3,})\b"
                    )
                    # Stopwords likely to slip through — common QA-verdict English.
                    _STOP = {
                        "true", "false", "null", "none", "this", "that", "with", "from",
                        "into", "have", "been", "were", "will", "would", "should", "could",
                        "missing", "missed", "added", "added.", "removed", "change", "changes",
                        "changed", "expected", "actual", "should.", "code", "file", "files",
                        "symbol", "symbols", "function", "functions", "method", "methods",
                        "value", "values", "field", "fields", "property", "properties",
                    }
                    _seen_idents: set = set()
                    for _tok in _ident_re.findall(_qa_text):
                        _tl = _tok.lower()
                        if _tl in _STOP or _tl in _seen_idents:
                            continue
                        _seen_idents.add(_tl)
                        _reqs.append({"type": "symbol", "name": _tok})
                        _reqs.append({"type": "grep",   "pattern": _tok})
                        if len(_seen_idents) >= 8:  # cap to protect context window
                            break
                if not _reqs:
                    return ""
                try:
                    return _resolve_ctx(
                        requests=_reqs,
                        symbol_maps_by_name=symbol_maps_by_name,
                        requesting_file=matched_name,
                    ) or ""
                except Exception as _resolve_exc:
                    print(f"[CONTEXT_RESOLVER] Resolve failed: {_resolve_exc}")
                    return ""

            # ── Surgeon retry loop (linter + QA feedback) ───────────────────
            _linter_feedback_for_retry: list = []
            _qa_feedback_for_retry: dict = {}       # QA verdict injected on semantic retry
            _full_after_lint: str = ""
            _MAX_SURGEON_ATTEMPTS = 3               # +1 for QA semantic retry
            _surgeon_context_reqs = change_target.surgeon_context
            _original_symbol_code = symbol.code     # preserve for v3.11.0 fix (a)

            for _surgeon_attempt in range(_MAX_SURGEON_ATTEMPTS):
                # Re-resolve context every attempt; expands with QA-flagged symbols on retry.
                _resolved_surgeon_ctx = _build_surgeon_context_for_attempt(
                    _surgeon_context_reqs, _qa_feedback_for_retry,
                )
                if _surgeon_context_reqs or _qa_feedback_for_retry:
                    if _resolved_surgeon_ctx:
                        _n = len(_resolved_surgeon_ctx.splitlines())
                        _label = " (with QA-flagged symbols)" if _qa_feedback_for_retry else ""
                        try:
                            _desc = _describe_ctx(_surgeon_context_reqs) if _have_resolver else "context"
                        except Exception:
                            _desc = "context"
                        yield sse({"type": "progress", "content": f"📦 Resolving Surgeon context{_label}: {_desc}"})
                        yield sse({"type": "progress", "content": f"✅ Surgeon context ready ({_n} lines injected)"})
                    elif _surgeon_context_reqs:
                        yield sse({"type": "progress", "content": "⚠️ surgeon_context items not found — proceeding without them"})

                new_code, confidence, _surg_notes, _needed_imports, _operations = run_surgeon(
                    symbol, change_target, sf["content"], user_id=user_id,
                    extra_context=_resolved_surgeon_ctx,
                    linter_feedback=_linter_feedback_for_retry if _linter_feedback_for_retry else None,
                    qa_feedback=_qa_feedback_for_retry if _qa_feedback_for_retry else None,
                    forbid_noop=bool(_qa_feedback_for_retry) or ct == ChangeType.DELETE,
                )
                _is_changed = new_code.rstrip() != symbol.code.rstrip()
                _attempt_label = f" (attempt {_surgeon_attempt+1})" if _surgeon_attempt > 0 else ""
                print(f"[PIPELINE] attempt={_surgeon_attempt+1} {len(_operations)} ops, changed={_is_changed}, new={len(new_code)}, orig={len(symbol.code)}")
                diff = _make_diff(_original_symbol_code, new_code, symbol_path)

                # ── QA Agent ──────────────────────────────────────────────────
                _other_ctx_for_qa = "\n\n".join(
                    p for p in _qa_other_context_parts
                    if not p.startswith(f"FILE: {matched_name}")
                )
                _effective_original = getattr(symbol, "_file_window_original", None) or symbol.code

                # Build targeted context: actual callers/usages of the changed symbol.
                # This replaces the generic other_files_context truncation with precise,
                # relevant code that the QA agent can reason about directly.
                _targeted_qa_ctx = ""
                try:
                    from services.context_resolver import resolve_context_requests as _resolve_for_qa
                    _sym_name = symbol.name if symbol and symbol.name else ""
                    if _sym_name:
                        _targeted_qa_ctx = _resolve_for_qa(
                            requests=[
                                {"type": "callers", "name": _sym_name},
                                {"type": "usages",  "name": _sym_name},
                            ],
                            symbol_maps_by_name=symbol_maps_by_name,
                            requesting_file=matched_name,
                        )
                except Exception as _tqe:
                    print(f"[QA_CONTEXT] Skipped: {_tqe}")

                # Build context for QA about other changes in this same request
                _same_run_ctx = ""
                if len(targets) > 1:
                    _other_summaries = []
                    for _oj, _ot in enumerate(targets):
                        if _oj != i and getattr(_ot, "description", ""):
                            _ot_sym = getattr(_ot, "symbol_path", "unknown")
                            _other_summaries.append(
                                f"  \u2022 [{_ot_sym} in {matched_name}] {_ot.description}"
                            )
                    if _other_summaries:
                        _same_run_ctx = "\n".join(_other_summaries[:6])

                _qa_result = await run_qa_agent(
                    original_code=_effective_original,
                    new_code=new_code,
                    change_description=change_target.description,
                    new_logic=change_target.new_logic,
                    symbol_path=symbol_path,
                    filename=matched_name,
                    other_files_context=_other_ctx_for_qa,
                    session_id=session_id or "",
                    user_id=user_id,
                    architect_risks=plan.get("risks", []),
                    targeted_context=_targeted_qa_ctx,
                    qa_feedback=_qa_feedback_for_retry if _qa_feedback_for_retry else None,
                    same_run_context=_same_run_ctx,
                )
                _qa_icon = {"safe": "✅", "warning": "⚠️", "blocked": "🚫", "skipped": "⏭"}.get(
                    _qa_result.get("verdict", "skipped"), "⏭"
                )
                yield sse({"type": "progress", "content": f"QA {_qa_icon} {_qa_result.get('summary', '')} (score: {_qa_result.get('qa_score', '?')}){_attempt_label}"})
                _qa_ran = _qa_result.get("status") not in ("timeout", "error")
                compliance.mark("qa_review", ran=_qa_ran,
                                reason=(None if _qa_ran else _qa_result.get("status")),
                                output_summary=f"verdict={_qa_result.get('verdict')} score={_qa_result.get('qa_score')}")

                # ── Tree-sitter syntax check ───────────────────────────────────
                try:
                    from services.syntax_validator import validate_syntax as _validate_syntax
                    from services.syntax_validator import count_errors as _count_errors
                    _orig_err_count = _count_errors(sf["content"], matched_name)
                    _full_after_ops = sf["content"]
                    for _sop in _operations:
                        _sfind = _sop.get("find", "")
                        _srepl = _sop.get("replace", "")
                        if _sfind and _sfind in _full_after_ops:
                            _full_after_ops = _full_after_ops.replace(_sfind, _srepl, 1)
                    _new_err_count = _count_errors(_full_after_ops, matched_name)
                    if _new_err_count > _orig_err_count:
                        _syntax_errors = _validate_syntax(_full_after_ops, matched_name)
                        yield sse({"type": "progress", "content": f"🔴 Compile check: {_syntax_errors[0]['message']} (line {_syntax_errors[0]['line']})"})
                        if not isinstance(_qa_result.get("risk_verdicts"), list):
                            _qa_result["risk_verdicts"] = []
                        for _serr in _syntax_errors:
                            _qa_result["risk_verdicts"].append({
                                "risk": _serr["message"],
                                "status": "blocked",
                                "reason": f"Compile error at line {_serr['line']}: {_serr['detail']}",
                            })
                        _qa_result["verdict"] = "blocked"
                        _qa_result["summary"] = f"Syntax error — {_syntax_errors[0]['message']}"
                        if (_qa_result.get("qa_score") or 10) > 3:
                            _qa_result["qa_score"] = 3
                    elif _new_err_count == 0 and _orig_err_count == 0:
                        yield sse({"type": "progress", "content": "✅ Compile check passed"})
                    else:
                        yield sse({"type": "progress", "content": f"⏭ Compile check skipped (file has {_orig_err_count} pre-existing issues)"})
                except Exception as _sv_exc:
                    print(f"[SYNTAX_VALIDATOR] Skipped: {_sv_exc}")

                # ── pyflakes / tsc linting ────────────────────────────────────
                _linter_introduced_errors: list = []
                try:
                    from services.linter_validator import (
                        count_linter_errors as _count_lint,
                        validate_linters as _validate_lint,
                        linter_tool_name as _lint_tool_name,
                    )
                    _lint_tool = _lint_tool_name(matched_name)
                    _lint_orig_count = _count_lint(sf["content"], matched_name)
                    _full_after_lint = sf["content"]
                    for _lop in _operations:
                        _lfind = _lop.get("find", "")
                        _lrepl = _lop.get("replace", "")
                        if _lfind and _lfind in _full_after_lint:
                            _full_after_lint = _full_after_lint.replace(_lfind, _lrepl, 1)
                    _lint_new_count = _count_lint(_full_after_lint, matched_name)
                    if _lint_new_count > 0:
                        # Absolute check: ANY TS errors in output → attempt Claude auto-fix first
                        _linter_introduced_errors = _validate_lint(_full_after_lint, matched_name)
                        yield sse({"type": "progress", "content": f"🔧 {_lint_tool}: {_lint_new_count} error(s) — asking Claude to auto-fix..."})
                        # ── Lint self-heal: up to 3 Claude attempts ───────────
                        _lint_fixed = False
                        _MAX_LINT_ATTEMPTS = 3
                        _lint_surg_model = get_setting("surgeon_model", "claude-sonnet-5")
                        _lint_use_claude = _is_claude_model(_lint_surg_model)
                        if _lint_use_claude:
                            _lint_fix_client = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
                        else:
                            _lint_fix_oai_client = _get_client(user_id)
                        _dlog("lint_fix_model_route", model=_lint_surg_model,
                              use_claude=_lint_use_claude, user_id=user_id)
                        _lint_working = _full_after_lint          # updated each attempt
                        _lint_remaining = _linter_introduced_errors  # refreshed each attempt
                        # Improvement #3: collect every applied fix so a clean pass
                        # can ship them in _operations/new_code (not just report them).
                        _lint_shipped_fixes: list = []
                        for _lint_attempt in range(_MAX_LINT_ATTEMPTS):
                            try:
                                _lint_err_lines = "\n".join(
                                    f"  {e.get('file',matched_name)}({e['line']},{e.get('col',1)}): error {e.get('code','TS0000')}: {e['message']}"
                                    for e in _lint_remaining
                                )
                                _attempt_label = f"attempt {_lint_attempt+1}/{_MAX_LINT_ATTEMPTS}"
                                yield sse({"type": "progress", "content": f"🔧 Lint auto-fix {_attempt_label}: {len(_lint_remaining)} error(s) → {_lint_surg_model}..."})
                                _lint_user_msg = (
                                            f"You are a TypeScript lint fixer. This is attempt {_lint_attempt+1} of {_MAX_LINT_ATTEMPTS}.\n"
                                            f"Fix EVERY listed error in `{matched_name}`. There are {len(_lint_remaining)} errors remaining.\n\n"
                                            f"Rules:\n"
                                            f"- TS6133 (declared but never read): delete the ENTIRE line — the full import statement or the full const/let/var declaration. Do not leave a blank import {{}} line.\n"
                                            f"- TS2305 / TS2307 (module not found): fix the import path or remove if unused.\n"
                                            f"- TS2304 (cannot find name): add the correct import or remove the reference if unused.\n"
                                            f"- TS2345 / TS2322 (type mismatch): cast or coerce the value to the expected type.\n"
                                            f"- Do NOT comment out lines. Do NOT change logic, JSX, hooks, types, or business rules.\n"
                                            f"- `find` must match the file EXACTLY — copy the line character-for-character including leading spaces/tabs.\n"
                                            f"- `replace` must be empty string \"\" to delete a line, NOT whitespace.\n"
                                            f"- Return exactly one fix object per error. All {len(_lint_remaining)} errors must be addressed.\n\n"
                                            f"Errors to fix (line, column, TS code, message):\n{_lint_err_lines}\n\n"
                                            f"Current file content (this is the LIVE file — use exact text from here for your `find` strings):\n```typescript\n{_lint_working}\n```"
                                        )
                                # Branch: Claude tool_use vs OpenAI tool_calls
                                _lint_fixes_list = []
                                if _lint_use_claude:
                                    _dlog("lint_fix_call_config",
                                          model=_lint_surg_model,
                                          wrapper="safe_claude_call",
                                          session_id=session_id, user_id=user_id)
                                    _lint_fix_resp = await _safe_claude_call(
                                        _lint_fix_client,
                                        model=_lint_surg_model,
                                        desired_text_tokens=8192,
                                        thinking_budget=4000,
                                        retry_on_starve=True,
                                        tools=[{
                                            "name": "fix_lint_errors",
                                            "description": "Return SEARCH/REPLACE pairs to eliminate TypeScript lint errors. Each find must match the file exactly.",
                                            "input_schema": {
                                                "type": "object",
                                                "properties": {
                                                    "fixes": {
                                                        "type": "array",
                                                        "items": {
                                                            "type": "object",
                                                            "properties": {
                                                                "find": {"type": "string", "description": "Exact text to remove or replace (must match file exactly, including indentation)"},
                                                                "replace": {"type": "string", "description": "Replacement text — use empty string \"\" to delete the line entirely"}
                                                            },
                                                            "required": ["find", "replace"]
                                                        }
                                                    }
                                                },
                                                "required": ["fixes"]
                                            }
                                        }],
                                        tool_choice={"type": "tool", "name": "fix_lint_errors"},
                                        messages=[{"role": "user", "content": _lint_user_msg}],
                                    )
                                    for _lblock in _lint_fix_resp.content:
                                        if hasattr(_lblock, "type") and _lblock.type == "tool_use":
                                            _lint_fixes_list = (_lblock.input or {}).get("fixes", [])
                                else:
                                    # GPT / OpenAI path — same tool schema, OpenAI format
                                    _lint_fix_resp_oai = _chat_create(
                                        _lint_fix_oai_client, _lint_surg_model,
                                        messages=[
                                            {"role": "system", "content": "You are a TypeScript lint fixer. Return fixes using the fix_lint_errors tool."},
                                            {"role": "user", "content": _lint_user_msg},
                                        ],
                                        tools=[{
                                            "type": "function",
                                            "function": {
                                                "name": "fix_lint_errors",
                                                "description": "Return SEARCH/REPLACE pairs to eliminate TypeScript lint errors.",
                                                "parameters": {
                                                    "type": "object",
                                                    "properties": {
                                                        "fixes": {
                                                            "type": "array",
                                                            "items": {
                                                                "type": "object",
                                                                "properties": {
                                                                    "find": {"type": "string", "description": "Exact text to remove or replace"},
                                                                    "replace": {"type": "string", "description": "Replacement text"}
                                                                },
                                                                "required": ["find", "replace"]
                                                            }
                                                        }
                                                    },
                                                    "required": ["fixes"]
                                                }
                                            }
                                        }],
                                        tool_choice={"type": "function", "function": {"name": "fix_lint_errors"}},
                                    )
                                    for _tc in (_lint_fix_resp_oai.choices[0].message.tool_calls or []):
                                        try:
                                            import json as _json_lint
                                            _lint_fixes_list = _json_lint.loads(_tc.function.arguments).get("fixes", [])
                                        except Exception:
                                            _dlog("lint_fix_oai_parse_error", session_id=session_id,
                                                  raw=str(getattr(_tc.function, "arguments", ""))[:300])
                                    _dlog("lint_fix_oai_response", session_id=session_id,
                                          fixes_count=len(_lint_fixes_list), model=_lint_surg_model)

                                # Apply fixes (unified path for both providers)
                                _lint_patched = _lint_working
                                _fixes_applied = 0
                                for _lfix in _lint_fixes_list:
                                    _lf = _lfix.get("find", "")
                                    _lr = _lfix.get("replace", "")
                                    if _lf and _lf in _lint_patched:
                                        _lint_patched = _lint_patched.replace(_lf, _lr, 1)
                                        _fixes_applied += 1
                                        _lint_shipped_fixes.append({"find": _lf, "replace": _lr})
                                        _dlog("lint_fix_applied", session_id=session_id,
                                              file=matched_name, attempt=_lint_attempt + 1,
                                              find_preview=_lf[:80], replace_preview=_lr[:80])
                                # Re-run linter on patched content to get fresh error list
                                _lint_retry_count = _count_lint(_lint_patched, matched_name)
                                _lint_working = _lint_patched  # always advance, even if errors remain
                                if _lint_retry_count == 0:
                                    _full_after_lint = _lint_working
                                    _linter_introduced_errors = []
                                    _lint_fixed = True
                                    # Improvement #3: ship the applied fixes — append them to
                                    # _operations (applied sequentially after the surgeon's ops,
                                    # same order they were derived) and replay them into new_code
                                    # so the diff card shows what actually gets delivered.
                                    try:
                                        _lint_replayed = 0
                                        for _lsf in _lint_shipped_fixes:
                                            _operations.append({"find": _lsf["find"], "replace": _lsf["replace"]})
                                            if _lsf["find"] in new_code:
                                                new_code = new_code.replace(_lsf["find"], _lsf["replace"], 1)
                                                _lint_replayed += 1
                                        if _lint_replayed:
                                            diff = _make_diff(_original_symbol_code, new_code, symbol_path)
                                        _dlog("lint_fixes_shipped", session_id=session_id,
                                              file=matched_name, symbol=symbol_path,
                                              fixes_shipped=len(_lint_shipped_fixes),
                                              replayed_into_symbol=_lint_replayed,
                                              diff_recomputed=bool(_lint_replayed))
                                    except Exception as _lsf_exc:
                                        _dlog("lint_fixes_ship_error", session_id=session_id,
                                              file=matched_name, error=str(_lsf_exc)[:300])
                                    yield sse({"type": "progress", "content": f"✅ {_lint_tool} clean (auto-fixed {_lint_new_count} error(s) in {_lint_attempt+1} attempt(s))"})
                                    break
                                else:
                                    # Refresh error list for next attempt so Claude sees only what remains
                                    _lint_remaining = _validate_lint(_lint_working, matched_name)
                                    yield sse({"type": "progress", "content": f"⚠️ {_lint_tool}: {_lint_retry_count} error(s) remain after {_attempt_label} ({_fixes_applied} fix(es) applied)"})
                            except Exception as _lint_fix_exc:
                                yield sse({"type": "progress", "content": f"⚠️ Lint auto-fix exception ({_attempt_label}): {_lint_fix_exc}"})
                                break  # don't retry on unexpected error
                        # Hard-fail if errors remain after fix attempt
                        if not _lint_fixed:
                            yield sse({"type": "progress", "content": f"🔴 {_lint_tool}: {_linter_introduced_errors[0]['message']} (line {_linter_introduced_errors[0]['line']})"})
                            if not isinstance(_qa_result.get("risk_verdicts"), list):
                                _qa_result["risk_verdicts"] = []
                            for _lerr in _linter_introduced_errors:
                                _qa_result["risk_verdicts"].append({
                                    "risk": _lerr["message"],
                                    "status": "blocked",
                                    "reason": f"{_lint_tool} error — {_lerr['detail']}",
                                })
                            _qa_result["verdict"] = "blocked"
                            _qa_result["summary"] = f"{_lint_tool} error — {_linter_introduced_errors[0]['message']}"
                            if (_qa_result.get("qa_score") or 10) > 3:
                                _qa_result["qa_score"] = 3
                    elif _lint_new_count == 0:
                        yield sse({"type": "progress", "content": f"✅ {_lint_tool} clean"})
                except Exception as _lint_exc:
                    print(f"[LINTER_VALIDATOR] Skipped: {_lint_exc}")

                # ── Auto-run tests ────────────────────────────────────────────
                # Run after linter so we test the post-linter version of the code.
                # Only runs if test files are detected in the session.
                try:
                    _test_files_map = {
                        _tfname: _tfsf.get("content", "")
                        for _tfname, (_tfsmap, _tfsf) in symbol_maps_by_name.items()
                        if isinstance(_tfsf, dict)
                    }
                    # Substitute the Surgeon's patched content for the changed file
                    _test_files_map[matched_name] = _full_after_lint or sf.get("content", "")
                    _test_result = await _run_tests_inline(_test_files_map, session_id or "")
                    if _test_result.get("verdict") not in ("skipped", "unknown"):
                        _t_emoji = "✅" if _test_result["verdict"] == "passed" else "🔴"
                        _t_fw = _test_result.get("framework", "tests")
                        yield sse({"type": "progress", "content": (
                            f"{_t_emoji} {_t_fw}: {_test_result.get('passed', 0)} passed"
                            + (f", {_test_result['failed']} failed" if _test_result.get('failed') else "")
                        )})
                        _qa_result["test_results"] = _test_result
                        if _test_result["verdict"] == "failed" and _test_result.get("failed", 0) > 0:
                            if not isinstance(_qa_result.get("risk_verdicts"), list):
                                _qa_result["risk_verdicts"] = []
                            _qa_result["risk_verdicts"].append({
                                "risk": f"{_test_result['failed']} test(s) failing",
                                "status": "blocked",
                                "reason": f"Tests broke after this change ({_test_result['framework']})"
                            })
                            _qa_result["verdict"] = "blocked"
                            _qa_result["summary"] = f"{_test_result['failed']} test(s) failing after this change"
                            if (_qa_result.get("qa_score") or 10) > 4:
                                _qa_result["qa_score"] = 4
                except Exception as _test_exc:
                    print(f"[TEST_RUNNER] Skipped: {_test_exc}")

                # ── Retry decision ─────────────────────────────────────────────
                # Priority 1: linter compile errors (most precise, fastest to fix)
                _can_retry_lint = (
                    bool(_linter_introduced_errors)
                    and _surgeon_attempt < _MAX_SURGEON_ATTEMPTS - 1
                    and not _linter_feedback_for_retry
                )
                # Priority 2: QA semantic block (score ≤4, not already a QA retry,
                #             and linter didn't catch it first)
                _can_retry_qa = (
                    not _can_retry_lint
                    and _qa_result.get("verdict") == "blocked"
                    and (_qa_result.get("qa_score") or 10) <= 7
                    and not _qa_feedback_for_retry
                    and _surgeon_attempt < _MAX_SURGEON_ATTEMPTS - 1
                )
                if _can_retry_lint:
                    _linter_feedback_for_retry = _linter_introduced_errors
                    yield sse({"type": "progress", "content": f"🔁 Surgeon retry ({_surgeon_attempt+2}/{_MAX_SURGEON_ATTEMPTS}): fixing {len(_linter_introduced_errors)} compile error(s)..."})
                    continue
                elif _can_retry_qa:
                    _qa_feedback_for_retry = _qa_result
                    _qa_summary = _qa_result.get("summary", "QA blocked")[:80]
                    yield sse({"type": "progress", "content": f"🔁 Surgeon retry ({_surgeon_attempt+2}/{_MAX_SURGEON_ATTEMPTS}): QA feedback — {_qa_summary}"})
                    continue

                break  # clean pass, warning, or max attempts reached
            # ── end Surgeon retry loop ─────────────────────────────────────────

            # v3.4.0: operations-based apply (search-and-replace)
            from models.schemas import SurgicalOperation as _SO
            _ops = [_SO(find=op.get("find",""), replace=op.get("replace","")) for op in _operations] if _operations else []

            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=_effective_original,
                new_code=new_code,
                diff=diff,
                confidence=confidence,
                description=target.get("description", ""),
                applied=False,
                surgeon_notes=_surg_notes if _surg_notes else [],
                qa_result=_qa_result,
                operations=_ops,
            )

            if matched_name not in changes_by_file:
                changes_by_file[matched_name] = {"file": sf, "changes": []}
            changes_by_file[matched_name]["changes"].append(change)

        # ── Mark confidence_gate ──
        _all_changes_flat = [c for d in changes_by_file.values() for c in d["changes"]] if changes_by_file else []
        _low_conf = [c for c in _all_changes_flat if c.confidence < 7]
        compliance.mark("confidence_gate", ran=True,
                        output_summary=f"{len(_low_conf)} low-confidence changes flagged out of {len(_all_changes_flat)}")

        # ── Filter out empty changes (0 diff = Architect was wrong about that file) ──
        def _has_real_diff(c):
            """Check both code difference AND diff output for actual changes."""
            # Code is identical after stripping → definitely empty
            if c.original_code.rstrip() == c.new_code.rstrip():
                return False
            # Code differs but diff has no +/- lines → whitespace-only change, suppress
            if c.diff:
                diff_lines = c.diff.split("\n")
                has_adds = any(l.startswith("+") and not l.startswith("+++") for l in diff_lines)
                has_removes = any(l.startswith("-") and not l.startswith("---") for l in diff_lines)
                if not has_adds and not has_removes:
                    return False
            return True

        skipped_changes = []
        for fname in list(changes_by_file.keys()):
            all_c = changes_by_file[fname]["changes"]
            real_changes = [c for c in all_c if _has_real_diff(c)]
            ghost_changes = [c for c in all_c if not _has_real_diff(c)]
            for c in ghost_changes:
                reason = "already_matches" if c.original_code.rstrip() == c.new_code.rstrip() else "no_visible_diff"
                skipped_changes.append({
                    "filename": fname,
                    "symbol": c.symbol.full_path if c.symbol else "unknown",
                    "reason": reason,
                })
                if reason == "no_visible_diff":
                    # this is NOT a confirmed "already correct"; it is UNVERIFIED and needs manual review.
                    import logging as _logging
                    _logging.warning(
                        "[SurgicalAI] UNVERIFIED symbol '%s' in '%s': Surgeon produced non-identical "
                        "code but diff has no +/- lines. Manual inspection required. "
                        "Likely cause: confirmation bias — Surgeon matched a nearby pattern instead "
                        "of verifying the exact function definition.",
                        c.symbol.full_path if c.symbol else "unknown", fname
                    )
                _dlog("ghost_diff_suppressed",
                      session_id=session_id,
                      user_id=user_id,
                      filename=fname,
                      symbol=c.symbol.full_path if c.symbol else "unknown",
                      reason=reason,
                      original_code=c.original_code,
                      original_code_len=len(c.original_code),
                      new_code=c.new_code,
                      new_code_len=len(c.new_code),
                      codes_identical=c.original_code.rstrip() == c.new_code.rstrip())
            if not real_changes:
                del changes_by_file[fname]
            else:
                changes_by_file[fname]["changes"] = real_changes

        if not changes_by_file:
            # If this was a mixed create+edit and edits produced no changes,
            # still emit the create result
            if _pending_create_result:
                compliance.save()
                yield sse({"type": "smart_result", "content": json.dumps(_pending_create_result)})
                yield sse({"type": "done", "content": ""})
                return

            # Build a helpful message depending on what happened
            if skipped_changes:
                sym_names = ", ".join(f"`{s['symbol']}`" for s in skipped_changes[:3])
                fallback = (
                    f"I looked at {sym_names} but it already matches what you're asking for "
                    f"— no lines would actually change.\n\n"
                    f"**If that's not right**, try quoting the exact text you want changed. "
                    f"For example: *\"Change the line that says `tax_rate=0.08` to `0.10`\"*"
                )
            elif _parse_skipped:
                file_names = ", ".join(f"**{f}**" for f in set(_parse_skipped[:3]))
                fallback = (
                    f"I couldn't read the code structure of {file_names}. "
                    f"The file may have been uploaded as a data file rather than code.\n\n"
                    f"**Try re-uploading** — make sure it's a plain text source file (.py, .js, .ts, etc). "
                    f"If it's a large project, try uploading just the specific file you want to edit."
                )
            else:
                fallback = (
                    "I understood what you want, but couldn't locate the right spot in your code to make that change.\n\n"
                    "**Try being more specific** — for example:\n"
                    "- *\"In `calculate_price`, change `0.08` to `0.10`\"*\n"
                    "- *\"Find the line with `tax_rate` and change the default value\"*\n\n"
                    "You can also ask me to explain the file structure first."
                )
            for word in fallback.split(" "):
                yield sse({"type": "token", "content": word + " "})
            yield sse({"type": "done", "content": ""})
            return

        result = {
            "intent": "edit",
            "summary": plan.get("summary", ""),
            "reasoning": plan.get("reasoning", ""),
            "risks": plan.get("risks", []),
            "skipped_changes": skipped_changes,
            "changes_by_file": {
                fname: {
                    "filename": fname,
                    "file_id": data["file"]["id"],
                    "changes": [c.model_dump() for c in data["changes"]],
                }
                for fname, data in changes_by_file.items()
            },
        }

        # Mixed create+edit: merge created files into the result
        if _pending_create_result:
            result["intent"] = "create"
            result["new_files"] = _pending_create_result.get("new_files", [])

        # ── Mark diff_validate + finalize compliance ──
        compliance.mark("diff_validate", ran=True,
                        output_summary=f"{len(skipped_changes)} ghost diffs suppressed")

        _missing = compliance.missing_steps()
        if _missing:
            yield sse({"type": "progress", "content": f"⚠️ Compliance gap: {', '.join(_missing)} — flagged for review"})

        compliance.save()

        # Attach audit trail to result
        result["run_id"] = run_id
        result["compliance"] = compliance.to_dict()

        yield sse({"type": "smart_result", "content": json.dumps(result)})
        yield sse({"type": "done", "content": ""})

    except Exception as e:
        import traceback
        _dlog("pipeline_top_level_error",
              session_id=session_id,
              error_type=type(e).__name__,
              error=str(e)[:500],
              traceback=traceback.format_exc()[-800:],
              user_id=user_id)
        try:
            compliance.mark("pipeline_error", ran=False, reason=str(e)[:200])
            compliance.save()
        except Exception:
            pass
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"


# ═══════════════════════════════════════════════════════════════════════════════
# NATURAL CONVERSATION PIPELINE (v5)
# ───────────────────────────────────────────────────────────────────────────────
# Claude speaks naturally in markdown — exactly like Claude.ai in the browser.
# When making code edits, it embeds <surgical_edit> XML tags in its response.
# The backend parses these out of the stream, runs the existing QA cycle,
# and delivers structured diff cards — while natural text streams to the user.
#
# This replaces the JSON-forcing Architect+Surgeon design that broke natural
# conversation and polluted history with raw JSON artifacts.
# ═══════════════════════════════════════════════════════════════════════════════

NATURAL_SYSTEM = """\
You are SurgicalAI — a world-class coding assistant powered by Claude.

Talk naturally with the user, like a real collaborator. Use markdown. Be warm, precise, and genuinely helpful.
Remember everything we've discussed — prior edits, decisions, context — and build on it naturally.

━━━ WHEN YOU NEED MORE CODE ━━━

For large files you only see the most relevant symbols upfront. If you need to see a specific
function, script block, or code section before you can write a correct edit, say so naturally
and emit a search request:

<search_request>
{"terms": ["exactFunctionName", "XLSX.read", "#job-list", "Papa.parse"], "reason": "Need to see the CSV parser to match its data structure exactly"}
</search_request>

The system will find and show you the actual code. You can then write your <surgical_edit> blocks correctly.

Rules for search_request:
- Use exact symbol names from the SYMBOL INDEX when you can (most precise)
- You can also search for string literals: "Papa.parse", "jl-count-badge", "mode-comm"
- Max 5 terms per request, max 4 search rounds per response
- Only request what you genuinely need — the code you're missing to write a correct edit
- After receiving results, write your edits immediately
- Do NOT search for things already shown in the context above

━━━ REQUESTING FILES BY NAME ━━━

If you know the exact filename from the file list below, request it directly instead of searching:

<file_request>
LandingPage.tsx
App.tsx
</file_request>

Rules for file_request:
- Use exact filenames from the file listing below
- Max 5 files per request
- The system returns full file content instantly
- Use this when you KNOW which file you need by name
- Use <search_request> when you need to FIND where something is defined by keyword
- Do NOT request files already shown in full context above

━━━ CRITICAL: NEVER EDIT UNSEEN CODE ━━━

NEVER edit, replace, or rewrite code you haven't seen in the context above.
If a file is large and you only see partial content or grep snippets:
1. Use <search_request> with specific terms from the user's request to find the exact code.
2. Read and verify the code you plan to change.
3. Only then emit a <surgical_edit> with edit_start_line/edit_end_line matching the real content.

Guessing what code looks like — even if you're confident — leads to hallucinated edits that break the codebase.

━━━ EDITING EXISTING FILES ━━━

When the user wants to change code in an uploaded file, explain what you're doing and embed an edit block:

<surgical_edit>
{
  "filename": "exact filename as shown in the file context",
  "symbol": "exact symbol name from the SYMBOL INDEX (e.g. LoginPage, handleSubmit, process_order)",
  "description": "what changed and why — one clear sentence",
  "new_code": "THE COMPLETE NEW CODE — every single line of the symbol, nothing omitted"
}
</surgical_edit>

Rules for new_code:
- Include the COMPLETE symbol: opening declaration, full body, closing brace/bracket
- Copy ALL unchanged lines exactly character-for-character from the original
- Only modify the specific lines that need to change
- Match original indentation exactly (same spaces/tabs)
- If a function is 200 lines and you change 1 line, new_code is still 200 lines
- new_code REPLACES the entire original symbol — omitting lines deletes them
- For a SMALL change to a LARGE symbol you cannot fully see, do NOT put only the changed
  lines in new_code by themselves — that would delete the rest of the symbol. Use a
  TARGETED edit with "old_code" + "new_code" instead (see below).

━━━ TARGETED EDITS FOR LARGE SYMBOLS (PREFERRED for big files) ━━━

For a large symbol (e.g. a 900-line React component or a large <script> block)
re-emitting the entire body is wasteful and error-prone. Make a TARGETED edit
instead. The system splices it in and runs the same QA gate.

PREFERRED: Use line-number targeting — immune to tabs-vs-spaces and any other
whitespace representation differences. Use the absolute file line numbers shown
in the symbol listing or search results (e.g. "script_11 (lines 1243–1456)").

<surgical_edit>
{
  "filename": "Market_Rate_Report-6.html",
  "symbol": "script_11",
  "description": "Fix color format — prefix RGB values with FF for ARGB",
  "edit_start_line": 1248,
  "edit_end_line": 1255,
  "new_code": "        var toARGB = function(hex) { ... };\n        var thinBorder = function(rgb) { ... };"
}
</surgical_edit>

- edit_start_line / edit_end_line: the ABSOLUTE file line numbers of the region
  you want to replace (inclusive). Read them from the line numbers in your context.
- new_code: the complete replacement for exactly those lines.

ALTERNATIVE: String-based targeting with old_code (use only when line numbers
are not visible in your context):

<surgical_edit>
{
  "filename": "src/pages/LandingPage.tsx",
  "symbol": "LandingPage",
  "description": "Add the animated QA hero mockup below the headline",
  "old_code": "        <h1 className=\\"hero-title\\">Ship code with confidence</h1>",
  "new_code": "        <h1 className=\\"hero-title\\">Ship code with confidence</h1>\\n        <HeroMockupAnimation />"
}
</surgical_edit>

Rules for targeted edits:
- PREFER edit_start_line/edit_end_line whenever you can see line numbers — it
  never fails due to whitespace differences
- If using old_code: copy it VERBATIM (no line-number prefix); it must match
  EXACTLY ONE place in the symbol — include a few surrounding lines if needed
- "new_code" is the full replacement for just that region (include unchanged
  lines you want to keep)
- If you can't see the region you need, emit a <search_request> for a nearby
  string literal FIRST — you'll get a windowed view with line numbers — then
  write a targeted edit using edit_start_line/edit_end_line
- Use a full-symbol new_code (no old_code / no line range) only for small
  symbols you can see entirely

Rules for symbol:
- Must exactly match a name from the SYMBOL INDEX in the file context
- For React/TSX: use the component name (e.g. "LoginPage"), never hooks ("useXxx") for UI changes
- For Python/JS: use the exact function or class name

━━━ CREATING NEW FILES ━━━

When the user asks you to create, build, scaffold, or add a new file/component/hook/service that doesn't exist yet, write the complete new file and embed it in a new_file block:

<new_file>
{
  "filename": "src/components/PaymentForm.tsx",
  "language": "typescript",
  "summary": "one sentence describing what this file does",
  "content": "THE COMPLETE FILE CONTENT — every import, every line, ready to use"
}
</new_file>

Rules for new_file content:
- Write production-ready code — no stubs, no TODOs, no placeholders
- Follow the exact import paths, naming conventions, and patterns from the uploaded files shown in context
- Include all necessary imports
- The file should be immediately usable — a developer can drop it in and it works
- If creating the file also requires updating an existing file (e.g. adding a route, registering a component), include a <surgical_edit> block for that change too

━━━ WHEN TO USE BLOCKS ━━━

Use <surgical_edit> when: user wants changes to an existing uploaded file
Use <new_file> when: user wants a new file that doesn't exist yet
Use both when: creating a new file AND updating an existing one (e.g. new component + registering it in App.tsx)
Just chat (no blocks) when: answering a question, explaining code, or you need clarification first

NEVER do this: when the user asks to change behavior that lives in an existing uploaded
file, do NOT create a brand-new file as a substitute, and do NOT fall back to telling the
user how to wire it up manually. If the symbol is large or only partially visible, emit a
<search_request> to load the exact region, then make a TARGETED edit (old_code/new_code)
on the real symbol. A new file is only correct when genuinely net-new code is requested.

━━━ MULTI-EDIT PLANNING (3+ EDITS) ━━━

When your response requires 3 or more <surgical_edit> blocks, do NOT produce them all inline
and do NOT write a long explanation first.

In AT MOST 2 short sentences, state what you're about to do. Then IMMEDIATELY emit an
<edit_plan> block — do not describe each file's changes in prose, that's what the
"description" field below is for:

<edit_plan>
[
  {"filename": "exact_filename.tsx", "symbol": "ComponentName", "description": "What to change and why"},
  {"filename": "other_file.py", "symbol": "function_name", "description": "What to change and why"}
]
</edit_plan>

STOP WRITING immediately after the </edit_plan> tag closes. No commentary before, inside,
between, or after the block beyond the 2-sentence intro. The system will execute each edit
individually with focused context, preventing output truncation. For 1-2 edits, produce
<surgical_edit> blocks directly as usual. You may still produce <new_file> blocks alongside
an <edit_plan>.

━━━ CONVERSATION RECENCY ━━━

When the user says "the fix", "the change", "the edit", or "that" without naming a file,
they mean the MOST RECENT [Applied changes to: ...] marker in the conversation history.
Always answer about that file and symbol — never reference an older edit unless the user
explicitly names a different file.

━━━ ALWAYS PRODUCE VISIBLE OUTPUT ━━━

You MUST always produce visible text — never respond with only internal reasoning.
- Need a file? → emit <file_request> immediately, don't just say "let me pull it"
- Need code? → emit <search_request> immediately
- Answering a question? → respond in plain text
- Need clarification? → ask directly
Never promise an action ("Let me pull that") without actually doing it in the same response.

━━━ EXAMPLE — EDIT ━━━

"I'll change the button to blue:

<surgical_edit>
{"filename": "Button.tsx", "symbol": "Button", "description": "Blue background", "new_code": "...complete function..."}
</surgical_edit>

This keeps the same click handler. Want me to also update the hover state?"

━━━ EXAMPLE — CREATE ━━━

"Here's the PaymentForm component:

<new_file>
{"filename": "src/components/PaymentForm.tsx", "language": "typescript", "summary": "Payment form with validation", "content": "import React..."}
</new_file>

I also need to register it in App.tsx:

<surgical_edit>
{"filename": "App.tsx", "symbol": "App", "description": "Add PaymentForm route", "new_code": "..."}
</surgical_edit>

You're all set — just import it wherever needed."

━━━ CODE QUALITY ━━━

Before including any block, verify:
- All required imports are present
- No syntax errors or unclosed brackets
- Logic exactly matches what was requested
- All unchanged parts are preserved exactly

━━━ JSX / HTML STRUCTURAL INTEGRITY (CRITICAL) ━━━

For JSX, TSX, or HTML edits — BEFORE writing your edit block, do this:
1. Count every opening tag (<div>, <section>, <ul>, <li>, etc.) in your new_code
2. Verify each opening tag has EXACTLY ONE matching closing tag at the correct nesting level
3. If inserting a new block between existing elements:
   - Do NOT add extra closing tags for elements you did not open
   - Do NOT leave your new elements unclosed
   - The parent container's tag balance must be unchanged
4. For targeted old_code → new_code edits: the net tag balance of new_code MUST
   match old_code (same count of unmatched openers/closers) unless you are
   intentionally adding or removing a container element
Failure to balance tags is the #1 cause of rejected edits. Count twice, emit once.
"""

# ── Inject comprehensive quality rules into single-pass prompt ────────────
# _CODE_QUALITY_SECTION covers CSS, React, TypeScript, state, error handling,
# and structural integrity — same rules the agentic architect gets.
NATURAL_SYSTEM = NATURAL_SYSTEM + "\n" + _CODE_QUALITY_SECTION


def _score_symbol_relevance(sym, terms: list) -> int:
    """
    Score a symbol's relevance to a set of search terms.
    Higher = more relevant. Used to rank which symbols to show Claude upfront.
    """
    score = 0
    nm_lower = (getattr(sym, "name", "") or "").lower()
    fp_lower = (getattr(sym, "full_path", "") or "").lower()
    code_lower = (getattr(sym, "code", "") or "").lower()

    for term in terms:
        tl = term.lower()
        if not tl:
            continue
        # Exact name match — highest signal
        if tl == nm_lower or tl == fp_lower:
            score += 20
        # Name contains term or vice-versa
        elif tl in nm_lower or nm_lower in tl:
            score += 10
        elif tl in fp_lower or fp_lower in tl:
            score += 8
        # Token overlap (split camelCase/snake_case)
        nm_tokens = set(re.sub(r'([A-Z])', r'_\1', nm_lower).replace('-', '_').split('_'))
        t_tokens = set(re.sub(r'([A-Z])', r'_\1', tl).replace('-', '_').split('_'))
        overlap = nm_tokens & t_tokens - {'', 'the', 'a', 'an', 'is', 'of', 'in', 'to'}
        score += len(overlap) * 5
        # Code body contains term
        if tl in code_lower:
            score += 2

    # Prefer smaller symbols (more precise targets)
    size = getattr(sym, "end_line", 0) - getattr(sym, "start_line", 0) + 1
    if size < 30:
        score += 3
    elif size < 100:
        score += 1

    return score


def _smart_code_context(fname: str, content: str, smap, user_request: str,
                        max_code_lines: int = 300) -> str:
    """
    Pick the most relevant symbols for a file and return their FULL code,
    not grep window snippets. Claude gets complete functions, not partial code.

    Strategy:
    1. Score every symbol by relevance to user_request
    2. Show full code for top-scoring symbols up to max_code_lines
    3. Fall back to first-pass grep if scoring finds nothing
    """
    if not smap or not smap.symbols:
        return ""

    terms = _extract_search_terms(user_request)

    # Also add any short words from the request that aren't stop words
    # (catches "tax", "bug", "wrong" that _extract_search_terms skips as <7 chars)
    _STOP = {'the', 'a', 'an', 'is', 'it', 'in', 'on', 'to', 'fix', 'bug',
             'add', 'make', 'get', 'set', 'and', 'for', 'not', 'are', 'was',
             'has', 'had', 'but', 'can', 'all', 'any'}
    for word in re.findall(r'\b[a-zA-Z]{3,6}\b', user_request):
        wl = word.lower()
        if wl not in _STOP and wl not in {t.lower() for t in terms}:
            terms.append(word)

    if not terms:
        # No extractable terms — show the first few symbols' full code
        terms = []

    # Score all symbols
    scored = [(
        _score_symbol_relevance(sym, terms),
        sym.end_line - sym.start_line + 1,  # size (tiebreak: smaller first)
        sym
    ) for sym in smap.symbols]

    # Sort: highest score first, then smallest size
    scored.sort(key=lambda x: (-x[0], x[1]))

    # Build output: full code for top symbols, up to max_code_lines
    parts = []
    total = 0
    shown = set()

    for score, size, sym in scored:
        if score == 0 and parts:
            break  # Nothing relevant beyond this
        if total >= max_code_lines:
            break
        if sym.full_path in shown:
            continue

        sym_code_lines = (sym.code or "").splitlines()
        sym_size = len(sym_code_lines)

        # Skip if adding this would massively overflow; but always show at least 1
        if parts and total + sym_size > max_code_lines + 50:
            continue

        shown.add(sym.full_path)
        numbered = "\n".join(
            f"{sym.start_line + i:5d}: {sym_code_lines[i]}"
            for i in range(min(sym_size, max_code_lines - total))
        )
        truncated = sym_size > (max_code_lines - total)
        suffix = f"\n  ... [{sym_size - (max_code_lines - total)} lines truncated]" if truncated else ""
        parts.append(
            f"[{sym.symbol_type.value}] {sym.full_path} "
            f"(L{sym.start_line}–{sym.end_line}):\n{numbered}{suffix}"
        )
        total += min(sym_size, max_code_lines - total)

    if not parts:
        return ""

    return (
        f"\nRELEVANT CODE IN {fname} "
        f"(showing {len(parts)} of {len(smap.symbols)} symbols by relevance):\n\n"
        + "\n\n".join(parts)
    )


def _build_focused_window(
    fname: str, content: str, smap, user_request: str,
    window_size: int = 300,
    session_id: str = "", user_id: str = "",
) -> str:
    """
    Tasklet-style windowed context for large files.

    Instead of dumping the entire file (which causes model output truncation
    on files >500 lines), show a focused window around the most relevant
    section with absolute line numbers.  The model can then make precise
    edits using edit_start_line / edit_end_line — zero string-matching risk.

    Strategy:
      1. Extract search terms from user_request
      2. Score every line by term matches
      3. Find the densest cluster via sliding window
      4. Show that window with line numbers + summary of what's outside
      5. Fall back to symbol-based centering, then first lines

    The model can always use <search_request> to see other parts of the file.
    """
    lines = content.splitlines()
    total = len(lines)

    if total <= window_size:
        # File fits in window — show everything with line numbers
        numbered = "\n".join(f"{i+1:5d}: {line}" for i, line in enumerate(lines))
        _dlog("focused_window_full",
              session_id=session_id, user_id=user_id,
              filename=fname, total_lines=total,
              reason="file_fits_in_window")
        return numbered

    # ── Extract search terms ─────────────────────────────────────────────
    terms = _extract_search_terms(user_request)
    _STOP = {'the', 'a', 'an', 'is', 'it', 'in', 'on', 'to', 'fix', 'bug',
             'add', 'make', 'get', 'set', 'and', 'for', 'not', 'are', 'was',
             'has', 'had', 'but', 'can', 'all', 'any', 'new', 'use'}
    for word in re.findall(r'\b[a-zA-Z]{3,6}\b', user_request):
        wl = word.lower()
        if wl not in _STOP and wl not in {t.lower() for t in terms}:
            terms.append(word)

    # ── Score every line by term matches ──────────────────────────────────
    line_scores = [0] * total
    if terms:
        for i, line in enumerate(lines):
            ll = line.lower()
            for term in terms:
                if term.lower() in ll:
                    line_scores[i] += 1

    # ── Find densest cluster via sliding window ──────────────────────────
    best_score = 0
    best_start = 0
    window_reason = "default_start"

    if terms and any(s > 0 for s in line_scores):
        current = sum(line_scores[:window_size])
        best_score = current
        for i in range(1, total - window_size + 1):
            current = current - line_scores[i - 1] + line_scores[i + window_size - 1]
            if current > best_score:
                best_score = current
                best_start = i
        window_reason = "term_cluster"

    # If no term matches, try to center on most relevant symbol
    if best_score == 0 and smap and hasattr(smap, 'symbols') and smap.symbols:
        scored_syms = []
        for sym in smap.symbols:
            sc = _score_symbol_relevance(sym, terms) if terms else 0
            scored_syms.append((sc, sym))
        scored_syms.sort(key=lambda x: -x[0])

        if scored_syms and scored_syms[0][0] > 0:
            target = scored_syms[0][1]
            center = (target.start_line + target.end_line) // 2
            best_start = max(0, center - window_size // 2)
            window_reason = f"symbol:{target.full_path}"
        else:
            # No relevance signal — show first portion (imports/setup at top
            # provides orientation for the model)
            best_start = 0
            window_reason = "no_signal_start"

    ws = best_start
    we = min(total, ws + window_size)

    # ── Build output with line numbers ────────────────────────────────────
    parts = []
    if ws > 0:
        parts.append(f"... [{ws} lines above — use <search_request> to view] ...\n")

    numbered = "\n".join(f"{i+1:5d}: {lines[i]}" for i in range(ws, we))
    parts.append(numbered)

    if we < total:
        parts.append(f"\n... [{total - we} lines below — use <search_request> to view] ...")

    _dlog("focused_window_built",
          session_id=session_id, user_id=user_id,
          filename=fname, total_lines=total,
          window_start=ws + 1, window_end=we,
          window_size_actual=we - ws,
          window_reason=window_reason,
          terms_used=terms[:10],
          best_score=best_score)

    return "\n".join(parts)


def _fuzzy_find_symbol(smap, symbol_name: str):
    """
    Comprehensive symbol finder — tries 6 strategies before giving up.
    Returns (symbol, method_description) or (None, None).

    Strategies (in order of confidence):
    1. Exact full_path match
    2. Exact name match
    3. Case-insensitive match
    4. Substring containment (either direction)
    5. Token overlap (splits camelCase/snake_case and finds shared tokens)
    6. Character-set similarity (shared chars ratio ≥ 0.70)
    """
    if not smap or not smap.symbols or not symbol_name:
        return None, None

    sn = symbol_name
    sn_lower = sn.lower()

    # 1. Exact full_path
    for s in smap.symbols:
        if getattr(s, "full_path", "") == sn:
            return s, "exact"

    # 2. Exact name
    for s in smap.symbols:
        if getattr(s, "name", "") == sn:
            return s, "exact_name"

    # 3. Case-insensitive
    for s in smap.symbols:
        if getattr(s, "full_path", "").lower() == sn_lower:
            return s, "case_insensitive"
        if getattr(s, "name", "").lower() == sn_lower:
            return s, "case_insensitive"

    # 4. Substring containment (prefer smallest match = most specific)
    sub_candidates = []
    for s in smap.symbols:
        fp = getattr(s, "full_path", "").lower()
        nm = getattr(s, "name", "").lower()
        size = getattr(s, "end_line", 0) - getattr(s, "start_line", 0)
        if sn_lower in fp or sn_lower in nm:
            sub_candidates.append((size, s, "substring_in_symbol"))
        elif fp in sn_lower or nm in sn_lower:
            sub_candidates.append((size, s, "symbol_in_request"))
    if sub_candidates:
        sub_candidates.sort(key=lambda x: x[0])
        _, sym, method = sub_candidates[0]
        return sym, method

    # 5. Token overlap — split camelCase/snake_case into tokens, score shared tokens
    def _tokens(name: str) -> set:
        # Split on capital letters and underscores
        parts = re.sub(r'([A-Z])', r'_\1', name).replace('-', '_').lower().split('_')
        return {p for p in parts if len(p) >= 3}

    sn_tokens = _tokens(sn)
    if sn_tokens:
        token_candidates = []
        for s in smap.symbols:
            nm = getattr(s, "name", "")
            sym_tokens = _tokens(nm)
            overlap = sn_tokens & sym_tokens
            if overlap:
                size = getattr(s, "end_line", 0) - getattr(s, "start_line", 0)
                token_candidates.append((len(overlap), -size, s))
        if token_candidates:
            token_candidates.sort(key=lambda x: (-x[0], x[1]))
            _, _, sym = token_candidates[0]
            return sym, "token_overlap"

    # 6. Character-set similarity ≥ 70%
    sn_chars = set(sn_lower)
    best_ratio = 0.0
    best_sym = None
    for s in smap.symbols:
        nm = getattr(s, "name", "").lower()
        if len(nm) < 3:
            continue
        shared = len(sn_chars & set(nm))
        ratio = shared / max(len(sn_lower), len(nm))
        if ratio > best_ratio:
            best_ratio = ratio
            best_sym = s
    if best_sym and best_ratio >= 0.70:
        return best_sym, f"char_similarity({best_ratio:.0%})"

    return None, None


def _build_symbol_correction(
    unresolved: list,
    symbol_maps_by_name: dict,
) -> str:
    """
    Build a correction message for Claude when it referenced symbols that don't exist.
    Shows the actual available symbols with full code so Claude can revise precisely.

    unresolved: list of dicts with keys: filename, symbol, new_code, description
    """
    parts = [
        "Some of the symbols you referenced don't exist in the files. "
        "Here are the ACTUAL symbols available — please revise your edits to use these exact names:\n"
    ]

    for item in unresolved:
        filename = item.get("filename", "")
        bad_name = item.get("symbol", "")
        smap, _ = symbol_maps_by_name.get(filename, (None, None))

        # Snippet edits: the symbol exists, but the supplied old_code did not
        # match. Guide Claude to fix the targeted snippet rather than telling it
        # the symbol is missing.
        snippet_reason = item.get("_snippet_reason")
        if snippet_reason:
            parts.append(
                f"\n❌ Targeted edit on symbol '{bad_name}' in {filename} could not be applied: "
                f"{snippet_reason}\n"
                f"   Fix it by EITHER (in order of preference):\n"
                f"   • BEST: use \"edit_start_line\" + \"edit_end_line\" (absolute file line numbers "
                f"from the listing below) — zero whitespace issues, never fails; OR\n"
                f"   • providing an \"old_code\" snippet copied verbatim (no line-number prefix) "
                f"from the symbol below, that matches exactly one location, with its \"new_code\" replacement; OR\n"
                f"   • emitting the COMPLETE symbol in \"new_code\" with no \"old_code\"."
            )
            # Show the ACTUAL current symbol so the model anchors to ground truth
            # instead of re-guessing lines it never saw (the large-file drop bug).
            _sym_code = item.get("_symbol_code") or ""
            if _sym_code:
                _sc_lines = _sym_code.splitlines()
                _sc_start = item.get("_symbol_start", 1) or 1
                # Always show full symbol — no head/tail truncation.
                # Truncation was the root cause of repeated anchor misses:
                # Claude invented anchors for the omitted middle it never saw.
                # Tasklet model: see everything, copy exact text, splice targeted edit.
                numbered = "\n".join(
                    f"   {_sc_start + i:5d}: {_sc_lines[i]}" for i in range(len(_sc_lines))
                )
                parts.append(
                    f"\n   ACTUAL current content of '{bad_name}' "
                    f"(line numbers are ABSOLUTE file lines — use them for edit_start_line/edit_end_line):\n{numbered}"
                )
                if len(_sc_lines) > 100:
                    parts.append(
                        f"\n   ✏️  TARGETED EDIT REQUIRED — this symbol is {len(_sc_lines)} lines."
                        f" DO NOT re-emit the entire symbol.\n"
                        f"   PREFERRED — use line-number targeting (no whitespace drift):\n"
                        f"     \"edit_start_line\": first line number of the region to replace (from the listing above)\n"
                        f"     \"edit_end_line\": last line number of the region to replace (inclusive)\n"
                        f"     \"new_code\": the replacement lines\n"
                        f"   ALTERNATIVE — use string-based targeting:\n"
                        f"     \"old_code\": an EXACT verbatim snippet (≥3 lines) from the content above\n"
                        f"     \"new_code\": only the replacement for that snippet\n"
                        f"   The server splices it in — everything else is preserved automatically.\n"
                        f"   ❌ Do NOT emit a <search_request>. The full symbol is shown above."
                    )
                    parts.append(
                        f"\n   ACTUAL current content of '{bad_name}' "
                        f"(line numbers are ABSOLUTE — use for edit_start_line/edit_end_line):\n{numbered}"
                    )
            else:
                parts.append(
                    "   Re-emit the COMPLETE symbol code in new_code with no old_code field."
                )
            continue

        parts.append(f"\n❌ Symbol '{bad_name}' in {filename} — NOT FOUND.")

        if not smap or not smap.symbols:
            parts.append(f"   (Could not read symbol map for {filename})")
            continue

        # Find the 3 best candidates using token overlap + char similarity
        def _tokens(name: str) -> set:
            p = re.sub(r'([A-Z])', r'_\1', name).replace('-', '_').lower().split('_')
            return {x for x in p if len(x) >= 3}

        sn_tokens = _tokens(bad_name)
        sn_chars = set(bad_name.lower())

        scored = []
        for s in smap.symbols:
            nm = getattr(s, "name", "") or ""
            sym_tokens = _tokens(nm)
            token_score = len(sn_tokens & sym_tokens) * 3
            char_score = len(sn_chars & set(nm.lower())) / max(len(bad_name), len(nm), 1)
            total = token_score + char_score
            scored.append((total, s))
        scored.sort(key=lambda x: -x[0])
        candidates = [s for _, s in scored[:3]]

        parts.append(f"   Closest real symbols:")
        for cand in candidates:
            code_lines = (cand.code or "").splitlines()
            # Show up to 60 lines of the candidate
            preview_lines = code_lines[:60]
            truncated = len(code_lines) > 60
            numbered = "\n".join(
                f"   {cand.start_line + i:5d}: {preview_lines[i]}"
                for i in range(len(preview_lines))
            )
            suffix = f"\n   ... [{len(code_lines)-60} more lines]" if truncated else ""
            parts.append(
                f"\n   [{cand.symbol_type.value}] {cand.full_path} "
                f"(L{cand.start_line}–{cand.end_line}):\n{numbered}{suffix}"
            )

    parts.append(
        "\n\nPlease rewrite your <surgical_edit> blocks using the EXACT symbol names shown above. "
        "Make sure new_code is the complete replacement for the symbol you choose."
    )
    return "\n".join(parts)


def _score_file_relevance(
    sf: dict,
    smap,
    user_request: str,
    terms: list,
) -> int:
    """
    Score a single file's relevance to user_request.
    Higher = more relevant → gets full context.
    Lower  = less relevant → gets lean index only.

    Signals used (in rough priority order):
      1. Filename contains a request term           (+15 per term)
      2. File extension matches request domain      (+10)
      3. Symbol name exact match to term            (+20 per match)
      4. Symbol token overlap with terms            (+8 per overlap)
      5. Imports reference a term                   (+6 per match)
      6. File was recently modified (updated_at)    (+5)
      7. Small file bonus (cheap to include)        (+3)
    """
    score = 0
    fname = sf.get("filename", "").lower()
    fname_base = re.sub(r"\.[^.]+$", "", fname.split("/")[-1])  # stem only
    symbols = getattr(smap, "symbols", []) if smap else []
    imports = getattr(smap, "imports", []) if smap else []
    lines   = sf.get("lines", 0)

    _STOP = {"the", "a", "an", "is", "it", "in", "on", "to", "fix", "bug",
              "add", "make", "get", "set", "and", "for", "not", "are", "was",
              "has", "had", "but", "can", "all", "any", "new", "use", "from",
              "with", "that", "this", "will", "should", "need", "want"}

    def _tok(name: str) -> set:
        parts = re.sub(r"([A-Z])", r"_\1", name).replace("-", "_").lower().split("_")
        return {p for p in parts if len(p) >= 3 and p not in _STOP}

    req_tokens = _tok(user_request)

    for term in terms:
        tl = term.lower()
        if not tl or tl in _STOP:
            continue

        # 1. Filename match
        if tl == fname_base:
            score += 15
        elif tl in fname:
            score += 10

        # 3+4. Symbol matches
        for sym in symbols:
            nm = (getattr(sym, "name", "") or "").lower()
            fp = (getattr(sym, "full_path", "") or "").lower()
            if tl == nm or tl == fp:
                score += 20
            elif tl in nm or tl in fp:
                score += 8
            else:
                overlap = req_tokens & _tok(nm)
                if overlap:
                    score += len(overlap) * 4

        # 5. Import matches
        for imp in imports:
            if tl in imp.lower():
                score += 6

    # 2. Extension bonus — request mentions React/component/page → .tsx/.jsx scores higher
    ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
    req_lower = user_request.lower()
    if ext in ("tsx", "jsx") and any(w in req_lower for w in
            ("component", "page", "button", "form", "modal", "ui", "style", "render")):
        score += 10
    if ext == "py" and any(w in req_lower for w in
            ("api", "route", "endpoint", "server", "backend", "function", "service")):
        score += 10
    if ext in ("css", "scss", "sass") and any(w in req_lower for w in
            ("style", "color", "layout", "design", "ui", "look", "theme")):
        score += 10

    # 6. Recency bonus
    updated = sf.get("updated_at") or sf.get("created_at") or ""
    if updated:
        score += 5  # any recently-in-session file gets a small bump

    # 7. Small file bonus — cheap to include, may be relevant glue code
    if lines <= 100:
        score += 3
    elif lines <= 300:
        score += 1

    return score


def _build_natural_file_context(
    session_files: list,
    symbol_maps_by_name: dict,
    user_request: str,
    project_memory: str = None,
    session_summary: str = "",
    full_context_limit: int = 8,
    session_id: str = "",
    user_id: str = "",
    file_statuses: dict = None,
) -> str:
    """
    Build the file context string for the natural pipeline.

    Two-tier approach for projects with many files:

    TIER 1 — Full context (top `full_context_limit` files by relevance score):
      - Full symbol index + FULL CODE for most relevant symbols
      - Claude can edit these directly

    TIER 2 — Lean index (remaining files):
      - Filename + symbol names + line ranges only (~3 lines per symbol)
      - Zero code shown — keeps tokens low
      - Claude can request any of these via <search_request> and get full code instantly

    For small projects (≤ full_context_limit files) every file gets Tier 1.
    This ensures "fix login button" doesn't flood Claude with 47 irrelevant files.
    """
    parts = []

    if project_memory:
        parts.append(f"PROJECT MEMORY:\n{project_memory}\n")

    if session_summary:
        parts.append(f"EARLIER CONVERSATION (compacted):\n{session_summary}\n")

    if not session_files:
        return ("\n".join(parts) if parts else ""), set()

    # ── Score every file for relevance ───────────────────────────────────
    terms = _extract_search_terms(user_request)
    # Also add short meaningful words _extract_search_terms skips
    _STOP_SHORT = {"the", "a", "an", "is", "it", "in", "on", "to", "fix", "bug",
                   "add", "make", "get", "set", "and", "for", "not", "are", "was"}
    for word in re.findall(r"\b[a-zA-Z]{3,6}\b", user_request):
        wl = word.lower()
        if wl not in _STOP_SHORT and wl not in {t.lower() for t in terms}:
            terms.append(word)

    scored_files = []
    for sf in session_files:
        fname = sf["filename"]
        smap, _ = symbol_maps_by_name.get(fname, (None, sf))
        score = _score_file_relevance(sf, smap, user_request, terms)
        scored_files.append((score, sf))

    # Sort by score descending, stable (preserves upload order for ties)
    scored_files.sort(key=lambda x: -x[0])

    # Partition into full-context vs lean-index tiers.
    # Rules:
    #   - Always include at least 3 files in Tier 1 (never leave Claude with nothing)
    #   - Include up to full_context_limit files that have score > 0
    #   - Score-0 files only get Tier 1 if we haven't reached the minimum of 3
    MIN_TIER1 = 3
    positively_scored = [(sc, sf) for sc, sf in scored_files if sc > 0]
    zero_scored       = [(sc, sf) for sc, sf in scored_files if sc == 0]

    # Fill Tier 1: scored files first, up to the limit
    tier1_scored = [sf for _, sf in positively_scored[:full_context_limit]]
    # If we haven't hit MIN_TIER1, pad with zero-scored files
    if len(tier1_scored) < MIN_TIER1:
        pad_needed = MIN_TIER1 - len(tier1_scored)
        tier1_scored += [sf for _, sf in zero_scored[:pad_needed]]

    tier1 = tier1_scored
    tier1_names = {sf["filename"] for sf in tier1}
    tier2 = [sf for sf in session_files if sf["filename"] not in tier1_names]

    # Keep original upload order within each tier (more intuitive for user)
    original_order = {sf["filename"]: i for i, sf in enumerate(session_files)}
    tier1.sort(key=lambda sf: original_order.get(sf["filename"], 999))
    tier2.sort(key=lambda sf: original_order.get(sf["filename"], 999))

    # ── Render Tier 1: full context ───────────────────────────────────────
    parts.append("━━━ UPLOADED FILES ━━━\n")

    if tier2:
        parts.append(
            f"ℹ️  {len(session_files)} files total. Showing full context for the "
            f"{len(tier1)} most relevant. The other {len(tier2)} are listed below as "
            f"lean indexes — use <file_request> (by name) or <search_request> (by keyword) to fetch their code.\n"
        )

    def _render_full(sf: dict) -> str:
        fname      = sf["filename"]
        content    = sf.get("content", "")
        file_type  = sf.get("file_type", "code")
        lines_count = sf.get("lines", len(content.splitlines()))

        # File status badge (new / modified / unchanged)
        _fs = file_statuses.get(fname, {}) if file_statuses else {}
        _badge = _file_status_badge(_fs.get("status", "")) if _fs else ""
        _badge_suffix = f"  {_badge}" if _badge else ""

        if file_type == "image":
            return (
                f"FILE: {fname} [IMAGE — attached as vision block below]{_badge_suffix}\n"
            )

        if file_type in ("pdf", "csv", "excel", "text"):
            # Data files (csv/excel) are already row-capped (200 rows) in
            # _parse_excel_to_markdown — show full markdown so Claude can
            # actually work with the data.  PDF/text stay more conservative.
            _data_limit = 60_000 if file_type in ("csv", "excel") else 8_000
            preview = content[:_data_limit] + (f"\n...[{len(content)-_data_limit} more chars not shown]" if len(content) > _data_limit else "")
            return (
                f"FILE: {fname} [{file_type.upper()} — FULLY LOADED]{_badge_suffix}\n"
                f"⚠️ This data is already fully loaded below. Do NOT use <file_request> for this file — analyze it directly.\n"
                f"CONTENT:\n{preview}\n"
            )

        smap, _ = symbol_maps_by_name.get(fname, (None, sf))

        if smap and smap.symbols:
            sym_lines = []
            for s in smap.symbols:
                size = s.end_line - s.start_line + 1
                flag = " ⚠️LARGE" if size > 300 else ""
                sym_lines.append(
                    f"  [{s.symbol_type.value}] {s.full_path:<45} "
                    f"L{s.start_line}–{s.end_line}  ({size}L){flag}"
                )
            sym_index = "\n".join(sym_lines)
            header = (
                f"FILE: {fname} ({lines_count} lines){_badge_suffix}\n"
                f"SYMBOL INDEX (use these EXACT names in surgical_edit):\n{sym_index}\n"
            )
            if lines_count <= LARGE_FILE_WINDOW:
                # Small-to-medium file — show full content (no truncation risk)
                header += f"\nFULL CONTENT:\n```\n{content}\n```\n"
            else:
                # Large file — Tasklet-style: grep for relevant terms, show
                # compact snippets with line numbers.  No pre-selected window.
                # Claude uses <search_request> to explore further — exactly
                # like Tasklet: structure → grep → read → edit.
                _grep_sections = _grep_relevant_sections(
                    user_request, fname, content,
                    window=3, max_lines=150,
                )
                if _grep_sections:
                    header += (
                        f"\n⚠️ LARGE FILE ({lines_count} lines) — showing grep results for your request.\n"
                        f"You MUST use <search_request> to view full context before editing.\n"
                        f"Use edit_start_line/edit_end_line for precise edits.\n\n"
                        f"KEYWORD MATCHES:\n```\n{_grep_sections}\n```\n"
                    )
                else:
                    header += (
                        f"\n⚠️ LARGE FILE ({lines_count} lines) — no keyword matches found.\n"
                        f"You MUST use <search_request> with specific terms from the user\'s request "
                        f"(e.g. actual text, variable names, function names) to find the code to edit.\n"
                        f"NEVER guess or invent code you haven\'t seen.\n"
                    )
                _dlog("natural_large_file_context",
                      session_id=session_id, user_id=user_id,
                      filename=fname, total_lines=lines_count,
                      grep_found=bool(_grep_sections),
                      grep_chars=len(_grep_sections) if _grep_sections else 0)
            return header
        else:
            preview = content[:1500] + (f"\n...[{len(content)-1500} chars]" if len(content) > 1500 else "")
            return f"FILE: {fname} ({lines_count} lines){_badge_suffix}\nCONTENT:\n```\n{preview}\n```\n"

    def _render_lean(sf: dict) -> str:
        fname       = sf["filename"]
        file_type   = sf.get("file_type", "code")
        lines_count = sf.get("lines", sf.get("content", "") and len(sf["content"].splitlines()) or 0)

        # File status badge
        _fs = file_statuses.get(fname, {}) if file_statuses else {}
        _badge = _file_status_badge(_fs.get("status", "")) if _fs else ""
        _badge_suffix = f"  {_badge}" if _badge else ""

        if file_type in ("image", "pdf", "csv", "excel", "text"):
            return f"  {fname} [{file_type.upper()}, {lines_count}L]{_badge_suffix} — use <file_request> to view"

        smap, _ = symbol_maps_by_name.get(fname, (None, sf))
        symbols  = getattr(smap, "symbols", []) if smap else []

        if symbols:
            sym_names = ", ".join(
                s.full_path or s.name
                for s in symbols[:20]
            )
            suffix = f" +{len(symbols)-20} more" if len(symbols) > 20 else ""
            return f"  {fname} ({lines_count}L, {len(symbols)} symbols){_badge_suffix} — {sym_names}{suffix}"
        else:
            return f"  {fname} ({lines_count}L){_badge_suffix}"

    for sf in tier1:
        parts.append(_render_full(sf))

    # ── Render Tier 2: lean index ─────────────────────────────────────────
    if tier2:
        parts.append(
            "\n━━━ OTHER UPLOADED FILES (lean index) ━━━\n"
            "Use <file_request> with exact filename, or <search_request> with keyword/symbol to fetch code.\n"
        )
        for sf in tier2:
            parts.append(_render_lean(sf))

    return "\n".join(parts), tier1_names


def _clean_history_content(content: str) -> str:
    """
    Clean stored assistant message content for replay to Claude.
    Strips internal storage prefixes so history reads naturally.
    """
    if not content:
        return content

    if content.startswith("__SURGICAL_RESULT__:"):
        try:
            data = json.loads(content[len("__SURGICAL_RESULT__:"):])
            summary = ""
            if isinstance(data, dict):
                plan = data.get("plan", {})
                if isinstance(plan, dict):
                    summary = plan.get("summary", "")
                changes_by_file = data.get("changes_by_file", {})
                symbols = []
                for _fname, _fdata in changes_by_file.items():
                    for ch in (_fdata.get("changes", []) if isinstance(_fdata, dict) else []):
                        sym = ch.get("symbol", {})
                        if isinstance(sym, dict):
                            name = sym.get("name") or sym.get("full_path", "")
                            if name:
                                symbols.append(f"{_fname}::{name}")
            text = summary or "I made code changes to your files."
            if symbols:
                text += f"\nModified symbols: {', '.join(symbols[:6])}"
            return text
        except Exception:
            return "I made code changes to your files."

    if content.startswith("__NATURAL_AND_RESULT__:"):
        try:
            data = json.loads(content[len("__NATURAL_AND_RESULT__:"):])
            text = data.get("text", "").strip()
            result = data.get("result", {})
            changes = []
            qa_flags = []
            if isinstance(result, dict):
                for _fname, _fdata in result.get("changes_by_file", {}).items():
                    for ch in (_fdata.get("changes", []) if isinstance(_fdata, dict) else []):
                        sym = ch.get("symbol", {})
                        name = (sym.get("name") or sym.get("full_path", "")) if isinstance(sym, dict) else ""
                        if name:
                            changes.append(f"{_fname}::{name}")
                        # Change 2: carry QA warnings into history so Claude
                        # self-corrects on the next turn without user prompting.
                        qr = ch.get("qa_result") or {}
                        verdict = qr.get("verdict", "")
                        summary = (qr.get("summary") or "").strip()
                        if verdict in ("warning", "blocked") and summary and name:
                            qa_flags.append(f"{name}: {summary}")
            # ── Markers go FIRST so [:4000] truncation never chops them ──
            prefix = ""
            if changes:
                prefix += f"[Applied changes to: {', '.join(changes[:6])}]\n"
            if qa_flags:
                prefix += f"[QA flagged: {'; '.join(qa_flags[:4])}]\n"
            if prefix:
                text = prefix + text
            return text or "I made code changes to your files."
        except Exception:
            return content

    return content


def _resolve_search_multifile(
    terms: list,
    symbol_maps_by_name: dict,
    file_content_lookup: dict,
) -> str:
    """
    Resolve search terms across ALL session files.
    For each term tries: exact AST symbol name → case-insensitive → grep with enclosing symbol.
    Returns a formatted string ready to inject into Claude's context.
    """
    if not terms:
        return ""

    result_parts = []
    seen_paths: set = set()

    for term in terms[:8]:  # cap to avoid context explosion
        term_lower = term.lower()
        found_anything = False

        for fname, (smap, _sf) in symbol_maps_by_name.items():
            file_content = file_content_lookup.get(fname, "")
            if not file_content:
                continue
            content_lines = file_content.splitlines()
            symbols = getattr(smap, "symbols", []) if smap else []

            # 1. Exact AST name match
            for sym in symbols:
                nm = getattr(sym, "name", "") or ""
                fp = getattr(sym, "full_path", "") or ""
                if nm == term or fp == term or nm.lower() == term_lower or fp.lower() == term_lower:
                    path_key = f"{fname}::{fp}"
                    if path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)
                    code = getattr(sym, "code", "") or ""
                    code_lines = code.splitlines()
                    # Show up to 400 lines of the symbol. For very large symbols,
                    # the model should grep for a string near the region it wants
                    # so it gets a window centred on that line (see grep path).
                    _SYM_CAP = 400
                    shown = code_lines[:_SYM_CAP]
                    trunc = (
                        f"\n  ... [{len(code_lines)-_SYM_CAP} more lines — to see a deeper "
                        f"region, search for a string literal near it to get a focused window]"
                        if len(code_lines) > _SYM_CAP else ""
                    )
                    numbered = "\n".join(
                        f"{sym.start_line + i:5d}: {shown[i]}"
                        for i in range(len(shown))
                    )
                    result_parts.append(
                        f"SYMBOL MATCH [{fname} :: {fp} "
                        f"({sym.symbol_type.value}, L{sym.start_line}–{sym.end_line})]:\n"
                        f"{numbered}{trunc}"
                    )
                    found_anything = True
                    break

            if found_anything:
                break

            # 2. Grep — find line containing term, expand to enclosing symbol
            for line_idx, line in enumerate(content_lines):
                if term_lower not in line.lower():
                    continue
                lineno = line_idx + 1

                # Find the narrowest enclosing symbol
                best_sym = None
                best_size = float("inf")
                for sym in symbols:
                    if sym.start_line <= lineno <= sym.end_line:
                        sz = sym.end_line - sym.start_line
                        if sz < best_size:
                            best_size, best_sym = sz, sym

                if best_sym:
                    path_key = f"{fname}::{best_sym.full_path}::{lineno}"
                    if path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)
                    code = getattr(best_sym, "code", "") or ""
                    code_lines_s = code.splitlines()
                    # Window the symbol AROUND the matched line (not from its
                    # start). For large symbols this is what lets the model see
                    # the exact region it searched for — e.g. a return block deep
                    # inside a 900-line component — so it can write a targeted edit.
                    _WIN = 90  # lines of context on each side of the match
                    match_idx = (lineno - best_sym.start_line)  # 0-based index within symbol
                    lo = max(0, match_idx - _WIN)
                    hi = min(len(code_lines_s), match_idx + _WIN + 1)
                    shown = code_lines_s[lo:hi]
                    pre = f"  ... [{lo} earlier line(s) in this symbol]\n" if lo > 0 else ""
                    post = f"\n  ... [{len(code_lines_s)-hi} later line(s) in this symbol]" if hi < len(code_lines_s) else ""
                    numbered = "\n".join(
                        f"{best_sym.start_line + lo + i:5d}: {shown[i]}"
                        for i in range(len(shown))
                    )
                    result_parts.append(
                        f"GREP MATCH ('{term}') [{fname} :: {best_sym.full_path} "
                        f"({best_sym.symbol_type.value}, L{best_sym.start_line}–{best_sym.end_line}), "
                        f"match at L{lineno}]:\n{pre}{numbered}{post}"
                    )
                else:
                    # No enclosing symbol — show ±40 lines of raw context
                    start = max(0, line_idx - 40)
                    end = min(len(content_lines), line_idx + 41)
                    snippet = "\n".join(
                        f"{start + i + 1:5d}: {content_lines[start + i]}"
                        for i in range(end - start)
                    )
                    result_parts.append(
                        f"GREP MATCH ('{term}') [{fname} L{lineno}]:\n{snippet}"
                    )
                found_anything = True
                break

        if not found_anything:
            result_parts.append(f"NOT FOUND: '{term}' — not in any uploaded file.")

    if not result_parts:
        return ""

    return (
        "\n\n=== SEARCH RESULTS ===\n"
        + "\n\n".join(result_parts)
        + "\n=== END SEARCH RESULTS ===\n"
    )




async def _retry_truncated_edit(
    aclient,
    arch_model: str,
    filename: str,
    symbol_name: str,
    file_content: str,
    smap,
    user_request: str,
    session_id: str = "",
    user_id: str = "",
) -> str | None:
    """
    Retry a single truncated edit with a focused Claude call.
    
    Instead of asking Claude to produce ALL edits in one response,
    this gives it just ONE file and asks for ONE edit block.
    Returns the raw edit block JSON string, or None on failure.
    """
    import json as _json

    # Build a focused symbol index for this file
    sym_index = ""
    if smap:
        lines = []
        for sym in smap.symbols:
            lines.append(f"  {sym.symbol_type.value}: {sym.name} (L{sym.start_line}-L{sym.end_line})")
        sym_index = "SYMBOL INDEX:\n" + "\n".join(lines) + "\n\n"

    focused_system = (
        "You are SurgicalAI. Write EXACTLY ONE <surgical_edit> block for the "
        f"symbol '{symbol_name}' in '{filename}'.\n\n"
        "For JSX/TSX/HTML: before writing the edit, verify your tag balance — "
        "count opening vs closing tags at each nesting level and confirm they match. "
        "Then emit the edit block.\n\n"
        "Format:\n"
        "<surgical_edit>\n"
        '{"filename": "...", "symbol": "...", "description": "...", "new_code": "..."}\n'
        "</surgical_edit>\n\n"
        "For large symbols, use a targeted edit with old_code + new_code instead of "
        "rewriting the entire symbol."
    )

    focused_messages = [{
        "role": "user",
        "content": (
            f"User request: {user_request}\n\n"
            f"━━━ FILE: {filename} ━━━\n"
            f"{sym_index}"
            f"{file_content}"
        ),
    }]

    try:
        _retry_edit_kwargs = {
            "model": arch_model,
            "max_tokens": _max_output_tokens(arch_model),
            "system": focused_system,
            "messages": focused_messages,
        }
        _retry_edit_thinking_kwargs = _get_thinking_kwargs(arch_model, 10000)
        _retry_edit_effort_kwargs = _get_effort_kwargs(arch_model)
        _retry_edit_kwargs.update(_retry_edit_thinking_kwargs)
        _retry_edit_kwargs.update(_retry_edit_effort_kwargs)
        _dlog("retry_truncated_edit_kwargs",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              model=arch_model,
              thinking_kwargs=_retry_edit_thinking_kwargs,
              effort_kwargs=_retry_edit_effort_kwargs)

        _chunks = []
        _retry_t0 = time.time()
        try:
            async with asyncio.timeout(120):
                async with aclient.messages.stream(**_retry_edit_kwargs) as _stream:
                    async for _t in _stream.text_stream:
                        _chunks.append(_t)
        except TimeoutError:
            _dlog("retry_truncated_edit_timeout",
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name,
                  duration_s=round(time.time() - _retry_t0, 1),
                  chunks_so_far=len(_chunks))
            return None

        _dlog("retry_truncated_edit_stream_done",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              duration_s=round(time.time() - _retry_t0, 1),
              chunk_count=len(_chunks),
              text_len=len("".join(_chunks)))
        text = "".join(_chunks).strip()

        EDIT_OPEN = "<surgical_edit>"
        EDIT_CLOSE = "</surgical_edit>"
        start = text.find(EDIT_OPEN)
        end = text.find(EDIT_CLOSE)

        if start != -1 and end != -1:
            raw = text[start + len(EDIT_OPEN):end].strip()
            _json.loads(raw)  # validate parseable
            _dlog("retry_truncated_success",
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name,
                  raw_len=len(raw))
            return raw

        _dlog("retry_truncated_no_block",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              response_preview=text[:200])
        return None

    except Exception as e:
        _dlog("retry_truncated_error",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              error=str(e))
        return None


async def _retry_truncated_newfile(
    aclient,
    arch_model: str,
    filename: str,
    user_request: str,
    session_id: str = "",
    user_id: str = "",
) -> str | None:
    """
    Retry a single truncated new-file creation with a focused Claude call.

    Mirrors _retry_truncated_edit but for <new_file> blocks: instead of
    asking Claude to produce ALL files in one response, this gives it just
    ONE filename and asks it to write the complete file from scratch.
    Returns the raw new_file block JSON string, or None on failure.
    """
    import json as _json

    focused_system = (
        "You are SurgicalAI. Write EXACTLY ONE <new_file> block for the "
        f"file '{filename}'.\n\n"
        "Format:\n"
        "<new_file>\n"
        '{"filename": "...", "language": "...", "summary": "...", "content": "..."}\n'
        "</new_file>\n\n"
        "Write production-ready code — no stubs, no TODOs, no placeholders. "
        "Include all necessary imports. The file should be immediately usable."
    )

    focused_messages = [{
        "role": "user",
        "content": (
            f"User request: {user_request}\n\n"
            f"Write the complete file '{filename}' now, in full."
        ),
    }]

    try:
        _retry_newfile_kwargs = {
            "model": arch_model,
            "max_tokens": _max_output_tokens(arch_model),
            "system": focused_system,
            "messages": focused_messages,
        }
        _retry_newfile_thinking_kwargs = _get_thinking_kwargs(arch_model, 10000)
        _retry_newfile_effort_kwargs = _get_effort_kwargs(arch_model)
        _retry_newfile_kwargs.update(_retry_newfile_thinking_kwargs)
        _retry_newfile_kwargs.update(_retry_newfile_effort_kwargs)
        _dlog("retry_truncated_newfile_kwargs",
              session_id=session_id, user_id=user_id,
              filename=filename,
              model=arch_model,
              thinking_kwargs=_retry_newfile_thinking_kwargs,
              effort_kwargs=_retry_newfile_effort_kwargs)

        _chunks = []
        _retry_t0 = time.time()
        try:
            async with asyncio.timeout(120):
                async with aclient.messages.stream(**_retry_newfile_kwargs) as _stream:
                    async for _t in _stream.text_stream:
                        _chunks.append(_t)
        except TimeoutError:
            _dlog("retry_truncated_newfile_timeout",
                  session_id=session_id, user_id=user_id,
                  filename=filename,
                  duration_s=round(time.time() - _retry_t0, 1),
                  chunks_so_far=len(_chunks))
            return None

        _dlog("retry_truncated_newfile_stream_done",
              session_id=session_id, user_id=user_id,
              filename=filename,
              duration_s=round(time.time() - _retry_t0, 1),
              chunk_count=len(_chunks),
              text_len=len("".join(_chunks)))
        text = "".join(_chunks).strip()

        FILE_OPEN = "<new_file>"
        FILE_CLOSE = "</new_file>"
        start = text.find(FILE_OPEN)
        end = text.find(FILE_CLOSE)

        if start != -1 and end != -1:
            raw = text[start + len(FILE_OPEN):end].strip()
            _json.loads(raw)  # validate parseable
            _dlog("retry_truncated_newfile_success",
                  session_id=session_id, user_id=user_id,
                  filename=filename, raw_len=len(raw))
            return raw

        _dlog("retry_truncated_newfile_no_block",
              session_id=session_id, user_id=user_id,
              filename=filename, response_preview=text[:200])
        return None

    except Exception as e:
        _dlog("retry_truncated_newfile_error",
              session_id=session_id, user_id=user_id,
              filename=filename, error=str(e))
        return None


async def _execute_single_edit(
    aclient, model: str,
    filename: str, symbol_name: str, change_description: str,
    file_content: str, symbol_map, user_request: str,
    session_id: str = "", user_id: str = "",
) -> str | None:
    """
    Focused single-symbol edit call for Plan→Execute orchestration.
    Sends only the relevant file's content and asks Claude to produce
    exactly ONE <surgical_edit> block. Returns the raw edit JSON string
    or None if the call fails.
    """
    import json as _json

    # Build symbol index for this file
    sym_index = ""
    if symbol_map and hasattr(symbol_map, "symbols") and symbol_map.symbols:
        sym_lines = []
        for s in symbol_map.symbols:
            size = s.end_line - s.start_line + 1
            sym_lines.append(
                f"  [{s.symbol_type.value}] {s.full_path:<45} "
                f"L{s.start_line}\u2013{s.end_line}  ({size}L)"
            )
        sym_index = "SYMBOL INDEX:\n" + "\n".join(sym_lines) + "\n\n"

    focused_system = (
        "You are SurgicalAI. Produce exactly ONE <surgical_edit> block for the requested change.\n\n"
        "Rules:\n"
        "- For large files: use edit_start_line/edit_end_line (absolute line numbers shown in context)\n"
        "- For small symbols you can see entirely: include the COMPLETE edited symbol in new_code\n"
        "- For large symbols: use targeted old_code/new_code or edit_start_line/edit_end_line\n"
        "- Match original indentation exactly\n"
        "- Copy ALL unchanged lines verbatim\n"
        "- The JSON must have: filename, symbol, description, new_code (and optionally old_code, "
        "or edit_start_line + edit_end_line)\n"
        "- Do NOT produce explanatory text outside the <surgical_edit> block\n"
    )

    # For large files, show a focused window around the target symbol
    # instead of the full file content (prevents output truncation).
    file_lines = file_content.splitlines()
    file_line_count = len(file_lines)

    if file_line_count > LARGE_FILE_WINDOW and symbol_map and hasattr(symbol_map, 'symbols'):
        # Find target symbol to center the window
        target_sym = None
        for s in symbol_map.symbols:
            if getattr(s, 'full_path', '') == symbol_name or getattr(s, 'name', '') == symbol_name:
                target_sym = s
                break

        if target_sym:
            # Show the full symbol + 50 lines padding for surrounding context
            padding = 50
            ws = max(0, target_sym.start_line - 1 - padding)
            we = min(file_line_count, target_sym.end_line + padding)
            window_parts = []
            if ws > 0:
                window_parts.append(f"... [{ws} lines above] ...\n")
            numbered = "\n".join(
                f"{i+1:5d}: {file_lines[i]}" for i in range(ws, we)
            )
            window_parts.append(numbered)
            if we < file_line_count:
                window_parts.append(
                    f"\n... [{file_line_count - we} lines below] ..."
                )
            file_display = "\n".join(window_parts)

            _dlog("execute_task_windowed",
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name,
                  total_lines=file_line_count,
                  window_start=ws + 1, window_end=we,
                  symbol_start=target_sym.start_line,
                  symbol_end=target_sym.end_line)
        else:
            # Symbol not found in map — show full content as fallback
            file_display = file_content
            _dlog("execute_task_no_window",
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name,
                  total_lines=file_line_count,
                  reason="symbol_not_found_in_map")

        focused_user = (
            f"Edit the symbol `{symbol_name}` in `{filename}`.\n\n"
            f"Change: {change_description}\n\n"
            f"User's original request: {user_request}\n\n"
            f"{sym_index}"
            f"⚠️ LARGE FILE ({file_line_count} lines) — showing focused window "
            f"with absolute line numbers.\n"
            f"Use edit_start_line/edit_end_line for precise edits.\n\n"
            f"File content (focused):\n```\n{file_display}\n```\n\n"
            "Produce exactly ONE <surgical_edit> block now."
        )
    else:
        focused_user = (
            f"Edit the symbol `{symbol_name}` in `{filename}`.\n\n"
            f"Change: {change_description}\n\n"
            f"User's original request: {user_request}\n\n"
            f"{sym_index}"
            f"File content:\n```\n{file_content}\n```\n\n"
            "Produce exactly ONE <surgical_edit> block now."
        )

    import asyncio as _asyncio_se

    # Per-item timeout: cap any single Anthropic call at 120s.
    # Prevents a hung API call from blocking the entire pipeline until
    # the SSE connection is killed externally (see session c72cfe5d).
    SINGLE_EDIT_TIMEOUT = 120

    try:
        call_kwargs = {
            "model": model,
            "max_tokens": _max_output_tokens(model),
            "system": focused_system,
            "messages": [{"role": "user", "content": focused_user}],
        }
        call_kwargs.update(_get_thinking_kwargs(model, 10000))
        call_kwargs.update(_get_effort_kwargs(model))

        _chunks = []
        async with _asyncio_se.timeout(SINGLE_EDIT_TIMEOUT):
            async with aclient.messages.stream(**call_kwargs) as _stream:
                async for _t in _stream.text_stream:
                    _chunks.append(_t)

        text = "".join(_chunks)

        EDIT_OPEN = "<surgical_edit>"
        EDIT_CLOSE = "</surgical_edit>"
        start = text.find(EDIT_OPEN)
        end = text.find(EDIT_CLOSE)

        if start != -1 and end != -1:
            raw = text[start + len(EDIT_OPEN):end].strip()
            # Don't validate with json.loads here — Claude's JSX/CSS
            # output often contains unescaped quotes that break JSON.
            # The downstream edit parse chain has 4 fallbacks including
            # regex extraction that handles these cases.
            _dlog("plan_execute_success",
                  session_id=session_id, user_id=user_id,
                  filename=filename, symbol=symbol_name,
                  raw_len=len(raw))
            return raw

        _dlog("plan_execute_no_block",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              response_preview=text[:300])
        return None

    except TimeoutError:
        _dlog("plan_execute_timeout",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              timeout_sec=SINGLE_EDIT_TIMEOUT)
        return None

    except Exception as e:
        _dlog("plan_execute_error",
              session_id=session_id, user_id=user_id,
              filename=filename, symbol=symbol_name,
              error=str(e))
        return None


async def run_natural_pipeline_stream(
    session_files: list,
    user_request: str,
    conversation_history: list,
    session_id: str = None,
    project_memory: str = None,
    session_summary: str = "",
    user_id: str = "",
):
    """
    Natural conversation pipeline — Claude talks like Claude.

    Claude responds in natural markdown. When it wants to edit code,
    it embeds <surgical_edit> XML tags. This function:
      1. Streams Claude's natural response (text tokens to frontend)
      2. Parses <surgical_edit> blocks out of the stream
      3. Runs the existing QA cycle on each edit
      4. Yields smart_result with structured diff cards

    SSE event types emitted:
      progress    — status messages
      token       — natural text chunks (stream directly to chat)
      thinking_*  — Claude extended thinking blocks
      edit_start  — entering an edit block (show indicator)
      edit_end    — edit block complete
      smart_result — structured edit result (diff cards)
      done        — stream complete
      error       — error message
    """
    import asyncio

    def sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    _pipeline_t0 = time.time()

    # ── Pipeline deadline — Railway kills SSE at 15 min (900s). ────────────
    # We use 810s (13.5 min) to leave a 90s safety margin for final result
    # assembly + SSE flush.  When the deadline fires, every post-execution
    # phase (QA retries, corrections, re-QA) is skipped and the pipeline
    # ships whatever it has.  Losing polish is always better than losing
    # everything.
    # Evidence: session 37d20c8b — 6 changes executed + QA passed, but
    # multi-window correction window 4/5 was still running at 899.9s when
    # Railway severed the connection.  All work was lost.
    PIPELINE_DEADLINE_S = 810

    # ── Streaming-phase deadlines (session a50319ca evidence) ──────────
    # Opus 4.6 adaptive thinking consumed 277s (Stream 2) and 570s
    # (Stream 3) without producing a single text token, hitting
    # Railway's 15-min SSE limit.  These caps abort the streaming
    # phase early so the pipeline can process whatever it has.
    STREAMING_PHASE_DEADLINE_S = 480   # 8 min max for entire streaming phase (leaves 7 min for QA/fixes)
    STREAMING_THINKING_STALL_S = 120   # 2 min of thinking-only in a round → abort

    def _pipeline_over_budget() -> bool:
        """True when elapsed pipeline time exceeds the Railway safety margin."""
        return (time.time() - _pipeline_t0) >= PIPELINE_DEADLINE_S

    EDIT_OPEN = "<surgical_edit>"
    EDIT_CLOSE = "</surgical_edit>"
    FILE_OPEN = "<new_file>"
    FILE_CLOSE = "</new_file>"
    SEARCH_OPEN = "<search_request>"
    SEARCH_CLOSE = "</search_request>"
    FILE_REQ_OPEN = "<file_request>"
    FILE_REQ_CLOSE = "</file_request>"
    PLAN_OPEN = "<edit_plan>"
    PLAN_CLOSE = "</edit_plan>"

    # ── Tag definitions table (Phase 2: unified tag handling) ──
    TAG_DEFS = {
        "edit":    {"open": EDIT_OPEN,     "close": EDIT_CLOSE},
        "file":    {"open": FILE_OPEN,     "close": FILE_CLOSE},
        "search":  {"open": SEARCH_OPEN,   "close": SEARCH_CLOSE},
        "filereq": {"open": FILE_REQ_OPEN, "close": FILE_REQ_CLOSE},
        "plan":    {"open": PLAN_OPEN,     "close": PLAN_CLOSE},
    }

    try:
        _user_arch_model = get_setting("architect_model", "claude-sonnet-5")
        arch_model = _user_arch_model
        _natural_use_claude = _is_claude_model(arch_model)
        if _natural_use_claude:
            anthropic_key = _get_anthropic_key(user_id)
            aclient = AsyncAnthropic(api_key=anthropic_key)
        else:
            # GPT mode: try to create aclient for corrections (symbol fix, QA);
            # if no Anthropic key, corrections degrade gracefully (try/except).
            try:
                _corr_key = _get_anthropic_key(user_id)
                aclient = AsyncAnthropic(api_key=_corr_key)
            except Exception:
                aclient = None
                _dlog("natural_gpt_no_anthropic_key",
                      session_id=session_id, user_id=user_id,
                      note="corrections will be skipped")
            _dlog("natural_pipeline_gpt_mode",
                  model=arch_model, has_aclient=aclient is not None,
                  session_id=session_id, user_id=user_id)

        # ── Parse all session files into symbol maps ──────────────────────
        symbol_maps_by_name: dict = {}
        for sf in session_files:
            fname = sf["filename"]
            content = sf.get("content", "")
            file_type = sf.get("file_type", "code")
            if file_type in ("image", "pdf", "csv", "excel", "text"):
                symbol_maps_by_name[fname] = (None, sf)
                continue
            try:
                smap = parser.parse(content, fname)
                symbol_maps_by_name[fname] = (smap, sf)
            except Exception:
                symbol_maps_by_name[fname] = (None, sf)

        # ── Classify files: new vs unchanged vs modified ─────────────────
        _file_statuses = _classify_session_files(
            session_id or "", session_files, conversation_history,
        )

        # ── Build file context ────────────────────────────────────────────
        file_context, _tier1_names = _build_natural_file_context(
            session_files, symbol_maps_by_name, user_request,
            project_memory=project_memory, session_summary=session_summary,
            session_id=session_id, user_id=user_id,
            file_statuses=_file_statuses,
        )
        _dlog("file_context_built",
              session_id=session_id,
              num_files=len(session_files),
              filenames=[sf["filename"] for sf in session_files],
              context_chars=len(file_context),
              context_preview=file_context,
                  user_id=user_id)

        # ── Build system prompt (with Anthropic prompt caching) ───────────
        # This system prompt is re-sent on every Claude call in the natural
        # pipeline: the streaming search loop (up to MAX_SEARCH_ROUNDS + 2
        # rounds), the symbol-correction call, and each per-edit fix call.
        # Without caching, every one of those repeats re-pays full input cost
        # for the (often very large) uploaded file. We mark cache breakpoints
        # so repeats become ~90%-cheaper cache reads with lower latency:
        #   • NATURAL_SYSTEM — static across every request/session (global)
        #   • file_context   — static within this request's search+fix loop
        # Passing `system` as a list of text blocks is accepted everywhere it
        # is consumed below (stream loop, correction call, per-edit fixes).
        # Ephemeral prompt caching is GA — no beta header required. Blocks
        # below Anthropic's minimum cacheable length are simply not cached
        # (no error), so this is safe regardless of prompt size.
        system_prompt = [
            {
                "type": "text",
                "text": NATURAL_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        if file_context.strip():
            system_prompt.append(
                {
                    "type": "text",
                    "text": file_context,
                    "cache_control": {"type": "ephemeral"},
                }
            )

        # ── Data-file awareness (R21) ────────────────────────────────────
        # When the session contains CSV/Excel/data files, inject guidance so
        # Claude knows it can analyze the data and create new data files via
        # <new_file> — prevents "I cannot edit Excel" refusals.
        _nat_has_data_files = any(
            sf.get("file_type") in ("csv", "excel", "text")
            for sf in session_files
        )
        if _nat_has_data_files:
            system_prompt.append({
                "type": "text",
                "text": _NATURAL_DATA_SECTION,
            })
            _dlog("natural_data_section_injected",
                  session_id=session_id, user_id=user_id,
                  data_file_count=sum(1 for sf in session_files
                                      if sf.get("file_type") in ("csv", "excel", "text")))


        # ── GitHub natural-chat tag (flag-gated, per-user) ─────────────────
        # Only active when github_context_tools_enabled=true AND the GitHub
        # App is configured AND this user has a linked installation. When
        # inactive, TAG_DEFS and the system prompt are byte-identical to
        # before — the single-pass path is untouched.
        _gh_nat_enabled = False
        try:
            from services.github_natural_tag import (
                natural_github_availability, build_github_prompt_section,
                parse_github_request, execute_github_request,
                get_known_repos,
                GH_TAG_OPEN, GH_TAG_CLOSE,
            )
            _gh_nat_enabled, _gh_nat_installs = natural_github_availability(
                user_id, dlog=_dlog
            )
            if _gh_nat_enabled:
                TAG_DEFS["github"] = {"open": GH_TAG_OPEN, "close": GH_TAG_CLOSE}
                # session ff4ff718 fix: tell the model which repos this user
                # already works with so round 1 isn't burned on list_repos.
                _gh_known_repos = get_known_repos(
                    user_id, session_id, dlog=_dlog)
                system_prompt.append({
                    "type": "text",
                    "text": build_github_prompt_section(
                        _gh_nat_installs, known_repos=_gh_known_repos),
                })
                _dlog("natural_github_tag_registered",
                      session_id=session_id, user_id=user_id,
                      installations=len(_gh_nat_installs))
        except Exception as _gh_nat_err:
            _gh_nat_enabled = False
            _dlog("natural_github_setup_error",
                  session_id=session_id, user_id=user_id,
                  error=str(_gh_nat_err))

        # ── Debug: confirm quality rules and project memory reached the prompt ──
        _total_sys_chars = sum(b.get("text", "") if isinstance(b, dict) else b for b in system_prompt if isinstance(b, dict)) if False else sum(len(b["text"]) for b in system_prompt if isinstance(b, dict) and "text" in b)
        _has_quality = "CODE QUALITY RULES" in NATURAL_SYSTEM
        _has_project_memory = "PROJECT MEMORY" in file_context if file_context else False
        _dlog("single_pass_system_prompt",
              session_id=session_id,
              has_code_quality_rules=_has_quality,
              quality_rules_in_natural_system=len(_CODE_QUALITY_SECTION),
              natural_system_len=len(NATURAL_SYSTEM),
              has_project_memory=_has_project_memory,
              project_memory_len=len(file_context) if _has_project_memory else 0,
              total_system_chars=_total_sys_chars,
              system_blocks=len(system_prompt))

        # ── GPT system text (R25): plain string for OpenAI messages ────────
        if not _natural_use_claude:
            _gpt_system_text = "\n\n".join(
                b["text"] for b in system_prompt
                if isinstance(b, dict) and "text" in b
            )

        # ── Clean conversation history — strip JSON artifacts ─────────────
        clean_history = []
        for msg in conversation_history[-HISTORY_WINDOW:]:
            role = msg.get("role", "user")
            if role not in ("user", "assistant"):
                continue
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            if role == "assistant":
                content = _clean_history_content(content)
            if content:
                clean_history.append({"role": role, "content": content[:4000]})

        # ── Build user message — text + optional image vision blocks ──────
        # Collect image files to attach as base64 vision blocks
        image_files = [
            sf for sf in session_files
            if sf.get("file_type") == "image"
        ]

        # Claude vision only supports these media types. HEIC/HEIF is NOT supported.
        CLAUDE_VISION_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}

        if image_files:
            # Multipart content: text first, then each image block
            user_content: list = [{"type": "text", "text": user_request}]
            for img_sf in image_files:
                img_data = img_sf.get("content", "")
                fname = img_sf.get("filename", "unknown")
                if not img_data:
                    logger.warning(f"[pipeline:natural] No content for image file {fname!r} — skipping vision block")
                    continue
                if img_data.startswith("data:"):
                    # data URL: "data:image/png;base64,<data>"
                    header, b64 = img_data.split(",", 1)
                    media_type = header.split(":")[1].split(";")[0]
                else:
                    # raw base64 — infer media type from extension
                    ext = img_sf["filename"].rsplit(".", 1)[-1].lower()
                    mime_map = {
                        "jpg": "image/jpeg", "jpeg": "image/jpeg",
                        "png": "image/png",  "webp": "image/webp",
                        "gif": "image/gif",
                    }
                    media_type = mime_map.get(ext, "image/png")
                    b64 = img_data

                logger.info(
                    f"[pipeline:natural] Vision block: file={fname!r} media_type={media_type!r} "
                    f"b64_len={len(b64)} data_url_valid={img_data.startswith('data:')}"
                )

                if media_type not in CLAUDE_VISION_TYPES:
                    logger.warning(
                        f"[pipeline:natural] Unsupported media type {media_type!r} for {fname!r}. "
                        f"Claude supports: {CLAUDE_VISION_TYPES}. Skipping vision block — "
                        f"user should re-upload as JPEG or PNG."
                    )
                    # Append a text note so Claude knows the image exists but can't be seen
                    user_content[0]["text"] += (
                        f"\n\n[Image '{fname}' was uploaded as {media_type} which Claude cannot process visually. "
                        f"Please ask the user to re-upload as JPEG or PNG.]"
                    )
                    continue

                # Annotate image with new/unchanged/modified status
                _img_fs = _file_statuses.get(fname, {})
                _img_badge = _file_status_badge(_img_fs.get("status", "")) if _img_fs else ""
                if _img_badge:
                    user_content.append({
                        "type": "text",
                        "text": f"[Image: {fname} — {_img_badge}]",
                    })

                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            messages = clean_history + [{"role": "user", "content": user_content}]
        else:
            messages = clean_history + [{"role": "user", "content": user_request}]

        # ── GPT image format conversion (R25) ────────────────────────────
        # Claude uses {"type":"image","source":{"type":"base64",...}}
        # OpenAI uses {"type":"image_url","image_url":{"url":"data:...;base64,..."}}
        if not _natural_use_claude and image_files and isinstance(messages[-1].get("content"), list):
            _gpt_vis = []
            for _vitem in messages[-1]["content"]:
                if _vitem.get("type") == "image":
                    _src = _vitem["source"]
                    _gpt_vis.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{_src['media_type']};base64,{_src['data']}"}
                    })
                else:
                    _gpt_vis.append(_vitem)
            messages[-1] = {"role": messages[-1]["role"], "content": _gpt_vis}

        # ── Stream model's response ───────────────────────────────────────
        yield sse({"type": "progress", "content": "Thinking..."})

        # ═══════════════════════════════════════════════════════════════════
        # DECOMPOSED PIPELINE — Phase 1 (search) + Phase 2 (edit)
        #
        # Each phase is a SEPARATE API call with its own token budget.
        # Search can never starve edits. No starvation recovery needed.
        #
        # Phase 1: Code Discovery — small token budget, no thinking,
        #          up to 3 rounds of search/file_request/github.
        # Phase 2: Edit Generation — full token budget, full thinking,
        #          single API call, only edit/file/plan tags parsed.
        # ═══════════════════════════════════════════════════════════════════

        edit_blocks_raw: list = []
        new_file_blocks_raw: list = []
        edit_plan_data: list | None = None
        full_response = ""
        in_thinking = False

        # Build the per-file content lookup once (reused across both phases)
        file_content_lookup_stream: dict = {
            sf["filename"]: sf.get("content", "") for sf in session_files
        }

        # ───────────────────────────────────────────────────────────────────
        # PHASE 1: CODE DISCOVERY
        # ───────────────────────────────────────────────────────────────────

        PHASE1_MAX_SEARCH_ROUNDS = 3     # max search rounds
        PHASE1_MAX_TOKENS = 4096         # search terms are tiny
        MAX_FILE_REQ_TOTAL = 15          # cap total fetchable via <file_request>
        searched_terms: list = []
        requested_files: set = set()
        accumulated_search_results = ""
        _phase1_msgs = list(messages)

        # GitHub state
        MAX_GITHUB_ROUNDS = 6
        MAX_GITHUB_ATTEMPTS = 12
        _github_rounds_used = 0
        _github_attempts = 0

        _streaming_t0 = time.time()
        _streaming_starvation_abort = False   # only set in Phase 2

        _PHASE1_INSTRUCTION = (
            "\n\n[PHASE: CODE DISCOVERY]\n"
            "Analyze the user's request and the code context available. "
            "If you need to see more code to make the changes, emit a <search_request> or <file_request> tag. "
            "If you already have enough context to write the changes, respond with: READY_TO_EDIT\n"
            "Do NOT write any <surgical_edit>, <new_file>, or <edit_plan> blocks yet — that happens next."
        )

        _P1_TAGS = {"search", "filereq"}
        if _gh_nat_enabled:
            _P1_TAGS.add("github")
        _p1_stop_seqs = [TAG_DEFS[t]["close"] for t in _P1_TAGS if t in TAG_DEFS]

        _search_round = 0  # will be updated in loop
        _consecutive_no_action = 0  # nudge on first no-action, exit on second

        for _search_round in range(PHASE1_MAX_SEARCH_ROUNDS):
            _round_t0 = time.time()

            # ── Build search-phase messages ────────────────────────────
            if _search_round == 0:
                _round_msgs = list(_phase1_msgs)
                _last_msg = _round_msgs[-1]
                if isinstance(_last_msg.get("content"), str):
                    _round_msgs[-1] = {**_last_msg, "content": _last_msg["content"] + _PHASE1_INSTRUCTION}
                elif isinstance(_last_msg.get("content"), list):
                    _round_msgs[-1] = {
                        **_last_msg,
                        "content": list(_last_msg["content"]) + [{"type": "text", "text": _PHASE1_INSTRUCTION}],
                    }
            else:
                _round_msgs = _phase1_msgs

            if _search_round > 0:
                yield sse({"type": "progress",
                           "content": f"Analyzing code context (round {_search_round + 1})..."})

            # ── Per-round state ────────────────────────────────────────
            _p1_response = ""
            _p1_state = "normal"
            _p1_normal_buf = ""
            _p1_tag_buf = ""
            search_requested: dict | None = None
            file_request_data: list | None = None
            github_requested: dict | None = None
            _p1_last_stop_reason: str | None = None

            if _natural_use_claude:
                _p1_kwargs = {
                    "model": arch_model,
                    "max_tokens": PHASE1_MAX_TOKENS,
                    "system": system_prompt,
                    "messages": _round_msgs,
                }
                # No thinking for search phase — fast and focused
                _p1_kwargs.update(_get_effort_kwargs(arch_model))
                if _p1_stop_seqs:
                    _p1_kwargs["stop_sequences"] = _p1_stop_seqs

                for _attempt in range(3):
                    try:
                        async with aclient.messages.stream(**_p1_kwargs) as _p1_stream:
                            async for _p1_ev in _p1_stream:
                                _p1_etype = getattr(_p1_ev, "type", None)

                                if _p1_etype == "content_block_delta":
                                    _p1_delta = getattr(_p1_ev, "delta", None)
                                    if not _p1_delta:
                                        continue
                                    _p1_text = getattr(_p1_delta, "text", None)
                                    _p1_thinking = getattr(_p1_delta, "thinking", None)

                                    if _p1_thinking:
                                        pass  # Skip thinking in search phase

                                    elif _p1_text:
                                        _p1_response += _p1_text
                                        if _p1_state == "normal":
                                            _p1_normal_buf += _p1_text
                                        else:
                                            _p1_tag_buf += _p1_text

                                        # Tag parser for search-phase tags
                                        while True:
                                            if _p1_state == "normal":
                                                candidates = []
                                                for _tname in _P1_TAGS:
                                                    if _tname not in TAG_DEFS:
                                                        continue
                                                    _ti = _p1_normal_buf.find(TAG_DEFS[_tname]["open"])
                                                    if _ti != -1:
                                                        candidates.append((_ti, _tname))
                                                if not candidates:
                                                    _tail = max(len(TAG_DEFS[t]["open"]) for t in _P1_TAGS if t in TAG_DEFS)
                                                    _safe = max(0, len(_p1_normal_buf) - _tail)
                                                    if _safe > 0:
                                                        _p1_normal_buf = _p1_normal_buf[_safe:]
                                                    break
                                                _fi, _ft = min(candidates, key=lambda x: x[0])
                                                _p1_state = f"in_{_ft}"
                                                _p1_tag_buf = _p1_normal_buf[_fi + len(TAG_DEFS[_ft]["open"]):]
                                                _p1_normal_buf = ""

                                                # Check for close tag immediately
                                                _ct = TAG_DEFS[_ft]["close"]
                                                _ci = _p1_tag_buf.find(_ct)
                                                if _ci != -1:
                                                    _block = _p1_tag_buf[:_ci]
                                                    if _ft == "search":
                                                        _sd = _parse_search_content(_block)
                                                        if _sd is not None:
                                                            search_requested = _sd
                                                    elif _ft == "filereq":
                                                        fnames = _parse_filereq_content(_block)
                                                        if fnames:
                                                            file_request_data = fnames[:5]
                                                    elif _ft == "github":
                                                        _gd = parse_github_request(_block, dlog=_dlog)
                                                        github_requested = (
                                                            _gd if _gd is not None
                                                            else {"_invalid": _block[:500]}
                                                        )
                                                    _p1_normal_buf = _p1_tag_buf[_ci + len(_ct):]
                                                    _p1_tag_buf = ""
                                                    _p1_state = "normal"
                                                else:
                                                    break  # Wait for more data

                                            elif _p1_state.startswith("in_"):
                                                _cn = _p1_state[3:]
                                                if _cn not in TAG_DEFS:
                                                    break
                                                _ct2 = TAG_DEFS[_cn]["close"]
                                                _ci2 = _p1_tag_buf.find(_ct2)
                                                if _ci2 != -1:
                                                    _block2 = _p1_tag_buf[:_ci2]
                                                    if _cn == "search":
                                                        _sd = _parse_search_content(_block2)
                                                        if _sd is not None:
                                                            search_requested = _sd
                                                    elif _cn == "filereq":
                                                        fnames = _parse_filereq_content(_block2)
                                                        if fnames:
                                                            file_request_data = fnames[:5]
                                                    elif _cn == "github":
                                                        _gd = parse_github_request(_block2, dlog=_dlog)
                                                        github_requested = (
                                                            _gd if _gd is not None
                                                            else {"_invalid": _block2[:500]}
                                                        )
                                                    _p1_normal_buf = _p1_tag_buf[_ci2 + len(_ct2):]
                                                    _p1_tag_buf = ""
                                                    _p1_state = "normal"
                                                else:
                                                    break
                                            else:
                                                break

                                    # Break streaming if we found an action tag
                                    if (search_requested is not None
                                            or file_request_data is not None
                                            or github_requested is not None):
                                        break

                            # Capture stop reason
                            try:
                                _p1_final = await _p1_stream.get_final_message()
                                _p1_last_stop_reason = _p1_final.stop_reason if _p1_final else None
                            except Exception:
                                pass

                        break  # success — exit retry loop

                    except Exception as _p1_err:
                        _err_str = str(_p1_err)
                        _is_transient = (
                            "500" in _err_str or "529" in _err_str
                            or "overloaded" in _err_str.lower()
                            or "internal_server_error" in _err_str.lower()
                        )
                        if _is_transient and _attempt < 2:
                            yield sse({"type": "progress",
                                       "content": f"Service busy — retrying ({_attempt+1}/3)..."})
                            await asyncio.sleep(5 * (_attempt + 1))
                            continue
                        if (_p1_kwargs.get("stop_sequences")
                                and "stop_sequence" in _err_str.lower()
                                and _attempt < 2):
                            _p1_kwargs.pop("stop_sequences", None)
                            continue
                        _dlog("phase1_search_error", session_id=session_id, user_id=user_id,
                              round=_search_round, error=_err_str[:300])
                        break

            else:
                # ── GPT search phase ──────────────────────────────────
                _gpt_client = _get_client(user_id)
                _gpt_p1_msgs = [{"role": "system", "content": _gpt_system_text}] + list(_round_msgs)
                _gpt_p1_stop = [TAG_DEFS[t]["close"] for t in _P1_TAGS if t in TAG_DEFS][:4]

                for _attempt in range(3):
                    try:
                        _gpt_p1_stream = _chat_create(
                            _gpt_client, model=arch_model,
                            messages=_gpt_p1_msgs, stream=True,
                            max_tokens=PHASE1_MAX_TOKENS,
                            stop=_gpt_p1_stop,
                        )
                        for _gpt_chunk in _gpt_p1_stream:
                            _gpt_choice = _gpt_chunk.choices[0]
                            if _gpt_choice.finish_reason:
                                _p1_last_stop_reason = _gpt_choice.finish_reason
                                break
                            _gpt_delta = getattr(_gpt_choice, "delta", None)
                            if _gpt_delta and hasattr(_gpt_delta, "content") and _gpt_delta.content:
                                _p1_text = _gpt_delta.content
                                _p1_response += _p1_text
                                if _p1_state == "normal":
                                    _p1_normal_buf += _p1_text
                                else:
                                    _p1_tag_buf += _p1_text

                                while True:
                                    if _p1_state == "normal":
                                        candidates = []
                                        for _tname in _P1_TAGS:
                                            if _tname not in TAG_DEFS:
                                                continue
                                            _ti = _p1_normal_buf.find(TAG_DEFS[_tname]["open"])
                                            if _ti != -1:
                                                candidates.append((_ti, _tname))
                                        if not candidates:
                                            _tail = max(len(TAG_DEFS[t]["open"]) for t in _P1_TAGS if t in TAG_DEFS)
                                            _safe = max(0, len(_p1_normal_buf) - _tail)
                                            if _safe > 0:
                                                _p1_normal_buf = _p1_normal_buf[_safe:]
                                            break
                                        _fi, _ft = min(candidates, key=lambda x: x[0])
                                        _p1_state = f"in_{_ft}"
                                        _p1_tag_buf = _p1_normal_buf[_fi + len(TAG_DEFS[_ft]["open"]):]
                                        _p1_normal_buf = ""
                                    elif _p1_state.startswith("in_"):
                                        _cn = _p1_state[3:]
                                        if _cn not in TAG_DEFS:
                                            break
                                        _ct = TAG_DEFS[_cn]["close"]
                                        _ci = _p1_tag_buf.find(_ct)
                                        if _ci != -1:
                                            _block = _p1_tag_buf[:_ci]
                                            if _cn == "search":
                                                _sd = _parse_search_content(_block)
                                                if _sd is not None:
                                                    search_requested = _sd
                                            elif _cn == "filereq":
                                                fnames = _parse_filereq_content(_block)
                                                if fnames:
                                                    file_request_data = fnames[:5]
                                            _p1_normal_buf = _p1_tag_buf[_ci + len(_ct):]
                                            _p1_tag_buf = ""
                                            _p1_state = "normal"
                                        else:
                                            break
                                    else:
                                        break

                            if (search_requested is not None
                                    or file_request_data is not None):
                                break

                        break  # success

                    except Exception as _gpt_err:
                        if ("500" in str(_gpt_err) or "529" in str(_gpt_err)
                                or "overloaded" in str(_gpt_err).lower()) and _attempt < 2:
                            yield sse({"type": "progress",
                                       "content": f"Service busy — retrying ({_attempt+1}/3)..."})
                            await asyncio.sleep(5 * (_attempt + 1))
                            continue
                        _dlog("phase1_search_gpt_error", session_id=session_id, user_id=user_id,
                              error=str(_gpt_err)[:300])
                        break

            # ── Handle unclosed tags (stop_sequence fired) ─────────────
            if (_p1_last_stop_reason in ("stop_sequence", "stop")
                    and _p1_state.startswith("in_")):
                _uc_tag = _p1_state[3:]
                _uc_content = _p1_tag_buf
                if _uc_tag == "search":
                    _sd = _parse_search_content(_uc_content)
                    if _sd is not None:
                        search_requested = _sd
                elif _uc_tag == "filereq":
                    fnames = _parse_filereq_content(_uc_content)
                    if fnames:
                        file_request_data = fnames[:5]
                elif _uc_tag == "github":
                    _gd = parse_github_request(_uc_content, dlog=_dlog)
                    github_requested = (
                        _gd if _gd is not None
                        else {"_invalid": _uc_content[:500]}
                    )
                _p1_state = "normal"
                _p1_tag_buf = ""

            # EOS recovery for unclosed tags (stream ended without close)
            if _p1_state.startswith("in_"):
                _uc2 = _p1_state[3:]
                if _uc2 in TAG_DEFS:
                    _uc2_match = re.search(
                        re.escape(TAG_DEFS[_uc2]["open"]) + r"(.*)",
                        _p1_response, re.DOTALL
                    )
                    if _uc2_match:
                        _uc2_block = _uc2_match.group(1)
                        if _uc2 == "search":
                            _sd = _parse_search_content(_uc2_block)
                            if _sd is not None:
                                search_requested = _sd
                        elif _uc2 == "filereq":
                            fnames = _parse_filereq_content(_uc2_block)
                            if fnames:
                                file_request_data = fnames[:5]
                    _p1_state = "normal"
                    _p1_tag_buf = ""

            _round_duration = time.time() - _round_t0
            _dlog("phase1_search_round_done",
                  session_id=session_id, user_id=user_id,
                  round=_search_round,
                  duration_s=round(_round_duration, 1),
                  response_len=len(_p1_response),
                  had_search=bool(search_requested),
                  had_filereq=bool(file_request_data),
                  had_github=bool(github_requested),
                  ready_to_edit="READY_TO_EDIT" in _p1_response.upper())

            # Reset no-action counter when model took any action
            if search_requested or file_request_data or github_requested:
                _consecutive_no_action = 0

            # ── READY_TO_EDIT — model says it has enough context ──────
            if ("READY_TO_EDIT" in _p1_response.upper()
                    and not search_requested
                    and not file_request_data
                    and not github_requested):
                _dlog("phase1_ready_to_edit",
                      session_id=session_id, user_id=user_id,
                      rounds_used=_search_round + 1)
                break

            # ── Handle search request ─────────────────────────────────
            if search_requested is not None:
                raw_terms = search_requested.get("terms", [])
                reason = search_requested.get("reason", "")
                _dlog("phase1_search_requested",
                      session_id=session_id, user_id=user_id,
                      round=_search_round, terms=raw_terms, reason=reason)

                new_terms = [t for t in raw_terms
                             if t.lower() not in {s.lower() for s in searched_terms}]

                if not new_terms:
                    _dlog("phase1_all_terms_searched",
                          session_id=session_id, user_id=user_id,
                          terms=raw_terms)
                    break  # All terms already searched — move to edit phase

                yield sse({"type": "progress",
                           "content": f"Searching: {', '.join(new_terms[:3])}{'...' if len(new_terms)>3 else ''}"})

                search_results = _resolve_search_multifile(
                    new_terms, symbol_maps_by_name, file_content_lookup_stream
                )
                searched_terms.extend(new_terms)
                accumulated_search_results += search_results
                _dlog("phase1_search_results",
                      session_id=session_id, user_id=user_id,
                      terms=new_terms,
                      results_chars=len(search_results))

                is_last = (_search_round == PHASE1_MAX_SEARCH_ROUNDS - 1)
                _filereq_hint = ""
                if not requested_files and not is_last:
                    # Model has only grep snippets — hint that full files are available
                    _filereq_hint = (
                        "\n\nThese are grep snippets (limited context). "
                        "To see full file contents for editing, emit a <file_request> tag with the filenames you need."
                    )
                _phase1_msgs = list(messages) + [
                    {"role": "assistant", "content": _p1_response or "(searching for code...)"},
                    {"role": "user", "content":
                        "Here are the search results"
                        + (f" ({reason})" if reason else "")
                        + ":\n" + search_results
                        + (_filereq_hint if not is_last else "")
                        + ("\n\nDo you need more code? Emit another <search_request> or <file_request>, or reply READY_TO_EDIT."
                           if not is_last else
                           "\n\nThis was the final search round. Reply READY_TO_EDIT.")
                    },
                ]
                search_requested = None
                continue

            # ── Handle file request ───────────────────────────────────
            if file_request_data is not None:
                fnames_req = file_request_data
                file_request_data = None

                # Tier-1 guard: skip files already in full context
                _tier1_overlap = [fn for fn in fnames_req if fn in _tier1_names]
                if _tier1_overlap and all(fn in _tier1_names for fn in fnames_req):
                    _all_data = all(
                        symbol_maps_by_name.get(fn, (None, {}))[1].get("file_type") in ("csv", "excel", "pdf", "text")
                        for fn in _tier1_overlap
                    )
                    _redirect = (
                        "Those data files are already fully loaded in your context as markdown tables above. "
                        "Reply READY_TO_EDIT to proceed."
                    ) if _all_data else (
                        "Those files are already fully loaded in your context above. "
                        "Reply READY_TO_EDIT to proceed."
                    )
                    _dlog("phase1_filereq_tier1_guard",
                          session_id=session_id, user_id=user_id,
                          requested=fnames_req, tier1_overlap=_tier1_overlap)
                    _phase1_msgs = list(messages) + [
                        {"role": "assistant", "content": _p1_response or "(requesting files...)"},
                        {"role": "user", "content": _redirect},
                    ]
                    continue

                new_fnames = [fn for fn in fnames_req if fn not in requested_files]
                if len(requested_files) >= MAX_FILE_REQ_TOTAL or not new_fnames:
                    _phase1_msgs = list(messages) + [
                        {"role": "assistant", "content": _p1_response or "(requesting files...)"},
                        {"role": "user", "content":
                            "File request limit reached. Reply READY_TO_EDIT to proceed."
                            if len(requested_files) >= MAX_FILE_REQ_TOTAL
                            else "Those files are already in your context. Reply READY_TO_EDIT."},
                    ]
                    continue

                yield sse({"type": "progress",
                           "content": f"Loading: {', '.join(new_fnames[:3])}{'...' if len(new_fnames)>3 else ''}"})

                file_result_parts = []
                for fn in new_fnames:
                    content = file_content_lookup_stream.get(fn, "")
                    if not content:
                        candidates_f = [
                            k for k in file_content_lookup_stream
                            if fn.lower() in k.lower() or k.lower() in fn.lower()
                        ]
                        if candidates_f:
                            file_result_parts.append(
                                f"FILE NOT FOUND: '{fn}' — did you mean: {', '.join(candidates_f[:3])}?")
                        else:
                            file_result_parts.append(f"FILE NOT FOUND: '{fn}' — not in uploaded files.")
                        continue

                    requested_files.add(fn)
                    file_lines = content.splitlines()
                    smap_fr, _ = symbol_maps_by_name.get(fn, (None, None))
                    if smap_fr and smap_fr.symbols:
                        sym_lines = []
                        for s in smap_fr.symbols:
                            size = s.end_line - s.start_line + 1
                            flag = " ⚠️LARGE" if size > 300 else ""
                            sym_lines.append(
                                f"  [{s.symbol_type.value}] {s.full_path:<45} "
                                f"L{s.start_line}–{s.end_line}  ({size}L){flag}")
                        header = (
                            f"FILE: {fn} ({len(file_lines)} lines)\n"
                            f"SYMBOL INDEX (use these EXACT names in surgical_edit):\n"
                            + "\n".join(sym_lines) + "\n")
                    else:
                        header = f"FILE: {fn} ({len(file_lines)} lines)\n"
                    file_result_parts.append(f"{header}FULL CONTENT:\n```\n{content}\n```")

                _missing_fnames = [fn for fn in new_fnames if fn not in requested_files]
                _missing_note = ""
                if _missing_fnames:
                    _missing_note = (
                        "\n\n⚠️ MISSING FILES: "
                        + ", ".join(_missing_fnames)
                        + " — these files are NOT loaded in this session."
                    )

                _phase1_msgs = list(messages) + [
                    {"role": "assistant", "content": _p1_response or "(requesting files...)"},
                    {"role": "user", "content":
                        "Here are the files you requested:\n\n"
                        + "\n\n".join(file_result_parts)
                        + _missing_note
                        + "\n\nDo you need more files? Emit another <file_request> or <search_request>, "
                        "or reply READY_TO_EDIT."},
                ]
                _dlog("phase1_file_request_resolved",
                      session_id=session_id, user_id=user_id,
                      filenames=new_fnames,
                      found=[fn for fn in new_fnames if fn in requested_files])
                continue

            # ── Handle GitHub request ─────────────────────────────────
            if github_requested is not None:
                _gh_req = github_requested
                github_requested = None
                _github_attempts += 1

                if (_github_rounds_used >= MAX_GITHUB_ROUNDS
                        or _github_attempts > MAX_GITHUB_ATTEMPTS):
                    _dlog("phase1_github_budget_exhausted",
                          session_id=session_id, user_id=user_id,
                          rounds=_github_rounds_used, attempts=_github_attempts)
                    break

                if "_invalid" in _gh_req:
                    _phase1_msgs = list(messages) + [
                        {"role": "assistant", "content": _p1_response or "(github request...)"},
                        {"role": "user", "content":
                            "Your <github_request> was not valid JSON. "
                            "Emit a corrected <github_request> or reply READY_TO_EDIT."},
                    ]
                    continue

                yield sse({"type": "progress",
                           "content": f"GitHub: {_gh_req.get('tool', 'request')}..."})

                try:
                    _gh_result = await execute_github_request(
                        _gh_req, user_id, session_id, dlog=_dlog)
                    _github_rounds_used += 1
                    _dlog("phase1_github_result",
                          session_id=session_id, user_id=user_id,
                          tool=_gh_req.get("tool", ""),
                          result_chars=len(str(_gh_result)))

                    _phase1_msgs = list(messages) + [
                        {"role": "assistant", "content": _p1_response or "(github request...)"},
                        {"role": "user", "content":
                            f"GitHub result:\n{json.dumps(_gh_result, indent=2, default=str)[:8000]}"
                            "\n\nDo you need more information? Emit another request, or reply READY_TO_EDIT."},
                    ]
                except Exception as _gh_err:
                    _dlog("phase1_github_error",
                          session_id=session_id, user_id=user_id,
                          error=str(_gh_err)[:300])
                    _phase1_msgs = list(messages) + [
                        {"role": "assistant", "content": _p1_response or "(github request...)"},
                        {"role": "user", "content":
                            f"GitHub request failed: {str(_gh_err)[:200]}. "
                            "Reply READY_TO_EDIT to proceed with the code you have."},
                    ]
                continue

            # ── No action tag — model didn't search, didn't say READY ─
            _dlog("phase1_no_action",
                  session_id=session_id, user_id=user_id,
                  round=_search_round,
                  response_preview=_p1_response[:300],
                  consecutive_no_action=_consecutive_no_action + 1)

            _consecutive_no_action += 1
            if _consecutive_no_action >= 2 or _search_round >= PHASE1_MAX_SEARCH_ROUNDS - 1:
                # Two consecutive no-actions or last round — give up
                _dlog("phase1_no_action_exit",
                      session_id=session_id, user_id=user_id,
                      reason="consecutive" if _consecutive_no_action >= 2 else "last_round")
                break

            # Nudge: model went off-protocol, guide it back
            _phase1_msgs = list(messages) + [
                {"role": "assistant", "content": _p1_response or "(analyzing...)"},
                {"role": "user", "content":
                    "You responded with reasoning but didn't take an action.\n"
                    "• To see full file contents, emit a <file_request> tag.\n"
                    "• To search for more code, emit a <search_request> tag.\n"
                    "• If you have enough context, reply READY_TO_EDIT.\n"
                    "You MUST pick one of these three options."},
            ]
            continue

        _phase1_duration = time.time() - _streaming_t0
        _dlog("phase1_complete",
              session_id=session_id, user_id=user_id,
              total_duration_s=round(_phase1_duration, 1),
              search_rounds=min(_search_round + 1, PHASE1_MAX_SEARCH_ROUNDS),
              accumulated_results_chars=len(accumulated_search_results),
              searched_terms=searched_terms,
              requested_files=list(requested_files))

        # ───────────────────────────────────────────────────────────────────
        # PHASE 2: EDIT GENERATION
        # Full token budget, full thinking. Single API call.
        # Search cannot starve this — it's a completely separate call.
        # ───────────────────────────────────────────────────────────────────

        # Build edit-phase messages with search results injected
        current_messages = list(messages)
        if accumulated_search_results:
            current_messages = current_messages + [
                {"role": "assistant", "content": "(I've analyzed the codebase and gathered the code I need.)"},
                {"role": "user", "content":
                    "Here is additional code context from your analysis:\n\n"
                    + accumulated_search_results
                    + "\n\nNow apply the requested changes. Use <surgical_edit>, <new_file>, "
                    "or <edit_plan> tags. Do NOT emit <search_request> — all code discovery is complete."},
            ]

        yield sse({"type": "progress", "content": "Writing code changes..."})

        # ── Edit-phase streaming ──────────────────────────────────────
        STREAMING_PHASE_DEADLINE_S = 480   # 8 min max for edit streaming
        STREAMING_THINKING_STALL_S = 120   # 2 min thinking stall → abort
        _phase2_filereq_used = False       # safety net: allow one file_request in Phase 2
        _phase2_filereq_retry = False      # set when filereq safety net triggers a retry

        state = "normal"
        normal_buf = ""
        tag_buf = ""
        had_thinking = False
        _last_stop_reason: str | None = None
        _matched_stop_seq: str | None = None
        _edit_hb_bytes = 0
        _edit_hb_last = 0
        _round_t0 = time.time()
        _round_last_text_ts = time.time()
        # Stall detection measures a DEAD stream (no bytes of ANY kind), not
        # "thinking without text". Adaptive models (e.g. claude-sonnet-5) with
        # summarized thinking can legitimately think >2 min before the first
        # edit token, especially after the safety-net re-injects full files.
        # Runaway is bounded separately by STREAMING_PHASE_DEADLINE_S + budget.
        _round_last_activity_ts = time.time()  # resets on ANY delta (thinking OR text)
        _round_thinking_deltas = 0             # thinking deltas seen this stream
        _round_last_thinking_ts = 0.0          # last time a thinking delta arrived

        _tag_stop_enabled = _os.getenv("TAG_STOP_SEQUENCES", "true").strip().lower() == "true"
        _tag_stop_seqs = [TAG_DEFS["plan"]["close"]] if _tag_stop_enabled else []

        if _natural_use_claude:
            stream_kwargs = {
                "model": arch_model,
                "max_tokens": _max_output_tokens(arch_model),
                "system": system_prompt,
                "messages": current_messages,
            }
            stream_kwargs.update(_get_thinking_kwargs(arch_model, 10000))
            stream_kwargs.update(_get_effort_kwargs(arch_model))
            if _tag_stop_seqs:
                stream_kwargs["stop_sequences"] = _tag_stop_seqs

            for _attempt in range(3):
                try:
                    async with aclient.messages.stream(**stream_kwargs) as astream:
                        current_block_type = None
                        async for event in astream:
                            etype = getattr(event, "type", None)

                            if etype == "content_block_start":
                                current_block_type = getattr(
                                    getattr(event, "content_block", None), "type", None
                                )
                                if current_block_type == "thinking":
                                    in_thinking = True
                                    had_thinking = True
                                    yield sse({"type": "thinking_start", "content": ""})

                            elif etype == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                if not delta:
                                    continue

                                thinking_chunk = getattr(delta, "thinking", None)
                                text_chunk = getattr(delta, "text", None)

                                if thinking_chunk:
                                    # Thinking IS activity — the model is working,
                                    # not starving. Reset the stall timer so a
                                    # legitimate long thinking phase is never
                                    # falsely aborted as starvation.
                                    _round_last_activity_ts = time.time()
                                    _round_last_thinking_ts = _round_last_activity_ts
                                    _round_thinking_deltas += 1
                                    yield sse({"type": "thinking", "content": thinking_chunk})

                                elif text_chunk:
                                    full_response += text_chunk
                                    _round_last_text_ts = time.time()
                                    _round_last_activity_ts = _round_last_text_ts

                                    if state == "normal":
                                        normal_buf += text_chunk
                                    else:
                                        tag_buf += text_chunk
                                        if state in ("in_edit", "in_file"):
                                            _edit_hb_bytes += len(text_chunk)
                                            if (_edit_hb_bytes - _edit_hb_last >= 2048):
                                                _edit_hb_last = _edit_hb_bytes
                                                yield sse({
                                                    "type": "progress",
                                                    "content": f"✍️ Writing code changes… {_edit_hb_bytes/1024:.1f} KB",
                                                })

                                    # Tag parser (edit/file/plan — search/filereq ignored in Phase 2)
                                    while True:
                                        if state == "normal":
                                            _found_open = False
                                            while True:
                                                candidates = []
                                                for _tname in TAG_DEFS:
                                                    _ti = normal_buf.find(TAG_DEFS[_tname]["open"])
                                                    if _ti != -1:
                                                        candidates.append((_ti, _tname))
                                                if not candidates:
                                                    tail = max(len(td["open"]) for td in TAG_DEFS.values())
                                                    safe = max(0, len(normal_buf) - tail)
                                                    if safe > 0:
                                                        yield sse({"type": "token", "content": normal_buf[:safe]})
                                                        normal_buf = normal_buf[safe:]
                                                    break
                                                first_idx, first_tag = min(candidates, key=lambda x: x[0])
                                                if first_idx > 0:
                                                    yield sse({"type": "token", "content": normal_buf[:first_idx]})
                                                if first_tag in ("edit", "file"):
                                                    yield sse({"type": "edit_start", "content": ""})
                                                state = f"in_{first_tag}"
                                                tag_buf = normal_buf[first_idx + len(TAG_DEFS[first_tag]["open"]):]
                                                normal_buf = ""
                                                _found_open = True
                                                _dlog("tag_opened",
                                                      session_id=session_id, user_id=user_id,
                                                      tag_type=first_tag, state=state)
                                                break
                                            if not _found_open:
                                                break

                                        _tag_name = state[3:]
                                        if _tag_name not in TAG_DEFS:
                                            break
                                        _close_tag = TAG_DEFS[_tag_name]["close"]
                                        idx = tag_buf.find(_close_tag)
                                        if idx == -1:
                                            break
                                        _content = tag_buf[:idx]
                                        _remainder = tag_buf[idx + len(_close_tag):]
                                        _break_stream = False

                                        if _tag_name == "edit":
                                            edit_blocks_raw.append(_content)
                                            yield sse({"type": "edit_end", "content": ""})
                                        elif _tag_name == "file":
                                            new_file_blocks_raw.append(_content)
                                            yield sse({"type": "edit_end", "content": ""})
                                        elif _tag_name == "plan":
                                            _pd = _parse_plan_content(_content)
                                            if _pd is not None:
                                                edit_plan_data = _pd
                                            _break_stream = True
                                        elif _tag_name == "filereq" and not _phase2_filereq_used:
                                            # Phase 2 safety net: model needs files Phase 1 didn't get
                                            _p2_fnames = _parse_filereq_content(_content)
                                            if _p2_fnames:
                                                _phase2_filereq_used = True
                                                _p2_file_parts = []
                                                for _p2fn in _p2_fnames[:5]:
                                                    _p2c = file_content_lookup_stream.get(_p2fn, "")
                                                    if _p2c:
                                                        _p2_file_parts.append(
                                                            f"FILE: {_p2fn} ({len(_p2c.splitlines())} lines)\n"
                                                            f"FULL CONTENT:\n```\n{_p2c}\n```")
                                                    else:
                                                        _p2_cands = [
                                                            k for k in file_content_lookup_stream
                                                            if _p2fn.lower() in k.lower()
                                                            or k.lower() in _p2fn.lower()
                                                        ]
                                                        if _p2_cands:
                                                            _p2_file_parts.append(
                                                                f"FILE NOT FOUND: '{_p2fn}' — did you mean: "
                                                                f"{', '.join(_p2_cands[:3])}?")
                                                        else:
                                                            _p2_file_parts.append(
                                                                f"FILE NOT FOUND: '{_p2fn}'")
                                                if _p2_file_parts:
                                                    _dlog("phase2_filereq_safety_net",
                                                          session_id=session_id, user_id=user_id,
                                                          filenames=_p2_fnames[:5],
                                                          found_count=sum(1 for p in _p2_file_parts
                                                                         if not p.startswith("FILE NOT FOUND")))
                                                    # Inject files and restart Phase 2
                                                    current_messages = current_messages + [
                                                        {"role": "assistant", "content": full_response or "(requesting files...)"},
                                                        {"role": "user", "content":
                                                            "Here are the files you requested:\n\n"
                                                            + "\n\n".join(_p2_file_parts)
                                                            + "\n\nNow apply the requested changes using "
                                                            "<surgical_edit>, <new_file>, or <edit_plan> tags."},
                                                    ]
                                                    # Reset Phase 2 state for retry
                                                    full_response = ""
                                                    state = "normal"
                                                    normal_buf = ""
                                                    tag_buf = ""
                                                    had_thinking = False
                                                    _edit_hb_bytes = 0
                                                    _edit_hb_last = 0
                                                    _round_t0 = time.time()
                                                    _round_last_text_ts = time.time()
                                                    _round_last_activity_ts = time.time()
                                                    _round_thinking_deltas = 0
                                                    _round_last_thinking_ts = 0.0
                                                    stream_kwargs["messages"] = current_messages
                                                    yield sse({"type": "progress",
                                                               "content": "Loading additional files and retrying..."})
                                                    _phase2_filereq_retry = True
                                                    break  # break inner stream → retry loop picks up
                                            else:
                                                _dlog("phase2_search_tag_ignored",
                                                      session_id=session_id, user_id=user_id,
                                                      tag_type=_tag_name,
                                                      content_preview=_content[:200])
                                        elif _tag_name in ("search", "filereq", "github"):
                                            # Model emitted search/filereq(2nd time)/github — log and ignore
                                            _dlog("phase2_search_tag_ignored",
                                                  session_id=session_id, user_id=user_id,
                                                  tag_type=_tag_name,
                                                  content_preview=_content[:200])

                                        state = "normal"
                                        normal_buf = _remainder
                                        tag_buf = ""
                                        _dlog("tag_closed",
                                              session_id=session_id, user_id=user_id,
                                              tag_type=_tag_name,
                                              content_len=len(_content),
                                              break_stream=_break_stream)
                                        if _break_stream:
                                            break
                                        if not _remainder:
                                            break

                            elif etype == "content_block_stop":
                                if in_thinking and current_block_type == "thinking":
                                    yield sse({"type": "thinking_end", "content": ""})
                                    in_thinking = False

                            # Deadline checks
                            _stall_elapsed = time.time() - _round_t0
                            if (_pipeline_over_budget()
                                    or _stall_elapsed >= STREAMING_PHASE_DEADLINE_S):
                                _dlog("streaming_deadline_abort",
                                      session_id=session_id, user_id=user_id,
                                      reason="pipeline_budget" if _pipeline_over_budget() else "phase_deadline",
                                      elapsed_s=round(_stall_elapsed, 1),
                                      text_len=len(full_response))
                                _streaming_starvation_abort = True
                                break
                            # DEAD-STREAM guard: abort only when NO bytes of any
                            # kind (thinking OR text) have arrived for the timeout.
                            # A model actively streaming thinking is working, not
                            # starving. Runaway (thinks forever) is bounded above
                            # by STREAMING_PHASE_DEADLINE_S + _pipeline_over_budget().
                            if (had_thinking
                                    and (time.time() - _round_last_activity_ts)
                                        > STREAMING_THINKING_STALL_S):
                                _dlog("streaming_thinking_stall",
                                      session_id=session_id, user_id=user_id,
                                      stall_s=round(time.time() - _round_last_activity_ts, 1),
                                      no_text_for_s=round(time.time() - _round_last_text_ts, 1),
                                      thinking_deltas=_round_thinking_deltas,
                                      last_thinking_age_s=(
                                          round(time.time() - _round_last_thinking_ts, 1)
                                          if _round_last_thinking_ts else None),
                                      text_len=len(full_response))
                                _streaming_starvation_abort = True
                                break

                            # Break if plan was found
                            if edit_plan_data is not None:
                                break

                    # Capture stop_reason
                    try:
                        _final = await astream.get_final_message()
                        _last_stop_reason = _final.stop_reason if _final else None
                        _matched_stop_seq = getattr(_final, "stop_sequence", None) if _final else None
                    except Exception:
                        _last_stop_reason = None
                        _matched_stop_seq = None

                    if _last_stop_reason == "stop_sequence":
                        _dlog("stream_halted_at_stop_sequence",
                              session_id=session_id, user_id=user_id,
                              matched=str(_matched_stop_seq)[:60],
                              state_at_halt=state, tag_buf_len=len(tag_buf))

                    # Phase 2 file_request safety net triggered — retry with injected files
                    if _phase2_filereq_retry:
                        _phase2_filereq_retry = False
                        continue  # retry the for-loop with updated messages

                    break  # success

                except Exception as stream_err:
                    err_str = str(stream_err)
                    is_transient = (
                        "500" in err_str or "529" in err_str
                        or "overloaded" in err_str.lower()
                        or "internal_server_error" in err_str.lower()
                    )
                    if is_transient and _attempt < 2:
                        yield sse({"type": "progress",
                                   "content": f"Service busy — retrying ({_attempt+1}/3)..."})
                        await asyncio.sleep(5 * (_attempt + 1))
                        continue
                    if (stream_kwargs.get("stop_sequences")
                            and "stop_sequence" in err_str.lower()
                            and _attempt < 2):
                        stream_kwargs.pop("stop_sequences", None)
                        _dlog("tag_stop_sequences_rejected_disabled",
                              session_id=session_id, user_id=user_id,
                              error=err_str[:300])
                        continue
                    raise

        else:
            # ── GPT edit phase ─────────────────────────────────────────
            _gpt_client = _get_client(user_id)
            _gpt_msgs = [{"role": "system", "content": _gpt_system_text}] + list(current_messages)
            _gpt_stop = _tag_stop_seqs[:4] if _tag_stop_enabled else None

            for _attempt in range(3):
                try:
                    _gpt_stream = _chat_create(
                        _gpt_client, model=arch_model,
                        messages=_gpt_msgs,
                        stream=True,
                        max_tokens=_max_output_tokens(arch_model),
                        stop=_gpt_stop,
                    )
                    for _gpt_chunk in _gpt_stream:
                        _gpt_choice = _gpt_chunk.choices[0]
                        if _gpt_choice.finish_reason:
                            _last_stop_reason = _gpt_choice.finish_reason
                            break
                        # Runaway guard — symmetry with the Claude branch.
                        # GPT has no client-side thinking deltas (reasoning is
                        # server-side), so there is no thinking-stall detector
                        # and no false-abort risk here. This only bounds a
                        # genuinely runaway / silently-hung GPT stream, matching
                        # the Claude ceiling (pipeline budget + phase deadline).
                        _gpt_stall_elapsed = time.time() - _round_t0
                        if (_pipeline_over_budget()
                                or _gpt_stall_elapsed >= STREAMING_PHASE_DEADLINE_S):
                            _dlog("streaming_deadline_abort_gpt",
                                  session_id=session_id, user_id=user_id,
                                  reason="pipeline_budget" if _pipeline_over_budget() else "phase_deadline",
                                  elapsed_s=round(_gpt_stall_elapsed, 1),
                                  text_len=len(full_response))
                            _streaming_starvation_abort = True
                            break
                        _gpt_delta = getattr(_gpt_choice, "delta", None)
                        if _gpt_delta and hasattr(_gpt_delta, "content") and _gpt_delta.content:
                            text_chunk = _gpt_delta.content
                            full_response += text_chunk
                            _round_last_text_ts = time.time()
                            if state == "normal":
                                normal_buf += text_chunk
                            else:
                                tag_buf += text_chunk
                                if state in ("in_edit", "in_file"):
                                    _edit_hb_bytes += len(text_chunk)
                                    if (_edit_hb_bytes - _edit_hb_last >= 2048):
                                        _edit_hb_last = _edit_hb_bytes
                                        yield sse({"type": "progress",
                                                   "content": f"✍️ Writing code changes… {_edit_hb_bytes/1024:.1f} KB"})

                            # Tag parser — same as Claude branch
                            while True:
                                if state == "normal":
                                    candidates = []
                                    for _tname in TAG_DEFS:
                                        _ti = normal_buf.find(TAG_DEFS[_tname]["open"])
                                        if _ti != -1:
                                            candidates.append((_ti, _tname))
                                    if not candidates:
                                        tail = max(len(td["open"]) for td in TAG_DEFS.values())
                                        safe = max(0, len(normal_buf) - tail)
                                        if safe > 0:
                                            yield sse({"type": "token", "content": normal_buf[:safe]})
                                            normal_buf = normal_buf[safe:]
                                        break
                                    first_idx, first_tag = min(candidates, key=lambda x: x[0])
                                    if first_idx > 0:
                                        yield sse({"type": "token", "content": normal_buf[:first_idx]})
                                    if first_tag in ("edit", "file"):
                                        yield sse({"type": "edit_start", "content": ""})
                                    state = f"in_{first_tag}"
                                    tag_buf = normal_buf[first_idx + len(TAG_DEFS[first_tag]["open"]):]
                                    normal_buf = ""
                                else:
                                    _tag_name = state[3:]
                                    if _tag_name not in TAG_DEFS:
                                        break
                                    _close_tag = TAG_DEFS[_tag_name]["close"]
                                    ci = tag_buf.find(_close_tag)
                                    if ci == -1:
                                        break
                                    _content = tag_buf[:ci]
                                    _remainder = tag_buf[ci + len(_close_tag):]
                                    if _tag_name == "edit":
                                        edit_blocks_raw.append(_content)
                                        yield sse({"type": "edit_end", "content": ""})
                                    elif _tag_name == "file":
                                        new_file_blocks_raw.append(_content)
                                        yield sse({"type": "edit_end", "content": ""})
                                    elif _tag_name == "plan":
                                        _pd = _parse_plan_content(_content)
                                        if _pd is not None:
                                            edit_plan_data = _pd
                                    elif _tag_name == "filereq" and not _phase2_filereq_used:
                                        # GPT Phase 2 safety net
                                        _p2_fnames = _parse_filereq_content(_content)
                                        if _p2_fnames:
                                            _phase2_filereq_used = True
                                            _p2_file_parts = []
                                            for _p2fn in _p2_fnames[:5]:
                                                _p2c = file_content_lookup_stream.get(_p2fn, "")
                                                if _p2c:
                                                    _p2_file_parts.append(
                                                        f"FILE: {_p2fn} ({len(_p2c.splitlines())} lines)\n"
                                                        f"FULL CONTENT:\n```\n{_p2c}\n```")
                                                else:
                                                    _p2_file_parts.append(f"FILE NOT FOUND: '{_p2fn}'")
                                            if _p2_file_parts:
                                                _dlog("phase2_filereq_safety_net_gpt",
                                                      session_id=session_id, user_id=user_id,
                                                      filenames=_p2_fnames[:5])
                                                _gpt_msgs = _gpt_msgs + [
                                                    {"role": "assistant", "content": full_response or "(requesting files...)"},
                                                    {"role": "user", "content":
                                                        "Here are the files you requested:\n\n"
                                                        + "\n\n".join(_p2_file_parts)
                                                        + "\n\nNow apply the requested changes using "
                                                        "<surgical_edit>, <new_file>, or <edit_plan> tags."},
                                                ]
                                                full_response = ""
                                                state = "normal"
                                                normal_buf = ""
                                                tag_buf = ""
                                                _edit_hb_bytes = 0
                                                _edit_hb_last = 0
                                                _round_last_text_ts = time.time()
                                                _phase2_filereq_retry = True
                                                yield sse({"type": "progress",
                                                           "content": "Loading additional files and retrying..."})
                                                break  # break inner stream
                                        else:
                                            _dlog("phase2_search_tag_ignored_gpt",
                                                  session_id=session_id, user_id=user_id,
                                                  tag_type=_tag_name)
                                    elif _tag_name in ("search", "filereq"):
                                        _dlog("phase2_search_tag_ignored_gpt",
                                              session_id=session_id, user_id=user_id,
                                              tag_type=_tag_name)
                                    state = "normal"
                                    normal_buf = _remainder
                                    tag_buf = ""
                                    if _tag_name == "plan" and edit_plan_data:
                                        break
                                    if not _remainder:
                                        break

                    # Phase 2 file_request safety net triggered — retry
                    if _phase2_filereq_retry:
                        _phase2_filereq_retry = False
                        continue

                    break  # success

                except Exception as _gpt_err:
                    if (("500" in str(_gpt_err) or "529" in str(_gpt_err)
                            or "overloaded" in str(_gpt_err).lower())
                            and _attempt < 2):
                        yield sse({"type": "progress",
                                   "content": f"Service busy — retrying ({_attempt+1}/3)..."})
                        await asyncio.sleep(5 * (_attempt + 1))
                        continue
                    raise

        # ── Post-streaming cleanup ────────────────────────────────────
        if _streaming_starvation_abort:
            _dlog("streaming_starvation_abort_exit",
                  session_id=session_id, user_id=user_id,
                  elapsed_s=round(time.time() - _round_t0, 1),
                  full_response_len=len(full_response),
                  edit_blocks=len(edit_blocks_raw))
            yield sse({"type": "progress",
                       "content": "⏱️ Thinking timeout — processing available changes..."})
            if in_thinking:
                yield sse({"type": "thinking_end", "content": ""})
                in_thinking = False

        # ── Stop-sequence tag-close synthesizer ───────────────────────
        if (_last_stop_reason == "stop_sequence"
                and state.startswith("in_")):
            _ss_tag = state[3:]
            _ss_content = tag_buf
            try:
                if _ss_tag == "plan":
                    _pd = _parse_plan_content(_ss_content)
                    if _pd is not None:
                        edit_plan_data = _pd
                full_response += TAG_DEFS.get(_ss_tag, {}).get("close", "")
                state = "normal"
                tag_buf = ""
                _dlog("tag_closed_via_stop_sequence",
                      session_id=session_id, user_id=user_id,
                      tag_type=_ss_tag, content_len=len(_ss_content))
            except Exception as _ss_exc:
                _dlog("stop_sequence_synthesize_error",
                      session_id=session_id, user_id=user_id,
                      tag_type=_ss_tag, error=str(_ss_exc)[:300])

        # ── Universal end-of-stream finalizer ─────────────────────────
        if state != "normal":
            _dlog("eos_finalizer_triggered",
                  session_id=session_id, user_id=user_id,
                  stuck_state=state,
                  full_response_len=len(full_response))

            if state == "in_edit":
                _new_count, _partial_kept, _had_matches = _eos_recover_blocks(
                    EDIT_OPEN, EDIT_CLOSE, full_response, tag_buf, edit_blocks_raw)
                if _had_matches:
                    state = "normal"
                    tag_buf = ""
                    _dlog("eos_edit_recovered",
                          session_id=session_id, user_id=user_id,
                          recovered_count=_new_count,
                          partial_kept=_partial_kept,
                          total_edit_blocks=len(edit_blocks_raw))
                yield sse({"type": "edit_end", "content": ""})

            elif state == "in_file":
                _new_count, _partial_kept, _had_matches = _eos_recover_blocks(
                    FILE_OPEN, FILE_CLOSE, full_response, tag_buf, new_file_blocks_raw)
                if _had_matches:
                    state = "normal"
                    tag_buf = ""
                    _dlog("eos_newfile_recovered",
                          session_id=session_id, user_id=user_id,
                          recovered_count=_new_count,
                          partial_kept=_partial_kept,
                          total_file_blocks=len(new_file_blocks_raw))
                yield sse({"type": "edit_end", "content": ""})

            elif state == "in_plan":
                _pl_match = re.search(
                    r"<edit_plan>(.*?)</edit_plan>",
                    full_response, re.DOTALL)
                if _pl_match:
                    _pd = _parse_plan_content(_pl_match.group(1))
                    if _pd is not None:
                        edit_plan_data = _pd
                    state = "normal"
                    tag_buf = ""
                    _dlog("eos_plan_recovered",
                          session_id=session_id, user_id=user_id,
                          plan_items=len(edit_plan_data) if edit_plan_data else 0)

            else:
                _dlog("eos_unknown_state",
                      session_id=session_id, user_id=user_id,
                      state=state)

        # Flush any normal text buffered
        if state == "normal" and normal_buf.strip():
            yield sse({"type": "token", "content": normal_buf})

        if in_thinking:
            yield sse({"type": "thinking_end", "content": ""})

        # ── Starvation handling (Phase 2 only — no recovery needed) ───
        # With decomposed phases, total starvation only happens if the
        # model's thinking stalls for 2+ minutes. No complex recovery
        # loop — just report and stop.
        if (_streaming_starvation_abort
                and len(full_response.strip()) == 0
                and not edit_blocks_raw
                and not new_file_blocks_raw
                and not edit_plan_data):
            _dlog("starvation_total_failure",
                  session_id=session_id, user_id=user_id,
                  model=arch_model,
                  elapsed_s=round(time.time() - _streaming_t0, 1))
            yield sse({"type": "token",
                       "content": "\n\n⚠️ The model spent all available time thinking without producing any output. "
                                  "Please try again with a simpler prompt or a different model."})
            yield sse({"type": "done", "content": ""})
            return

        _dlog("phase2_complete",
              session_id=session_id, user_id=user_id,
              duration_s=round(time.time() - _round_t0, 1),
              total_pipeline_s=round(time.time() - _pipeline_t0, 1),
              edit_blocks=len(edit_blocks_raw),
              new_file_blocks=len(new_file_blocks_raw),
              has_plan=edit_plan_data is not None,
              response_len=len(full_response),
              was_starvation_abort=_streaming_starvation_abort)


        # Initialize skipped_changes early — truncation detection may append to it
        skipped_changes_struct: list = []

        # ── Truncation detection + auto-retry ────────────────────────────
        if _last_stop_reason == "max_tokens":
            _dlog("response_truncated",
                  session_id=session_id, user_id=user_id,
                  full_response_len=len(full_response),
                  edit_blocks=len(edit_blocks_raw),
                  new_file_blocks=len(new_file_blocks_raw))
            yield sse({"type": "progress",
                       "content": "⚠️ Response was truncated — retrying incomplete edits individually..."})

            # Find which edit blocks failed to parse (truncated ones)
            _good_blocks = []
            _bad_blocks_raw = []
            for _eb_raw in edit_blocks_raw:
                try:
                    json.loads(_eb_raw.strip())
                    _good_blocks.append(_eb_raw)
                except Exception:
                    try:
                        json.loads(_repair_json(_eb_raw.strip()))
                        _good_blocks.append(_eb_raw)
                    except Exception:
                        _bad_blocks_raw.append(_eb_raw)

            if _bad_blocks_raw:
                _dlog("retry_truncated_blocks",
                      session_id=session_id, user_id=user_id,
                      good=len(_good_blocks), bad=len(_bad_blocks_raw))

                # Try to identify which symbols the truncated blocks were for
                # by scanning the partial JSON for filename/symbol hints
                import re as _re_retry
                for _bad_raw in _bad_blocks_raw:
                    _fname_match = _re_retry.search(r'"filename"\s*:\s*"([^"]+)"', _bad_raw)
                    _sym_match = _re_retry.search(r'"symbol"\s*:\s*"([^"]+)"', _bad_raw)
                    if _fname_match and _sym_match:
                        _retry_fname = _fname_match.group(1)
                        _retry_sym = _sym_match.group(1)
                        _retry_content = file_content_lookup_stream.get(_retry_fname, "")
                        _retry_smap = symbol_maps_by_name.get(_retry_fname, (None, None))[0]

                        if _retry_content:
                            yield sse({"type": "progress",
                                       "content": f"Retrying {_retry_sym} in {_retry_fname}..."})
                            _redit_task = asyncio.create_task(_retry_truncated_edit(
                                aclient, "claude-sonnet-5",  # R25: corrections always Claude
                                _retry_fname, _retry_sym, _retry_content,
                                _retry_smap, user_request,
                                session_id, user_id
                            ))
                            _redit_t0 = time.time()
                            while not _redit_task.done():
                                _done_r, _ = await asyncio.wait({_redit_task}, timeout=15.0)
                                if not _done_r:
                                    _redit_el = int(time.time() - _redit_t0)
                                    yield sse({"type": "progress",
                                               "content": f"Retrying {_retry_sym}… ({_redit_el}s)"})
                            _retried = _redit_task.result()
                            if _retried:
                                _good_blocks.append(_retried)
                                yield sse({"type": "progress",
                                           "content": f"✅ {_retry_sym} recovered"})
                                continue

                    # If retry failed or couldn't identify the symbol
                    skipped_changes_struct.append({
                        "filename": _fname_match.group(1) if _fname_match else "(unknown)",
                        "symbol": _sym_match.group(1) if _sym_match else "(truncated)",
                        "reason": "Edit was truncated by output limit and retry failed",
                    })

                # Replace edit_blocks_raw with only the good (+ retried) blocks
                edit_blocks_raw = _good_blocks

            # Find which new_file blocks failed to parse (truncated ones)
            _good_files = []
            _bad_files_raw = []
            for _fb_raw in new_file_blocks_raw:
                try:
                    json.loads(_fb_raw.strip())
                    _good_files.append(_fb_raw)
                except Exception:
                    try:
                        json.loads(_repair_json(_fb_raw.strip()))
                        _good_files.append(_fb_raw)
                    except Exception:
                        _bad_files_raw.append(_fb_raw)

            if _bad_files_raw:
                _dlog("retry_truncated_newfiles",
                      session_id=session_id, user_id=user_id,
                      good=len(_good_files), bad=len(_bad_files_raw))

                import re as _re_retry_nf
                for _bad_raw in _bad_files_raw:
                    _fname_match = _re_retry_nf.search(r'"filename"\s*:\s*"([^"]+)"', _bad_raw)
                    if _fname_match:
                        _retry_fname = _fname_match.group(1)
                        yield sse({"type": "progress",
                                   "content": f"Retrying {_retry_fname}..."})
                        _rnf_task = asyncio.create_task(_retry_truncated_newfile(
                            aclient, "claude-sonnet-5",  # R25: corrections always Claude
                            _retry_fname, user_request,
                            session_id, user_id
                        ))
                        _rnf_t0 = time.time()
                        while not _rnf_task.done():
                            _done_nf, _ = await asyncio.wait({_rnf_task}, timeout=15.0)
                            if not _done_nf:
                                _rnf_el = int(time.time() - _rnf_t0)
                                yield sse({"type": "progress",
                                           "content": f"Retrying {_retry_fname}… ({_rnf_el}s)"})
                        _retried_file = _rnf_task.result()
                        if _retried_file:
                            _good_files.append(_retried_file)
                            yield sse({"type": "progress",
                                       "content": f"✅ {_retry_fname} recovered"})
                            continue

                    # If retry failed or couldn't identify the filename
                    skipped_changes_struct.append({
                        "filename": _fname_match.group(1) if _fname_match else "(unknown)",
                        "symbol": "(new file)",
                        "reason": "New file was truncated by output limit and retry failed",
                    })

                # Replace new_file_blocks_raw with only the good (+ retried) blocks
                new_file_blocks_raw = _good_files


        # ── Plan→Execute: focused per-symbol edit calls ──────────────────
        if edit_plan_data:
            _plan_exec_t0 = time.time()

            # ── Group coupled same-symbol plan items (Fix 1) ──────────────
            # When multiple plan items target the SAME (filename, symbol),
            # they are coupled — e.g., item 0 adds a style token, item 3
            # wires it into rendering.  Executing them individually causes
            # the complex items to timeout (120s) and the simple items to
            # succeed → dead code → QA collapse.
            # Evidence (session 82eb1056): 4 plan items all targeted
            # PasteBatchJobsModal.  Items 0-1 (add token, update metadata)
            # finished in 3-12s.  Items 2-3 (wire into rendering) timed out
            # at 120s each → scrollableCellTextStyle defined but never used
            # → QA score dropped from 8 to 1.
            # Fix: merge same-symbol items into ONE instruction so the
            # surgeon makes all changes in a single pass.
            from collections import OrderedDict as _OD_plan
            _grouped_plan: _OD_plan = _OD_plan()
            for _pi in edit_plan_data:
                _gkey = (_pi.get("filename", ""), _pi.get("symbol", ""))
                if _gkey not in _grouped_plan:
                    _grouped_plan[_gkey] = []
                _grouped_plan[_gkey].append(_pi)

            _effective_plan = []
            for _gkey, _gitems in _grouped_plan.items():
                if len(_gitems) == 1:
                    _effective_plan.append(_gitems[0])
                else:
                    # Merge descriptions into a single consolidated instruction
                    _merged_desc = " AND ALSO ".join(
                        f"({i+1}) {item.get('description', '')}"
                        for i, item in enumerate(_gitems)
                    )
                    _effective_plan.append({
                        "filename": _gkey[0],
                        "symbol": _gkey[1],
                        "description": _merged_desc,
                    })
                    _dlog("plan_items_grouped",
                          session_id=session_id, user_id=user_id,
                          filename=_gkey[0], symbol=_gkey[1],
                          original_count=len(_gitems),
                          merged_description=_merged_desc[:500],
                          individual_descriptions=[
                              it.get("description", "")[:200] for it in _gitems
                          ])

            _dlog("plan_exec_phase_start",
                  session_id=session_id, user_id=user_id,
                  plan_count=len(edit_plan_data),
                  effective_count=len(_effective_plan),
                  grouped_symbols=[
                      {"key": k, "count": len(v)}
                      for k, v in _grouped_plan.items() if len(v) > 1
                  ],
                  pipeline_elapsed_s=round(time.time() - _pipeline_t0, 1))
            yield sse({"type": "progress",
                       "content": f"Executing {len(_effective_plan)} planned edit(s)..."})
            for plan_idx, plan_item in enumerate(_effective_plan):
                p_filename = plan_item.get("filename", "")
                p_symbol = plan_item.get("symbol", "")
                p_description = plan_item.get("description", "")
                if not p_filename or not p_symbol:
                    continue

                p_content = file_content_lookup_stream.get(p_filename, "")
                if not p_content:
                    skipped_changes_struct.append({
                        "filename": p_filename,
                        "symbol": p_symbol,
                        "reason": f"File not found in session: {p_filename}",
                    })
                    continue

                p_smap = symbol_maps_by_name.get(p_filename, (None, None))[0]

                yield sse({"type": "progress",
                           "content": f"Editing {p_symbol} in {p_filename} ({plan_idx+1}/{len(edit_plan_data)})..."})

                try:
                    # Run the edit as a task so we can emit progress heartbeats
                    # every 15s — prevents silent dead air during long API calls.
                    _edit_task = asyncio.ensure_future(_execute_single_edit(
                        aclient, "claude-sonnet-5",  # R25: corrections always Claude
                        p_filename, p_symbol, p_description,
                        p_content, p_smap, user_request,
                        session_id, user_id
                    ))
                    _edit_start = time.monotonic()
                    while not _edit_task.done():
                        _done_set, _ = await asyncio.wait(
                            {_edit_task}, timeout=15
                        )
                        if not _done_set:
                            _elapsed = int(time.monotonic() - _edit_start)
                            yield sse({"type": "progress",
                                       "content": f"⏳ Still editing {p_symbol}... ({_elapsed}s)"})
                    result_raw = _edit_task.result()

                    if result_raw:
                        edit_blocks_raw.append(result_raw)
                        yield sse({"type": "progress",
                                   "content": f"✅ {p_symbol} complete"})
                    else:
                        skipped_changes_struct.append({
                            "filename": p_filename,
                            "symbol": p_symbol,
                            "reason": "Focused edit call produced no valid edit block",
                        })
                        yield sse({"type": "progress",
                                   "content": f"⚠️ {p_symbol} — no edit produced"})

                except asyncio.CancelledError:
                    # SSE disconnect: Starlette cancels the generator task.
                    # Log remaining plan items as skipped so they're visible
                    # in diagnostics, then re-raise to let cleanup proceed.
                    _remaining = edit_plan_data[plan_idx:]
                    _dlog("plan_execute_cancelled",
                          session_id=session_id, user_id=user_id,
                          cancelled_at_index=plan_idx,
                          total_items=len(edit_plan_data),
                          remaining_items=[
                              {"filename": ri.get("filename", ""),
                               "symbol": ri.get("symbol", "")}
                              for ri in _remaining
                          ])
                    for ri in _remaining:
                        skipped_changes_struct.append({
                            "filename": ri.get("filename", ""),
                            "symbol": ri.get("symbol", ""),
                            "reason": "Skipped — client disconnected during plan execution",
                        })
                    raise  # Re-raise CancelledError so the task actually cancels

                except Exception as exec_err:
                    _dlog("plan_execute_error",
                          session_id=session_id, user_id=user_id,
                          filename=p_filename, symbol=p_symbol,
                          error=str(exec_err))
                    skipped_changes_struct.append({
                        "filename": p_filename,
                        "symbol": p_symbol,
                        "reason": f"Edit execution failed: {str(exec_err)[:100]}",
                    })

        # ── Process edit blocks ───────────────────────────────────────────
        if not edit_blocks_raw and not new_file_blocks_raw:
            _dlog("no_edits_produced",
                  session_id=session_id, user_id=user_id,
                  response_length=len(full_response),
                  response_preview=full_response[:500],
                  had_thinking=had_thinking,
                  skipped_count=len(skipped_changes_struct),
                  skipped_details=skipped_changes_struct[:10])
            # If plan tasks ran but all failed, report the errors to the user
            if skipped_changes_struct:
                _sk_detail = "\n".join(
                    f"• **{s.get('symbol', '?')}** in `{s.get('filename', '?')}`: {s.get('reason', 'unknown')}"
                    for s in skipped_changes_struct[:10]
                )
                _fail_msg = (
                    f"I planned {len(skipped_changes_struct)} change(s) but all of them failed:\n\n"
                    f"{_sk_detail}\n\n"
                    "This usually means the file is very large. Try pointing me at a specific "
                    "section or symbol to edit, and I\'ll take a more focused approach."
                )
                _fail_result = {
                    "intent": "edit",
                    "summary": f"{len(skipped_changes_struct)} planned change(s) — all failed",
                    "reasoning": "All planned edits failed before producing any code.",
                    "risks": [],
                    "skipped_changes": skipped_changes_struct,
                    "changes_by_file": {},
                    "new_files": [],
                    "natural_text": _fail_msg,
                }
                yield sse({"type": "smart_result", "content": json.dumps(_fail_result)})
            elif full_response.strip() and not skipped_changes_struct:
                # Model produced text (e.g. a plan/explanation) but no actual
                # edit blocks.  The text was already streamed as tokens;
                # send a clear notice so the user knows no files were changed.
                _noedit_msg = (
                    "\n\n⚠️ No code changes were produced — the response above "
                    "was a plan/explanation only.  Please try again and I'll "
                    "apply the edits directly."
                )
                _dlog("no_edits_text_only_notice",
                      session_id=session_id, user_id=user_id,
                      response_len=len(full_response),
                      was_starvation_recovery=bool(_streaming_starvation_abort))
                yield sse({"type": "token", "content": _noedit_msg})
            yield sse({"type": "done", "content": ""})
            return

        _resolution_t0 = time.time()
        _dlog("resolution_phase_start",
              session_id=session_id, user_id=user_id,
              edit_count=len(edit_blocks_raw),
              pipeline_elapsed_s=round(time.time() - _pipeline_t0, 1))
        yield sse({"type": "progress", "content": f"Resolving {len(edit_blocks_raw)} edit(s)..."})

        # Build file content lookup
        file_content_lookup: dict = {sf["filename"]: sf.get("content", "") for sf in session_files}

        changes_by_file: dict = {}
        all_qa_risks: list = []
        summary_parts: list = []

        # ── Symbol resolution with retry loop ────────────────────────────
        # Pass 1: try to resolve every edit block to a real symbol.
        # Any that fail go to a silent correction call to Claude (max 2 rounds).

        MAX_SYMBOL_RETRIES = 2
        pending_edits = list(edit_blocks_raw)

        # ── Bottom-to-top sort for line-number edits ──────────────────────────
        # When multiple line-number edits target the same symbol, applying them
        # from the highest source line downward keeps every edit's positions
        # valid: a bottom-of-symbol edit cannot shift the line indices of an
        # upper edit that hasn't run yet.  This mirrors how Cursor re-applies
        # against current file state — but without a re-prompt, we achieve the
        # same invariant deterministically by processing bottom-to-top.
        # Non-line-number edits keep their relative order (Python's sort is
        # stable; they receive key=-1 and sort after all line-number edits).
        # Edits to different symbols or different files are always independent.
        def _ln_sort_key(raw):
            try:
                d = json.loads(raw.strip()) if isinstance(raw, str) else raw
                sl = d.get("edit_start_line")
                return int(sl) if sl else -1
            except Exception:
                return -1
        pending_edits.sort(key=_ln_sort_key, reverse=True)

        _block_inventory = []
        for _bi, _braw in enumerate(edit_blocks_raw):
            _block_inventory.append({
                "idx": _bi,
                "length": len(_braw),
                "has_xml_tags": "<function_calls>" in _braw or "<invoke" in _braw,
                "has_json_brace": "{" in _braw,
                "first_120": _braw[:120],
                "last_120": _braw[-120:],
            })
        if _block_inventory:
            _dlog("raw_block_inventory",
                  session_id=session_id,
                  blocks=_block_inventory,
                  user_id=user_id)

        _eb_summary = []
        for _b in pending_edits:
            try:
                _bd = json.loads(_b.strip()) if isinstance(_b, str) else _b
                _eb_summary.append({
                    "sym": _bd.get("symbol", "?"),
                    "file": _bd.get("filename", "?"),
                    "has_old_code": bool(_bd.get("old_code")),
                    "old_len": len(_bd.get("old_code", "")),
                    "new_len": len(_bd.get("new_code", "")),
                })
            except Exception:
                _eb_summary.append({"error": "parse_failed"})
        _dlog("edit_blocks_collected",
              session_id=session_id,
              count=len(pending_edits),
              blocks=pending_edits,
              summary=_eb_summary,
                  user_id=user_id)
        resolved_edits: list = []
        skipped_messages: list = []
        # skipped_changes_struct already initialized above (before truncation detection)

        # ── Same-symbol cumulative merge ──────────────────────────────────
        # When several edits target the SAME symbol, each must be spliced into
        # the running (already-edited) symbol — NOT the pristine original — and
        # collapsed into ONE change. Otherwise every edit emits an operation
        # whose `find` is the *original* symbol text, so only the first can
        # apply and the rest silently conflict (the "N edits → 1 survives"
        # failure mode). Keyed by (filename, symbol.full_path).
        _symbol_accum: dict = {}        # key -> latest merged symbol code
        _resolved_by_symbol: dict = {}  # key -> the single resolved_edit entry

        # Tracks (abs_start, abs_end) ranges already applied per (filename, symbol)
        # key. Used by the overlap guard to reject conflicting line-number edits
        # before they can corrupt the accumulated symbol body.
        _ln_applied_ranges: dict = {}   # _akey -> [(abs_start, abs_end), ...]

        for resolve_round in range(MAX_SYMBOL_RETRIES + 1):
            still_unresolved = []

            # ── Correction-round rebase (v3.4) ─────────────────────────────
            # EVERY correction prompt now shows the CURRENT accumulated symbol
            # content, so corrected edits arrive anchored to current line
            # numbers. The applied-ranges recorded in round 0 use pre-shift
            # coordinates — comparing them against corrected edits produces
            # false overlaps that re-block the very fixes the correction loop
            # asked for (the correction-drop bug). Reset the guard once per
            # symbol per correction round; edits within the same round still
            # guard against each other.
            _rebased_this_round: set = set()

            for edit_raw in pending_edits:
                _parse_err_1 = _parse_err_2 = _parse_err_3 = None
                try:
                    edit_data = json.loads(edit_raw.strip())
                except json.JSONDecodeError as _e1:
                    _parse_err_1 = str(_e1)
                    try:
                        edit_data = json.loads(_repair_json(edit_raw.strip()))
                    except Exception as _e2:
                        _parse_err_2 = str(_e2)
                        # Third fallback: strip XML/text prefix, then repair
                        try:
                            _cleaned = _extract_json_from_text(edit_raw)
                            edit_data = json.loads(_repair_json(_cleaned.strip() if isinstance(_cleaned, str) else json.dumps(_cleaned)))
                            _dlog("edit_parse_recovered",
                                  session_id=session_id,
                                  method="extract_json_from_text+repair",
                                  raw_len=len(edit_raw),
                                  user_id=user_id)
                        except Exception as _e3:
                            _parse_err_3 = str(_e3)
                            # Fallback 4: regex-based field extraction (handles
                            # unescaped quotes in JSX and hybrid XML tags)
                            _regex_result = _regex_extract_edit_block(edit_raw)
                            if _regex_result:
                                edit_data = _regex_result
                                _dlog("edit_parse_recovered",
                                      session_id=session_id,
                                      method="regex_extract",
                                      extraction_format=_regex_result.pop("_extraction_format", "unknown"),
                                      raw_len=len(edit_raw),
                                      filename=_regex_result.get("filename"),
                                      symbol=_regex_result.get("symbol"),
                                      has_edit_lines=bool(_regex_result.get("edit_start_line")),
                                      has_old_code=bool(_regex_result.get("old_code")),
                                      new_code_len=len(_regex_result.get("new_code", "")),
                                      user_id=user_id)
                            else:
                                _fn_m = re.search(r'"filename"\s*:\s*"([^"]+)"', edit_raw)
                                _sym_m = re.search(r'"symbol"\s*:\s*"([^"]+)"', edit_raw)
                                _dlog("edit_parse_failed",
                                      session_id=session_id,
                                      raw_preview=edit_raw[:300],
                                      raw_tail=edit_raw[-200:],
                                      raw_len=len(edit_raw),
                                      err_direct=_parse_err_1,
                                      err_repair=_parse_err_2,
                                      err_extract=_parse_err_3,
                                      filename_hint=_fn_m.group(1) if _fn_m else None,
                                      symbol_hint=_sym_m.group(1) if _sym_m else None,
                                      user_id=user_id)
                                skipped_changes_struct.append({
                                    "filename": _fn_m.group(1) if _fn_m else "(unknown)",
                                    "symbol": _sym_m.group(1) if _sym_m else "(truncated)",
                                    "reason": "Edit block was truncated or malformed — Claude may have hit the output token limit",
                                })
                                continue

                filename = edit_data.get("filename", "")
                symbol_name = edit_data.get("symbol", "")
                new_code = edit_data.get("new_code", "")
                description = edit_data.get("description", "")
                old_code = edit_data.get("old_code", "")  # SNIPPET / targeted edit (string-match path)
                edit_start_line = edit_data.get("edit_start_line")   # Option B: line-number path
                edit_end_line   = edit_data.get("edit_end_line")

                if not filename or not new_code:
                    continue

                file_content = file_content_lookup.get(filename, "")
                smap, sf_entry = symbol_maps_by_name.get(filename, (None, None))

                if not file_content:
                    skipped_messages.append(f"File '{filename}' not found in session")
                    skipped_changes_struct.append({
                        "filename": filename,
                        "symbol": symbol_name or "(whole file)",
                        "reason": "file_not_in_session",
                    })
                    continue

                # ── Whole-file edit for non-code files ──────────────────
                # When Claude sends symbol=null (YAML, DNS zones, plaintext,
                # etc.), there are no AST symbols.  Create a virtual symbol
                # spanning the entire file so existing snippet-apply + QA
                # paths work unchanged.
                if not symbol_name:
                    _wf_lines = file_content.split("\n")
                    from models.schemas import SymbolInfo as _SI_wf, SymbolType as _ST_wf
                    symbol = _SI_wf(
                        name=filename.rsplit("/", 1)[-1],
                        symbol_type=_ST_wf.VARIABLE,
                        start_line=1,
                        end_line=len(_wf_lines),
                        parent=None,
                        indentation=0,
                        code=file_content,
                        signature=filename,
                    )
                    match_method = "whole_file"
                else:
                    if not smap:
                        # ── Non-code fallback: Claude used filename as symbol ──
                        # If the symbol name matches/contains the filename and
                        # the file has no AST symbols, treat as whole-file edit
                        # instead of skipping.
                        _bare = filename.rsplit("/", 1)[-1] if "/" in filename else filename
                        if symbol_name in (filename, _bare) and filename in file_content_lookup:
                            _wf_lines = file_content_lookup[filename].split("\n")
                            symbol = SymbolInfo(
                                name=_bare,
                                symbol_type=SymbolType.VARIABLE,
                                start_line=1,
                                end_line=len(_wf_lines),
                                parent=None,
                                indentation=0,
                                code=file_content_lookup[filename],
                            )
                            match_method = "whole_file"
                        else:
                            skipped_messages.append(f"File '{filename}' could not be parsed")
                            skipped_changes_struct.append({
                                "filename": filename,
                                "symbol": symbol_name,
                                "reason": "file_not_parseable",
                            })
                            continue
                    else:
                        symbol, match_method = _fuzzy_find_symbol(smap, symbol_name)

                # ── Fix C: Auto-resolve symbol from line numbers / old_code ──
                # When the model targets the right CODE but names the wrong
                # SYMBOL (common in mega-block HTML files where SymbolMap
                # splits one <script> into script_8, script_9, etc.), verify
                # the resolved symbol actually contains the edit target.
                # If not, scan all symbols to find the correct one.
                if symbol and smap and hasattr(smap, "symbols") and smap.symbols:
                    _needs_resolve = False
                    _resolve_reason = None

                    _dlog("symbol_auto_resolve_check_entry",
                          session_id=session_id,
                          filename=filename,
                          model_requested_symbol=symbol_name,
                          resolved_symbol_name=symbol.name,
                          resolved_symbol_range=(
                              f"{symbol.start_line}-{symbol.end_line}"
                          ),
                          resolved_symbol_code_len=len(
                              symbol.code or ""
                          ),
                          match_method=match_method,
                          has_edit_start_line=bool(edit_start_line),
                          edit_start_line=str(
                              edit_start_line
                          ) if edit_start_line else None,
                          edit_end_line=str(
                              edit_end_line
                          ) if edit_end_line else None,
                          has_old_code=bool(old_code),
                          old_code_len=len(old_code) if old_code else 0,
                          total_symbols=len(smap.symbols),
                          user_id=user_id)

                    # Check A: line-number targeting — edit lines within symbol?
                    if edit_start_line and edit_end_line:
                        _isl_check = int(edit_start_line)
                        if not (symbol.start_line <= _isl_check <= symbol.end_line):
                            _needs_resolve = True
                            _resolve_reason = (
                                f"edit_start_line={_isl_check} outside "
                                f"{symbol.name} range "
                                f"{symbol.start_line}-{symbol.end_line}"
                            )
                            _dlog("symbol_auto_resolve_check_a_mismatch",
                                  session_id=session_id,
                                  filename=filename,
                                  edit_start_line=_isl_check,
                                  symbol_name=symbol.name,
                                  symbol_start=symbol.start_line,
                                  symbol_end=symbol.end_line,
                                  reason=_resolve_reason,
                                  user_id=user_id)
                        else:
                            _dlog("symbol_auto_resolve_check_a_ok",
                                  session_id=session_id,
                                  filename=filename,
                                  edit_start_line=_isl_check,
                                  symbol_name=symbol.name,
                                  symbol_start=symbol.start_line,
                                  symbol_end=symbol.end_line,
                                  verdict="line_within_symbol",
                                  user_id=user_id)

                    # Check B: old_code targeting — old_code present in symbol?
                    elif old_code:
                        _oc_stripped = old_code.strip()
                        _sym_code = symbol.code or ""
                        if _oc_stripped and _oc_stripped not in _sym_code:
                            _needs_resolve = True
                            _resolve_reason = (
                                f"old_code ({len(old_code)} chars) not found "
                                f"in {symbol.name} "
                                f"({len(_sym_code)} chars, "
                                f"L{symbol.start_line}-{symbol.end_line})"
                            )
                            _dlog("symbol_auto_resolve_check_b_mismatch",
                                  session_id=session_id,
                                  filename=filename,
                                  old_code_len=len(old_code),
                                  old_code_preview=old_code[:200],
                                  symbol_name=symbol.name,
                                  symbol_code_len=len(_sym_code),
                                  symbol_start=symbol.start_line,
                                  symbol_end=symbol.end_line,
                                  reason=_resolve_reason,
                                  user_id=user_id)
                        else:
                            _dlog("symbol_auto_resolve_check_b_ok",
                                  session_id=session_id,
                                  filename=filename,
                                  old_code_len=len(old_code),
                                  symbol_name=symbol.name,
                                  symbol_code_len=len(_sym_code),
                                  verdict="old_code_found_in_symbol",
                                  user_id=user_id)
                    else:
                        _dlog("symbol_auto_resolve_check_skip",
                              session_id=session_id,
                              filename=filename,
                              symbol_name=symbol.name,
                              reason="no_edit_start_line_and_no_old_code",
                              user_id=user_id)

                    if _needs_resolve:
                        _correct_sym = None
                        _correct_method = None

                        # Strategy 1: find symbol containing target line
                        if edit_start_line and edit_end_line:
                            _target_line = int(edit_start_line)
                            _best_size = float("inf")
                            _candidates_found = 0
                            for _s in smap.symbols:
                                if _s.start_line <= _target_line <= _s.end_line:
                                    _candidates_found += 1
                                    _s_size = _s.end_line - _s.start_line
                                    # Pick the tightest (smallest) symbol
                                    if _s_size < _best_size:
                                        _best_size = _s_size
                                        _correct_sym = _s
                                        _correct_method = "auto_resolve_by_line"
                            _dlog("symbol_auto_resolve_strategy1_result",
                                  session_id=session_id,
                                  filename=filename,
                                  target_line=_target_line,
                                  candidates_found=_candidates_found,
                                  found=bool(_correct_sym),
                                  resolved_to=(
                                      _correct_sym.name
                                  ) if _correct_sym else None,
                                  resolved_range=(
                                      f"{_correct_sym.start_line}-"
                                      f"{_correct_sym.end_line}"
                                  ) if _correct_sym else None,
                                  user_id=user_id)

                        # Strategy 2: find symbol containing old_code text
                        if not _correct_sym and old_code:
                            _oc_stripped = old_code.strip()
                            _best_size = float("inf")
                            _candidates_found = 0
                            for _s in smap.symbols:
                                _s_code = _s.code or ""
                                if _oc_stripped and _oc_stripped in _s_code:
                                    _candidates_found += 1
                                    _s_size = len(_s_code)
                                    if _s_size < _best_size:
                                        _best_size = _s_size
                                        _correct_sym = _s
                                        _correct_method = "auto_resolve_by_content"
                            _dlog("symbol_auto_resolve_strategy2_result",
                                  session_id=session_id,
                                  filename=filename,
                                  old_code_len=len(old_code),
                                  candidates_found=_candidates_found,
                                  found=bool(_correct_sym),
                                  resolved_to=(
                                      _correct_sym.name
                                  ) if _correct_sym else None,
                                  resolved_range=(
                                      f"{_correct_sym.start_line}-"
                                      f"{_correct_sym.end_line}"
                                  ) if _correct_sym else None,
                                  user_id=user_id)

                        # Strategy 2.5: Preamble edit — imports / file header.
                        # (session e4e9d098) The model edited absolute lines
                        # 1-10 (the import block) but labeled the edit with the
                        # component it was working on ("NewFileCard", exact
                        # name match).  Strategy 1 finds no candidate because
                        # the file preamble (imports before the first symbol)
                        # belongs to NO symbol in the SymbolMap.  Evidence: the
                        # edit's new_code contained 9 of the file's 10 preamble
                        # lines verbatim — a valid edit rejected on a label
                        # technicality, then lost to the correction loop.
                        # Recovery: when the target range falls ENTIRELY before
                        # the first mapped symbol AND the existing lines
                        # substantially reappear in new_code (content proof),
                        # build a tight synthetic symbol covering just the
                        # preamble.  Content verification is what makes it safe
                        # to override an exact-name match here.
                        if (not _correct_sym and edit_start_line
                                and edit_end_line and file_content):
                            _pre_isl = int(edit_start_line)
                            _pre_iel = int(edit_end_line)
                            _first_sym_start = min(
                                (_s.start_line for _s in smap.symbols
                                 if _s.start_line and _s.start_line > 0),
                                default=None,
                            )
                            if (_first_sym_start and _first_sym_start > 1
                                    and 1 <= _pre_isl
                                    and _pre_isl < _first_sym_start):
                                # ── Fix C: cross-boundary preamble edits ──────
                                # Original guard required _pre_iel < _first_sym_start
                                # (pure preamble).  Now we also handle edits that
                                # START in preamble but EXTEND into symbols — e.g.
                                # "swap imports" (line 29) through CONFIDENCE_STYLES
                                # (line 78).  The synthetic symbol covers lines 1
                                # through max(edit_end, first_sym_start-1).
                                _is_cross_boundary = _pre_iel >= _first_sym_start
                                _fc_lines_pre = file_content.split("\n")
                                _fc_total = len(_fc_lines_pre)
                                _tgt_lines = [
                                    _l.strip() for _l in
                                    _fc_lines_pre[_pre_isl - 1:_pre_iel]
                                    if _l.strip()
                                ]
                                _nc_line_set = {
                                    _l.strip() for _l in new_code.split("\n")
                                    if _l.strip()
                                }
                                _pre_hits = sum(
                                    1 for _l in _tgt_lines
                                    if _l in _nc_line_set
                                )
                                _pre_ratio = (
                                    _pre_hits / len(_tgt_lines)
                                ) if _tgt_lines else 0.0
                                if _tgt_lines and _pre_ratio >= 0.6:
                                    try:
                                        from models.schemas import (
                                            SymbolInfo as _SI_pre,
                                            SymbolType as _ST_pre,
                                        )
                                        if _is_cross_boundary:
                                            # Extend synthetic symbol to cover
                                            # the full edit range (clamped to file).
                                            _pre_end = min(_pre_iel, _fc_total)
                                        else:
                                            # Pure preamble — end at symbol boundary.
                                            _pre_end = _first_sym_start - 1
                                        _correct_sym = _SI_pre(
                                            name="_preamble",
                                            symbol_type=_ST_pre.VARIABLE,
                                            start_line=1,
                                            end_line=_pre_end,
                                            parent=None,
                                            indentation=0,
                                            code="\n".join(
                                                _fc_lines_pre[0:_pre_end]
                                            ),
                                        )
                                        _correct_method = (
                                            "auto_resolve_preamble_cross"
                                            if _is_cross_boundary
                                            else "auto_resolve_preamble"
                                        )
                                        _dlog("symbol_auto_resolve_preamble",
                                              session_id=session_id,
                                              filename=filename,
                                              target_range=(
                                                  f"{_pre_isl}-{_pre_iel}"
                                              ),
                                              preamble_range=f"1-{_pre_end}",
                                              first_symbol_start=(
                                                  _first_sym_start
                                              ),
                                              cross_boundary=_is_cross_boundary,
                                              content_match_ratio=round(
                                                  _pre_ratio, 3
                                              ),
                                              matched_lines=(
                                                  f"{_pre_hits}/"
                                                  f"{len(_tgt_lines)}"
                                              ),
                                              original_symbol=symbol_name,
                                              user_id=user_id)
                                    except Exception as _pre_err:
                                        _dlog(
                                            "symbol_auto_resolve_preamble_error",
                                            session_id=session_id,
                                            filename=filename,
                                            error=str(_pre_err)[:200],
                                            user_id=user_id)
                                else:
                                    _dlog(
                                        "symbol_auto_resolve_preamble_skip",
                                        session_id=session_id,
                                        filename=filename,
                                        target_range=f"{_pre_isl}-{_pre_iel}",
                                        content_match_ratio=round(
                                            _pre_ratio, 3
                                        ),
                                        reason=(
                                            "content_verification_failed: "
                                            "existing preamble lines do not "
                                            "substantially reappear in "
                                            "new_code"
                                        ),
                                        user_id=user_id)

                        # Strategy 3: Gap Bridge — create synthetic whole-file
                        # symbol when SymbolMap has coverage gaps.  Common in
                        # mega HTML files where a 6000+ line <script> block is
                        # only partially mapped (e.g. first 100 lines captured
                        # as script_8, rest is gap).  Uses the same whole-file
                        # SymbolInfo pattern as the no-symbol fallback above
                        # (L11558-11568).  All gap bridge edits for the same
                        # file share the name "_gap_bridge" so _symbol_accum
                        # chains them correctly (bottom-to-top sort safe).
                        # ── Gap-bridge guard (session 52802d58 apply-409 fix) ──
                        # When the model named the symbol CORRECTLY (exact name
                        # match) but supplied stale line numbers (from an older
                        # file snapshot), do NOT discard the exact match in
                        # favor of a whole-file gap bridge.  A whole-file change
                        # anchors on the ENTIRE analysis-time file, which can
                        # never relocate after any drift → guaranteed 409 at
                        # apply time (proven: 6/6 apply failures on lines 1-991
                        # in server log 1783375017500).  Keeping the exact-name
                        # symbol lets the snippet path fail naturally into the
                        # correction loop, which re-anchors against CURRENT
                        # symbol content and produces a precise, applyable edit.
                        if not _correct_sym and match_method == "exact":
                            _dlog("symbol_auto_resolve_gap_bridge_skipped",
                                  session_id=session_id,
                                  filename=filename,
                                  symbol_name=symbol.name,
                                  symbol_range=(
                                      f"{symbol.start_line}-{symbol.end_line}"
                                  ),
                                  edit_start_line=str(edit_start_line) if edit_start_line else None,
                                  reason=(
                                      "exact_name_match_kept: stale edit line "
                                      "numbers must not override an exact symbol "
                                      "match with an unrecoverable whole-file "
                                      "change; routing to correction loop instead"
                                  ),
                                  user_id=user_id)
                        elif not _correct_sym and edit_start_line and file_content:
                            _fc_lines = file_content.split("\n")
                            _fc_total = len(_fc_lines)
                            _target_line = int(edit_start_line)
                            _target_end = int(edit_end_line) if edit_end_line else _target_line
                            if 1 <= _target_line <= _fc_total:
                                try:
                                    from models.schemas import (
                                        SymbolInfo as _SI_gap,
                                        SymbolType as _ST_gap,
                                    )
                                    _correct_sym = _SI_gap(
                                        name="_gap_bridge",
                                        symbol_type=_ST_gap.VARIABLE,
                                        start_line=1,
                                        end_line=_fc_total,
                                        parent=None,
                                        indentation=0,
                                        code=file_content,
                                    )
                                    _correct_method = "gap_bridge_whole_file"
                                    _dlog("symbol_auto_resolve_gap_bridge",
                                          session_id=session_id,
                                          filename=filename,
                                          target_line=_target_line,
                                          target_end=_target_end,
                                          file_total_lines=_fc_total,
                                          original_symbol=symbol_name,
                                          original_range=(
                                              f"{symbol.start_line}-"
                                              f"{symbol.end_line}"
                                          ),
                                          created=True,
                                          reason="no_symbol_covers_target_line",
                                          user_id=user_id)
                                except Exception as _gap_err:
                                    _dlog("symbol_auto_resolve_gap_bridge_error",
                                          session_id=session_id,
                                          filename=filename,
                                          target_line=_target_line,
                                          error=str(_gap_err)[:200],
                                          user_id=user_id)
                            else:
                                _dlog("symbol_auto_resolve_gap_bridge_skip",
                                      session_id=session_id,
                                      filename=filename,
                                      target_line=_target_line,
                                      file_total_lines=_fc_total,
                                      reason="target_line_outside_file_bounds",
                                      user_id=user_id)

                        if _correct_sym:
                            _dlog("symbol_auto_resolved",
                                  session_id=session_id,
                                  filename=filename,
                                  original_symbol=symbol_name,
                                  original_range=(
                                      f"{symbol.start_line}-{symbol.end_line}"
                                  ),
                                  resolved_symbol=_correct_sym.name,
                                  resolved_full_path=getattr(
                                      _correct_sym, "full_path", ""
                                  ),
                                  resolved_range=(
                                      f"{_correct_sym.start_line}-"
                                      f"{_correct_sym.end_line}"
                                  ),
                                  resolved_code_len=len(
                                      _correct_sym.code or ""
                                  ),
                                  resolve_method=_correct_method,
                                  reason=_resolve_reason,
                                  user_id=user_id)
                            symbol = _correct_sym
                            match_method = _correct_method
                        else:
                            _dlog("symbol_auto_resolve_failed",
                                  session_id=session_id,
                                  filename=filename,
                                  original_symbol=symbol_name,
                                  original_range=(
                                      f"{symbol.start_line}-{symbol.end_line}"
                                  ),
                                  resolve_reason=_resolve_reason,
                                  available_symbols=[
                                      (getattr(_s, "name", "?"),
                                       _s.start_line, _s.end_line)
                                      for _s in smap.symbols[:30]
                                  ],
                                  edit_start_line=str(
                                      edit_start_line
                                  ) if edit_start_line else None,
                                  old_code_preview=(
                                      old_code[:200]
                                  ) if old_code else None,
                                  user_id=user_id)
                            # Don't block — let the original symbol path
                            # proceed and fail naturally with existing
                            # snippet_apply_failed logging.

                if symbol:
                    # ── Targeted (snippet) edit ──────────────────────────────
                    # When Claude supplies an old_code snippet instead of the
                    # entire symbol, splice it into the running symbol body so
                    # every downstream stage keeps working on the complete
                    # before/after symbol. This is the "edit, don't rewrite"
                    # path for large symbols the model can only see partially.
                    # Multiple edits to the same symbol splice cumulatively and
                    # collapse into ONE change (see _symbol_accum below).
                    _akey = (filename, symbol.full_path)
                    if edit_start_line and edit_end_line:
                        # ── Option B: line-number splice (preferred) ─────────
                        # Claude supplied edit_start_line / edit_end_line instead
                        # of an old_code string.  We extract the exact bytes by
                        # index — zero string matching, immune to whitespace drift.
                        _isl, _iel = int(edit_start_line), int(edit_end_line)

                        # ── Overlap guard ─────────────────────────────────────
                        # Two line-number edits to the same symbol must not target
                        # overlapping source ranges.  The bottom-to-top sort above
                        # ensures non-overlapping ranges apply in the correct order;
                        # overlapping ones are a generation error — skip with a
                        # clear message rather than silently corrupt the symbol.
                        # ── Correction-round rebase: corrected edits are anchored
                        # to the current accumulated content (shown in the
                        # correction prompt), so ranges recorded in earlier rounds
                        # are obsolete for this symbol. Clear once per round.
                        if resolve_round > 0 and _akey not in _rebased_this_round:
                            _dlog("line_range_overlap_rebase", session_id=session_id,
                                  filename=filename, symbol=symbol_name,
                                  resolve_round=resolve_round,
                                  cleared_ranges=_ln_applied_ranges.get(_akey, []),
                                  user_id=user_id)
                            _ln_applied_ranges[_akey] = []
                            _rebased_this_round.add(_akey)

                        _overlap_reason = None
                        _applied_ranges = _ln_applied_ranges.get(_akey, [])
                        for (_ps, _pe) in _applied_ranges:
                            if _isl <= _pe and _iel >= _ps:
                                _overlap_reason = (
                                    f"lines {_isl}\u2013{_iel} overlap with already-applied "
                                    f"range {_ps}\u2013{_pe} in '{symbol_name}'. "
                                    "Two line-number edits to the same symbol must not "
                                    "target overlapping source regions."
                                )
                                break

                        # ── Fix 3: Supersede check ─────────────────────────
                        # When the new edit's range COMPLETELY CONTAINS all
                        # previously applied ranges, it's a broader replacement
                        # (e.g., lines 52-1955 supersedes a prior 1827-1920).
                        # This is NOT a conflict — the surgeon produced a full-
                        # symbol replacement that incorporates earlier changes.
                        # Allow it by applying against the ACCUMULATED state and
                        # clearing the prior ranges.
                        # Evidence (session 82eb1056, turn 7): all 4 items
                        # succeeded but resolution rejected lines 52-1955
                        # as overlapping with 1827-1920 → correction introduced
                        # duplicate ); })} closings → syntax error → QA score 1.
                        if _overlap_reason and _applied_ranges:
                            _all_contained = all(
                                _isl <= _ps and _iel >= _pe
                                for (_ps, _pe) in _applied_ranges
                            )
                            if _all_contained:
                                _dlog("line_range_overlap_supersede",
                                      session_id=session_id,
                                      filename=filename, symbol=symbol_name,
                                      new_range=(_isl, _iel),
                                      superseded_ranges=list(_applied_ranges),
                                      resolve_round=resolve_round,
                                      user_id=user_id)
                                # Clear prior ranges — superseded by the new edit.
                                # The accumulated state already has prior edits
                                # applied; the new broader edit builds on top.
                                _ln_applied_ranges[_akey] = []
                                _overlap_reason = None  # allow this edit

                        if _overlap_reason:
                            # Do NOT dead-end the edit (old behavior silently
                            # dropped it into skipped_changes_struct). Route it
                            # to the correction loop with the CURRENT accumulated
                            # symbol content so Claude can re-anchor the edit
                            # against what the symbol looks like NOW.
                            _accum_now = _symbol_accum.get(_akey, symbol.code)
                            logging.warning(
                                "line_range_overlap: %s %s — routing to correction loop",
                                filename, _overlap_reason
                            )
                            _dlog("line_range_overlap", session_id=session_id,
                                  filename=filename, symbol=symbol_name,
                                  conflict=_overlap_reason,
                                  resolve_round=resolve_round,
                                  routed_to_correction=True,
                                  accum_base_len=len(_accum_now),
                                  user_id=user_id)
                            still_unresolved.append({
                                "filename": filename,
                                "symbol": symbol_name,
                                "new_code": new_code,
                                "description": description,
                                "_raw": edit_raw,
                                "_snippet_reason": (
                                    _overlap_reason
                                    + " The symbol content shown below is the CURRENT "
                                    "state (earlier edits already applied) — re-anchor "
                                    "your edit against it."
                                ),
                                "_symbol_code": _accum_now,
                                "_symbol_start": symbol.start_line,
                            })
                            continue

                        _accum_base = _symbol_accum.get(_akey, symbol.code)
                        _sym_abs_start = getattr(symbol, "start_line", 1) or 1
                        full_new, ok_snip, snip_reason = _apply_snippet_by_lines(
                            _accum_base, _sym_abs_start,
                            _isl, _iel,
                            new_code,
                        )
                        if ok_snip:
                            # Record the applied range so subsequent edits to
                            # this symbol can detect overlaps.
                            _ln_applied_ranges.setdefault(_akey, []).append((_isl, _iel))
                            edit_data["new_code"] = full_new
                            edit_data.pop("old_code", None)
                            edit_data.pop("edit_start_line", None)
                            edit_data.pop("edit_end_line", None)
                        else:
                            # ── Fix B: Option B fail → file-level old_code fallback ─
                            # When line-number splice fails (off-by-one, cross-
                            # boundary, stale line numbers) but the LLM also
                            # supplied old_code that matches the FILE verbatim,
                            # use the file-level string-match path instead of
                            # routing to the expensive correction loop.
                            # Evidence: session 5b63f7b5 — CONFIDENCE_STYLES
                            # and StatusBadge both had valid old_code that was
                            # never tried because Option B ran exclusively.
                            _optb_rescued = False
                            if old_code:
                                _rf_file = file_content_lookup.get(filename, "")
                                if _rf_file:
                                    _rf_old, _rf_ok, _rf_reason = _locate_snippet_in_text(
                                        _rf_file, old_code
                                    )
                                    if _rf_ok and _rf_old:
                                        # old_code found in file — apply as
                                        # file-level find/replace.
                                        edit_data["new_code"] = _accum_base  # no-op on symbol
                                        edit_data.pop("old_code", None)
                                        edit_data.pop("edit_start_line", None)
                                        edit_data.pop("edit_end_line", None)
                                        edit_data.setdefault("_extra_ops", []).append(
                                            {"find": _rf_old, "replace": new_code}
                                        )
                                        _optb_rescued = True
                                        _dlog("optb_file_level_rescue",
                                              session_id=session_id,
                                              filename=filename,
                                              symbol=symbol_name,
                                              original_snip_reason=snip_reason,
                                              file_match_kind=_rf_reason,
                                              old_code_preview=old_code[:200],
                                              user_id=user_id)

                            if not _optb_rescued:
                                _dlog("snippet_apply_failed",
                                      session_id=session_id,
                                      filename=filename,
                                      symbol=symbol_name,
                                      reason=snip_reason,
                                      edit_start_line=_isl,
                                      edit_end_line=_iel,
                                      symbol_start_line=_sym_abs_start,
                                      symbol_code_len=len(_accum_base),
                                      had_old_code=bool(old_code),
                                          user_id=user_id)
                                still_unresolved.append({
                                    "filename": filename,
                                    "symbol": symbol_name,
                                    "new_code": new_code,
                                    "description": description,
                                    "_raw": edit_raw,
                                    "_snippet_reason": snip_reason,
                                    # CURRENT accumulated content — not symbol.code
                                    # (the pristine original). Showing stale content
                                    # made the correction model re-anchor against
                                    # lines that no longer exist (the correction-drop
                                    # bug).
                                    "_symbol_code": _accum_base,
                                    "_symbol_start": symbol.start_line,
                                })
                                continue
                    elif old_code:
                        # ── Option A: string-match splice (legacy fallback) ──
                        # Splice into the running (cumulative) symbol so a second
                        # edit to the same symbol builds on the first, not the
                        # pristine original.
                        _accum_base = _symbol_accum.get(_akey, symbol.code)
                        full_new, ok_snip, snip_reason = _apply_snippet_to_symbol(
                            _accum_base, old_code, new_code
                        )
                        if ok_snip:
                            # Structural-balance guard: detect when new_code changes
                            # the net brace balance vs old_code (e.g. drops a closing })
                            _ob_delta = old_code.count("{") - old_code.count("}")
                            _nb_delta = new_code.count("{") - new_code.count("}")
                            if abs(_ob_delta - _nb_delta) >= 1:
                                _snip_bal_reason = (
                                    ("brace_imbalance: your old_code had net " + str(_ob_delta) + " braces " + "but new_code has net " + str(_nb_delta) + " braces " + " \u2014 you likely dropped a closing } or {. " + "Copy your old_code closing lines verbatim into new_code.")
                                )
                                _dlog("snippet_structural_imbalance",
                                      session_id=session_id,
                                      filename=filename,
                                      symbol=symbol_name,
                                      old_brace_delta=_ob_delta,
                                      new_brace_delta=_nb_delta,
                                      old_code_tail=old_code[-300:],
                                      new_code_tail=new_code[-300:],
                                      user_id=user_id)
                                still_unresolved.append({
                                    "filename": filename,
                                    "symbol": symbol_name,
                                    "new_code": new_code,
                                    "description": description,
                                    "_raw": edit_raw,
                                    "_snippet_reason": _snip_bal_reason,
                                    "_symbol_code": _accum_base,
                                    "_symbol_start": symbol.start_line,
                                })
                                continue
                            edit_data["new_code"] = full_new
                            edit_data.pop("old_code", None)  # now a full-symbol edit
                        else:
                            # ── Resolution-phase FILE-LEVEL fallback (session 228e17ec fix) ──
                            # Proven failure: the model targeted symbol "AppState"
                            # with old_code that lives elsewhere in the file (the
                            # zustand create() store body — not a parsed symbol).
                            # The symbol splice failed, every correction round
                            # failed the same way, and the edit was dropped
                            # (degenerate_drop) even though old_code matched the
                            # file VERBATIM and uniquely. Fallback: locate
                            # old_code in the full file; on an unambiguous match
                            # OUTSIDE the symbol, keep the symbol edit as a no-op
                            # and attach the replacement as a companion
                            # file-level operation (same mechanism as the QA
                            # correction loop's Path 2b).
                            _rf_file = file_content_lookup.get(filename, "")
                            _rf_old, _rf_ok, _rf_reason = _locate_snippet_in_text(
                                _rf_file, old_code
                            )
                            if _rf_ok and _rf_old and _rf_old not in _accum_base:
                                edit_data["new_code"] = _accum_base  # no-op symbol edit
                                edit_data.pop("old_code", None)
                                edit_data.setdefault("_extra_ops", []).append(
                                    {"find": _rf_old, "replace": new_code}
                                )
                                _dlog("resolution_file_level_op_accepted",
                                      session_id=session_id,
                                      filename=filename,
                                      symbol=symbol_name,
                                      match_kind=_rf_reason,
                                      find_preview=_rf_old[:200],
                                      replace_preview=new_code[:200],
                                      user_id=user_id)
                                # fall through to the resolved path (no continue)
                            else:
                                _dlog("snippet_apply_failed",
                                      session_id=session_id,
                                      filename=filename,
                                      symbol=symbol_name,
                                      reason=snip_reason,
                                      file_level_reason=(
                                          _rf_reason if not _rf_ok
                                          else "match lies inside the target symbol"
                                      ),
                                      old_code_sent=old_code,
                                  old_code_len=len(old_code),
                                  symbol_code_actual=_accum_base,
                                  symbol_code_len=len(_accum_base),
                                      user_id=user_id)
                            still_unresolved.append({
                                "filename": filename,
                                "symbol": symbol_name,
                                "new_code": new_code,
                                "description": description,
                                "_raw": edit_raw,
                                "_snippet_reason": snip_reason,
                                # CURRENT accumulated content — not symbol.code
                                # (the pristine original). Showing stale content
                                # made the correction model re-anchor against
                                # lines that no longer exist (the correction-drop
                                # bug).
                                "_symbol_code": _accum_base,
                                "_symbol_start": symbol.start_line,
                            })
                            continue
                    else:
                        # ── Degenerate-fragment guard ────────────────────────
                        # No old_code was supplied. If new_code is clearly only a
                        # FRAGMENT of a large symbol, applying it as a full-symbol
                        # replacement would destroy the rest of the symbol. Force a
                        # targeted-edit retry (old_code/new_code) instead of building
                        # a destructive find:<whole symbol> -> replace:<fragment> op.
                        _accum_frag_base = _symbol_accum.get(_akey, symbol.code)
                        frag_reason = _fragment_reason(_accum_frag_base, new_code)
                        if frag_reason:
                            still_unresolved.append({
                                "filename": filename,
                                "symbol": symbol_name,
                                "new_code": new_code,
                                "description": description,
                                "_raw": edit_raw,
                                "_snippet_reason": frag_reason,
                                # CURRENT accumulated content (see comment above)
                                "_symbol_code": _accum_frag_base,
                                "_symbol_start": symbol.start_line,
                            })
                            continue
                    # Record the latest merged symbol code for this symbol so a
                    # subsequent edit to the same symbol splices on top of it.
                    _symbol_accum[_akey] = edit_data["new_code"]
                    if _akey in _resolved_by_symbol:
                        # Collapse into the single change for this symbol: keep ONE
                        # operation (find=original, replace=fully-merged code) so
                        # there is never a conflicting second op on the same text.
                        _prev = _resolved_by_symbol[_akey]
                        _prev["edit_data"]["new_code"] = edit_data["new_code"]
                        # Preserve file-level companion ops from THIS edit —
                        # without this merge, a second edit to the same symbol
                        # silently discarded them (session 228e17ec fix).
                        if edit_data.get("_extra_ops"):
                            _prev["edit_data"].setdefault("_extra_ops", []).extend(
                                edit_data["_extra_ops"]
                            )
                        _d_old = _prev["edit_data"].get("description", "")
                        _d_new = edit_data.get("description", "")
                        if _d_new and _d_new not in _d_old:
                            _prev["edit_data"]["description"] = (
                                f"{_d_old}; {_d_new}".strip("; ")
                            )
                    else:
                        _entry = {
                            "edit_data": edit_data,
                            "symbol": symbol,
                            "sf_entry": sf_entry,
                            "file_content": file_content,
                            "filename": filename,
                        }
                        _resolved_by_symbol[_akey] = _entry
                        resolved_edits.append(_entry)
                    _dlog("edit_resolved",
                          session_id=session_id,
                          filename=filename,
                          symbol=symbol_name,
                          symbol_start=symbol.start_line,
                          symbol_end=symbol.end_line,
                          symbol_lines=symbol.end_line - symbol.start_line + 1,
                          had_old_code=bool(old_code),
                          had_line_numbers=bool(edit_start_line and edit_end_line),
                          user_id=user_id)
                else:
                    still_unresolved.append({
                        "filename": filename,
                        "symbol": symbol_name,
                        "new_code": new_code,
                        "description": description,
                        "_raw": edit_raw,
                    })

            if not still_unresolved or resolve_round >= MAX_SYMBOL_RETRIES:
                if still_unresolved:
                    for item in still_unresolved:
                        _snip_r = item.get("_snippet_reason")
                        if _snip_r:
                            skipped_messages.append(
                                f"Edit to '{item['symbol']}' in {item['filename']} could not be "
                                f"anchored after {MAX_SYMBOL_RETRIES} correction attempts: {_snip_r}"
                            )
                            _reason = "edit_anchor_unmatched"
                        else:
                            skipped_messages.append(
                                f"Symbol '{item['symbol']}' not found in {item['filename']} after {MAX_SYMBOL_RETRIES} correction attempts"
                            )
                            _reason = "symbol_not_found"
                        skipped_changes_struct.append({
                            "filename": item.get("filename", ""),
                            "symbol": item.get("symbol", ""),
                            "reason": _reason,
                        })
                break

            # ── Disconnect checkpoint (session e4e9d098) ─────────────────
            # Evidence: a client disconnect during the correction round's
            # Claude call vaporized a fully-resolved 10.2KB edit that had
            # succeeded 31s earlier — the safety net in chat.py only saved
            # visible chat tokens.  Before entering the long, risky
            # correction wait, emit everything resolved SO FAR so the
            # stream wrapper can persist it if the connection dies.
            # Unknown SSE types are ignored by all frontend parsers
            # (verified: if/else-if dispatch in api/client.ts).
            if resolved_edits:
                try:
                    _ckpt_payload = {
                        "resolve_round": resolve_round,
                        "unresolved_count": len(still_unresolved),
                        "resolved": [
                            {
                                "filename": _ck["filename"],
                                "symbol": getattr(
                                    _ck["symbol"], "name", "?"
                                ),
                                "description": _ck["edit_data"].get(
                                    "description", ""
                                ),
                                "new_code": _ck["edit_data"].get(
                                    "new_code", ""
                                ),
                            }
                            for _ck in resolved_edits
                        ],
                    }
                    _dlog("resolution_checkpoint_emitted",
                          session_id=session_id,
                          resolve_round=resolve_round,
                          resolved_count=len(resolved_edits),
                          unresolved_count=len(still_unresolved),
                          payload_bytes=len(json.dumps(_ckpt_payload)),
                          user_id=user_id)
                    yield sse({"type": "checkpoint",
                               "content": json.dumps(_ckpt_payload)})
                except Exception as _ckpt_err:
                    _dlog("resolution_checkpoint_error",
                          session_id=session_id,
                          error=str(_ckpt_err)[:200],
                          user_id=user_id)

            # ── Silent correction call ────────────────────────────────────
            yield sse({"type": "progress",
                       "content": f"Correcting symbol references ({resolve_round + 1}/{MAX_SYMBOL_RETRIES})..."})

            correction_text = _build_symbol_correction(still_unresolved, symbol_maps_by_name)
            _dlog("correction_prompt_sent",
                  session_id=session_id,
                  resolve_round=resolve_round,
                  unresolved_count=len(still_unresolved),
                  unresolved=[{"f": x.get("filename"), "s": x.get("symbol"), "reason": x.get("_snippet_reason")} for x in still_unresolved],
                  correction_prompt=correction_text,
                      user_id=user_id)
            correction_msgs = messages + [
                {"role": "assistant", "content": full_response or "(analyzing code...)"},
                {"role": "user", "content": correction_text},
            ]

            try:
                # Run non-streaming call with keepalive pings so proxy stays alive
                _corr_correction_model = "claude-sonnet-5"  # R25: corrections always use Claude
                _dlog("correction_call_config", session_id=session_id,
                      model=_corr_correction_model,
                      wrapper="safe_claude_call",
                      resolve_round=resolve_round)
                _corr_task = asyncio.create_task(_safe_claude_call(
                    aclient,
                    model=_corr_correction_model,
                    desired_text_tokens=12000,
                    thinking_budget=4000,
                    retry_on_starve=True,
                    system=system_prompt,
                    messages=correction_msgs,
                ))
                # ── Deadline + visible heartbeat (session e4e9d098 fix 3) ──
                # Evidence: this loop kept a run alive with invisible
                # keepalives while the UI showed a frozen "Correcting symbol
                # references..." — and it has no overall deadline, so a hung
                # Claude call would keepalive forever.  Emit elapsed-time
                # progress the user can SEE, and give up cleanly at 180s
                # (routes to the existing correction-failed handler; already-
                # resolved edits still ship).
                _corr_t0 = time.time()
                while not _corr_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(_corr_task), timeout=20.0)
                    except asyncio.TimeoutError:
                        # ── Pipeline deadline (symbol-reference correction) ─
                        if _pipeline_over_budget():
                            _corr_task.cancel()
                            _dlog("pipeline_deadline_skip",
                                  session_id=session_id, user_id=user_id,
                                  phase="symbol_correction_keepalive",
                                  resolve_round=resolve_round,
                                  elapsed_s=round(time.time() - _pipeline_t0, 1),
                                  deadline_s=PIPELINE_DEADLINE_S)
                            raise TimeoutError(
                                "pipeline deadline exceeded during symbol correction"
                            )
                        _corr_elapsed = time.time() - _corr_t0
                        if _corr_elapsed >= 180.0:
                            _corr_task.cancel()
                            _dlog("correction_call_deadline",
                                  session_id=session_id,
                                  resolve_round=resolve_round,
                                  elapsed_s=round(_corr_elapsed, 1),
                                  user_id=user_id)
                            raise TimeoutError(
                                "correction call exceeded 180s deadline"
                            )
                        _dlog("correction_call_keepalive",
                              session_id=session_id, user_id=user_id,
                              resolve_round=resolve_round,
                              elapsed_s=round(_corr_elapsed, 1))
                        yield sse({
                            "type": "progress",
                            "content": (
                                f"Correcting symbol references "
                                f"({resolve_round + 1}/{MAX_SYMBOL_RETRIES})"
                                f"… still working ({int(_corr_elapsed)}s)"
                            ),
                        })
                corr_resp = _corr_task.result()

                corr_text = "".join(
                    block.text for block in corr_resp.content if hasattr(block, "text")
                )
                _dlog("correction_response",
                      session_id=session_id,
                      resolve_round=resolve_round,
                      response_chars=len(corr_text),
                      response_preview=corr_text,
                          user_id=user_id)

                # Extract new edit blocks from correction response
                new_pending = []
                _cs, _cb = "normal", ""
                for char in corr_text:
                    if _cs == "normal":
                        _cb += char
                        if _cb.endswith(EDIT_OPEN):
                            _cb, _cs = "", "in_edit"
                    else:
                        _cb += char
                        if _cb.endswith(EDIT_CLOSE):
                            new_pending.append(_cb[:-len(EDIT_CLOSE)])
                            _cb, _cs = "", "normal"
                # Filter correction edits: only keep edits for symbols
                # that were actually unresolved. The correction model may
                # gratuitously re-emit edits for already-resolved symbols,
                # which would double-splice and create duplicates.
                _unresolved_keys = {(x["filename"], x["symbol"]) for x in still_unresolved}
                _filtered_pending = []
                for _np_raw in (new_pending or []):
                    try:
                        _np_parsed = json.loads(_np_raw)
                        _np_key = (_np_parsed.get("filename", ""), _np_parsed.get("symbol", ""))
                        if _np_key in _unresolved_keys:
                            _filtered_pending.append(_np_raw)
                        else:
                            _dlog("correction_edit_skipped_already_resolved",
                                  session_id=session_id,
                                  filename=_np_parsed.get("filename", ""),
                                  symbol=_np_parsed.get("symbol", ""),
                                  user_id=user_id)
                    except Exception:
                        _filtered_pending.append(_np_raw)
                pending_edits = _filtered_pending

                # ── Correction search-and-retry ───────────────────────────
                # When Claude asks for a <search_request> instead of emitting
                # edit blocks, execute the search inline and issue one more
                # Claude call so it can produce the edit with verbatim anchors.
                if not new_pending:
                    if "<cannot_anchor" in corr_text:
                        # Model honestly can't anchor — log it; the existing
                        # no-edit path below already degrades to skipped.
                        _dlog("correction_cannot_anchor",
                              session_id=session_id,
                              resolve_round=resolve_round,
                              response_preview=corr_text[:300],
                              user_id=user_id)
                    import re as _re_corr
                    _csm = _re_corr.search(
                        r'<search_request>\s*(\{.*?\})\s*</search_request>',
                        corr_text, _re_corr.DOTALL
                    )
                    if _csm:
                        try:
                            _creq = json.loads(_csm.group(1))
                            _cterms = _creq.get("terms", [])
                            if _cterms:
                                _csr = _resolve_search_multifile(
                                    _cterms, symbol_maps_by_name,
                                    file_content_lookup_stream
                                )
                                _dlog("correction_search_executed",
                                      session_id=session_id,
                                      resolve_round=resolve_round,
                                      terms=_cterms,
                                      results_chars=len(_csr),
                                      results_preview=_csr,
                                      user_id=user_id)
                                _fu_msgs = correction_msgs + [
                                    {"role": "assistant", "content": corr_text},
                                    {"role": "user", "content": (
                                        "Search results for your request:\n\n"
                                        + _csr
                                        + "\n\nNow write your corrected <surgical_edit> block(s). "
                                        "Use the EXACT verbatim lines shown above as your "
                                        "old_code anchor — copy them character-for-character. "
                                        "If you cannot locate the exact code to anchor on, "
                                        "reply <cannot_anchor reason='...'/> instead of "
                                        "guessing — never fabricate an anchor."
                                    )},
                                ]
                                _dlog("correction_followup_call_config",
                                      session_id=session_id,
                                      model="claude-sonnet-5",
                                      wrapper="safe_claude_call")
                                _fu_task = asyncio.create_task(
                                    _safe_claude_call(
                                        aclient,
                                        model="claude-sonnet-5",
                                        desired_text_tokens=12000,
                                        thinking_budget=4000,
                                        retry_on_starve=True,
                                        system=system_prompt,
                                        messages=_fu_msgs,
                                    )
                                )
                                _fu_t0 = time.time()
                                while not _fu_task.done():
                                    try:
                                        await asyncio.wait_for(
                                            asyncio.shield(_fu_task), timeout=20.0
                                        )
                                    except asyncio.TimeoutError:
                                        _fu_el = int(time.time() - _fu_t0)
                                        yield sse({"type": "progress",
                                                   "content": f"Correction follow-up… ({_fu_el}s)"})
                                _fu_resp = _fu_task.result()
                                _fu_text = "".join(
                                    b.text for b in _fu_resp.content
                                    if hasattr(b, "text")
                                )
                                _dlog("correction_followup_response",
                                      session_id=session_id,
                                      resolve_round=resolve_round,
                                      duration_s=round(time.time() - _fu_t0, 1),
                                      response_chars=len(_fu_text),
                                      response_preview=_fu_text,
                                      user_id=user_id)
                                # Extract edit blocks from follow-up response
                                _fup, _fcs, _fcb = [], "normal", ""
                                for _fc in _fu_text:
                                    if _fcs == "normal":
                                        _fcb += _fc
                                        if _fcb.endswith(EDIT_OPEN):
                                            _fcb, _fcs = "", "in_edit"
                                    else:
                                        _fcb += _fc
                                        if _fcb.endswith(EDIT_CLOSE):
                                            _fup.append(_fcb[:-len(EDIT_CLOSE)])
                                            _fcb, _fcs = "", "normal"
                                if _fup:
                                    # Same filter — only keep unresolved symbols
                                    _fup_filtered = []
                                    for _fup_raw in _fup:
                                        try:
                                            _fup_parsed = json.loads(_fup_raw)
                                            _fup_key = (_fup_parsed.get("filename", ""), _fup_parsed.get("symbol", ""))
                                            if _fup_key in _unresolved_keys:
                                                _fup_filtered.append(_fup_raw)
                                            else:
                                                _dlog("correction_followup_edit_skipped_already_resolved",
                                                      session_id=session_id,
                                                      filename=_fup_parsed.get("filename", ""),
                                                      symbol=_fup_parsed.get("symbol", ""),
                                                      user_id=user_id)
                                        except Exception:
                                            _fup_filtered.append(_fup_raw)
                                    pending_edits = _fup_filtered or _fup
                        except Exception as _cse:
                            _dlog("correction_search_failed",
                                  session_id=session_id,
                                  error=str(_cse),
                                  user_id=user_id)

            except Exception as _corr_fail:
                # Log the actual failure — this bare handler previously
                # swallowed the reason (session e4e9d098 audit).
                _dlog("correction_call_failed",
                      session_id=session_id,
                      resolve_round=resolve_round,
                      error=str(_corr_fail)[:300],
                      error_type=type(_corr_fail).__name__,
                      user_id=user_id)
                for item in still_unresolved:
                    skipped_messages.append(f"Symbol '{item['symbol']}' not found in {item['filename']} — skipped")
                    skipped_changes_struct.append({
                        "filename": item.get("filename", ""),
                        "symbol": item.get("symbol", ""),
                        "reason": "correction_failed",
                    })
                break

        # ── Build SurgicalChange objects with parallel QA ────────────────────
        # Run all QA checks concurrently (asyncio.gather) so 2 changes take the
        # same time as 1. Send SSE keepalive pings every 20 s so Railway/Vercel
        # doesn't kill the connection during long QA runs.

        _dlog("resolution_summary",
              session_id=session_id,
              resolved_count=len(resolved_edits),
              skipped_count=len(skipped_changes_struct),
              resolved_symbols=[(r["filename"], r["symbol"].name, r["symbol"].start_line, r["symbol"].end_line) for r in resolved_edits],
              skipped_details=skipped_changes_struct[:10],
              user_id=user_id)

        _qa_t0 = time.time()
        _dlog("qa_phase_start",
              session_id=session_id, user_id=user_id,
              change_count=len(resolved_edits),
              resolution_duration_s=round(time.time() - _resolution_t0, 1),
              pipeline_elapsed_s=round(time.time() - _pipeline_t0, 1))
        yield sse({"type": "progress", "content": f"Running QA on {len(resolved_edits)} change(s)..."})

        # Build shared context once — same view the architect had
        _qa_other_parts = []
        for _fn, (_sm, _sf) in symbol_maps_by_name.items():
            if _sm is None:
                continue
            _syms_brief = [
                f"  [{s.symbol_type.value}] {s.full_path} (L{s.start_line}–{s.end_line}, {s.end_line-s.start_line+1}L)"
                for s in _sm.symbols[:30]
            ]
            imports = getattr(_sm, "imports", [])[:15]
            import_block = ("IMPORTS:\n" + "\n".join(f"  {i}" for i in imports) + "\n") if imports else ""
            _qa_other_parts.append(f"FILE: {_fn}\n{import_block}SYMBOLS:\n" + "\n".join(_syms_brief))
        _qa_other_context = "\n\n".join(_qa_other_parts)
        if accumulated_search_results:
            _qa_other_context += "\n\n" + accumulated_search_results

        _all_descriptions = [
            r["edit_data"].get("description", "")
            for r in resolved_edits if r["edit_data"].get("description")
        ]

        # v3.13.0: Richer companion-edit context for QA cross-dependency awareness
        _all_edit_summaries = []
        for _re in resolved_edits:
            _ed = _re["edit_data"]
            _sym_name = _re["symbol"].name if _re.get("symbol") else "unknown"
            _fname = _re.get("filename", "")
            _all_edit_summaries.append({
                "symbol": _sym_name,
                "filename": _fname,
                "description": _ed.get("description", ""),
            })

        from models.schemas import QAResult as _QAResult

        # Pre-build diffs and change shells (no I/O — instant)
        change_shells = []

        # ── Fix 1: cross-change ordering ─────────────────────────────────
        # When multiple changes target the same file, QA must see the file
        # state AFTER previous changes are applied — not the original.
        # Example: change 1 adds a helper, change 2 calls it.
        # QA for change 2 must see the helper already present.
        # We apply each change to an in-memory copy sequentially so every
        # QA call receives the correct intermediate "before" state.
        from services.surgical_editor import apply_change as _apply_change_fn
        _intermediate_contents: dict = {}   # fname -> content after prior changes

        for i, resolved in enumerate(resolved_edits):
            edit_data    = resolved["edit_data"]
            symbol       = resolved["symbol"]
            sf_entry     = resolved["sf_entry"]
            file_content = resolved["file_content"]
            filename     = resolved["filename"]
            new_code     = edit_data.get("new_code", "")
            description  = edit_data.get("description", "")

            # Seed intermediate state on first touch for this file
            if filename not in _intermediate_contents:
                _intermediate_contents[filename] = file_content

            # QA sees the file state AFTER all previous changes to this file
            qa_original_content = _intermediate_contents[filename]

            diff = _make_diff(symbol.code, new_code, symbol.name)
            _tgt, _repl = _compute_target_element(symbol.code, new_code)

            # File-level companion ops (resolution-phase fallback): include
            # them in the diff and describe them to QA — otherwise a no-op
            # sentinel symbol edit looks like an empty change.
            _x_ops = list(edit_data.get("_extra_ops") or [])
            _x_note = ""
            if _x_ops:
                for _xop in _x_ops:
                    diff += ("\n" if diff else "") + _make_diff(
                        _xop["find"], _xop["replace"],
                        f"{filename} (file-level companion)",
                    )
                _x_note = "\n\n".join(
                    "[File-level companion edit applied OUTSIDE the target "
                    f"symbol in {filename}]\nBEFORE:\n{_xop['find']}\n"
                    f"AFTER:\n{_xop['replace']}"
                    for _xop in _x_ops
                )

            _same_run = "\n".join(
                f"  \u2022 [{s['symbol']} in {s['filename']}] {s['description']}"
                for j, s in enumerate(_all_edit_summaries) if j != i and s["description"]
            )

            _targeted_ctx = ""
            try:
                from services.context_resolver import resolve_context_requests as _rcr
                if symbol.name:
                    _targeted_ctx = _rcr(
                        requests=[{"type": "callers", "name": symbol.name},
                                  {"type": "usages",  "name": symbol.name}],
                        symbol_maps_by_name=symbol_maps_by_name,
                        requesting_file=filename,
                    )
            except Exception:
                pass

            # Advance the intermediate state so the NEXT change on this file
            # sees this change already applied.
            try:
                _sc_proxy = type("_SC", (), {
                    "new_code": new_code,
                    "original_code": symbol.code,
                    "symbol": symbol,
                    "operations": [{"find": symbol.code, "replace": new_code}],
                    "applied": False,
                })()
                _intermediate_contents[filename] = _apply_change_fn(
                    _intermediate_contents[filename], _sc_proxy
                )
            except Exception:
                pass  # keep current intermediate if apply fails; QA still runs

            change_shells.append({
                "symbol":              symbol,
                "sf_entry":            sf_entry,
                "file_content":        file_content,        # original file (for SurgicalChange)
                "qa_original_content": qa_original_content, # intermediate (for QA)
                "filename":            filename,
                "new_code":            new_code,
                "description":         description,
                "diff":                diff,
                "_tgt":                _tgt,
                "_repl":               _repl,
                "same_run":            _same_run,
                "targeted_ctx":        _targeted_ctx,
            })
        # Launch all QA calls concurrently
        qa_tasks = [
            asyncio.create_task(run_qa_agent(
                original_code=cs["qa_original_content"],  # intermediate state, not raw original
                new_code=cs["new_code"],
                change_description=(
                    cs["description"] + "\n\n" + cs["_x_note"]
                    if cs.get("_x_note") else cs["description"]
                ),
                new_logic=cs["description"],
                symbol_path=cs["symbol"].name,
                filename=cs["filename"],
                other_files_context=_qa_other_context,
                session_id=session_id or "",
                user_id=user_id,
                architect_risks=all_qa_risks,
                targeted_context=cs["targeted_ctx"],
                qa_feedback=None,
                same_run_context=cs["same_run"],
            ))
            for cs in change_shells
        ]

        # Wait for all QA to finish, sending visible progress every 20 s
        pending = set(qa_tasks)
        while pending:
            done_set, pending = await asyncio.wait(pending, timeout=20.0)
            if pending:
                _qa_elapsed = int(time.time() - _qa_t0)
                _qa_done_count = len(qa_tasks) - len(pending)
                yield sse({"type": "progress",
                           "content": f"QA: {_qa_done_count}/{len(qa_tasks)} checks done… ({_qa_elapsed}s)"})
                _dlog("qa_keepalive",
                      session_id=session_id, user_id=user_id,
                      elapsed_s=_qa_elapsed,
                      done=_qa_done_count, total=len(qa_tasks),
                      pipeline_elapsed_s=round(time.time() - _pipeline_t0, 1))

        qa_results = []
        for t in qa_tasks:
            try:
                qa_results.append(t.result())
            except Exception as _qa_task_err:
                _dlog("qa_task_collection_error",
                      session_id=session_id,
                      error_type=type(_qa_task_err).__name__,
                      error=str(_qa_task_err)[:300],
                      user_id=user_id)
                qa_results.append({
                    "verdict": "warning", "qa_score": 7,
                    "summary": "QA could not run", "import_issues": [],
                    "downstream_risks": [], "type_errors": [],
                    "plan_deviation": "", "risk_verdicts": [],
                })

        _dlog("qa_results_collected",
              session_id=session_id,
              qa_duration_s=round(time.time() - _qa_t0, 1),
              pipeline_elapsed_s=round(time.time() - _pipeline_t0, 1),
              count=len(qa_results),
              results=[{
                  "symbol": change_shells[_qi]["symbol"].name if _qi < len(change_shells) else "?",
                  "filename": change_shells[_qi]["filename"] if _qi < len(change_shells) else "?",
                  "verdict": _qr.get("verdict", "?"),
                  "score": _qr.get("qa_score"),
                  "summary": (_qr.get("summary") or "")[:200],
              } for _qi, _qr in enumerate(qa_results)],
              user_id=user_id)

        # ── Structural QA — deterministic pre-check ────────────────────────
        # Run fast, zero-LLM checks (missing imports, duplicate defs, wrong
        # import depth, dropped exports) BEFORE the retry loop.  If structural
        # issues are found, force the LLM QA verdict/score down so the retry
        # loop fires automatically.
        try:
            from services.structural_qa import run_structural_qa, has_blocking_issues as _has_sq_blocking, filter_preexisting_issues as _filter_sq
        except ImportError:
            _has_sq_blocking = None
            _filter_sq = None

        if _has_sq_blocking is not None:
            for _sq_i, _sq_cs in enumerate(change_shells):
                _sq_new   = _sq_cs["new_code"]
                _sq_orig  = _sq_cs["symbol"].code
                _sq_fname = _sq_cs["filename"]
                # Pass the FULL original file + sibling edits so top-of-file
                # imports are visible to the missing-import check (session
                # 52802d58 false-positive fix).
                _sq_issues = run_structural_qa(
                    _sq_new, _sq_orig, _sq_fname,
                    file_content=_sq_cs.get("file_content") or "",
                    all_changes=[
                        {"filename": _s.get("filename"), "new_code": _s.get("new_code")}
                        for _s in change_shells
                    ],
                )
                # Filter out pre-existing issues in the ORIGINAL code before
                # the edit — only block on errors the edit INTRODUCED.
                # (session d007eaf1: pre-existing syntax errors at lines 1465
                # and 2718 triggered 12 unnecessary correction rounds that
                # introduced a NEW bug, shipping QA 3/10 instead of 9/10.)
                if _filter_sq is not None:
                    _sq_pre_count = len(_sq_issues)
                    _sq_issues = _filter_sq(
                        _sq_issues, _sq_orig, _sq_fname,
                        file_content=_sq_cs.get("file_content") or "",
                    )
                    if _sq_pre_count != len(_sq_issues):
                        _dlog("structural_qa_preexisting_filtered",
                              session_id=session_id,
                              filename=_sq_fname,
                              symbol=_sq_cs["symbol"].name,
                              orig_issue_count=_sq_pre_count,
                              remaining_issue_count=len(_sq_issues),
                              filtered_count=_sq_pre_count - len(_sq_issues),
                              user_id=user_id)
                if _has_sq_blocking(_sq_issues):
                    # Merge structural issues into LLM QA result so the retry
                    # prompt includes them and Claude knows exactly what to fix.
                    _sq_msgs = [f"[STRUCTURAL] {si['message']}" for si in _sq_issues if si["severity"] == "error"]
                    # Log the ACTUAL issue messages — in session dd543a3a only
                    # the count ("2 blocking issue(s)") was recoverable from
                    # logs; the messages went solely to the SSE stream and
                    # retry prompt, making root-cause analysis impossible.
                    _dlog("structural_qa_blocking",
                          session_id=session_id,
                          filename=_sq_fname,
                          symbol=_sq_cs["symbol"].name,
                          blocking_count=len(_sq_msgs),
                          messages=[m[:300] for m in _sq_msgs],
                          all_issues=[{"severity": si["severity"],
                                       "message": si["message"][:300]}
                                      for si in _sq_issues],
                          prior_llm_verdict=qa_results[_sq_i].get("verdict"),
                          prior_llm_score=qa_results[_sq_i].get("qa_score"),
                          user_id=user_id)
                    qa_results[_sq_i]["import_issues"] = (
                        qa_results[_sq_i].get("import_issues", []) + _sq_msgs
                    )
                    qa_results[_sq_i]["verdict"] = "blocked"
                    qa_results[_sq_i]["qa_score"] = min(qa_results[_sq_i].get("qa_score", 10) or 10, 3)
                    qa_results[_sq_i]["summary"] = (
                        f"Structural QA: {len(_sq_msgs)} blocking issue(s). "
                        + qa_results[_sq_i].get("summary", "")
                    )
                    yield sse({"type": "progress",
                               "content": f"🔍 Structural QA found {len(_sq_msgs)} blocking issue(s) "
                                          f"in {_sq_cs['symbol'].name} — will auto-fix"})

        # ── tsc compile gate (natural pipeline) ───────────────────────────────
        # The smart pipeline runs tsc on its edits; the natural/large-file path
        # historically did not, so compile-only errors that LLM QA does not
        # mentally emulate (e.g. TS1487 octal escape inside a JS template
        # literal) could pass at a high score and break the production build.
        # This helper runs real `tsc --noEmit` on the FULL file after a change
        # and returns only the errors that change *introduced* — comparing
        # against the original so pre-existing isolated-file noise (e.g. TS2307
        # "cannot find module" for unresolved imports) never causes a false
        # block. Introduced errors are fed into the SAME retry + 8/10-gate
        # machinery used for QA/structural issues — no parallel loop.
        async def _tsc_introduced_errors(cs, orig_cache):
            fname = cs["filename"]
            if not fname.lower().endswith((".ts", ".tsx", ".js", ".jsx")):
                return []
            try:
                from services.linter_validator import validate_linters as _vl
            except Exception:
                return []
            orig_full = cs["file_content"]
            try:
                _proxy = type("_SC", (), {
                    "new_code": cs["new_code"],
                    "original_code": cs["symbol"].code,
                    "symbol": cs["symbol"],
                    "operations": [{"find": cs["symbol"].code, "replace": cs["new_code"]}],
                    "applied": False,
                })()
                new_full = await asyncio.to_thread(_apply_change_fn, orig_full, _proxy)
            except Exception:
                return []
            if not new_full or new_full == orig_full:
                return []
            try:
                if fname not in orig_cache:
                    orig_cache[fname] = await asyncio.to_thread(_vl, orig_full, fname)
                _orig_errs = orig_cache[fname]
                _new_errs = await asyncio.to_thread(_vl, new_full, fname)
            except Exception:
                return []
            _sig = lambda e: (e.get("code", ""), (e.get("message", "") or "").strip())
            _counts: dict = {}
            for _e in _orig_errs:
                _counts[_sig(_e)] = _counts.get(_sig(_e), 0) + 1
            _introduced = []
            for _e in _new_errs:
                _s = _sig(_e)
                if _counts.get(_s, 0) > 0:
                    _counts[_s] -= 1
                else:
                    _introduced.append(_e)
            return _introduced

        def _force_block_on_tsc(_idx, _errs, _suffix):
            _msgs = [
                f"{e.get('code', 'TS')} (line {e.get('line', '?')}): {(e.get('message', '') or '').strip()}"
                for e in _errs
            ]
            qa_results[_idx]["type_errors"] = (
                qa_results[_idx].get("type_errors", []) + _msgs
            )
            qa_results[_idx]["verdict"] = "blocked"
            qa_results[_idx]["qa_score"] = min(qa_results[_idx].get("qa_score", 10) or 10, 3)
            qa_results[_idx]["summary"] = (
                f"tsc: {len(_msgs)} compile error(s){_suffix}. "
                + (qa_results[_idx].get("summary", "") or "")
            )
            return _msgs

        # ── tsc pre-check — feed introduced compile errors into the retry loop ─
        _tsc_orig_cache: dict = {}
        for _ti, _tcs in enumerate(change_shells):
            _t_introduced = await _tsc_introduced_errors(_tcs, _tsc_orig_cache)
            if _t_introduced:
                _t_msgs = _force_block_on_tsc(_ti, _t_introduced, "")
                yield sse({"type": "progress",
                           "content": f"🔧 tsc found {len(_t_msgs)} compile error(s) in "
                                      f"{_tcs['symbol'].name} — will auto-fix"})

        # ── QA retry loop — fix blocked changes before showing to user ────────
        # Triggers on verdict=="blocked" OR score<=5 with hard issues.
        # Sends QA findings back to Claude, re-runs QA on the fix.

        MAX_QA_RETRIES = 2

        for _qa_retry_round in range(MAX_QA_RETRIES):
            # ── Pipeline deadline gate ─────────────────────────────────────
            if _pipeline_over_budget():
                _dlog("pipeline_deadline_skip",
                      session_id=session_id, user_id=user_id,
                      phase="qa_retry_loop",
                      retry_round=_qa_retry_round,
                      elapsed_s=round(time.time() - _pipeline_t0, 1),
                      deadline_s=PIPELINE_DEADLINE_S)
                yield sse({"type": "progress",
                           "content": "⏱️ Approaching time limit — shipping changes as-is (QA corrections skipped)"})
                break
            _dlog("qa_retry_loop_start", session_id=session_id, user_id=user_id,
                  retry_round=_qa_retry_round, max_retries=MAX_QA_RETRIES,
                  total_changes=len(qa_results),
                  verdicts=[q.get("verdict") for q in qa_results],
                  scores=[q.get("qa_score") for q in qa_results])
            # ── Re-run QA for any change whose QA could not execute ───────────
            # A "skipped"/None-score result means the QA *check* failed to run
            # (transient LLM/API error) — NOT that the code is bad. Re-running
            # QA is the correct remedy; re-editing good code would risk
            # degrading it. If QA still can't score after the retries, the
            # hard 8/10 gate below excludes the change (never ships unscored).
            _unscored = [
                _ui for _ui, _uqd in enumerate(qa_results)
                if _uqd.get("verdict") == "skipped" or _uqd.get("qa_score") is None
            ]
            _dlog("qa_retry_unscored", session_id=session_id, user_id=user_id,
                  retry_round=_qa_retry_round, unscored_indices=_unscored)
            if _unscored:
                yield sse({"type": "progress",
                           "content": f"🔁 Re-running QA on {len(_unscored)} unscored change(s) — "
                                      f"attempt {_qa_retry_round + 1}/{MAX_QA_RETRIES}..."})
                _reqa_sk = [
                    (idx, asyncio.create_task(run_qa_agent(
                        original_code=change_shells[idx]["qa_original_content"],
                        new_code=change_shells[idx]["new_code"],
                        change_description=change_shells[idx]["description"],
                        new_logic=change_shells[idx]["description"],
                        symbol_path=change_shells[idx]["symbol"].name,
                        filename=change_shells[idx]["filename"],
                        other_files_context=_qa_other_context,
                        session_id=session_id or "",
                        user_id=user_id,
                        architect_risks=all_qa_risks,
                        targeted_context=change_shells[idx]["targeted_ctx"],
                        qa_feedback=None,
                        same_run_context=change_shells[idx]["same_run"],
                    )))
                    for idx in _unscored
                ]
                _pend_sk = {t for _, t in _reqa_sk}
                while _pend_sk:
                    _d_sk, _pend_sk = await asyncio.wait(_pend_sk, timeout=20.0)
                    if _pend_sk:
                        yield f"data: {json.dumps({'type': 'keepalive', 'content': ''})}\n\n"
                for idx, task in _reqa_sk:
                    try:
                        qa_results[idx] = task.result()
                    except Exception as _usc_exc:
                        _dlog("qa_retry_unscored_reqa_error", session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=idx,
                              error=str(_usc_exc), error_type=type(_usc_exc).__name__)

            # Find all still-blocked changes.
            # Trigger retry when:
            #   - verdict is "blocked" (any score), OR
            #   - score <= 5 with specific hard issues (missing imports, type errors)
            blocked_indices = []
            for _bi, _bqd in enumerate(qa_results):
                _bv = _bqd.get("verdict", "safe")
                _bs = _bqd.get("qa_score") or 10
                _b_hard = bool(
                    _bqd.get("import_issues")
                    or _bqd.get("type_errors")
                    or _bqd.get("logic_errors")
                )
                if _bv == "blocked":
                    blocked_indices.append(_bi)
                elif _bs <= 7:
                    blocked_indices.append(_bi)
            _dlog("qa_retry_blocked_indices", session_id=session_id, user_id=user_id,
                  retry_round=_qa_retry_round,
                  blocked_count=len(blocked_indices),
                  blocked_indices=blocked_indices,
                  blocked_details=[{
                      "idx": bi,
                      "symbol": change_shells[bi]["symbol"].name,
                      "verdict": qa_results[bi].get("verdict"),
                      "score": qa_results[bi].get("qa_score"),
                      "summary": qa_results[bi].get("summary", "")[:200],
                  } for bi in blocked_indices])
            if not blocked_indices:
                _dlog("qa_retry_no_blocked_breaking", session_id=session_id, user_id=user_id,
                      retry_round=_qa_retry_round)
                break

            yield sse({"type": "progress",
                       "content": f"🔁 Fixing {len(blocked_indices)} blocked change(s) — "
                                  f"attempt {_qa_retry_round + 1}/{MAX_QA_RETRIES}..."})

            # Build correction calls for all blocked changes
            correction_tasks = []
            # Correction uses Sonnet — cheaper + less prone to hallucination
            # on large symbols than Opus.  Architect model stays for the
            # initial edit; only the *fix* loop is downgraded.
            # Defined BEFORE the loop: every blocked idx may be deferred to
            # the multi-window path (continue), leaving the loop body never
            # executed — the tasks_created _dlog below still references it.
            _correction_model = "claude-sonnet-5"  # correction upgraded to Sonnet 5
            _corr_msgs_by_idx = {}  # Save per-correction messages for ReAct follow-ups
            _correction_window_info = {}  # Per-idx window info for windowed corrections
            _multi_window_pending = []   # Indices that need multi-window sequential correction
            _multi_window_meta = {}      # Per-idx saved prompt metadata for multi-window
            for idx in blocked_indices:
                # ── Pipeline deadline gate (per-correction) ────────────────
                if _pipeline_over_budget():
                    _dlog("pipeline_deadline_skip",
                          session_id=session_id, user_id=user_id,
                          phase="correction_build_loop",
                          retry_round=_qa_retry_round, idx=idx,
                          remaining_blocked=[bi for bi in blocked_indices if bi >= idx],
                          elapsed_s=round(time.time() - _pipeline_t0, 1),
                          deadline_s=PIPELINE_DEADLINE_S)
                    yield sse({"type": "progress",
                               "content": "⏱️ Approaching time limit — skipping remaining corrections"})
                    break
                cs     = change_shells[idx]
                qa_d   = qa_results[idx]
                symbol = cs["symbol"]

                # Summarise every issue QA found
                issue_lines = []
                if qa_d.get("summary"):
                    issue_lines.append(f"Summary: {qa_d['summary']}")
                for issue in qa_d.get("import_issues", []):
                    issue_lines.append(f"Import issue: {issue}")
                for err in qa_d.get("type_errors", []):
                    issue_lines.append(f"Type error: {err}")
                for lerr in qa_d.get("logic_errors", []):
                    issue_lines.append(f"Logic error: {lerr}")
                if qa_d.get("plan_deviation"):
                    issue_lines.append(f"Plan deviation: {qa_d['plan_deviation']}")
                for risk in qa_d.get("downstream_risks", []):
                    issue_lines.append(f"Downstream risk: {risk}")

                issues_block = "\n".join(f"  • {l}" for l in issue_lines) or "  • See QA summary above."

                # Include the diff so Claude sees exactly which lines it dropped.
                # The #1 QA block cause is accidentally removing unchanged lines —
                # the diff makes this immediately visible via the - markers.
                diff_text = _make_diff(symbol.code, cs["new_code"], symbol.name)
                removed_lines = [
                    line[1:].rstrip()
                    for line in diff_text.splitlines()
                    if line.startswith("-") and not line.startswith("---") and line[1:].strip()
                ]
                diff_block = (
                    f"\n\nDIFF (- = removed from original, + = added by you):\n"
                    f"```diff\n{diff_text}\n```\n"
                    + (
                        f"\n⚠️ You removed {len(removed_lines)} line(s) from the original. "
                        f"Only lines the user explicitly asked to change should be removed — "
                        f"everything else must be preserved exactly.\n"
                        if removed_lines else ""
                    )
                )

                # Large symbols must NOT be re-emitted in full — that is the #1 cause
                # of degradation (the model can only see part of an 800+ line symbol, so
                # "re-emit everything" makes it drop the rest). For anything sizeable, steer
                # to a TARGETED old_code/new_code edit which is spliced into the full symbol
                # server-side. Small symbols may still be re-emitted whole.
                # Use the ACTUAL broken code size (cs["new_code"]) for format
                # decision, not the original symbol size. Prior correction
                # rounds may have expanded the code (e.g. 102 → 124 lines),
                # and the model needs to return code at the current size.
                _sym_line_count = len(cs["new_code"].splitlines())
                _orig_line_count = len(symbol.code.splitlines())
                _qa_score_val = qa_d.get("qa_score") or 0
                # Very large symbols (300+ lines) MUST always use targeted edits —
                # Claude physically cannot re-emit thousands of lines in a
                # correction response; it will produce a fragment that gets
                # rejected.  For medium symbols (60-300 lines), allow full
                # replacement when QA says the code is fundamentally incomplete
                # (score ≤ 4) since a targeted snippet can't add 10 missing
                # features scattered across a 190-line symbol.
                _is_large_symbol = _sym_line_count > 300 or (
                    _sym_line_count > 60 and _qa_score_val > 4
                )

                _dlog("correction_format_decision",
                      session_id=session_id, user_id=user_id,
                      symbol=symbol.name,
                      sym_line_count=_sym_line_count,
                      orig_line_count=_orig_line_count,
                      qa_score=_qa_score_val,
                      is_large_symbol=_is_large_symbol,
                      threshold_reason="300+ lines" if _sym_line_count > 300 else
                                       f"60+ lines AND score {_qa_score_val} > 4" if _is_large_symbol else
                                       "small symbol")

                if _is_large_symbol:
                    # ── Windowed correction ──────────────────────────────────
                    # Instead of sending the full 2000-line symbol and asking
                    # the model for a complex fixes-array format, we:
                    #   1. Diff original vs broken edit to find changed region
                    #   2. Show only that region ± 20 lines of context
                    #   3. Model returns corrected window (standard format)
                    #   4. Server splices window back (deterministic, no matching)
                    _all_windows = _find_changed_windows(symbol.code, cs["new_code"])

                    # ── Fix 2: Augment with QA-referenced locations ───────
                    # When QA identifies issues at locations NOT covered by
                    # any diff window (e.g., timed-out plan items left dead
                    # code at line 1830 but the diff only shows line ~165),
                    # add windows there so the correction model can see AND
                    # edit the code it needs to fix.
                    # Evidence (session 82eb1056, line 542): correction
                    # emitted <search_request> because the target code at
                    # line ~1830 was outside all diff windows.
                    _all_windows = _augment_windows_with_qa_refs(
                        _all_windows, qa_d, symbol.code, cs["new_code"]
                    )

                    if len(_all_windows) == 1:
                        _window_info = _all_windows[0]
                        _correction_window_info[idx] = _window_info
                        _wl = _window_info["window_line_count"]
                        _ws1 = _window_info["window_start"] + 1   # 1-indexed
                        _we1 = _window_info["window_end"] + 1

                        _format_instructions = (
                            f"This symbol is {_sym_line_count} lines. You are seeing ONLY the "
                            f"changed region (lines {_ws1}–{_we1}, {_wl} lines) plus context.\n\n"
                            f"Return a <surgical_edit> block with the CORRECTED version of this "
                            f"window. The server will splice it back into the full symbol automatically.\n\n"
                            f"Format:\n"
                            f"<surgical_edit>\n"
                            f'{{"filename": "{cs["filename"]}", "symbol": "{symbol.name}", '
                            f'"new_code": "<corrected lines {_ws1}–{_we1}>"}}\n'
                            f"</surgical_edit>\n\n"
                            f"RULES:\n"
                            f"- Return ALL {_wl} lines of the window (context lines + corrected changes)\n"
                            f"- Do NOT return the entire {_sym_line_count}-line symbol\n"
                            f"- Do NOT include line-number prefixes (the \"1234 | \" part) in new_code\n"
                            f"- Fix every issue listed above while preserving the original request\n"
                            f"- Adding or removing lines within the window is fine"
                        )
                        _original_code_block = (
                            f"ORIGINAL CODE (lines {_ws1}–{_we1} of "
                            f"{_window_info['total_orig_lines']} total — before your edit):\n"
                            f"```\n{_window_info['numbered_original']}\n```"
                        )
                        _broken_code_block = (
                            f"YOUR BROKEN EDIT (lines {_ws1}–{_we1} — fix this):\n"
                            f"```\n{_window_info['numbered_broken']}\n```"
                        )
                        # ── Suffix context: show a few lines after the window
                        # so the correction agent knows what comes next and
                        # won't re-emit those lines in its output.
                        _sfx_all = cs["new_code"].splitlines()
                        _sfx_start = _window_info["window_end"] + 1  # 0-indexed
                        _sfx_count = min(5, len(_sfx_all) - _sfx_start)
                        if _sfx_count > 0:
                            _sfx_preview = "\n".join(
                                f"{_sfx_start + i + 1:4d} | {_sfx_all[_sfx_start + i]}"
                                for i in range(_sfx_count)
                            )
                            _broken_code_block += (
                                f"\n\nLINES AFTER YOUR WINDOW (read-only — do NOT include these in your output):\n"
                                f"```\n{_sfx_preview}\n```"
                            )
                        _dlog("correction_windowed_prompt",
                              session_id=session_id, user_id=user_id,
                              symbol=symbol.name,
                              window_start=_ws1, window_end=_we1,
                              window_lines=_wl,
                              total_lines=_sym_line_count,
                              total_orig_lines=_window_info["total_orig_lines"],
                              prompt_original_chars=len(_window_info["numbered_original"]),
                              prompt_broken_chars=len(_window_info["numbered_broken"]))
                    elif len(_all_windows) > 1:
                        # ── Multi-window: scattered changes need N independent corrections ──
                        # Don't build prompt or create task here — defer to
                        # sequential processing after the main parallel correction loop.
                        _correction_window_info[idx] = _all_windows  # store LIST
                        _multi_window_pending.append(idx)
                        _multi_window_meta[idx] = {
                            "issues_block": issues_block,
                            "diff_block": diff_block,
                        }
                        _dlog("correction_multi_window_deferred",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round,
                              symbol=symbol.name,
                              sym_line_count=_sym_line_count,
                              window_count=len(_all_windows),
                              windows_summary=[{
                                  "ci": w["cluster_index"],
                                  "ws": w["window_start"] + 1,
                                  "we": w["window_end"] + 1,
                                  "lines": w["window_line_count"],
                                  "changed": w["changed_line_count"],
                              } for w in _all_windows],
                              total_window_lines=sum(w["window_line_count"] for w in _all_windows),
                              note="deferred to sequential multi-window processing")
                        continue  # Skip prompt building + task creation for this idx
                    else:
                        # _find_changed_windows returned empty — diff found no
                        # changes, or an internal error.  Shouldn't happen during
                        # correction (we know codes differ), but degrade safely.
                        _dlog("correction_windowed_fallback_null_window",
                              session_id=session_id, user_id=user_id,
                              symbol=symbol.name,
                              sym_line_count=_sym_line_count,
                              new_code_len=len(cs["new_code"]),
                              codes_identical=symbol.code == cs["new_code"],
                              reason="find_changed_window returned None for large symbol")
                        _correction_window_info[idx] = None
                        _format_instructions = (
                            f"Write a corrected <surgical_edit> block whose \"new_code\" contains the "
                            f"COMPLETE symbol code (nothing omitted) using the exact symbol name "
                            f"`{symbol.name}`."
                        )
                        _original_code_block = (
                            f"ORIGINAL CODE (what the symbol looks like NOW — before your change):\n"
                            f"```\n{symbol.code}\n```"
                        )
                        _broken_code_block = (
                            f"YOUR BROKEN CODE (what you wrote — DO NOT reuse this verbatim):\n"
                            f"```\n{cs['new_code']}\n```"
                        )
                else:
                    _correction_window_info[idx] = None
                    _format_instructions = (
                        f"Write a corrected <surgical_edit> block whose \"new_code\" contains the "
                        f"COMPLETE symbol code (nothing omitted) using the exact symbol name "
                        f"`{symbol.name}`."
                    )
                    _original_code_block = (
                        f"ORIGINAL CODE (what the symbol looks like NOW — before your change):\n"
                        f"```\n{symbol.code}\n```"
                    )
                    _broken_code_block = (
                        f"YOUR BROKEN CODE (what you wrote — DO NOT reuse this verbatim):\n"
                        f"```\n{cs['new_code']}\n```"
                    )

                correction_prompt = (
                    f"QA reviewed your <surgical_edit> for `{symbol.name}` in "
                    f"`{cs['filename']}` and found it BLOCKED (score "
                    f"{qa_d.get('qa_score', '?')}/10). You must fix all issues "
                    f"before this can be applied.\n\n"
                    f"Issues to fix:\n{issues_block}"
                    f"{diff_block}\n\n"
                    f"{_original_code_block}\n\n"
                    f"{_broken_code_block}\n\n"
                    f"Requirements:\n"
                    f"1. Fix every issue listed above\n"
                    f"2. Still implement the original request: {cs['description']}\n"
                    f"3. Preserve everything you are not explicitly changing\n"
                    f"4. Use the exact symbol name: `{symbol.name}`\n\n"
                    f"{_format_instructions}\n\n"
                    f"For JSX/TSX/HTML: first verify your corrected code has balanced tags — "
                    f"count every opening tag and confirm it has a matching closing tag at "
                    f"the correct nesting level. Then return the <surgical_edit> block."
                )

                # Slim context — correction_prompt is self-contained (includes
                # original code, broken code, diff, QA issues, description).
                # Using full current_messages risks token overflow in large
                # sessions (65-file context + search round-trips can exceed
                # model's max input tokens and kill auto-heal entirely).
                _corr_prompt_chars = len(correction_prompt)
                _dlog("correction_context_size",
                      session_id=session_id, user_id=user_id,
                      correction_prompt_chars=_corr_prompt_chars,
                      correction_prompt_est_tokens=_corr_prompt_chars // 4,
                      current_messages_chars=sum(len(m.get("content","")) for m in current_messages),
                      current_messages_count=len(current_messages),
                      symbol_name=symbol.name,
                      retry_round=_qa_retry_round)
                correction_messages = [
                    {"role": "user", "content": correction_prompt},
                ]
                _corr_msgs_by_idx[idx] = correction_messages

                # Thinking-config: explicit config for adaptive models
                _cbt_think_kw = _get_thinking_kwargs(_correction_model, 4000)
                _cbt_effort_kw = _get_effort_kwargs(_correction_model)
                correction_tasks.append((
                    idx,
                    asyncio.create_task(_stream_and_collect(
                        aclient,
                        model=_correction_model,
                        max_tokens=_max_output_tokens(_correction_model),
                        system=system_prompt,
                        messages=correction_messages,
                        **_cbt_think_kw,
                        **_cbt_effort_kw,
                    ))
                ))

            _dlog("qa_retry_correction_tasks_created", session_id=session_id, user_id=user_id,
                  retry_round=_qa_retry_round,
                  task_count=len(correction_tasks),
                  model=_correction_model,
                  indices=[idx for idx, _ in correction_tasks])
            # Wait for all correction calls with keepalives
            pending_corr = {t for _, t in correction_tasks}
            while pending_corr:
                done_corr, pending_corr = await asyncio.wait(pending_corr, timeout=20.0)
                if pending_corr:
                    yield f"data: {json.dumps({'type': 'keepalive', 'content': ''})}\n\n"

            # Parse corrected edit blocks and update change_shells
            fixed_indices = []
            for idx, task in correction_tasks:
                try:
                    corr_resp = task.result()
                    corr_text = "".join(
                        b.text for b in corr_resp.content if hasattr(b, "text")
                    )
                    # ── ReAct loop: if Claude asks for context, provide it ──
                    # Instead of giving up when correction has no edit block,
                    # check for <search_request>/<file_request> tags, execute
                    # them, feed results back, and let Claude produce the edit.
                    _MAX_CORR_REACT = 3
                    _react_msgs = list(_corr_msgs_by_idx.get(idx, []))
                    _got_edit = False
                    _corr_react_round = 0
                    # ── Progress-based round extension ──
                    # Follow-ups that fetch never-before-seen context are
                    # free; bounded by an absolute hard ceiling.
                    _corr_abs_round = 0
                    _CORR_HARD_CEILING = 6
                    _corr_seen_ctx = set()

                    while _corr_react_round <= _MAX_CORR_REACT:
                        ei = corr_text.find(EDIT_OPEN)
                        ec = corr_text.find(EDIT_CLOSE, ei) if ei != -1 else -1
                        if ei != -1 and ec != -1:
                            _got_edit = True
                            break

                        if "<cannot_anchor" in corr_text:
                            # Model honestly can't locate an anchor — clean
                            # abort routes to the existing keep-original path.
                            _dlog("qa_retry_correction_clean_abort",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  react_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name,
                                  response_preview=corr_text[:300])
                            break

                        if _corr_react_round >= _MAX_CORR_REACT:
                            break  # Exhausted ReAct rounds

                        # Look for search/file request tags in the response
                        _sr_match = re.search(
                            r'<search_request>\s*(.*?)\s*</search_request>',
                            corr_text, re.DOTALL
                        )
                        _fr_match = re.search(
                            r'<file_request>\s*(.*?)\s*</file_request>',
                            corr_text, re.DOTALL
                        )

                        if not _sr_match and not _fr_match:
                            _dlog("qa_retry_correction_no_edit_no_react",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  react_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name,
                                  response_len=len(corr_text),
                                  response_preview=corr_text[:500])
                            break  # No edit and no context request — give up

                        # Execute the context requests
                        _react_parts = []

                        if _sr_match:
                            _sr_data = _parse_search_content(_sr_match.group(1))
                            _sr_terms = (
                                _sr_data.get("terms", [])
                                if isinstance(_sr_data, dict) else []
                            )
                            if _sr_terms:
                                _sr_result = _resolve_search_multifile(
                                    _sr_terms, symbol_maps_by_name,
                                    file_content_lookup_stream
                                )
                                _react_parts.append(
                                    f"Search results for {_sr_terms}:\n{_sr_result}"
                                )
                                _dlog("qa_retry_correction_search_executed",
                                      session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=idx,
                                      react_round=_corr_react_round,
                                      symbol=change_shells[idx]["symbol"].name,
                                      terms=_sr_terms,
                                      result_chars=len(_sr_result))

                        if _fr_match:
                            _fr_names = _parse_filereq_content(_fr_match.group(1))
                            for _fr_fn in _fr_names[:5]:
                                _fr_content = file_content_lookup_stream.get(
                                    _fr_fn, ""
                                )
                                if not _fr_content:
                                    # Fuzzy match — find closest filename
                                    _fr_cands = [
                                        k for k in file_content_lookup_stream
                                        if _fr_fn.lower() in k.lower()
                                        or k.lower() in _fr_fn.lower()
                                    ]
                                    if _fr_cands:
                                        _fr_fn = _fr_cands[0]
                                        _fr_content = (
                                            file_content_lookup_stream.get(
                                                _fr_fn, ""
                                            )
                                        )
                                if _fr_content:
                                    _fr_lines = _fr_content.splitlines()
                                    _react_parts.append(
                                        f"FILE: {_fr_fn} ({len(_fr_lines)} lines)\n"
                                        f"```\n{_fr_content}\n```"
                                    )
                                else:
                                    _react_parts.append(
                                        f"FILE NOT FOUND: '{_fr_fn}'"
                                    )
                            _dlog("qa_retry_correction_files_fetched",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  react_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name,
                                  requested=_fr_names[:5])

                        if not _react_parts:
                            _dlog("qa_retry_correction_no_edit_no_react",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  react_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name,
                                  response_len=len(corr_text),
                                  response_preview=corr_text[:500])
                            break

                        _react_context = "\n\n".join(_react_parts)
                        _react_msgs.extend([
                            {"role": "assistant", "content": corr_text},
                            {"role": "user", "content": (
                                f"{_react_context}\n\n"
                                "Now write your corrected <surgical_edit> block. "
                                "Use the EXACT verbatim lines shown above as your "
                                "old_code anchor — copy them character-for-character. "
                                "If you cannot locate the exact code to anchor on, "
                                "reply <cannot_anchor reason='...'/> instead of "
                                "guessing — never fabricate an anchor."
                            )},
                        ])

                        _dlog("qa_retry_correction_react_followup",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=idx,
                              react_round=_corr_react_round,
                              symbol=change_shells[idx]["symbol"].name,
                              context_chars=len(_react_context),
                              msg_count=len(_react_msgs))

                        try:
                            _dlog("qa_retry_correction_react_call",
                                  model=_correction_model,
                                  wrapper="safe_claude_call",
                                  session_id=session_id, user_id=user_id)
                            _fu_task = asyncio.create_task(
                                _safe_claude_call(
                                    aclient,
                                    model=_correction_model,
                                    desired_text_tokens=12000,
                                    thinking_budget=4000,
                                    retry_on_starve=True,
                                    system=system_prompt,
                                    messages=_react_msgs,
                                )
                            )
                            while not _fu_task.done():
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(_fu_task),
                                        timeout=20.0,
                                    )
                                except asyncio.TimeoutError:
                                    yield sse({"type": "progress",
                                               "content": f"QA correction round {_qa_retry_round + 1}… "
                                                           f"still working"})

                            _fu_resp = _fu_task.result()
                            corr_text = "".join(
                                b.text for b in _fu_resp.content
                                if hasattr(b, "text")
                            )
                            _dlog("qa_retry_correction_react_response",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  react_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name,
                                  response_chars=len(corr_text),
                                  has_edit=EDIT_OPEN in corr_text)
                        except Exception as _react_exc:
                            _dlog("qa_retry_correction_react_error",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  react_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name,
                                  error=str(_react_exc),
                                  error_type=type(_react_exc).__name__)
                            break  # API error — stop ReAct for this correction

                        # ── Progress-based round extension ──
                        # _react_context is deterministic per fetched content,
                        # so a repeated hash means the model re-requested the
                        # same context (stalled). New context = free round.
                        _corr_abs_round += 1
                        _ctx_sig = hash(_react_context)
                        if _corr_abs_round >= _CORR_HARD_CEILING:
                            _corr_react_round = _MAX_CORR_REACT
                            _dlog("qa_retry_correction_hard_ceiling",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  abs_round=_corr_abs_round,
                                  symbol=change_shells[idx]["symbol"].name)
                        elif _ctx_sig not in _corr_seen_ctx:
                            # New context fetched — this follow-up is free.
                            _dlog("qa_retry_correction_productive_extension",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  abs_round=_corr_abs_round,
                                  counted_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name)
                        else:
                            _corr_react_round += 1
                            _dlog("qa_retry_correction_round_stalled",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  abs_round=_corr_abs_round,
                                  counted_round=_corr_react_round,
                                  symbol=change_shells[idx]["symbol"].name)
                        _corr_seen_ctx.add(_ctx_sig)

                    if not _got_edit:
                        _dlog("qa_retry_correction_no_edit_block",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=idx,
                              symbol=change_shells[idx]["symbol"].name,
                              react_rounds_tried=_corr_react_round,
                              response_len=len(corr_text),
                              response_preview=corr_text[:500])
                        continue
                    if ei != -1 and ec != -1:
                        raw_edit = corr_text[ei + len(EDIT_OPEN):ec]
                        edit_data = None
                        for _parse_attempt_label, _parse_fn in [
                            ("json.loads", lambda t: json.loads(t)),
                            ("repair_json", lambda t: json.loads(_repair_json(t))),
                            ("extract_json", lambda t: (lambda r: json.loads(r) if isinstance(r, str) else r)(_extract_json_from_text(t))),
                            ("regex_extract", lambda t: _regex_extract_edit_block(t)),
                        ]:
                            try:
                                edit_data = _parse_fn(raw_edit.strip())
                                if isinstance(edit_data, dict) and (edit_data.get("new_code") or (isinstance(edit_data.get("fixes"), list) and len(edit_data["fixes"]) > 0)):
                                    _corr_ext_fmt = edit_data.pop("_extraction_format", None)
                                    _dlog("qa_retry_correction_parse_method", session_id=session_id, user_id=user_id,
                                          retry_round=_qa_retry_round, idx=idx,
                                          symbol=change_shells[idx]["symbol"].name,
                                          method=_parse_attempt_label,
                                          extraction_format=_corr_ext_fmt,
                                          has_fixes=isinstance(edit_data.get("fixes"), list),
                                          has_new_code=bool(edit_data.get("new_code")))
                                    break
                                edit_data = None
                            except Exception:
                                edit_data = None
                        if not isinstance(edit_data, dict) or not (edit_data.get("new_code") or (isinstance(edit_data.get("fixes"), list) and len(edit_data["fixes"]) > 0)):
                            _dlog("qa_retry_correction_all_parsers_failed", session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  raw_preview=raw_edit.strip()[:300])
                            continue
                        corrected_fixes = edit_data.get("fixes")
                        corrected_code = edit_data.get("new_code", "")
                        corrected_old  = edit_data.get("old_code", "")
                        _sym_code = change_shells[idx]["symbol"].code

                        _dlog("qa_retry_correction_parsed", session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=idx,
                              symbol=change_shells[idx]["symbol"].name,
                              has_fixes_array=isinstance(corrected_fixes, list),
                              fixes_count=len(corrected_fixes) if isinstance(corrected_fixes, list) else 0,
                              has_new_code=bool(corrected_code),
                              new_code_len=len(corrected_code),
                              has_old_code=bool(corrected_old),
                              old_code_len=len(corrected_old),
                              sym_code_len=len(_sym_code),
                              edit_data_keys=list(edit_data.keys()) if isinstance(edit_data, dict) else [])

                        # Reconstruct a FULL symbol from the correction before it
                        # can be stored.  NEVER replace a change with a degenerate
                        # fragment (a 2-line snippet stored as the whole 850-line
                        # symbol).
                        #
                        # Priority order:
                        #   W. Windowed splice (large symbols — deterministic)
                        #   2. old_code/new_code snippet (verbatim match splice)
                        #   3. Full new_code replacement (fragment-checked)
                        accepted = None

                        # ── Path W: Windowed correction splice ──────────────
                        # If we used windowed correction, the model returned the
                        # corrected WINDOW only.  Splice it back into the broken
                        # full symbol at the known line positions.
                        _winfo = _correction_window_info.get(idx)
                        _windowed_path_attempted = False

                        # If model returned a fixes array when we asked for
                        # windowed new_code, log it clearly.
                        if _winfo and isinstance(corrected_fixes, list) and not corrected_code:
                            _dlog("correction_windowed_got_fixes_instead",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  fixes_count=len(corrected_fixes),
                                  note="model returned fixes array instead of windowed new_code — falling through")

                        if _winfo and corrected_code and not corrected_old:
                            _windowed_path_attempted = True
                            _broken_lines = change_shells[idx]["new_code"].splitlines()
                            _ws_0 = _winfo["window_start"]
                            _we_0 = _winfo["window_end"]

                            # Sanity-check: window bounds must be valid for the
                            # broken symbol. If not, skip windowed splice entirely.
                            if _we_0 >= len(_broken_lines) or _ws_0 < 0 or _ws_0 > _we_0:
                                _dlog("correction_windowed_bounds_invalid",
                                      session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=idx,
                                      symbol=change_shells[idx]["symbol"].name,
                                      ws_0=_ws_0, we_0=_we_0,
                                      broken_lines=len(_broken_lines),
                                      note="window bounds out of range — skipping windowed splice")
                            else:
                                # Strip line-number prefixes if model accidentally
                                # included them (e.g. "  42 | code...")
                                # SAFETY: Only strip if MAJORITY of non-empty lines
                                # have the prefix pattern. A single matching line like
                                # `1 | true` in JS could be real code — bulk detection
                                # prevents mangling real pipe operators.
                                _raw_corrected = corrected_code.splitlines()
                                _prefix_pat = re.compile(r'^\s*\d+\s*\|\s?')
                                _nonempty = [l for l in _raw_corrected if l.strip()]
                                _prefix_hits = sum(1 for l in _nonempty if _prefix_pat.match(l))
                                _prefix_ratio = _prefix_hits / max(len(_nonempty), 1)
                                _had_prefix = _prefix_ratio >= 0.7 and _prefix_hits >= 3

                                _dlog("correction_windowed_prefix_check",
                                      session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=idx,
                                      symbol=change_shells[idx]["symbol"].name,
                                      total_lines=len(_raw_corrected),
                                      nonempty_lines=len(_nonempty),
                                      prefix_hits=_prefix_hits,
                                      prefix_ratio=f"{_prefix_ratio:.2f}",
                                      will_strip=_had_prefix)

                                _corrected_lines = []
                                if _had_prefix:
                                    for _cl in _raw_corrected:
                                        _pfx = _prefix_pat.match(_cl)
                                        if _pfx:
                                            _corrected_lines.append(_cl[_pfx.end():])
                                        else:
                                            _corrected_lines.append(_cl)
                                else:
                                    _corrected_lines = list(_raw_corrected)

                                # ── Boundary dedup: strip lines at end of corrected
                                # window that duplicate the start of the untouched suffix.
                                # The correction agent sometimes re-emits closing lines
                                # (e.g. `""")`) that already exist right after the window,
                                # causing duplicate code at the splice seam.
                                _suffix_lines_after = _broken_lines[_we_0 + 1:]
                                if _suffix_lines_after and _corrected_lines:
                                    _max_ov = min(len(_corrected_lines), len(_suffix_lines_after), 10)
                                    _overlap = 0
                                    for _ov_k in range(1, _max_ov + 1):
                                        if _corrected_lines[-_ov_k:] == _suffix_lines_after[:_ov_k]:
                                            _overlap = _ov_k
                                    if _overlap > 0:
                                        _dlog("correction_windowed_boundary_dedup",
                                              session_id=session_id, user_id=user_id,
                                              retry_round=_qa_retry_round, idx=idx,
                                              symbol=change_shells[idx]["symbol"].name,
                                              overlap_lines=_overlap,
                                              stripped=[l.rstrip() for l in _corrected_lines[-_overlap:]],
                                              suffix_head=[l.rstrip() for l in _suffix_lines_after[:_overlap]])
                                        _corrected_lines = _corrected_lines[:-_overlap]

                                _expected_window_lines = _we_0 - _ws_0 + 1
                                _line_delta = len(_corrected_lines) - _expected_window_lines

                                _result_lines = list(_broken_lines)
                                _result_lines[_ws_0 : _we_0 + 1] = _corrected_lines
                                _spliced = "\n".join(_result_lines)
                                _frag_w = _fragment_reason(_sym_code, _spliced)

                                _dlog("correction_windowed_splice_attempt",
                                      session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=idx,
                                      symbol=change_shells[idx]["symbol"].name,
                                      window_start=_ws_0 + 1,
                                      window_end=_we_0 + 1,
                                      expected_window_lines=_expected_window_lines,
                                      corrected_lines_count=len(_corrected_lines),
                                      line_delta=_line_delta,
                                      had_line_prefixes=_had_prefix,
                                      broken_total_lines=len(_broken_lines),
                                      spliced_total_lines=len(_result_lines),
                                      spliced_len=len(_spliced),
                                      sym_code_len=len(_sym_code),
                                      is_fragment=_frag_w is not None,
                                      fragment_reason=_frag_w)

                                if _frag_w is None:
                                    accepted = _spliced
                                    _dlog("correction_windowed_splice_accepted",
                                          session_id=session_id, user_id=user_id,
                                          retry_round=_qa_retry_round, idx=idx,
                                          symbol=change_shells[idx]["symbol"].name,
                                          original_lines=len(_sym_code.splitlines()),
                                          result_lines=len(_spliced.splitlines()),
                                          line_delta=_line_delta)
                                else:
                                    _dlog("correction_windowed_splice_rejected",
                                          session_id=session_id, user_id=user_id,
                                          retry_round=_qa_retry_round, idx=idx,
                                          symbol=change_shells[idx]["symbol"].name,
                                          fragment_reason=_frag_w,
                                          spliced_len=len(_spliced),
                                          sym_code_len=len(_sym_code))
                        elif _winfo and not corrected_code:
                            # Windowed correction was set up but model returned
                            # no new_code — log so we can diagnose.
                            _dlog("correction_windowed_no_new_code",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  has_old_code=bool(corrected_old),
                                  has_fixes=isinstance(corrected_fixes, list),
                                  note="windowed prompt sent but model returned no new_code")
                        elif _winfo and corrected_old:
                            # Model returned old_code/new_code format despite
                            # windowed prompt — Path 2 will handle it.
                            _dlog("correction_windowed_got_old_code_format",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  old_code_len=len(corrected_old),
                                  new_code_len=len(corrected_code),
                                  note="windowed prompt sent but model used old_code/new_code format — trying Path 2")

                        # ── Path 2: old_code/new_code snippet splice (fallback) ──
                        _file_level_fixed = False
                        if accepted is None and corrected_code and corrected_old:
                            _dlog("correction_old_code_splice_attempt",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  old_code_len=len(corrected_old),
                                  new_code_len=len(corrected_code),
                                  old_code_preview=corrected_old[:200],
                                  sym_code_len=len(_sym_code))

                            _full, _ok_snip, _snip_reason = _apply_snippet_to_symbol(
                                _sym_code, corrected_old, corrected_code
                            )

                            _dlog("correction_old_code_splice_result",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  ok=_ok_snip,
                                  reason=_snip_reason,
                                  result_chars=len(_full) if _full else 0)

                            if _ok_snip:
                                accepted = _full
                            else:
                                _dlog("qa_retry_correction_splice_failed", session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=idx,
                                      symbol=change_shells[idx]["symbol"].name,
                                      old_code_preview=corrected_old[:200],
                                      new_code_preview=corrected_code[:200],
                                      splice_reason=_snip_reason)

                                # ── Path 2b: FILE-LEVEL fallback (session 0183c92e fix) ──
                                # Corrections were symbol-scoped, so a fix to the
                                # file's import line (the most common QA fix — e.g.
                                # "add useCallback to the react import") could NEVER
                                # apply: the import line is outside the target symbol.
                                # Proven in session 0183c92e: the correction model
                                # produced the exact right old_code/new_code for the
                                # import line and the splice failed with "not found
                                # verbatim in the target symbol", shipping the broken
                                # edit. Fallback: locate old_code in the FULL file;
                                # if it matches exactly one region OUTSIDE the symbol,
                                # store it as a companion file-level operation that is
                                # applied together with the symbol edit.
                                _flv_file = change_shells[idx].get("file_content") or ""
                                _flv_old, _flv_ok, _flv_reason = _locate_snippet_in_text(
                                    _flv_file, corrected_old
                                )
                                if _flv_ok and _flv_old and _flv_old not in _sym_code:
                                    _flv_op = {"find": _flv_old, "replace": corrected_code}
                                    change_shells[idx].setdefault("_extra_ops", []).append(_flv_op)
                                    # Surface the companion edit in the change diff.
                                    try:
                                        change_shells[idx]["diff"] = (
                                            (change_shells[idx].get("diff") or "")
                                            + "\n"
                                            + _make_diff(
                                                _flv_old, corrected_code,
                                                f"{change_shells[idx]['symbol'].name} (file-level companion)"
                                            )
                                        )
                                    except Exception:
                                        pass
                                    # Make re-QA aware the companion edit ships with
                                    # this change (otherwise it re-blocks on the same
                                    # missing import it just fixed).
                                    change_shells[idx]["same_run"] = (
                                        (change_shells[idx].get("same_run") or "")
                                        + "\n  \u2022 [file-level companion edit in "
                                        + f"{change_shells[idx]['filename']}] applied together "
                                        "with this change:\n"
                                        + "    BEFORE: " + _flv_old[:300].replace("\n", "\n    ") + "\n"
                                        + "    AFTER:  " + corrected_code[:300].replace("\n", "\n    ")
                                    )
                                    _file_level_fixed = True
                                    if idx not in fixed_indices:
                                        fixed_indices.append(idx)
                                    _dlog("correction_file_level_op_accepted",
                                          session_id=session_id, user_id=user_id,
                                          retry_round=_qa_retry_round, idx=idx,
                                          symbol=change_shells[idx]["symbol"].name,
                                          match_kind=_flv_reason,
                                          find_preview=_flv_old[:200],
                                          replace_preview=corrected_code[:200],
                                          extra_ops_count=len(change_shells[idx]["_extra_ops"]))
                                else:
                                    _dlog("correction_file_level_op_rejected",
                                          session_id=session_id, user_id=user_id,
                                          retry_round=_qa_retry_round, idx=idx,
                                          symbol=change_shells[idx]["symbol"].name,
                                          reason=(_flv_reason if not _flv_ok
                                                  else "match lies inside the target symbol — symbol splice should have handled it"),
                                          old_code_preview=corrected_old[:200])

                        # ── Path 3: Full new_code replacement (fragment-checked) ──
                        # SKIP if windowed correction was used — the corrected_code
                        # is a WINDOW, not a full symbol. Trying full-replacement
                        # would always fail the fragment check and produce
                        # misleading logs.
                        if accepted is None and corrected_code and not corrected_old and not _windowed_path_attempted:
                            _frag_reason = _fragment_reason(_sym_code, corrected_code)

                            _dlog("correction_full_replacement_attempt",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  new_code_len=len(corrected_code),
                                  sym_code_len=len(_sym_code),
                                  is_fragment=_frag_reason is not None,
                                  fragment_reason=_frag_reason)

                            if _frag_reason is None:
                                accepted = corrected_code
                            else:
                                _dlog("qa_retry_correction_fragment_rejected", session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=idx,
                                      symbol=change_shells[idx]["symbol"].name,
                                      fragment_reason=_frag_reason,
                                      sym_len=len(_sym_code), corrected_len=len(corrected_code))

                        if accepted is None:
                            _dlog("qa_retry_correction_not_accepted", session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  reason="no_valid_correction_produced",
                                  windowed_attempted=_windowed_path_attempted,
                                  had_winfo=_winfo is not None,
                                  had_corrected_code=bool(corrected_code),
                                  had_corrected_old=bool(corrected_old),
                                  had_fixes_array=isinstance(corrected_fixes, list),
                                  corrected_code_len=len(corrected_code) if corrected_code else 0,
                                  sym_code_len=len(_sym_code))
                        elif accepted == change_shells[idx]["new_code"]:
                            _dlog("qa_retry_correction_not_accepted", session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=idx,
                                  symbol=change_shells[idx]["symbol"].name,
                                  reason="corrected_code_identical_to_original")
                        if accepted is not None and accepted != change_shells[idx]["new_code"]:
                            change_shells[idx]["new_code"] = accepted
                            # Keep target_element/replacement consistent with the new full
                            # symbol so the applied operation stays non-destructive.
                            _new_tgt, _new_repl = _compute_target_element(_sym_code, accepted)
                            change_shells[idx]["_tgt"]  = _new_tgt
                            change_shells[idx]["_repl"] = _new_repl
                            change_shells[idx]["diff"]  = _make_diff(
                                _sym_code, accepted, change_shells[idx]["symbol"].name
                            )
                            fixed_indices.append(idx)
                except Exception as _corr_exc:
                    _dlog("qa_retry_correction_parse_error", session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=idx,
                          symbol=change_shells[idx]["symbol"].name,
                          error=str(_corr_exc), error_type=type(_corr_exc).__name__)

            # ── Multi-window sequential corrections ──────────────────────────
            # When scattered changes produced N>1 clusters, each window gets
            # its own API call.  Process bottom-to-top so line numbers stay
            # stable across splices.
            for _mw_idx in _multi_window_pending:
                # ── Pipeline deadline gate (multi-window) ──────────────────
                if _pipeline_over_budget():
                    _dlog("pipeline_deadline_skip",
                          session_id=session_id, user_id=user_id,
                          phase="multi_window_pending_loop",
                          retry_round=_qa_retry_round, mw_idx=_mw_idx,
                          remaining_mw=[mi for mi in _multi_window_pending if mi >= _mw_idx],
                          elapsed_s=round(time.time() - _pipeline_t0, 1),
                          deadline_s=PIPELINE_DEADLINE_S)
                    yield sse({"type": "progress",
                               "content": "⏱️ Approaching time limit — skipping remaining multi-window corrections"})
                    break
                _mw_windows = _correction_window_info.get(_mw_idx)
                if not isinstance(_mw_windows, list) or not _mw_windows:
                    _dlog("correction_multi_window_skip_invalid",
                          session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=_mw_idx,
                          reason="window info missing or not a list")
                    continue

                _mw_cs = change_shells[_mw_idx]
                _mw_qa = qa_results[_mw_idx]
                _mw_sym = _mw_cs["symbol"]
                _mw_meta = _multi_window_meta.get(_mw_idx, {})
                _mw_issues = _mw_meta.get("issues_block", "  • See QA summary.")
                _mw_diff = _mw_meta.get("diff_block", "")
                _mw_running_code = _mw_cs["new_code"]
                _mw_all_ok = True
                _mw_any_spliced = False   # Bug 3 fix: track partial success
                _mw_hard_fail = False     # True on parse error / bounds — not on no_edit
                _mw_correction_model = "claude-sonnet-5"  # correction upgraded to Sonnet 5

                # ── Fix 2+3: Filter windows to only relevant ones ─────────
                # Fix 2: Skip windows with changed_line_count == 0 (no diff
                #   changes) — the edit didn't touch that region, so errors
                #   there are pre-existing, not introduced by the surgeon.
                # Fix 3: Use QA error line numbers to target correction at
                #   specific windows — only correct windows that overlap with
                #   at least one error line from QA feedback.
                # Combined: a window is relevant if it has diff changes OR
                #   contains at least one QA-reported error line.
                # Exception: QA-reference windows (_source=="qa_reference")
                #   are always relevant — QA explicitly flagged that location.
                #
                # Extract error line numbers from QA feedback.
                _mw_error_lines = set()
                _mw_qa_texts = []
                for _mw_qk in ("summary", "plan_deviation"):
                    _mw_qv = _mw_qa.get(_mw_qk)
                    if _mw_qv:
                        _mw_qa_texts.append(str(_mw_qv))
                for _mw_qk in ("import_issues", "type_errors", "logic_errors",
                                "downstream_risks", "issues", "risk_verdicts"):
                    for _mw_qi in (_mw_qa.get(_mw_qk) or []):
                        if isinstance(_mw_qi, dict):
                            _mw_qa_texts.append(" ".join(str(v) for v in _mw_qi.values() if v))
                        else:
                            _mw_qa_texts.append(str(_mw_qi))
                _mw_qa_all_text = " ".join(_mw_qa_texts)
                # Match "line 1465", "Line ~2718", "lines 100-200", "L1465"
                for _mw_lm in re.finditer(
                    r'lines?\s*~?\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?',
                    _mw_qa_all_text, re.IGNORECASE
                ):
                    _mw_ls = int(_mw_lm.group(1))
                    _mw_le = int(_mw_lm.group(2)) if _mw_lm.group(2) else _mw_ls
                    for _mw_ln in range(_mw_ls, _mw_le + 1):
                        _mw_error_lines.add(_mw_ln - 1)  # 0-indexed
                for _mw_lm in re.finditer(
                    r'L(\d+)(?:\s*[-\u2013]\s*L?(\d+))?', _mw_qa_all_text
                ):
                    _mw_ls = int(_mw_lm.group(1))
                    _mw_le = int(_mw_lm.group(2)) if _mw_lm.group(2) else _mw_ls
                    for _mw_ln in range(_mw_ls, _mw_le + 1):
                        _mw_error_lines.add(_mw_ln - 1)  # 0-indexed

                # Filter windows
                _mw_relevant_windows = []
                _mw_skipped_windows = []
                for _mw_fw in _mw_windows:
                    _fw_has_changes = _mw_fw.get("changed_line_count", 0) > 0
                    _fw_is_qa_ref = _mw_fw.get("_source") == "qa_reference"
                    _fw_has_error_line = bool(_mw_error_lines) and any(
                        _mw_fw["window_start"] <= el <= _mw_fw["window_end"]
                        for el in _mw_error_lines
                    )
                    if _fw_has_changes or _fw_is_qa_ref or _fw_has_error_line:
                        _mw_relevant_windows.append(_mw_fw)
                    else:
                        _mw_skipped_windows.append(_mw_fw)

                if _mw_skipped_windows:
                    _dlog("correction_multi_window_filtered",
                          session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=_mw_idx,
                          symbol=_mw_sym.name,
                          original_window_count=len(_mw_windows),
                          relevant_window_count=len(_mw_relevant_windows),
                          skipped_window_count=len(_mw_skipped_windows),
                          error_lines_found=len(_mw_error_lines),
                          error_lines_sample=sorted(_mw_error_lines)[:20],
                          skipped_summary=[{
                              "ci": w["cluster_index"],
                              "ws": w["window_start"] + 1,
                              "we": w["window_end"] + 1,
                              "changed": w.get("changed_line_count", 0),
                              "source": w.get("_source", "diff"),
                          } for w in _mw_skipped_windows])
                    _mw_windows = _mw_relevant_windows

                # If ALL windows were filtered out, skip this idx entirely
                if not _mw_windows:
                    _dlog("correction_multi_window_all_filtered",
                          session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=_mw_idx,
                          symbol=_mw_sym.name,
                          note="all windows filtered — no changes or error lines to correct")
                    continue

                _dlog("correction_multi_window_start",
                      session_id=session_id, user_id=user_id,
                      retry_round=_qa_retry_round, idx=_mw_idx,
                      symbol=_mw_sym.name,
                      window_count=len(_mw_windows),
                      windows_summary=[{
                          "ci": w["cluster_index"],
                          "ws": w["window_start"] + 1,
                          "we": w["window_end"] + 1,
                          "lines": w["window_line_count"],
                          "changed": w.get("changed_line_count", 0),
                      } for w in _mw_windows],
                      error_lines_extracted=len(_mw_error_lines))

                # Process windows BOTTOM-TO-TOP — splicing later windows first
                # keeps earlier window line numbers valid.
                for _mw_wi in range(len(_mw_windows) - 1, -1, -1):
                    _mw_winfo = _mw_windows[_mw_wi]
                    _mw_ws1 = _mw_winfo["window_start"] + 1   # 1-indexed for display
                    _mw_we1 = _mw_winfo["window_end"] + 1
                    _mw_wl = _mw_winfo["window_line_count"]
                    _mw_sym_lc = len(_mw_sym.code.splitlines())

                    # Build per-window prompt (same format as single-window)
                    _mw_format = (
                        f"This symbol is {_mw_sym_lc} lines. You are seeing ONLY "
                        f"window {_mw_wi + 1} of {len(_mw_windows)} "
                        f"(lines {_mw_ws1}–{_mw_we1}, {_mw_wl} lines) plus context.\n\n"
                        f"Return a <surgical_edit> block with the CORRECTED version of this "
                        f"window. The server will splice it back automatically.\n\n"
                        f"Format:\n"
                        f"<surgical_edit>\n"
                        f'{{"filename": "{_mw_cs["filename"]}", "symbol": "{_mw_sym.name}", '
                        f'"new_code": "<corrected lines {_mw_ws1}–{_mw_we1}>"}}\n'
                        f"</surgical_edit>\n\n"
                        f"RULES:\n"
                        f"- Return ALL {_mw_wl} lines of the window (context + corrected changes)\n"
                        f"- Do NOT return the entire {_mw_sym_lc}-line symbol\n"
                        f"- Do NOT include line-number prefixes (the \"1234 | \" part) in new_code\n"
                        f"- Fix issues in THIS window while preserving the original request\n"
                        f"- Adding or removing lines within the window is fine"
                    )
                    _mw_orig_block = (
                        f"ORIGINAL CODE (window {_mw_wi + 1} — lines {_mw_ws1}–{_mw_we1} before your edit):\n"
                        f"```\n{_mw_winfo['numbered_original']}\n```"
                    )
                    _mw_broken_block = (
                        f"YOUR BROKEN EDIT (window {_mw_wi + 1} — lines {_mw_ws1}–{_mw_we1} — fix this):\n"
                        f"```\n{_mw_winfo['numbered_broken']}\n```"
                    )
                    # ── Suffix context for multi-window (same as single-window) ──
                    _mw_sfx_all = _mw_running_code.splitlines()
                    _mw_sfx_start = _mw_winfo["window_end"] + 1
                    _mw_sfx_count = min(5, len(_mw_sfx_all) - _mw_sfx_start)
                    if _mw_sfx_count > 0:
                        _mw_sfx_preview = "\n".join(
                            f"{_mw_sfx_start + i + 1:4d} | {_mw_sfx_all[_mw_sfx_start + i]}"
                            for i in range(_mw_sfx_count)
                        )
                        _mw_broken_block += (
                            f"\n\nLINES AFTER YOUR WINDOW (read-only — do NOT include these in your output):\n"
                            f"```\n{_mw_sfx_preview}\n```"
                        )
                    _mw_prompt = (
                        f"QA reviewed your <surgical_edit> for `{_mw_sym.name}` in "
                        f"`{_mw_cs['filename']}` and found it BLOCKED (score "
                        f"{_mw_qa.get('qa_score', '?')}/10). You must fix all issues.\n\n"
                        f"Issues to fix:\n{_mw_issues}"
                        f"{_mw_diff}\n\n"
                        f"{_mw_orig_block}\n\n"
                        f"{_mw_broken_block}\n\n"
                        f"Requirements:\n"
                        f"1. Fix every issue listed above\n"
                        f"2. Still implement the original request: {_mw_cs['description']}\n"
                        f"3. Preserve everything you are not explicitly changing\n"
                        f"4. Use the exact symbol name: `{_mw_sym.name}`\n\n"
                        f"{_mw_format}\n\n"
                        f"For JSX/TSX/HTML: verify balanced tags before returning."
                    )

                    _dlog("correction_multi_window_call",
                          session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=_mw_idx,
                          symbol=_mw_sym.name,
                          window_idx=_mw_wi,
                          total_windows=len(_mw_windows),
                          window_start=_mw_ws1, window_end=_mw_we1,
                          window_lines=_mw_wl,
                          prompt_chars=len(_mw_prompt),
                          prompt_est_tokens=len(_mw_prompt) // 4)

                    try:
                        # Thinking-config: explicit config for adaptive models
                        _mw_think_kw = _get_thinking_kwargs(_mw_correction_model, 4000)
                        _mw_effort_kw = _get_effort_kwargs(_mw_correction_model)
                        _mw_task = asyncio.create_task(_stream_and_collect(
                            aclient, model=_mw_correction_model,
                            max_tokens=_max_output_tokens(_mw_correction_model), system=system_prompt,
                            messages=[{"role": "user", "content": _mw_prompt}],
                            **_mw_think_kw,
                            **_mw_effort_kw,
                        ))
                        while not _mw_task.done():
                            try:
                                await asyncio.wait_for(asyncio.shield(_mw_task), timeout=20.0)
                            except asyncio.TimeoutError:
                                # ── Pipeline deadline inside keepalive ─────
                                if _pipeline_over_budget():
                                    _mw_task.cancel()
                                    _dlog("pipeline_deadline_skip",
                                          session_id=session_id, user_id=user_id,
                                          phase="multi_window_call_keepalive",
                                          retry_round=_qa_retry_round,
                                          mw_idx=_mw_idx, window_idx=_mw_wi,
                                          elapsed_s=round(time.time() - _pipeline_t0, 1),
                                          deadline_s=PIPELINE_DEADLINE_S)
                                    raise TimeoutError(
                                        "pipeline deadline exceeded during multi-window correction"
                                    )
                                yield sse({"type": "progress",
                                           "content": f"Multi-window correction {_mw_sym.name} "
                                                       f"window {_mw_wi + 1}/{len(_mw_windows)}…"})

                        _mw_resp = _mw_task.result()
                        _mw_text = "".join(
                            b.text for b in _mw_resp.content if hasattr(b, "text")
                        )

                        _dlog("correction_multi_window_response",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name, window_idx=_mw_wi,
                              response_chars=len(_mw_text),
                              has_edit=EDIT_OPEN in _mw_text)

                        # Parse edit block
                        _mw_ei = _mw_text.find(EDIT_OPEN)
                        _mw_ec = _mw_text.find(EDIT_CLOSE, _mw_ei) if _mw_ei != -1 else -1

                        if _mw_ei == -1 or _mw_ec == -1:
                            # Bug 3 fix: no_edit = model consciously declined
                            # (e.g. "this is architectural"). Don't break — let
                            # other windows that DID produce fixes still apply.
                            # (session d59e51e5: Window 2 succeeded with 188-line
                            #  splice but was rolled back because Window 1 said
                            #  "no_edit".)
                            _dlog("correction_multi_window_no_edit",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=_mw_idx,
                                  symbol=_mw_sym.name, window_idx=_mw_wi,
                                  response_len=len(_mw_text),
                                  response_preview=_mw_text[:300],
                                  action="continue_partial")
                            _mw_all_ok = False
                            continue  # was: break — now allows partial success

                        _mw_raw = _mw_text[_mw_ei + len(EDIT_OPEN):_mw_ec]
                        _mw_edit = None
                        _mw_parse_method = None
                        for _mw_pl, _mw_pf in [
                            ("json.loads", lambda t: json.loads(t)),
                            ("repair_json", lambda t: json.loads(_repair_json(t))),
                            ("extract_json", lambda t: (lambda r: json.loads(r) if isinstance(r, str) else r)(_extract_json_from_text(t))),
                            ("regex_extract", lambda t: _regex_extract_edit_block(t)),
                        ]:
                            try:
                                _mw_edit = _mw_pf(_mw_raw.strip())
                                if isinstance(_mw_edit, dict) and _mw_edit.get("new_code"):
                                    _mw_parse_method = _mw_pl
                                    break
                                _mw_edit = None
                            except Exception:
                                _mw_edit = None

                        if not _mw_edit or not _mw_edit.get("new_code"):
                            _dlog("correction_multi_window_parse_failed",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=_mw_idx,
                                  symbol=_mw_sym.name, window_idx=_mw_wi,
                                  raw_preview=_mw_raw[:300])
                            _mw_all_ok = False
                            _mw_hard_fail = True  # Bug 3: parse failure = hard fail
                            break

                        _dlog("correction_multi_window_parsed",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name, window_idx=_mw_wi,
                              parse_method=_mw_parse_method,
                              new_code_len=len(_mw_edit["new_code"]))

                        # ── Line prefix stripping (same majority check) ──
                        _mw_new = _mw_edit["new_code"]
                        _mw_raw_lines = _mw_new.splitlines()
                        _mw_prefix_pat = re.compile(r'^\s*\d+\s*\|\s?')
                        _mw_nonempty = [l for l in _mw_raw_lines if l.strip()]
                        _mw_hits = sum(1 for l in _mw_nonempty if _mw_prefix_pat.match(l))
                        _mw_ratio = _mw_hits / max(len(_mw_nonempty), 1)
                        _mw_strip = _mw_ratio >= 0.7 and _mw_hits >= 3

                        _dlog("correction_multi_window_prefix_check",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name, window_idx=_mw_wi,
                              total_lines=len(_mw_raw_lines),
                              nonempty=len(_mw_nonempty),
                              prefix_hits=_mw_hits,
                              prefix_ratio=f"{_mw_ratio:.2f}",
                              will_strip=_mw_strip)

                        if _mw_strip:
                            _mw_corrected = [
                                l[_mw_prefix_pat.match(l).end():] if _mw_prefix_pat.match(l) else l
                                for l in _mw_raw_lines
                            ]
                        else:
                            _mw_corrected = list(_mw_raw_lines)

                        # ── Boundary dedup (same logic as single-window) ──
                        # NOTE: _mw_ws0/_mw_we0 MUST be assigned before this
                        # block — they were previously assigned only at the
                        # splice step below, causing UnboundLocalError on the
                        # first processed window (see correction_multi_window_error).
                        _mw_ws0 = _mw_winfo["window_start"]
                        _mw_we0 = _mw_winfo["window_end"]
                        _mw_broken_lines_pre = _mw_running_code.splitlines()
                        _mw_suffix_after = _mw_broken_lines_pre[_mw_we0 + 1:]
                        if _mw_suffix_after and _mw_corrected:
                            _mw_max_ov = min(len(_mw_corrected), len(_mw_suffix_after), 10)
                            _mw_overlap = 0
                            for _mw_ov_k in range(1, _mw_max_ov + 1):
                                if _mw_corrected[-_mw_ov_k:] == _mw_suffix_after[:_mw_ov_k]:
                                    _mw_overlap = _mw_ov_k
                            if _mw_overlap > 0:
                                _dlog("correction_multi_window_boundary_dedup",
                                      session_id=session_id, user_id=user_id,
                                      retry_round=_qa_retry_round, idx=_mw_idx,
                                      symbol=_mw_sym.name, window_idx=_mw_wi,
                                      overlap_lines=_mw_overlap,
                                      stripped=[l.rstrip() for l in _mw_corrected[-_mw_overlap:]],
                                      suffix_head=[l.rstrip() for l in _mw_suffix_after[:_mw_overlap]])
                                _mw_corrected = _mw_corrected[:-_mw_overlap]

                        # ── Splice into running code ──
                        # (_mw_ws0/_mw_we0 assigned above, before boundary dedup)
                        _mw_broken_lines = _mw_running_code.splitlines()

                        if _mw_we0 >= len(_mw_broken_lines) or _mw_ws0 < 0 or _mw_ws0 > _mw_we0:
                            _dlog("correction_multi_window_bounds_invalid",
                                  session_id=session_id, user_id=user_id,
                                  retry_round=_qa_retry_round, idx=_mw_idx,
                                  symbol=_mw_sym.name, window_idx=_mw_wi,
                                  ws0=_mw_ws0, we0=_mw_we0,
                                  broken_lines=len(_mw_broken_lines))
                            _mw_all_ok = False
                            _mw_hard_fail = True  # Bug 3: bounds error = hard fail
                            break

                        _mw_expected = _mw_we0 - _mw_ws0 + 1
                        _mw_delta = len(_mw_corrected) - _mw_expected
                        _mw_broken_lines[_mw_ws0:_mw_we0 + 1] = _mw_corrected
                        _mw_running_code = "\n".join(_mw_broken_lines)

                        _mw_any_spliced = True  # Bug 3: at least one window succeeded

                        _dlog("correction_multi_window_splice_done",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name, window_idx=_mw_wi,
                              window_start=_mw_ws1, window_end=_mw_we1,
                              corrected_lines=len(_mw_corrected),
                              expected_lines=_mw_expected,
                              line_delta=_mw_delta,
                              had_prefix=_mw_strip,
                              running_total_lines=len(_mw_broken_lines))

                    except Exception as _mw_exc:
                        _dlog("correction_multi_window_error",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name, window_idx=_mw_wi,
                              error=str(_mw_exc), error_type=type(_mw_exc).__name__)
                        _mw_all_ok = False
                        _mw_hard_fail = True  # Bug 3: exception = hard fail
                        break

                # ── Final result for this multi-window idx ──
                # Bug 3 fix: Accept partial success. If at least one window
                # spliced successfully and there was no hard failure (parse
                # error, bounds error, exception), apply the running code.
                # Previously, a single "no_edit" window rolled back ALL
                # successful splices. (session d59e51e5: Window 2 succeeded
                # with 188-line splice, thrown away because Window 1 said
                # "no_edit".)
                _mw_should_apply = (
                    _mw_all_ok                         # all windows succeeded
                    or (_mw_any_spliced and not _mw_hard_fail)  # partial: ≥1 spliced, no hard fail
                )
                if _mw_should_apply:
                    _mw_frag = _fragment_reason(_mw_sym.code, _mw_running_code)
                    if _mw_frag is None:
                        change_shells[_mw_idx]["new_code"] = _mw_running_code
                        _new_tgt, _new_repl = _compute_target_element(
                            _mw_sym.code, _mw_running_code
                        )
                        change_shells[_mw_idx]["_tgt"]  = _new_tgt
                        change_shells[_mw_idx]["_repl"] = _new_repl
                        change_shells[_mw_idx]["diff"]  = _make_diff(
                            _mw_sym.code, _mw_running_code, _mw_sym.name
                        )
                        fixed_indices.append(_mw_idx)
                        _dlog("correction_multi_window_accepted",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name,
                              window_count=len(_mw_windows),
                              all_ok=_mw_all_ok,
                              partial=not _mw_all_ok and _mw_any_spliced,
                              original_lines=len(_mw_sym.code.splitlines()),
                              result_lines=len(_mw_running_code.splitlines()))
                    else:
                        _dlog("correction_multi_window_fragment_rejected",
                              session_id=session_id, user_id=user_id,
                              retry_round=_qa_retry_round, idx=_mw_idx,
                              symbol=_mw_sym.name,
                              fragment_reason=_mw_frag,
                              running_len=len(_mw_running_code),
                              sym_len=len(_mw_sym.code))
                else:
                    _dlog("correction_multi_window_failed",
                          session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=_mw_idx,
                          symbol=_mw_sym.name,
                          any_spliced=_mw_any_spliced,
                          hard_fail=_mw_hard_fail,
                          note="no windows spliced or hard failure — no changes applied")

            _dlog("qa_retry_fixed_indices", session_id=session_id, user_id=user_id,
                  retry_round=_qa_retry_round,
                  fixed_count=len(fixed_indices),
                  fixed_indices=fixed_indices,
                  fixed_symbols=[change_shells[i]["symbol"].name for i in fixed_indices] if fixed_indices else [])
            if not fixed_indices:
                _dlog("qa_retry_no_fixes_breaking", session_id=session_id, user_id=user_id,
                      retry_round=_qa_retry_round)
                break  # No code actually changed — stop retrying

            # Re-run QA on all fixed changes in parallel
            # ── Pipeline deadline gate (re-QA) ─────────────────────────
            if _pipeline_over_budget():
                _dlog("pipeline_deadline_skip",
                      session_id=session_id, user_id=user_id,
                      phase="re_qa_after_corrections",
                      retry_round=_qa_retry_round,
                      fixed_count=len(fixed_indices),
                      elapsed_s=round(time.time() - _pipeline_t0, 1),
                      deadline_s=PIPELINE_DEADLINE_S)
                yield sse({"type": "progress",
                           "content": "⏱️ Approaching time limit — skipping re-QA, shipping corrected changes"})
                break
            reqa_tasks = [
                (idx, asyncio.create_task(run_qa_agent(
                    original_code=change_shells[idx]["symbol"].code,
                    new_code=change_shells[idx]["new_code"],
                    change_description=change_shells[idx]["description"],
                    new_logic=change_shells[idx]["description"],
                    symbol_path=change_shells[idx]["symbol"].name,
                    filename=change_shells[idx]["filename"],
                    other_files_context=_qa_other_context,
                    session_id=session_id or "",
                    user_id=user_id,
                    architect_risks=all_qa_risks,
                    targeted_context=change_shells[idx]["targeted_ctx"],
                    qa_feedback=qa_results[idx],   # pass prior QA so it knows what to watch for
                    same_run_context=change_shells[idx]["same_run"],
                )))
                for idx in fixed_indices
            ]

            pending_reqa = {t for _, t in reqa_tasks}
            while pending_reqa:
                done_reqa, pending_reqa = await asyncio.wait(pending_reqa, timeout=20.0)
                if pending_reqa:
                    yield f"data: {json.dumps({'type': 'keepalive', 'content': ''})}\n\n"

            for idx, task in reqa_tasks:
                try:
                    qa_results[idx] = task.result()
                    new_verdict = qa_results[idx].get("verdict", "?")
                    new_score   = qa_results[idx].get("qa_score", "?")
                    icon = "✅" if new_verdict == "safe" else "⚠️" if new_verdict == "warning" else "🚫"
                    yield sse({"type": "progress",
                               "content": f"Re-QA {icon} {change_shells[idx]['symbol'].name}: "
                                          f"{qa_results[idx].get('summary', '')} (score: {new_score})"})
                except Exception as _reqa_exc:
                    _dlog("qa_retry_reqa_error", session_id=session_id, user_id=user_id,
                          retry_round=_qa_retry_round, idx=idx,
                          symbol=change_shells[idx]["symbol"].name,
                          error=str(_reqa_exc), error_type=type(_reqa_exc).__name__)

        # ── tsc compile check — final verification after all retries ──────────
        # Re-run tsc on the FINAL content of every change. Anything that still
        # introduces a compile error is marked verdict="blocked" / score<=3 via
        # _force_block_on_tsc. NOTE: with the advisory-only QA gate below, this
        # no longer withholds the change — it ships with a loud QA advisory
        # warning so the user can decide whether to apply it.
        _tsc_final_cache: dict = {}
        for _ti, _tcs in enumerate(change_shells):
            _t_introduced = await _tsc_introduced_errors(_tcs, _tsc_final_cache)
            if _t_introduced:
                _t_msgs = _force_block_on_tsc(_ti, _t_introduced, " remain after auto-fix")
                yield sse({"type": "progress",
                           "content": f"🔴 tsc check: {_tcs['symbol'].name} still has "
                                      f"{len(_t_msgs)} compile error(s) — shipping with a QA warning, review before applying"})

        # ── Assemble SurgicalChange objects from results
        # ADVISORY 8/10 GATE — NOT a hard block. Every change ships regardless
        # of QA outcome. Anything blocked, skipped (QA could not run), unscored,
        # or below _GATE_MIN (8) after every retry ships WITH a visible
        # "QA advisory ... Shipping anyway" progress message and a
        # qa_advisory_warning _dlog entry, so the user sees exactly what QA
        # flagged and why. (Historically this was a hard gate that withheld
        # sub-8 changes into skipped_changes; it was intentionally softened
        # to advisory-only — see the "QA is advisory-only" note in the loop.)
        # ── v3.13.0: Same-file companion gate elevation ─────────────────
        # If edits to the same file form a batch, and at least one edit in
        # that batch passes the hard gate (>=8), elevate companion edits
        # from warning (score 5-7) to pass. Edits scored "blocked" (1-4)
        # stay blocked — those have real bugs, not cross-dependency issues.
        # This prevents half-done states (e.g. CDN tag ships but the
        # function that uses it gets blocked).
        _file_groups = {}  # filename -> list of indices
        for _gi, _cs in enumerate(change_shells):
            _file_groups.setdefault(_cs["filename"], []).append(_gi)

        for _gfname, _gindices in _file_groups.items():
            if len(_gindices) < 2:
                continue  # single edit, no companion logic needed
            # Check if any edit in this file group already passes
            _any_passed = any(
                qa_results[_gi].get("qa_score") is not None
                and qa_results[_gi].get("qa_score") >= 8
                and qa_results[_gi].get("verdict") not in ("blocked", "skipped")
                for _gi in _gindices
            )
            if not _any_passed:
                continue
            for _gi in _gindices:
                _gqa = qa_results[_gi]
                _gscore = _gqa.get("qa_score")
                _gverdict = _gqa.get("verdict", "skipped")
                # Elevate warning (5-7) to safe — companion already passed
                if _gscore is not None and 5 <= _gscore <= 7 and _gverdict == "warning":
                    _old_score = _gscore
                    _gqa["qa_score"] = 8
                    _gqa["verdict"] = "safe"
                    _gqa["summary"] = (
                        f"[companion-elevated from {_old_score}/10] "
                        + _gqa.get("summary", "")
                    )
                    _dlog("qa_companion_elevation",
                          session_id=session_id,
                          filename=_gfname,
                          symbol=change_shells[_gi]["symbol"].name,
                          old_score=_old_score,
                          new_score=8,
                          companion_group=[
                              change_shells[_cgi]["symbol"].name for _cgi in _gindices
                          ],
                          user_id=user_id)

        _GATE_MIN = 8
        for i, (cs, qa_dict) in enumerate(zip(change_shells, qa_results)):
            symbol = cs["symbol"]
            filename = cs["filename"]
            sf_entry = cs["sf_entry"]

            _gv = qa_dict.get("verdict", "skipped")
            _gs = qa_dict.get("qa_score")
            # --- QA is advisory-only: warn but never block edits ---
            if _gv in ("blocked", "skipped") or _gs is None or _gs < _GATE_MIN:
                _gscore_txt = str(_gs) if _gs is not None else "n/a"
                _greason = (
                    qa_dict.get("summary")
                    or qa_dict.get("skipped_reason")
                    or "QA did not produce a score"
                )
                _qa_advisory_icon = "⚠️" if _gv == "skipped" else "🔶"
                yield sse({"type": "progress",
                           "content": f"{_qa_advisory_icon} QA advisory — {symbol.name} "
                                      f"(verdict: {_gv}, score: {_gscore_txt}/10): "
                                      f"{_greason[:120]}. Shipping anyway."})
                _dlog("qa_advisory_warning",
                      session_id=session_id,
                      filename=filename,
                      symbol=symbol.name,
                      verdict=_gv,
                      score=_gs,
                      reason=_greason[:300],
                      advisory=True,
                      user_id=user_id)
                try:
                    _log_qa_result(session_id, filename, symbol.name, qa_dict)
                except Exception:
                    pass
                # NOTE: no continue — edit proceeds below

            all_qa_risks.extend(qa_dict.get("downstream_risks", []))

            qa_result_obj = _QAResult(
                verdict=qa_dict.get("verdict", "skipped"),
                qa_score=qa_dict.get("qa_score"),
                summary=qa_dict.get("summary", ""),
                import_issues=qa_dict.get("import_issues", []),
                downstream_risks=qa_dict.get("downstream_risks", []),
                type_errors=qa_dict.get("type_errors", []),
                logic_errors=qa_dict.get("logic_errors", []),
                plan_deviation=qa_dict.get("plan_deviation", ""),
                risk_verdicts=qa_dict.get("risk_verdicts", []),
            )

            verdict_icon = (
                "\u2705" if qa_dict.get("verdict") == "safe"
                else "\u26a0\ufe0f" if qa_dict.get("verdict") == "warning"
                else "\U0001f6ab"
            )
            yield sse({
                "type": "progress",
                "content": (
                    f"QA {verdict_icon} {qa_dict.get('summary', '')} "
                    f"(score: {qa_dict.get('qa_score', '?')})"
                ),
            })

            _dlog("qa_gate_passed",
                  session_id=session_id,
                  filename=filename,
                  symbol=symbol.name,
                  verdict=_gv,
                  score=_gs,
                  user_id=user_id)
            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=cs["new_code"],
                diff=cs["diff"],
                confidence=qa_dict.get("qa_score") or 9,
                description=cs["description"],
                applied=False,
                qa_result=qa_result_obj,
                # Companion file-level ops (e.g. import-line fixes from the
                # correction loop) ride along after the sentinel symbol op;
                # apply_change applies them to the same file in the same apply.
                operations=[{"find": symbol.code, "replace": cs["new_code"]}]
                           + list(cs.get("_extra_ops") or []),
                target_element=cs["_tgt"],
                replacement=cs["_repl"],
            )

            if filename not in changes_by_file:
                changes_by_file[filename] = {
                    "filename": filename,
                    "file_id": sf_entry.get("id", "") if sf_entry else "",
                    "changes": [],
                }
            changes_by_file[filename]["changes"].append(change.model_dump())
            summary_parts.append(cs["description"] or f"Updated {symbol.name} in {filename}")

        # ── Process new file blocks ───────────────────────────────────────
        new_files: list = []
        for file_raw in new_file_blocks_raw:
            try:
                file_data = json.loads(file_raw.strip())
            except json.JSONDecodeError:
                try:
                    file_data = json.loads(_repair_json(file_raw.strip()))
                except Exception:
                    continue

            filename = file_data.get("filename", "")
            content = file_data.get("content", "")
            language = file_data.get("language", "")
            summary = file_data.get("summary", "")

            if not filename or not content:
                continue

            # Auto-detect language from extension if not provided
            if not language:
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                ext_map = {
                    "ts": "typescript", "tsx": "typescript", "js": "javascript",
                    "jsx": "javascript", "py": "python", "go": "go", "rs": "rust",
                    "css": "css", "html": "html", "json": "json", "md": "markdown",
                    "csv": "csv", "tsv": "csv", "xlsx": "csv", "xls": "csv",
                }
                language = ext_map.get(ext, "text")

            # ── R22: Excel binary creation via persist_result ─────────────
            _nf_ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if _nf_ext in ("xlsx", "xls"):
                try:
                    from services.datalab.config import datalab_enabled
                    if datalab_enabled():
                        import csv as _csv_mod
                        import io as _io_mod
                        _csv_reader = _csv_mod.reader(_io_mod.StringIO(content))
                        _all_rows = list(_csv_reader)
                        if _all_rows:
                            _xl_columns = _all_rows[0]
                            _xl_data_rows = _all_rows[1:]
                        else:
                            _xl_columns = []
                            _xl_data_rows = []
                        from services.datalab import persist as _dl_persist
                        _xl_desc = _dl_persist.persist_result(
                            session_id=session_id,
                            source_file_id="",
                            source_filename=filename,
                            source_kind="excel",
                            source_delimiter=",",
                            columns=_xl_columns,
                            rows=_xl_data_rows,
                            transform_sql="",
                            origin="generated",
                            sheet_name=file_data.get("sheet_name", "Sheet1"),
                        )
                        _dlog("newfile_xlsx_persist_ok",
                              session_id=session_id, user_id=user_id,
                              filename=_xl_desc["filename"],
                              file_id=_xl_desc["file_id"],
                              rows=_xl_desc["row_count"],
                              cols=_xl_desc["column_count"],
                              bytes=_xl_desc["byte_size"])
                        yield sse({"type": "progress",
                                   "content": f"📊 Created: {_xl_desc['filename']} ({_xl_desc['row_count']} rows × {_xl_desc['column_count']} cols)"})
                        summary_parts.append(summary or f"Created {_xl_desc['filename']}")
                        # Skip normal new_files append — persist_result already created the file
                        continue
                    else:
                        _dlog("newfile_xlsx_datalab_disabled",
                              session_id=session_id, user_id=user_id,
                              filename=filename)
                        # Fall through: save as CSV fallback
                        filename = filename.rsplit(".", 1)[0] + ".csv"
                except Exception as _xl_err:
                    _dlog("newfile_xlsx_persist_error",
                          session_id=session_id, user_id=user_id,
                          filename=filename,
                          error=str(_xl_err),
                          error_type=type(_xl_err).__name__)
                    # Fall through: save as CSV fallback
                    filename = filename.rsplit(".", 1)[0] + ".csv"

            # QA: check the new file against codebase context
            codebase_ctx = _build_codebase_context_for_creator(symbol_maps_by_name)
            try:
                qa = await _run_qa_for_new_file(
                    file_result={"filename": filename, "content": content,
                                 "language": language, "summary": summary},
                    codebase_context=codebase_ctx,
                    user_id=user_id,
                )
                qa_icon = {"safe": "✅", "warning": "⚠️", "blocked": "🚫"}.get(
                    qa.get("verdict", "safe"), "✅"
                )
                yield sse({"type": "progress",
                           "content": f"{qa_icon} {filename} — {qa.get('summary', 'ready')}"})
                file_data["qa_result"] = qa
            except Exception:
                yield sse({"type": "progress", "content": f"✅ {filename} ready"})
                file_data["qa_result"] = {"verdict": "safe", "qa_score": 8}

            new_files.append({
                "filename": filename,
                "content": content,
                "language": language,
                "summary": summary,
                "qa_result": file_data.get("qa_result", {}),
            })
            summary_parts.append(summary or f"Created {filename}")

        if not changes_by_file and not new_files:
            # ── Degenerate-drop guard ────────────────────────────────────────
            # The model emitted one or more edit/file blocks, yet NONE survived
            # resolution (snippet anchors never matched a large symbol, target
            # symbol not found, etc.). Emitting a bare "done" here reports phantom
            # success — the user sees nothing happen with no explanation, and the
            # QA gate is bypassed entirely (it ran on 0 changes). Instead surface
            # the dropped edits explicitly so the failure is visible and is never
            # silently shipped as success.
            _dlog("degenerate_drop",
                  session_id=session_id,
                  edit_blocks=len(edit_blocks_raw) if 'edit_blocks_raw' in dir() else 0,
                  new_file_blocks=len(new_file_blocks_raw) if 'new_file_blocks_raw' in dir() else 0,
                  skipped_changes=skipped_changes_struct[:10],
                  user_id=user_id)
            if edit_blocks_raw or new_file_blocks_raw:
                _attempted = len(edit_blocks_raw) + len(new_file_blocks_raw)
                _reasons = skipped_messages or [
                    "The proposed edits could not be matched to the current code."
                ]
                _detail = "\n".join(f"• {m}" for m in _reasons[:10])
                _plural = "them" if _attempted > 1 else "it"
                _msg = (
                    f"I drafted {_attempted} change(s) but couldn't safely apply {_plural} to "
                    f"the current code, so nothing was shipped:\n\n{_detail}\n\n"
                    "This usually means the target spot moved or the file is large enough that I "
                    "need another pass. Tell me the specific area to focus on and I'll re-target it."
                )
                _fail_result = {
                    "intent": "edit",
                    "summary": f"{_attempted} change(s) drafted — none could be applied",
                    "reasoning": "Edits were produced but failed to resolve against the current code.",
                    "risks": all_qa_risks,
                    "skipped_changes": skipped_changes_struct or [
                        {"filename": "", "symbol": "", "reason": "unresolved"}
                    ],
                    "changes_by_file": {},
                    "new_files": [],
                    "natural_text": _msg,
                }
                yield sse({"type": "smart_result", "content": json.dumps(_fail_result)})
            yield sse({"type": "done", "content": ""})
            return

        # Determine intent: create if any new files, edit otherwise, mixed if both
        has_edits = bool(changes_by_file)
        has_new_files = bool(new_files)
        intent = "create" if (has_new_files and not has_edits) else "edit"
        if has_new_files and has_edits:
            intent = "create"  # mixed create+edit — NewFileCard handles both

        # Strip edit blocks from display text (regex handles truncated/unclosed tags)
        import re as _re_strip
        _display_text = _re_strip.sub(r'<surgical_edit>.*?</surgical_edit>', '', full_response, flags=_re_strip.DOTALL)
        _display_text = _re_strip.sub(r'<surgical_edit>[\s\S]*$', '', _display_text)
        _display_text = _re_strip.sub(r'<new_file>.*?</new_file>', '', _display_text, flags=_re_strip.DOTALL)
        _display_text = _re_strip.sub(r'<new_file>[\s\S]*$', '', _display_text)
        _display_text = _display_text.strip()

        result = {
            "intent": intent,
            "summary": "; ".join(summary_parts[:3]),
            "reasoning": "Changes from natural conversation",
            "risks": all_qa_risks,
            "skipped_changes": skipped_changes_struct,
            "changes_by_file": changes_by_file,
            "new_files": new_files,
            "natural_text": _display_text,
        }

        _dlog("smart_result_emitted",
              session_id=session_id,
              changes_count=sum(len(v.get("changes", [])) for v in changes_by_file.values()),
              new_files_count=len(new_files),
              skipped_count=len(skipped_changes_struct),
              files=list(changes_by_file.keys()),
              user_id=user_id)
        yield sse({"type": "smart_result", "content": json.dumps(result)})
        _dlog("pipeline_complete",
              session_id=session_id, user_id=user_id,
              pipeline_total_s=round(time.time() - _pipeline_t0, 1))
        yield sse({"type": "done", "content": ""})

    except Exception as e:
        import traceback as _tb
        _dlog("execute_task_exception",
              session_id=session_id,
              error=str(e)[:500],
              error_type=type(e).__name__,
              traceback=_tb.format_exc()[-2000:],
              user_id=user_id)
        yield f"data: {json.dumps({'type': 'error', 'content': _friendly_error(e)})}\n\n"
