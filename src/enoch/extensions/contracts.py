from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable, Literal

from enoch.identity import Identity
from enoch.providers.contracts import (
    AgentRuntime,
    ChatEvent,
    ChatProvider,
    ConversationId,
    RepositoryProvider,
    ReviewProvider,
    TaskRequirements,
)
from enoch.providers.authorization import CapabilityAuthorizationError
from enoch.storage import StorageLayout
from enoch.tasks.queue import (
    TaskAlreadyExists,
    TaskJob,
    TaskQueueStatus,
    TaskRetryError,
)
from enoch.workflows import WorkflowEngine, WorkflowEngineError


AGENT_EXTENSION_API_VERSION = 1
EXTENSION_COMMAND_RESULT_API_VERSION = 1
ExtensionCommandStatus = Literal["succeeded", "failed"]
ExtensionTaskControlOperation = Literal["status", "cancel", "retry", "rerun"]
ExtensionTaskState = Literal[
    "pending",
    "paused",
    "running",
    "completed",
    "failed",
    "cancelled",
    "regressed",
]
ExtensionTaskEventType = Literal[
    "queued",
    "started",
    "completed",
    "failed",
    "cancelled",
]


class AgentExtensionError(RuntimeError):
    pass


class ExtensionCommandEnqueueError(AgentExtensionError):
    """A governed task could not be enqueued by an extension command."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "enqueue_failed",
        task_ids: tuple[int, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.task_ids = task_ids


class ExtensionWorkflowControlError(AgentExtensionError):
    """An extension requested a task transition outside its bounded authority."""

    def __init__(
        self,
        code: str,
        operation: ExtensionTaskControlOperation,
        task_id: int,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation
        self.task_id = task_id


@dataclass(frozen=True)
class ExtensionTaskStatus:
    """Read-only status for a task owned by one extension."""

    extension_name: str
    task_id: int
    state: ExtensionTaskState
    request: str
    parent_task_id: int | None = None
    retryable: bool = False
    idempotency_key: str = ""

    @property
    def terminal(self) -> bool:
        return self.state in {"completed", "failed", "cancelled", "regressed"}


@dataclass(frozen=True)
class ExtensionCommandResult:
    """Versioned outcome returned by an agent-extension command."""

    final_text: str
    status: ExtensionCommandStatus = "succeeded"
    code: str = "ok"
    task_ids: tuple[int, ...] = ()
    output_refs: tuple[str, ...] = ()
    api_version: int = EXTENSION_COMMAND_RESULT_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != EXTENSION_COMMAND_RESULT_API_VERSION:
            raise AgentExtensionError(
                "Extension command result uses API version "
                f"{self.api_version}; Enoch supports version "
                f"{EXTENSION_COMMAND_RESULT_API_VERSION}."
            )
        if not isinstance(self.final_text, str):
            raise AgentExtensionError(
                "Extension command result final text must be a string."
            )
        if self.status not in ("succeeded", "failed"):
            raise AgentExtensionError(
                f"Invalid extension command result status {self.status!r}."
            )
        if not isinstance(self.code, str):
            raise AgentExtensionError(
                "Extension command result code must be a string."
            )
        code = self.code.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", code):
            raise AgentExtensionError(
                f"Invalid extension command result code {self.code!r}."
            )
        if self.status == "failed" and code == "ok":
            raise AgentExtensionError(
                "Failed extension command results require a non-ok code."
            )
        task_ids = _result_task_ids(self.task_ids)
        output_refs = _result_output_refs(self.output_refs)
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "task_ids", task_ids)
        object.__setattr__(self, "output_refs", output_refs)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @classmethod
    def success(
        cls,
        final_text: str,
        *,
        code: str = "ok",
        task_ids: tuple[int, ...] = (),
        output_refs: tuple[str, ...] = (),
    ) -> ExtensionCommandResult:
        return cls(
            final_text=final_text,
            status="succeeded",
            code=code,
            task_ids=task_ids,
            output_refs=output_refs,
        )

    @classmethod
    def failure(
        cls,
        final_text: str,
        *,
        code: str,
        task_ids: tuple[int, ...] = (),
        output_refs: tuple[str, ...] = (),
    ) -> ExtensionCommandResult:
        return cls(
            final_text=final_text,
            status="failed",
            code=code,
            task_ids=task_ids,
            output_refs=output_refs,
        )


def normalize_extension_command_result(
    result: str | ExtensionCommandResult,
) -> ExtensionCommandResult:
    """Normalize the v1 string shorthand into a typed command result."""

    if isinstance(result, ExtensionCommandResult):
        return result
    if isinstance(result, str):
        return ExtensionCommandResult.success(result)
    raise AgentExtensionError(
        "Extension command handlers must return str or ExtensionCommandResult."
    )


@dataclass(frozen=True)
class ExtensionTaskEvent:
    """A durable workflow event routed back to its originating extension."""

    id: str
    extension_name: str
    task_id: int
    event: ExtensionTaskEventType
    occurred_at: str
    request: str
    result_summary: str = ""
    workspace_id: str = ""
    review_id: str = ""
    review_urls: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    publish_stage: str = ""
    revision_id: str = ""
    attempt: int = 0
    max_attempts: int = 3
    failure_code: str = ""
    failure_class: str = ""
    retryable: bool = False
    runtime_provider: str = ""
    runtime_session_id: str = ""
    runtime_completion_reason: str = ""
    runtime_usage: dict[str, int] = field(default_factory=dict)
    runtime_output_refs: tuple[str, ...] = ()
    runtime_side_effects: tuple[str, ...] = ()

    @property
    def delivery_key(self) -> str:
        return f"extension:{self.extension_name}:task-event:{self.id}"


@dataclass(frozen=True)
class ExtensionWorkflow:
    """Namespaced access to Enoch's single governed task workflow."""

    extension_name: str
    _enqueue: Callable[..., TaskJob] = field(repr=False)
    _inspect: Callable[[], TaskQueueStatus] = field(repr=False)
    _find: Callable[[int], TaskJob | None] = field(repr=False)
    _cancel: Callable[..., TaskJob | None] = field(repr=False)
    _retry_failed: Callable[..., TaskJob] = field(repr=False)
    _request_running_cancellation: Callable[[int], None] = field(
        default=lambda _task_id: None,
        repr=False,
    )
    _task_options: dict[str, object] = field(default_factory=dict, repr=False)

    @classmethod
    def from_engine(
        cls,
        extension_name: str,
        engine: WorkflowEngine,
        *,
        task_options: dict[str, object] | None = None,
        request_running_cancellation: Callable[[int], None] | None = None,
    ) -> ExtensionWorkflow:
        return cls(
            extension_name=_extension_name(extension_name),
            _enqueue=engine.enqueue,
            _inspect=engine.inspect,
            _find=engine.find,
            _cancel=engine.cancel,
            _retry_failed=engine.retry_failed,
            _request_running_cancellation=(
                request_running_cancellation or (lambda _task_id: None)
            ),
            _task_options=dict(task_options or {}),
        )

    def enqueue(
        self,
        conversation_id: ConversationId,
        request: str,
        *,
        context: str = "",
        initiated_by: str = "agent",
        event_actor: str = "agent",
        trigger: str = "",
        required_capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
    ) -> TaskJob:
        options = dict(self._task_options)
        inherited = tuple(options.pop("required_capabilities", ()))
        options["required_capabilities"] = tuple(
            dict.fromkeys((*inherited, *required_capabilities))
        )
        key = idempotency_key.strip()
        return self._enqueue(
            conversation_id,
            request,
            context=context,
            context_source=f"extension:{self.extension_name}",
            source="task",
            initiated_by=initiated_by,
            event_actor=event_actor,
            trigger=trigger.strip() or f"extension:{self.extension_name}",
            idempotency_key=(
                f"extension:{self.extension_name}:{key}" if key else ""
            ),
            **options,
        )

    def inspect(self) -> TaskQueueStatus:
        return self._inspect()

    def find(self, task_id: int) -> TaskJob | None:
        return self._find(task_id)

    def status(self, task_id: int) -> ExtensionTaskStatus:
        return self._status(self._owned_task(task_id, "status"))

    def cancel(
        self,
        task_id: int,
        *,
        result: str = "",
    ) -> ExtensionTaskStatus:
        job = self._owned_task(task_id, "cancel")
        if job.status == "cancelled":
            return self._status(job)
        if job.status not in {"pending", "paused", "running"}:
            raise self._control_error(
                "invalid_state",
                "cancel",
                task_id,
                f"Extension task #{task_id} is {job.status} and cannot be cancelled.",
            )
        if job.status == "running":
            self._request_running_cancellation(task_id)
        cancelled = self._cancel(
            task_id,
            result=result.strip() or f"Cancelled by extension {self.extension_name}.",
            event_actor="agent",
            trigger=f"extension:{self.extension_name}:cancel",
        )
        if cancelled is None:
            raise self._control_error(
                "transition_conflict",
                "cancel",
                task_id,
                f"Extension task #{task_id} changed before it could be cancelled.",
            )
        return self._status(cancelled)

    def retry(self, task_id: int) -> ExtensionTaskStatus:
        job = self._owned_task(task_id, "retry")
        if job.status != "failed":
            raise self._control_error(
                "invalid_state",
                "retry",
                task_id,
                f"Extension task #{task_id} is not failed.",
            )
        if not job.retryable:
            raise self._control_error(
                "not_retryable",
                "retry",
                task_id,
                f"Extension task #{task_id} is not eligible for retry; rerun it instead.",
            )
        try:
            retried = self._retry_failed(
                task_id,
                event_actor="agent",
                trigger=f"extension:{self.extension_name}:retry",
            )
        except TaskRetryError as error:
            raise self._control_error(
                "retry_conflict",
                "retry",
                task_id,
                str(error),
            ) from error
        return self._status(retried)

    def rerun(
        self,
        task_id: int,
        *,
        idempotency_key: str,
    ) -> ExtensionTaskStatus:
        original = self._owned_task(task_id, "rerun")
        if original.status not in {"completed", "failed", "cancelled", "regressed"}:
            raise self._control_error(
                "invalid_state",
                "rerun",
                task_id,
                f"Extension task #{task_id} is not terminal.",
            )
        key = idempotency_key.strip()
        if not key:
            raise self._control_error(
                "idempotency_required",
                "rerun",
                task_id,
                "Extension task reruns require a stable idempotency key.",
            )
        options = dict(self._task_options)
        inherited = tuple(options.pop("required_capabilities", ()))
        options["required_capabilities"] = tuple(
            dict.fromkeys((*inherited, *original.required_capabilities))
        )
        options["max_attempts"] = original.max_attempts
        options["timeout_seconds"] = original.timeout_seconds
        rerun = self._enqueue(
            original.chat_id,
            original.text,
            context=original.context,
            context_source=f"extension:{self.extension_name}",
            source=original.source,
            initiated_by="agent",
            event_actor="agent",
            trigger=f"extension:{self.extension_name}:rerun",
            candidate_id=original.candidate_id,
            parent_task_id=original.id,
            evidence_source=original.evidence_source,
            signal_actor=original.signal_actor,
            candidate_actor=original.candidate_actor,
            approval_actor=original.approval_actor,
            parent_candidate_id=original.parent_candidate_id,
            source_task_id=original.source_task_id,
            idempotency_key=(
                f"extension:{self.extension_name}:rerun:{key}"
            ),
            **options,
        )
        return self._status(rerun)

    def _owned_task(
        self,
        task_id: int,
        operation: ExtensionTaskControlOperation,
    ) -> TaskJob:
        if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
            raise self._control_error(
                "invalid_task_id",
                operation,
                task_id,
                "Extension task IDs must be positive integers.",
            )
        job = self._find(task_id)
        if job is None:
            raise self._control_error(
                "task_not_found",
                operation,
                task_id,
                f"Task #{task_id} does not exist.",
            )
        if job.context_source != f"extension:{self.extension_name}":
            raise self._control_error(
                "task_not_owned",
                operation,
                task_id,
                f"Task #{task_id} is not owned by extension {self.extension_name}.",
            )
        return job

    def _status(self, job: TaskJob) -> ExtensionTaskStatus:
        return ExtensionTaskStatus(
            extension_name=self.extension_name,
            task_id=job.id,
            state=job.status,
            request=job.text,
            parent_task_id=job.parent_task_id,
            retryable=job.retryable,
            idempotency_key=job.idempotency_key,
        )

    @staticmethod
    def _control_error(
        code: str,
        operation: ExtensionTaskControlOperation,
        task_id: int,
        message: str,
    ) -> ExtensionWorkflowControlError:
        return ExtensionWorkflowControlError(
            code,
            operation,
            task_id,
            message,
        )


