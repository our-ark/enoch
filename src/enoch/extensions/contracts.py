from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Callable

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


DOMAIN_EXTENSION_API_VERSION = 1


class DomainExtensionError(RuntimeError):
    pass


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
            context_source=(
                f"extension:{self.extension_name}" if context.strip() else ""
            ),
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
class DomainCommandContext:
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
    ) -> TaskJob:
        return self.workflow.enqueue(
            self.conversation_id,
            request,
            context=context,
            initiated_by="human",
            event_actor="human",
            trigger=self.command,
            required_capabilities=required_capabilities,
            idempotency_key=f"command:{self.event.message_id}",
        )


DomainCommandHandler = Callable[[DomainCommandContext], str]


@dataclass(frozen=True)
class DomainCommandSpec:
    name: str
    summary: str
    handler: DomainCommandHandler
    usage: str = ""
    required_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = _command_name(self.name)
        summary = self.summary.strip()
        if not summary:
            raise DomainExtensionError(
                f"Domain extension command /{name} requires a summary."
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
        except DomainExtensionError:
            return False


@dataclass(frozen=True)
class DomainLifecycleContext:
    identity: Identity
    root: Path
    storage: StorageLayout
    chat: ChatProvider
    runtime: AgentRuntime
    repository: RepositoryProvider
    review: ReviewProvider
    workflow: ExtensionWorkflow


DomainLifecycleHook = Callable[[DomainLifecycleContext], None]


@dataclass(frozen=True)
class DomainLifecycleHooks:
    on_initialize: DomainLifecycleHook | None = None
    on_startup: DomainLifecycleHook | None = None
    before_run: DomainLifecycleHook | None = None
    after_run: DomainLifecycleHook | None = None
    on_shutdown: DomainLifecycleHook | None = None


@dataclass(frozen=True)
class DomainExtension:
    """A trusted domain module composed into Enoch's application lifecycle."""

    name: str
    api_version: int = DOMAIN_EXTENSION_API_VERSION
    commands: tuple[DomainCommandSpec, ...] = ()
    lifecycle: DomainLifecycleHooks = field(default_factory=DomainLifecycleHooks)
    help_heading: str = ""

    def __post_init__(self) -> None:
        name = _extension_name(self.name)
        if self.api_version != DOMAIN_EXTENSION_API_VERSION:
            raise DomainExtensionError(
                f"Domain extension {name} uses API version {self.api_version}; "
                f"Enoch supports version {DOMAIN_EXTENSION_API_VERSION}."
            )
        heading = self.help_heading.strip() or f"Extension ({name})"
        if "\n" in heading or len(heading) > 80:
            raise DomainExtensionError(
                f"Domain extension {name} help heading must be one line and "
                "80 characters or fewer."
            )
        seen: set[str] = set()
        for spec in self.commands:
            if spec.name in seen:
                raise DomainExtensionError(
                    f"Duplicate domain extension command /{spec.name}."
                )
            seen.add(spec.name)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "help_heading", heading)

    def command(self, name: str) -> DomainCommandSpec | None:
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
        raise DomainExtensionError(f"Invalid domain extension name {value!r}.")
    return name


def _command_name(value: str) -> str:
    name = value.strip().lower().lstrip("/")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise DomainExtensionError(f"Invalid domain extension command {value!r}.")
    return name
