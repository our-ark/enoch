from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import Any

from enoch.app.epoch import DaemonEpoch, daemon_epoch_guard
from enoch.providers.contracts import ConversationId, MessageId, RuntimeResult
from enoch.tasks.queue import (
    TaskJob,
    TaskQueueStatus,
    begin_direct_task,
    begin_next_task,
    cancel_running_task,
    cancel_task,
    claim_running_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    fail_task,
    heartbeat_task,
    pause_task,
    record_task_publish_state,
    record_task_result,
    record_task_runtime_result,
    record_task_status_message,
    record_task_worktree,
    recover_interrupted_task,
    regress_task,
    resolve_regressed_task,
    resume_paused_tasks,
    retry_failed_task,
    retry_running_task,
    task_queue_status,
    task_result_has_pull_request,
    task_worker_is_active,
)
from enoch.workflows.contracts import (
    WORKFLOW_API_VERSION,
    EnqueueMode,
    FinalTaskStatus,
)


class LocalWorkflowEngine:
    """File-backed single-owner workflow engine used by Enoch."""

    api_version = WORKFLOW_API_VERSION

    def __init__(self, root: Path, *, epoch: DaemonEpoch | None = None) -> None:
        self.root = root
        self.epoch = epoch

    def enqueue(
        self,
        conversation_id: ConversationId,
        request: str,
        *,
        mode: EnqueueMode = "queued",
        **options: Any,
    ) -> TaskJob:
        function = {
            "queued": enqueue_task,
            "front": enqueue_task_front,
            "direct": begin_direct_task,
        }.get(mode)
        if function is None:
            raise ValueError(f"Unknown workflow enqueue mode {mode!r}.")
        with self._mutation():
            return function(conversation_id, request, self.root, **options)

    def start_next(self) -> TaskJob | None:
        with self._mutation():
            return begin_next_task(self.root)

    def claim(
        self,
        task_id: int,
        worker_id: str,
        worker_pid: int,
    ) -> TaskJob | None:
        with self._mutation():
            return claim_running_task(task_id, worker_id, worker_pid, self.root)

    def heartbeat(self, task_id: int, worker_id: str) -> TaskJob | None:
        with self._mutation():
            return heartbeat_task(task_id, worker_id, self.root)

    def cancel(
        self,
        task_id: int | None,
        *,
        result: str = "",
        event_actor: str = "human",
        trigger: str = "/task cancel",
        worker_id: str = "",
    ) -> TaskJob | None:
        with self._mutation():
            running = task_queue_status(self.root).running
            if running is not None and (task_id is None or running.id == task_id):
                return cancel_running_task(
                    self.root,
                    result=result or "Stopped by /stop.",
                    event_actor=event_actor,
                    trigger=trigger,
                    expected_task_id=task_id,
                    worker_id=worker_id,
                )
            if task_id is None:
                return None
            return cancel_task(
                task_id,
                self.root,
                event_actor=event_actor,
                trigger=trigger,
            )

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
    ) -> TaskJob | None:
        with self._mutation():
            if status == "completed":
                return complete_task(
                    task_id,
                    self.root,
                    result,
                    event_actor=event_actor,
                    trigger=trigger,
                    worker_id=worker_id,
                )
            if status == "failed":
                return fail_task(
                    task_id,
                    self.root,
                    result,
                    event_actor=event_actor,
                    trigger=trigger,
                    worker_id=worker_id,
                    failure_code=failure_code,
                    failure_class=failure_class,
                    retryable=retryable,
                )
            if status == "cancelled":
                return cancel_running_task(
                    self.root,
                    result=result,
                    event_actor=event_actor,
                    trigger=trigger,
                    expected_task_id=task_id,
                    worker_id=worker_id,
                )
        raise ValueError(f"Unknown final task status {status!r}.")

    def recover(self) -> TaskJob | None:
        with self._mutation():
            return recover_interrupted_task(self.root)

    def inspect(self) -> TaskQueueStatus:
        return task_queue_status(self.root)

    def find(self, task_id: int) -> TaskJob | None:
        status = self.inspect()
        return next(
            (
                job
                for job in [
                    *status.pending,
                    *status.paused,
                    *([status.running] if status.running is not None else []),
                    *status.history,
                ]
                if job.id == task_id
            ),
            None,
        )

    def retry_running(self, task_id: int, **options: Any) -> TaskJob | None:
        with self._mutation():
            return retry_running_task(task_id, self.root, **options)

    def retry_failed(self, task_id: int, **options: Any) -> TaskJob:
        with self._mutation():
            return retry_failed_task(task_id, self.root, **options)

    def pause(self, task_id: int, **options: Any) -> TaskJob | None:
        with self._mutation():
            return pause_task(task_id, self.root, **options)

    def resume(self, **options: Any) -> tuple[TaskJob, ...]:
        with self._mutation():
            return resume_paused_tasks(self.root, **options)

    def regress(self, task_id: int, **options: Any) -> TaskJob | None:
        with self._mutation():
            return regress_task(task_id, self.root, **options)

    def resolve_regression(
        self,
        task_id: int,
        resolution: str,
        **options: Any,
    ) -> TaskJob | None:
        with self._mutation():
            return resolve_regressed_task(
                task_id,
                resolution,
                self.root,
                **options,
            )

    def record_status_message(self, task_id: int, message_id: MessageId) -> None:
        with self._mutation():
            record_task_status_message(task_id, message_id, self.root)

    def record_worktree(
        self,
        task_id: int,
        worker_id: str,
        worktree_path: Path,
        branch_name: str,
    ) -> TaskJob | None:
        with self._mutation():
            return record_task_worktree(
                task_id,
                worker_id,
                worktree_path,
                branch_name,
                self.root,
            )

    def record_result(self, task_id: int, result: str) -> None:
        with self._mutation():
            record_task_result(task_id, result, self.root)

    def record_publish_state(
        self,
        task_id: int,
        worker_id: str,
        **state: Any,
    ) -> TaskJob | None:
        with self._mutation():
            return record_task_publish_state(
                task_id,
                worker_id,
                self.root,
                **state,
            )

    def record_runtime_result(
        self,
        task_id: int,
        result: RuntimeResult,
        *,
        provider: str,
    ) -> None:
        with self._mutation():
            record_task_runtime_result(
                task_id,
                result,
                self.root,
                provider=provider,
            )

    def worker_is_active(self, job: TaskJob) -> bool:
        return task_worker_is_active(job)

    def result_has_pull_request(self, result: str) -> bool:
        return task_result_has_pull_request(result)

    def _mutation(self):
        if self.epoch is None:
            return nullcontext()
        return daemon_epoch_guard(self.epoch, self.root)
