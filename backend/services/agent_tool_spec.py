"""Shared wording for agent tools, rendered once for tags and once for functions.

Claude, GPT, and Gemini learn tools from tag text. Grok, and GPT in Ask/Plan,
learn them from function descriptions. Both come from the functions below so
the same tool cannot be described two different ways.

GitHub and live web search are not described here. They keep their own
catalogues.
"""
import os

MAX_FILES_PER_REQUEST = 5
MAX_SEARCH_TERMS = 5
# Same default as AGENT_MAX_TURNS and the Ask/Plan round cap.
DEFAULT_MAX_TURNS = 60

_NAMES = {
    "tag": {
        "file": "<file_request>",
        "search": "<search_request>",
        "repo": "<repo_request>",
        "github": "<github_request>",
        "web": "<web_search_request>",
        "edit": "<surgical_edit>",
        "new": "<new_file>",
        "plan": "<edit_plan>",
        "blocked": "<blocked>",
        "history": "<history_request>",
        "git": "<git_cli>",
        "local": "<local_cli>",
    },
    "function": {
        "file": "request_file",
        "search": "request_search",
        "repo": "request_repo",
        "github": "request_github",
        "web": "request_web_search",
        "edit": "write_surgical_edit",
        "new": "write_new_file",
        "plan": "write_edit_plan",
        "blocked": "report_blocked",
        "history": "request_history",
        "git": "git_cli",
        "local": "run_local",
    },
}


def _names(form: str) -> dict:
    if form not in _NAMES:
        raise ValueError(f"unknown form {form}")
    return _NAMES[form]


def default_max_turns() -> int:
    """Round cap the edit loop and Ask/Plan both read from AGENT_MAX_TURNS."""
    raw = os.getenv("AGENT_MAX_TURNS", str(DEFAULT_MAX_TURNS))
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_TURNS


def scope_block(*, session_files=False, repo=False, github=False, web=False, git=False, local=False, form="tag") -> str:
    """Where each enabled read tool looks. Empty unless at least two are on."""
    n = _names(form)
    lines = []
    if session_files:
        lines.append(
            f"{n['file']} and {n['search']}: files already attached to this chat."
        )
    if repo:
        lines.append(
            f"{n['repo']}: the folder open on the user's computer. "
            "read_repo_file copies a file into this chat so you can edit it. "
            "Nothing is written to disk until the user applies it."
        )
    if github:
        lines.append(f"{n['github']}: the connected GitHub repository.")
    if web:
        lines.append(f"{n['web']}: the public internet, not the code.")
    if git:
        lines.append(
            f"{n['git']}: git and pull requests on the open folder. "
            "One command at a time. Push, pull requests, and merging wait for approval."
        )
    if local:
        lines.append(
            f"{n['local']}: commands and files on this Mac. "
            "Reads run immediately. Changes wait for approval."
        )
    if len(lines) < 2:
        return ""
    return "Where each tool looks. " + " ".join(lines)


def limits_block(form="tag", max_rounds=None) -> str:
    """Caps the model can hit. Round count comes from the caller, not a guess."""
    rounds = default_max_turns() if max_rounds is None else int(max_rounds)
    return (
        "One context request per turn, then wait for the result. "
        f"At most {MAX_FILES_PER_REQUEST} files or {MAX_SEARCH_TERMS} search terms "
        "in that request. Extra requests in the same turn are skipped. "
        f"You can continue across turns, up to {rounds} turns."
    )


def _family_line(family: str) -> str:
    if family == "claude":
        return (
            "If line numbers are shown, also send edit_start_line and edit_end_line "
            "(absolute file lines, inclusive). Lines are tried first and old_code is the backup."
        )
    if family in ("gpt", "gemini"):
        return gpt_anchor_block().strip()
    if family == "grok":
        return (
            "Send old_code with new_code for each site. "
            "edit_start_line and edit_end_line are optional."
        )
    raise ValueError(f"unknown family {family}")


def edit_ladder(form="tag", family="claude") -> str:
    """One way to choose how an edit is addressed. Family line is the only difference."""
    n = _names(form)
    return "\n".join([
        "How to point at the code you change.",
        f"1. A small symbol you can see completely: send new_code only, with {n['edit']}. "
        "It replaces the whole symbol, so include every line.",
        "2. Anything larger: send old_code and new_code. They replace only that text. "
        "Copy old_code exactly. It must match one place, so add a few surrounding lines "
        "if it could match twice.",
        "3. " + _family_line(family),
        "4. A file with no named symbols, such as yaml or plain text: use the filename as symbol.",
        f"5. {n['new']} is only for a file that does not exist yet. "
        "Never create a new file to avoid editing an existing one.",
    ])