@dataclass(frozen=True)
class ExtensionCommandContext:
    identity: Identity
    root: Path
    storage: StorageLayout
    conversation_id: ConversationId
    event: ChatEvent
    command: str
    argument: str
    runtime: AgentRuntime
    repository: RepositoryProvider
    review: ReviewProvider
    workflow: ExtensionWorkflow

    def enqueue_task(
        self,
        request: str,
        *,
        context: str = "",
        required_capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
    ) -> TaskJob:
        key = idempotency_key.strip() or f"command:{self.event.message_id}"
        try:
            return self.workflow.enqueue(
                self.conversation_id,
                request,
                context=context,
                initiated_by="human",
                event_actor="human",
                trigger=self.command,
                required_capabilities=required_capabilities,
                idempotency_key=key,
            )
        except TaskAlreadyExists as error:
            raise ExtensionCommandEnqueueError(
                str(error),
                code="task_already_exists",
                task_ids=(error.job.id,),
            ) from error
        except CapabilityAuthorizationError:
            raise
        except (OSError, ValueError, WorkflowEngineError) as error:
            raise ExtensionCommandEnqueueError(str(error)) from error


ExtensionCommandHandler = Callable[
    [ExtensionCommandContext],
    str | ExtensionCommandResult,
]


@dataclass(frozen=True)
class ExtensionCommandSpec:
    name: str
    summary: str
    handler: ExtensionCommandHandler
    usage: str = ""
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = _command_name(self.name)
        summary = self.summary.strip()
        if not summary:
            raise AgentExtensionError(
                f"Agent extension command /{name} requires a summary."
            )
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "usage", self.usage.strip())
        object.__setattr__(
            self,
            "required_capabilities",
            TaskRequirements(self.required_capabilities).capabilities,
        )

    @property
    def command(self) -> str:
        return f"/{self.name}"

    def matches(self, command: str) -> bool:
        try:
            return _command_name(command) == self.name
        except AgentExtensionError:
            return False


