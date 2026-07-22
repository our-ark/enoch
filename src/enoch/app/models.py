from __future__ import annotations

from dataclasses import dataclass, field
import threading

from enoch.providers.contracts import ConversationId, MessageId


class ShutdownRequested(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class WorkStatusMessage:
    chat_id: ConversationId
    message_id: MessageId
    request: str
    started_at: float
    task_id: int | None = None
    status: str = "queued"
    latest_update: str = "Queued."
    prs: list[str] = field(default_factory=list)
    context: str = ""


@dataclass(frozen=True)
class ForgeMaintenanceRequest:
    close_numbers: tuple[int, ...]
    keep_number: int | None = None


@dataclass(frozen=True)
class TaskContextSnapshot:
    context: str = ""
    source: str = ""
    clarification: str = ""
    error: str = ""
    codex_unavailable_reason: str = ""


@dataclass
class TaskDeadline:
    timeout_seconds: int
    cancellation_event: threading.Event
    expired: threading.Event = field(default_factory=threading.Event)
    timer: threading.Timer | None = None

    def start(self) -> None:
        self.timer = threading.Timer(self.timeout_seconds, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def cancel(self) -> None:
        if self.timer is not None:
            self.timer.cancel()

    def _expire(self) -> None:
        self.expired.set()
        self.cancellation_event.set()
