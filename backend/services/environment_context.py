"""Environment section injected into every agent prompt for a repo chat.

Unscoped chats and hosted deploys get an empty string, so their prompts
stay the same as before this feature. A local install that sets
DATABASE_URL is hosted by the existing USE_POSTGRES signal and also gets
an empty string.
"""
import os
import platform
from pathlib import Path

from services.repositories import detect_git, repository_for_session


def environment_block(conn, session_id: str) -> str:
    """Stable identity and capabilities, plus the current branch on the last line.

    `conn` is unused on purpose: the repository lookup has its own connection
    and must not depend on the caller's transaction. The argument is kept so
    every _load_effective_memory site can pass the connection it already holds.
    """
    del conn
    from database import USE_POSTGRES
    if USE_POSTGRES or not session_id:
        return ""
    try:
        repo = repository_for_session(session_id)
    except Exception:
        return ""
    if not repo or repo.get("removed_at"):
        return ""
    root = repo.get("root_path") or ""
    if not root or not os.path.isdir(root):
        return (
            "ENVIRONMENT\n"
            "SurgicalAI is running locally, but this chat's folder is missing on disk. "
            "Ask the user to relink it. Do not invent file contents."
        )
    shell = os.environ.get("SHELL") or "unknown"
    owner = (repo.get("github_owner") or "").strip()
    gh_repo = (repo.get("github_repo") or "").strip()
    remote = (repo.get("git_remote_url") or "").strip()
    git = detect_git(root)
    if git.get("remote"):
        remote = git["remote"]
    branch = (git.get("branch") or repo.get("default_branch") or "").strip()
    lines = [
        "ENVIRONMENT",
        "SurgicalAI is running locally on this machine.",
        f"OS: {platform.system() or 'unknown'}",
        f"Shell: {Path(shell).name}",
        f"Repository: {repo.get('name') or Path(root).name}",
        f"Root: {root}",
    ]
    if remote:
        lines.append(f"Git remote: {remote}")
    if owner and gh_repo:
        lines.append(f"GitHub: {owner}/{gh_repo}")
        lines.append("GitHub reads and pushes are available only when that account is connected.")
    else:
        lines.append("This folder is not linked to a GitHub repository.")
    lines.append(
        "You can read files in this repository. Edits are staged in the chat and "
        "are not written to disk until the user applies them. "
        "When git_cli is available, use it for git and pull requests. "
        "You cannot run other shell commands."
    )
    if branch:
        lines.append(f"Current branch: {branch}. Call git_status for uncommitted changes.")
    return "\n".join(lines)