@dataclass(frozen=True)
class ExtensionLifecycleContext:
    identity: Identity
    root: Path
    storage: StorageLayout
    chat: ChatProvider
    runtime: AgentRuntime
    repository: RepositoryProvider
    review: ReviewProvider
    workflow: ExtensionWorkflow


ExtensionLifecycleHook = Callable[[ExtensionLifecycleContext], None]
ExtensionTaskEventHook = Callable[
    [ExtensionLifecycleContext, ExtensionTaskEvent],
    None,
]


@dataclass(frozen=True)
class ExtensionLifecycleHooks:
    on_initialize: ExtensionLifecycleHook | None = None
    on_startup: ExtensionLifecycleHook | None = None
    on_task_event: ExtensionTaskEventHook | None = None
    before_run: ExtensionLifecycleHook | None = None
    after_run: ExtensionLifecycleHook | None = None
    on_shutdown: ExtensionLifecycleHook | None = None


@dataclass(frozen=True)
class AgentExtension:
    """A trusted domain module composed into Enoch's application lifecycle."""

    name: str
    api_version: int = AGENT_EXTENSION_API_VERSION
    commands: tuple[ExtensionCommandSpec, ...] = ()
    lifecycle: ExtensionLifecycleHooks = field(default_factory=ExtensionLifecycleHooks)
    help_heading: str = ""

    def __post_init__(self) -> None:
        name = _extension_name(self.name)
        if self.api_version != AGENT_EXTENSION_API_VERSION:
            raise AgentExtensionError(
                f"Agent extension {name} uses API version {self.api_version}; "
                f"Enoch supports version {AGENT_EXTENSION_API_VERSION}."
            )
        heading = self.help_heading.strip() or f"Extension ({name})"
        if "\n" in heading or len(heading) > 80:
            raise AgentExtensionError(
                f"Agent extension {name} help heading must be one line and "
                "80 characters or fewer."
            )
        seen: set[str] = set()
        for spec in self.commands:
            if spec.name in seen:
                raise AgentExtensionError(
                    f"Duplicate agent extension command /{spec.name}."
                )
            seen.add(spec.name)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "help_heading", heading)

    def command(self, name: str) -> ExtensionCommandSpec | None:
        return next((spec for spec in self.commands if spec.matches(name)), None)


