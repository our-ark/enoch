from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol

from enoch.app.activity import record_direct_action
from enoch.app.execution_context import (
    CURRENT_TASK_ID,
    CURRENT_TASK_WORKER_ID,
    CURRENT_WORK_STATUS,
)
from enoch.app.models import ForgeMaintenanceRequest, WorkOutcome, WorkStatusMessage
from enoch.app.parsing import (
    existing_branch_publish_request,
    forge_maintenance_request,
)
from enoch.app.presentation import clip_activity_text
from enoch.config import read_section
from enoch.formatting import (
    format_doctor_result,
    format_pr_result,
    format_publish_result,
    format_remote_publish_result,
    pr_step_update,
    pr_summary,
    publish_summary,
    remote_publish_summary,
)
from enoch.immune import ImmuneResult, run_immune_system
from enoch.identity import Identity
from enoch.operations.update_tools import task_branch_base
from enoch.prompt_append import (
    extract_memory_requests,
    repository_handoff_note,
    work_request_prompt,
)
from enoch.providers.contracts import (
    AgentRuntimeAccessUnavailable,
    AgentRuntime,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    AgentRuntimeTimedOut,
    ConversationId,
    EvolutionProvenance,
    ForgeProvider,
    ForgeProviderError,
    MessageId,
    PullRequestCloseResult,
    PullRequestResult,
    RuntimeExecutionControl,
    RuntimeResult,
)
from enoch.providers.forge import (
    FunctionForgeProvider,
    close_pull_request,
    create_pull_request,
    feature_title,
    inspect_pull_request,
    inspect_pull_request_merge,
    list_open_pull_requests,
    merge_pull_request,
    prepare_local_publish,
    push_current_branch,
)
from enoch.providers.runtime import invoke_runtime_action
from enoch.runtime import (
    ACTION_SANDBOX_FULL_ACCESS,
    DEFAULT_BRANCH,
    WORKSPACE_WRITE_SANDBOX,
)
from enoch.tasks.failures import classify_task_failure
from enoch.tasks.queue import (
    TaskJob,
    record_task_publish_state,
    record_task_result,
    record_task_runtime_result,
    record_task_worktree,
    task_queue_status,
)
from enoch.tasks.worktree import (
    TaskWorktree,
    prepare_existing_branch_worktree,
    prepare_task_worktree,
    remove_task_worktree,
)
from enoch.vcs_tools import (
    VcsError,
    changed_files,
    current_branch,
    delete_branch,
    ensure_clean_worktree,
    switch_branch,
)


@dataclass(frozen=True)
class TaskWorkflowDependencies:
    """Version-control and validation effects supplied by the application shell."""

    run_immune_system: Callable[..., Any] = run_immune_system
    changed_files: Callable[..., Any] = changed_files
    task_branch_base: Callable[..., Any] = task_branch_base
    prepare_task_worktree: Callable[..., Any] = prepare_task_worktree
    prepare_existing_branch_worktree: Callable[..., Any] = (
        prepare_existing_branch_worktree
    )
    remove_task_worktree: Callable[..., Any] = remove_task_worktree
    ensure_clean_worktree: Callable[..., Any] = ensure_clean_worktree
    prepare_local_publish: Callable[..., Any] = prepare_local_publish
    push_current_branch: Callable[..., Any] = push_current_branch
    feature_title: Callable[..., Any] = feature_title
    current_branch: Callable[..., Any] = current_branch
    switch_branch: Callable[..., Any] = switch_branch
    delete_branch: Callable[..., Any] = delete_branch


