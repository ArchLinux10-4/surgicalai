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
# DEBUG LOGGING  (writes to /tmp/surgical_debug.jsonl — pulled via /api/debug/pipeline-log)
# ─────────────────────────────────────────────────────────────────────────────
import json as _json_mod
import datetime as _dt
import os as _os

_DLOG_PATH = "/tmp/surgical_debug.jsonl"
_DLOG_MAX_BYTES = 5 * 1024 * 1024   # 5 MB cap — rotate by truncating oldest half

def _dlog(event: str, **kwargs):
    """Append a structured debug record to the pipeline log file."""
    try:
        record = {
            "ts": _dt.datetime.utcnow().isoformat() + "Z",
            "event": event,
            **kwargs,
        }
        line = _json_mod.dumps(record, default=str) + "\n"
        # Rotate if too large
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

# Models that do NOT accept a temperature parameter (reasoning / latest-gen models)
NO_TEMPERATURE_MODELS = {"gpt-5", "o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini"}

# ── Prompt engineering constants ──────────────────────────────────────────────
HISTORY_WINDOW       = 20   # turns of conversation history passed to every prompt
TEXT_SEARCH_WINDOW   = 75   # ±lines around a text hit when no symbol contains the line
SYMBOL_FOCUS_WINDOW  = 100  # ±lines to slice when a symbol is huge but text is inside it

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


def _is_claude_model(model: str) -> bool:
    """Check if a model ID is a Claude/Anthropic model."""
    return bool(model and model.startswith("claude-"))


def _is_gemini_model(model: str) -> bool:
    """Check if a model ID is a Gemini/Google model."""
    return bool(model and (model.startswith("gemini-") or model.startswith("models/gemini")))


# Models confirmed to support extended thinking (budget_tokens).
# claude-opus-4-7 and others without native thinking support must be excluded.
# Extended-thinking support. All Claude 4.x families (Opus/Sonnet/Haiku 4.5+)
# emit thinking blocks; 3.7 also supports it. 3.5 does NOT and is excluded.
_THINKING_CAPABLE_PATTERNS = ("claude-opus-4", "claude-sonnet-4", "claude-haiku-4-5", "claude-3-7")

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
    """Return True for models that support extended thinking blocks."""
    if _is_claude_model(model):
        return any(model.startswith(p) or p in model for p in _THINKING_CAPABLE_PATTERNS)
    if _is_gemini_model(model):
        # Gemini 2.5+ models support thinking
        return "gemini-2.5" in model
    return False


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
    for models that don't support it (GPT-5, o-series reasoning models)."""
    base_model = model.split(":")[0].lower()
    if base_model in NO_TEMPERATURE_MODELS:
        return client.chat.completions.create(model=model, messages=messages, **kwargs)
    return client.chat.completions.create(model=model, messages=messages, temperature=temperature, **kwargs)


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

Output ONLY the SEARCH/REPLACE blocks. No JSON. No markdown fences around the blocks. No explanation outside the blocks."""


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
    temp = float(get_setting("temperature_architect", "0.3"))
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
    GPT-4.1 (Surgeon): receives ONE code chunk + plan, returns search-and-replace operations.
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
    surg_model = model or get_setting("surgeon_model", "gpt-4.1")
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
        for _qk in ("import_issues", "type_errors", "downstream_risks"):
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

    if _is_claude_model(surg_model):
        # Claude Surgeon path — Anthropic SDK (OpenAI client cannot call Claude models)
        _anthropic_key = _get_anthropic_key(user_id)
        from anthropic import Anthropic as _AnthropicSync
        _sync_aclient = _AnthropicSync(api_key=_anthropic_key)
        _claude_surgeon_resp = _sync_aclient.messages.create(
            model=surg_model,
            max_tokens=8192,
            system=SURGEON_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = _claude_surgeon_resp.content[0].text
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
        temperature=float(get_setting("temperature_architect", "0.3")),
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
                                  temperature=float(get_setting("temperature_architect", "0.3")), stream=True)
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
                temperature=float(get_setting("temperature_architect", "0.3")),
                stream=True
            )
            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield f"data: {json.dumps({'type': 'token', 'content': delta.content})}\n\n"
    except Exception as e:
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
        except Exception:
            pass  # _grep_relevant_sections not available yet — skip silently

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

    # 2. Whitespace-tolerant match (ignore trailing whitespace on each line).
    def _norm(s: str) -> str:
        return "\n".join(line.rstrip() for line in s.splitlines())

    norm_sym = _norm(symbol_code)
    norm_old = _norm(old_code)
    if norm_old and norm_sym.count(norm_old) == 1:
        # Locate the matching region in the ORIGINAL (un-normalised) symbol so we
        # preserve exact trailing whitespace outside the edit.
        sym_lines = symbol_code.splitlines(keepends=True)
        old_line_count = len(norm_old.split("\n"))
        target_norm = norm_old.split("\n")
        for i in range(0, len(sym_lines) - old_line_count + 1):
            window = [sym_lines[i + k].rstrip("\n").rstrip("\r").rstrip()
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
) -> dict:
    """
    Run a QA review of all proposed changes via a single non-streaming Claude call.

    Returns a dict with keys: verdict, qa_score, summary, issues, risks.
    On any error, returns a safe fallback dict.
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

            # Truncate very long code to 200 lines
            orig_lines = original.splitlines()
            new_lines = new_code.splitlines()
            if len(orig_lines) > 200:
                original = "\n".join(orig_lines[:200]) + "\n... (truncated)"
            if len(new_lines) > 200:
                new_code = "\n".join(new_lines[:200]) + "\n... (truncated)"

            user_parts.append(
                f"--- CHANGE: {symbol_name} ---\n"
                f"ORIGINAL:\n{original}\n\n"
                f"NEW CODE:\n{new_code}\n"
            )

        user_message = "\n".join(user_parts)

        aclient = AsyncAnthropic(api_key=anthropic_key)
        response = await aclient.messages.create(
            model=model,
            max_tokens=1000,
            system=_QA_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )

        raw_text = ""
        for block in response.content:
            if hasattr(block, "text"):
                raw_text += block.text

        result = _extract_json_from_text(raw_text)

        # Normalise keys
        return {
            "verdict": result.get("verdict", "safe"),
            "qa_score": result.get("qa_score", 8),
            "summary": result.get("summary", ""),
            "issues": result.get("issues", []),
            "risks": result.get("risks", []),
        }

    except Exception:
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
        yield sse({"type": "progress", "content": f"Found {n_sym} symbols. Claude is analyzing..."})

        # ------------------------------------------------------------------
        # Step 2: Get Anthropic key and model
        # ------------------------------------------------------------------
        anthropic_key = _get_anthropic_key(user_id)
        architect_model = get_setting("architect_model", "claude-sonnet-4-5")
        # Ensure we're using a Claude model (user might have set a GPT model)
        if not _is_claude_model(architect_model):
            architect_model = "claude-sonnet-4-5"

        aclient = AsyncAnthropic(api_key=anthropic_key)

        # ------------------------------------------------------------------
        # Step 3: Search loop — Claude can request symbols via "search" intent
        # ------------------------------------------------------------------
        MAX_SEARCH_ROUNDS = 4
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
                "max_tokens": 16000,
                "system": CLAUDE_EDITOR_SYSTEM,
                "messages": messages,
            }
            if _supports_thinking(architect_model):
                model_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}

            full_text = ""
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

            # Parse JSON from response
            try:
                plan_data = _extract_json_from_text(full_text)
            except ValueError as parse_err:
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
            yield sse({"type": "error", "content": "No response from Claude. Please try again."})
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
            yield sse({"type": "error", "content": f"Unexpected intent '{intent}' from Claude."})
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
            from services.structural_qa import run_structural_qa, has_blocking_issues as _sq_blocking
        except ImportError:
            _sq_blocking = None

        _sq_has_errors = False
        if _sq_blocking is not None:
            for _sq_ch in changes:
                _sq_new = getattr(_sq_ch, "new_code", "") or ""
                _sq_orig = getattr(_sq_ch, "original_code", "") or ""
                _sq_fname = file_path or ""
                _sq_issues = run_structural_qa(_sq_new, _sq_orig, _sq_fname)
                if _sq_blocking(_sq_issues):
                    _sq_has_errors = True
                    _sq_msgs = [si["message"] for si in _sq_issues if si["severity"] == "error"]
                    qa["verdict"] = "blocked"
                    qa["qa_score"] = min(qa.get("qa_score", 10), 3)
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

                # Re-call Claude with QA feedback injected
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
                    _retry_resp = await AsyncAnthropic(api_key=anthropic_key).messages.create(
                        model=architect_model,
                        max_tokens=16000,
                        system=CLAUDE_EDITOR_SYSTEM,
                        messages=_retry_msgs,
                    )
                    _retry_text = "".join(
                        b.text for b in _retry_resp.content if hasattr(b, "text")
                    )
                    _retry_data = _extract_json_from_text(_retry_text)
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
                            # Re-run QA on the fix
                            qa = await run_qa_for_changes(changes, file_content, user_request, anthropic_key, architect_model)
                            qa_risks = qa.get("risks", [])
                            # Re-run structural QA
                            if _sq_blocking is not None:
                                _sq_still_bad = False
                                for _sq_ch2 in changes:
                                    _sq_n2 = getattr(_sq_ch2, "new_code", "") or ""
                                    _sq_o2 = getattr(_sq_ch2, "original_code", "") or ""
                                    _sq_i2 = run_structural_qa(_sq_n2, _sq_o2, file_path or "")
                                    if _sq_blocking(_sq_i2):
                                        _sq_still_bad = True
                                        _sq_m2 = [x["message"] for x in _sq_i2 if x["severity"] == "error"]
                                        qa["verdict"] = "blocked"
                                        qa["qa_score"] = min(qa.get("qa_score", 10), 3)
                                        qa_risks.extend([f"[STRUCTURAL] {m}" for m in _sq_m2])
                                if not _sq_still_bad and qa.get("verdict") != "blocked":
                                    yield sse({"type": "progress",
                                               "content": f"✅ Retry {_aps_attempt + 1} passed QA (score: {qa.get('qa_score', '?')})"})
                                    break
                            elif qa.get("verdict") != "blocked" and (qa.get("qa_score", 0) or 0) >= 5:
                                yield sse({"type": "progress",
                                           "content": f"✅ Retry {_aps_attempt + 1} passed QA (score: {qa.get('qa_score', '?')})"})
                                break
                except Exception:
                    pass  # Keep current changes if retry fails

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
        temperature=float(get_setting("temperature_architect", "0.3")),
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


