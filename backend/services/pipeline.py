"""
Two-model AI pipeline.
- Architect: GPT-5 (or configured model) — reads symbol map, reasons, produces plan
- Surgeon: GPT-4.1 — receives plan + code chunk, writes minimal precise replacement

Best Practice #1: Read the map before touching the territory (AST-first)
Best Practice #2: Minimal footprint (surgeon only touches requested symbol)
Best Practice #3: Verify before commit (confidence scoring + diff)
"""
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
    "Format code blocks with syntax highlighting. Use markdown."
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
Your job: produce EXACT search-and-replace operations that implement the plan.

You will receive:
- FILE HEADER: top of the file (imports, key state/variables) for reference
- CONTEXT BEFORE: lines just before the target symbol
- TARGET CODE: the symbol you are editing — your "find" strings MUST come from here
- CONTEXT AFTER: lines just after the target symbol

OUTPUT FORMAT (return ONLY this JSON, nothing else):
{
  "operations": [
    {"find": "exact text from the code", "replace": "replacement text"}
  ],
  "confidence": 8,
  "reasoning": "one-line explanation",
  "imports_needed": ["import xyz"]
}

HARD RULES:
1. "find" MUST be an EXACT substring of the TARGET CODE or a well-known anchor (</script>, </body>, etc).
   Copy it character-for-character, including all whitespace and indentation.
2. "find" should be the MINIMUM text needed to UNIQUELY identify the location. Usually 2-6 lines.
   If a short string like </div> could match many places, include the surrounding 2-3 lines.
3. "replace" is the new text for that exact location. Preserve original indentation style.
4. Each operation = ONE logical change. Multiple changes = multiple operations.
5. Do NOT include unchanged surrounding code in your operations.
6. MULTI-PART CHANGES: if the plan requires changes in multiple locations (e.g. add CSS + add state + modify JSX),
   produce one operation per location. Verify each "find" string is unique in the file.

CHANGE TYPES:
- MODIFY/WRAP: find = the element to change, replace = the modified version.
  Example wrapping an input: find the <input> tag, replace with <div><input><button></div>
- ADD/INSERT: find = an anchor line (like </script> or a closing tag), replace = new code + that anchor.
  Example: find "</script>", replace "function newFunc(){...}\n</script>"
- DELETE: find = the lines to remove, replace = "" (empty string).

STYLE RULES (for UI/CSS changes):
- Use ONLY colors and CSS variables that already exist in the FILE HEADER or TARGET CODE.
  Do NOT invent new color values. If in doubt, reuse an existing color exactly as written.
- Preserve TypeScript types. If adding a new useState hook, infer the type from surrounding hooks.
- Match the indentation style exactly (spaces vs tabs, 2 vs 4 spaces).

ALREADY CORRECT RULE:
If the TARGET CODE already implements what the plan describes, return:
{"operations": [], "confidence": 10, "reasoning": "already implemented"}

CRITICAL - NO EXTRA STRUCTURE:
- When wrapping an HTML element, wrap ONLY the exact element specified.
  Do NOT add extra labels, divs, aria attributes, or any structure not in the plan.
- When adding a script function, write ONLY the function. Do NOT duplicate existing code.
- Count the elements in the plan. Your operations should produce exactly that many changes.