def gpt_anchor_block() -> str:
    """GPT and Gemini miscount line numbers. old_code is required for them.

    Kept as its own block so the meaning stays the one shipped after session
    e387acd9. Claude does not see this block.
    """
    return (
        "\n\n━━━ IMPORTANT — LINE NUMBERS FOR THIS MODEL ━━━\n"
        "You (this model) have been observed miscounting exact file "
        "line numbers on large files, which silently destroys edits. "
        "For every <surgical_edit> you emit:\n"
        "- ALWAYS include \"old_code\" with the exact original text "
        "you are replacing, copied verbatim — this is your primary "
        "anchor, not the line numbers.\n"
        "- edit_start_line/edit_end_line are OPTIONAL extra context "
        "for you; they are not required and are not a substitute "
        "for old_code.\n"
        "- Never omit old_code just because you can see line "
        "numbers in the file listing — your line counting on large "
        "files is not reliable enough to use alone.\n"
    )


def plan_text(form="tag") -> str:
    n = _names(form)
    load = n["file"]
    edit = n["edit"]
    distinct = "DISTINCT" if form == "function" else "distinct"
    return (
        f"Use {n['plan']} for changes across 3 or more {distinct} files or symbols. "
        "It ends your turn: each step runs separately with focused context, so put a "
        "short description in each step and no code. One step per symbol. "
        f"Several changes inside one large symbol are not a plan: use {load}, then "
        f"several {edit} calls."
    )


def blocked_text(form="tag") -> str:
    n = _names(form)
    if form == "tag":
        return (
            "Use only when required context truly cannot be found and you cannot continue. "
            f"Say exactly what you need inside {n['blocked']}…</blocked>. This ends your turn."
        )
    return (
        f"Call {n['blocked']} only when required context truly cannot be found and you "
        "cannot continue. Say exactly what you need. This ends your turn."
    )


def history_text(form="tag") -> str:
    n = _names(form)
    body = (
        f"{n['history']} shows how a file in this chat looked before an edit that was saved here. "
        "Use it when the user asks how the code used to look, what an edit changed, "
        "or to go back toward an older version. "
        "The result is marked HISTORICAL. It is NOT the current file. "
        "Never use it as what the code is now. "
        "It covers the edits made in this chat, including a file opened from a folder "
        "once that edit is saved here. It does not see a change made on disk outside this chat. "
        "Use git_diff for the open folder's git history and uncommitted changes. "
        "Use GitHub diff_branches for two branches on GitHub. "
        "This is read only. You cannot restore a version. The user restores one from "
        "the file's version history in the app. You can show the old code and write a "
        "normal edit that brings it back. "
        "filename is required. "
        "view list numbers the saved versions, oldest first. "
        "Omit view to show one version's content. version last is the file right before "
        "the most recent edit. version first is the file before any edit in this chat. "
        "view diff compares that version with the current file, or with compare_to, "
        "another version number. "
        "query searches the saved text and names the version each hit came from."
    )
    if form == "tag":
        examples = (
            '<history_request>{"filename":"app.py","version":"last"}</history_request>\n'
            '<history_request>{"filename":"app.py","version":"last","view":"diff"}</history_request>'
        )
    else:
        examples = (
            'Example: request_history with filename "app.py" and version "last", '
            "for how the file looked before the most recent edit. "
            'Example: request_history with filename "app.py", version "last", and view "diff", '
            "for what that edit changed."
        )
    return body + "\n" + examples


def file_request_example(form="tag") -> str:
    if form == "tag":
        return '<file_request>["LandingPage.tsx", "App.tsx"]</file_request>'
    return 'request_file with filenames ["LandingPage.tsx", "App.tsx"]'


