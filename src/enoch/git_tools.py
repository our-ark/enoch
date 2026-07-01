from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path

from enoch.paths import repo_root


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def run_git(args: list[str], root: Path | None = None) -> GitResult:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root(root),
        text=True,
        capture_output=True,
        check=False,
    )
    return GitResult(result.returncode, result.stdout.strip(), result.stderr.strip())


def current_branch(root: Path | None = None) -> str:
    result = run_git(["branch", "--show-current"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not determine current branch.")
    return result.stdout


def ensure_clean_worktree(root: Path | None = None) -> None:
    result = run_git(["status", "--porcelain"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect worktree.")
    if result.stdout:
        raise GitError("Worktree is not clean. Commit, stash, or discard changes before evolving.")


def create_branch(branch: str, root: Path | None = None) -> None:
    result = run_git(["switch", "-c", branch], root)
    if result.returncode != 0:
        raise GitError(result.stderr or f"Could not create branch {branch}.")


def switch_branch(branch: str, root: Path | None = None) -> None:
    result = run_git(["switch", branch], root)
    if result.returncode != 0:
        raise GitError(result.stderr or f"Could not switch to branch {branch}.")


def delete_branch(branch: str, root: Path | None = None, *, force: bool = False) -> None:
    flag = "-D" if force else "-d"
    result = run_git(["branch", flag, branch], root)
    if result.returncode != 0:
        raise GitError(result.stderr or f"Could not delete branch {branch}.")


def diff_summary(root: Path | None = None) -> str:
    result = run_git(["diff", "--stat", "HEAD"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect diff.")
    untracked = untracked_files(root)
    parts = []
    if result.stdout:
        parts.append(result.stdout)
    if untracked:
        parts.append("Untracked files:\n" + "\n".join(f"  {path}" for path in untracked))
    return "\n\n".join(parts) or "No working tree changes."


def changed_files(root: Path | None = None) -> list[str]:
    result = run_git(["diff", "--name-only", "HEAD"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect changed files.")
    tracked = [line for line in result.stdout.splitlines() if line]
    return [*tracked, *untracked_files(root)]


def untracked_files(root: Path | None = None) -> list[str]:
    result = run_git(["ls-files", "--others", "--exclude-standard"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect untracked files.")
    return [line for line in result.stdout.splitlines() if line]