class TaskWorkflowHost(Protocol):
    """Application-shell capabilities used by task execution and publishing."""

    identity: Identity
    root: Path
    runtime: AgentRuntime
    forge: ForgeProvider

    def _raise_if_current_task_cancelled(self) -> None: ...

    def _run_forge_maintenance(self, request: ForgeMaintenanceRequest) -> str: ...

    def _publish_existing_branch(self, chat_id: ConversationId, branch: str) -> str: ...

    def _send_step_update(
        self,
        chat_id: ConversationId | None,
        message: str,
    ) -> None: ...

    def _prepare_task_worktree(self, request: str) -> TaskWorktree: ...

    def _profile_prompt(
        self,
        prompt: str,
        *,
        purpose: str,
        chat_id: ConversationId | None = None,
    ) -> str: ...

    def _current_task_cancellation_event(self) -> threading.Event | None: ...

    def _send_progress(
        self,
        chat_id: ConversationId,
        elapsed_seconds: int,
        sandbox: str,
    ) -> None: ...

    def _capture_task_regression_signals(self, reply: str) -> str: ...

    def _save_memory_requests(self, requests: tuple[str, ...]) -> str: ...

    def _publish_feature_pr(
        self,
        chat_id: ConversationId,
        request: str,
        allowed_files: tuple[str, ...],
        **kwargs: Any,
    ) -> WorkOutcome: ...

    def _record_current_publish_stage(self, stage: str, **kwargs: Any) -> None: ...

    def _resident_branch_name(self, fallback: str = "") -> str: ...

    def _update_work_status(
        self,
        latest_update: str,
        *,
        status: str | None = None,
        pr_url: str = "",
    ) -> bool: ...

    def _safe_send_message_id(
        self,
        chat_id: ConversationId,
        message: str,
    ) -> MessageId | None: ...

    def _format_work_status(self, status: WorkStatusMessage) -> str: ...

    def _prepare_existing_branch_task_worktree(self, branch: str) -> TaskWorktree: ...

    def _queue_session_sync(
        self,
        chat_id: ConversationId | None,
        note: str,
    ) -> None: ...

    def _authoritative_branch_name(self) -> str: ...

    def _return_to_resident_after_handoff(
        self,
        *,
        published_remotely: bool = True,
    ) -> str: ...