Return ONLY the JSON object. No markdown fences, no preamble, no explanation outside the JSON."""


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
    architect_risks: list = None
) -> tuple:
    """
    GPT-4.1 (Surgeon): receives ONE code chunk + plan, returns search-and-replace operations.
    Returns (new_code, confidence, surgeon_notes, import_needed, operations).

    v3.4.0: Surgeon returns JSON operations [{find, replace}] instead of rewriting code blocks.
    The LLM decides WHAT to change; the backend applies changes mechanically. Zero truncation risk.
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
    user_msg = f"""CHANGE PLAN:
Type: {target.change_type.value}
Description: {target.description}
New logic required: {target.new_logic}{_import_hint}{_file_header}{_semantic_section}

CONTEXT BEFORE (read-only reference, do NOT include in operations):
{before_context}

TARGET CODE (lines {symbol.start_line}-{symbol.end_line}) -- your "find" text should come from here:
{symbol.code}

CONTEXT AFTER (read-only reference, do NOT include in operations):
{after_context}

Return JSON with search-and-replace operations."""

    response = _chat_create(client,
        model=surg_model,
        messages=[
            {"role": "system", "content": SURGEON_SYSTEM},
            {"role": "user", "content": user_msg}
        ],
        temperature=temp,
        response_format={"type": "json_object"}
    )

    raw = response.choices[0].message.content

    # Parse JSON response
    operations = []
    confidence = target.confidence
    surgeon_notes = []
    import_needed_lines = []

    try:
        data = json.loads(raw)
        operations = data.get("operations", [])
        confidence = data.get("confidence", target.confidence)
        reasoning = data.get("reasoning", "")
        import_needed_lines = data.get("imports_needed", [])
        if reasoning:
            surgeon_notes.append(f"Surgeon: {reasoning}")

        # Fix double-escaped newlines: GPT often returns \\n in JSON strings
        # instead of \n, resulting in literal backslash-n after json.loads
        for _op in operations:
            for _key in ("find", "replace"):
                if _key in _op and isinstance(_op[_key], str):
                    _val = _op[_key]
                    # Debug: log what we see before processing
                    if "\\n" in _val or "\\t" in _val:
                        print(f"[ESCAPE-FIX] {_key} has literal backslash-n, converting ({len(_val)} chars)")
                    _op[_key] = _val.replace("\\n", "\n").replace("\\t", "\t")
                    # Check if there are still escaped sequences (triple-escape)
                    if "\\n" in _op[_key]:
                        print(f"[ESCAPE-FIX] Still has \\\\n after first pass, doing second pass")
                        _op[_key] = _op[_key].replace("\\n", "\n")
    except json.JSONDecodeError:
        # Fallback: treat as old-style code block (pre-v3.4.0 model behavior)
        if raw.lstrip().startswith("```"):
            fence_lines = raw.split("\n")
            start_i = next((i for i, l in enumerate(fence_lines) if l.strip().startswith("```")), 0) + 1
            end_i = len(fence_lines) - 1 if fence_lines[-1].strip() == "```" else len(fence_lines)
            raw = "\n".join(fence_lines[start_i:end_i])
        new_code = raw.rstrip()
        while new_code.startswith("\n"):
            new_code = new_code[1:]
        return new_code, confidence, surgeon_notes, import_needed_lines, []

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
            mod_lines = modified_file.splitlines()
            orig_lines = file_content.splitlines()
            # Use symbol line range with ±5 padding
            win_start = max(0, symbol.start_line - 6)  # start_line is 1-indexed
            win_end = min(len(mod_lines), symbol.end_line + 5)
            new_code = "\n".join(mod_lines[win_start:win_end])
            _original_for_qa = "\n".join(orig_lines[win_start:min(len(orig_lines), win_end)])
            print(f"[MATCH] apply_operations OK: {len(operations)} ops, window L{win_start+1}-L{win_end}")
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

4. "search" — use this when:
   - The symbol map was capped AND you cannot see the specific element mentioned in the request
   - A KEYWORD MATCH section shows surrounding context but NOT the element you need to edit
   - You need to find an input/function/element by a name or ID not yet visible in your context
   - ONLY use this up to 3 times per request — you have a limited search budget
   - NEVER request terms already listed in ALREADY SEARCHED TERMS
   - As soon as you locate the target element -> switch to "edit" intent immediately

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
      // OPTIONAL — semantic sections the Surgeon needs from OUTSIDE the target symbol.
      // Use when the change spans multiple file regions (e.g. add animation CSS + add useState + modify JSX).
      // Available values: "style_block" | "state_declarations" | "hooks" | "css_vars"
      //                   | "imports_block" | "type_declarations" | "constants"
      // Rule: only include what the Surgeon genuinely needs to write correct code.
      // Leave empty [] for simple single-location edits.
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

IF search:
{
  "intent": "search",
  "reasoning": "one sentence: what you found so far and what you still need to locate",
  "search_terms": ["exactId", "type=\"date\"", "<input"],
  "confidence_if_found": 8
}

━━━ INTENT ROUTING — READ THIS CAREFULLY ━━━

DEFAULT RULE: If the user asks to change, add, modify, update, fix, or refactor ANY code — use "edit" intent.
ONLY use "chat" for pure questions ("what does X do?", "explain Y", "why is Z slow?") that require NO code changes.
ONLY use "needs_clarification" when you genuinely cannot identify WHICH file or WHAT to change.

If you can see the file and you can describe the change — use "edit". Do NOT fall back to "chat" just because
the change is small or simple.