def extension_storage(storage: StorageLayout, name: str) -> StorageLayout:
    normalized = _extension_name(name)
    return StorageLayout(
        software_body=storage.software_body,
        private_state=storage.private_path(f"extensions/{normalized}"),
        artifacts=storage.artifact_path(f"extensions/{normalized}"),
    )


def _extension_name(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
        raise AgentExtensionError(f"Invalid agent extension name {value!r}.")
    return name


def _command_name(value: str) -> str:
    name = value.strip().lower().lstrip("/")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise AgentExtensionError(f"Invalid agent extension command {value!r}.")
    return name


def _result_task_ids(values: tuple[int, ...]) -> tuple[int, ...]:
    try:
        task_ids = tuple(values)
    except TypeError as error:
        raise AgentExtensionError(
            "Extension command result task IDs must be an iterable."
        ) from error
    if len(task_ids) > 64:
        raise AgentExtensionError(
            "Extension command results support at most 64 task IDs."
        )
    if any(
        isinstance(task_id, bool)
        or not isinstance(task_id, int)
        or task_id < 1
        for task_id in task_ids
    ):
        raise AgentExtensionError(
            "Extension command result task IDs must be positive integers."
        )
    if len(set(task_ids)) != len(task_ids):
        raise AgentExtensionError(
            "Extension command result task IDs must be unique."
        )
    return task_ids


def _result_output_refs(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise AgentExtensionError(
            "Extension command result output references must be an iterable "
            "of strings, not one string."
        )
    try:
        raw_refs = tuple(values)
    except TypeError as error:
        raise AgentExtensionError(
            "Extension command result output references must be an iterable."
        ) from error
    if any(not isinstance(value, str) for value in raw_refs):
        raise AgentExtensionError(
            "Extension command result output references must be strings."
        )
    output_refs = tuple(value.strip() for value in raw_refs)
    if len(output_refs) > 64:
        raise AgentExtensionError(
            "Extension command results support at most 64 output references."
        )
    if any(
        not value or "\n" in value or len(value) > 512
        for value in output_refs
    ):
        raise AgentExtensionError(
            "Extension command result output references must be non-empty, "
            "single-line strings of 512 characters or fewer."
        )
    if len(set(output_refs)) != len(output_refs):
        raise AgentExtensionError(
            "Extension command result output references must be unique."
        )
    return output_refs