class TaskWorkflow:
    """Owns isolated task execution and the commit/push/PR handoff lifecycle."""

    def __init__(
        self,
        application: TaskWorkflowHost,
        *,
        dependencies: TaskWorkflowDependencies | None = None,
    ) -> None:
        self.application = application
        self.dependencies = dependencies or TaskWorkflowDependencies()

    def run_direct_work(
        self,
        chat_id: ConversationId,
        request: str,
        *,
        context: str = "",
        session_key: str,
        execution: RuntimeExecutionControl | None = None,
    ) -> WorkOutcome:
        app = self.application
        app._raise_if_current_task_cancelled()
        forge_maintenance = forge_maintenance_request(request)
        if forge_maintenance is not None:
            reply = app._run_forge_maintenance(forge_maintenance)
            app._raise_if_current_task_cancelled()
            return WorkOutcome.completed(reply)

        publish_branch = existing_branch_publish_request(request)
        if publish_branch is not None:
            reply = app._publish_existing_branch(chat_id, publish_branch)
            app._raise_if_current_task_cancelled()
            if reply.strip().lower().startswith("enoch could not"):
                failure = classify_task_failure(reply)
                return WorkOutcome.failure(
                    reply,
                    code=failure.code,
                    failure_class=failure.failure_class,
                    retryable=failure.retryable,
                )
            return WorkOutcome.completed(reply)

        try:
            sandbox = action_sandbox(app.root)
            app._send_step_update(chat_id, "Preparing an isolated task worktree.")
            task_worktree = app._prepare_task_worktree(request)
            work_root = task_worktree.path
            branch_note = (
                f"Enoch prepared isolated worktree {work_root} on branch "
                f"{task_worktree.branch} from the latest task base."
            )
            app._send_step_update(chat_id, "Working.")
            runtime_result = invoke_runtime_action(
                app.runtime,
                app.identity,
                app._profile_prompt(
                    work_request_prompt(
                        work_request_with_context(request, context),
                        remote_review=bool(
                            getattr(app.forge, "supports_remote_review", True)
                        ),
                    ),
                    purpose="task",
                    chat_id=chat_id,
                ),
                cwd=work_root,
                sandbox=sandbox,
                execution=execution
                or RuntimeExecutionControl(
                    request_id=f"task:{CURRENT_TASK_ID.get() or 'inline'}",
                    session_key=session_key,
                    cancellation_event=app._current_task_cancellation_event(),
                    progress_callback=lambda progress: app._send_progress(
                        chat_id,
                        progress.elapsed_seconds,
                        progress.sandbox,
                    ),
                ),
                state_root=app.root,
            )
            record_current_task_runtime_result(
                runtime_result,
                provider=app.runtime.name,
                root=app.root,
            )
            result = runtime_result.final_text
            app._raise_if_current_task_cancelled()
            result = app._capture_task_regression_signals(result)
            memory_result = extract_memory_requests(result)
            result = memory_result.visible_reply
            memory_note = app._save_memory_requests(memory_result.requests)
            record_direct_action(request, result, app.root)
            try:
                action_files = tuple(sorted(self.dependencies.changed_files(work_root)))
            except VcsError:
                action_files = ()
        except (AgentRuntimeCancelled, AgentRuntimeTimedOut, AgentRuntimeAccessUnavailable):
            raise
        except (AgentRuntimeError, TypeError, VcsError, OSError) as error:
            message = f"Enoch could not complete the requested work yet: {error}"
            failure = classify_task_failure(message)
            return WorkOutcome.failure(
                message,
                code=failure.code,
                failure_class=failure.failure_class,
                retryable=failure.retryable,
            )

        parts = [branch_note, result or "Enoch completed the requested work.", memory_note]
        if not action_files:
            try:
                cleanup = self.dependencies.remove_task_worktree(
                    app.root,
                    task_worktree,
                    force_delete_branch=True,
                )
                parts.append("No files changed.")
                parts.append(cleanup)
            except VcsError as error:
                parts.append(f"Enoch could not clean up the task worktree: {error}")
            return WorkOutcome.completed(
                "\n\n".join(part for part in parts if part),
                completed_stages=("edited",),
            )

        app._send_step_update(chat_id, "Running doctor.")
        app._raise_if_current_task_cancelled()
        doctor = self.dependencies.run_immune_system(
            work_root,
            state_root=app.root,
        )
        app._raise_if_current_task_cancelled()
        parts.append(format_doctor_result(doctor))
        app._send_step_update(
            chat_id,
            "Doctor passed." if doctor.passed else "Doctor failed.",
        )
        if not doctor.passed:
            parts.append(
                f"I did not open a PR because doctor failed. Task worktree {work_root} "
                "was preserved for inspection."
            )
            return WorkOutcome.failure(
                "\n\n".join(part for part in parts if part),
                code="validation_failed",
                failure_class="permanent",
                retryable=False,
                completed_stages=("edited",),
            )

        app._raise_if_current_task_cancelled()
        app._record_current_publish_stage("validated")
        publish_outcome = coerce_work_outcome(
            app._publish_feature_pr(
                chat_id,
                request,
                action_files,
                work_root=work_root,
                task_worktree=task_worktree,
                validation_result=doctor,
            )
        )
        app._raise_if_current_task_cancelled()
        return replace(
            publish_outcome,
            message="\n\n".join(
                part for part in [*parts, publish_outcome.message] if part
            ),
            completed_stages=tuple(
                dict.fromkeys(("edited", "validated", *publish_outcome.completed_stages))
            ),
        )

    def prepare_task_worktree(self, request: str) -> TaskWorktree:
        app = self.application
        task_id = CURRENT_TASK_ID.get()
        worker_id = CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            raise VcsError("Task worktree preparation requires an owned running task.")
        job = task_by_id(task_id, app.root)
        if job is None or job.status != "running" or job.worker_id != worker_id:
            raise VcsError(f"Task #{task_id} no longer owns its execution lease.")
        base = self.dependencies.task_branch_base(app.root)
        worktree = self.dependencies.prepare_task_worktree(
            app.root,
            task_id,
            request,
            start_point=base,
            resident_branch=app._resident_branch_name(),
            created_at=job.created_at,
            existing_path=job.worktree_path,
            existing_branch=job.branch_name,
        )
        recorded = record_task_worktree(
            task_id,
            worker_id,
            worktree.path,
            worktree.branch,
            app.root,
        )
        if recorded is None:
            raise VcsError(
                f"Task #{task_id} lost its execution lease while preparing its worktree."
            )
        return worktree

    def run_forge_maintenance(self, request: ForgeMaintenanceRequest) -> str:
        app = self.application
        app._update_work_status("Updating pull requests.")
        results = [
            app.forge.close_pull_request(
                number,
                root=app.root,
                comment=duplicate_close_comment(request.keep_number)
                if request.keep_number
                else None,
            )
            for number in request.close_numbers
        ]
        return format_pr_close_results(results, request.keep_number)

    def run_existing_branch_publish_with_status(
        self,
        chat_id: int,
        text: str,
        branch: str,
    ) -> str:
        app = self.application
        status_message = CURRENT_WORK_STATUS.get()
        token = None
        if status_message is None:
            status_message = WorkStatusMessage(
                chat_id=chat_id,
                message_id=0,
                request=text,
                started_at=time.monotonic(),
                status="running",
                latest_update=f"Publishing branch {branch}.",
            )
            message_id = app._safe_send_message_id(
                chat_id,
                app._format_work_status(status_message),
            )
            if message_id is not None:
                status_message.message_id = message_id
                token = CURRENT_WORK_STATUS.set(status_message)
        try:
            result = app._publish_existing_branch(chat_id, branch)
            if status_message is not None and status_message.message_id:
                app._update_work_status(
                    clip_activity_text(result, limit=800),
                    status="completed",
                )
                return ""
            return result
        finally:
            if token is not None:
                CURRENT_WORK_STATUS.reset(token)

    def publish_existing_branch(self, chat_id: int, branch: str) -> str:
        app = self.application
        resident_branch = app._resident_branch_name()
        outputs: list[str] = []
        try:
            app._send_step_update(
                chat_id,
                f"Preparing an isolated worktree for {branch}.",
            )
            task_worktree = app._prepare_existing_branch_task_worktree(branch)
            work_root = task_worktree.path
            self.dependencies.ensure_clean_worktree(work_root)

            app._send_step_update(chat_id, f"Handing off branch {branch}.")
            pushed = self.dependencies.push_current_branch(root=work_root)
            outputs.append(format_remote_publish_result(pushed))
            app._send_step_update(
                chat_id,
                (
                    f"Pushed branch {pushed.branch}."
                    if pushed.pushed
                    else f"Kept branch {pushed.branch} locally."
                ),
            )

            app._send_step_update(chat_id, "Preparing the review handoff.")
            pr = create_pull_request_for_current_task(
                work_root,
                app.root,
                forge=app.forge,
            )
            outputs.append(format_pr_result(pr))
            if pr.url:
                app._update_work_status(pr_step_update(pr), pr_url=pr.url)
                record_current_task_result("\n\n".join(outputs), app.root)
            app._send_step_update(chat_id, pr_step_update(pr))

            app._send_step_update(
                chat_id,
                "Cleaning up the isolated task worktree.",
            )
            outputs.append(
                self.dependencies.remove_task_worktree(
                    app.root,
                    task_worktree,
                    delete_local_branch=False,
                )
            )
            app._send_step_update(
                chat_id,
                f"Resident checkout remains on {resident_branch}.",
            )
            if pr.url:
                app._queue_session_sync(
                    chat_id,
                    repository_handoff_note(
                        pr.branch,
                        pr.url,
                        resident_branch,
                        app._authoritative_branch_name(),
                    ),
                )
        except (VcsError, ForgeProviderError) as error:
            failure = f"Enoch could not publish existing branch {branch}: {error}"
            app._send_step_update(chat_id, failure)
            return "\n\n".join([*outputs, failure]) if outputs else failure
        return "\n\n".join(outputs)

    def prepare_existing_branch_task_worktree(self, branch: str) -> TaskWorktree:
        app = self.application
        task_id = CURRENT_TASK_ID.get()
        worker_id = CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            raise VcsError("Branch publishing requires an owned running task.")
        job = task_by_id(task_id, app.root)
        if job is None or job.status != "running" or job.worker_id != worker_id:
            raise VcsError(f"Task #{task_id} no longer owns its execution lease.")
        worktree = self.dependencies.prepare_existing_branch_worktree(
            app.root,
            task_id,
            branch,
            existing_path=job.worktree_path,
        )
        recorded = record_task_worktree(
            task_id,
            worker_id,
            worktree.path,
            worktree.branch,
            app.root,
        )
        if recorded is None:
            raise VcsError(
                f"Task #{task_id} lost its execution lease while preparing its worktree."
            )
        return worktree

    def publish_feature_pr(
        self,
        chat_id: int,
        request: str,
        allowed_files: tuple[str, ...],
        *,
        work_root: Path | None = None,
        task_worktree: TaskWorktree | None = None,
        validation_result: ImmuneResult | None = None,
        resume_job: TaskJob | None = None,
    ) -> WorkOutcome:
        app = self.application
        publish_root = work_root or app.root
        outputs: list[str] = []
        summaries: list[str] = []
        stage = resume_job.publish_stage if resume_job is not None else ""
        commit_sha = resume_job.commit_sha if resume_job is not None else ""
        remote_branch = resume_job.remote_branch if resume_job is not None else ""
        pr_url = resume_job.pr_url if resume_job is not None else ""
        pushed_remotely = (
            resume_job.published_remotely if resume_job is not None else False
        )
        completed_stages = [
            candidate
            for candidate in ("committed", "pushed", "pr_opened")
            if candidate == stage
            or (stage == "pushed" and candidate == "committed")
            or (stage == "pr_opened" and candidate in {"committed", "pushed"})
        ]
        try:
            if stage not in {"committed", "pushed", "pr_opened"}:
                app._send_step_update(chat_id, "Committing the change.")
                commit = self.dependencies.prepare_local_publish(
                    self.dependencies.feature_title(request),
                    root=publish_root,
                    allowed_files=allowed_files,
                    validation_result=validation_result,
                )
                commit_sha = commit.commit_sha
                remote_branch = commit.branch
                outputs.append(format_publish_result(commit))
                summaries.append(publish_summary(commit))
                completed_stages.append("committed")
                app._record_current_publish_stage(
                    "committed",
                    commit_sha=commit_sha,
                    remote_branch=remote_branch,
                )
                app._send_step_update(chat_id, f"Committed {commit.commit_sha}.")
                stage = "committed"
            else:
                outputs.append(
                    f"Resuming publish workflow after commit {commit_sha or 'unknown'}."
                )

            if stage not in {"pushed", "pr_opened"}:
                app._send_step_update(
                    chat_id,
                    "Handing off the branch to the configured forge.",
                )
                pushed = self.dependencies.push_current_branch(root=publish_root)
                remote_branch = pushed.branch
                pushed_remotely = pushed.pushed
                outputs.append(format_remote_publish_result(pushed))
                summaries.append(remote_publish_summary(pushed))
                completed_stages.append("pushed")
                app._record_current_publish_stage(
                    "pushed",
                    commit_sha=commit_sha,
                    remote_branch=remote_branch,
                    published_remotely=pushed_remotely,
                )
                app._send_step_update(
                    chat_id,
                    (
                        f"Pushed branch {pushed.branch}."
                        if pushed.pushed
                        else f"Kept branch {pushed.branch} locally."
                    ),
                )
                stage = "pushed"

            if stage != "pr_opened":
                app._send_step_update(chat_id, "Preparing the review handoff.")
                pr = create_pull_request_for_current_task(
                    publish_root,
                    app.root,
                    forge=app.forge,
                )
                outputs.append(format_pr_result(pr))
                summaries.append(pr_summary(pr))
                app._send_step_update(chat_id, pr_step_update(pr))
                if bool(getattr(app.forge, "supports_remote_review", True)) and (
                    not pr.created or not pr.url
                ):
                    detail = (
                        pr.note
                        or pr.fallback_url
                        or "the forge did not return a PR URL"
                    )
                    failure = (
                        "Enoch pushed the task branch but could not open its pull request. "
                        f"The worktree and branch were preserved for retry. {detail}"
                    )
                    app._send_step_update(chat_id, failure)
                    return WorkOutcome.failure(
                        "\n\n".join([*outputs, failure]),
                        status="publish_incomplete",
                        code="pr_creation_failed",
                        failure_class="transient",
                        retryable=True,
                        completed_stages=tuple(dict.fromkeys(completed_stages)),
                        commit_sha=commit_sha,
                        remote_branch=remote_branch,
                    )
                pr_url = pr.url or ""
                if pr_url:
                    completed_stages.append("pr_opened")
                    app._record_current_publish_stage(
                        "pr_opened",
                        commit_sha=commit_sha,
                        remote_branch=remote_branch,
                        pr_url=pr_url,
                        published_remotely=pushed_remotely,
                    )
                    app._update_work_status(pr_step_update(pr), pr_url=pr_url)
                    record_current_task_result("\n\n".join(outputs), app.root)
                    stage = "pr_opened"
            elif pr_url:
                outputs.append(f"Pull request already opened: {pr_url}")

            resident_branch = app._resident_branch_name()
            if task_worktree is not None:
                app._send_step_update(
                    chat_id,
                    "Cleaning up the isolated task worktree.",
                )
                handoff = self.dependencies.remove_task_worktree(
                    app.root,
                    task_worktree,
                    delete_local_branch=pushed_remotely,
                    force_delete_branch=pushed_remotely,
                )
            else:
                app._send_step_update(
                    chat_id,
                    f"Returning local checkout to {resident_branch}.",
                )
                handoff = app._return_to_resident_after_handoff(
                    published_remotely=pushed_remotely,
                )
            outputs.append(handoff)
            summaries.append(handoff)
            app._send_step_update(
                chat_id,
                f"Resident checkout remains on {resident_branch}.",
            )
            if pr_url:
                app._queue_session_sync(
                    chat_id,
                    repository_handoff_note(
                        remote_branch,
                        pr_url,
                        resident_branch,
                        app._authoritative_branch_name(),
                    ),
                )
        except (VcsError, ForgeProviderError) as error:
            failure = f"Enoch could not publish this edit as a pull request: {error}"
            app._send_step_update(chat_id, failure)
            classified = classify_task_failure(failure)
            publish_started = bool(completed_stages)
            return WorkOutcome.failure(
                "\n\n".join([*outputs, failure]) if outputs else failure,
                status="publish_incomplete" if publish_started else "failed",
                code=(
                    classified.code
                    if classified.code != "unknown_failure"
                    else "publish_failed"
                ),
                failure_class=(
                    "transient" if publish_started else classified.failure_class
                ),
                retryable=publish_started or classified.retryable,
                completed_stages=tuple(dict.fromkeys(completed_stages)),
                commit_sha=commit_sha,
                remote_branch=remote_branch,
            )

        action = (
            f"published edit as pull request: {request}"
            if pr_url
            else f"committed edit to local branch: {request}"
        )
        record_direct_action(action, "\n\n".join(summaries), app.root)
        reply = "\n\n".join(outputs)
        app._queue_session_sync(
            chat_id,
            activity_sync_note(
                f"Enoch {action}",
                (
                    "Final workflow summary: "
                    f"{clip_activity_text(summaries[-1]) if summaries else 'none'}"
                ),
                f"Result: {clip_activity_text(reply)}",
            ),
        )
        return WorkOutcome.completed(
            reply,
            completed_stages=tuple(dict.fromkeys(completed_stages)),
            commit_sha=commit_sha,
            remote_branch=remote_branch,
            pr_url=pr_url,
        )

    def record_current_publish_stage(
        self,
        stage: str,
        *,
        commit_sha: str = "",
        remote_branch: str = "",
        pr_url: str = "",
        published_remotely: bool | None = None,
    ) -> None:
        app = self.application
        task_id = CURRENT_TASK_ID.get()
        worker_id = CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            return
        recorded = record_task_publish_state(
            task_id,
            worker_id,
            app.root,
            stage=stage,
            commit_sha=commit_sha,
            remote_branch=remote_branch,
            pr_url=pr_url,
            published_remotely=published_remotely,
        )
        if recorded is None:
            raise VcsError(
                f"Task #{task_id} lost its execution lease while recording "
                f"publish stage {stage}."
            )

    def resume_task_publish(self, job: TaskJob) -> WorkOutcome:
        app = self.application
        if not job.worktree_path or not job.branch_name:
            return WorkOutcome.failure(
                f"Task #{job.id} cannot resume publishing because its worktree "
                "metadata is missing.",
                code="worktree_precondition",
            )
        worktree = TaskWorktree(
            task_id=job.id,
            path=Path(job.worktree_path),
            branch=job.branch_name,
            created=False,
        )
        return app._publish_feature_pr(
            job.chat_id,
            job.text,
            (),
            work_root=worktree.path,
            task_worktree=worktree,
            resume_job=job,
        )

    def return_to_resident_after_handoff(
        self,
        *,
        published_remotely: bool = True,
    ) -> str:
        app = self.application
        branch = self.dependencies.current_branch(app.root)
        resident_branch = app._resident_branch_name(branch)
        if branch == resident_branch:
            return f"Local checkout is already on {resident_branch}."
        self.dependencies.ensure_clean_worktree(app.root)
        self.dependencies.switch_branch(resident_branch, app.root)
        cleanup = ""
        if published_remotely:
            cleanup = delete_local_branch_if_enabled(
                branch,
                app.root,
                protected_branch=resident_branch,
                delete_branch_fn=self.dependencies.delete_branch,
            )
        location = (
            "The change remains on the pushed remote branch."
            if published_remotely
            else f"The change remains on local branch {branch}."
        )
        if cleanup:
            return "\n".join(
                [
                    f"Enoch switched local checkout back to {resident_branch}.",
                    cleanup,
                    location,
                ]
            )
        return (
            f"Enoch switched local checkout back to {resident_branch}. "
            f"{location}"
        )


