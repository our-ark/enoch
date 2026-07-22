from __future__ import annotations

from datetime import datetime
from pathlib import Path

from enoch.git_tools import GitError, run_git
from enoch.paths import repo_root
from enoch.providers.registry import load_provider, provider_name
from enoch.runtime import DEFAULT_BRANCH, DEFAULT_REMOTE


def main_pull_summary(root: Path | None = None) -> str:
    fetch_head = fetch_head_path(root)
    main_sha = origin_main_sha(root)
    if fetch_head is None or not fetch_head.exists():
        return f"Last git main pull observed: unavailable{_sha_suffix(main_sha)}"
    try:
        content = fetch_head.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return f"Last git main pull observed: unavailable{_sha_suffix(main_sha)}"
    if f"branch '{DEFAULT_BRANCH}'" not in content:
        return f"Last git main pull observed: unavailable{_sha_suffix(main_sha)}"
    pulled_at = datetime.fromtimestamp(fetch_head.stat().st_mtime).astimezone()
    return f"Last git main pull observed: {pulled_at.strftime('%Y-%m-%d %H:%M:%S %Z')}{_sha_suffix(main_sha)}"


def fetch_head_path(root: Path | None = None) -> Path | None:
    result = run_git(["rev-parse", "--git-path", "FETCH_HEAD"], root)
    if result.returncode != 0 or not result.stdout:
        return None
    path = Path(result.stdout)
    if path.is_absolute():
        return path
    return (root or Path.cwd()) / path


def origin_main_sha(root: Path | None = None) -> str:
    result = run_git(["rev-parse", "--short", f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"], root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def fetch_origin_main(root: Path | None = None) -> None:
    result = run_git(["fetch", DEFAULT_REMOTE, DEFAULT_BRANCH], root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or f"Could not fetch {DEFAULT_REMOTE}/{DEFAULT_BRANCH}.")


def head_merged_into_origin_main(root: Path | None = None) -> bool:
    return revision_merged_into_origin_main("HEAD", root)


def revision_merged_into_origin_main(
    revision: str,
    root: Path | None = None,
) -> bool:
    result = run_git(
        [
            "merge-base",
            "--is-ancestor",
            revision,
            f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}",
        ],
        root,
    )
    return result.returncode == 0


def current_head(root: Path | None = None) -> str:
    result = run_git(["rev-parse", "HEAD"], root)
    if result.returncode != 0 or not result.stdout:
        raise GitError(result.stderr or "Could not inspect HEAD.")
    return result.stdout.strip()


def pull_origin_main(root: Path | None = None) -> str:
    result = run_git(["pull", "--ff-only", DEFAULT_REMOTE, DEFAULT_BRANCH], root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or f"Could not pull {DEFAULT_REMOTE}/{DEFAULT_BRANCH}.")
    return result.stdout or "Already up to date."


def reset_hard(revision: str, root: Path | None = None) -> None:
    result = run_git(["reset", "--hard", revision], root)
    if result.returncode != 0:
        raise GitError(result.stderr or result.stdout or f"Could not roll back to {revision}.")


def schedule_daemon_restart(root: Path | None = None) -> None:
    resolved_root = repo_root(root)
    load_provider("service", resolved_root).schedule_restart(resolved_root)


def schedule_daemon_stop(root: Path | None = None) -> None:
    resolved_root = repo_root(root)
    load_provider("service", resolved_root).schedule_stop(resolved_root)


def ensure_local_main_current(root: Path) -> None:
    fetch_origin_main(root)
    local = rev_parse(DEFAULT_BRANCH, root)
    remote = rev_parse(f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}", root)
    if local and remote and local != remote:
        branch = current_branch_name(root)
        if branch != DEFAULT_BRANCH:
            raise GitError(
                f"Local {DEFAULT_BRANCH} is not up to date with {DEFAULT_REMOTE}/{DEFAULT_BRANCH}. "
                f"Switch to {DEFAULT_BRANCH} before evolving."
            )
        pull_origin_main(root)


def task_branch_base(root: Path) -> str:
    selected = provider_name("vcs", root)
    if selected != "git":
        provider = load_provider("vcs", root, name=selected)
        if hasattr(provider, "task_base"):
            return str(provider.task_base(root)).strip()
    fetch_error: GitError | None = None
    try:
        fetch_origin_main(root)
    except GitError as error:
        fetch_error = error

    remote = f"{DEFAULT_REMOTE}/{DEFAULT_BRANCH}"
    if rev_parse(remote, root):
        return remote
    if rev_parse(DEFAULT_BRANCH, root):
        return DEFAULT_BRANCH
    if fetch_error is not None:
        raise GitError(f"Could not prepare a task branch base: {fetch_error}") from fetch_error
    raise GitError(f"Could not find {remote} or local {DEFAULT_BRANCH}.")


def current_branch_name(root: Path | None = None) -> str:
    result = run_git(["branch", "--show-current"], root)
    if result.returncode != 0:
        raise GitError(result.stderr or "Could not determine current branch.")
    return result.stdout.strip()


def rev_parse(revision: str, root: Path | None = None) -> str:
    result = run_git(["rev-parse", revision], root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _sha_suffix(sha: str) -> str:
    return f" ({DEFAULT_REMOTE}/{DEFAULT_BRANCH} {sha})" if sha else ""