def search_blurb(form="tag") -> str:
    n = _names(form)
    if form == "tag":
        return (
            f"{n['search']} greps files already in this chat. "
            'Example: <search_request>{"terms": ["exactSymbol"], "reason": "why"}</search_request>. '
            "Use it to find where something is defined. "
            f"Do not use it for the public internet, GitHub, or the open folder."
        )
    return (
        "Grep files already in this chat for a symbol or text. "
        "Returns matching snippets with file and line. "
        "Not the public internet, not GitHub, and not the open folder. "
        "One context request per turn; extra requests are skipped. "
        f"Up to {MAX_SEARCH_TERMS} terms."
    )


def file_blurb(form="tag") -> str:
    n = _names(form)
    if form == "tag":
        return (
            f"{n['file']} loads the full text of files already in this chat, by exact name. "
            f"Example: {file_request_example('tag')}. "
            f"Up to {MAX_FILES_PER_REQUEST} files. "
            "Use it when you know the filename. Use search when you do not."
        )
    return (
        "Load the full text of files already in this chat, by exact name, before you edit. "
        "One context request per turn; extra requests are skipped. "
        f"Up to {MAX_FILES_PER_REQUEST} files."
    )


def new_code_description() -> str:
    return (
        "The replacement for exactly the text old_code (or the line range) covers. "
        "With neither old_code nor a line range, this is the complete new symbol."
    )


def old_code_description() -> str:
    return (
        "The exact original text you are replacing, copied verbatim. "
        "It must match one place. With old_code set, new_code replaces only that text, "
        "not the rest of the symbol."
    )


def symbol_description() -> str:
    return (
        "Exact symbol name from the SYMBOL INDEX. "
        "For a file with no named symbols, use the filename."
    )


def repo_text(form="tag", repo=None) -> str:
    """Open-folder read tool. Same words everywhere it is shown."""
    n = _names(form)
    if repo:
        name = repo.get("name") or "repository"
        root = repo.get("root_path") or ""
        where = f'You can read the open local repository "{name}" at {root}.'
    else:
        where = "You can read the open local repository."
    when = "Use this for files on disk that are not in this chat yet."
    diff = (
        "git_diff shows what changed. With no extra fields it compares your "
        "local HEAD to the repository (origin/main, or the repo's default "
        "branch, or main) and also shows uncommitted changes. It does not "
        "fetch. scope is unstaged, staged, worktree, or range. view is stat "
        "(the default) or patch. path limits it to one file. base and head "
        "name the two refs for a range, and dots is two or three. Use "
        "git_status for just the file list, and GitHub diff_branches only "
        "for two branches on GitHub."
    )
    if form == "tag":
        examples = "\n".join([
            '<repo_request>{"tool":"list_repo_files"}</repo_request>',
            '<repo_request>{"tool":"read_repo_file","path":"src/app.py"}</repo_request>',
            '<repo_request>{"tool":"search_repo","terms":["symbol"]}</repo_request>',
            '<repo_request>{"tool":"git_status"}</repo_request>',
            '<repo_request>{"tool":"git_diff"}</repo_request>',
            '<repo_request>{"tool":"git_diff","scope":"worktree","view":"patch","path":"backend/app.py"}</repo_request>',
        ])
    else:
        examples = (
            f"Call {n['repo']} with tool list_repo_files, read_repo_file (needs path), "
            "search_repo (needs terms), git_status, or git_diff."
        )
    return (
        f"{where}\n{when}\n{examples}\n{diff}\n"
        "read_repo_file loads the file into this chat. Chat edits are not git-staged; "
        "git status includes which chat files are unsaved or changed on disk. "
        "You cannot run a shell from this tool."
    )