def action_sandbox(_root: Path) -> str:
    return ACTION_SANDBOX_FULL_ACCESS


def sandbox_description(sandbox: str) -> str:
    if sandbox == WORKSPACE_WRITE_SANDBOX:
        return "editing her code body"
    if sandbox == ACTION_SANDBOX_FULL_ACCESS:
        return "working with full filesystem access"
    return "thinking in read-only mode"


def changed_files_or_empty(root: Path) -> tuple[str, ...]:
    try:
        return tuple(changed_files(root))
    except VcsError:
        return ()


def delete_local_branch_if_enabled(
    branch: str,
    root: Path,
    *,
    protected_branch: str = "",
    delete_branch_fn: Callable[..., Any] = delete_branch,
) -> str:
    if not cleanup_local_branches(root):
        return ""
    if not branch or branch in {DEFAULT_BRANCH, protected_branch}:
        return ""
    delete_branch_fn(branch, root, force=True)
    return f"Deleted local branch {branch}."


def cleanup_local_branches(root: Path) -> bool:
    value = read_section("git", root).get("cleanup_local_branches", "").strip().lower()
    if not value:
        return True
    return value not in {"0", "false", "no", "off"}


def activity_sync_note(*lines: str) -> str:
    body = "\n".join(f"- {line.strip()}" for line in lines if line.strip())
    return "\n".join(
        [
            "Internal Enoch activity sync.",
            (
                "Record this as factual recent context for future recall. "
                "Do not treat it as a new user request."
            ),
            body,
        ]
    )


