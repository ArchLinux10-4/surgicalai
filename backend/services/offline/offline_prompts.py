"""
Prompts tuned for Qwen2.5-Coder:7b specifically — NOT shared with Claude/GPT prompts.

Evidence-based design (see OFFLINE_MODE.md):
  - SEARCH/REPLACE diff-style editing is unreliable at 7B (Aider leaderboard:
    ~51.9% success even with whole-file format; smaller models sometimes echo
    the SEARCH/REPLACE instructions back instead of executing them). So this
    pipeline asks for a WHOLE-FILE rewrite, wrapped in one unambiguous marker
    the model is unlikely to also use for anything else.
  - No tool-calling / function-calling is used anywhere in this module —
    local 7B models emit tool-call JSON as plain text or loop forever waiting
    for a tool response (cline/cline#10843). Plain chat completions only.
  - No agent mode / multi-step task planning — out of scope by design.
"""

OFFLINE_CHAT_SYSTEM = """You are a helpful local coding assistant running fully offline.
Answer the user's question directly and concisely. If code is attached, you may
reference it, but do not rewrite files unless explicitly asked to."""

# Unique marker unlikely to collide with anything a model would naturally emit.
FILE_REWRITE_START = "###OFFLINE_FILE_REWRITE_START###"
FILE_REWRITE_END = "###OFFLINE_FILE_REWRITE_END###"

OFFLINE_EDIT_SYSTEM = f"""You are a local coding assistant running fully offline. You are given ONE file
and a request to change it. You must rewrite the ENTIRE file with the requested
change applied — do not use diffs, do not use SEARCH/REPLACE blocks, do not
omit any part of the file.

Output format (follow EXACTLY, nothing else):

1. One short paragraph explaining what you changed, in plain text.
2. Then the marker on its own line: {FILE_REWRITE_START}
3. Then the COMPLETE new file content, with no markdown code fences.
4. Then the marker on its own line: {FILE_REWRITE_END}

Do not include anything after the end marker. Do not truncate the file —
if the file is long, still write all of it."""


def build_edit_user_prompt(filename: str, file_content: str, user_request: str) -> str:
    return (
        f"FILE: {filename}\n"
        f"--- CURRENT CONTENT ---\n{file_content}\n--- END CURRENT CONTENT ---\n\n"
        f"REQUEST: {user_request}\n\n"
        f"Rewrite the entire file above with this change applied, following the "
        f"exact output format you were given."
    )
