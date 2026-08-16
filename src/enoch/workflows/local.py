from __future__ import annotations

from contextlib import nullcontext
import os
from pathlib import Path
import threading
from typing import Callable

from enoch.app.epoch import DaemonEpoch, daemon_epoch_guard
from enoch.memory.paths import now as current_time
from enoch.providers.contracts import ConversationId, MessageId, RuntimeResult
from enoch.tasks.queue import (
    TaskJob,
    TaskPublicationState,
    TaskQueueStatus,
    TaskReconciliationRequest,
    TaskReconciliationResult,
    TaskTerminalEvidence,
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
    record_task_publication,
    record_task_result,
    record_task_runtime_result,
    record_task_status_message,
    record_task_terminal_evidence,
    record_task_workspace,
    reconcile_running_task,
    regress_task,
    resolve_regressed_task,
    resume_paused_tasks,
    retry_failed_task,
    retry_running_task,
    task_queue_status,
    task_result_has_review,
    task_worker_is_active,
)
from enoch.tasks.payloads import ExtensionArtifactReference, JsonValue
from enoch.workflows.contracts import (
    WORKFLOW_API_VERSION,
    WORKFLOW_FEATURE_ARTIFACT_REFERENCES,
    WORKFLOW_FEATURE_EXECUTION_LANES,
    WORKFLOW_FEATURE_STRUCTURED_METADATA,
    EnqueueMode,
    FinalTaskStatus,
)