def work_reply_failed(reply: str) -> bool:
    normalized = reply.strip().lower()
    return (
        normalized.startswith("enoch could not ")
        or "i did not open a pr because doctor failed" in normalized
        or "doctor failed:" in normalized
    )


def coerce_work_outcome(value: WorkOutcome | str) -> WorkOutcome:
    if isinstance(value, WorkOutcome):
        return value
    message = str(value)
    if work_reply_failed(message):
        failure = classify_task_failure(message)
        return WorkOutcome.failure(
            message,
            code=failure.code,
            failure_class=failure.failure_class,
            retryable=failure.retryable,
        )
    return WorkOutcome.completed(message)


def work_request_with_context(request: str, context: str) -> str:
    context = context.strip()
    if not context:
        return request
    return "\n\n".join(
        [
            "Task request:",
            request.strip(),
            "Conversation context snapshot:",
            context,
        ]
    )


def record_current_task_result(result: str, root: Path) -> None:
    task_status = CURRENT_WORK_STATUS.get()
    task_id = (
        task_status.task_id
        if task_status is not None and task_status.task_id is not None
        else CURRENT_TASK_ID.get()
    )
    if task_id is None:
        return
    record_task_result(task_id, result, root)


def record_current_task_runtime_result(
    result: RuntimeResult,
    *,
    provider: str,
    root: Path,
) -> None:
    task_status = CURRENT_WORK_STATUS.get()
    task_id = (
        task_status.task_id
        if task_status is not None and task_status.task_id is not None
        else CURRENT_TASK_ID.get()
    )
    if task_id is None:
        return
    record_task_runtime_result(task_id, result, root, provider=provider)


