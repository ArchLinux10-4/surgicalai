"""Claude (Anthropic) native web-search tool adapter — Ask/Plan research mode.

New file, additive only. Nothing here is imported by any existing working
code path unless that path explicitly opts in (see `run_chat_stream`'s
Claude branch in pipeline.py, gated on the `web_search_enabled` setting AND
`mode in ("ask", "plan")`). The already-working Claude Edit/Agent pipeline,
and the already-working GPT QA fallback, never import this file.

Why this is safe to bolt on:
  Anthropic's web_search tool (`web_search_20250305`) is a *server-executed*
  tool — Claude decides when to search, Anthropic runs the search, and the
  results + citations are streamed back as ordinary `server_tool_use` /
  `web_search_tool_result` content blocks in the *same* `messages.stream()`
  response the Claude branch already consumes. No new endpoint, no new
  request shape, no local search backend to build or maintain — see
  https://docs.claude.com/en/docs/agents-and-tools/tool-use/web-search-tool
  (verified 2026-08 against the live docs, not guessed).

This module only:
  1. Builds the tool definition to add to `tools=[...]`.
  2. Translates the three new streaming event shapes Claude emits when the
     tool fires into small SSE-ready dicts the frontend can render as a
     live "Searching the web…" trail + a de-duplicated Sources list.

It does not touch tokenization/thinking-block handling — pipeline.py's
existing Claude loop keeps doing that exactly as before.
"""
import json
from typing import Any, Dict, List, Optional

from database import _dlog


# Anthropic's documented tool versions (2026-08 docs):
#   web_search_20250305 — basic web search (used here: no dynamic filtering,
#     no extra `allowed_callers` requirement, the most broadly supported and
#     lowest-complexity version — deliberate choice for a first, robust cut).
#   web_search_20260209 / web_search_20260318 — add dynamic filtering /
#     response-inclusion controls we do not need yet.
CLAUDE_WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
CLAUDE_WEB_SEARCH_TOOL_NAME = "web_search"
DEFAULT_MAX_USES = 5


def build_web_search_tool(max_uses: int = DEFAULT_MAX_USES) -> Dict[str, Any]:
    """Returns the Anthropic tool definition to append to `tools=[...]`.

    Anthropic executes this tool server-side — Claude decides when to
    search, runs it, and streams results back in the same response. No
    client-side search implementation needed.
    """
    _dlog("claude_web_search_tool_built", max_uses=max_uses)
    return {
        "type": CLAUDE_WEB_SEARCH_TOOL_TYPE,
        "name": CLAUDE_WEB_SEARCH_TOOL_NAME,
        "max_uses": max_uses,
    }


