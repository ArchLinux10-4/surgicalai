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
from pathlib import Path
from typing import Optional
from openai import OpenAI
import httpx

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
_THINKING_CAPABLE_PATTERNS = ("claude-sonnet-4", "claude-3-7", "claude-3-5")

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

FOR REDESIGN / RESTYLE / COMPLETE REWRITE (> ~30% of symbol changing):
Use a single block covering the entire symbol body — SEARCH = full original symbol, REPLACE = full new symbol.

FOR TARGETED CHANGES (bug fix, add a field, change a color):
Use small precise blocks — one per change location.

STYLE RULES:
- Targeted tweaks: prefer existing color values/CSS variables.
- Redesign/restyle/modernize: freely introduce new colors, gradients, glassmorphism, modern DaaS/SaaS patterns.
- Preserve TypeScript types. Match indentation exactly (spaces vs tabs, 2 vs 4 spaces).

IF ALREADY CORRECT: output a single empty block pair to signal no change needed:
<<<<<<< SEARCH
=======
>>>>>>> REPLACE

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

    user_msg = f"""CHANGE PLAN:
Type: {target.change_type.value}
Description: {target.description}
New logic required: {target.new_logic}{_import_hint}{_file_header}{_semantic_section}{_linter_block}{_extra_ctx_block}{_qa_feedback_block}

CONTEXT BEFORE (read-only reference, do NOT include in operations):
{before_context}

TARGET CODE (lines {symbol.start_line}-{symbol.end_line}) -- your "find" text should come from here:
{symbol.code}

CONTEXT AFTER (read-only reference, do NOT include in operations):
{after_context}

Return SEARCH/REPLACE blocks ONLY. No JSON, no explanations outside blocks."""

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


async def analyze_and_plan_stream(
    file_path: str,
    file_content: str,
    user_request: str,
    session_id: Optional[str] = None,
    project_memory: Optional[str] = None,
    pinned_context: Optional[list] = None,
    user_id: str = ""
):
    """
    Streaming surgical analysis. Yields SSE progress events then final result.
    """
    try:
        yield f"data: {json.dumps({'type': 'progress', 'content': 'Parsing file structure...'})}\n\n"
        symbol_map = parser.parse(file_content, file_path)

        yield f"data: {json.dumps({'type': 'progress', 'content': f'Found {len(symbol_map.symbols)} symbols. Running Architect...'})}\n\n"

        plan = run_architect(symbol_map, user_request, file_content, user_id=user_id)

        yield f"data: {json.dumps({'type': 'progress', 'content': f'Plan: {len(plan.targets)} changes identified. Running Surgeon...'})}\n\n"

        changes = []

        for i, target in enumerate(plan.targets):
            yield f"data: {json.dumps({'type': 'progress', 'content': f'Surgeon working on {target.symbol_path} ({i+1}/{len(plan.targets)})...'})}\n\n"

            symbol = None
            for sym in symbol_map.symbols:
                if sym.full_path == target.symbol_path or sym.name == target.symbol_path:
                    symbol = sym
                    break

            if symbol is None:
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
                        _inj_iss_add, _inj_fn_add = _is_script_injection_issue(parent_symbol.code, new_code)
                        _ins_mode_add = False
                        _ins_anc_add = None
                        if _inj_iss_add and _inj_fn_add:
                            _ins_anc_add = _find_script_close_line(file_content, parent_symbol.end_line)
                            _ins_mode_add = True
                            _tgt_elem = None
                            _replacement = _inj_fn_add
                            new_code = parent_symbol.code
                        changes.append(SurgicalChange(
                            id=str(uuid.uuid4()),
                            symbol=parent_symbol,
                            original_code=parent_symbol.code,
                            new_code=new_code if not _ins_mode_add else parent_symbol.code + "\n" + _inj_fn_add,
                            diff=diff,
                            confidence=confidence,
                            description=target.description,
                            applied=False,
                            target_element=_tgt_elem,
                            replacement=_replacement,
                            insert_mode=_ins_mode_add,
                            insert_anchor=_ins_anc_add
                        ))
                continue

            if target.change_type == ChangeType.DELETE:
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

            new_code, confidence, _surg_notes, _needed_imports, _operations = run_surgeon(symbol, target, file_content, user_id=user_id)
            diff = _make_diff(symbol.code, new_code, target.symbol_path)
            _tgt_elem, _replacement = _compute_target_element(symbol.code, new_code)

            # v3.3.1: detect script injection truncation / phantom </script>
            _inject_issue, _injected_fn = _is_script_injection_issue(symbol.code, new_code)
            _insert_mode = False
            _insert_anchor = None
            if _inject_issue and _injected_fn:
                _insert_anchor_line = _find_script_close_line(file_content, symbol.end_line)
                _insert_mode = True
                _insert_anchor = _insert_anchor_line
                _tgt_elem = None
                _replacement = _injected_fn
                new_code = symbol.code  # keep original for display; diff will show only additions

            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code if not _insert_mode else symbol.code + "\n" + _injected_fn,
                diff=diff,
                confidence=confidence,
                description=target.description,
                applied=False,
                target_element=_tgt_elem,
                replacement=_replacement,
                insert_mode=_insert_mode,
                insert_anchor=_insert_anchor
            )
            changes.append(change)

        result = SurgicalAnalyzeResponse(
            session_id=session_id or str(uuid.uuid4()),
            plan=plan,
            changes=changes,
            tokens_used=0
        )

        yield f"data: {json.dumps({'type': 'result', 'content': result.model_dump_json()})}\n\n"
        yield f"data: {json.dumps({'type': 'done', 'content': ''})}\n\n"

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