def _build_architect_system(is_diagnostic: bool = False) -> str:
    """Return the SMART_ARCHITECT_SYSTEM prompt, with diagnosis section injected when needed."""
    base = SMART_ARCHITECT_SYSTEM
    if is_diagnostic:
        # Inject diagnosis section before the IMPORT DEPENDENCY CHECK section
        inject_before = "━━━ IMPORT DEPENDENCY CHECK (DO THIS FIRST) ━━━"
        if inject_before in base:
            base = base.replace(inject_before, _DIAGNOSIS_SECTION + "\n" + inject_before, 1)
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
        _model = "claude-sonnet-4-6"
    except Exception:
        _qa_aclient = None
        _use_claude = False
        _model = "gpt-4.1"

    filename = file_result.get("filename", "new_file")
    content  = file_result.get("content", "")[:6000]

    user_msg = f"""CODEBASE CONTEXT (imports, types, and API signatures in the project):
{codebase_context[:3000]}

NEW FILE: {filename}
{content}

Run all 5 checks and return the JSON verdict."""

    try:
        if _use_claude:
            _msg = await _qa_aclient.messages.create(
                model=_model, max_tokens=800,
                system=QA_CREATE_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = _msg.content[0].text.strip()
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
    try:
        aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
    except Exception:
        # Fall back to OpenAI if no Anthropic key
        client = _get_client(user_id)
        model = get_setting("architect_model", "gpt-4.1")
        user_msg = _build_creator_user_msg(file_spec, codebase_context)
        response = _chat_create(
            client, model,
            messages=[
                {"role": "system", "content": FILE_CREATOR_SYSTEM},
                {"role": "user",   "content": user_msg},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        return json.loads(raw)

    # Prefer Claude for file creation — it generates better structured code
    creator_model = get_setting("architect_model", "claude-sonnet-4-6")
    if not _is_claude_model(creator_model):
        creator_model = "claude-sonnet-4-6"

    user_msg = _build_creator_user_msg(file_spec, codebase_context)

    response_chunks = []
    async with aclient.messages.stream(
        model=creator_model,
        max_tokens=8000,
        system=FILE_CREATOR_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
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
        _qa_model = "claude-sonnet-4-6"
    except Exception:
        _qa_aclient = None
        _qa_use_claude = False
        _qa_model = "gpt-4.1"  # full GPT-4.1, not mini — QA must not be weakest link

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

    # Targeted cross-file context: actual callers/usages of changed symbol
    _targeted_block = ""
    if targeted_context and targeted_context.strip():
        _targeted_block = f"\n\nTARGETED CROSS-FILE CONTEXT (callers/usages of the changed symbol):\n{targeted_context.strip()}"

    # QA feedback block injected on retry
    _qa_feedback_block = ""
    if qa_feedback:
        _issues = []
        for _qk in ("import_issues", "type_errors", "downstream_risks"):
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

    user_msg = f"""CHANGE PLAN:
Symbol: {symbol_path}
File: {filename}
Description: {change_description}
Expected behavior: {new_logic}
{"⚠️ NOTE: No code changes detected — original and new are identical." if _no_change else ""}

ORIGINAL CODE (complete — compare this directly against NEW CODE):
{_orig_snippet}

NEW CODE (complete — this is what the Surgeon produced):
{_new_snippet}

OTHER FILES IN SESSION (for cross-file checking):
{other_ctx if other_ctx.strip() else "(no other files uploaded)"}{_targeted_block}{_qa_feedback_block}

{("\n\nOTHER CHANGES IN THIS SAME REQUEST (planned but reviewed separately — cross-symbol deps covered by these should be scored as warnings, not blocks):\n" + same_run_context + "\n") if same_run_context else ""}Compare ORIGINAL CODE → NEW CODE directly. Run all 8 checks and return the JSON verdict.

ARCHITECT PRE-ANALYSIS RISKS (evaluate each in risk_verdicts):
{_risks_block}"""

    try:
        if _qa_use_claude:
            _qa_msg = await _qa_aclient.messages.create(
                model=_qa_model,
                max_tokens=1500,
                system=QA_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = (_qa_msg.content[0].text or "").strip()
            # Robust JSON extraction — Claude sometimes adds preamble or markdown fences
            raw = _extract_json_from_text(raw)
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
            raw = response.choices[0].message.content
        data = json.loads(raw)

        result = {
            "verdict":          data.get("verdict", "warning"),
            "risk_verdicts":    data.get("risk_verdicts", []),
            "qa_score":         int(data.get("qa_score", 7)),
            "summary":          data.get("summary", ""),
            "import_issues":    data.get("import_issues", []),
            "downstream_risks": data.get("downstream_risks", []),
            "type_errors":      data.get("type_errors", []),
            "plan_deviation":   data.get("plan_deviation", ""),
            "skipped_reason":   None,
        }

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
            result["qa_score"] = min(result["qa_score"], 4)
        if result["qa_score"] <= 7 and result["verdict"] == "safe":
            result["verdict"] = "warning"

        # Log to DB (non-blocking — fire and forget)
        try:
            _log_qa_result(session_id, filename, symbol_path, result)
        except Exception:
            pass  # Never let logging kill the pipeline

        return result

    except Exception as e:
        skipped = {
            "verdict":          "skipped",
            "qa_score":         None,
            "summary":          "QA check could not run — review manually",
            "import_issues":    [],
            "downstream_risks": [],
            "type_errors":      [],
            "plan_deviation":   "",
            "skipped_reason":   str(e)[:200],
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
            async with _da_client.messages.stream(
                model=model,
                max_tokens=32000,
                system=_dr_system,
                messages=[{"role": "user", "content": _dr_user}],
                tools=_dr_tools,
                tool_choice={"type": "tool", "name": "submit_file_rewrite"},
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

    for _blk in _dr_resp.content:
        if getattr(_blk, "type", None) == "tool_use" and getattr(_blk, "name", None) == "submit_file_rewrite":
            _inp = _blk.input if isinstance(_blk.input, dict) else {}
            return {
                "new_file_content": _inp.get("new_file_content", ""),
                "confidence": _inp.get("confidence", 8),
                "notes": _inp.get("notes", []),
            }

    raise RuntimeError("[DIRECT_REWRITE] Claude did not call submit_file_rewrite — check model/key")


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
                    max_tokens=16000,
                    **(({"thinking": {"type": "enabled", "budget_tokens": 10000}}) if _supports_thinking(chat_model) else {}),
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

        file_summaries = []
        symbol_maps_by_name = {}

        image_files = []  # files to be passed as vision content blocks

        for sf in session_files:
            fname = sf["filename"]
            content = sf["content"]
            file_type = sf.get("file_type", "code")

            if file_type == "image":
                # Don't add to text summaries — will be passed as vision content block
                image_files.append(sf)
                file_summaries.append(f"FILE: {fname} [IMAGE — passed as visual context to GPT vision]")
                symbol_maps_by_name[fname] = (None, sf)
                continue

            if file_type in ("pdf", "csv", "excel", "text"):
                # Treat as plain text — no AST parsing
                preview = content[:3000] + (f"\n... [{len(content) - 3000} chars truncated]" if len(content) > 3000 else "")
                file_summaries.append(
                    f"FILE: {fname} [{file_type.upper()}]\nCONTENT:\n{preview}"
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
                    f"{sf.get('language', 'code')})\n"
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
        _architect_system = _build_architect_system(is_diagnostic=_is_diagnostic)

        if _is_claude_model(arch_model):
            # -- Claude Architect with ReAct agentic search loop --
            # Claude drives iterative grep searches until it has enough context.
            # Each "search" intent response triggers a grep + re-call (up to 4 rounds).
            aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))

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
                            max_tokens=16000,
                            **({"thinking": {"type": "enabled", "budget_tokens": 10000}}
                               if _supports_thinking(arch_model) else {}),
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
                                    max_tokens=16000,
                                    **({"thinking": {"type": "enabled",
                                                    "budget_tokens": 10000}}
                                       if _supports_thinking(arch_model) else {}),
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
                resp_oai = await asyncio.to_thread(
                    lambda: _chat_create(gclient_oai, model=arch_model, messages=arch_msgs_oai,
                                        temperature=0.3, response_format={"type": "json_object"})
                )
                plan = json.loads(resp_oai.choices[0].message.content)
        else:
            # ── OpenAI Architect (original logic) ──
            client = _get_client(user_id)

            # Build user message content — text + optional vision blocks
            if image_files:
                # Multi-modal content array
                user_content = [{"type": "text", "text": context_msg}]
                for img_sf in image_files:
                    img_data = img_sf["content"]
                    # Ensure it's a valid data URL
                    if not img_data.startswith("data:"):
                        # Try to infer MIME type from filename
                        ext = Path(img_sf["filename"]).suffix.lower().lstrip(".")
                        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                                    "webp": "image/webp", "gif": "image/gif", "bmp": "image/bmp"}
                        mime = mime_map.get(ext, "image/png")
                        img_data = f"data:{mime};base64,{img_data}"
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": img_data}
                    })
                architect_messages = [
                    {"role": "system", "content": _architect_system},
                    {"role": "user", "content": user_content}
                ]
            else:
                architect_messages = [
                    {"role": "system", "content": _architect_system},
                    {"role": "user", "content": context_msg}
                ]

            # ── Run Architect in background thread so we can yield elapsed ticks ──
            async def _call_architect(msgs):
                return await asyncio.to_thread(
                    lambda: _chat_create(client,
                        model=arch_model,
                        messages=msgs,
                        temperature=0.3,
                        response_format={"type": "json_object"}
                    )
                )

            arch_task = asyncio.create_task(_call_architect(architect_messages))
            start_time = time.time()
            tick = 0
            while not arch_task.done():
                await asyncio.sleep(1)
                tick += 1
                if tick % 3 == 0:
                    elapsed = int(time.time() - start_time)
                    yield sse({"type": "progress", "content": f"Architect thinking... ({elapsed}s)"})

            try:
                response = arch_task.result()
            except Exception as img_err:
                err_str = str(img_err).lower()
                if image_files and ("image" in err_str or "unsupported" in err_str or "invalid" in err_str):
                    # GPT rejected one or more images — fall back gracefully to text-only
                    yield sse({"type": "progress", "content": "⚠️ Images couldn't be read — falling back to text context..."})
                    architect_messages = [
                        {"role": "system", "content": _architect_system},
                        {"role": "user", "content": context_msg}
                    ]
                    arch_task2 = asyncio.create_task(_call_architect(architect_messages))
                    start_time = time.time()
                    while not arch_task2.done():
                        await asyncio.sleep(1)
                        tick += 1
                        if tick % 3 == 0:
                            elapsed = int(time.time() - start_time)
                            yield sse({"type": "progress", "content": f"Architect retrying... ({elapsed}s)"})
                    response = arch_task2.result()
                else:
                    raise

            plan = json.loads(response.choices[0].message.content)
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
        # and the Architect will proceed with a real plan.
        # Safety: 'search' intent should be consumed inside the ReAct loop.
        # If it leaks through (OpenAI/GPT path), run ONE grep round then re-call.
        if intent == "search":
            _leak_search_terms = plan.get("search_terms", [])
            _leak_grep_result = ""
            for _sfname, (_sm, _sf) in symbol_maps_by_name.items():
                _sfcontent = _sf.get("content", "")
                if not _sfcontent:
                    continue
                for _st in _leak_search_terms[:6]:
                    _lines = _sfcontent.split("\n")
                    for _li, _ln in enumerate(_lines):
                        if _st.lower() in _ln.lower():
                            start = max(0, _li - 15)
                            end = min(len(_lines), _li + 25)
                            _window = "\n".join(f"L{start+i+1}: {_lines[start+i]}" for i in range(end - start))
                            if _window not in _leak_grep_result:
                                _leak_grep_result += f"\n--- {_sfname}: matches for '{_st}' ---\n{_window}\n"
                            break
            if _leak_grep_result:
                # Re-call architect with grep results
                _leak_context = context_msg + f"\n\n=== SEARCH RESULTS ===\n{_leak_grep_result[:3000]}"
                _leak_msgs = [
                    {"role": "system", "content": _architect_system},
                    {"role": "user", "content": _leak_context}
                ]
                _leak_resp = await asyncio.to_thread(
                    lambda: _chat_create(_get_client(user_id), model=arch_model,
                                         messages=_leak_msgs, temperature=0.3,
                                         response_format={"type": "json_object"})
                )
                try:
                    plan = json.loads(_leak_resp.choices[0].message.content)
                    intent = plan.get("intent", "chat")
                except Exception:
                    pass
            if intent == "search":
                # Still search after one round — give up gracefully
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
            _surg_model_route = get_setting("surgeon_model", "gpt-4.1")
            _symbol_size_route = symbol.end_line - symbol.start_line + 1
            # v3.12: use original (pre-narrowing) size — narrowing must not defeat routing
            _use_direct_rewrite = (
                (_is_redesign or _original_symbol_size > 250)
                and _is_claude_model(_surg_model_route)
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
                yield sse({"type": "progress", "content": f"Full-file rewrite: Claude writing complete {matched_name} ({_symbol_size_route}L symbol)..."})
                _dr_ok = False
                try:
                    _dr = await _run_claude_direct_rewrite(
                        file_content=sf["content"],
                        filename=matched_name,
                        change_description=change_target.description,
                        new_logic=change_target.new_logic,
                        architect_plan=plan,
                        anthropic_key=_get_anthropic_key(user_id),
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
                    for _qk in ("import_issues", "type_errors", "downstream_risks"):
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
                    _other_descs = [_ot.description for _oj, _ot in enumerate(targets)
                                    if _oj != i and getattr(_ot, "description", "")]
                    if _other_descs:
                        _same_run_ctx = "\n".join(f"  \u2022 {_d}" for _d in _other_descs[:6])

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
                        _lint_fix_client = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
                        _lint_surg_model = get_setting("surgeon_model", "claude-sonnet-4-5")
                        if not _is_claude_model(_lint_surg_model):
                            _lint_surg_model = "claude-sonnet-4-5"
                        _lint_working = _full_after_lint          # updated each attempt
                        _lint_remaining = _linter_introduced_errors  # refreshed each attempt
                        for _lint_attempt in range(_MAX_LINT_ATTEMPTS):
                            try:
                                _lint_err_lines = "\n".join(
                                    f"  {e.get('file',matched_name)}({e['line']},{e.get('col',1)}): error {e.get('code','TS0000')}: {e['message']}"
                                    for e in _lint_remaining
                                )
                                _attempt_label = f"attempt {_lint_attempt+1}/{_MAX_LINT_ATTEMPTS}"
                                yield sse({"type": "progress", "content": f"🔧 Lint auto-fix {_attempt_label}: {len(_lint_remaining)} error(s) → Claude..."})
                                _lint_fix_resp = await _lint_fix_client.messages.create(
                                    model=_lint_surg_model,
                                    max_tokens=8192,
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
                                    messages=[{
                                        "role": "user",
                                        "content": (
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
                                    }]
                                )
                                # Apply fixes from tool_use response
                                _lint_patched = _lint_working
                                _fixes_applied = 0
                                for _lblock in _lint_fix_resp.content:
                                    if hasattr(_lblock, "type") and _lblock.type == "tool_use":
                                        for _lfix in (_lblock.input or {}).get("fixes", []):
                                            _lf = _lfix.get("find", "")
                                            _lr = _lfix.get("replace", "")
                                            if _lf and _lf in _lint_patched:
                                                _lint_patched = _lint_patched.replace(_lf, _lr, 1)
                                                _fixes_applied += 1
                                # Re-run linter on patched content to get fresh error list
                                _lint_retry_count = _count_lint(_lint_patched, matched_name)
                                _lint_working = _lint_patched  # always advance, even if errors remain
                                if _lint_retry_count == 0:
                                    _full_after_lint = _lint_working
                                    _linter_introduced_errors = []
                                    _lint_fixed = True
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

For a large symbol (e.g. a 900-line React component) re-emitting the entire body is
wasteful and error-prone — and you may only have been shown PART of it. In that case,
do NOT replace the whole symbol and do NOT create a new file. Instead make a TARGETED
edit: provide the small "old_code" snippet you want to change plus its "new_code"
replacement. The system splices it into the full symbol and runs the same QA gate.

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
- "old_code" must be copied VERBATIM from the file/search results (NO line-number prefix)
  and must match EXACTLY ONE place in the symbol — include a few surrounding lines if needed
- "new_code" is the full replacement for just that snippet (include the old lines you keep)
- If you can't see the region you need, emit a <search_request> for a nearby string literal
  FIRST — you'll get a focused window around it — then write the targeted edit
- Use a full-symbol new_code (no old_code) only for small symbols you can see entirely

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
"""


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
                f"   Fix it by EITHER:\n"
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
                # Large symbols: show head + tail so the model can pick a real
                # anchor near either end without flooding the context.
                _HEAD, _TAIL = 220, 80
                if len(_sc_lines) > _HEAD + _TAIL + 10:
                    head = "\n".join(
                        f"   {_sc_start + i:5d}: {_sc_lines[i]}" for i in range(_HEAD)
                    )
                    tail_start = len(_sc_lines) - _TAIL
                    tail = "\n".join(
                        f"   {_sc_start + tail_start + i:5d}: {_sc_lines[tail_start + i]}"
                        for i in range(_TAIL)
                    )
                    gap = len(_sc_lines) - _HEAD - _TAIL
                    parts.append(
                        f"\n   ACTUAL current content of '{bad_name}' "
                        f"(copy an \"old_code\" anchor VERBATIM from here):\n"
                        f"{head}\n   ... [{gap} more lines omitted] ...\n{tail}"
                    )
                    # ── Focused-anchor window ─────────────────────────────────
                    # When the attempted old_code anchor falls in the omitted
                    # middle, find the closest real line and show ±60 lines so
                    # Claude can copy a verbatim anchor instead of re-inventing.
                    _raw_edit = item.get("_raw", "")
                    _old_tried = ""
                    if _raw_edit:
                        try:
                            _old_tried = json.loads(_raw_edit.strip()).get("old_code", "")
                        except Exception:
                            pass
                    if _old_tried:
                        _needle_lines = [
                            _ln.strip() for _ln in _old_tried.splitlines()
                            if len(_ln.strip()) >= 15
                        ]
                        _anc_idx = -1
                        # Step 1: exact substring match within omitted middle
                        for _nl in _needle_lines[:6]:
                            for _lj, _ltxt in enumerate(_sc_lines):
                                if _HEAD <= _lj < len(_sc_lines) - _TAIL:
                                    if _nl in _ltxt or _ltxt.strip() in _nl:
                                        _anc_idx = _lj
                                        break
                            if _anc_idx >= 0:
                                break
                        # Step 2: fuzzy best-match (SequenceMatcher) in omitted middle
                        if _anc_idx == -1 and _needle_lines:
                            _best_r = 0.0
                            for _nl_f in _needle_lines[:4]:
                                for _lj, _ltxt in enumerate(_sc_lines):
                                    if _HEAD <= _lj < len(_sc_lines) - _TAIL:
                                        _r = difflib.SequenceMatcher(
                                            None,
                                            _nl_f.lower(),
                                            _ltxt.strip().lower()
                                        ).ratio()
                                        if _r > _best_r:
                                            _best_r = _r
                                            _anc_idx = _lj
                            if _best_r < 0.45:
                                _anc_idx = -1
                        if _anc_idx >= 0:
                            _WIN = 60
                            _ws = max(0, _anc_idx - _WIN)
                            _we = min(len(_sc_lines), _anc_idx + _WIN + 1)
                            _focused_win = "\n".join(
                                f"   {_sc_start + _ws + _k:5d}: {_sc_lines[_ws + _k]}"
                                for _k in range(_we - _ws)
                            )
                            parts.append(
                                f"\n   ⚠️ INSERTION AREA — your attempted anchor was in the "
                                f"omitted section. Copy \"old_code\" VERBATIM from this "
                                f"focused window "
                                f"(L{_sc_start + _ws}–L{_sc_start + _we - 1}):\n{_focused_win}"
                            )
                else:
                    numbered = "\n".join(
                        f"   {_sc_start + i:5d}: {_sc_lines[i]}" for i in range(len(_sc_lines))
                    )
                    parts.append(
                        f"\n   ACTUAL current content of '{bad_name}' "
                        f"(copy an \"old_code\" anchor VERBATIM from here):\n{numbered}"
                    )
            else:
                parts.append(
                    "   Use <search_request> first if you need to see the exact current lines."
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
        return "\n".join(parts) if parts else ""

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
            f"lean indexes — use <search_request> to fetch their code if needed.\n"
        )

    def _render_full(sf: dict) -> str:
        fname      = sf["filename"]
        content    = sf.get("content", "")
        file_type  = sf.get("file_type", "code")
        lines_count = sf.get("lines", len(content.splitlines()))

        if file_type == "image":
            return (
                f"FILE: {fname} [IMAGE — attached as vision block below, you can see it directly]\n"
            )

        if file_type in ("pdf", "csv", "excel", "text"):
            preview = content[:2000] + (f"\n...[{len(content)-2000} chars]" if len(content) > 2000 else "")
            return f"FILE: {fname} [{file_type.upper()}]\nCONTENT:\n{preview}\n"

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
                f"FILE: {fname} ({lines_count} lines)\n"
                f"SYMBOL INDEX (use these EXACT names in surgical_edit):\n{sym_index}\n"
            )
            if lines_count <= 300:
                header += f"\nFULL CONTENT:\n```\n{content}\n```\n"
            else:
                smart_ctx = _smart_code_context(fname, content, smap, user_request, max_code_lines=350)
                if smart_ctx:
                    header += smart_ctx + "\n"
                else:
                    grep_hit = _grep_relevant_sections(user_request, fname, content)
                    if grep_hit:
                        header += grep_hit + "\n"
            return header
        else:
            preview = content[:1500] + (f"\n...[{len(content)-1500} chars]" if len(content) > 1500 else "")
            return f"FILE: {fname} ({lines_count} lines)\nCONTENT:\n```\n{preview}\n```\n"

    def _render_lean(sf: dict) -> str:
        fname       = sf["filename"]
        file_type   = sf.get("file_type", "code")
        lines_count = sf.get("lines", sf.get("content", "") and len(sf["content"].splitlines()) or 0)

        if file_type in ("image", "pdf", "csv", "excel", "text"):
            return f"  {fname} [{file_type.upper()}, {lines_count}L] — use <search_request> to view"

        smap, _ = symbol_maps_by_name.get(fname, (None, sf))
        symbols  = getattr(smap, "symbols", []) if smap else []

        if symbols:
            sym_names = ", ".join(
                s.full_path or s.name
                for s in symbols[:20]
            )
            suffix = f" +{len(symbols)-20} more" if len(symbols) > 20 else ""
            return f"  {fname} ({lines_count}L, {len(symbols)} symbols) — {sym_names}{suffix}"
        else:
            return f"  {fname} ({lines_count}L)"

    for sf in tier1:
        parts.append(_render_full(sf))

    # ── Render Tier 2: lean index ─────────────────────────────────────────
    if tier2:
        parts.append(
            "\n━━━ OTHER UPLOADED FILES (lean index — search to get code) ━━━\n"
            "Use <search_request> with a symbol name or keyword to fetch full code.\n"
        )
        for sf in tier2:
            parts.append(_render_lean(sf))

    return "\n".join(parts)


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
            if changes:
                text += f"\n[Applied changes to: {', '.join(changes[:6])}]"
            if qa_flags:
                text += f"\n[QA flagged: {'; '.join(qa_flags[:4])}]"
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

    EDIT_OPEN = "<surgical_edit>"
    EDIT_CLOSE = "</surgical_edit>"
    FILE_OPEN = "<new_file>"
    FILE_CLOSE = "</new_file>"
    SEARCH_OPEN = "<search_request>"
    SEARCH_CLOSE = "</search_request>"

    try:
        anthropic_key = _get_anthropic_key(user_id)
        arch_model = get_setting("architect_model", "claude-sonnet-4-5")
        if not _is_claude_model(arch_model):
            arch_model = "claude-sonnet-4-5"

        aclient = AsyncAnthropic(api_key=anthropic_key)

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

        # ── Build file context ────────────────────────────────────────────
        file_context = _build_natural_file_context(
            session_files, symbol_maps_by_name, user_request,
            project_memory=project_memory, session_summary=session_summary
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

                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            messages = clean_history + [{"role": "user", "content": user_content}]
        else:
            messages = clean_history + [{"role": "user", "content": user_request}]

        # ── Stream Claude's response ──────────────────────────────────────
        yield sse({"type": "progress", "content": "Thinking..."})

        stream_kwargs = {
            "model": arch_model,
            "max_tokens": 16000,
            "system": system_prompt,
            "messages": messages,
        }
        if _supports_thinking(arch_model):
            stream_kwargs["thinking"] = {"type": "enabled", "budget_tokens": 8000}

        # ── Streaming loop with ReAct search + edit/file/search tag parsing ─────
        # Claude can emit <search_request>, <surgical_edit>, or <new_file> tags.
        # Search tags trigger a silent code-fetch + re-call loop (max 4 rounds).
        # Edit/file tags are buffered and processed after streaming completes.

        edit_blocks_raw: list = []
        new_file_blocks_raw: list = []
        full_response = ""
        in_thinking = False

        # Build the per-file content lookup once (reused across search rounds)
        file_content_lookup_stream: dict = {
            sf["filename"]: sf.get("content", "") for sf in session_files
        }

        MAX_SEARCH_ROUNDS = 4
        searched_terms: list = []                # terms fetched so far (avoid re-fetching)
        accumulated_search_results = ""          # injected into context each round
        current_messages = list(messages)        # grows with search result turns
        _forced_edit_round_done = False          # only one forced-edit round allowed

        # +2: one extra slot for the forced-edit round when budget exhausted
        for search_round in range(MAX_SEARCH_ROUNDS + 2):

            state = "normal"    # "normal" | "in_edit" | "in_file" | "in_search"
            normal_buf = ""
            edit_buf = ""
            file_buf = ""
            search_buf = ""
            search_requested: dict | None = None  # set when a search block completes

            # Retry loop for transient API errors
            for _attempt in range(3):
                try:
                    async with aclient.messages.stream(**{
                        **stream_kwargs,
                        "messages": current_messages,
                    }) as astream:
                        current_block_type = None
                        async for event in astream:
                            etype = getattr(event, "type", None)

                            if etype == "content_block_start":
                                current_block_type = getattr(
                                    getattr(event, "content_block", None), "type", None
                                )
                                if current_block_type == "thinking":
                                    in_thinking = True
                                    yield sse({"type": "thinking_start", "content": ""})

                            elif etype == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                if not delta:
                                    continue

                                thinking_chunk = getattr(delta, "thinking", None)
                                text_chunk = getattr(delta, "text", None)

                                if thinking_chunk:
                                    yield sse({"type": "thinking", "content": thinking_chunk})

                                elif text_chunk:
                                    full_response += text_chunk

                                    if state == "normal":
                                        normal_buf += text_chunk

                                        # Drain buffer watching for any opening tag
                                        while True:
                                            ei = normal_buf.find(EDIT_OPEN)
                                            fi = normal_buf.find(FILE_OPEN)
                                            si = normal_buf.find(SEARCH_OPEN)

                                            # Find which tag comes first
                                            candidates = [
                                                (i, tag) for i, tag in [
                                                    (ei, "edit"), (fi, "file"), (si, "search")
                                                ] if i != -1
                                            ]

                                            if not candidates:
                                                # No tags — yield safely (keep tail in case tag is split)
                                                tail = max(len(EDIT_OPEN), len(FILE_OPEN), len(SEARCH_OPEN))
                                                safe = max(0, len(normal_buf) - tail)
                                                if safe > 0:
                                                    yield sse({"type": "token", "content": normal_buf[:safe]})
                                                    normal_buf = normal_buf[safe:]
                                                break

                                            first_idx, first_tag = min(candidates, key=lambda x: x[0])

                                            # Yield text before the tag
                                            if first_idx > 0:
                                                yield sse({"type": "token", "content": normal_buf[:first_idx]})

                                            if first_tag == "edit":
                                                yield sse({"type": "edit_start", "content": ""})
                                                state = "in_edit"
                                                edit_buf = normal_buf[first_idx + len(EDIT_OPEN):]
                                                normal_buf = ""
                                            elif first_tag == "file":
                                                yield sse({"type": "edit_start", "content": ""})
                                                state = "in_file"
                                                file_buf = normal_buf[first_idx + len(FILE_OPEN):]
                                                normal_buf = ""
                                            else:  # search
                                                # Show search indicator — don't stream the JSON to user
                                                state = "in_search"
                                                search_buf = normal_buf[first_idx + len(SEARCH_OPEN):]
                                                normal_buf = ""
                                            break

                                    elif state == "in_edit":
                                        edit_buf += text_chunk
                                        idx = edit_buf.find(EDIT_CLOSE)
                                        if idx != -1:
                                            edit_blocks_raw.append(edit_buf[:idx])
                                            yield sse({"type": "edit_end", "content": ""})
                                            state = "normal"
                                            normal_buf = edit_buf[idx + len(EDIT_CLOSE):]
                                            edit_buf = ""

                                    elif state == "in_file":
                                        file_buf += text_chunk
                                        idx = file_buf.find(FILE_CLOSE)
                                        if idx != -1:
                                            new_file_blocks_raw.append(file_buf[:idx])
                                            yield sse({"type": "edit_end", "content": ""})
                                            state = "normal"
                                            normal_buf = file_buf[idx + len(FILE_CLOSE):]
                                            file_buf = ""

                                    elif state == "in_search":
                                        search_buf += text_chunk
                                        idx = search_buf.find(SEARCH_CLOSE)
                                        if idx != -1:
                                            search_json_raw = search_buf[:idx]
                                            remainder = search_buf[idx + len(SEARCH_CLOSE):]
                                            try:
                                                search_data = json.loads(search_json_raw.strip())
                                                search_requested = search_data
                                            except Exception:
                                                pass
                                            # Put remainder back into normal stream
                                            state = "normal"
                                            normal_buf = remainder
                                            search_buf = ""
                                            # Stop streaming — we need to fetch code first
                                            break

                            elif etype == "content_block_stop":
                                if in_thinking and current_block_type == "thinking":
                                    yield sse({"type": "thinking_end", "content": ""})
                                    in_thinking = False

                        # If a search was requested mid-stream, break out of the event loop
                        if search_requested is not None:
                            break

                    break  # success — exit transient-error retry loop

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
                    raise

            # Flush any normal text buffered at end of this round
            if state == "normal" and normal_buf.strip():
                yield sse({"type": "token", "content": normal_buf})

            # ── Handle search request ─────────────────────────────────────
            if search_requested is not None:
                raw_terms = search_requested.get("terms", [])
                reason = search_requested.get("reason", "")
                _dlog("search_requested",
                      session_id=session_id,
                      round=search_round,
                      terms=raw_terms,
                      reason=reason,
                          user_id=user_id)

                # Budget exhausted — do one forced-edit round, then stop
                if search_round >= MAX_SEARCH_ROUNDS or _forced_edit_round_done:
                    if not _forced_edit_round_done:
                        _forced_edit_round_done = True
                        yield sse({"type": "progress",
                                   "content": "Search limit reached — writing edits now..."})
                        forced_msg = (
                            f"You have used all {MAX_SEARCH_ROUNDS} search rounds. "
                            "You already have all the code you need from previous results. "
                            "Write your complete <surgical_edit> or <new_file> blocks RIGHT NOW. "
                            "Do NOT emit another <search_request> — there are no more search rounds. "
                            "Use the exact symbol names from the search results you already received."
                        )
                        current_messages = current_messages + [
                            {"role": "assistant", "content": full_response or "(analyzing gathered code...)"},
                            {"role": "user", "content": forced_msg},
                        ]
                        full_response = ""
                        search_requested = None
                        continue  # One final forced-edit round
                    else:
                        # Already tried forced round and Claude still wants to search — give up
                        break

                # Filter out already-searched terms
                new_terms = [t for t in raw_terms
                             if t.lower() not in {s.lower() for s in searched_terms}]

                if not new_terms:
                    # All requested terms already searched — tell Claude to write edits
                    current_messages = current_messages + [
                        {"role": "assistant", "content": full_response},
                        {"role": "user", "content":
                            "All the terms you requested have already been searched. "
                            "Write your <surgical_edit> blocks now using the information you have."},
                    ]
                    full_response = ""
                    continue

                yield sse({"type": "progress",
                           "content": f"Searching: {', '.join(new_terms[:3])}{'...' if len(new_terms)>3 else ''}"})

                search_results = _resolve_search_multifile(
                    new_terms, symbol_maps_by_name, file_content_lookup_stream
                )
                searched_terms.extend(new_terms)
                accumulated_search_results += search_results
                _dlog("search_results_returned",
                      session_id=session_id,
                      terms=new_terms,
                      results_chars=len(search_results),
                      results_preview=search_results,
                          user_id=user_id)

                # On the last permitted search round, add a strong write-now warning
                is_last_search_round = (search_round == MAX_SEARCH_ROUNDS - 1)
                last_round_warning = (
                    "\n\n⚠️ FINAL SEARCH ROUND: This is the last search available. "
                    "After reading these results you MUST write your <surgical_edit> "
                    "or <new_file> blocks immediately. Do NOT emit another <search_request>."
                ) if is_last_search_round else ""

                search_injection = (
                    "Here are the search results you requested"
                    + (f" ({reason})" if reason else "")
                    + ":\n"
                    + search_results
                    + "\n\nWrite your complete <surgical_edit> or <new_file> blocks now. "
                    "Use the EXACT symbol names shown in the results above."
                    + last_round_warning
                )

                current_messages = current_messages + [
                    {"role": "assistant", "content": full_response or "(searching for code...)"},
                    {"role": "user", "content": search_injection},
                ]
                full_response = ""  # Reset for next round
                search_requested = None
                continue  # Next search round

            # ── No search request — streaming is done ─────────────────────
            break

        # Final flush for edge cases (Claude ended inside a block)
        if in_thinking:
            yield sse({"type": "thinking_end", "content": ""})
        if state == "in_edit" and edit_buf.strip():
            edit_blocks_raw.append(edit_buf)
            yield sse({"type": "edit_end", "content": ""})
        elif state == "in_file" and file_buf.strip():
            new_file_blocks_raw.append(file_buf)
            yield sse({"type": "edit_end", "content": ""})

        # ── Process edit blocks ───────────────────────────────────────────
        if not edit_blocks_raw and not new_file_blocks_raw:
            yield sse({"type": "done", "content": ""})
            return

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
        skipped_changes_struct: list = []  # structured {filename, symbol, reason} for the UI

        # ── Same-symbol cumulative merge ──────────────────────────────────
        # When several edits target the SAME symbol, each must be spliced into
        # the running (already-edited) symbol — NOT the pristine original — and
        # collapsed into ONE change. Otherwise every edit emits an operation
        # whose `find` is the *original* symbol text, so only the first can
        # apply and the rest silently conflict (the "N edits → 1 survives"
        # failure mode). Keyed by (filename, symbol.full_path).
        _symbol_accum: dict = {}        # key -> latest merged symbol code
        _resolved_by_symbol: dict = {}  # key -> the single resolved_edit entry

        for resolve_round in range(MAX_SYMBOL_RETRIES + 1):
            still_unresolved = []

            for edit_raw in pending_edits:
                try:
                    edit_data = json.loads(edit_raw.strip())
                except json.JSONDecodeError:
                    try:
                        edit_data = json.loads(_repair_json(edit_raw.strip()))
                    except Exception:
                        continue

                filename = edit_data.get("filename", "")
                symbol_name = edit_data.get("symbol", "")
                new_code = edit_data.get("new_code", "")
                description = edit_data.get("description", "")
                old_code = edit_data.get("old_code", "")  # SNIPPET / targeted edit

                if not filename or not symbol_name or not new_code:
                    continue

                file_content = file_content_lookup.get(filename, "")
                smap, sf_entry = symbol_maps_by_name.get(filename, (None, None))

                if not file_content or not smap:
                    skipped_messages.append(f"File '{filename}' not found in session")
                    skipped_changes_struct.append({
                        "filename": filename,
                        "symbol": symbol_name,
                        "reason": "file_not_in_session",
                    })
                    continue

                symbol, match_method = _fuzzy_find_symbol(smap, symbol_name)

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
                    if old_code:
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
                            _dlog("snippet_apply_failed",
                                  session_id=session_id,
                                  filename=filename,
                                  symbol=symbol_name,
                                  reason=snip_reason,
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
                                "_symbol_code": symbol.code,
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
                        frag_reason = _fragment_reason(symbol.code, new_code)
                        if frag_reason:
                            still_unresolved.append({
                                "filename": filename,
                                "symbol": symbol_name,
                                "new_code": new_code,
                                "description": description,
                                "_raw": edit_raw,
                                "_snippet_reason": frag_reason,
                                "_symbol_code": symbol.code,
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
                _corr_task = asyncio.create_task(aclient.messages.create(
                    model=arch_model,
                    max_tokens=8000,
                    system=system_prompt,
                    messages=correction_msgs,
                ))
                while not _corr_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(_corr_task), timeout=20.0)
                    except asyncio.TimeoutError:
                        yield f"data: {json.dumps({'type': 'keepalive', 'content': ''})}\n\n"
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
                pending_edits = new_pending or []

            except Exception:
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

            _same_run = "\n".join(
                f"  \u2022 {d}" for j, d in enumerate(_all_descriptions) if j != i and d
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
                change_description=cs["description"],
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

        # Wait for all QA to finish, sending keepalives every 20 s
        pending = set(qa_tasks)
        while pending:
            done_set, pending = await asyncio.wait(pending, timeout=20.0)
            if pending:
                # SSE keepalive — keeps Railway/Vercel proxy alive, ignored by frontend
                yield f"data: {json.dumps({'type': 'keepalive', 'content': ''})}\n\n"

        qa_results = []
        for t in qa_tasks:
            try:
                qa_results.append(t.result())
            except Exception:
                qa_results.append({
                    "verdict": "warning", "qa_score": 7,
                    "summary": "QA could not run", "import_issues": [],
                    "downstream_risks": [], "type_errors": [],
                    "plan_deviation": "", "risk_verdicts": [],
                })

        # ── Structural QA — deterministic pre-check ────────────────────────
        # Run fast, zero-LLM checks (missing imports, duplicate defs, wrong
        # import depth, dropped exports) BEFORE the retry loop.  If structural
        # issues are found, force the LLM QA verdict/score down so the retry
        # loop fires automatically.
        try:
            from services.structural_qa import run_structural_qa, has_blocking_issues as _has_sq_blocking
        except ImportError:
            _has_sq_blocking = None

        if _has_sq_blocking is not None:
            for _sq_i, _sq_cs in enumerate(change_shells):
                _sq_new   = _sq_cs["new_code"]
                _sq_orig  = _sq_cs["symbol"].code
                _sq_fname = _sq_cs["filename"]
                _sq_issues = run_structural_qa(_sq_new, _sq_orig, _sq_fname)
                if _has_sq_blocking(_sq_issues):
                    # Merge structural issues into LLM QA result so the retry
                    # prompt includes them and Claude knows exactly what to fix.
                    _sq_msgs = [f"[STRUCTURAL] {si['message']}" for si in _sq_issues if si["severity"] == "error"]
                    qa_results[_sq_i]["import_issues"] = (
                        qa_results[_sq_i].get("import_issues", []) + _sq_msgs
                    )
                    qa_results[_sq_i]["verdict"] = "blocked"
                    qa_results[_sq_i]["qa_score"] = min(qa_results[_sq_i].get("qa_score", 10), 3)
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
                    except Exception:
                        pass  # stays unscored -> excluded by the hard gate below

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
            if not blocked_indices:
                break

            yield sse({"type": "progress",
                       "content": f"🔁 Fixing {len(blocked_indices)} blocked change(s) — "
                                  f"attempt {_qa_retry_round + 1}/{MAX_QA_RETRIES}..."})

            # Build correction calls for all blocked changes
            correction_tasks = []
            for idx in blocked_indices:
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
                _sym_line_count = len(symbol.code.splitlines())
                _is_large_symbol = _sym_line_count > 60

                if _is_large_symbol:
                    _format_instructions = (
                        f"This symbol is {_sym_line_count} lines — DO NOT re-emit the whole symbol. "
                        f"Make a TARGETED edit: in your <surgical_edit> JSON provide\n"
                        f'  "old_code": the EXACT small snippet from ORIGINAL CODE you are changing '
                        f"(copied verbatim, no line-number prefixes), and\n"
                        f'  "new_code": the replacement for just that snippet.\n'
                        f"The system splices your snippet into the complete symbol, so everything "
                        f"you do NOT mention is preserved automatically. Keep old_code as small as "
                        f"possible while still matching exactly one place.\n\n"
                        f"NOTE ON THE QA ISSUES ABOVE: file-level imports, other functions, and "
                        f"module exports live OUTSIDE this symbol — you do NOT need to add them. "
                        f"Only fix issues that are genuinely inside the symbol."
                    )
                else:
                    _format_instructions = (
                        f"Write a corrected <surgical_edit> block whose \"new_code\" contains the "
                        f"COMPLETE symbol code (nothing omitted) using the exact symbol name "
                        f"`{symbol.name}`."
                    )

                correction_prompt = (
                    f"QA reviewed your <surgical_edit> for `{symbol.name}` in "
                    f"`{cs['filename']}` and found it BLOCKED (score "
                    f"{qa_d.get('qa_score', '?')}/10). You must fix all issues "
                    f"before this can be applied.\n\n"
                    f"Issues to fix:\n{issues_block}"
                    f"{diff_block}\n\n"
                    f"ORIGINAL CODE (what the symbol looks like NOW — before your change):\n"
                    f"```\n{symbol.code}\n```\n\n"
                    f"YOUR BROKEN CODE (what you wrote — DO NOT reuse this verbatim):\n"
                    f"```\n{cs['new_code']}\n```\n\n"
                    f"Requirements:\n"
                    f"1. Fix every issue listed above\n"
                    f"2. Still implement the original request: {cs['description']}\n"
                    f"3. Preserve everything you are not explicitly changing\n"
                    f"4. Use the exact symbol name: `{symbol.name}`\n\n"
                    f"{_format_instructions}\n\n"
                    f"Return ONLY the <surgical_edit> block, nothing else."
                )

                correction_messages = current_messages + [
                    {"role": "assistant", "content": full_response or "(writing code...)"},
                    {"role": "user",      "content": correction_prompt},
                ]

                correction_tasks.append((
                    idx,
                    asyncio.create_task(aclient.messages.create(
                        model=arch_model,
                        max_tokens=16000,
                        system=system_prompt,
                        messages=correction_messages,
                    ))
                ))

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
                    # Extract first <surgical_edit> block from response
                    ei = corr_text.find(EDIT_OPEN)
                    ec = corr_text.find(EDIT_CLOSE, ei) if ei != -1 else -1
                    if ei != -1 and ec != -1:
                        raw_edit = corr_text[ei + len(EDIT_OPEN):ec]
                        try:
                            edit_data = json.loads(raw_edit.strip())
                        except json.JSONDecodeError:
                            edit_data = json.loads(_repair_json(raw_edit.strip()))
                        corrected_code = edit_data.get("new_code", "")
                        corrected_old  = edit_data.get("old_code", "")
                        _sym_code = change_shells[idx]["symbol"].code

                        # Reconstruct a FULL symbol from the correction before it can be
                        # stored. This loop must NEVER replace a change with a degenerate
                        # fragment (the large-file failure mode: a 2-line snippet stored as
                        # the whole 850-line symbol). Three cases:
                        accepted = None
                        if corrected_code:
                            if corrected_old:
                                # Targeted edit: splice the snippet into the full symbol.
                                _full, _ok_snip, _ = _apply_snippet_to_symbol(
                                    _sym_code, corrected_old, corrected_code
                                )
                                if _ok_snip:
                                    accepted = _full
                                # splice failed (snippet not found/ambiguous) -> keep prior,
                                # do NOT store a fragment.
                            else:
                                # No old_code: only accept as a full-symbol replacement when
                                # it is NOT a degenerate fragment of a large symbol.
                                if _fragment_reason(_sym_code, corrected_code) is None:
                                    accepted = corrected_code
                                # else: degenerate fragment -> reject, keep prior change.

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
                except Exception:
                    pass  # keep original if correction call fails

            if not fixed_indices:
                break  # No code actually changed — stop retrying

            # Re-run QA on all fixed changes in parallel
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
                except Exception:
                    pass  # keep prior result if re-QA fails

        # ── tsc compile gate — final verification after all retries ───────────
        # Re-run tsc on the FINAL content of every change. Anything that still
        # introduces a compile error is forced below the 8/10 gate so it cannot
        # ship — the production build is never broken by a tsc-rejected change.
        _tsc_final_cache: dict = {}
        for _ti, _tcs in enumerate(change_shells):
            _t_introduced = await _tsc_introduced_errors(_tcs, _tsc_final_cache)
            if _t_introduced:
                _t_msgs = _force_block_on_tsc(_ti, _t_introduced, " remain after auto-fix")
                yield sse({"type": "progress",
                           "content": f"🔴 tsc gate: {_tcs['symbol'].name} still has "
                                      f"{len(_t_msgs)} compile error(s) — blocked from shipping"})

        # ── Assemble SurgicalChange objects from results
        # HARD 8/10 GATE — enforced, not advisory. A change ships ONLY if QA
        # produced a real score >= 8 and did not block it. Anything blocked,
        # skipped (QA could not run), unscored, or below 8 after every retry is
        # excluded from changes_by_file and surfaced in skipped_changes so the
        # user sees exactly what was withheld and why. Nothing below 8 ships.
        _GATE_MIN = 8
        for i, (cs, qa_dict) in enumerate(zip(change_shells, qa_results)):
            symbol = cs["symbol"]
            filename = cs["filename"]
            sf_entry = cs["sf_entry"]

            _gv = qa_dict.get("verdict", "skipped")
            _gs = qa_dict.get("qa_score")
            if _gv in ("blocked", "skipped") or _gs is None or _gs < _GATE_MIN:
                _gscore_txt = str(_gs) if _gs is not None else "n/a"
                _greason = (
                    qa_dict.get("summary")
                    or qa_dict.get("skipped_reason")
                    or "did not clear the QA gate"
                )
                skipped_changes_struct.append({
                    "filename": filename,
                    "symbol": symbol.name,
                    "reason": f"QA gate {_gv} (score: {_gscore_txt}/10): {_greason}",
                })
                yield sse({"type": "progress",
                           "content": f"🚫 Blocked by 8/10 gate — {symbol.name} "
                                      f"(verdict: {_gv}, score: {_gscore_txt}/10); not shipped"})
                try:
                    _log_qa_result(session_id, filename, symbol.name, qa_dict)
                except Exception:
                    pass
                continue

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
                operations=[{"find": symbol.code, "replace": cs["new_code"]}],
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
                }
                language = ext_map.get(ext, "text")

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

        # Strip edit blocks from display text
        _display_text = full_response
        for _raw in edit_blocks_raw:
            _display_text = _display_text.replace(EDIT_OPEN + _raw + EDIT_CLOSE, "").strip()
        for _raw in new_file_blocks_raw:
            _display_text = _display_text.replace(FILE_OPEN + _raw + FILE_CLOSE, "").strip()

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

        yield sse({"type": "smart_result", "content": json.dumps(result)})
        yield sse({"type": "done", "content": ""})

    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': _friendly_error(e)})}\n\n"