def _domain_of(url: str) -> str:
    """Best-effort bare domain for a favicon/display hint. Never raises."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


class ClaudeWebSearchStreamTracker:
    """Feed each Anthropic streaming `event` in; get back zero or more
    SSE-ready dicts for the frontend and an end-of-turn de-duplicated
    sources list for persistence.

    Usage (mirrors the existing Claude branch's per-event loop exactly —
    see pipeline.py `run_chat_stream`):

        tracker = ClaudeWebSearchStreamTracker(session_id=..., user_id=...)
        async for event in cstream:
            for sse_event in tracker.on_event(event):
                yield sse(sse_event)
        sources = tracker.get_sources()   # for the final `done` payload
    """

    def __init__(self, session_id: Optional[str] = None, user_id: str = "") -> None:
        self._session_id = session_id
        self._user_id = user_id
        # index -> accumulated partial_json string for an in-flight
        # server_tool_use(web_search) input.
        self._pending_query_json: Dict[int, str] = {}
        # index -> True once we've confirmed this block is our search tool.
        self._search_block_indexes: Dict[int, bool] = {}
        # De-duplicated by URL, insertion-ordered.
        self._sources: List[Dict[str, Any]] = []
        self._seen_urls: set = set()

    def get_sources(self) -> List[Dict[str, Any]]:
        return list(self._sources)

    def on_event(self, event: Any) -> List[Dict[str, Any]]:
        """Returns a list (possibly empty) of SSE-ready dicts for this event.
        Never raises — a parse failure here must never break the chat
        stream for the already-working text/thinking path.
        """
        try:
            return self._on_event(event)
        except Exception as e:
            _dlog("claude_web_search_event_parse_error",
                  session_id=self._session_id, user_id=self._user_id,
                  error_type=type(e).__name__, error=str(e)[:200])
            return []

    def _on_event(self, event: Any) -> List[Dict[str, Any]]:
        event_type = getattr(event, "type", None)
        out: List[Dict[str, Any]] = []

        if event_type == "content_block_start":
            block = getattr(event, "content_block", None)
            index = getattr(event, "index", None)
            block_type = getattr(block, "type", "") if block else ""

            if block_type == "server_tool_use" and getattr(block, "name", "") == CLAUDE_WEB_SEARCH_TOOL_NAME:
                if index is not None:
                    self._search_block_indexes[index] = True
                    self._pending_query_json[index] = ""
                _dlog("claude_web_search_started", session_id=self._session_id,
                      user_id=self._user_id, block_index=index)
                out.append({"type": "web_search_start", "content": ""})

            elif block_type == "web_search_tool_result":
                tool_use_id = getattr(block, "tool_use_id", None)
                content = getattr(block, "content", None)
                results, error_code = self._parse_result_content(content)
                for r in results:
                    url = r.get("url", "")
                    if url and url not in self._seen_urls:
                        self._seen_urls.add(url)
                        self._sources.append(r)
                _dlog("claude_web_search_results", session_id=self._session_id,
                      user_id=self._user_id, tool_use_id=tool_use_id,
                      result_count=len(results), error_code=error_code)
                out.append({
                    "type": "web_search_results",
                    "results": results,
                    "error": error_code,
                })

        elif event_type == "content_block_delta":
            index = getattr(event, "index", None)
            if index is not None and index in self._pending_query_json:
                delta = getattr(event, "delta", None)
                partial = getattr(delta, "partial_json", None) if delta else None
                if partial:
                    self._pending_query_json[index] += partial

        elif event_type == "content_block_stop":
            index = getattr(event, "index", None)
            if index is not None and self._search_block_indexes.pop(index, None):
                raw = self._pending_query_json.pop(index, "")
                query = ""
                try:
                    query = json.loads(raw).get("query", "") if raw else ""
                except Exception as e:
                    _dlog("claude_web_search_query_parse_error", session_id=self._session_id,
                          user_id=self._user_id, raw=raw[:200], error=str(e)[:200])
                if query:
                    _dlog("claude_web_search_query", session_id=self._session_id,
                          user_id=self._user_id, query=query[:200])
                    out.append({"type": "web_search_query", "content": query})

        return out

    @staticmethod
    def _parse_result_content(content: Any):
        """`content` is either a list of web_search_result blocks or a single
        web_search_tool_result_error dict (per Anthropic docs). Returns
        (results_list, error_code_or_None). Never raises."""
        results: List[Dict[str, Any]] = []
        error_code = None
        try:
            if content is None:
                return results, None
            # Error shape: single object with type == web_search_tool_result_error
            content_type = getattr(content, "type", None) if not isinstance(content, (list, dict)) else (
                content.get("type") if isinstance(content, dict) else None
            )
            if content_type == "web_search_tool_result_error":
                error_code = getattr(content, "error_code", None) if not isinstance(content, dict) else content.get("error_code")
                return results, error_code
            items = content if isinstance(content, list) else []
            for item in items:
                url = getattr(item, "url", None) if not isinstance(item, dict) else item.get("url")
                title = getattr(item, "title", None) if not isinstance(item, dict) else item.get("title")
                page_age = getattr(item, "page_age", None) if not isinstance(item, dict) else item.get("page_age")
                if url:
                    results.append({
                        "url": url,
                        "title": title or url,
                        "page_age": page_age,
                        "domain": _domain_of(url),
                    })
        except Exception:
            pass
        return results, error_code
