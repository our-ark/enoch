from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from enoch.channel import load_channel_lifecycle
from enoch.providers.registry import load_provider


LifecycleLoader = Callable[[str, Path | None], dict[str, Any]]
VcsLoader = Callable[..., Any]
_STATUS_DATA_ERRORS = (AttributeError, OSError, RuntimeError, TypeError, ValueError)


@dataclass(frozen=True)
class CodeVersionStatus:
    running: str = ""
    local: str = ""
    authoritative: str = ""
    authoritative_source: str = ""
    state: str = "comparison unavailable"


def code_version_status(
    root: Path,
    channel_name: str,
    *,
    lifecycle_loader: LifecycleLoader = load_channel_lifecycle,
    vcs_loader: VcsLoader = load_provider,
) -> CodeVersionStatus:
    running = ""
    try:
        lifecycle = lifecycle_loader(channel_name, root)
        running = _revision(lifecycle.get("started_head"))
    except _STATUS_DATA_ERRORS:
        pass

    local = ""
    authoritative = ""
    authoritative_source = ""
    provider = None
    try:
        provider = vcs_loader("vcs", root)
        authoritative_source = str(
            getattr(provider, "authoritative_revision_source", "") or ""
        ).strip()
    except _STATUS_DATA_ERRORS:
        pass

    if provider is not None:
        try:
            local = _revision(provider.current_revision(root))
        except _STATUS_DATA_ERRORS:
            pass
        try:
            authoritative = _revision(provider.authoritative_revision(root))
        except _STATUS_DATA_ERRORS:
            pass

    state = _version_state(running, local, authoritative, provider, root)
    return CodeVersionStatus(
        running=running,
        local=local,
        authoritative=authoritative,
        authoritative_source=authoritative_source,
        state=state,
    )


def format_code_version_status(
    root: Path,
    channel_name: str,
    *,
    lifecycle_loader: LifecycleLoader = load_channel_lifecycle,
    vcs_loader: VcsLoader = load_provider,
) -> str:
    status = code_version_status(
        root,
        channel_name,
        lifecycle_loader=lifecycle_loader,
        vcs_loader=vcs_loader,
    )
    authoritative_label = "Authoritative"
    if status.authoritative_source:
        authoritative_label += f" ({status.authoritative_source})"
    return "\n".join(
        [
            "Code version:",
            f"- Running: {_display_revision(status.running)}",
            f"- Local: {_display_revision(status.local)}",
            f"- {authoritative_label}: {_display_revision(status.authoritative)}",
            f"- State: {status.state}",
        ]
    )


def _version_state(
    running: str,
    local: str,
    authoritative: str,
    provider: Any,
    root: Path,
) -> str:
    if not running or not local or not authoritative:
        return "comparison unavailable"
    if local == authoritative:
        return "current" if running == local else "restart needed"
    if provider is None:
        return "comparison unavailable"
    try:
        if provider.is_ancestor(local, authoritative, root):
            return "update available"
        if provider.is_ancestor(authoritative, local, root):
            return "local changes not published"
    except _STATUS_DATA_ERRORS:
        return "comparison unavailable"
    return "comparison unavailable"


def _revision(value: object) -> str:
    return str(value or "").strip()


def _display_revision(revision: str) -> str:
    return revision[:7] if revision else "unavailable"