━━━ CREATIVE / GENERATIVE REQUESTS ━━━
If the user asks for "design options", "variants", "alternatives", "mockups", different "versions",
"approaches", or to "reimagine" / "redesign" / "rewrite from scratch" — this is a CREATIVE request.
Use "chat" intent and provide your full response in markdown with complete code blocks for each option.
The Surgeon is for precision edits to EXISTING code, NOT for generating entirely new code variants.
Similarly, if the user says "rewrite this entire file" or "start from scratch" — use "chat" with full code in markdown.

━━━ DIAGNOSIS BEST PRACTICES ━━━
[Injected dynamically for debug/bug requests — see _build_architect_system()]

━━━ IMPORT DEPENDENCY CHECK (DO THIS FIRST) ━━━

Before forming any edit plan, do the following:

1. SCAN IMPORTS — Look at every import/require statement in every uploaded file.
   Build a mental list of files those imports reference (e.g. `../api/client`, `./stores/appStore`, `services/pipeline`).

2. IDENTIFY DEPENDENCIES — For the specific change the user is asking for, ask:
   "Does implementing this change require calling functions, types, or constants from an imported file
   that was NOT uploaded?" If yes → those are MISSING FILES.

3. ACT ON MISSING FILES:
   - If the change would generate a NEW call into a missing file (e.g. adding `api.chat.search()`
     but `api/client.ts` was not uploaded) → use "needs_clarification" intent.
   - List the missing files by name in your questions: "To implement this I also need `src/api/client.ts`
     — could you upload it? I don't want to guess the method signature."
   - NEVER invent function names, method signatures, or type shapes for files you haven't seen.
     Guessing silently causes broken code that's hard to debug.

4. EXCEPTION — If the change is self-contained within the uploaded file(s) and does NOT call into
   any external file, proceed with "edit" intent normally.

Example:
  - User uploads `Sidebar.tsx`, asks "add a search bar that calls the API"
  - Sidebar.tsx imports from `../api/client` but that file wasn't uploaded
  - CORRECT: return needs_clarification — "I need `src/api/client.ts` to see the correct method
    signature before I add the API call. Could you upload it?"
  - WRONG: invent `api.chat.search(query)` and hope it's right

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
- For TSX/JSX/HTML/CSS files: use the component function or export name as symbol_path (e.g. "LoginPage", "App", "Header").
  Inner elements like divs/spans are NOT symbols — but that's OK. Set symbol_path to the containing component
  and use description + new_logic to precisely identify the inner element to change.
  IMPORTANT FOR LARGE HTML FILES: When a SEARCH RESULT or KEYWORD MATCH section shows the exact line of
  an element (e.g. "rsp-eff-date at L9318"), set target_line to that line number (e.g. 9318).
  This lets the pipeline create a focused edit window exactly where needed — not 500 lines away.
  For multi-instance edits ("add X to ALL Y fields"), each target MUST have a different target_line.
- confidence scale: 9-10=clear isolated change; 7-8=has dependencies; 5-6=ambiguous → use needs_clarification; <5=missing files → ask
- confidence < 7 = use needs_clarification instead (enforce this — do NOT guess at low confidence)
- Minimal footprint: if the user said "add X to function Y", only plan function Y"""



# ─────────────────────────────────────────────────────────────────────────────
# QA AGENT — runs after every Surgeon output, before diff card is shown to user
# ─────────────────────────────────────────────────────────────────────────────

QA_SYSTEM = """You are the QA agent in a two-model coding pipeline.
The Surgeon has just produced a code replacement. Your job: verify it is correct, complete, and safe.

You receive:
- ORIGINAL: the code being replaced
- NEW CODE: what the Surgeon produced
- CHANGE PLAN: what was asked for
- OTHER FILES CONTEXT: symbol maps of other uploaded files (for cross-file checks)

Check for ALL of the following:
1. IMPORT ISSUES — does new code call/use anything that needs an import not in the file?
2. DOWNSTREAM RISKS — does this change affect function signatures, types, or constants that other files depend on?
3. TYPE ERRORS — obvious type mismatches, wrong argument counts, wrong return types
4. PLAN DEVIATION — does new code do something OTHER than what the plan says? (adding/removing unasked features)
5. DUPLICATION — is any code duplicated (same function defined twice, same block copy-pasted)?
6. ALREADY CORRECT — if new code is identical to original, flag it
7. ARCHITECT RISKS — if architect_risks are provided, evaluate each one against the actual diff and mark whether it truly applies to THIS specific change

