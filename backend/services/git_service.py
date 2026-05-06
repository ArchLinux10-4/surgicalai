"""Git service — runs git operations in a subprocess."""
import subprocess
import os
from pathlib import Path
from typing import Optional

from models.schemas import GitStatus


def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a git command and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_status(repo_path: str) -> GitStatus:
    """Get git status for a directory."""
    code, out, err = _run_git(["rev-parse", "--is-inside-work-tree"], repo_path)
    if code != 0:
        return GitStatus(is_repo=False)

    # Current branch
    _, branch, _ = _run_git(["branch", "--show-current"], repo_path)

    # Status
    _, status_out, _ = _run_git(["status", "--porcelain"], repo_path)

    staged, unstaged, untracked = [], [], []
    for line in status_out.splitlines():
        if len(line) < 3:
            continue
        x, y, filepath = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            untracked.append(filepath)
        elif x != " " and x != "?":
            staged.append(filepath)
        elif y != " " and y != "?":
            unstaged.append(filepath)

    return GitStatus(
        is_repo=True,
        branch=branch,
        staged=staged,
        unstaged=unstaged,
        untracked=untracked
    )


def get_diff(repo_path: str, file_path: Optional[str] = None) -> str:
    """Get git diff for repo or specific file."""
    args = ["diff"]
    if file_path:
        args.append(file_path)
    _, out, _ = _run_git(args, repo_path)
    return out


def stage_file(repo_path: str, file_path: str) -> bool:
    code, _, err = _run_git(["add", file_path], repo_path)
    return code == 0


def commit(repo_path: str, message: str, files: Optional[list[str]] = None) -> tuple[bool, str]:
    """Stage files and commit."""
    if files:
        for f in files:
            _run_git(["add", f], repo_path)
    else:
        _run_git(["add", "-A"], repo_path)

    code, out, err = _run_git(["commit", "-m", message], repo_path)
    return code == 0, out or err


def get_log(repo_path: str, limit: int = 20) -> list[dict]:
    """Get recent commit log."""
    _, out, _ = _run_git(
        ["log", f"--max-count={limit}", "--pretty=format:%H|%s|%an|%ar"],
        repo_path
    )
    log = []
    for line in out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            log.append({
                "hash": parts[0][:8],
                "message": parts[1],
                "author": parts[2],
                "when": parts[3]
            })
    return log
