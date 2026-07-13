# Offline Mode (Qwen2.5-Coder:7b via Ollama)

## Why this is a separate codebase
This package (`backend/services/offline/`) does not import from or modify
`services.pipeline`'s Claude/OpenAI logic. The only touchpoint outside this
folder is a single dispatch check in `backend/routers/chat.py`'s `/smart-stream`
handler, gated behind `_should_use_ollama()` (an existing, unmodified helper
in `pipeline.py`). If offline mode is off or a cloud key exists, the exact
original Claude/GPT code path runs unchanged.

## Scope (v1)
- Plain chat (multi-turn, with project memory + session summary)
- Single-file whole-file rewrite (edit intent detected by keyword cues)
- Manual review/apply of rewritten files — no auto-apply to disk

## Explicitly out of scope (v1), and why
Real-world evidence (GitHub issues, Aider's official leaderboard, Stack
Overflow, community reports) shows these break down at the 7B scale:

1. **Agent mode / multi-step task planning** — not attempted. Already
   structurally gated: the task-planning branch in `chat.py` only fires for
   Claude (`_is_claude`), so non-Claude/offline models always fall through to
   single-pass regardless of this feature.
2. **Tool-calling / function-calling** — none anywhere in this module.
   `cline/cline#10843`: local models emit tool-call JSON as OpenAI-style
   `{"name":...}` text instead of using the framework's expected format,
   causing infinite retry loops that burn context until timeout.
3. **SEARCH/REPLACE diff-style edits** — not used. Aider's own leaderboard
   defaults small/local models to whole-file format rather than diffs;
   quantized 7B builds have been observed echoing SEARCH/REPLACE
   instructions back as output instead of executing them.
4. **Multi-file edits in one pass** — v1 edits one file (auto-picked or
   named in the request) per turn.

## Mitigations baked into `offline_client.py` (each evidence-backed)
| Failure mode | Source | Mitigation |
|---|---|---|
| Ollama's default 2048-token context silently drops the system prompt | Aider-AI/aider#2371 | `num_ctx` always set explicitly (16384 default) |
| Hard mid-token truncation, no "stop at sentence" option | ollama/ollama#4230 (maintainer-confirmed) | Size-aware `num_predict` + `done_reason == "length"` truncation detection surfaced to the user |
| Requests hang indefinitely under memory pressure | StackOverflow #79040747 (67-min hang), continuedev/continue#10124 (500s) | Explicit client-side timeout (180s) + 1 retry, then a clear error instead of an infinite wait |
| Model unloads after 5 min idle, slow reload | Ollama FAQ | `keep_alive: "30m"` set on every request |
| Quantized builds echo chat-template tokens into output | Aider-AI/aider#2371 thread | Regex-based defensive stripping before treating output as final |

## Settings
`ollama_enabled`, `ollama_base_url`, `ollama_model` — configurable from
Settings → Models tab. Falls back to Ollama only when no cloud API key is
configured (existing `_should_use_ollama()` behavior, unchanged).
