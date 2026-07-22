from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any, Callable, Protocol, Sequence, runtime_checkable


ConversationId = int | str
MessageId = int | str
Cursor = int | str
ProgressCallback = Callable[[int, str], None]


def normalize_conversation_id(value: object) -> ConversationId | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value != 0 else None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def normalize_message_id(value: object) -> MessageId | None:
    return normalize_conversation_id(value)


class AgentRuntimeError(RuntimeError):
    """Raised when an agent runtime cannot complete a request."""


class AgentRuntimeCancelled(AgentRuntimeError):
    """Raised when a running agent request is cancelled."""


class AgentRuntimeAccessUnavailable(AgentRuntimeError):
    """Raised when authentication, quota, or rate limits block a runtime."""


class ChatProviderError(RuntimeError):
    """Raised when a chat provider cannot receive or deliver an event."""


class ForgeProviderError(RuntimeError):
    """Raised when a remote code forge operation fails."""


class VersionControlProviderError(RuntimeError):
    """Raised when a version control provider operation fails."""


class ServiceProviderError(RuntimeError):
    """Raised when a host service manager operation fails."""


@dataclass(frozen=True)
class Attachment:
    """A provider-neutral attachment reference carried by a chat event."""

    kind: str
    file_id: str = ""
    mime_type: str = ""
    filename: str = ""
    size: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatEvent:
    cursor: Cursor
    conversation_id: ConversationId
    text: str
    message_id: MessageId | None = None
    replied_text: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    passed: bool
    command: str
    output: str = ""
    summary: str = ""


@dataclass(frozen=True)
class LocalPublishResult:
    branch: str
    commit_message: str
    changed_files: list[str]
    diff: str
    doctor: Any
    commit_sha: str


@dataclass(frozen=True)
class RemotePublishResult:
    branch: str
    remote: str
    pushed: bool
    ahead_count: int
    compare_url: str | None


@dataclass(frozen=True)
class PullRequestResult:
    branch: str
    title: str
    body: str
    created: bool
    url: str | None
    fallback_url: str | None
    note: str | None = None
    draft: bool = False


@dataclass(frozen=True)
class EvolutionProvenance:
    candidate_id: str
    evidence_source: str
    signal_actor: str
    candidate_actor: str
    approval_actor: str
    task_id: int
    parent_candidate_id: str = ""
    source_task_id: int | None = None
    retry_of_task_id: int | None = None


@dataclass(frozen=True)
class PullRequestCloseResult:
    number: int
    closed: bool
    url: str
    note: str | None = None


@dataclass(frozen=True)
class PullRequestMergeStatus:
    reference: str
    url: str
    state: str
    base_branch: str
    merge_commit: str
    merged_at: str
    number: int = 0
    repository: str = ""
    is_draft: bool = False
    mergeable: str = ""
    merge_state_status: str = ""
    head_sha: str = ""
    note: str | None = None


@dataclass(frozen=True)
class PullRequestTarget:
    reference: str
    number: int
    repository: str = ""


@dataclass(frozen=True)
class PullRequestMergeCandidate:
    target: PullRequestTarget
    number: int
    repository: str
    url: str
    state: str
    is_draft: bool
    mergeable: str
    merge_state_status: str
    head_oid: str
    base_branch: str
    title: str = ""
    head_branch: str = ""
    author: str = ""
    updated_at: str = ""
    merged_at: str = ""


@dataclass(frozen=True)
class PullRequestMergeResult:
    number: int
    url: str
    method: str
    merge_commit: str
    message: str


@runtime_checkable
class AgentIdentity(Protocol):
    name: str
    mission: str


@runtime_checkable
class ChatProvider(Protocol):
    name: str
    provider_kind: str

    @property
    def allowed_conversation_id(self) -> ConversationId | None: ...

    def receive(self, cursor: Cursor | None = None) -> list[ChatEvent]: ...

    def send_message(
        self,
        conversation_id: ConversationId,
        text: str,
    ) -> MessageId | None: ...

    def edit_message(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
        text: str,
    ) -> None: ...

    def send_read_ack(
        self,
        conversation_id: ConversationId,
        message_id: MessageId,
    ) -> None: ...


@runtime_checkable
class AttachmentProvider(Protocol):
    def download_attachment(
        self,
        attachment: Attachment,
        destination: Path,
        *,
        max_bytes: int,
    ) -> None: ...