Respond with ONLY valid JSON — no explanation outside the JSON:
{
  "verdict": "safe" | "warning" | "blocked",
  "qa_score": <integer 1-10>,
  "summary": "<one sentence>",
  "import_issues": ["<issue>"],
  "downstream_risks": ["<risk>"],
  "type_errors": ["<error>"],
  "plan_deviation": "<empty string if none, or description of deviation>",
  "risk_verdicts": [
    {"risk": "<exact text from architect_risks>", "status": "verified_safe|warning|blocked", "reason": "<one sentence why>"}
  ]
}
Note: risk_verdicts should have one entry per item in architect_risks. If architect_risks is empty, return [].
status meanings: verified_safe=does not apply to this change, warning=possibly relevant, blocked=confirmed real risk.

Score guide (ENFORCE STRICTLY):
9-10 → safe:    Change exactly matches plan, no issues, all imports present
7-8  → safe:    Minor notes but nothing breaking
5-6  → warning: Potential issue user should review — imports unclear, subtle signature change, minor scope creep
3-4  → blocked: Likely broken — missing critical imports, wrong function signature, obvious logic error
1-2  → blocked: Severely wrong — duplicated code, completely wrong output, plan not implemented at all

verdict MUST match score: score ≥6 = safe or warning, score ≤5 = blocked.
A warning verdict means Apply is allowed but user sees a yellow banner.
A blocked verdict means Apply is disabled until user overrides."""


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
) -> dict:
    """
    QA agent: verifies Surgeon output before showing diff card to user.
    Uses gpt-4.1-mini for speed/cost efficiency.
    Returns a dict matching QAResult schema.
    Guaranteed to return a result — never raises (skipped verdict on error).
    """
    _risks_list = architect_risks or []
    _risks_block = "\n".join(f"- {r}" for r in _risks_list) if _risks_list else "(none — skip risk_verdicts)"
    import asyncio

    qa_model = "gpt-4.1-mini"

    # v3.4.0: Send DIFF to QA instead of truncated code blocks.
    # The change may be deep in a large window — truncating at 3000 chars can hide it entirely.
    _diff_lines = list(difflib.unified_diff(
        original_code.splitlines(keepends=True),
        new_code.splitlines(keepends=True),
        fromfile="original",
        tofile="modified",
        n=5  # 5 lines of context around each change
    ))
    _diff_text = "".join(_diff_lines)[:4000]
    if not _diff_text.strip():
        _diff_text = "(no visible changes — code is identical)"

    # Also include a small focused snippet: first 1500 chars of original + new for structure context
    _orig_snippet = original_code[:1500] + ("\n... [truncated]" if len(original_code) > 1500 else "")
    _new_snippet  = new_code[:1500]      + ("\n... [truncated]" if len(new_code) > 1500 else "")
    other_ctx    = other_files_context[:2000] + ("\n... [truncated]" if len(other_files_context) > 2000 else "")

    user_msg = f"""CHANGE PLAN:
Symbol: {symbol_path}
File: {filename}
Description: {change_description}
Expected behavior: {new_logic}

UNIFIED DIFF (what actually changed):
{_diff_text}

ORIGINAL CODE (first 1500 chars for structure context):
{_orig_snippet}

NEW CODE (first 1500 chars for structure context):
{_new_snippet}

OTHER FILES IN SESSION (for cross-file checking):
{other_ctx if other_ctx.strip() else "(no other files uploaded)"}

IMPORTANT: Focus on the DIFF above — it shows exactly what lines were added/removed.
Run all 6 checks and return the JSON verdict.

