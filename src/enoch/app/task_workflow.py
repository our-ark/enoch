from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import threading
import time
from typing import Any, Callable, Protocol

from enoch.app.activity import record_direct_action
from enoch.app.effects import DaemonEffectFence
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
    format_remote_publish_result,
    pr_step_update,
    pr_summary,
)
from enoch.immune import ImmuneResult, run_immune_system
from enoch.identity import Identity
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
    ChangeCaptureRequest,
    ChangeCaptureResult,
    RepositoryProvider,
    RepositoryProviderError,
    RepositoryRevision,
    ReviewProvider,
    ReviewProviderError,
    ReviewRecord,
    ReviewSubmission,
    RuntimeExecutionControl,
    RuntimeResult,
    UnsupportedProviderFeature,
    require_repository_features,
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
    push_current_branch,
)
from enoch.providers.authorization import CapabilityAuthorizationError
from enoch.providers.runtime import invoke_runtime_action
from enoch.runtime import (
    ACTION_SANDBOX_FULL_ACCESS,
    DEFAULT_BRANCH,
    WORKSPACE_WRITE_SANDBOX,
)
from enoch.tasks.failures import classify_task_failure
from enoch.tasks.queue import (
    TaskJob,
    record_task_result,
    record_task_runtime_result,
    task_queue_status,
)
from enoch.tasks.worktree import (
    TaskWorktree,
    prepare_existing_branch_worktree,
    prepare_repository_task_workspace,
    remove_repository_task_workspace,
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
from enoch.workflows import WorkflowEngine


@dataclass(frozen=True)
class TaskWorkflowDependencies:
    """Version-control and validation effects supplied by the application shell."""

    run_immune_system: Callable[..., Any] = run_immune_system
    prepare_repository_task_workspace: Callable[..., Any] = (
        prepare_repository_task_workspace
    )
    prepare_existing_branch_worktree: Callable[..., Any] = (
        prepare_existing_branch_worktree
    )
    remove_task_worktree: Callable[..., Any] = remove_task_worktree
    remove_repository_task_workspace: Callable[..., Any] = (
        remove_repository_task_workspace
    )
    ensure_clean_worktree: Callable[..., Any] = ensure_clean_worktree
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
    repository: RepositoryProvider
    review: ReviewProvider
    workflow: WorkflowEngine
    effect_fence: DaemonEffectFence

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
        *,
        notification_key: str = "",
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
            self.preflight_portable_task()
            sandbox = action_sandbox(app.root)
            app._send_step_update(chat_id, "Preparing an isolated task workspace.")
            task_worktree = app._prepare_task_worktree(request)
            work_root = task_worktree.path
            branch_note = (
                f"Enoch prepared isolated workspace {task_worktree.branch} at "
                f"{work_root} from the latest authoritative revision."
            )
            app._send_step_update(chat_id, "Working.")
            runtime_execution = execution or RuntimeExecutionControl(
                request_id=f"task:{CURRENT_TASK_ID.get() or 'inline'}",
                session_key=session_key,
                cancellation_event=app._current_task_cancellation_event(),
                progress_callback=lambda progress: app._send_progress(
                    chat_id,
                    progress.elapsed_seconds,
                    progress.sandbox,
                ),
            )
            runtime_result = app.effect_fence.run_runtime_authorized(
                "runtime.execute",
                ("runtime.execute",),
                lambda fenced_execution: invoke_runtime_action(
                    app.runtime,
                    app.identity,
                    app._profile_prompt(
                        work_request_prompt(
                            work_request_with_context(request, context),
                            remote_review=bool(
                                getattr(app.review, "supports_remote_review", True)
                            ),
                        ),
                        purpose="task",
                        chat_id=chat_id,
                    ),
                    cwd=work_root,
                    sandbox=sandbox,
                    execution=fenced_execution,
                    state_root=app.root,
                ),
                runtime_execution,
                task_id=CURRENT_TASK_ID.get(),
            )
            record_current_task_runtime_result(
                runtime_result,
                provider=app.runtime.name,
                root=app.root,
                workflow=app.workflow,
            )
            result = runtime_result.final_text
            app._raise_if_current_task_cancelled()
            result = app._capture_task_regression_signals(result)
            memory_result = extract_memory_requests(result)
            result = memory_result.visible_reply
            memory_note = app.effect_fence.run(
                app._save_memory_requests,
                memory_result.requests,
            )
            app.effect_fence.run(record_direct_action, request, result, app.root)
            action_state = app.repository.inspect_working_copy(work_root)
            action_files = tuple(sorted(action_state.changed_paths))
        except (AgentRuntimeCancelled, AgentRuntimeTimedOut, AgentRuntimeAccessUnavailable):
            raise
        except (
            AgentRuntimeError,
            CapabilityAuthorizationError,
            TypeError,
            VcsError,
            RepositoryProviderError,
            ReviewProviderError,
            UnsupportedProviderFeature,
            OSError,
        ) as error:
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
                cleanup = app.effect_fence.run_authorized(
                    "vcs.remove-workspace",
                    ("vcs.workspace",),
                    self.dependencies.remove_repository_task_workspace,
                    app.repository,
                    app.root,
                    task_worktree,
                    task_id=CURRENT_TASK_ID.get(),
                    force=True,
                )
                parts.append("No files changed.")
                parts.append(cleanup)
            except (RepositoryProviderError, VcsError) as error:
                parts.append(f"Enoch could not clean up the task workspace: {error}")
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
                f"I did not publish a review because doctor failed. Task workspace "
                f"{work_root} was preserved for inspection."
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

    def preflight_portable_task(self) -> None:
        app = self.application
        require_repository_features(
            app.repository,
            "isolated_workspaces",
            "immutable_revisions",
        )
        app.effect_fence.authorize(
            "task.portable-workflow",
            (
                "runtime.execute",
                "vcs.inspect",
                "vcs.authoritative",
                "vcs.workspace",
                "vcs.capture",
                "forge.review",
            ),
            task_id=CURRENT_TASK_ID.get(),
        )

    def prepare_task_worktree(self, request: str) -> TaskWorktree:
        app = self.application
        task_id = CURRENT_TASK_ID.get()
        worker_id = CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            raise VcsError("Task worktree preparation requires an owned running task.")
        job = app.workflow.find(task_id)
        if job is None or job.status != "running" or job.worker_id != worker_id:
            raise VcsError(f"Task #{task_id} no longer owns its execution lease.")
        worktree = app.effect_fence.run_authorized(
            "vcs.prepare-workspace",
            ("vcs.authoritative", "vcs.workspace"),
            self.dependencies.prepare_repository_task_workspace,
            app.repository,
            app.root,
            task_id,
            request,
            task_id=task_id,
            resident_branch=app._resident_branch_name(),
            created_at=job.created_at,
            existing_path=job.worktree_path,
            existing_workspace_id=job.branch_name,
        )
        recorded = app.workflow.record_worktree(
            task_id,
            worker_id,
            worktree.path,
            worktree.branch,
        )
        if recorded is None:
            raise VcsError(
                f"Task #{task_id} lost its execution lease while preparing its worktree."
            )
        return worktree

    def run_forge_maintenance(self, request: ForgeMaintenanceRequest) -> str:
        app = self.application
        app._update_work_status("Updating pull requests.")
        results = []
        for number in request.close_numbers:
            results.append(
                app.effect_fence.run_authorized(
                    "forge.maintain",
                    ("forge.maintain",),
                    app.forge.close_pull_request,
                    number,
                    task_id=CURRENT_TASK_ID.get(),
                    root=app.root,
                    comment=(
                        duplicate_close_comment(request.keep_number)
                        if request.keep_number
                        else None
                    ),
                )
            )
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
            pushed = app.effect_fence.run_authorized(
                "forge.publish-branch",
                ("forge.publish",),
                self.dependencies.push_current_branch,
                task_id=CURRENT_TASK_ID.get(),
                root=work_root,
            )
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
            pr = app.effect_fence.run_authorized(
                "forge.create-pull-request",
                ("forge.publish",),
                create_pull_request_for_current_task,
                work_root,
                app.root,
                task_id=CURRENT_TASK_ID.get(),
                forge=app.forge,
                workflow=app.workflow,
            )
            outputs.append(format_pr_result(pr))
            if pr.url:
                app._update_work_status(pr_step_update(pr), pr_url=pr.url)
                record_current_task_result(
                    "\n\n".join(outputs),
                    app.root,
                    workflow=app.workflow,
                )
            app._send_step_update(chat_id, pr_step_update(pr))

            app._send_step_update(
                chat_id,
                "Cleaning up the isolated task worktree.",
            )
            outputs.append(
                app.effect_fence.run_authorized(
                    "vcs.remove-worktree",
                    ("vcs.write",),
                    self.dependencies.remove_task_worktree,
                    app.root,
                    task_worktree,
                    task_id=CURRENT_TASK_ID.get(),
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
        except (
            VcsError,
            ForgeProviderError,
            CapabilityAuthorizationError,
        ) as error:
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
        job = app.workflow.find(task_id)
        if job is None or job.status != "running" or job.worker_id != worker_id:
            raise VcsError(f"Task #{task_id} no longer owns its execution lease.")
        worktree = app.effect_fence.run_authorized(
            "vcs.prepare-worktree",
            ("vcs.write",),
            self.dependencies.prepare_existing_branch_worktree,
            app.root,
            task_id,
            branch,
            task_id=task_id,
            existing_path=job.worktree_path,
        )
        recorded = app.workflow.record_worktree(
            task_id,
            worker_id,
            worktree.path,
            worktree.branch,
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
        stage = portable_publish_stage(
            resume_job.publish_stage if resume_job is not None else ""
        )
        revision_id = resume_job.commit_sha if resume_job is not None else ""
        workspace_id = resume_job.remote_branch if resume_job is not None else ""
        review_url = resume_job.pr_url if resume_job is not None else ""
        review_published = stage == "review_published"
        completed_stages = [
            candidate
            for candidate in ("captured", "review_published")
            if candidate == stage
            or (stage == "review_published" and candidate == "captured")
        ]
        captured_revision: RepositoryRevision | None = None
        try:
            if stage != "captured" and stage != "review_published":
                app._send_step_update(chat_id, "Capturing the validated change.")
                state = app.repository.inspect_working_copy(publish_root)
                changed = tuple(state.changed_paths)
                allowed = frozenset(path for path in allowed_files if path)
                unexpected = tuple(path for path in changed if path not in allowed)
                if unexpected:
                    raise RepositoryProviderError(
                        "Refusing to capture unexpected files: "
                        + ", ".join(unexpected[:8])
                    )
                if not changed:
                    raise RepositoryProviderError("No working-copy changes to capture.")
                capture = app.effect_fence.run_authorized(
                    "vcs.capture-change",
                    ("vcs.capture",),
                    app.repository.capture_change,
                    ChangeCaptureRequest(
                        message=self.dependencies.feature_title(request),
                        paths=changed,
                        metadata={"task_id": CURRENT_TASK_ID.get()},
                    ),
                    task_id=CURRENT_TASK_ID.get(),
                    root=publish_root,
                )
                captured_revision = capture.revision
                revision_id = capture.revision.id
                workspace_id = (
                    task_worktree.repository_workspace.id
                    if task_worktree is not None
                    and task_worktree.repository_workspace is not None
                    else (task_worktree.branch if task_worktree is not None else "")
                )
                capture_message = format_change_capture(capture)
                outputs.append(capture_message)
                summaries.append(capture_message)
                completed_stages.append("captured")
                app._record_current_publish_stage(
                    "captured",
                    commit_sha=revision_id,
                    remote_branch=workspace_id,
                )
                require_captured_working_copy(
                    app.repository,
                    captured_revision,
                    publish_root,
                )
                app._send_step_update(
                    chat_id,
                    f"Captured revision {capture.revision.display}.",
                )
                stage = "captured"
            else:
                outputs.append(
                    "Resuming review publication after captured revision "
                    f"{revision_id or 'unknown'}."
                )
                captured_revision = app.repository.resolve_repository_revision(
                    revision_id,
                    publish_root,
                )
                if captured_revision is None:
                    raise RepositoryProviderError(
                        f"Captured revision {revision_id!r} is no longer available."
                    )
                require_captured_working_copy(
                    app.repository,
                    captured_revision,
                    publish_root,
                )

            if not review_published:
                assert captured_revision is not None
                app._send_step_update(chat_id, "Preparing the review handoff.")
                workspace = (
                    task_worktree.repository_workspace
                    if task_worktree is not None
                    else None
                )
                base_revision = (
                    workspace.base_revision
                    if workspace is not None
                    else app.repository.authoritative_base(publish_root).revision
                )
                current_job = task_by_id(
                    CURRENT_TASK_ID.get() or 0,
                    app.root,
                    workflow=app.workflow,
                )
                provenance = (
                    evolution_provenance_for_job(current_job)
                    if current_job is not None
                    else None
                )
                review = app.effect_fence.run_authorized(
                    "forge.publish-review",
                    ("forge.review",),
                    app.review.publish_review,
                    ReviewSubmission(
                        title=self.dependencies.feature_title(request),
                        body="",
                        revision=captured_revision,
                        base_revision=base_revision,
                        evidence=review_evidence(validation_result),
                        metadata={
                            "base_name": app._authoritative_branch_name(),
                            "workspace_id": workspace_id,
                            "task_id": CURRENT_TASK_ID.get(),
                            "evolution_provenance": provenance,
                        },
                    ),
                    task_id=CURRENT_TASK_ID.get(),
                    root=publish_root,
                )
                review_message = format_review_record(review)
                outputs.append(review_message)
                summaries.append(review_message)
                app._send_step_update(
                    chat_id,
                    review_step_update(review),
                )
                review_url = review.identity.url
                if bool(
                    getattr(app.review, "supports_remote_review", True)
                ) and (
                    not review_url or review.state not in {"open", "published"}
                ):
                    failure = (
                        "Enoch captured the change but the review provider did not "
                        "return a review URL. The workspace was preserved for retry."
                    )
                    app._send_step_update(chat_id, failure)
                    return WorkOutcome.failure(
                        "\n\n".join([*outputs, failure]),
                        status="publish_incomplete",
                        code="review_publication_failed",
                        failure_class="transient",
                        retryable=True,
                        completed_stages=tuple(dict.fromkeys(completed_stages)),
                        commit_sha=revision_id,
                        remote_branch=workspace_id,
                    )
                completed_stages.append("review_published")
                app._record_current_publish_stage(
                    "review_published",
                    commit_sha=revision_id,
                    remote_branch=workspace_id,
                    pr_url=review_url,
                    published_remotely=bool(review_url),
                )
                if review_url:
                    app._update_work_status(
                        review_step_update(review),
                        pr_url=review_url,
                    )
                    record_current_task_result(
                        "\n\n".join(outputs),
                        app.root,
                        workflow=app.workflow,
                    )
                review_published = True
                stage = "review_published"
            elif review_url:
                outputs.append(f"Review already published: {review_url}")

            resident_branch = app._resident_branch_name()
            if task_worktree is not None:
                app._send_step_update(
                    chat_id,
                    "Cleaning up the isolated task workspace.",
                )
                handoff = app.effect_fence.run_authorized(
                    "vcs.remove-workspace",
                    ("vcs.workspace",),
                    self.dependencies.remove_repository_task_workspace,
                    app.repository,
                    app.root,
                    task_worktree,
                    task_id=CURRENT_TASK_ID.get(),
                    force=bool(review_url),
                )
            else:
                app._send_step_update(
                    chat_id,
                    f"Returning local checkout to {resident_branch}.",
                )
                handoff = app.effect_fence.run_authorized(
                    "vcs.return-to-resident",
                    ("vcs.write",),
                    app._return_to_resident_after_handoff,
                    task_id=CURRENT_TASK_ID.get(),
                    published_remotely=bool(review_url),
                )
            outputs.append(handoff)
            summaries.append(handoff)
            app._send_step_update(
                chat_id,
                f"Resident checkout remains on {resident_branch}.",
            )
            if review_url:
                app._queue_session_sync(
                    chat_id,
                    repository_handoff_note(
                        workspace_id,
                        review_url,
                        resident_branch,
                        app._authoritative_branch_name(),
                    ),
                )
        except (
            VcsError,
            ForgeProviderError,
            RepositoryProviderError,
            ReviewProviderError,
            UnsupportedProviderFeature,
            CapabilityAuthorizationError,
        ) as error:
            failure = f"Enoch could not publish this edit for review: {error}"
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
                commit_sha=revision_id,
                remote_branch=workspace_id,
            )

        action = (
            f"published edit for review: {request}"
            if review_url
            else f"captured edit in the repository: {request}"
        )
        app.effect_fence.run(
            record_direct_action,
            action,
            "\n\n".join(summaries),
            app.root,
        )
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
            commit_sha=revision_id,
            remote_branch=workspace_id,
            pr_url=review_url,
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
        recorded = app.workflow.record_publish_state(
            task_id,
            worker_id,
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


def record_current_task_result(
    result: str,
    root: Path,
    *,
    workflow: WorkflowEngine | None = None,
) -> None:
    task_status = CURRENT_WORK_STATUS.get()
    task_id = (
        task_status.task_id
        if task_status is not None and task_status.task_id is not None
        else CURRENT_TASK_ID.get()
    )
    if task_id is None:
        return
    if workflow is not None:
        workflow.record_result(task_id, result)
    else:
        record_task_result(task_id, result, root)


def record_current_task_runtime_result(
    result: RuntimeResult,
    *,
    provider: str,
    root: Path,
    workflow: WorkflowEngine | None = None,
) -> None:
    task_status = CURRENT_WORK_STATUS.get()
    task_id = (
        task_status.task_id
        if task_status is not None and task_status.task_id is not None
        else CURRENT_TASK_ID.get()
    )
    if task_id is None:
        return
    if workflow is not None:
        workflow.record_runtime_result(task_id, result, provider=provider)
    else:
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
    workflow: WorkflowEngine | None = None,
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
    job = None
    if task_id is not None:
        job = (
            workflow.find(task_id)
            if workflow is not None
            else task_by_id(task_id, state_root)
        )
    provenance = evolution_provenance_for_job(job) if job is not None else None
    if provenance is None:
        return forge.create_pull_request(root=work_root)
    return forge.create_pull_request(
        root=work_root,
        evolution_provenance=provenance,
    )


def task_by_id(
    task_id: int,
    root: Path,
    *,
    workflow: WorkflowEngine | None = None,
) -> TaskJob | None:
    status = workflow.inspect() if workflow is not None else task_queue_status(root)
    jobs = [*status.pending, *status.paused, *status.history]
    if status.running is not None:
        jobs.append(status.running)
    return next((job for job in jobs if job.id == task_id), None)


def portable_publish_stage(stage: str) -> str:
    normalized = stage.strip().lower()
    if normalized in {"committed", "pushed", "captured"}:
        return "captured"
    if normalized in {"pr_opened", "review_published"}:
        return "review_published"
    return normalized


def format_change_capture(result: ChangeCaptureResult) -> str:
    return "\n".join(
        [
            "Repository change captured.",
            f"Revision: {result.revision.display}",
            "Changed files:",
            *(f"- {path}" for path in result.changed_paths),
        ]
    )


def require_captured_working_copy(
    repository: RepositoryProvider,
    revision: RepositoryRevision,
    root: Path,
) -> None:
    state = repository.inspect_working_copy(root)
    if state.revision.id != revision.id:
        raise RepositoryProviderError(
            "The working copy did not remain on captured revision "
            f"{revision.display}."
        )
    if not state.clean:
        raise RepositoryProviderError(
            "The working copy remained dirty after capturing revision "
            f"{revision.display}."
        )


def review_evidence(validation_result: ImmuneResult | None) -> tuple[str, ...]:
    if validation_result is None:
        return ()
    status = "passed" if validation_result.passed else "failed"
    summary = str(
        getattr(getattr(validation_result, "diagnosis", None), "summary", "") or ""
    ).strip()
    evidence = f"doctor:{status}"
    if summary:
        evidence = f"{evidence} ({summary})"
    return (evidence,)


def format_review_record(review: ReviewRecord) -> str:
    lines = [
        f"Review {review.identity.id}",
        f"Status: {review.state}",
        f"Title: {review.title}",
        f"Revision: {review.versions[-1].revision.display}",
    ]
    if review.identity.url:
        lines.append(f"URL: {review.identity.url}")
    return "\n".join(lines)


def review_step_update(review: ReviewRecord) -> str:
    if review.identity.url and review.state in {"open", "published"}:
        return f"Review ready: {review.identity.url}"
    if review.identity.url:
        return f"Review handoff incomplete: {review.identity.url}"
    return f"Review recorded as {review.state}."


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
