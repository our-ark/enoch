from __future__ import annotations

from pathlib import Path

from enoch.vcs_tools import (
    VcsError,
    authoritative_branch,
    authoritative_revision,
    current_branch,
    current_revision,
    refresh_authoritative,
    resolve_revision,
    restore_revision,
    revision_is_ancestor,
    update_to_authoritative,
)
from enoch.paths import repo_root
from enoch.providers.registry import load_provider


def repository_sync_summary(root: Path | None = None) -> str:
    provider = load_provider("vcs", root)
    summary = getattr(provider, "sync_summary", None)
    if callable(summary):
        return str(summary(root)).strip()
    try:
        branch = authoritative_branch(root)
        revision = authoritative_revision(root)
    except VcsError as error:
        return f"Authoritative repository revision: unavailable ({error})"
    return f"Authoritative repository revision: {branch} {revision[:7]}"


def main_pull_summary(root: Path | None = None) -> str:
    """Compatibility alias for integrations using the original Git-specific name."""
    return repository_sync_summary(root)


def fetch_origin_main(root: Path | None = None) -> None:
    refresh_repository(root)


def head_merged_into_origin_main(root: Path | None = None) -> bool:
    return revision_merged_into_origin_main("HEAD", root)


def revision_merged_into_origin_main(
    revision: str,
    root: Path | None = None,
) -> bool:
    return revision_on_authoritative(revision, root)


def current_head(root: Path | None = None) -> str:
    return current_repository_revision(root)


def pull_origin_main(root: Path | None = None) -> str:
    return update_repository(root)


def reset_hard(revision: str, root: Path | None = None) -> None:
    restore_repository_revision(revision, root)


def authoritative_branch_name(root: Path | None = None) -> str:
    return authoritative_branch(root)


def refresh_repository(root: Path | None = None) -> str:
    return refresh_authoritative(root)


def authoritative_repository_revision(root: Path | None = None) -> str:
    return authoritative_revision(root)


def current_repository_revision(root: Path | None = None) -> str:
    return current_revision(root)


def revision_on_authoritative(revision: str, root: Path | None = None) -> bool:
    return revision_is_ancestor(revision, authoritative_repository_revision(root), root)


def current_revision_on_authoritative(root: Path | None = None) -> bool:
    return revision_on_authoritative(current_repository_revision(root), root)


def update_repository(root: Path | None = None) -> str:
    return update_to_authoritative(root) or "Already up to date."


def restore_repository_revision(revision: str, root: Path | None = None) -> None:
    restore_revision(revision, root)


def schedule_daemon_restart(root: Path | None = None) -> None:
    resolved_root = repo_root(root)
    load_provider("service", resolved_root).schedule_restart(resolved_root)


def schedule_daemon_stop(root: Path | None = None) -> None:
    resolved_root = repo_root(root)
    load_provider("service", resolved_root).schedule_stop(resolved_root)


def ensure_authoritative_current(root: Path) -> None:
    refresh_repository(root)
    branch_name = authoritative_branch_name(root)
    local = resolve_revision(branch_name, root)
    remote = authoritative_repository_revision(root)
    if local and remote and local != remote:
        branch = current_branch(root)
        if branch != branch_name:
            raise VcsError(
                f"Local {branch_name} is not up to date with the authoritative revision. "
                f"Switch to {branch_name} before evolving."
            )
        update_repository(root)


def ensure_local_main_current(root: Path) -> None:
    """Compatibility alias for integrations using the original Git-specific name."""
    ensure_authoritative_current(root)


def task_branch_base(root: Path) -> str:
    provider = load_provider("vcs", root)
    method = getattr(provider, "task_base", None)
    if not callable(method):
        raise VcsError(
            f"VCS provider {getattr(provider, 'name', 'unknown')} does not support task_base."
        )
    return str(method(root)).strip()


def current_branch_name(root: Path | None = None) -> str:
    return current_branch(root)


def rev_parse(revision: str, root: Path | None = None) -> str:
    return resolve_revision(revision, root)