ARCHITECT PRE-ANALYSIS RISKS (evaluate each in risk_verdicts):
{_risks_block}"""

    try:
        client = _get_client(user_id)

        def _call():
            return _chat_create(
                client,
                model=qa_model,
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
        "architect_routing": ["edit", "chat", "needs_clarification"],
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
        if not session_files:
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
                # ── Smart symbol selection for large files ──
                # Sort: prefer smaller (more surgical) symbols; functions before HTML elements
                _PRIORITY_TYPES = {"function": 0, "method": 1, "class": 2, "variable": 3}
                _sorted_syms = sorted(
                    smap.symbols,
                    key=lambda s: (
                        _PRIORITY_TYPES.get(s.symbol_type.value, 9),  # functions first
                        s.end_line - s.start_line,                    # smaller symbols first
                    )
                )
                _MAX_SYMS = 60
                _SNIPPET_LEN = 80  # shorter snippets reduce noise for large files
                syms = []
                for s in _sorted_syms[:_MAX_SYMS]:
                    size = s.end_line - s.start_line + 1
                    size_warn = " ⚠️LARGE" if size > 500 else ""
                    entry = f"  [{s.symbol_type.value}] {s.full_path} ({size} lines, L{s.start_line}-{s.end_line}){size_warn}"
                    snippet = s.code[:_SNIPPET_LEN].replace("\n", " ").strip()
                    if snippet:
                        entry += f"  >> {snippet}"
                    if s.signature:
                        entry += f" — {s.signature}"
                    syms.append(entry)
                _total_syms = len(smap.symbols)
                _sym_header = f"SYMBOLS ({_total_syms} total" + (f", showing {_MAX_SYMS} most targeted" if _total_syms > _MAX_SYMS else "") + "):"
                _sym_suffix = "" if _total_syms <= _MAX_SYMS else f"\n  ... [{_total_syms - _MAX_SYMS} more — use narrowest symbol rule; prefer small symbols under 200 lines]"
                _file_summary = (
                    f"FILE: {fname} ({sf.get('lines', len(content.splitlines()))} lines, {sf.get('language', 'code')})\n"
                    f"{_sym_header}\n" + ("\n".join(syms) + _sym_suffix if syms else "  (no symbols parsed)")
                )
                # ── Grep injection for large files (>_MAX_SYMS hidden symbols) ──
                # When Architect can only see 60 of 338 symbols, grep the file
                # for terms from the user's request and inject matching sections.
                # This is how Tasklet works: grep first, then edit.
                if _total_syms > _MAX_SYMS:
                    # Also extract terms from the last user answer in history
                    # (catches cases where user answered a clarification question)
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
                        output_summary=f"{len([f for f in file_summaries if 'SYMBOLS:' in f])} files parsed with symbol maps")
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

            # ReAct loop state
            _REACT_MAX_ROUNDS = 4
            _react_round = 0
            _react_searched_terms = []
            _react_accumulated = ""   # accumulated grep sections across all rounds
            _react_budget_lines = 0   # total lines injected (budget guard at 2500)

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

                if _react_budget_lines < 2500:
                    yield sse({"type": "progress", "content":
                               "Searching for: {} (round {}/{})...".format(
                                   ", ".join(_new_terms[:3]), _react_round, _REACT_MAX_ROUNDS
                               )})
                    for _fname_r, (_smap_r, _sf_r) in symbol_maps_by_name.items():
                        _fcontent_r = _sf_r.get("content", "")
                        if not _fcontent_r:
                            continue
                        _grep_r = _grep_relevant_sections(
                            "", _fname_r, _fcontent_r,
                            extra_terms=_new_terms,
                            window=100, max_lines=300,
                        )
                        if _grep_r:
                            _react_accumulated += "\n" + _grep_r
                            _react_budget_lines += _grep_r.count("\n")
                    _react_searched_terms.extend(_new_terms)
                    # Emit found line ranges so user can see what was found
                    _found_line_nums = re.findall(r'Lines (\d+)-\d+:', _grep_r) if _grep_r else []
                    if _found_line_nums:
                        yield sse({"type": "progress", "content":
                                   "Found matches at: {}".format(
                                       ", ".join("L{}".format(l) for l in _found_line_nums[:4])
                                   )})
                    elif _react_round > 1:
                        yield sse({"type": "progress", "content":
                                   "Round {}: no new matches for: {}".format(
                                       _react_round, ", ".join(_new_terms[:3])
                                   )})
                    # Persist to session cache for follow-up edits
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
        # If it leaks through (OpenAI path), convert to needs_clarification.
        if intent == "search":
            intent = "needs_clarification"
            plan.setdefault("clarification_response",
                "I need to look for more code context. Could you paste a snippet "
                "of the code you want to change, or give me the exact element ID "
                "or function name?")
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
            )

            # ── Oversized symbol guardrail ──
            # If symbol is >500 lines, try to find a more specific child symbol
            symbol_size = symbol.end_line - symbol.start_line + 1
            if symbol_size > 500:
                # Look for child symbols within this symbol's range
                child_candidates = [
                    s for s in smap.symbols
                    if s.start_line >= symbol.start_line
                    and s.end_line <= symbol.end_line
                    and s.full_path != symbol.full_path
                    and (s.end_line - s.start_line + 1) < symbol_size
                ]
                # Try to find a child that matches the target description
                desc_lower = (target.get("description", "") + " " + target.get("new_logic", "")).lower()
                best_child = None
                best_score = 0
                for child in child_candidates:
                    child_size = child.end_line - child.start_line + 1
                    if child_size > 500:
                        continue  # Skip huge children too
                    # Score by keyword match in symbol name/code
                    score = 0
                    child_name_lower = child.full_path.lower()
                    for keyword in desc_lower.split():
                        if len(keyword) > 3 and keyword in child_name_lower:
                            score += 10
                        if len(keyword) > 3 and keyword in child.code[:500].lower():
                            score += 5
                    # Prefer smaller symbols (more surgical)
                    if child_size < 100:
                        score += 3
                    if score > best_score:
                        best_score = score
                        best_child = child
                if best_child:
                    yield sse({"type": "progress", "content": f"Narrowing: {symbol.full_path} ({symbol_size}L) → {best_child.full_path} ({best_child.end_line - best_child.start_line + 1}L)"})
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

            new_code, confidence, _surg_notes, _needed_imports, _operations = run_surgeon(symbol, change_target, sf["content"], user_id=user_id)
            # Trace: operations applied (visible in Railway logs only)
            _is_changed = new_code.rstrip() != symbol.code.rstrip()
            print(f"[PIPELINE] {len(_operations)} ops applied, changed={_is_changed}, new={len(new_code)}, orig={len(symbol.code)}")
            diff = _make_diff(symbol.code, new_code, symbol_path)

            # ── QA Agent: verify Surgeon output before showing to user ──
            _other_ctx_for_qa = "\n\n".join(
                p for p in _qa_other_context_parts
                if not p.startswith(f"FILE: {matched_name}")
            )
            # v3.4.0: use file-window original if Tier 4 matching was used
            _effective_original = getattr(symbol, "_file_window_original", None) or symbol.code

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
            )
            # Emit QA progress to user
            _qa_icon = {"safe": "✅", "warning": "⚠️", "blocked": "🚫", "skipped": "⏭"}.get(
                _qa_result.get("verdict", "skipped"), "⏭"
            )
            yield sse({"type": "progress", "content": f"QA {_qa_icon} {_qa_result.get('summary', '')} (score: {_qa_result.get('qa_score', '?')})"})
            _qa_ran = _qa_result.get("status") not in ("timeout", "error")
            compliance.mark("qa_review", ran=_qa_ran,
                            reason=(None if _qa_ran else _qa_result.get("status")),
                            output_summary=f"verdict={_qa_result.get('verdict')} score={_qa_result.get('qa_score')}")

            # ── v3.9.2: Compile-time syntax validation (tree-sitter) ──
            try:
                from services.syntax_validator import validate_syntax as _validate_syntax
                from services.syntax_validator import count_errors as _count_errors
                # Compare error count before/after surgeon's operations
                # This avoids false positives from pre-existing issues in the file
                _orig_err_count = _count_errors(sf["content"], matched_name)
                _full_after_ops = sf["content"]
                for _sop in _operations:
                    _sfind = _sop.get("find", "")
                    _srepl = _sop.get("replace", "")
                    if _sfind and _sfind in _full_after_ops:
                        _full_after_ops = _full_after_ops.replace(_sfind, _srepl, 1)
                _new_err_count = _count_errors(_full_after_ops, matched_name)
                if _new_err_count > _orig_err_count:
                    # Surgeon introduced NEW syntax errors
                    _syntax_errors = _validate_syntax(_full_after_ops, matched_name)
                    yield sse({"type": "progress", "content": f"🔴 Compile check: {_syntax_errors[0]['message']} (line {_syntax_errors[0]['line']})"})
                    if not isinstance(_qa_result.get("risk_verdicts"), list):
                        _qa_result["risk_verdicts"] = []
                    for _serr in _syntax_errors:
                        _qa_result["risk_verdicts"].append({
                            "risk": _serr["message"],
                            "status": "blocked",
                            "reason": f"Compile error at line {_serr['line']}: {_serr['detail']}"
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
