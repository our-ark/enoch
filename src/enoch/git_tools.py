from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enoch.providers.contracts import VersionControlProviderError
from enoch.providers.registry import load_provider


GitError = VersionControlProviderError


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def run_git(args: list[str], root: Path | None = None) -> GitResult:
    result = load_provider("vcs", root).run(args, root)
    return GitResult(
        int(result.returncode),
        str(result.stdout).strip(),
        str(result.stderr).strip(),
    )


def current_branch(root: Path | None = None) -> str:
    provider = load_provider("vcs", root)
    if hasattr(provider, "current_branch"):
        return str(provider.current_branch(root)).strip()
    result = run_git(["branch", "--show-current"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not determine current branch.")
    return result.stdout


def ensure_clean_worktree(root: Path | None = None) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "is_clean"):
        if not provider.is_clean(root):
            raise GitError("Worktree is not clean. Commit, stash, or discard changes before evolving.")
        return
    result = run_git(["status", "--porcelain"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect worktree.")
    if result.stdout:
        raise GitError("Worktree is not clean. Commit, stash, or discard changes before evolving.")


def create_branch(
    branch: str,
    root: Path | None = None,
    *,
    start_point: str = "",
) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "create_branch"):
        provider.create_branch(branch, root, start_point=start_point)
        return
    args = ["switch", "-c", branch]
    if start_point:
        args.append(start_point)
    result = run_git(args, root)
    if result.returncode != 0:
        raise GitError(result.stderr or f"Could not create branch {branch}.")


def switch_branch(branch: str, root: Path | None = None) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "switch_branch"):
        provider.switch_branch(branch, root)
        return
    result = run_git(["switch", branch], root)
    if result.returncode != 0:
        raise GitError(result.stderr or f"Could not switch to branch {branch}.")


def delete_branch(branch: str, root: Path | None = None, *, force: bool = False) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "delete_branch"):
        provider.delete_branch(branch, root, force=force)
        return
    flag = "-D" if force else "-d"
    result = run_git(["branch", flag, branch], root)
    if result.returncode != 0:
        raise GitError(result.stderr or f"Could not delete branch {branch}.")


def diff_summary(root: Path | None = None) -> str:
    provider = load_provider("vcs", root)
    if hasattr(provider, "diff_summary"):
        return str(provider.diff_summary(root))
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
    provider = load_provider("vcs", root)
    if hasattr(provider, "changed_files"):
        return [str(path) for path in provider.changed_files(root)]
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


def stage_files(files: list[str] | tuple[str, ...], root: Path | None = None) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "stage"):
        provider.stage(files, root)
        return
    result = run_git(["add", "--", *files], root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or "Could not stage local changes.")


def commit(message: str, root: Path | None = None) -> str:
    provider = load_provider("vcs", root)
    if hasattr(provider, "commit"):
        return str(provider.commit(message, root)).strip()
    result = run_git(["commit", "-m", message], root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or "Could not commit local changes.")
    revision = run_git(["rev-parse", "--short", "HEAD"], root)
    if revision.returncode != 0:
        raise GitError(revision.stderr or revision.stdout or "Could not read commit revision.")
    return revision.stdout.strip()


def branch_exists(branch: str, root: Path | None = None) -> bool:
    provider = load_provider("vcs", root)
    if hasattr(provider, "branch_exists"):
        return bool(provider.branch_exists(branch, root))
    result = run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], root)
    return result.returncode == 0


def workspace_paths(root: Path | None = None) -> tuple[Path, ...]:
    provider = load_provider("vcs", root)
    if hasattr(provider, "workspace_paths"):
        return tuple(Path(path).expanduser().resolve() for path in provider.workspace_paths(root))
    result = run_git(["worktree", "list", "--porcelain"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not inspect repository workspaces.")
    return tuple(
        Path(line.removeprefix("worktree ")).expanduser().resolve()
        for line in result.stdout.splitlines()
        if line.startswith("worktree ")
    )


def create_workspace(
    path: Path,
    branch: str,
    root: Path | None = None,
    *,
    start_point: str = "",
    create_branch: bool = False,
) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "create_workspace"):
        provider.create_workspace(
            path,
            branch,
            root,
            start_point=start_point,
            create_branch=create_branch,
        )
        return
    args = ["worktree", "add"]
    if create_branch:
        args.extend(["-b", branch])
    args.extend([str(path), start_point or branch])
    result = run_git(args, root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or f"Could not create workspace for {branch}.")


def remove_workspace(path: Path, root: Path | None = None) -> None:
    provider = load_provider("vcs", root)
    if hasattr(provider, "remove_workspace"):
        provider.remove_workspace(path, root)
        return
    result = run_git(["worktree", "remove", str(path)], root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or f"Could not remove workspace {path}.")
