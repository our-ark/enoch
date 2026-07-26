from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Literal

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
    reviews: list[str] = field(default_factory=list)
    context: str = ""

    @property
    def prs(self) -> list[str]:
        return self.reviews

    @prs.setter
    def prs(self, value: list[str]) -> None:
        self.reviews = value


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


WorkOutcomeStatus = Literal["completed", "failed", "publish_incomplete"]


@dataclass(frozen=True)
class WorkOutcome:
    """Structured task result; presentation text is never used as control flow."""

    status: WorkOutcomeStatus
    message: str
    code: str = ""
    failure_class: str = ""
    retryable: bool = False
    completed_stages: tuple[str, ...] = ()
    revision_id: str = ""
    workspace_id: str = ""
    review_id: str = ""
    review_url: str = ""

    @property
    def failed(self) -> bool:
        return self.status != "completed"

    @property
    def commit_sha(self) -> str:
        return self.revision_id

    @property
    def remote_branch(self) -> str:
        return self.workspace_id

    @property
    def pr_url(self) -> str:
        return self.review_url

    def __str__(self) -> str:
        return self.message

    def __contains__(self, value: object) -> bool:
        return isinstance(value, str) and value in self.message

    @classmethod
    def completed(
        cls,
        message: str,
        *,
        completed_stages: tuple[str, ...] = (),
        revision_id: str = "",
        workspace_id: str = "",
        review_id: str = "",
        review_url: str = "",
    ) -> WorkOutcome:
        return cls(
            status="completed",
            message=message,
            completed_stages=completed_stages,
            revision_id=revision_id,
            workspace_id=workspace_id,
            review_id=review_id,
            review_url=review_url,
        )

    @classmethod
    def failure(
        cls,
        message: str,
        *,
        code: str,
        failure_class: str = "permanent",
        retryable: bool = False,
        status: WorkOutcomeStatus = "failed",
        completed_stages: tuple[str, ...] = (),
        revision_id: str = "",
        workspace_id: str = "",
        review_id: str = "",
        review_url: str = "",
    ) -> WorkOutcome:
        return cls(
            status=status,
            message=message,
            code=code,
            failure_class=failure_class,
            retryable=retryable,
            completed_stages=completed_stages,
            revision_id=revision_id,
            workspace_id=workspace_id,
            review_id=review_id,
            review_url=review_url,
        )
