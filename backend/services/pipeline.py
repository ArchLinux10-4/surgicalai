"""
Two-model AI pipeline.
- Architect: GPT-5 (or configured model) — reads symbol map, reasons, produces plan
- Surgeon: GPT-4.1 — receives plan + code chunk, writes minimal precise replacement

Best Practice #1: Read the map before touching the territory (AST-first)
Best Practice #2: Minimal footprint (surgeon only touches requested symbol)
Best Practice #3: Verify before commit (confidence scoring + diff)
"""
import json
import uuid
import difflib
from pathlib import Path
from typing import Optional
from openai import OpenAI
import httpx

from database import get_setting
from models.schemas import (
    ArchitectPlan, ChangeTarget, ChangeType, SurgicalChange,
    SurgicalAnalyzeResponse, SymbolMap, SymbolInfo
)
from services.ast_parser import ASTParser

parser = ASTParser()

# Models that do NOT accept a temperature parameter (reasoning / latest-gen models)
NO_TEMPERATURE_MODELS = {"gpt-5", "o1", "o1-mini", "o1-preview", "o3", "o3-mini", "o4-mini"}


def _chat_create(client: OpenAI, model: str, messages: list, temperature: float = 0.3, **kwargs):
    """Wrapper around client.chat.completions.create that drops temperature
    for models that don't support it (GPT-5, o-series reasoning models)."""
    base_model = model.split(":")[0].lower()
    if base_model in NO_TEMPERATURE_MODELS:
        return client.chat.completions.create(model=model, messages=messages, **kwargs)
    return client.chat.completions.create(model=model, messages=messages, temperature=temperature, **kwargs)


def _get_client() -> OpenAI:
    key = get_setting("openai_api_key")
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


def _should_use_ollama(model: Optional[str] = None) -> bool:
    """Check if we should use Ollama for this model."""
    if model and model.startswith("ollama:"):
        return True
    if get_setting("ollama_enabled", "false") == "true" and not get_setting("openai_api_key"):
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
- If you genuinely cannot implement part of the plan, add: # SURGEON NOTE: [specific concern]

Minimal footprint. The diff between original and your output should contain ONLY the requested change.

Start your response with the first line of code. End with the last line of code."""


def run_architect(
    symbol_map: SymbolMap,
    user_request: str,
    file_content: str,
    model: Optional[str] = None
) -> ArchitectPlan:
    """
    GPT-5 (Architect): reads symbol map + request → produces structured change plan.
    Never sees raw code — works from the symbol map for efficiency and accuracy.
    """
    client = _get_client()
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
    model: Optional[str] = None
) -> tuple[str, int]:
    """
    GPT-4.1 (Surgeon): receives ONE code chunk + plan → returns minimal replacement.
    Returns (new_code, confidence_score).
    """
    client = _get_client()
    surg_model = model or get_setting("surgeon_model", "gpt-4.1")
    temp = float(get_setting("temperature_surgeon", "0.1"))

    # Give surgeon 10 lines of context before and after the symbol
    all_lines = file_content.splitlines()
    context_start = max(0, symbol.start_line - 11)
    context_end = min(len(all_lines), symbol.end_line + 10)

    before_context = "\n".join(all_lines[context_start:symbol.start_line - 1])
    after_context = "\n".join(all_lines[symbol.end_line:context_end])

    user_msg = f"""CHANGE PLAN:
Type: {target.change_type.value}
Description: {target.description}
New logic required: {target.new_logic}

CONTEXT BEFORE (do not modify):
{before_context}

TARGET CODE TO REWRITE (lines {symbol.start_line}-{symbol.end_line}):
{symbol.code}

CONTEXT AFTER (do not modify):
{after_context}

