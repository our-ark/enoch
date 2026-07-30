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
from enoch.storage import StorageLayout
from enoch.tasks.queue import TaskJob, TaskQueueStatus
from enoch.workflows import WorkflowEngine


AGENT_EXTENSION_API_VERSION = 1
ExtensionTaskEventType = Literal[
    "queued",
    "started",
    "completed",
    "failed",
    "cancelled",
]


class AgentExtensionError(RuntimeError):
    pass


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
    _task_options: dict[str, object] = field(default_factory=dict, repr=False)

    @classmethod
    def from_engine(
        cls,
        extension_name: str,
        engine: WorkflowEngine,
        *,
        task_options: dict[str, object] | None = None,
    ) -> ExtensionWorkflow:
        return cls(
            extension_name=extension_name,
            _enqueue=engine.enqueue,
            _inspect=engine.inspect,
            _find=engine.find,
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


ExtensionCommandHandler = Callable[[ExtensionCommandContext], str]


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