SMART_ARCHITECT_SYSTEM = """You are SurgicalAI — a two-model coding system. You are the ARCHITECT.

Your only job is to READ THE MAP and produce a precise plan. You never write code yourself.
The Surgeon (GPT-4.1) will receive your plan and execute only what you specify — nothing more.

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
      "new_logic": "precise description of the new behavior — the Surgeon will implement exactly this. QUALITY RULE: Reference ACTUAL variable/function names from the code. Be specific: 'After const fee = calcFee(), add: if (fee < 0) throw new Error()'. Never say just 'add error handling' — always say WHAT, WHERE, and HOW.",
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
- NEW CODE: the complete, untruncated code block after the change
- OTHER FILES CONTEXT: symbol maps of other files for cross-file checks

IMPORTANT: You have the FULL original and FULL new code — there is no truncation.
Compare them directly, like a real code reviewer would. Do NOT ask for a diff or complain
about missing context — everything you need is in ORIGINAL CODE and NEW CODE.

Check ALL of the following by comparing ORIGINAL → NEW:
1. COMPLETENESS — does NEW CODE contain everything it should? Flag anything dropped that wasn't part of the plan.
2. PLAN COMPLIANCE — does NEW CODE implement exactly what was asked? No more, no less.
3. SYNTAX — unclosed brackets, missing semicolons, malformed JSX/TSX tags, broken template literals.
4. IMPORT ISSUES — does new code use anything that needs an import not already present in the file?
5. TYPE ERRORS — obvious type mismatches, wrong argument counts, wrong return types.
6. DUPLICATION — is any function, component, or block defined twice?
7. DOWNSTREAM RISKS — does this change break signatures, exported types, or constants other files depend on?
8. ARCHITECT RISKS — evaluate each provided risk against actual code changes.

SCORING RULES (ENFORCE STRICTLY):
9-10 → safe:    Change exactly matches plan, no issues, all imports present
7-8  → safe:    Minor style notes but nothing breaking
5-6  → warning: Potential issue user should review (unclear import, subtle type change, minor scope creep)
3-4  → blocked: Likely broken — missing critical imports, wrong signature, obvious logic error
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
  "plan_deviation": "<empty string if none, or exact description of deviation>",
  "risk_verdicts": [
    {"risk": "<exact text from architect_risks>", "status": "verified_safe|warning|blocked", "reason": "<one sentence>"}
  ]
}
risk_verdicts must have one entry per item in architect_risks. If architect_risks is empty, return []."""


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

    # v3.11.4: QA receives FULL code — no truncation, no diff confusion.
    # Claude Sonnet has 200K context. A full symbol is typically <20KB (~5K tokens).
    # Sending full original + full new lets QA compare directly, like a real code reviewer.
    # Hard cap at 60K chars per block to guard against pathological files (still ~15K tokens each).
    _MAX_CODE_CHARS = 60_000
    _orig_snippet = original_code if len(original_code) <= _MAX_CODE_CHARS else (
        original_code[:_MAX_CODE_CHARS] + f"\n... [hard cap: file exceeds {_MAX_CODE_CHARS} chars — review manually]"
    )
    _new_snippet = new_code if len(new_code) <= _MAX_CODE_CHARS else (
        new_code[:_MAX_CODE_CHARS] + f"\n... [hard cap: file exceeds {_MAX_CODE_CHARS} chars — review manually]"
    )
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