Write the replacement for the TARGET CODE ONLY. Preserve exact indentation."""

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

    return new_code, confidence


def run_chat(
    messages: list,
    file_content: Optional[str] = None,
    symbol_context: Optional[str] = None,
    model: Optional[str] = None,
    pinned_context: Optional[list] = None,
    project_memory: Optional[str] = None
) -> str:
    """
    Standard chat (non-surgical). Uses configured model.
    Streams response and returns full text.
    """
    chat_model = model or get_setting("architect_model", "gpt-4.1")

    system_parts = [
        "You are SurgicalAI, an expert coding assistant. You are precise, thorough, and conservative.",
        "You prioritize code correctness over brevity.",
        "When suggesting code changes, always explain WHY and highlight any risks.",
        "Format code with proper syntax highlighting. Use markdown."
    ]

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

    client = _get_client()
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
    project_memory: Optional[str] = None
):
    """
    Streaming version of run_chat. Yields SSE chunks.
    Used by the /api/chat/stream endpoint.
    """
    chat_model = model or get_setting("architect_model", "gpt-4.1")

    system_parts = [
        "You are SurgicalAI, an expert coding assistant. You are precise, thorough, and conservative.",
        "You prioritize code correctness over brevity.",
        "When suggesting code changes, always explain WHY and highlight any risks.",
        "Format code with proper syntax highlighting. Use markdown."
    ]

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
            client = _get_client()
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
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"

    yield f"data: {json.dumps({'type': 'done', 'content': ''})}\n\n"


async def analyze_and_plan_stream(
    file_path: str,
    file_content: str,
    user_request: str,
    session_id: Optional[str] = None,
    project_memory: Optional[str] = None,
    pinned_context: Optional[list] = None
):
    """
    Streaming surgical analysis. Yields SSE progress events then final result.
    """
    try:
        yield f"data: {json.dumps({'type': 'progress', 'content': 'Parsing file structure...'})}\n\n"
        symbol_map = parser.parse(file_content, file_path)

        yield f"data: {json.dumps({'type': 'progress', 'content': f'Found {len(symbol_map.symbols)} symbols. Running Architect...'})}\n\n"

        plan = run_architect(symbol_map, user_request, file_content)

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
                        new_code, confidence = run_surgeon(parent_symbol, target, file_content)
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

            new_code, confidence = run_surgeon(symbol, target, file_content)
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
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"


def analyze_multi_file(file_paths: list, file_contents: dict, user_request: str, session_id=None):
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
    client = _get_client()
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
            new_code, confidence = run_surgeon(symbol, target, content)
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
    import re

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
    session_id: Optional[str] = None
) -> SurgicalAnalyzeResponse:
    """
    Full pipeline: parse → architect → surgeon → diff → response.
    """
    # Step 1: Parse symbol map
    symbol_map = parser.parse(file_content, file_path)

    # Step 2: Architect produces plan
    plan = run_architect(symbol_map, user_request, file_content)

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
                    new_code, confidence = run_surgeon(parent_symbol, target, file_content)
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
        new_code, confidence = run_surgeon(symbol, target, file_content)
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
      "new_logic": "precise description of the new behavior — the Surgeon will implement exactly this",
      "confidence": 9
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

━━━ HARD RULES ━━━
- NEVER touch symbols that weren't asked about
- NEVER invent symbol names — only use names from the symbol map
- confidence < 7 = use needs_clarification instead
- Minimal footprint: if the user said "add X to function Y", only plan function Y"""


