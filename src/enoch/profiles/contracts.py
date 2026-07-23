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
    ForgeProvider,
)
from enoch.tasks.queue import TaskJob


PROFILE_API_VERSION = 1
PromptPurpose = Literal["conversation", "image", "task-context", "task"]
TaskEnqueuer = Callable[[str, str], TaskJob]


class ProfileError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromptContext:
    identity: Identity
    root: Path
    purpose: PromptPurpose
    conversation_id: ConversationId
    prompt: str


PromptContributor = Callable[[PromptContext], str]


@dataclass(frozen=True)
class CommandContext:
    identity: Identity
    root: Path
    conversation_id: ConversationId
    event: ChatEvent
    command: str
    argument: str
    runtime: AgentRuntime
    forge: ForgeProvider
    _enqueue: TaskEnqueuer = field(repr=False)

    def enqueue_task(self, request: str, *, context: str = "") -> TaskJob:
        """Queue human-requested work through Enoch's single task queue."""
        return self._enqueue(request, context)


CommandHandler = Callable[[CommandContext], str]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    summary: str
    handler: CommandHandler
    usage: str = ""
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        name = _command_name(self.name)
        aliases = tuple(_command_name(alias) for alias in self.aliases)
        if name in aliases or len(set(aliases)) != len(aliases):
            raise ProfileError(f"Profile command /{name} has duplicate aliases.")
        if not self.summary.strip():
            raise ProfileError(f"Profile command /{name} requires a summary.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "summary", self.summary.strip())
        object.__setattr__(self, "usage", self.usage.strip())
        object.__setattr__(self, "aliases", aliases)

    @property
    def command(self) -> str:
        return f"/{self.name}"

    def matches(self, command: str) -> bool:
        try:
            normalized = _command_name(command)
        except ProfileError:
            return False
        return normalized == self.name or normalized in self.aliases


@dataclass(frozen=True)
class LifecycleContext:
    identity: Identity
    root: Path
    chat: ChatProvider
    runtime: AgentRuntime
    forge: ForgeProvider


LifecycleHook = Callable[[LifecycleContext], None]


@dataclass(frozen=True)
class LifecycleHooks:
    on_initialize: LifecycleHook | None = None
    on_startup: LifecycleHook | None = None
    before_run: LifecycleHook | None = None
    after_run: LifecycleHook | None = None
    on_shutdown: LifecycleHook | None = None


@dataclass(frozen=True)
class WorkflowPolicy:
    """Profile-owned defaults for work submitted to the shared task queue."""

    timeout_seconds: int | None = None
    max_attempts: int | None = None
    allow_direct_work: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, int)
            or self.timeout_seconds <= 0
        ):
            raise ProfileError("Profile workflow timeout must be a positive integer.")
        if self.max_attempts is not None and (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 100
        ):
            raise ProfileError(
                "Profile workflow max attempts must be between 1 and 100."
            )
        if not isinstance(self.allow_direct_work, bool):
            raise ProfileError("Profile workflow allow direct work must be boolean.")

    def task_options(self) -> dict[str, int]:
        options: dict[str, int] = {}
        if self.timeout_seconds is not None:
            options["timeout_seconds"] = self.timeout_seconds
        if self.max_attempts is not None:
            options["max_attempts"] = self.max_attempts
        return options


@dataclass(frozen=True)
class ProfilePresentation:
    """Bounded human-facing labels owned by a downstream profile."""

    display_name: str = ""
    help_heading: str = ""
    task_label: str = "Task"

    def __post_init__(self) -> None:
        for field_name in ("display_name", "help_heading", "task_label"):
            value = str(getattr(self, field_name)).strip()
            if "\n" in value:
                raise ProfileError(
                    f"Profile presentation {field_name.replace('_', ' ')} "
                    "must be a single line."
                )
            if len(value) > 80:
                raise ProfileError(
                    f"Profile presentation {field_name.replace('_', ' ')} "
                    "must be 80 characters or fewer."
                )
            object.__setattr__(self, field_name, value)
        if not self.task_label:
            raise ProfileError("Profile presentation task label is required.")

    def resolved_display_name(self, profile_name: str) -> str:
        return self.display_name or profile_name

    def resolved_help_heading(self, profile_name: str) -> str:
        return self.help_heading or (
            f"Profile ({self.resolved_display_name(profile_name)})"
        )


@dataclass(frozen=True)
class AgentProfile:
    name: str
    api_version: int = PROFILE_API_VERSION
    commands: tuple[CommandSpec, ...] = ()
    prompt_contributors: tuple[PromptContributor, ...] = ()
    workflow: WorkflowPolicy = field(default_factory=WorkflowPolicy)
    presentation: ProfilePresentation = field(default_factory=ProfilePresentation)
    lifecycle: LifecycleHooks = field(default_factory=LifecycleHooks)

    def __post_init__(self) -> None:
        name = self.name.strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", name):
            raise ProfileError(f"Invalid profile name {self.name!r}.")
        if self.api_version != PROFILE_API_VERSION:
            raise ProfileError(
                f"Profile {name} uses API version {self.api_version}; "
                f"Enoch supports version {PROFILE_API_VERSION}."
            )
        seen: set[str] = set()
        for spec in self.commands:
            for command_name in (spec.name, *spec.aliases):
                if command_name in seen:
                    raise ProfileError(f"Duplicate profile command /{command_name}.")
                seen.add(command_name)
        object.__setattr__(self, "name", name)

    @property
    def display_name(self) -> str:
        return self.presentation.resolved_display_name(self.name)

    @property
    def help_heading(self) -> str:
        return self.presentation.resolved_help_heading(self.name)

    def command(self, name: str) -> CommandSpec | None:
        candidate = name.strip().split(maxsplit=1)[0] if name.strip() else ""
        try:
            normalized = _command_name(candidate)
        except ProfileError:
            return None
        return next(
            (
                spec
                for spec in self.commands
                if normalized == spec.name or normalized in spec.aliases
            ),
            None,
        )


def extend_prompt(prompt: str, profile: AgentProfile, context: PromptContext) -> str:
    sections: list[str] = []
    for contributor in profile.prompt_contributors:
        try:
            contribution = contributor(context).strip()
        except Exception as error:
            raise ProfileError(
                f"Profile {profile.name} prompt contributor failed: {error}"
            ) from error
        if contribution:
            sections.append(contribution)
    if not sections:
        return prompt
    return "\n\n".join([prompt.rstrip(), "Profile context:\n" + "\n\n".join(sections)])


def _command_name(value: str) -> str:
    name = value.strip().lower().lstrip("/")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        raise ProfileError(f"Invalid profile command {value!r}.")
    return name