class LocalWorkflowEngine:
    """File-backed single-owner workflow engine used by Enoch."""

    api_version = WORKFLOW_API_VERSION
    features = frozenset(
        {
            WORKFLOW_FEATURE_ARTIFACT_REFERENCES,
            WORKFLOW_FEATURE_EXECUTION_LANES,
            WORKFLOW_FEATURE_STRUCTURED_METADATA,
        }
    )

    def __init__(
        self,
        root: Path,
        *,
        epoch: DaemonEpoch | None = None,
        clock: Callable[[], str] = current_time,
        worker_liveness: Callable[[TaskJob], bool] = task_worker_is_active,
    ) -> None:
        self.root = root
        self.epoch = epoch
        self._clock = clock
        self._worker_liveness = worker_liveness
        self._worker_threads: dict[str, threading.Thread] = {}
        self._worker_threads_lock = threading.Lock()

    def enqueue(
        self,
        conversation_id: ConversationId,
        request: str,
        *,
        mode: EnqueueMode = "queued",
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
    ) -> TaskJob:
        function = {
            "queued": enqueue_task,
            "front": enqueue_task_front,
            "direct": begin_direct_task,
        }.get(mode)
        if function is None:
            raise ValueError(f"Unknown workflow enqueue mode {mode!r}.")
        resolved_source = source or (
            "task" if mode == "queued" else "chat-task"
        )
        resolved_trigger = trigger or (
            "/task" if mode == "queued" else "/do"
        )
        with self._mutation():
            return function(
                conversation_id,
                request,
                self.root,
                context=context,
                context_source=context_source,
                source=resolved_source,
                initiated_by=initiated_by,
                event_actor=event_actor,
                trigger=resolved_trigger,
                candidate_id=candidate_id,
                parent_task_id=parent_task_id,
                evidence_source=evidence_source,
                signal_actor=signal_actor,
                candidate_actor=candidate_actor,
                approval_actor=approval_actor,
                parent_candidate_id=parent_candidate_id,
                source_task_id=source_task_id,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                required_capabilities=required_capabilities,
                idempotency_key=idempotency_key,
                extension_metadata=extension_metadata,
                extension_artifact_refs=extension_artifact_refs,
                execution_lane=execution_lane,
            )

    def start_next(self) -> TaskJob | None:
        with self._mutation():
            return begin_next_task(self.root)

    def claim(
        self,
        task_id: int,
        worker_id: str,
        worker_pid: int,
    ) -> TaskJob | None:
        self._remember_worker_thread(worker_id)
        try:
            with self._mutation():
                claimed = claim_running_task(
                    task_id,
                    worker_id,
                    worker_pid,
                    self.root,
                )
        except BaseException:
            self._forget_worker_thread(worker_id)
            raise
        if claimed is None:
            self._forget_worker_thread(worker_id)
        return claimed

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
            result = reconcile_running_task(
                root=self.root,
                worker_liveness=self._tracked_worker_is_active,
                checked_at=self._clock(),
            )
        if result.outcome not in {
            "terminal_repair",
            "interrupted_worker_recovery",
        } or result.task_id is None:
            return None
        return self.find(result.task_id)

    def reconcile(
        self,
        request: TaskReconciliationRequest | None = None,
    ) -> TaskReconciliationResult:
        with self._mutation():
            return reconcile_running_task(
                request,
                self.root,
                worker_liveness=self._tracked_worker_is_active,
                checked_at=self._clock(),
            )

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
    ) -> TaskJob | None:
        with self._mutation():
            return retry_running_task(
                task_id,
                self.root,
                result,
                failure_code=failure_code,
                failure_class=failure_class,
                worker_id=worker_id,
                delay_seconds=delay_seconds,
                event_actor=event_actor,
                trigger=trigger,
            )

    def retry_failed(
        self,
        task_id: int,
        *,
        reconciled_result: str = "",
        event_actor: str = "human",
        trigger: str = "/task retry",
    ) -> TaskJob:
        with self._mutation():
            return retry_failed_task(
                task_id,
                self.root,
                reconciled_result=reconciled_result,
                event_actor=event_actor,
                trigger=trigger,
            )

    def pause(
        self,
        task_id: int,
        *,
        result: str = "",
        event_actor: str = "system",
        trigger: str = "runtime-unavailable",
        worker_id: str = "",
    ) -> TaskJob | None:
        with self._mutation():
            return pause_task(
                task_id,
                self.root,
                result,
                event_actor=event_actor,
                trigger=trigger,
                worker_id=worker_id,
            )

    def resume(
        self,
        *,
        task_id: int | None = None,
        event_actor: str = "human",
        trigger: str = "/task resume",
    ) -> tuple[TaskJob, ...]:
        with self._mutation():
            return resume_paused_tasks(
                self.root,
                task_id=task_id,
                event_actor=event_actor,
                trigger=trigger,
            )

    def regress(
        self,
        task_id: int,
        *,
        result: str = "",
        event_actor: str = "agent",
        trigger: str = "agent-regression-signal",
    ) -> TaskJob | None:
        with self._mutation():
            return regress_task(
                task_id,
                self.root,
                result,
                event_actor=event_actor,
                trigger=trigger,
            )

    def resolve_regression(
        self,
        task_id: int,
        resolution: str,
        *,
        result: str = "",
        event_actor: str = "agent",
        trigger: str = "agent-regression-signal",
        related_task_id: int | None = None,
    ) -> TaskJob | None:
        with self._mutation():
            return resolve_regressed_task(
                task_id,
                resolution,
                self.root,
                result,
                event_actor=event_actor,
                trigger=trigger,
                related_task_id=related_task_id,
            )

    def record_status_message(self, task_id: int, message_id: MessageId) -> None:
        with self._mutation():
            record_task_status_message(task_id, message_id, self.root)

    def record_workspace(
        self,
        task_id: int,
        worker_id: str,
        workspace_path: Path,
        workspace_id: str,
    ) -> TaskJob | None:
        with self._mutation():
            return record_task_workspace(
                task_id,
                worker_id,
                workspace_path,
                workspace_id,
                self.root,
            )

    def record_worktree(
        self,
        task_id: int,
        worker_id: str,
        worktree_path: Path,
        branch_name: str,
    ) -> TaskJob | None:
        """Compatibility adapter for workflow API v1 callers."""

        return self.record_workspace(
            task_id,
            worker_id,
            worktree_path,
            branch_name,
        )

    def record_result(self, task_id: int, result: str) -> None:
        with self._mutation():
            record_task_result(task_id, result, self.root)

    def record_publication(
        self,
        task_id: int,
        worker_id: str,
        state: TaskPublicationState,
    ) -> TaskJob | None:
        with self._mutation():
            return record_task_publication(
                task_id,
                worker_id,
                state,
                self.root,
            )

    def record_publish_state(
        self,
        task_id: int,
        worker_id: str,
        *,
        stage: str,
        commit_sha: str = "",
        remote_branch: str = "",
        pr_url: str = "",
        published_remotely: bool | None = None,
    ) -> TaskJob | None:
        """Compatibility adapter for workflow API v1 callers."""

        return self.record_publication(
            task_id,
            worker_id,
            TaskPublicationState(
                stage=stage,
                revision_id=commit_sha,
                workspace_id=remote_branch,
                review_url=pr_url,
                review_published=published_remotely,
            ),
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

    def record_terminal_evidence(
        self,
        task_id: int,
        worker_id: str,
        evidence: TaskTerminalEvidence,
    ) -> TaskJob | None:
        with self._mutation():
            return record_task_terminal_evidence(
                task_id,
                worker_id,
                evidence,
                self.root,
            )

    def worker_is_active(self, job: TaskJob) -> bool:
        return self._tracked_worker_is_active(job)

    def _remember_worker_thread(self, worker_id: str) -> None:
        cleaned_worker_id = worker_id.strip()
        if not cleaned_worker_id:
            return
        current_worker = threading.current_thread()
        with self._worker_threads_lock:
            self._worker_threads = {
                key: thread
                for key, thread in self._worker_threads.items()
                if thread.is_alive() and thread is not current_worker
            }
            self._worker_threads[cleaned_worker_id] = current_worker

    def _forget_worker_thread(self, worker_id: str) -> None:
        with self._worker_threads_lock:
            self._worker_threads.pop(worker_id.strip(), None)

    def _tracked_worker_is_active(self, job: TaskJob) -> bool:
        if job.worker_pid == os.getpid():
            with self._worker_threads_lock:
                worker = self._worker_threads.get(job.worker_id)
                active = worker is not None and worker.is_alive()
                if worker is not None and not active:
                    self._worker_threads.pop(job.worker_id, None)
            return active
        return self._worker_liveness(job)

    def result_has_review(self, result: str) -> bool:
        return task_result_has_review(result)

    def result_has_pull_request(self, result: str) -> bool:
        """Compatibility adapter for workflow API v1 callers."""

        return self.result_has_review(result)

    def _mutation(self):
        if self.epoch is None:
            return nullcontext()
        return daemon_epoch_guard(self.epoch, self.root)
