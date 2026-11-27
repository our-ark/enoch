from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from enoch.providers.contracts import ConversationId, MessageId, RuntimeResult
from enoch.tasks.queue import (
    TaskJob,
    TaskPublicationState,
    TaskQueueStatus,
    TaskReconciliationRequest,
    TaskReconciliationResult,
    TaskTerminalEvidence,
)
from enoch.tasks.payloads import ExtensionArtifactReference, JsonValue
from enoch.tasks.recovery import ExtensionRecoveryHook


WORKFLOW_API_VERSION = 4
WORKFLOW_FEATURE_STRUCTURED_METADATA = "structured_task_metadata"
WORKFLOW_FEATURE_ARTIFACT_REFERENCES = "artifact_references"
WORKFLOW_FEATURE_EXECUTION_LANES = "execution_lanes"
WORKFLOW_FEATURE_EXTENSION_RECOVERY = "extension_recovery"
EnqueueMode = Literal["queued", "front", "direct"]
FinalTaskStatus = Literal["completed", "failed", "cancelled"]


class WorkflowEngineError(RuntimeError):
    pass


@runtime_checkable
class ExtensionRecoveryWorkflow(Protocol):
    """Optional workflow feature, independent of the base engine API version."""

    def configure_extension_recovery(
        self, hooks: dict[str, ExtensionRecoveryHook], *, timeout_seconds: float = 5.0,
    ) -> None: ...


@runtime_checkable
class EndedTaskReviewWorkflow(Protocol):
    """Optional: attribute a review to a task that already ended unfinished.

    Enoch calls it when a timed-out, failed or stopped task turns out to have
    opened a review from its own branch. Engines without it still work; the
    review is then reported and watched but not stored on the task.
    """

    def record_ended_task_reviews(
        self,
        task_id: int,
        review_urls: tuple[str, ...],
    ) -> TaskJob | None: ...


