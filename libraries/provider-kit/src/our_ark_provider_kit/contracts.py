from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol, Sequence, runtime_checkable


ConversationId = int | str
MessageId = int | str
Cursor = int | str
ProgressCallback = Callable[[int, str], None]
CAPABILITY_CONTRACT_VERSION = 1
RUNTIME_CONTRACT_VERSION = 1
RUNTIME_EXECUTION_CONTRACT_VERSION = 1
NOTIFICATION_CONTRACT_VERSION = 1
REPOSITORY_CONTRACT_VERSION = 1
REVIEW_CONTRACT_VERSION = 1


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


class AgentRuntimeTimedOut(AgentRuntimeError):
    """Raised when a runtime invocation exceeds its execution deadline."""


class AgentRuntimeAccessUnavailable(AgentRuntimeError):
    """Raised when authentication, quota, or rate limits block a runtime."""


class ChatProviderError(RuntimeError):
    """Raised when a chat provider cannot receive or deliver an event."""


class NotificationDeliveryError(ChatProviderError):
    """Raised when notification delivery fails with known retry semantics."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous


class ForgeProviderError(RuntimeError):
    """Raised when a remote code forge operation fails."""


class VersionControlProviderError(RuntimeError):
    """Raised when a version control provider operation fails."""


class RepositoryProviderError(RuntimeError):
    """Raised when a semantic repository operation fails."""


class ReviewProviderError(RuntimeError):
    """Raised when a semantic review operation fails."""


class UnsupportedProviderFeature(RuntimeError):
    """Raised before an operation requiring an unsupported provider feature."""

    def __init__(self, provider_kind: str, missing: Sequence[str]) -> None:
        features = tuple(dict.fromkeys(str(item).strip() for item in missing if str(item).strip()))
        super().__init__(
            f"The selected {provider_kind} provider does not support: "
            f"{', '.join(features) or 'the requested feature'}."
        )
        self.provider_kind = provider_kind
        self.missing = features


class ServiceProviderError(RuntimeError):
    """Raised when a host service manager operation fails."""


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_kind: str
    capabilities: frozenset[str]
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        kind = _capability_segment(self.provider_kind, "provider kind")
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError(
                f"Provider capabilities use contract version {self.contract_version}; "
                f"supported version is {CAPABILITY_CONTRACT_VERSION}."
            )
        capabilities = frozenset(
            _normalize_capability(value)
            for value in self.capabilities
        )
        invalid = sorted(
            capability
            for capability in capabilities
            if capability.split(".", 1)[0] != kind
        )
        if invalid:
            raise ValueError(
                f"Provider kind {kind} cannot declare capabilities for another "
                f"provider kind: {', '.join(invalid)}."
            )
        object.__setattr__(self, "provider_kind", kind)
        object.__setattr__(self, "capabilities", capabilities)

    def supports(self, capability: str) -> bool:
        return _normalize_capability(capability) in self.capabilities


@dataclass(frozen=True)
class TaskRequirements:
    capabilities: tuple[str, ...] = ()
    reason: str = ""
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError(
                f"Task requirements use contract version {self.contract_version}; "
                f"supported version is {CAPABILITY_CONTRACT_VERSION}."
            )
        capabilities = tuple(
            dict.fromkeys(
                _normalize_capability(value)
                for value in self.capabilities
            )
        )
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "reason", str(self.reason).strip())


@dataclass(frozen=True)
class AuthorizationRequest:
    action: str
    requirements: TaskRequirements
    provider_capabilities: tuple[ProviderCapabilities, ...]
    task_id: int | None = None
    profile_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError(
                f"Authorization request uses contract version {self.contract_version}; "
                f"supported version is {CAPABILITY_CONTRACT_VERSION}."
            )
        action = str(self.action).strip().lower().replace("_", "-")
        if not action:
            raise ValueError("Authorization action is required.")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "profile_name", self.profile_name.strip().lower())


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str = ""
    denied_capabilities: tuple[str, ...] = ()
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError(
                f"Authorization decision uses contract version {self.contract_version}; "
                f"supported version is {CAPABILITY_CONTRACT_VERSION}."
            )
        denied = tuple(
            dict.fromkeys(
                _normalize_capability(value)
                for value in self.denied_capabilities
            )
        )
        if self.allowed and denied:
            raise ValueError(
                "An allowed authorization decision cannot deny capabilities."
            )
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", str(self.reason).strip())
        object.__setattr__(self, "denied_capabilities", denied)


@runtime_checkable
class AuthorizationPolicy(Protocol):
    def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision: ...


@runtime_checkable
class CapabilityProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...


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
class NotificationCapabilities:
    idempotent_delivery: bool = False
    reconciliation: bool = False
    contract_version: int = NOTIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != NOTIFICATION_CONTRACT_VERSION:
            raise ValueError(
                f"Notification capabilities use contract version {self.contract_version}; "
                f"supported version is {NOTIFICATION_CONTRACT_VERSION}."
            )


@dataclass(frozen=True)
class NotificationIntent:
    idempotency_key: str
    operation: str
    conversation_id: ConversationId
    text: str
    message_id: MessageId | None = None
    daemon_epoch: str = ""
    contract_version: int = NOTIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        key = self.idempotency_key.strip()
        operation = self.operation.strip().lower()
        if self.contract_version != NOTIFICATION_CONTRACT_VERSION:
            raise ValueError(
                f"Notification intent uses contract version {self.contract_version}; "
                f"supported version is {NOTIFICATION_CONTRACT_VERSION}."
            )
        if not key:
            raise ValueError("Notification idempotency key is required.")
        if operation not in {"send", "edit"}:
            raise ValueError("Notification operation must be send or edit.")
        if operation == "edit" and normalize_message_id(self.message_id) is None:
            raise ValueError("Edit notifications require a message id.")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "text", str(self.text))
        object.__setattr__(self, "daemon_epoch", self.daemon_epoch.strip())


@dataclass(frozen=True)
class NotificationReceipt:
    idempotency_key: str
    status: str
    message_id: MessageId | None = None
    provider_reference: str = ""
    detail: str = ""
    contract_version: int = NOTIFICATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        key = self.idempotency_key.strip()
        status = self.status.strip().lower()
        if self.contract_version != NOTIFICATION_CONTRACT_VERSION:
            raise ValueError(
                f"Notification receipt uses contract version {self.contract_version}; "
                f"supported version is {NOTIFICATION_CONTRACT_VERSION}."
            )
        if status not in {"delivered", "not_found", "unknown"}:
            raise ValueError(
                "Notification receipt status must be delivered, not_found, or unknown."
            )
        if not key:
            raise ValueError("Notification receipt idempotency key is required.")
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "provider_reference", self.provider_reference.strip())
        object.__setattr__(self, "detail", self.detail.strip())


@dataclass(frozen=True)
class ProviderHealth:
    name: str
    passed: bool
    command: str
    output: str = ""
    summary: str = ""


@dataclass(frozen=True)
class RuntimeProgress:
    elapsed_seconds: int
    stage: str = "running"
    message: str = ""
    sandbox: str = ""
    session_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = RUNTIME_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUNTIME_EXECUTION_CONTRACT_VERSION:
            raise ValueError(
                f"Runtime progress uses contract version {self.contract_version}; "
                f"supported version is {RUNTIME_EXECUTION_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "elapsed_seconds", max(0, int(self.elapsed_seconds)))
        object.__setattr__(self, "stage", str(self.stage).strip().lower() or "running")
        object.__setattr__(self, "message", str(self.message).strip())
        object.__setattr__(self, "sandbox", str(self.sandbox).strip())
        object.__setattr__(self, "session_id", str(self.session_id).strip())


RuntimeProgressCallback = Callable[[RuntimeProgress], None]


@dataclass(frozen=True)
class RuntimeExecutionControl:
    request_id: str = ""
    session_key: str = ""
    timeout_seconds: int | None = None
    cancellation_event: threading.Event | None = None
    timeout_event: threading.Event | None = None
    progress_callback: RuntimeProgressCallback | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    started_at_monotonic: float = field(default_factory=time.monotonic)
    contract_version: int = RUNTIME_EXECUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUNTIME_EXECUTION_CONTRACT_VERSION:
            raise ValueError(
                f"Runtime execution uses contract version {self.contract_version}; "
                f"supported version is {RUNTIME_EXECUTION_CONTRACT_VERSION}."
            )
        timeout = self.timeout_seconds
        if timeout is not None:
            timeout = int(timeout)
            if timeout <= 0:
                raise ValueError("Runtime execution timeout must be positive.")
        object.__setattr__(self, "request_id", str(self.request_id).strip())
        object.__setattr__(self, "session_key", str(self.session_key).strip())
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(
            self,
            "started_at_monotonic",
            float(self.started_at_monotonic),
        )

    @property
    def timed_out(self) -> bool:
        if self.timeout_event is not None and self.timeout_event.is_set():
            return True
        return (
            self.timeout_seconds is not None
            and time.monotonic() - self.started_at_monotonic >= self.timeout_seconds
        )

    @property
    def cancelled(self) -> bool:
        return (
            not self.timed_out
            and self.cancellation_event is not None
            and self.cancellation_event.is_set()
        )

    def raise_if_stopped(self) -> None:
        if self.timed_out:
            raise AgentRuntimeTimedOut("Agent runtime execution timed out.")
        if self.cancelled:
            raise AgentRuntimeCancelled("Agent runtime execution was cancelled.")

    def emit_progress(self, progress: RuntimeProgress) -> None:
        if self.progress_callback is not None:
            self.progress_callback(progress)


@dataclass(frozen=True)
class RuntimeUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_tokens",
        ):
            object.__setattr__(self, name, max(0, int(getattr(self, name))))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeOutputReference:
    kind: str
    uri: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeSideEffect:
    kind: str
    reference: str
    status: str = "completed"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeResult:
    final_text: str
    session_id: str = ""
    completion_reason: str = "completed"
    usage: RuntimeUsage = field(default_factory=RuntimeUsage)
    events: tuple[RuntimeEvent, ...] = ()
    output_refs: tuple[RuntimeOutputReference, ...] = ()
    side_effects: tuple[RuntimeSideEffect, ...] = ()
    contract_version: int = RUNTIME_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != RUNTIME_CONTRACT_VERSION:
            raise ValueError(
                f"Runtime result uses contract version {self.contract_version}; "
                f"supported version is {RUNTIME_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "final_text", str(self.final_text))
        object.__setattr__(self, "session_id", str(self.session_id).strip())
        reason = str(self.completion_reason).strip().lower().replace("_", "-")
        object.__setattr__(self, "completion_reason", reason or "completed")


RuntimeResultLike = RuntimeResult | str


def normalize_runtime_result(value: RuntimeResultLike) -> RuntimeResult:
    if isinstance(value, RuntimeResult):
        return value
    if isinstance(value, str):
        return RuntimeResult(final_text=value)
    raise TypeError(
        "Agent runtime must return RuntimeResult or str, "
        f"not {type(value).__name__}."
    )


@dataclass(frozen=True)
class RepositoryFeatures:
    """Portable repository behavior, independent of branches or staging."""

    staging_index: bool = False
    named_branches: bool = False
    isolated_workspaces: bool = True
    immutable_revisions: bool = True
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Repository features use contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )

    def supports(self, feature: str) -> bool:
        name = str(feature).strip().lower().replace("-", "_")
        if name not in {
            "staging_index",
            "named_branches",
            "isolated_workspaces",
            "immutable_revisions",
        }:
            raise ValueError(f"Unknown repository feature {feature!r}.")
        return bool(getattr(self, name))


@dataclass(frozen=True)
class RepositoryRevision:
    id: str
    display: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        revision_id = str(self.id).strip()
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Repository revision uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        if not revision_id:
            raise ValueError("Repository revision id is required.")
        object.__setattr__(self, "id", revision_id)
        object.__setattr__(self, "display", str(self.display).strip() or revision_id)


@dataclass(frozen=True)
class WorkingCopyState:
    revision: RepositoryRevision
    clean: bool
    changed_paths: tuple[str, ...] = ()
    summary: str = ""
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Working-copy state uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        paths = tuple(dict.fromkeys(str(path).strip() for path in self.changed_paths if str(path).strip()))
        if self.clean and paths:
            raise ValueError("A clean working copy cannot report changed paths.")
        object.__setattr__(self, "clean", bool(self.clean))
        object.__setattr__(self, "changed_paths", paths)
        object.__setattr__(self, "summary", str(self.summary).strip())


@dataclass(frozen=True)
class AuthoritativeBase:
    revision: RepositoryRevision
    name: str = ""
    refreshed: bool = False
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Authoritative base uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "name", str(self.name).strip())
        object.__setattr__(self, "refreshed", bool(self.refreshed))


@dataclass(frozen=True)
class ChangeCaptureRequest:
    message: str
    paths: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        message = " ".join(str(self.message).split())
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Change-capture request uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        if not message:
            raise ValueError("Change-capture message is required.")
        object.__setattr__(self, "message", message)
        object.__setattr__(
            self,
            "paths",
            tuple(dict.fromkeys(str(path).strip() for path in self.paths if str(path).strip())),
        )


@dataclass(frozen=True)
class ChangeCaptureResult:
    revision: RepositoryRevision
    changed_paths: tuple[str, ...]
    summary: str = ""
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Change-capture result uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        paths = tuple(dict.fromkeys(str(path).strip() for path in self.changed_paths if str(path).strip()))
        if not paths:
            raise ValueError("A captured change must report at least one changed path.")
        object.__setattr__(self, "changed_paths", paths)
        object.__setattr__(self, "summary", str(self.summary).strip())


@dataclass(frozen=True)
class WorkspaceRequest:
    path: Path
    base_revision: RepositoryRevision
    workspace_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Workspace request uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "path", self.path.expanduser().resolve())
        object.__setattr__(self, "workspace_id", str(self.workspace_id).strip())


@dataclass(frozen=True)
class RepositoryWorkspace:
    id: str
    path: Path
    base_revision: RepositoryRevision
    current_revision: RepositoryRevision
    contract_version: int = REPOSITORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        workspace_id = str(self.id).strip()
        if self.contract_version != REPOSITORY_CONTRACT_VERSION:
            raise ValueError(
                f"Repository workspace uses contract version {self.contract_version}; "
                f"supported version is {REPOSITORY_CONTRACT_VERSION}."
            )
        if not workspace_id:
            raise ValueError("Repository workspace id is required.")
        object.__setattr__(self, "id", workspace_id)
        object.__setattr__(self, "path", self.path.expanduser().resolve())


@dataclass(frozen=True)
class ReviewFeatures:
    stacked_changes: bool = False
    signals: bool = True
    landing: bool = True
    mutable_versions: bool = True
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review features use contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )

    def supports(self, feature: str) -> bool:
        name = str(feature).strip().lower().replace("-", "_")
        if name not in {
            "stacked_changes",
            "signals",
            "landing",
            "mutable_versions",
        }:
            raise ValueError(f"Unknown review feature {feature!r}.")
        return bool(getattr(self, name))


@dataclass(frozen=True)
class ReviewIdentity:
    id: str
    url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        review_id = str(self.id).strip()
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review identity uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not review_id:
            raise ValueError("Review identity is required.")
        object.__setattr__(self, "id", review_id)
        object.__setattr__(self, "url", str(self.url).strip())


@dataclass(frozen=True)
class ReviewVersion:
    id: str
    revision: RepositoryRevision
    created_at: str = ""
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        version_id = str(self.id).strip()
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review version uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not version_id:
            raise ValueError("Review version id is required.")
        object.__setattr__(self, "id", version_id)
        object.__setattr__(self, "created_at", str(self.created_at).strip())


@dataclass(frozen=True)
class ReviewSignal:
    name: str
    status: str
    summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        status = str(self.status).strip().lower().replace("_", "-")
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review signal uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not name or not status:
            raise ValueError("Review signals require name and status.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "summary", str(self.summary).strip())


@dataclass(frozen=True)
class ReviewSubmission:
    title: str
    body: str
    revision: RepositoryRevision
    base_revision: RepositoryRevision | None = None
    review: ReviewIdentity | None = None
    dependencies: tuple[ReviewIdentity, ...] = ()
    evidence: tuple[str, ...] = ()
    draft: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        title = " ".join(str(self.title).split())
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review submission uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not title:
            raise ValueError("Review title is required.")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "body", str(self.body))
        object.__setattr__(
            self,
            "evidence",
            tuple(dict.fromkeys(str(item).strip() for item in self.evidence if str(item).strip())),
        )
        object.__setattr__(self, "draft", bool(self.draft))


@dataclass(frozen=True)
class ReviewRecord:
    identity: ReviewIdentity
    title: str
    body: str
    state: str
    versions: tuple[ReviewVersion, ...]
    dependencies: tuple[ReviewIdentity, ...] = ()
    signals: tuple[ReviewSignal, ...] = ()
    draft: bool = False
    contract_version: int = REVIEW_CONTRACT_VERSION
    landed_revision: RepositoryRevision | None = None
    landed_at: str = ""

    def __post_init__(self) -> None:
        state = str(self.state).strip().lower().replace("_", "-")
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review record uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not state:
            raise ValueError("Review state is required.")
        if not self.versions:
            raise ValueError("A review record requires at least one version.")
        object.__setattr__(self, "title", " ".join(str(self.title).split()))
        object.__setattr__(self, "body", str(self.body))
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "draft", bool(self.draft))
        object.__setattr__(self, "landed_at", str(self.landed_at).strip())


@dataclass(frozen=True)
class ReviewCloseRequest:
    review: ReviewIdentity
    note: str = ""
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review-close request uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        object.__setattr__(self, "note", str(self.note).strip())


@dataclass(frozen=True)
class ReviewLandRequest:
    review: ReviewIdentity
    strategy: str = "merge"
    contract_version: int = REVIEW_CONTRACT_VERSION

    def __post_init__(self) -> None:
        strategy = str(self.strategy).strip().lower().replace("_", "-")
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review-land request uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not strategy:
            raise ValueError("Review landing strategy is required.")
        object.__setattr__(self, "strategy", strategy)


@dataclass(frozen=True)
class ReviewLandResult:
    review: ReviewIdentity
    status: str
    revision: RepositoryRevision | None = None
    message: str = ""
    contract_version: int = REVIEW_CONTRACT_VERSION
    landed_at: str = ""

    def __post_init__(self) -> None:
        status = str(self.status).strip().lower().replace("_", "-")
        if self.contract_version != REVIEW_CONTRACT_VERSION:
            raise ValueError(
                f"Review-land result uses contract version {self.contract_version}; "
                f"supported version is {REVIEW_CONTRACT_VERSION}."
            )
        if not status:
            raise ValueError("Review landing status is required.")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "landed_at", str(self.landed_at).strip())
        object.__setattr__(self, "message", str(self.message).strip())


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
class DurableNotificationProvider(Protocol):
    @property
    def notification_capabilities(self) -> NotificationCapabilities: ...

    def deliver_notification(
        self,
        intent: NotificationIntent,
    ) -> NotificationReceipt: ...

    def reconcile_notification(
        self,
        intent: NotificationIntent,
    ) -> NotificationReceipt: ...


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
        execution: RuntimeExecutionControl | None = None,
    ) -> RuntimeResultLike: ...

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
        execution: RuntimeExecutionControl | None = None,
    ) -> RuntimeResultLike: ...

    def model_summary(self, root: Path | None = None) -> str: ...

    def model_options(self) -> tuple[Any, ...]: ...

    def reset_usage(self) -> None: ...

    def health(self, root: Path | None = None) -> ProviderHealth: ...


@runtime_checkable
class RepositoryProvider(Protocol):
    """Portable repository operations without required branches or staging."""

    name: str
    provider_kind: str
    capabilities: ProviderCapabilities
    repository_features: RepositoryFeatures

    def inspect_working_copy(
        self,
        root: Path | None = None,
    ) -> WorkingCopyState: ...

    def resolve_repository_revision(
        self,
        reference: str,
        root: Path | None = None,
    ) -> RepositoryRevision | None: ...

    def repository_is_ancestor(
        self,
        ancestor: RepositoryRevision,
        descendant: RepositoryRevision,
        root: Path | None = None,
    ) -> bool: ...

    def authoritative_base(
        self,
        root: Path | None = None,
        *,
        refresh: bool = False,
    ) -> AuthoritativeBase: ...

    def capture_change(
        self,
        request: ChangeCaptureRequest,
        root: Path | None = None,
    ) -> ChangeCaptureResult: ...

    def restore_repository_revision(
        self,
        revision: RepositoryRevision,
        root: Path | None = None,
    ) -> None: ...

    def list_repository_workspaces(
        self,
        root: Path | None = None,
    ) -> tuple[RepositoryWorkspace, ...]: ...

    def create_repository_workspace(
        self,
        request: WorkspaceRequest,
        root: Path | None = None,
    ) -> RepositoryWorkspace: ...

    def remove_repository_workspace(
        self,
        workspace: RepositoryWorkspace,
        root: Path | None = None,
        *,
        force: bool = False,
    ) -> None: ...


@runtime_checkable
class ReviewProvider(Protocol):
    """Portable review operations using opaque review identities."""

    name: str
    provider_kind: str
    capabilities: ProviderCapabilities
    review_features: ReviewFeatures

    def publish_review(
        self,
        request: ReviewSubmission,
        root: Path | None = None,
    ) -> ReviewRecord: ...

    def inspect_review(
        self,
        review: ReviewIdentity,
        root: Path | None = None,
    ) -> ReviewRecord: ...

    def list_open_reviews(
        self,
        root: Path | None = None,
        *,
        limit: int = 20,
    ) -> tuple[ReviewRecord, ...]: ...

    def close_review(
        self,
        request: ReviewCloseRequest,
        root: Path | None = None,
    ) -> ReviewRecord: ...

    def land_review(
        self,
        request: ReviewLandRequest,
        root: Path | None = None,
    ) -> ReviewLandResult: ...


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


def require_repository_features(
    provider: RepositoryProvider,
    *features: str,
) -> None:
    missing = tuple(
        feature
        for feature in features
        if not provider.repository_features.supports(feature)
    )
    if missing:
        raise UnsupportedProviderFeature("repository", missing)


def require_review_features(
    provider: ReviewProvider,
    *features: str,
) -> None:
    missing = tuple(
        feature
        for feature in features
        if not provider.review_features.supports(feature)
    )
    if missing:
        raise UnsupportedProviderFeature("review", missing)


def _normalize_capability(value: object) -> str:
    text = str(value).strip().lower().replace("_", "-")
    parts = text.split(".")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid capability {value!r}; expected <provider-kind>.<operation>."
        )
    return ".".join(
        _capability_segment(part, "capability segment")
        for part in parts
    )


def _capability_segment(value: object, label: str) -> str:
    text = str(value).strip().lower().replace("_", "-")
    if (
        not text
        or not text[0].isalpha()
        or any(not (character.isalnum() or character == "-") for character in text)
    ):
        raise ValueError(f"Invalid {label} {value!r}.")
    return text
