"""Full-fidelity disconnect checkpoint recovery.

WHY THIS EXISTS  (smoking gun: session 430d9711)
-------------------------------------------------
Proven from surgical_debug_430d9711.jsonl + Railway log
(logs.1786332663778.json):

  * 201 local files, 11 edit blocks, 6 resolved. A client SSE disconnect
    fired at 03:09:28.928 — 112s into a multi-window QA correction of the
    880-line ``DashboardAIAssistant`` symbol — BEFORE the pipeline's ``done``
    chunk could flush.
  * The last checkpoint that reached the stream wrapper was
    ``resolution_checkpoint_emitted`` (03:06:45.159, payload 105304B,
    resolved_count=6). chat.py's safety net recovered those 6 edits 228ms
    after the drop (``safety_net_checkpoint_recovered`` 03:09:29.156,
    recovered_edits=6) — but saved them as PLAIN MARKDOWN.
  * TWO independent defects meant those 6 edits could never become usable:
      1. Wrong save format. The normal ``done`` path saves
         ``"__NATURAL_AND_RESULT__:" + {text, result}`` (services/pipeline.py
         result dict, routers/chat.py:~1426) which the frontend renders as
         applyable InlineDiffCards. The disconnect path saved a Markdown chat
         bubble -> inert text, never cards.
      2. Thin checkpoint payload. It carried only
         ``{filename, symbol(name), description, new_code}``. The frontend
         render gate (InlineDiffCard.tsx) drops any change with no real
         ``+/-`` ``diff`` line, and the apply schema (SurgicalChange in
         models/schemas.py) 422s without ``id`` / full ``symbol`` /
         ``original_code`` / ``diff`` / ``confidence``.

    Net effect: "201 files loaded, not one single edit was done."

THE FIX
-------
This module rebuilds resolved edits into the EXACT render/apply-ready
``changes_by_file`` structure the normal ``done`` path emits (mirroring
services/pipeline.py ~24805-24852 and models/schemas.py ``SurgicalChange`` /
``SymbolInfo``), so a disconnect-recovered save shows real, applyable diff
cards instead of dead Markdown text.

The pipeline attaches the output of :func:`build_recovery_changes_by_file` to
its checkpoint payloads (``changes_by_file`` key + ``format_version`` =
:data:`CHECKPOINT_FORMAT_VERSION`). The chat.py safety net, on a disconnect,
detects that key and saves it through the same ``__NATURAL_AND_RESULT__:``
envelope the healthy path uses.

Every function is ``_dlog``-instrumented via an injected ``dlog`` callable so
recovery is fully traceable in the session debug trace. Kept in a NEW file so
the proven-working QA / done paths are not disturbed.
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, List, Optional

# Bumped whenever the recovery payload schema changes. ``1`` (implicit) = the
# legacy thin ``resolved`` list; ``2`` = carries a render/apply-ready
# ``changes_by_file`` that the frontend can render and apply directly.
CHECKPOINT_FORMAT_VERSION = 2


def _noop_dlog(*_a: Any, **_k: Any) -> None:  # pragma: no cover - fallback
    """Fallback so the module is safe to call without an injected logger."""
    return None


def diff_has_real_body(diff: str) -> bool:
    """True if ``diff`` contains at least one genuine ``+``/``-`` body line.

    Mirrors the frontend ghost-diff filter (InlineDiffCard.tsx) and the
    server-side ``_has_real_diff`` guard: a diff whose only ``+``/``-`` lines
    are the ``+++``/``---`` headers can never render a card, so we must never
    emit one. See services/pipeline.py ``_make_diff`` for why single-line
    symbols are the classic offender.
    """
    if not diff:
        return False
    for ln in diff.split("\n"):
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        if ln.startswith("+") or ln.startswith("-"):
            return True
    return False


def symbol_to_dict(symbol: Any) -> dict:
    """Serialize a SymbolInfo (pydantic OR already-a-dict) to the JSON shape
    the frontend expects, including the computed ``full_path``.

    Defensive on purpose: at the resolution checkpoint the value is a pydantic
    ``SymbolInfo``; elsewhere it may already be a plain dict. Both must yield
    the same serialized shape as ``SymbolInfo.model_dump()``.
    """
    if isinstance(symbol, dict):
        d = dict(symbol)
    elif hasattr(symbol, "model_dump"):
        try:
            d = symbol.model_dump()
        except Exception:
            d = {}
    else:
        d = {}
    if not d:
        d = {
            "name": getattr(symbol, "name", ""),
            "symbol_type": getattr(symbol, "symbol_type", "function"),
            "start_line": getattr(symbol, "start_line", 0),
            "end_line": getattr(symbol, "end_line", 0),
            "parent": getattr(symbol, "parent", None),
            "indentation": getattr(symbol, "indentation", 0),
            "code": getattr(symbol, "code", ""),
            "signature": getattr(symbol, "signature", ""),
        }
    # ``symbol_type`` may be an Enum -> coerce to its str value for JSON.
    st = d.get("symbol_type")
    if hasattr(st, "value"):
        d["symbol_type"] = st.value
    # ``full_path`` is a pydantic computed_field; guarantee it is present so
    # the frontend card-label fallback never sees ``undefined``.
    if not d.get("full_path"):
        parent = d.get("parent")
        name = d.get("name", "")
        d["full_path"] = f"{parent}.{name}" if parent else name
    return d


def build_recovery_change(
    *,
    symbol: Any,
    new_code: str,
    description: str,
    make_diff: Callable[[str, str, str], str],
    confidence: int = 8,
    original_code: Optional[str] = None,
    precomputed_diff: Optional[str] = None,
    qa_result: Optional[dict] = None,
) -> Optional[dict]:
    """Build ONE render/apply-ready change dict mirroring
    ``SurgicalChange.model_dump()`` (models/schemas.py).

    Returns ``None`` when the change cannot produce a real ``+/-`` diff — such
    a change would be dropped by the frontend ghost-diff filter anyway, so we
    never emit a card that cannot render.
    """
    orig = original_code if original_code is not None else getattr(symbol, "code", "")
    if orig is None:
        orig = ""
    sym_name = getattr(symbol, "name", None)
    if isinstance(symbol, dict):
        sym_name = symbol.get("name")
    diff = precomputed_diff if precomputed_diff else make_diff(orig, new_code, sym_name or "change")
    if not diff_has_real_body(diff):
        return None
    sym_dict = symbol_to_dict(symbol)
    # Fields and defaults exactly mirror models/schemas.py::SurgicalChange so
    # the frontend renders it and POST /apply validates it (no 422). No extra
    # keys are added to the change itself — provenance lives on the wrapping
    # result object instead — so each change stays byte-shape-identical to a
    # normally-shipped one.
    return {
        "id": str(uuid.uuid4()),
        "symbol": sym_dict,
        "original_code": orig,
        "new_code": new_code,
        "diff": diff,
        "confidence": confidence,
        "description": description or f"Updated {sym_dict.get('name', 'symbol')}",
        "applied": False,
        "surgeon_notes": [],
        "qa_result": qa_result,
        "operations": [{"find": orig, "replace": new_code}],
        "target_element": None,
        "replacement": None,
        "insert_mode": False,
        "insert_anchor": None,
    }


def build_recovery_changes_by_file(
    resolved_entries: List[dict],
    *,
    make_diff: Callable[[str, str, str], str],
    resolve_file_id: Optional[Callable[[str], str]] = None,
    confidence: int = 8,
    session_id: str = "",
    user_id: str = "",
    dlog: Callable[..., None] = _noop_dlog,
) -> dict:
    """Group resolved edits into the EXACT ``changes_by_file`` structure the
    normal ``done`` path emits::

        {filename: {"filename": str, "file_id": str, "changes": [change, ...]}}

    ``resolved_entries`` items are the pipeline's resolution-phase dicts. Both
    shapes are accepted:
      * resolution checkpoint entry:
        ``{symbol, sf_entry, file_content, filename, edit_data:{new_code, description}}``
      * change_shell entry:
        ``{symbol, sf_entry, filename, new_code, description, diff, ...}``
    """
    changes_by_file: dict = {}
    built = 0
    skipped_no_diff = 0
    skipped_incomplete = 0
    for ent in resolved_entries:
        try:
            if not isinstance(ent, dict):
                skipped_incomplete += 1
                continue
            symbol = ent.get("symbol")
            filename = ent.get("filename") or ""
            edit_data = ent.get("edit_data") or {}
            new_code = edit_data.get("new_code", ent.get("new_code", ""))
            description = edit_data.get("description", ent.get("description", ""))
            precomputed_diff = ent.get("diff")  # change_shells already have it
            if not symbol or not filename or not new_code:
                skipped_incomplete += 1
                continue
            change = build_recovery_change(
                symbol=symbol,
                new_code=new_code,
                description=description,
                make_diff=make_diff,
                confidence=confidence,
                precomputed_diff=precomputed_diff,
            )
            if change is None:
                skipped_no_diff += 1
                continue
            if filename not in changes_by_file:
                sf_entry = ent.get("sf_entry") or {}
                fid = (sf_entry.get("id") or "") if isinstance(sf_entry, dict) else ""
                if not fid and resolve_file_id is not None:
                    try:
                        fid = resolve_file_id(filename) or ""
                    except Exception:
                        fid = ""
                changes_by_file[filename] = {
                    "filename": filename,
                    "file_id": fid,
                    "changes": [],
                }
            changes_by_file[filename]["changes"].append(change)
            built += 1
        except Exception as e:  # never let recovery-building kill the stream
            dlog(
                "checkpoint_recovery_change_error",
                session_id=session_id,
                user_id=user_id,
                filename=(ent.get("filename", "?") if isinstance(ent, dict) else "?"),
                error=str(e)[:200],
            )
    dlog(
        "checkpoint_recovery_built",
        session_id=session_id,
        user_id=user_id,
        files=len(changes_by_file),
        changes=built,
        skipped_no_diff=skipped_no_diff,
        skipped_incomplete=skipped_incomplete,
    )
    return changes_by_file


def enrich_checkpoint_payload(
    payload: dict,
    resolved_entries: List[dict],
    *,
    make_diff: Callable[[str, str, str], str],
    resolve_file_id: Optional[Callable[[str], str]] = None,
    confidence: int = 8,
    session_id: str = "",
    user_id: str = "",
    dlog: Callable[..., None] = _noop_dlog,
) -> dict:
    """Attach a render/apply-ready ``changes_by_file`` + ``format_version`` to
    an existing (thin) checkpoint ``payload``, IN PLACE.

    This is the single call the pipeline makes at each of its checkpoint emit
    sites (resolution + QA round-start + QA round-end). The thin ``resolved``
    list already on the payload is preserved untouched for backward compat
    with older clients / older saved sessions; new clients read
    ``changes_by_file``. Never raises — a failure here must not break the
    stream, it just leaves the payload thin (pre-fix behavior).
    """
    try:
        cbf = build_recovery_changes_by_file(
            resolved_entries,
            make_diff=make_diff,
            resolve_file_id=resolve_file_id,
            confidence=confidence,
            session_id=session_id,
            user_id=user_id,
            dlog=dlog,
        )
        if cbf:
            payload["changes_by_file"] = cbf
            payload["format_version"] = CHECKPOINT_FORMAT_VERSION
    except Exception as e:
        dlog(
            "checkpoint_recovery_enrich_error",
            session_id=session_id,
            user_id=user_id,
            error=str(e)[:200],
        )
    return payload


def build_recovery_result(
    changes_by_file: dict,
    *,
    natural_text: str = "",
) -> dict:
    """Wrap ``changes_by_file`` in the SmartResult shape the frontend parser
    expects (mirrors services/pipeline.py result dict ~25046). ``recovered``
    marks provenance for the UI without touching the individual changes.
    """
    n = sum(len(v.get("changes", [])) for v in changes_by_file.values())
    return {
        "intent": "edit",
        "summary": (
            f"Recovered {n} edit(s) from an interrupted run" if n else "Interrupted run"
        ),
        "reasoning": "Recovered from a connection interruption before the run finished.",
        "risks": [],
        "skipped_changes": [],
        "changes_by_file": changes_by_file,
        "new_files": [],
        "natural_text": natural_text,
        "recovered": True,
    }
