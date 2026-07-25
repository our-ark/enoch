from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from enoch.providers.contracts import ConversationId, MessageId, RuntimeResult
from enoch.tasks.queue import TaskJob, TaskQueueStatus


WORKFLOW_API_VERSION = 1
EnqueueMode = Literal["queued", "front", "direct"]
FinalTaskStatus = Literal["completed", "failed", "cancelled"]


class WorkflowEngineError(RuntimeError):
    pass


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
        **options: Any,
    ) -> TaskJob: ...

    def start_next(self) -> TaskJob | None: ...

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

    def inspect(self) -> TaskQueueStatus: ...

    def find(self, task_id: int) -> TaskJob | None: ...

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

    def retry_failed(self, task_id: int, **options: Any) -> TaskJob: ...

    def pause(self, task_id: int, **options: Any) -> TaskJob | None: ...

    def resume(self, **options: Any) -> tuple[TaskJob, ...]: ...

    def regress(self, task_id: int, **options: Any) -> TaskJob | None: ...

    def resolve_regression(
        self,
        task_id: int,
        resolution: str,
        **options: Any,
    ) -> TaskJob | None: ...

    def record_status_message(
        self,
        task_id: int,
        message_id: MessageId,
    ) -> None: ...

    def record_worktree(
        self,
        task_id: int,
        worker_id: str,
        worktree_path: Path,
        branch_name: str,
    ) -> TaskJob | None: ...

    def record_result(self, task_id: int, result: str) -> None: ...

    def record_publish_state(
        self,
        task_id: int,
        worker_id: str,
        **state: Any,
    ) -> TaskJob | None: ...

    def record_runtime_result(
        self,
        task_id: int,
        result: RuntimeResult,
        *,
        provider: str,
    ) -> None: ...

    def worker_is_active(self, job: TaskJob) -> bool: ...

    def result_has_pull_request(self, result: str) -> bool: ...


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