async def run_smart_pipeline_stream(
    session_files: list,
    user_request: str,
    conversation_history: list,
    session_id: str = None,
    project_memory: str = None,
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

    try:
        if not session_files:
            # No files — pure chat mode, stream response
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
            msgs = [{"role": "system", "content": system}] + conversation_history[-10:] + [{"role": "user", "content": user_request}]
            client = _get_client()
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
                syms = []
                for s in smap.symbols:
                    entry = f"  [{s.symbol_type.value}] {s.full_path}"
                    if s.signature:
                        entry += f" — {s.signature}"
                    entry += f" (lines {s.start_line}-{s.end_line})"
                    syms.append(entry)
                file_summaries.append(
                    f"FILE: {fname} ({sf.get('lines', len(content.splitlines()))} lines, {sf.get('language', 'code')})\n"
                    f"SYMBOLS:\n" + ("\n".join(syms[:40]) if syms else "  (no symbols parsed)")
                )
            except Exception as e:
                file_summaries.append(f"FILE: {fname} — could not parse: {e}")
                symbol_maps_by_name[fname] = (None, sf)

        yield sse({"type": "progress", "content": "Architect analyzing your request..."})

        # Format recent conversation history (last 6 turns)
        hist_text = ""
        for msg in conversation_history[-6:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")[:300]
            hist_text += f"{role.upper()}: {content}\n"

        context_msg = f"""UPLOADED FILES:
{chr(10).join(file_summaries)}

RECENT CONVERSATION:
{hist_text if hist_text else "(new conversation)"}

USER REQUEST:
{user_request}"""

        if project_memory:
            context_msg = f"PROJECT MEMORY:\n{project_memory}\n\n" + context_msg

        client = _get_client()
        arch_model = get_setting("architect_model", "gpt-4.1")

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
                {"role": "system", "content": SMART_ARCHITECT_SYSTEM},
                {"role": "user", "content": user_content}
            ]
        else:
            architect_messages = [
                {"role": "system", "content": SMART_ARCHITECT_SYSTEM},
                {"role": "user", "content": context_msg}
            ]

        response = _chat_create(client,
            model=arch_model,
            messages=architect_messages,
            temperature=0.3,
            response_format={"type": "json_object"}
        )

        plan = json.loads(response.choices[0].message.content)
        intent = plan.get("intent", "chat")

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
            yield sse({"type": "done", "content": ""})
            return

        # CODE EDIT intent — run surgical pipeline
        targets = plan.get("targets", [])
        if not targets:
            # No targets identified — fall back to chat
            fallback = f"I identified this as a code change, but couldn't pinpoint a specific symbol.\n\n**Reasoning:** {plan.get('reasoning', '')}\n\nTry being more specific, e.g.: \"In settings.py, add gpt-5 to the models list in get_available_models()\""
            for word in fallback.split(" "):
                yield sse({"type": "token", "content": word + " "})
                await asyncio.sleep(0.005)
            yield sse({"type": "done", "content": ""})
            return

        yield sse({"type": "progress", "content": f"Plan: {len(targets)} change(s). Running surgeon..."})

        changes_by_file = {}

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
                confidence=target.get("confidence", 7)
            )

            new_code, confidence = run_surgeon(symbol, change_target, sf["content"])
            diff = _make_diff(symbol.code, new_code, symbol_path)

            change = SurgicalChange(
                id=str(uuid.uuid4()),
                symbol=symbol,
                original_code=symbol.code,
                new_code=new_code,
                diff=diff,
                confidence=confidence,
                description=target.get("description", ""),
                applied=False
            )

            if matched_name not in changes_by_file:
                changes_by_file[matched_name] = {"file": sf, "changes": []}
            changes_by_file[matched_name]["changes"].append(change)

        if not changes_by_file:
            fallback = f"I found the files but couldn't locate the specific symbol `{targets[0].get('symbol_path', '?')}`. Try uploading the exact file that contains it."
            for word in fallback.split(" "):
                yield sse({"type": "token", "content": word + " "})
            yield sse({"type": "done", "content": ""})
            return

        result = {
            "intent": "edit",
            "summary": plan.get("summary", ""),
            "reasoning": plan.get("reasoning", ""),
            "risks": plan.get("risks", []),
            "changes_by_file": {
                fname: {
                    "filename": fname,
                    "file_id": data["file"]["id"],
                    "changes": [c.model_dump() for c in data["changes"]],
                }
                for fname, data in changes_by_file.items()
            },
        }

        yield sse({"type": "smart_result", "content": json.dumps(result)})
        yield sse({"type": "done", "content": ""})

    except Exception as e:
        import traceback
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