@runtime_checkable
class AgentRuntime(Protocol):
    name: str
    provider_kind: str
    config_section: str

    def respond(
        self,
        identity: AgentIdentity,
        message: str,
        cwd: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        session_key: str = "",
        image_paths: Sequence[Path] = (),
    ) -> str: ...

    def act_in_session(
        self,
        identity: AgentIdentity,
        message: str,
        cwd: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        sandbox: str = "",
        session_key: str = "",
        cancellation_event: threading.Event | None = None,
        state_root: Path | None = None,
    ) -> str: ...

    def model_summary(self, root: Path | None = None) -> str: ...

    def model_options(self) -> tuple[Any, ...]: ...

    def reset_usage(self) -> None: ...

    def health(self, root: Path | None = None) -> ProviderHealth: ...


@runtime_checkable
class VersionControlProvider(Protocol):
    name: str
    provider_kind: str

    def current_branch(self, root: Path | None = None) -> str: ...

    def is_clean(self, root: Path | None = None) -> bool: ...

    def changed_files(self, root: Path | None = None) -> list[str]: ...

    def diff_summary(self, root: Path | None = None) -> str: ...

    def stage(self, files: Sequence[str], root: Path | None = None) -> None: ...

    def commit(self, message: str, root: Path | None = None) -> str: ...

    def create_branch(
        self,
        branch: str,
        root: Path | None = None,
        *,
        start_point: str = "",
    ) -> None: ...

    def switch_branch(self, branch: str, root: Path | None = None) -> None: ...

    def delete_branch(
        self,
        branch: str,
        root: Path | None = None,
        *,
        force: bool = False,
    ) -> None: ...

    def branch_exists(self, branch: str, root: Path | None = None) -> bool: ...

    def task_base(self, root: Path | None = None) -> str: ...

    def authoritative_branch(self, root: Path | None = None) -> str: ...

    def refresh_authoritative(self, root: Path | None = None) -> str: ...

    def authoritative_revision(self, root: Path | None = None) -> str: ...

    def current_revision(self, root: Path | None = None) -> str: ...

    def resolve_revision(self, revision: str, root: Path | None = None) -> str: ...

    def is_ancestor(
        self,
        revision: str,
        descendant: str,
        root: Path | None = None,
    ) -> bool: ...

    def update_to_authoritative(self, root: Path | None = None) -> str: ...

    def restore_revision(self, revision: str, root: Path | None = None) -> None: ...

    def workspace_paths(self, root: Path | None = None) -> tuple[Path, ...]: ...

    def create_workspace(
        self,
        path: Path,
        branch: str,
        root: Path | None = None,
        *,
        start_point: str = "",
        create_branch: bool = False,
    ) -> None: ...

    def remove_workspace(self, path: Path, root: Path | None = None) -> None: ...


@runtime_checkable
class ServiceProvider(Protocol):
    name: str
    provider_kind: str

    def install(self, root: Path | None = None) -> str: ...

    def uninstall(self, root: Path | None = None) -> str: ...

    def start(self, root: Path | None = None) -> str: ...

    def stop(
        self,
        root: Path | None = None,
        *,
        allow_missing: bool = False,
    ) -> str: ...

    def restart(self, root: Path | None = None) -> str: ...

    def status(self, root: Path | None = None) -> str: ...

    def logs(self, root: Path | None = None, *, lines: int = 80) -> str: ...

    def doctor(self, root: Path | None = None) -> str: ...

    def manifest(self, root: Path | None = None) -> str: ...

    def schedule_restart(self, root: Path | None = None) -> None: ...

    def schedule_stop(self, root: Path | None = None) -> None: ...


@runtime_checkable
class ForgeProvider(Protocol):
    name: str
    provider_kind: str

    def feature_title(self, text: str) -> str: ...

    def prepare_local_publish(self, commit_message: str, **kwargs: Any) -> LocalPublishResult: ...

    def push_current_branch(self, **kwargs: Any) -> RemotePublishResult: ...

    def format_evolution_provenance(self, provenance: EvolutionProvenance) -> str: ...

    def close_pull_request(
        self,
        number: int,
        *,
        root: Path | None = None,
        comment: str | None = None,
    ) -> Any: ...

    def create_pull_request(self, **kwargs: Any) -> Any: ...

    def inspect_pull_request(self, reference: str, root: Path | None = None) -> Any: ...

    def inspect_pull_request_merge(
        self,
        reference: str,
        root: Path | None = None,
    ) -> Any: ...

    def list_open_pull_requests(
        self,
        root: Path | None = None,
        *,
        limit: int = 20,
    ) -> tuple[Any, ...]: ...

    def merge_pull_request(self, reference: str, root: Path | None = None) -> Any: ...
