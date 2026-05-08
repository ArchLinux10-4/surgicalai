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
    ArchitectPlan, ChangeTarget, ChangeType, SurgicalChange,
    SurgicalAnalyzeResponse, SymbolMap, SymbolInfo
)
from services.ast_parser import ASTParser

try:
    from anthropic import AsyncAnthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

parser = ASTParser()

# Models that do NOT accept a temperature parameter (reasoning / latest-gen models)
NO_TEMPERATURE_MODELS = {"gpt-5", "o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini"}

# ── Prompt engineering constants ──────────────────────────────────────────────
HISTORY_WINDOW       = 10   # turns of conversation history passed to every prompt
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


# Models confirmed to support extended thinking (budget_tokens).
# claude-opus-4-7 and others without native thinking support must be excluded.
_THINKING_CAPABLE_PATTERNS = ("claude-sonnet-4", "claude-3-7", "claude-3-5")

def _supports_thinking(model: str) -> bool:
    """Return True only for Claude models that support extended thinking."""
    if not _is_claude_model(model):
        return False
    return any(model.startswith(p) or p in model for p in _THINKING_CAPABLE_PATTERNS)


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

The ARCHITECT (a separate model) has already:
1. Read the symbol map — never the raw file
2. Identified exactly which symbol needs to change
3. Written a precise plan describing the new behavior

Your ONLY job: implement that plan on the specific code block you receive. Nothing more.

━━━ HARD RULES ━━━
- Return ONLY the replacement code — no explanation, no markdown fences, no preamble
- Implement EXACTLY what the Architect's plan says. No improvisation.
- Preserve EXACT indentation of the original
- Preserve ALL functionality NOT mentioned in the plan
- Do NOT change function signatures unless the plan explicitly says to
- Do NOT add features, error handling, logging, or comments that weren't asked for
- Do NOT remove existing error handling unless explicitly requested
- If you genuinely cannot implement part of the plan, add a comment: # SURGEON NOTE: [specific concern]

━━━ IMPORT HANDLING ━━━
- If the new code requires an import that isn't already present, add a comment on the FIRST LINE of your output:
  # IMPORT_NEEDED: import uuid  (or whatever the import is)
- Use one # IMPORT_NEEDED: comment per missing import
- The system will extract these and add them to the file top automatically
- Do NOT add the import inline inside a function or class body

━━━ ALREADY CORRECT RULE ━━━
- If the TARGET CODE already implements what the plan describes — return it UNCHANGED.
- Do NOT invent changes. The diff between original and your output should be empty if the code already matches.
- Signal this by returning the original code exactly as-is (no comments added).

━━━ CRITICAL: NO DUPLICATION ━━━
- The CONTEXT BEFORE and CONTEXT AFTER blocks are shown for your reference ONLY — do NOT include them in your output
- Do NOT repeat function declarations, type annotations, closing braces, or ANY line that appears in CONTEXT BEFORE
- If the target starts with a function/class declaration, output that declaration ONCE and only once

Minimal footprint. The diff between original and your output should contain ONLY the requested change.

Start your response with the first line of code. End with the last line of code."""


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
    client = _get_client(user_id)
    arch_model = model or get_setting("architect_model", "gpt-4.1")
    temp = float(get_setting("temperature_architect", "0.3"))

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
            confidence=t.get("confidence", 7)
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
                            confidence=target.confidence
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
                            confidence=target.confidence
                        )

    return ArchitectPlan(
        summary=data.get("summary", ""),
        targets=validated_targets,
        new_symbols_needed=data.get("new_symbols_needed", []),
        import_changes=data.get("import_changes", []),
        risks=data.get("risks", [])
    )


def run_surgeon(
    symbol: SymbolInfo,
    target: ChangeTarget,
    file_content: str,
    model: Optional[str] = None,
    user_id: str = ""
) -> tuple[str, int]:
    """
    GPT-4.1 (Surgeon): receives ONE code chunk + plan → returns minimal replacement.
    Returns (new_code, confidence_score).
    """
    client = _get_client(user_id)
    surg_model = model or get_setting("surgeon_model", "gpt-4.1")
    temp = float(get_setting("temperature_surgeon", "0.1"))

    # Give surgeon 10 lines of context before and after the symbol
    all_lines = file_content.splitlines()
    context_start = max(0, symbol.start_line - 11)
    context_end = min(len(all_lines), symbol.end_line + 10)

    before_context = "\n".join(all_lines[context_start:symbol.start_line - 1])
    after_context = "\n".join(all_lines[symbol.end_line:context_end])

    # Thread import_changes from Architect plan if present
    _import_hint = ""
    if hasattr(target, "import_changes") and target.import_changes:
        _import_hint = "\nREQUIRED IMPORT CHANGES (add # IMPORT_NEEDED: comments for any that need adding):\n" + "\n".join(f"  {ic}" for ic in target.import_changes)

    user_msg = f"""CHANGE PLAN:
Type: {target.change_type.value}
Description: {target.description}
New logic required: {target.new_logic}{_import_hint}

CONTEXT BEFORE (do not modify, reference only):
{before_context}

TARGET CODE TO REWRITE (lines {symbol.start_line}-{symbol.end_line}):
{symbol.code}

CONTEXT AFTER (do not modify, reference only):
{after_context}

Write the replacement for the TARGET CODE ONLY. Preserve exact indentation.
If any imports are needed, add # IMPORT_NEEDED: <import statement> as the first line(s) of your output."""

    response = _chat_create(client, 
        model=surg_model,
        messages=[
            {"role": "system", "content": SURGEON_SYSTEM},
            {"role": "user", "content": user_msg}
        ],
        temperature=temp
    )

    raw = response.choices[0].message.content

    # Remove accidental markdown fences if surgeon wrapped it
    if raw.lstrip().startswith("```"):
        fence_lines = raw.split("\n")
        # Find first ``` and strip it; strip trailing ``` too
        start = next((i for i, l in enumerate(fence_lines) if l.strip().startswith("```")), 0) + 1
        end = len(fence_lines) - 1 if fence_lines[-1].strip() == "```" else len(fence_lines)
        raw = "\n".join(fence_lines[start:end])

    # Extract # IMPORT_NEEDED: lines before stripping
    import_needed_lines = []
    remaining_lines = []
    for _ln in raw.splitlines():
        if _ln.strip().startswith("# IMPORT_NEEDED:"):
            import_needed_lines.append(_ln.strip()[len("# IMPORT_NEEDED:"):].strip())
        else:
            remaining_lines.append(_ln)
    if import_needed_lines:
        raw = "\n".join(remaining_lines)

    # Extract # SURGEON NOTE: comments (surface to caller)
    surgeon_notes = []
    clean_lines = []
    for _ln in raw.splitlines():
        if "# SURGEON NOTE:" in _ln:
            surgeon_notes.append(_ln.strip())
        else:
            clean_lines.append(_ln)
    if surgeon_notes:
        raw = "\n".join(clean_lines)

    # Strip trailing whitespace only — preserve leading indentation
    new_code = raw.rstrip()
    # Remove any leading blank lines only (surgeon sometimes adds a blank line before code)
    while new_code.startswith("\n"):
        new_code = new_code[1:]

    # Re-apply indentation if surgeon dropped leading spaces (common with some models)
    if symbol.indentation > 0 and new_code and not new_code[0].isspace():
        indent_str = " " * symbol.indentation
        new_code = "\n".join(
            indent_str + line if line.strip() else line
            for line in new_code.splitlines()
        )

    # Confidence: use target confidence but reduce if code is suspiciously short/long
    confidence = target.confidence
    orig_lines = len(symbol.code.splitlines())
    new_lines = len(new_code.splitlines())
    if new_lines < orig_lines * 0.3:  # suspicious shrinkage
        confidence = min(confidence, 5)
    if new_lines > orig_lines * 5:  # suspicious bloat
        confidence = min(confidence, 6)

    return new_code, confidence, surgeon_notes, import_needed_lines


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
        if _should_use_ollama(chat_model):
            # Ollama streaming
            base_url = get_setting("ollama_base_url", "http://localhost:11434")
            ollama_model = chat_model.replace("ollama:", "") if chat_model.startswith("ollama:") else get_setting("ollama_model", "qwen2.5-coder:7b")
            with httpx.stream("POST", f"{base_url}/api/chat", json={"model": ollama_model, "messages": all_messages, "stream": True}, timeout=120) as resp:
                for line in resp.iter_lines():
                    if line:
                        try:
                            data = json.loads(line)
                            token = data.get("message", {}).get("content", "")
                            if token:
                                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                        except Exception:
                            pass
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
                        new_code, confidence, _surg_notes, _needed_imports = run_surgeon(parent_symbol, target, file_content, user_id=user_id)
                        diff = _make_diff(parent_symbol.code, new_code, f"{target.symbol_path} (added to {parent_path})")
                        changes.append(SurgicalChange(
                            id=str(uuid.uuid4()),
                            symbol=parent_symbol,
                            original_code=parent_symbol.code,
                            new_code=new_code,
                            diff=diff,
                            confidence=confidence,
                            description=target.description,
                            applied=False
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

            new_code, confidence, _surg_notes, _needed_imports = run_surgeon(symbol, target, file_content, user_id=user_id)
            diff = _make_diff(symbol.code, new_code, target.symbol_path)

            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code,
                diff=diff,
                confidence=confidence,
                description=target.description,
                applied=False
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
            confidence=t.get("confidence", 7)
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
            new_code, confidence, _surg_notes, _needed_imports = run_surgeon(symbol, target, content, user_id=user_id)
            diff = _make_diff(symbol.code, new_code, target.symbol_path)
            changes.append(SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code,
                diff=diff,
                confidence=confidence,
                description=target.description,
                applied=False
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
                    new_code, confidence, _surg_notes, _needed_imports = run_surgeon(parent_symbol, target, file_content, user_id=user_id)
                    diff = _make_diff(parent_symbol.code, new_code, f"{target.symbol_path} (added to {parent_path})")
                    change = SurgicalChange(
                        id=str(uuid.uuid4()),
                        symbol=parent_symbol,
                        original_code=parent_symbol.code,
                        new_code=new_code,
                        diff=diff,
                        confidence=confidence,
                        description=target.description,
                        applied=False
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
        new_code, confidence, _surg_notes, _needed_imports = run_surgeon(symbol, target, file_content, user_id=user_id)
        diff = _make_diff(symbol.code, new_code, target.symbol_path)

        change = SurgicalChange(
            id=str(uuid.uuid4()),
            symbol=symbol,
            original_code=symbol.code,
            new_code=new_code,
            diff=diff,
            confidence=confidence,
            description=target.description,
            applied=False
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
      "change_type": "modify|add|delete",
      "description": "what changes in this symbol",
      "new_logic": "precise description of the new behavior — the Surgeon will implement exactly this. QUALITY RULE: Reference ACTUAL variable/function names from the code. Be specific: 'After const fee = calcFee(), add: if (fee < 0) throw new Error()'. Never say just 'add error handling' — always say WHAT, WHERE, and HOW.",
      "import_changes": ["add: import uuid", "remove: from datetime import date"],
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

Respond with ONLY valid JSON — no explanation outside the JSON:
{
  "verdict": "safe" | "warning" | "blocked",
  "qa_score": <integer 1-10>,
  "summary": "<one sentence>",
  "import_issues": ["<issue>"],
  "downstream_risks": ["<risk>"],
  "type_errors": ["<error>"],
  "plan_deviation": "<empty string if none, or description of deviation>"
}

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
) -> dict:
    """
    QA agent: verifies Surgeon output before showing diff card to user.
    Uses gpt-4.1-mini for speed/cost efficiency.
    Returns a dict matching QAResult schema.
    Guaranteed to return a result — never raises (skipped verdict on error).
    """
    import asyncio

    qa_model = "gpt-4.1-mini"

    # Trim inputs to avoid huge context
    orig_trimmed = original_code[:3000] + ("\n... [truncated]" if len(original_code) > 3000 else "")
    new_trimmed  = new_code[:3000]      + ("\n... [truncated]" if len(new_code) > 3000 else "")
    other_ctx    = other_files_context[:2000] + ("\n... [truncated]" if len(other_files_context) > 2000 else "")

    user_msg = f"""CHANGE PLAN:
Symbol: {symbol_path}
File: {filename}
Description: {change_description}
Expected behavior: {new_logic}

ORIGINAL CODE:
{orig_trimmed}

NEW CODE (Surgeon output):
{new_trimmed}

OTHER FILES IN SESSION (for cross-file checking):
{other_ctx if other_ctx.strip() else "(no other files uploaded)"}

Run all 6 checks and return the JSON verdict."""

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
            msgs = [{"role": "system", "content": system}] + conversation_history[-HISTORY_WINDOW:] + [{"role": "user", "content": user_request}]

            if _is_claude_model(chat_model):
                # ── Claude streaming with extended thinking ──
                aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))
                claude_msgs = conversation_history[-HISTORY_WINDOW:] + [{"role": "user", "content": user_request}]
                async with aclient.messages.stream(
                    model=chat_model,
                    max_tokens=16000,
                    **({"thinking": {"type": "enabled", "budget_tokens": 10000}} if _supports_thinking(chat_model) else {}),
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
                file_summaries.append(
                    f"FILE: {fname} ({sf.get('lines', len(content.splitlines()))} lines, {sf.get('language', 'code')})\n"
                    f"{_sym_header}\n" + ("\n".join(syms) + _sym_suffix if syms else "  (no symbols parsed)")
                )
            except Exception as e:
                file_summaries.append(f"FILE: {fname} — could not parse: {e}")
                symbol_maps_by_name[fname] = (None, sf)

        compliance.mark("symbol_map_read", ran=True,
                        output_summary=f"{len([f for f in file_summaries if 'SYMBOLS:' in f])} files parsed with symbol maps")
        yield sse({"type": "progress", "content": "Architect analyzing your request..."})

        # Format recent conversation history (last 6 turns)
        hist_text = ""
        for msg in conversation_history[-HISTORY_WINDOW:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")[:1200]  # raised from 300 — preserves clarification Q&A
            hist_text += f"{role.upper()}: {content}\n"

        context_msg = f"""UPLOADED FILES:
{chr(10).join(file_summaries)}

RECENT CONVERSATION:
{hist_text if hist_text else "(new conversation)"}

USER REQUEST:
{user_request}"""

        if project_memory:
            context_msg = f"PROJECT MEMORY:\n{project_memory}\n\n" + context_msg

        arch_model = get_setting("architect_model", "gpt-4.1")

        # Detect if this is a diagnostic request — inject extra guidance if so
        _req_lower = user_request.lower()
        _is_diagnostic = any(kw in _req_lower for kw in _DIAGNOSIS_KEYWORDS)
        _architect_system = _build_architect_system(is_diagnostic=_is_diagnostic)

        if _is_claude_model(arch_model):
            # ── Claude Architect with streaming extended thinking ──
            aclient = AsyncAnthropic(api_key=_get_anthropic_key(user_id))

            # Build user content for Claude (images use different format)
            if image_files:
                user_content = [{"type": "text", "text": context_msg}]
                for img_sf in image_files:
                    img_data = img_sf["content"]
                    if img_data.startswith("data:"):
                        parts = img_data.split(",", 1)
                        media_type = parts[0].split(":")[1].split(";")[0]
                        b64_data = parts[1]
                    else:
                        ext = Path(img_sf["filename"]).suffix.lower().lstrip(".")
                        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                                    "webp": "image/webp", "gif": "image/gif"}
                        media_type = mime_map.get(ext, "image/png")
                        b64_data = img_data
                    user_content.append({
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": b64_data}
                    })
            else:
                user_content = context_msg

            thinking_chunks = []
            response_text_chunks = []
            claude_failed = False

            # ── Retry loop: up to 2 attempts for transient 500/529 errors ──
            _claude_attempt = 0
            while _claude_attempt < 2:
                _claude_attempt += 1
                thinking_chunks = []
                response_text_chunks = []
                try:
                    async with aclient.messages.stream(
                        model=arch_model,
                        max_tokens=16000,
                        **({"thinking": {"type": "enabled", "budget_tokens": 10000}} if _supports_thinking(arch_model) else {}),
                        system=_architect_system,
                        messages=[{"role": "user", "content": user_content}],
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
                                    thinking_chunks.append(event.delta.thinking)
                                elif hasattr(event.delta, 'text'):
                                    response_text_chunks.append(event.delta.text)
                            elif event.type == "content_block_stop":
                                if current_block == "thinking":
                                    yield sse({"type": "thinking_end", "content": ""})
                    break  # ← success: exit retry loop
                except Exception as claude_err:
                    err_str = str(claude_err)
                    err_low = err_str.lower()
                    _is_transient = "500" in err_str or "529" in err_str or "overloaded" in err_low or "internal_server_error" in err_low
                    if _is_transient and _claude_attempt < 2:
                        yield sse({"type": "progress", "content": "⚡ AI service hiccup — retrying once..."})
                        await asyncio.sleep(2)
                        continue  # retry
                    if image_files and ("image" in err_low or "unsupported" in err_low):
                        yield sse({"type": "progress", "content": "⚠️ Images failed — retrying text-only..."})
                        thinking_chunks = []
                        response_text_chunks = []
                        try:
                            async with aclient.messages.stream(
                                model=arch_model,
                                max_tokens=16000,
                                **({"thinking": {"type": "enabled", "budget_tokens": 10000}} if _supports_thinking(arch_model) else {}),
                                system=_architect_system,
                                messages=[{"role": "user", "content": context_msg}],
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
                                            thinking_chunks.append(event.delta.thinking)
                                        elif hasattr(event.delta, 'text'):
                                            response_text_chunks.append(event.delta.text)
                                    elif event.type == "content_block_stop":
                                        if current_block == "thinking":
                                            yield sse({"type": "thinking_end", "content": ""})
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

            # Robust JSON parsing — Claude may not always produce perfect JSON
            try:
                plan = json.loads(raw_text)
            except json.JSONDecodeError:
                # Try to extract JSON object from the text
                # Manual JSON extraction (avoids 're' module scoping issues)
                _brace_start = raw_text.find('{')
                _brace_end = raw_text.rfind('}')
                json_match = raw_text[_brace_start:_brace_end + 1] if _brace_start >= 0 and _brace_end > _brace_start else None
                if json_match:
                    try:
                        plan = json.loads(json_match)
                    except json.JSONDecodeError:
                        # Last resort: treat entire response as chat
                        plan = {"intent": "chat", "chat_response": raw_text}
                else:
                    # No JSON found at all — Claude gave a plain text answer
                    plan = {"intent": "chat", "chat_response": raw_text}
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
                        # Find the text line within the file
                        _tline_inner = None
                        for li, line in enumerate(_file_lines, 1):
                            if qt.lower() in line.lower():
                                _tline_inner = li
                                break
                        if _tline_inner:
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
                # Find where the text actually is
                _tline = None
                for li, line in enumerate(_file_lines, 1):
                    if qt.lower() in line.lower():
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
                    targets[qi]["symbol_path"] = _best.full_path
                elif not _best:
                    # Create virtual window
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

            new_code, confidence, _surg_notes, _needed_imports = run_surgeon(symbol, change_target, sf["content"], user_id=user_id)
            diff = _make_diff(symbol.code, new_code, symbol_path)

            # ── QA Agent: verify Surgeon output before showing to user ──
            _other_ctx_for_qa = "\n\n".join(
                p for p in _qa_other_context_parts
                if not p.startswith(f"FILE: {matched_name}")
            )
            _qa_result = await run_qa_agent(
                original_code=symbol.code,
                new_code=new_code,
                change_description=change_target.description,
                new_logic=change_target.new_logic,
                symbol_path=symbol_path,
                filename=matched_name,
                other_files_context=_other_ctx_for_qa,
                session_id=session_id or "",
                user_id=user_id,
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

            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code,
                diff=diff,
                confidence=confidence,
                description=target.get("description", ""),
                applied=False,
                surgeon_notes=_surg_notes if _surg_notes else [],
                qa_result=_qa_result,
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