Compare ORIGINAL CODE → NEW CODE directly. Run all 8 checks and return the JSON verdict.

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
        if result["qa_score"] >= 6 and result["verdict"] == "blocked":
            result["verdict"] = "warning"
        if result["qa_score"] <= 5 and result["verdict"] == "safe":
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
        cur = conn.cursor()
        # Works for both SQLite and PostgreSQL (psycopg2 uses %s, sqlite3 uses ?)
        try:
            cur.execute(
                """INSERT INTO qa_log (session_id, filename, symbol_name, verdict, qa_score, issues_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, filename, symbol_name,
                 result.get("verdict", "skipped"),
                 result.get("qa_score"),
                 issues_json)
            )
        except Exception:
            # PostgreSQL uses %s placeholders
            cur.execute(
                """INSERT INTO qa_log (session_id, filename, symbol_name, verdict, qa_score, issues_json)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
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
                cur = conn.cursor()
                steps_json = json.dumps(self.steps)
                missing_json = json.dumps(self.missing_steps())
                _pass = 1 if self.overall_pass() else 0
                try:
                    cur.execute(
                        """INSERT INTO compliance_log
                           (run_id, session_id, intent, steps_json, missing_steps, overall_pass)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (self.run_id, self.session_id, self.intent, steps_json, missing_json, _pass)
                    )
                except Exception:
                    cur.execute(
                        """INSERT INTO compliance_log
                           (run_id, session_id, intent, steps_json, missing_steps, overall_pass)
                           VALUES (%s, %s, %s, %s, %s, %s)""",
                        (self.run_id, self.session_id, self.intent, steps_json, missing_json, _pass)
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # Never let compliance logging kill the pipeline


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
                    user_content = [{"type": "text", "text": _react_context}]
                    for img_sf in image_files:
                        img_data = img_sf["content"]
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

                # -- Retry loop: up to 2 attempts for transient 500/529 errors --
                _claude_attempt = 0
                while _claude_attempt < 2:
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
                        if _is_transient and _claude_attempt < 2:
                            yield sse({"type": "progress",
                                       "content": "AI service hiccup -- retrying once..."})
                            await asyncio.sleep(2)
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
                    # Try to extract JSON object from the text
                    # Manual JSON extraction (avoids 're' module scoping issues)
                    _brace_start = raw_text.find('{')
                    _brace_end = raw_text.rfind('}')
                    json_match = (raw_text[_brace_start:_brace_end + 1]
                                  if _brace_start >= 0 and _brace_end > _brace_start
                                  else None)
                    if json_match:
                        try:
                            plan = json.loads(json_match)
                        except json.JSONDecodeError:
                            # Last resort: treat entire response as chat
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
            MIN_NARROW_SCORE = 10  # one name-match (+10) or two code-matches (+5+5)
            _REDESIGN_KEYWORDS = ("redesign", "restyle", "rewrite", "modernize",
                                  "overhaul", "revamp", "refactor")
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
                    forbid_noop=bool(_qa_feedback_for_retry),
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
                    if _lint_new_count > _lint_orig_count:
                        _linter_introduced_errors = _validate_lint(_full_after_lint, matched_name)
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
                    elif _lint_new_count == 0 and _lint_orig_count == 0:
                        yield sse({"type": "progress", "content": f"✅ {_lint_tool} clean"})
                    else:
                        yield sse({"type": "progress", "content": f"⏭ {_lint_tool} skipped (file has {_lint_orig_count} pre-existing issue(s))"})
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
                    and (_qa_result.get("qa_score") or 10) <= 4
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