def git_cli_text(form="tag") -> str:
    """How to publish the open folder. Tag form is for Claude, GPT, and Gemini."""
    rules = (
        "One command per turn, then stop. "
        "Reads run immediately, including in Ask and Plan: git status, diff, log, show, "
        "branch with no name, rev-parse, remote -v, ls-files, blame, fetch, "
        "gh pr view, gh pr list, gh pr checks, and gh auth status. "
        "On a branch that is not the default, Edit and Agent also run these immediately: "
        "git switch, git switch -c, git checkout -b, git branch NAME, git add, "
        "git restore --staged, git commit -m, git stash push, git stash pop, "
        "git merge --abort, and git pull (it is forced to --ff-only). "
        "A commit while you are on the default branch asks the user first. "
        "git push and gh pr create and gh pr ready ask the user first. "
        "Never push the default branch. Open a pull request instead. "
        "gh pr merge and git merge always ask the user. You cannot merge until they approve. "
        "Ask and Plan cannot change the repository. "
        "reset --hard, clean, rebase, force push, and git config are refused. "
        "Branch first. Do not commit on the default branch. "
        "After a merge, git switch to the default branch and git pull --ff-only to sync. "
        "If a pull cannot fast-forward, propose git merge origin/BRANCH. "
        "Edits in this chat are not on disk until the user applies them. "
        "The only remote name is origin. "
        "To put local changes on GitHub, call git publish once with --branch, --message, --title, and --body. "
        "Do not chain add, commit, push, and gh pr create for that. "
        "If a push or a pull request fails, tell the user the error and stop. Do not read this app's source code."
    )
    if form == "tag":
        return (
            "How to call git. Emit exactly one tag and then stop. "
            "The open tag is exactly <git_cli> with no attributes. "
            "The body is one command, not JSON.\n"
            "<git_cli>\ngit status\n</git_cli>\n"
            "<git_cli>\ngit publish --branch surgicalai/short-name --message \"Why\" --title \"Title\" --body \"What changed\"\n</git_cli>\n"
            "<git_cli>\ngit switch -c surgicalai/short-name\n</git_cli>\n"
            "<git_cli>\ngh pr create --title \"Title\" --body \"Why\" --base main\n</git_cli>\n"
            "<git_cli>\ngh pr merge 123 --merge\n</git_cli>\n"
            + rules
        )
    return (
        "Run one git or gh command on the open folder. "
        'Call git_cli with {"command": "git status"}. '
        'Example: {"command": "git publish --branch surgicalai/short-name --message \\"Why\\" --title \\"Title\\" --body \\"What changed\\""}. '
        'Example: {"command": "git switch -c surgicalai/short-name"}. '
        'Example: {"command": "gh pr create --title \\"Title\\" --body \\"Why\\" --base main"}. '
        'Example: {"command": "gh pr merge 123 --merge"}. '
        + rules
    )


def local_text(form="tag") -> str:
    """Mac terminal and file tool. Tag form is for Claude, GPT, and Gemini."""
    rules = (
        "run_local runs one action on the user's Mac: exec (a command), read, write, or list. "
        "Use it for work the other tools cannot do: running tests, builds, and scripts, and "
        "reading or writing files that are not part of this chat. One action per turn, then "
        "wait for the result. "
        "Reads and plain listing run at once. Writes, installs, network use, deletes, and "
        "anything outside the open folder wait for the user's approval. If the user rejects, "
        "do not try it again unless they ask. "
        "Commands run without a shell: no pipes, no redirects, no &&. If you truly need a "
        "shell, send bash -lc \"...\" and it will always ask. No sudo. "
        "Do not use it for git, gh, or aws. Use their tools. "
        "Edits to files in the open repository go through the normal edit flow, not write. "
        "Each result shows the exit code, stdout, stderr, and says if output was cut at "
        "32000 characters. Ask and Plan can only read."
    )
    if form == "tag":
        return (
            "How to call the local Mac tool. Emit exactly one tag and then stop. "
            "The open tag is exactly <local_cli> with no attributes.\n"
            "<local_cli>\naction: exec\nls\n</local_cli>\n"
            "<local_cli>\naction: read\npath: README.md\nlines: 1-80\n</local_cli>\n"
            "<local_cli>\naction: write\npath: scratch-note.txt\n---\nhello\n</local_cli>\n"
            + rules
        )
    return (
        "Call run_local with action exec, read, write, or list. "
        'Example: {"action": "exec", "command": "ls"}. '
        'Example: {"action": "read", "path": "README.md", "lines": "1-80"}. '
        'Example: {"action": "write", "path": "scratch-note.txt", "content": "hello"}. '
        + rules
    )


def data_file_section(form="function") -> str:
    """Data-file guidance without tag examples. Used for Grok only."""
    n = _names(form)
    return (
        "━━━ DATA FILE ANALYSIS ━━━\n\n"
        "The uploaded files include data files (CSV, Excel, or similar). "
        "Their content is shown as markdown tables above.\n"
        "Answer questions from those tables.\n"
        f"To create or replace a data file, call {n['new']} with the complete CSV content. "
        "An .xlsx filename is converted to Excel.\n"
        f"Do not call {n['file']} for data files. They are already loaded.\n"
        "Do not say you cannot edit Excel. Do not tell the user to run a Python script."
    )
