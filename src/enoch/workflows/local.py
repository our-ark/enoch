from __future__ import annotations

from contextlib import nullcontext
import math
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
    find_task_by_message,
    heartbeat_task,
    pause_task,
    record_task_publication,
    record_task_result,
    record_task_result_message,
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
    prepare_extension_recovery,
    apply_extension_recovery,
    declare_completion_gate,
    append_completion_evidence,
    completion_snapshots,
    completion_gate_met,
    apply_completion_decision,
)
from enoch.tasks.completion import CompletionGate, CompletionHook, CompletionDecision, gate_time, invoke_completion_hook
from enoch.tasks.recovery import ExtensionRecoveryHook, ExtensionRecoverySnapshot, invoke_recovery_hook
from enoch.tasks.payloads import ExtensionArtifactReference, JsonValue
from enoch.workflows.contracts import (
    WORKFLOW_API_VERSION,
    WORKFLOW_FEATURE_ARTIFACT_REFERENCES,
    WORKFLOW_FEATURE_EXECUTION_LANES,
    WORKFLOW_FEATURE_STRUCTURED_METADATA,
    WORKFLOW_FEATURE_EXTENSION_RECOVERY,
    WORKFLOW_FEATURE_COMPLETION_GATES,
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
            WORKFLOW_FEATURE_EXTENSION_RECOVERY,
            WORKFLOW_FEATURE_COMPLETION_GATES,
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
        self._recovery_hooks: dict[str, ExtensionRecoveryHook] = {}
        self._recovery_timeout = 5.0
        self._completion_hooks: dict[str, CompletionHook] = {}
        self._completion_timeout = 5.0
        self._completion_lock = threading.Lock()

    def configure_completion_gates(self, hooks: dict[str, CompletionHook], *, timeout_seconds: float = 5.0) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or any(not callable(h) for h in hooks.values()):
            raise ValueError("Completion hooks require callable handlers and a positive finite timeout.")
        self._completion_hooks = dict(hooks)
        self._completion_timeout = timeout_seconds

    def declare_completion_gate(self, task_id: int, owner: str, gate: CompletionGate) -> TaskJob:
        if owner not in self._completion_hooks:
            raise ValueError("Extension must register a completion confirmation hook first.")
        with self._mutation():
            return declare_completion_gate(task_id, owner, gate, self.root)

    def record_completion_evidence(self, task_id: int, owner: str, name: str, reference: str) -> TaskJob:
        with self._mutation():
            job = append_completion_evidence(task_id, owner, name, reference, self.root)
        self._evaluate_completion_gates()
        return self.find(task_id) or job

    def _evaluate_completion_gates(self) -> None:
        if not self._completion_lock.acquire(blocking=False):
            return
        try:
            with self._mutation():
                snapshots = completion_snapshots(self.root)
            attempted: set[int] = set()
            # Terminal child gates may release ancestors in the same tick. Each
            # owner is consulted at most once per tick, even if it extends a gate.
            while snapshots:
                changed = False
                for snapshot in snapshots:
                    if snapshot.task.id in attempted:
                        continue
                    expired = gate_time(snapshot.task.completion_gate.deadline_at) <= gate_time(self._clock())
                    if expired:
                        decision = CompletionDecision("fail", "Completion gate timed out.")
                    elif completion_gate_met(snapshot):
                        hook = self._completion_hooks.get(snapshot.extension_name)
                        if hook is None:
                            continue  # Missing extension after restart holds until its deadline.
                        decision = invoke_completion_hook(hook, snapshot, self._completion_timeout)
                    else:
                        continue
                    attempted.add(snapshot.task.id)
                    with self._mutation():
                        updated = apply_completion_decision(snapshot, decision, self.root, checked_at=self._clock())
                    changed = changed or updated is not None
                if not changed:
                    break
                with self._mutation():
                    snapshots = completion_snapshots(self.root)
        finally:
            self._completion_lock.release()

    def configure_extension_recovery(
        self, hooks: dict[str, ExtensionRecoveryHook], *, timeout_seconds: float = 5.0,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("Recovery timeout must be finite and positive.")
        if any(not callable(hook) for hook in hooks.values()):
            raise ValueError("Recovery hooks must be callable.")
        self._recovery_hooks = dict(hooks)
        self._recovery_timeout = timeout_seconds

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
                thread_id=thread_id,
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
                **({"merge_policy": merge_policy} if merge_policy else {}),
            )

    def start_next(
        self,
        *,
        triggers: frozenset[str] | None = None,
    ) -> TaskJob | None:
        self._evaluate_completion_gates()
        with self._mutation():
            if triggers is None:
                return begin_next_task(self.root)
            return begin_next_task(self.root, triggers=triggers)

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
        self, task_id: int | None, *, result: str = "", event_actor: str = "human",
        trigger: str = "/task cancel", worker_id: str = "",
    ) -> TaskJob | None:
        job = self._cancel_task(task_id, result=result, event_actor=event_actor, trigger=trigger, worker_id=worker_id)
        self._evaluate_completion_gates()
        return job

    def _cancel_task(
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
        self, task_id: int, status: FinalTaskStatus, *, result: str = "",
        event_actor: str = "agent", trigger: str = "task-runner", worker_id: str = "",
        failure_code: str = "", failure_class: str = "", retryable: bool = False,
    ) -> TaskJob | None:
        job = self._finalize_task(task_id, status, result=result, event_actor=event_actor,
                                  trigger=trigger, worker_id=worker_id, failure_code=failure_code,
                                  failure_class=failure_class, retryable=retryable)
        self._evaluate_completion_gates()
        return self.find(task_id) if job is not None else None

    def _finalize_task(
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
        result = self._reconcile_with_recovery()
        self._evaluate_completion_gates()
        if result.outcome not in {
            "terminal_repair",
            "interrupted_worker_recovery",
            "extension_recovery",
        } or result.task_id is None:
            return None
        return self.find(result.task_id)

    def reconcile(
        self,
        request: TaskReconciliationRequest | None = None,
    ) -> TaskReconciliationResult:
        result = self._reconcile_with_recovery(request)
        self._evaluate_completion_gates()
        return result

    def _reconcile_with_recovery(
        self, request: TaskReconciliationRequest | None = None,
    ) -> TaskReconciliationResult:
        if not self._recovery_hooks:
            with self._mutation():
                return reconcile_running_task(
                    request, self.root, worker_liveness=self._tracked_worker_is_active,
                    checked_at=self._clock(),
                )
        with self._mutation():
            running = self.inspect().running
            owner = ""
            if running is not None and running.context_source.startswith("extension:"):
                owner = running.context_source.removeprefix("extension:")
            hook = self._recovery_hooks.get(owner)
            # Preserve the caller's compare-and-swap constraints before invoking a hook.
            matches = request is None or (
                running is not None
                and request.expected_task_id == running.id
                and request.expected_worker_id == running.worker_id
                and request.expected_worker_heartbeat_at == running.worker_heartbeat_at
                and request.expected_worker_lease_id == running.worker_lease_id
            )
            held = None
            if running is not None and hook is not None and matches:
                held = prepare_extension_recovery(
                    running, self.root, worker_liveness=self._tracked_worker_is_active,
                )
            if held is None:
                return reconcile_running_task(
                    request, self.root, worker_liveness=self._tracked_worker_is_active,
                    checked_at=self._clock(),
                )
        assert running is not None and hook is not None
        decision = invoke_recovery_hook(
            hook, ExtensionRecoverySnapshot(owner, running), self._recovery_timeout,
        )
        with self._mutation():
            return apply_extension_recovery(held, decision, self.root, checked_at=self._clock())

    def inspect(self) -> TaskQueueStatus:
        return task_queue_status(self.root)

    def find_by_message(
        self,
        message_id: MessageId,
        *,
        chat_id: ConversationId | None = None,
    ) -> TaskJob | None:
        return find_task_by_message(message_id, self.root, chat_id=chat_id)

    def find(self, task_id: int) -> TaskJob | None:
        status = self.inspect()
        return next(
            (
                job
                for job in [
                    *status.pending,
                    *status.paused,
                    *status.awaiting,
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

    def record_result_message(self, task_id: int, message_id: MessageId) -> None:
        with self._mutation():
            record_task_result_message(task_id, message_id, self.root)

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