@runtime_checkable
class WorkflowEngine(Protocol):
    """Versioned single-owner task lifecycle contract."""

    api_version: int
    root: Path

    def enqueue(
        self,
        conversation_id: ConversationId,
        request: str,
        *,
        mode: EnqueueMode = "queued",
        thread_id: MessageId | None = None,
        context: str = "",
        context_source: str = "",
        source: str = "",
        initiated_by: str = "human",
        event_actor: str = "human",
        trigger: str = "",
        candidate_id: str = "",
        parent_task_id: int | None = None,
        evidence_source: str = "",
        signal_actor: str = "",
        candidate_actor: str = "",
        approval_actor: str = "",
        parent_candidate_id: str = "",
        source_task_id: int | None = None,
        max_attempts: int = 3,
        timeout_seconds: int | None = None,
        required_capabilities: tuple[str, ...] = (),
        idempotency_key: str = "",
        extension_metadata: dict[str, JsonValue] | None = None,
        extension_artifact_refs: tuple[ExtensionArtifactReference, ...] = (),
        execution_lane: str = "",
        merge_policy: str = "",
    ) -> TaskJob: ...

    def start_next(
        self,
        *,
        triggers: frozenset[str] | None = None,
    ) -> TaskJob | None: ...

    def claim(
        self,
        task_id: int,
        worker_id: str,
        worker_pid: int,
    ) -> TaskJob | None: ...

    def heartbeat(self, task_id: int, worker_id: str) -> TaskJob | None: ...

    def cancel(
        self,
        task_id: int | None,
        *,
        result: str = "",
        event_actor: str = "human",
        trigger: str = "/task cancel",
        worker_id: str = "",
    ) -> TaskJob | None: ...

    def finalize(
        self,
        task_id: int,
        status: FinalTaskStatus,
        *,
        result: str = "",
        event_actor: str = "agent",
        trigger: str = "task-runner",
        worker_id: str = "",
        failure_code: str = "",
        failure_class: str = "",
        retryable: bool = False,
    ) -> TaskJob | None: ...

    def recover(self) -> TaskJob | None: ...

    def reconcile(
        self,
        request: TaskReconciliationRequest | None = None,
    ) -> TaskReconciliationResult: ...

    def inspect(self) -> TaskQueueStatus: ...

    def find(self, task_id: int) -> TaskJob | None: ...

    def find_by_message(
        self,
        message_id: MessageId,
        *,
        chat_id: ConversationId | None = None,
    ) -> TaskJob | None: ...

    def retry_running(
        self,
        task_id: int,
        *,
        result: str,
        failure_code: str,
        failure_class: str,
        worker_id: str = "",
        delay_seconds: int = 0,
        event_actor: str = "agent",
        trigger: str = "task-runner",
    ) -> TaskJob | None: ...

    def retry_failed(
        self,
        task_id: int,
        *,
        reconciled_result: str = "",
        # Optional: reviews confirmed to belong to the original task. Enoch
        # passes it only when there are some, so engines without it still work.
        reconciled_review_urls: tuple[str, ...] = (),
        event_actor: str = "human",
        trigger: str = "/task retry",
    ) -> TaskJob: ...

    def pause(
        self,
        task_id: int,
        *,
        result: str = "",
        event_actor: str = "system",
        trigger: str = "runtime-unavailable",
        worker_id: str = "",
    ) -> TaskJob | None: ...

    def resume(
        self,
        *,
        task_id: int | None = None,
        event_actor: str = "human",
        trigger: str = "/task resume",
    ) -> tuple[TaskJob, ...]: ...

    def regress(
        self,
        task_id: int,
        *,
        result: str = "",
        event_actor: str = "agent",
        trigger: str = "agent-regression-signal",
    ) -> TaskJob | None: ...

    def resolve_regression(
        self,
        task_id: int,
        resolution: str,
        *,
        result: str = "",
        event_actor: str = "agent",
        trigger: str = "agent-regression-signal",
        related_task_id: int | None = None,
    ) -> TaskJob | None: ...

    def record_status_message(
        self,
        task_id: int,
        message_id: MessageId,
    ) -> None: ...

    def record_result_message(
        self,
        task_id: int,
        message_id: MessageId,
    ) -> None: ...

    def record_workspace(
        self,
        task_id: int,
        worker_id: str,
        workspace_path: Path,
        workspace_id: str,
    ) -> TaskJob | None: ...

    def record_result(self, task_id: int, result: str) -> None: ...

    def record_publication(
        self,
        task_id: int,
        worker_id: str,
        state: TaskPublicationState,
    ) -> TaskJob | None: ...

    def record_runtime_result(
        self,
        task_id: int,
        result: RuntimeResult,
        *,
        provider: str,
    ) -> None: ...

    def record_terminal_evidence(
        self,
        task_id: int,
        worker_id: str,
        evidence: TaskTerminalEvidence,
    ) -> TaskJob | None: ...

    def worker_is_active(self, job: TaskJob) -> bool: ...

def validate_workflow_engine(engine: WorkflowEngine) -> WorkflowEngine:
    if not isinstance(engine, WorkflowEngine):
        raise WorkflowEngineError(
            "Workflow engine does not implement the versioned WorkflowEngine contract."
        )
    if engine.api_version != WORKFLOW_API_VERSION:
        raise WorkflowEngineError(
            f"Workflow engine uses API version {engine.api_version}; "
            f"Enoch supports version {WORKFLOW_API_VERSION}."
        )
    return engine


def workflow_features(engine: WorkflowEngine) -> frozenset[str]:
    """Discover optional workflow features without breaking API v4 engines."""

    raw = getattr(engine, "features", ())
    if isinstance(raw, str):
        return frozenset()
    try:
        values = tuple(raw)
    except TypeError:
        return frozenset()
    return frozenset(
        value.strip().lower()
        for value in values
        if isinstance(value, str) and value.strip()
    )