def evolution_provenance_for_job(job: TaskJob) -> EvolutionProvenance | None:
    if not job.candidate_id:
        return None
    return EvolutionProvenance(
        candidate_id=job.candidate_id,
        evidence_source=job.evidence_source or job.source,
        signal_actor=job.signal_actor or legacy_candidate_signal_actor(job.source),
        candidate_actor=job.candidate_actor or "agent",
        approval_actor=job.approval_actor or legacy_task_approval_actor(job),
        task_id=job.id,
        parent_candidate_id=job.parent_candidate_id,
        source_task_id=job.source_task_id,
        retry_of_task_id=job.parent_task_id,
    )


def legacy_candidate_signal_actor(source: str) -> str:
    if source in {"backlog", "feedback", "learning"}:
        return "human"
    if source in {"inheritance", "brainstorming"}:
        return "agent"
    return "system"


def legacy_task_approval_actor(job: TaskJob) -> str:
    if (
        job.trigger.startswith("/evolve ")
        or job.context_source in {"evolve-approve", "evolve-retry"}
    ):
        return "human"
    if job.context_source == "evolve-scheduler":
        return "agent"
    return job.initiated_by


def create_pull_request_for_current_task(
    work_root: Path,
    state_root: Path | None = None,
    *,
    forge: ForgeProvider | None = None,
) -> PullRequestResult:
    forge = forge or FunctionForgeProvider(
        close_fn=close_pull_request,
        create_fn=create_pull_request,
        inspect_fn=inspect_pull_request,
        inspect_merge_fn=inspect_pull_request_merge,
        list_fn=list_open_pull_requests,
        merge_fn=merge_pull_request,
    )
    state_root = state_root or work_root
    task_id = CURRENT_TASK_ID.get()
    job = task_by_id(task_id, state_root) if task_id is not None else None
    provenance = evolution_provenance_for_job(job) if job is not None else None
    if provenance is None:
        return forge.create_pull_request(root=work_root)
    return forge.create_pull_request(
        root=work_root,
        evolution_provenance=provenance,
    )


