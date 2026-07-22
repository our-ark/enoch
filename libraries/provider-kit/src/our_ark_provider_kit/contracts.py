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

    def run(
        self,
        args: list[str],
        root: Path | None = None,
    ) -> Any: ...


@runtime_checkable
class ForgeProvider(Protocol):
    name: str
    provider_kind: str

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
