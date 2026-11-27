"""Versioned contracts for hosts embedding Enoch's existing application."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from enoch.app.epoch import StaleDaemonEpoch
from enoch.providers import ConversationId


EMBEDDING_API_VERSION = 1


class EmbeddingConversationDenied(PermissionError):
    """The host supplied a conversation outside the configured chat boundary."""


@dataclass(frozen=True)
class EvolveCommandResult:
    conversation_id: ConversationId
    reply: str


PendingTaskStopReason = Literal[
    "idle", "limit", "paused", "busy", "stopping", "restart", "update", "review",
]


@dataclass(frozen=True)
class PendingTaskExecutionResult:
    """Task IDs attempted by this call, not a claim that they succeeded."""

    task_ids: tuple[int, ...]
    stop_reason: PendingTaskStopReason


@runtime_checkable
class EmbeddedApplication(Protocol):
    embedding_api_version: int

    def handle_evolve_command(
        self, conversation_id: ConversationId, argument: str = "",
    ) -> EvolveCommandResult: ...

    def resume_pending_tasks(
        self, *, max_tasks: int = 1, promote_backlog: bool = False,
        reconcile_schedules: bool = False,
    ) -> PendingTaskExecutionResult: ...


__all__ = [
    "EMBEDDING_API_VERSION", "EmbeddedApplication", "EmbeddingConversationDenied",
    "EvolveCommandResult", "PendingTaskExecutionResult", "PendingTaskStopReason",
    "StaleDaemonEpoch",
]