def task_by_id(task_id: int, root: Path) -> TaskJob | None:
    status = task_queue_status(root)
    jobs = [*status.pending, *status.paused, *status.history]
    if status.running is not None:
        jobs.append(status.running)
    return next((job for job in jobs if job.id == task_id), None)


def duplicate_close_comment(keep_number: int | None) -> str:
    if keep_number is None:
        return "Closing this pull request from a Enoch maintenance job."
    return (
        f"Closing as a duplicate of #{keep_number}. Keeping #{keep_number} "
        "as the canonical PR for this change."
    )


def format_pr_close_results(
    results: list[PullRequestCloseResult],
    keep_number: int | None,
) -> str:
    if not results:
        return (
            "Enoch could not close any pull requests: "
            "no duplicate PR numbers were found."
        )
    lines = ["Enoch updated pull requests."]
    if keep_number is not None:
        lines.append(f"Kept PR: #{keep_number}")
    lines.append("Closed PRs:")
    failed = False
    for result in results:
        if result.closed:
            target = result.url or f"#{result.number}"
            lines.append(f"- #{result.number}: closed ({target})")
        else:
            failed = True
            lines.append(
                f"- #{result.number}: failed ({result.note or 'unknown error'})"
            )
    if failed:
        return (
            "Enoch could not complete every pull request update.\n\n"
            + "\n".join(lines)
        )
    return "\n".join(lines)
