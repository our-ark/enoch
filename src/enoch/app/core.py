from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from enoch.backlog import (
    BacklogItem,
    add_backlog_item,
    backlog_item,
    next_backlog_item,
    promote_backlog_item,
    remove_backlog_item,
    reprioritize_backlog_item,
)
from enoch.automatic_learning import record_learning_artifact
from enoch.brain import (
    act_in_session,
    codex_model_options,
    codex_reasoning_efforts,
    model_summary,
    reset_token_usage,
    respond,
)
from enoch.evolution.sources.brainstorming import (
    BrainstormError,
    parse_brainstorm_response,
    prepare_brainstorm_request,
)
from enoch.channel import (
    ChannelAttachmentError,
    begin_channel_lifecycle,
    image_prompt,
    load_channel_cursor,
    provider_label,
    record_channel_shutdown,
    save_channel_cursor,
    select_image_attachment,
    shutdown_message as channel_shutdown_message,
    startup_message as channel_startup_message,
    temporary_image_attachment,
)
from enoch.cron import (
    CronJob,
    add_cron_job,
    cancel_cron_job,
    claim_due_cron_jobs,
    cron_scheduler_wait_seconds,
    format_cron_interval,
    parse_cron_interval,
    record_cron_task,
)
from enoch.evolution.core import (
    MODE_AUTO_EVOLVE,
    MODE_DISABLED,
    BrainstormCreation,
    EvolveCandidate,
    EvolveProposal,
    acknowledge_evolve_schedule,
    approve_evolve_candidate,
    claim_due_evolve_schedule,
    claim_scheduled_brainstorm,
    create_brainstorm_candidates,
    create_learning_candidate,
    disable_evolve_schedule,
    evolve_report,
    get_evolve_candidate,
    load_evolve_candidates,
    load_evolve_state,
    propose_evolve,
    remove_evolve_candidate,
    set_evolve_cron_schedule,
    set_evolve_daily_schedule,
    set_evolve_schedule,
    set_evolve_mode,
    set_evolve_theme,
    synthesize_evolve_candidates_from_evidence,
)
from enoch.evolution.evidence import (
    EVIDENCE_SOURCES,
    EvidenceError,
    EvidenceScanResult,
    save_evidence_batch_size,
    scan_evidence,
)
from enoch.evolution.curation import (
    REMOVE_CLASSIFICATIONS,
    curation_evidence_refs,
    latest_remove_suggestion,
    recent_completion_evidence,
)
from enoch.evolution.events import (
    EvolveEvent,
    close_open_proposals,
    latest_open_proposal_id,
    record_evolve_event,
)
from enoch.app.epoch import (
    DaemonEpoch,
    StaleDaemonEpoch,
    begin_daemon_epoch,
    require_current_daemon_epoch,
)
from enoch.app.effects import DaemonEffectFence
from enoch.app.notifications import (
    NotificationDeliveryService,
    NotificationResult,
)
from enoch.vcs_tools import (
    VcsError,
    changed_files,
    delete_branch,
    current_branch,
    ensure_clean_worktree,
    switch_branch,
)
from enoch.workflows import (
    LocalWorkflowEngine,
    WorkflowEngine,
    validate_workflow_engine,
)
from enoch.formatting import (
    format_doctor_result,
    format_pr_result,
    format_publish_result,
    format_remote_publish_result,
    pr_step_update,
    pr_summary,
    publish_summary,
    remote_publish_summary,
    summarize_for_log,
)
from enoch.providers.contracts import (
    LocalPublishResult,
    PullRequestResult,
    RemotePublishResult,
)
from enoch.providers.forge import (
    close_pull_request,
    create_pull_request,
    feature_title,
    format_evolution_provenance,
    inspect_pull_request,
    inspect_pull_request_merge,
    list_open_pull_requests,
    merge_pull_request,
    prepare_local_publish,
    push_current_branch,
)
from enoch.identity import Identity, identity_file_path, load_identity
from enoch.instance import instance_branch
from enoch.immune import ImmuneResult, run_immune_system
from enoch.learn import (
    LearnError,
    learning_assessment_prompt,
    learn_command,
    load_published_skill,
    parse_learn_request,
    parse_learning_assessment,
)
from enoch.lineage.core import (
    ASSESSMENT_ASSESSED,
    ASSESSMENT_FAILED,
    find_parent_inbox_candidate,
    format_inheritance_scan_queued,
    format_lineage_assessment_complete,
    format_parent_inherit_report,
    LineageError,
    STATUS_ADOPTED,
    STATUS_LINKED,
    lineage_adaptation_request,
    lineage_candidate_context,
    link_inbox_candidate,
    load_inbox_candidates,
    load_lineage_inbox_report,
    refresh_lineage_inbox,
    resolve_lineage,
)
from enoch.lineage.assessment import (
    LineageAssessmentProgress,
    assess_lineage_inbox,
    lineage_assessment_candidates,
)
from enoch.lineage.assessment_queue import (
    LineageAssessmentJob,
    claim_lineage_assessment,
    complete_lineage_assessment,
    enqueue_lineage_assessment,
    fail_lineage_assessment,
    load_lineage_assessment_queue,
)
from enoch.lineage.lifecycle import (
    lineage_context_source,
    reconcile_lineage_adoptions,
)
from enoch.logs import log_conversation_turn, log_system_event, system_log_dirs
from enoch.memory.paths import clean_text
from enoch.memory.prompt import memory_for_prompt
from enoch.memory.store import ensure_long_term_memory, remember_memory
from enoch.paths import repo_root, storage_layout
from enoch.prompt_append import (
    TaskRegressionSignal,
    extract_edit_request,
    extract_memory_requests,
    extract_task_regression_signals,
    read_only_turn_prompt,
    startup_context_note,
)
from enoch.private_state import assert_private_state_supported
from enoch.providers.contracts import (
    AgentRuntime,
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    AgentRuntimeTimedOut,
    Attachment,
    AuthorizationPolicy,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ConversationId,
    Cursor,
    ForgeProvider,
    ForgeProviderError,
    MessageId,
    RepositoryProvider,
    RepositoryProviderError,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewProvider,
    ReviewProviderError,
    RuntimeExecutionControl,
    normalize_message_id,
)
from enoch.providers.authorization import (
    DEFAULT_TASK_REQUIREMENTS,
    CapabilityAuthorizationError,
    CapabilityAuthorizer,
    CompositeAuthorizationPolicy,
)
from enoch.providers import as_repository_provider, as_review_provider
from enoch.providers.registry import ProviderError, load_provider
from enoch.providers.runtime import (
    FunctionAgentRuntime,
    invoke_runtime_respond,
)
from enoch.skills import SkillsError, load_agent_skills
from enoch.profiles import (
    AgentProfile,
    CommandContext,
    CommandSpec,
    LifecycleContext,
    ProfileError,
    PromptContext,
    PromptPurpose,
    load_profile,
)
from enoch.profiles.contracts import extend_prompt
from enoch.extensions import (
    ExtensionCommandContext,
    ExtensionCommandSpec,
    AgentExtension,
    AgentExtensionError,
    ExtensionLifecycleContext,
    ExtensionWorkflow,
    extension_storage,
    load_extensions,
)
from enoch.extensions.events import (
    acknowledge_extension_task_event,
    undelivered_extension_task_events,
)
from enoch.runtime import DEFAULT_BRANCH
from enoch.tasks.queue import (
    TaskJob,
    TaskQueueStatus,
    TaskAlreadyExists,
    TaskRetryError,
)
from enoch.commands import (
    CoreCommand,
    action_lock_message as _format_action_lock_message,
    config_command,
    core_command,
    core_command_names,
    doctor_command,
    help_message as _help_message,
    identity_summary,
    inherit_command,
    lineage_command,
    mission_command,
    pr_usage,
    skills_command,
    status_message,
    worktree_usage,
)
from enoch.tasks.config import format_task_timeout, task_timeout_seconds
from enoch.tasks.failures import (
    TaskFailure,
    automatic_retry_delay_seconds,
    classify_task_failure,
)
from enoch.tasks.worktree import (
    TaskWorktree,
    TaskWorktreeState,
    list_task_worktrees,
    prepare_existing_revision_workspace,
    prepare_task_worktree,
    remove_managed_task_worktree,
    remove_task_worktree,
    task_worktree_state,
)
from enoch.operations.update_tools import (
    authoritative_branch_name as _authoritative_branch_name,
    schedule_daemon_restart as _schedule_daemon_restart,
    task_branch_base as _task_branch_base,
)
from enoch.operations.updater import update_from_authoritative
from enoch.app.models import (
    ForgeMaintenanceRequest,
    ShutdownRequested,
    TaskContextSnapshot,
    TaskDeadline,
    WorkStatusMessage,
    WorkOutcome,
)
from enoch.app.execution_context import (
    CURRENT_REGRESSION_SIGNALS as _CURRENT_REGRESSION_SIGNALS,
    CURRENT_TASK_ID as _CURRENT_TASK_ID,
    CURRENT_TASK_WORKER_ID as _CURRENT_TASK_WORKER_ID,
    CURRENT_WORK_STATUS as _CURRENT_WORK_STATUS,
)
from enoch.app.inbox import (
    InboxReceipt,
    acknowledge_event,
    begin_event,
    complete_event,
    fail_event,
    mark_reply_sent,
)
from enoch.app.parsing import (
    backlog_item_id as _backlog_item_id,
    backlog_priority_and_request as _backlog_priority_and_request,
    backlog_priority_update as _backlog_priority_update,
    cron_job_id as _cron_job_id,
    parse_chat_command as _parse_chat_command,
    task_cancel_id as _task_cancel_id,
    task_resume_target as _task_resume_target,
    task_retry_id as _task_retry_id,
    unquote_schedule_text as _unquote_schedule_text,
)
from enoch.app.presentation import (
    backlog_usage as _backlog_usage,
    clip_activity_text as _clip_activity_text,
    cron_usage as _cron_usage,
    evolve_usage as _evolve_usage,
    final_task_status_update as _final_task_status_update,
    format_elapsed as _format_elapsed,
    format_open_reviews as _format_open_reviews,
    format_review as _format_review,
    format_review_land_result as _format_review_land_result,
    format_task_final_message as _format_task_final_message,
    format_work_status_message as _format_work_status_message,
)
from enoch.app.reporting import (
    _evolve_check_reason,
    _evolve_skip_reason,
    _format_backlog_report,
    _format_cron_report,
    _format_evolve_candidate,
    _format_evolve_candidates,
    _format_evolve_config,
    _format_evidence_report,
    _format_evidence_scan_results,
    _format_evolve_proposal,
    _format_evolve_report,
    _format_evolve_theme,
    _format_tasks_report,
    _task_status_message,
)
from enoch.app.task_workflow import (
    TaskWorkflow,
    TaskWorkflowDependencies,
    action_sandbox as _action_sandbox,
    activity_sync_note as _activity_sync_note,
    coerce_work_outcome as _coerce_work_outcome,
    evolution_provenance_for_job as _evolution_provenance_for_job,
    sandbox_description as _sandbox_description,
    work_reply_failed as _work_reply_failed,
)


TASK_CONTEXT_SOURCE_CHAT = "chat-snapshot"
NEEDS_CLARIFICATION_PREFIX = "NEEDS_CLARIFICATION:"
NO_EXTRA_TASK_CONTEXT = "No extra context needed."
_CURRENT_EVENT_KEY: ContextVar[str] = ContextVar("enoch_event_key", default="")


def _load_provider_cursor(name: str, root: Path | None = None) -> Cursor | None:
    return load_channel_cursor(name, root)


def _save_provider_cursor(name: str, cursor: Cursor, root: Path | None = None) -> None:
    save_channel_cursor(name, cursor, root)


def _begin_lifecycle_run(root: Path | None = None, *, provider: str = "chat") -> str:
    return begin_channel_lifecycle(provider, root)


def _record_lifecycle_shutdown(
    root: Path | None,
    reason: str,
    *,
    shutdown_notification_sent: bool,
    provider: str = "chat",
) -> None:
    record_channel_shutdown(
        provider,
        root,
        reason,
        shutdown_notification_sent=shutdown_notification_sent,
    )


def _startup_message(
    identity: Identity,
    root: Path | None = None,
    previous_shutdown_warning: str = "",
    *,
    provider: str = "chat",
) -> str:
    return channel_startup_message(identity, provider, root, previous_shutdown_warning)


def _shutdown_message(
    identity: Identity,
    root: Path | None = None,
    reason: str = "shutdown",
    *,
    provider: str = "chat",
) -> str:
    del root
    return channel_shutdown_message(identity, provider, reason)


class EnochApplication:
    def __init__(
        self,
        identity: Identity,
        root: Path,
        client: ChatProvider,
        previous_shutdown_warning: str = "",
        *,
        runtime: AgentRuntime | None = None,
        forge: ForgeProvider | None = None,
        repository: RepositoryProvider | None = None,
        review: ReviewProvider | None = None,
        profile: AgentProfile | None = None,
        extensions: tuple[AgentExtension, ...] = (),
        daemon_epoch: DaemonEpoch | None = None,
        workflow: WorkflowEngine | None = None,
        authorization_policy: AuthorizationPolicy | None = None,
    ) -> None:
        self.identity = identity
        self.root = root
        self.storage = storage_layout(root)
        assert_private_state_supported(root)
        self.client = client
        self.channel_name = _chat_provider_name(client)
        self.daemon_epoch = daemon_epoch or begin_daemon_epoch(
            root,
            provider=self.channel_name,
        )
        self.runtime = runtime or FunctionAgentRuntime(
            respond_fn=lambda *args, **kwargs: respond(*args, **kwargs),
            act_in_session_fn=lambda *args, **kwargs: act_in_session(*args, **kwargs),
            model_summary_fn=lambda root=None: model_summary(root),
            model_options_fn=lambda: codex_model_options(),
            reset_usage_fn=lambda: reset_token_usage(),
            reasoning_efforts_fn=lambda root=None: codex_reasoning_efforts(root),
        )
        selected_forge = forge
        if selected_forge is None and review is None:
            selected_forge = load_provider("forge", root)
        self.repository = repository or as_repository_provider(load_provider("vcs", root))
        self.review = review or as_review_provider(selected_forge)
        self.profile = profile or AgentProfile(name="enoch")
        self.extensions = tuple(extensions)
        policies = tuple(
            policy
            for policy in (self.profile.authorization, authorization_policy)
            if policy is not None
        )
        self.authorization = CapabilityAuthorizer(
            self._provider_for_authorization,
            policy=CompositeAuthorizationPolicy(policies) if policies else None,
            profile_name=self.profile.name,
        )
        self.effect_fence = DaemonEffectFence(
            root,
            self.daemon_epoch,
            authorizer=self.authorization,
        )
        self.notifications = NotificationDeliveryService(
            client,
            self.channel_name,
            root,
            self.daemon_epoch,
            self.authorization,
        )
        self._notification_order_lock = threading.RLock()
        self.workflow = validate_workflow_engine(
            workflow or LocalWorkflowEngine(root, epoch=self.daemon_epoch)
        )
        self._validate_profile_commands()
        self._validate_extension_commands()
        self.previous_shutdown_warning = previous_shutdown_warning
        self.offset: Cursor | None = _load_provider_cursor(self.channel_name, root)
        self._restart_after_reply = False
        self._pending_session_syncs: list[tuple[int, str]] = []
        self._task_worker: threading.Thread | None = None
        self._task_worker_lock = threading.Lock()
        self._direct_workers: dict[int, threading.Thread] = {}
        self._cron_scheduler_thread: threading.Thread | None = None
        self._cron_scheduler_lock = threading.Lock()
        self._cron_scheduler_stop = threading.Event()
        self._cron_scheduler_wake = threading.Event()
        self._startup_hook_lock = threading.Lock()
        self._startup_hooks_ran = False
        self._extension_task_event_lock = threading.Lock()
        self._lineage_worker: threading.Thread | None = None
        self._lineage_worker_lock = threading.Lock()
        self._task_cancellations: dict[int, threading.Event] = {}
        self._stopping = False
        self._resident_branch = instance_branch(root)
        self._task_workflow = TaskWorkflow(
            self,
            dependencies=TaskWorkflowDependencies(
                run_immune_system=lambda *args, **kwargs: run_immune_system(
                    *args,
                    **kwargs,
                ),
                prepare_existing_revision_workspace=lambda *args, **kwargs: (
                    prepare_existing_revision_workspace(*args, **kwargs)
                ),
                ensure_clean_worktree=lambda *args, **kwargs: ensure_clean_worktree(
                    *args,
                    **kwargs,
                ),
                feature_title=lambda *args, **kwargs: feature_title(*args, **kwargs),
                current_branch=lambda *args, **kwargs: current_branch(*args, **kwargs),
                switch_branch=lambda *args, **kwargs: switch_branch(*args, **kwargs),
                delete_branch=lambda *args, **kwargs: delete_branch(*args, **kwargs),
            ),
        )
        recovered = _recover_running_task_from_direct_action_log(
            root,
            workflow=self.workflow,
        )
        if recovered is None:
            recovered = self.workflow.recover()
        _cleanup_completed_task_worktree(recovered, root)
        self._work_status_messages: dict[int, MessageId] = _load_task_status_messages(
            self.workflow
        )
        self.notifications.recover()
        self._run_profile_hook("on_initialize")
        self._run_extension_hooks("on_initialize")

    def _provider_for_authorization(self, kind: str) -> object:
        if kind == "chat":
            return self.client
        if kind == "runtime":
            return self.runtime
        if kind == "forge":
            return self.review
        if kind == "vcs":
            return self.repository
        return load_provider(kind, self.root)

    def run_forever(self) -> None:
        self.start()
        self._start_cron_scheduler()
        try:
            while True:
                try:
                    self.run_once()
                except ShutdownRequested:
                    raise
                except StaleDaemonEpoch:
                    raise
                except Exception as error:
                    print(f"Enoch {provider_label(self.channel_name)} polling error: {error}")
                    time.sleep(5)
        finally:
            self._stop_cron_scheduler()

    def notify_startup(self) -> None:
        self.start()
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return
        result = self._deliver_message(
            chat_id,
            _startup_message(
                self.identity,
                self.root,
                self.previous_shutdown_warning,
                provider=self.channel_name,
            ),
            notification_key=f"daemon:{self.daemon_epoch.generation}:startup",
        )
        if not result.delivered:
            raise ChatProviderError(result.error or "Startup notification was not delivered.")
        _sync_session_activity(
            self.identity,
            self.root,
            chat_id,
            startup_context_note(memory_for_prompt(self.root)),
            runtime=self.runtime,
            session_key=self._session_key(chat_id),
            effect_fence=self.effect_fence,
        )

    def start(self) -> None:
        """Run process-start hooks once, independently of chat notification."""

        run_hooks = False
        with self._startup_hook_lock:
            if not self._startup_hooks_ran:
                self._startup_hooks_ran = True
                run_hooks = True
        if run_hooks:
            self._run_profile_hook("on_startup")
            self._run_extension_hooks("on_startup")
        self._drain_extension_task_events()

    def notify_shutdown(self, reason: str) -> None:
        self._drain_extension_task_events()
        self._run_extension_hooks("on_shutdown", reverse=True)
        self._run_profile_hook("on_shutdown")
        _record_system_event("shutdown", self.root, details={"reason": reason})
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return
        result = self._deliver_message(
            chat_id,
            _shutdown_message(self.identity, self.root, reason, provider=self.channel_name),
            notification_key=f"daemon:{self.daemon_epoch.generation}:shutdown",
        )
        if not result.delivered:
            raise ChatProviderError(result.error or "Shutdown notification was not delivered.")

    def run_once(self) -> None:
        require_current_daemon_epoch(self.daemon_epoch, self.root)
        self._maybe_start_lineage_worker()
        self._run_profile_hook("before_run")
        self._run_extension_hooks("before_run")
        try:
            recovered = _recover_running_task_from_direct_action_log(
                self.root,
                workflow=self.workflow,
            )
            if recovered is None:
                recovered = self.workflow.recover()
            _cleanup_completed_task_worktree(recovered, self.root)
            self.effect_fence.authorize(
                "chat.receive",
                ("chat.receive",),
            )
            for event in self.client.receive(self.offset):
                self.handle_event(event)
            self._run_due_evidence_scans()
            self._run_due_evolve_schedule()
            self._maybe_start_task_worker()
            self._maybe_start_lineage_worker()
        finally:
            self._drain_extension_task_events()
            self._run_extension_hooks("after_run", reverse=True)
            self._run_profile_hook("after_run")

    def handle_event(self, event: ChatEvent) -> None:
        require_current_daemon_epoch(self.daemon_epoch, self.root)
        chat_id = event.conversation_id
        message_id = event.message_id
        if not self._chat_allowed(chat_id):
            self._remember_update_offset(event.cursor)
            return

        receipt = begin_event(self.channel_name, event, self.root)
        if receipt.completed:
            self._finish_chat_event(event, receipt)
            return

        event_token = _CURRENT_EVENT_KEY.set(receipt.key)
        try:
            reply, logged_input = self._dispatch_chat_event(event)
            receipt = complete_event(
                self.channel_name,
                receipt.key,
                self.root,
                reply=reply,
                logged_input=logged_input,
            )
        except StaleDaemonEpoch:
            raise
        except Exception as error:
            failed = fail_event(self.channel_name, receipt.key, str(error), self.root)
            print(
                f"Enoch could not process {self.channel_name} update "
                f"{receipt.key[:12]} (attempt {failed.attempts}/3): {error}"
            )
            if not failed.exhausted:
                return
            receipt = complete_event(
                self.channel_name,
                receipt.key,
                self.root,
                reply=(
                    "Enoch skipped this update after three failed processing attempts. "
                    f"The failure was recorded for debugging: {type(error).__name__}."
                ),
                logged_input=event.text.strip(),
            )
        finally:
            _CURRENT_EVENT_KEY.reset(event_token)
        self._finish_chat_event(event, receipt)

    def _dispatch_chat_event(self, event: ChatEvent) -> tuple[str, str]:
        chat_id = event.conversation_id
        text = event.text.strip()
        self._safe_send_read_ack(chat_id, event.message_id)
        try:
            self.authorization.require(
                "runtime.reset-usage",
                ("runtime.respond",),
            )
        except CapabilityAuthorizationError:
            pass
        else:
            self.runtime.reset_usage()
        image = select_image_attachment(event.attachments)
        logged_input = text
        if image is not None:
            reply = self._respond_to_image(chat_id, image, text)
            logged_input = f"[{provider_label(self.channel_name)} image]" + (
                f" {text}" if text else ""
            )
            return reply, logged_input

        command, argument = _parse_chat_command(text)
        work_text = _with_replied_text_context(
            text,
            event.replied_text,
            provider_name=_chat_provider_name(self.client),
        )
        profile_command = self.profile.command(command) if command else None
        extension_command = self._extension_command(command) if command else None
        registered_command = core_command(command) if command else None
        if profile_command is not None:
            reply = self._run_profile_command(profile_command, event, command, argument)
        elif extension_command is not None:
            extension, spec = extension_command
            reply = self._run_extension_command(
                extension,
                spec,
                event,
                command,
                argument,
            )
        elif registered_command is not None:
            reply = self._run_core_command(
                registered_command,
                event,
                text=text,
                argument=argument,
                work_text=work_text,
            )
        else:
            reply = self._natural(chat_id, text)
        return reply, logged_input

    def _finish_chat_event(self, event: ChatEvent, receipt: InboxReceipt) -> None:
        if not receipt.reply_sent:
            delivery = (
                self._deliver_message(
                    event.conversation_id,
                    receipt.reply,
                    notification_key=f"inbox:{receipt.key}:reply",
                )
                if receipt.reply
                else NotificationResult(delivered=True)
            )
            if not delivery.delivered:
                details = {
                    "provider": self.channel_name,
                    "chat_id": event.conversation_id,
                    "message_id": event.message_id,
                    "error": delivery.error,
                }
                if delivery.terminal:
                    details["terminal"] = True
                _record_system_event(
                    "chat_reply_failed",
                    self.root,
                    status="failed",
                    details=details,
                )
                if not delivery.terminal:
                    return
            self._record_turn(
                event.conversation_id,
                receipt.logged_input,
                receipt.reply,
            )
            self._flush_session_syncs()
            mark_reply_sent(self.channel_name, receipt.key, self.root)
        self._remember_update_offset(event.cursor)
        acknowledge_event(self.channel_name, receipt.key, self.root)
        if self._restart_after_reply:
            self._restart_after_reply = False
            _schedule_daemon_restart(self.root)
            return
        self._maybe_start_lineage_worker()

    def _remember_update_offset(self, offset: Cursor | None) -> None:
        if offset is None:
            return
        self.offset = offset
        _save_provider_cursor(self.channel_name, offset, self.root)

    def _chat_allowed(self, chat_id: ConversationId) -> bool:
        allowed = _allowed_conversation_id(self.client)
        return allowed is None or allowed == chat_id

    def _validate_profile_commands(self) -> None:
        conflicts = sorted(
            {
                spec.name
                for spec in self.profile.commands
                if spec.name in core_command_names()
            }
        )
        if conflicts:
            commands = ", ".join(f"/{name}" for name in conflicts)
            raise ProfileError(
                f"Profile {self.profile.name} conflicts with core commands: {commands}."
            )

    def _validate_extension_commands(self) -> None:
        seen_names: set[str] = set()
        seen_commands = set(core_command_names())
        seen_commands.update(spec.name for spec in self.profile.commands)
        for extension in self.extensions:
            if extension.name in seen_names:
                raise AgentExtensionError(
                    f"Duplicate agent extension {extension.name!r}."
                )
            seen_names.add(extension.name)
            conflicts = sorted(
                spec.name
                for spec in extension.commands
                if spec.name in seen_commands
            )
            if conflicts:
                commands = ", ".join(f"/{name}" for name in conflicts)
                raise AgentExtensionError(
                    f"Agent extension {extension.name} conflicts with registered "
                    f"commands: {commands}."
                )
            seen_commands.update(spec.name for spec in extension.commands)

    def _extension_command(
        self,
        command: str,
    ) -> tuple[AgentExtension, ExtensionCommandSpec] | None:
        for extension in self.extensions:
            spec = extension.command(command)
            if spec is not None:
                return extension, spec
        return None

    def _help(self, topic: str) -> str:
        profile_command = self.profile.command(topic) if topic.strip() else None
        if profile_command is not None:
            return profile_command.usage or (
                f"{profile_command.command} - {profile_command.summary}"
            )
        extension_command = self._extension_command(topic) if topic.strip() else None
        if extension_command is not None:
            _, spec = extension_command
            return spec.usage or f"{spec.command} - {spec.summary}"
        core_help = _help_message(topic, chat_provider=self.channel_name)
        if topic.strip():
            return core_help
        sections = [core_help]
        if self.profile.commands:
            sections.append(
                "\n".join(
                    [
                        f"{self.profile.help_heading}:",
                        *(
                            f"{spec.command} - {spec.summary}"
                            for spec in self.profile.commands
                        ),
                    ]
                )
            )
        sections.extend(
            "\n".join(
                [
                    f"{extension.help_heading}:",
                    *(
                        f"{spec.command} - {spec.summary}"
                        for spec in extension.commands
                    ),
                ]
            )
            for extension in self.extensions
            if extension.commands
        )
        return "\n\n".join(sections)

    def _run_core_command(
        self,
        spec: CoreCommand,
        event: ChatEvent,
        *,
        text: str,
        argument: str,
        work_text: str,
    ) -> str:
        handlers = self._core_command_handlers(
            event,
            text=text,
            argument=argument,
            work_text=work_text,
        )
        handler = handlers.get(spec.handler)
        if handler is None:
            raise RuntimeError(
                f"Core command /{spec.name} has unknown handler {spec.handler!r}."
            )
        return handler()

    def _core_command_handlers(
        self,
        event: ChatEvent,
        *,
        text: str,
        argument: str,
        work_text: str,
    ) -> dict[str, Callable[[], str]]:
        chat_id = event.conversation_id
        return {
            "start": lambda: "\n".join(
                [
                    "Enoch is ready.",
                    "Use /help to see every command.",
                    "Use /help <command> for detailed usage and subcommands.",
                ]
            ),
            "help": lambda: self._help(argument),
            "ancestors": lambda: self._ancestors(chat_id, text),
            "inherit": lambda: self._inherit(chat_id, text),
            "mission": lambda: self._mission(text),
            "skills": lambda: self._skills(text),
            "learn": lambda: self._learn(chat_id, text),
            "do": lambda: self._do(chat_id, work_text),
            "task": lambda: self._task(chat_id, work_text),
            "queue": lambda: _format_tasks_report(
                self.root,
                task_status=self.workflow.inspect(),
            ),
            "stop": self._stop_running_job,
            "backlog": lambda: self._backlog(chat_id, work_text),
            "cron": lambda: self._cron(chat_id, work_text),
            "evolve": lambda: self._evolve(chat_id, argument),
            "config": lambda: config_command(
                text,
                self.root,
                runtime=self.runtime,
                active_profile_name=self.profile.name,
            ),
            "self": lambda: identity_summary(self.identity, self.root),
            "status": lambda: self._status(chat_id),
            "doctor": self._doctor,
            "worktree": lambda: self._worktree(chat_id, argument),
            "pr": lambda: self._pr(chat_id, argument),
            "update": lambda: self._update_from_chat(chat_id),
            "restart": self._restart_from_chat,
        }

    def _update_from_chat(self, chat_id: ConversationId) -> str:
        reply = self._update()
        self._queue_session_sync(
            chat_id,
            _activity_sync_note(
                "User ran /update.",
                f"Result: {_clip_activity_text(reply)}",
            ),
        )
        return reply

    def _run_profile_command(
        self,
        spec: CommandSpec,
        event: ChatEvent,
        command: str,
        argument: str,
    ) -> str:
        enqueue_index = 0

        def queue(
            request: str,
            context: str,
            requirements,
        ) -> TaskJob:
            nonlocal enqueue_index
            enqueue_index += 1
            return self.workflow.enqueue(
                event.conversation_id,
                request,
                context=context,
                context_source=f"profile:{self.profile.name}" if context else "",
                source="task",
                initiated_by="human",
                event_actor="human",
                trigger=command,
                idempotency_key=_event_idempotency_key(
                    f"profile:{self.profile.name}:{command}:{enqueue_index}"
                ),
                **self._profile_task_options(requirements.capabilities),
            )

        context = CommandContext(
            identity=self.identity,
            root=self.root,
            storage=self.storage,
            conversation_id=event.conversation_id,
            event=event,
            command=command,
            argument=argument,
            runtime=self.runtime,
            repository=self.repository,
            review=self.review,
            _enqueue=queue,
        )
        try:
            self.authorization.require(
                f"profile-command:{spec.name}",
                spec.required_capabilities,
                metadata={"command": command},
            )
            return str(spec.handler(context))
        except CapabilityAuthorizationError as error:
            return str(error)
        except Exception as error:
            _record_system_event(
                "profile_command_failed",
                self.root,
                details={
                    "profile": self.profile.name,
                    "command": command,
                    "error": str(error),
                },
            )
            return f"Profile command {command} failed: {error}"

    def _extension_workflow(self, extension: AgentExtension) -> ExtensionWorkflow:
        return ExtensionWorkflow.from_engine(
            extension_name=extension.name,
            engine=self.workflow,
            task_options=self._profile_task_options(),
        )

    def _run_extension_command(
        self,
        extension: AgentExtension,
        spec: ExtensionCommandSpec,
        event: ChatEvent,
        command: str,
        argument: str,
    ) -> str:
        context = ExtensionCommandContext(
            identity=self.identity,
            root=self.root,
            storage=extension_storage(self.storage, extension.name),
            conversation_id=event.conversation_id,
            event=event,
            command=command,
            argument=argument,
            runtime=self.runtime,
            repository=self.repository,
            review=self.review,
            workflow=self._extension_workflow(extension),
        )
        try:
            self.authorization.require(
                f"extension-command:{extension.name}:{spec.name}",
                spec.required_capabilities,
                metadata={
                    "extension": extension.name,
                    "command": command,
                },
            )
            return str(spec.handler(context))
        except CapabilityAuthorizationError as error:
            return str(error)
        except Exception as error:
            _record_system_event(
                "agent_extension_command_failed",
                self.root,
                details={
                    "extension": extension.name,
                    "command": command,
                    "error": str(error),
                },
            )
            return f"Extension command {command} failed: {error}"

    def _profile_task_options(
        self,
        extra_capabilities: tuple[str, ...] = (),
    ) -> dict[str, object]:
        options: dict[str, object] = self.profile.workflow.task_options()
        options["required_capabilities"] = tuple(
            dict.fromkeys(
                (
                    *DEFAULT_TASK_REQUIREMENTS.capabilities,
                    *extra_capabilities,
                )
            )
        )
        return options

    def _authorize_task(self, job: TaskJob) -> None:
        self.authorization.require(
            "task.execute",
            job.required_capabilities or DEFAULT_TASK_REQUIREMENTS,
            task_id=job.id,
            metadata={
                "source": job.source,
                "trigger": job.trigger,
            },
        )

    def _profile_status_name(self) -> str:
        display_name = self.profile.display_name
        if display_name == self.profile.name:
            return self.profile.name
        return f"{display_name} ({self.profile.name})"

    def _format_work_status(self, status: WorkStatusMessage) -> str:
        return _format_work_status_message(
            status,
            task_label=self.profile.presentation.task_label,
        )

    def _format_task_final(
        self,
        job: TaskJob,
        final_status: str,
        result: str,
    ) -> str:
        return _format_task_final_message(
            job,
            final_status,
            result,
            task_label=self.profile.presentation.task_label,
        )

    def _profile_prompt(
        self,
        prompt: str,
        *,
        purpose: PromptPurpose,
        chat_id: ConversationId,
    ) -> str:
        try:
            return extend_prompt(
                prompt,
                self.profile,
                PromptContext(
                    identity=self.identity,
                    root=self.root,
                    storage=self.storage,
                    purpose=purpose,
                    conversation_id=chat_id,
                    prompt=prompt,
                ),
            )
        except ProfileError as error:
            _record_system_event(
                "profile_prompt_failed",
                self.root,
                details={
                    "profile": self.profile.name,
                    "purpose": purpose,
                    "error": str(error),
                },
            )
            return prompt

    def _run_profile_hook(self, name: str) -> None:
        hook = getattr(self.profile.lifecycle, name)
        if hook is None:
            return
        try:
            hook(
                LifecycleContext(
                    identity=self.identity,
                    root=self.root,
                    storage=self.storage,
                    chat=self.client,
                    runtime=self.runtime,
                    repository=self.repository,
                    review=self.review,
                )
            )
        except Exception as error:
            _record_system_event(
                "profile_lifecycle_failed",
                self.root,
                details={
                    "profile": self.profile.name,
                    "hook": name,
                    "error": str(error),
                },
            )

    def _run_extension_hooks(self, name: str, *, reverse: bool = False) -> None:
        extensions = reversed(self.extensions) if reverse else self.extensions
        for extension in extensions:
            hook = getattr(extension.lifecycle, name)
            if hook is None:
                continue
            try:
                hook(self._extension_lifecycle_context(extension))
            except Exception as error:
                _record_system_event(
                    "agent_extension_lifecycle_failed",
                    self.root,
                    details={
                        "extension": extension.name,
                        "hook": name,
                        "error": str(error),
                    },
                )

    def _drain_extension_task_events(self) -> None:
        if not self._extension_task_event_lock.acquire(blocking=False):
            return
        try:
            for extension in self.extensions:
                hook = extension.lifecycle.on_task_event
                if hook is None:
                    continue
                storage = extension_storage(self.storage, extension.name)
                for event in undelivered_extension_task_events(
                    self.workflow.root,
                    storage,
                    extension.name,
                ):
                    try:
                        require_current_daemon_epoch(
                            self.daemon_epoch,
                            self.root,
                        )
                        hook(
                            self._extension_lifecycle_context(extension),
                            event,
                        )
                        require_current_daemon_epoch(
                            self.daemon_epoch,
                            self.root,
                        )
                        acknowledge_extension_task_event(storage, event)
                    except StaleDaemonEpoch:
                        raise
                    except Exception as error:
                        _record_system_event(
                            "agent_extension_task_event_failed",
                            self.root,
                            status="failed",
                            details={
                                "extension": extension.name,
                                "event_id": event.id,
                                "task_id": event.task_id,
                                "task_event": event.event,
                                "error": str(error),
                            },
                        )
                        break
        finally:
            self._extension_task_event_lock.release()

    def _extension_lifecycle_context(
        self,
        extension: AgentExtension,
    ) -> ExtensionLifecycleContext:
        return ExtensionLifecycleContext(
            identity=self.identity,
            root=self.root,
            storage=extension_storage(self.storage, extension.name),
            chat=self.client,
            runtime=self.runtime,
            repository=self.repository,
            review=self.review,
            workflow=self._extension_workflow(extension),
        )

    def _session_key(self, chat_id: ConversationId) -> str:
        provider = _chat_provider_name(self.client)
        return f"{provider}:{chat_id}"

    def _invoke_runtime_response(
        self,
        prompt: str,
        *,
        execution: RuntimeExecutionControl,
        image_paths: tuple[Path, ...] = (),
    ):
        return self.effect_fence.run_runtime_authorized(
            "runtime.respond",
            ("runtime.respond",),
            lambda fenced_execution: invoke_runtime_respond(
                self.runtime,
                self.identity,
                prompt,
                cwd=self.root,
                image_paths=image_paths,
                execution=fenced_execution,
            ),
            execution,
            task_id=_CURRENT_TASK_ID.get(),
        )

    def _respond_read_only_turn(
        self,
        chat_id: ConversationId,
        text: str,
        *,
        session_key: str | None = None,
    ) -> str:
        resolved_session_key = session_key or self._session_key(chat_id)
        try:
            return self._invoke_runtime_response(
                self._profile_prompt(
                    read_only_turn_prompt(text),
                    purpose="conversation",
                    chat_id=chat_id,
                ),
                execution=RuntimeExecutionControl(
                    request_id=f"conversation:{chat_id}",
                    session_key=resolved_session_key,
                ),
            ).final_text
        except (
            AgentRuntimeError,
            CapabilityAuthorizationError,
            TypeError,
        ) as error:
            return str(error)

    def _respond_isolated_evidence_turn(
        self,
        chat_id: ConversationId,
        prompt: str,
        *,
        phase: str,
    ) -> str:
        return self._invoke_runtime_response(
            self._profile_prompt(
                prompt,
                purpose="evidence",
                chat_id=chat_id,
            ),
            execution=RuntimeExecutionControl(
                request_id=f"evidence:{phase}:{uuid4().hex}",
                session_key="",
            ),
        ).final_text

    def _respond_to_image(
        self,
        chat_id: ConversationId,
        image: Attachment,
        caption: str,
    ) -> str:
        try:
            self.effect_fence.authorize(
                "chat.attachment",
                ("chat.attachment", "runtime.respond"),
            )
            with temporary_image_attachment(
                self.client,
                image,
                self.root,
                channel_name=self.channel_name,
            ) as image_path:
                return self._invoke_runtime_response(
                    self._profile_prompt(
                        image_prompt(caption, self.channel_name),
                        purpose="image",
                        chat_id=chat_id,
                    ),
                    image_paths=(image_path,),
                    execution=RuntimeExecutionControl(
                        request_id=f"image:{chat_id}",
                        session_key=self._session_key(chat_id),
                        progress_callback=lambda progress: self._send_progress(
                            chat_id,
                            progress.elapsed_seconds,
                            progress.sandbox,
                        ),
                    ),
                ).final_text
        except (
            AgentRuntimeError,
            CapabilityAuthorizationError,
            TypeError,
            OSError,
            ChatProviderError,
            ChannelAttachmentError,
        ) as error:
            return f"Enoch could not view that image: {error}"

    def _queue_session_sync(self, chat_id: ConversationId | None, note: str) -> None:
        if chat_id is None or not note.strip():
            return
        self._pending_session_syncs.append((chat_id, note.strip()))

    def _flush_session_syncs(self) -> None:
        pending = self._pending_session_syncs
        self._pending_session_syncs = []
        for chat_id, note in pending:
            _sync_session_activity(
                self.identity,
                self.root,
                chat_id,
                note,
                runtime=self.runtime,
                session_key=self._session_key(chat_id),
                effect_fence=self.effect_fence,
            )

    def _natural(self, chat_id: ConversationId, text: str) -> str:
        return self._natural_with_session(chat_id, text, session_key=self._session_key(chat_id))

    def _natural_with_session(
        self,
        chat_id: ConversationId,
        text: str,
        *,
        session_key: str,
    ) -> str:
        reply = self._respond_read_only_turn(chat_id, text, session_key=session_key)
        regression_result = extract_task_regression_signals(reply)
        self._apply_task_regression_signals(regression_result.signals)
        reply = regression_result.visible_reply
        memory_result = extract_memory_requests(reply)
        reply = memory_result.visible_reply
        edit_request = extract_edit_request(reply)
        if edit_request is not None:
            reply = edit_request.visible_reply
        memory_note = self._save_memory_requests(memory_result.requests)
        return "\n\n".join(part for part in [reply, memory_note] if part)

    def _do(self, chat_id: ConversationId, text: str) -> str:
        command, argument = _parse_chat_command(text)
        if command != "/do" or not argument:
            return "Use /do <request> to run work now."
        if not self.profile.workflow.allow_direct_work:
            return (
                f"Profile {self.profile.display_name} does not permit immediate "
                "/do work. Use /task <request> to queue it."
            )
        if not self._action_allowed():
            return self._action_lock_message()
        queue_status = self.workflow.inspect()
        if queue_status.paused_count:
            return (
                "Enoch has paused tasks. Restore agent runtime access and use "
                "/task resume <id|all> before starting /do."
            )
        running = queue_status.running
        snapshot = self._resolve_task_context_snapshot(chat_id, argument)
        if snapshot.codex_unavailable_reason:
            return self._queue_paused_request(
                chat_id,
                argument,
                source="chat-task",
                trigger="/do",
                reason=snapshot.codex_unavailable_reason,
            )
        if snapshot.error:
            return f"Enoch could not prepare conversation context for that /do request yet: {snapshot.error}"
        if snapshot.clarification:
            return f"Enoch needs one clarification before running that: {snapshot.clarification}"
        if running is not None:
            return self._queue_direct_work_next(
                chat_id,
                argument,
                running,
                context=snapshot.context,
                context_source=snapshot.source,
            )
        return self._run_direct_work_with_status(
            chat_id,
            argument,
            context=snapshot.context,
            context_source=snapshot.source,
        )

    def _resolve_task_context_snapshot(
        self,
        chat_id: ConversationId,
        request: str,
    ) -> TaskContextSnapshot:
        try:
            reply = self._invoke_runtime_response(
                self._profile_prompt(
                    _task_context_snapshot_prompt(request, provider=self.channel_name),
                    purpose="task-context",
                    chat_id=chat_id,
                ),
                execution=RuntimeExecutionControl(
                    request_id=f"task-context:{chat_id}",
                    session_key=self._session_key(chat_id),
                ),
            ).final_text
        except AgentRuntimeAccessUnavailable as error:
            return TaskContextSnapshot(codex_unavailable_reason=str(error))
        except (
            AgentRuntimeError,
            CapabilityAuthorizationError,
            TypeError,
        ) as error:
            return TaskContextSnapshot(error=str(error))
        return _parse_task_context_snapshot(reply)

    def _run_direct_work_with_status(
        self,
        chat_id: ConversationId,
        request: str,
        *,
        context: str = "",
        context_source: str = "",
        session_key: str = "",
    ) -> str:
        context = context.strip()
        context_source = context_source.strip()
        try:
            direct_task = self.workflow.enqueue(
                chat_id,
                request,
                mode="direct",
                context=context,
                context_source=context_source,
                idempotency_key=_event_idempotency_key("direct"),
                **self._profile_task_options(),
            )
        except TaskAlreadyExists as duplicate:
            return (
                f"Task #{duplicate.job.id} was already accepted for this chat update "
                f"and is {duplicate.job.status}."
            )
        except RuntimeError:
            running = self.workflow.inspect().running
            if running is not None:
                return f"Enoch is already running task #{running.id}. Use /task <request> to queue this work."
            return "Enoch could not create a task id for this /do job."
        except (OSError, ValueError):
            return "Enoch could not create a task id for this /do job."
        context = direct_task.context
        if not session_key:
            session_key = f"{self._session_key(chat_id)}:do:{direct_task.id}"
        status_message = WorkStatusMessage(
            chat_id=chat_id,
            message_id=0,
            request=request,
            started_at=time.monotonic(),
            task_id=direct_task.id,
            status="running",
            latest_update="Starting work.",
            context=context,
        )
        message_id = self._safe_send_message_id(
            chat_id,
            self._format_work_status(status_message),
            notification_key=f"task:{direct_task.id}:status",
        )
        if message_id is not None:
            self._work_status_messages[direct_task.id] = message_id
            self.workflow.record_status_message(direct_task.id, message_id)
        self._start_direct_work_worker(direct_task, session_key=session_key)
        if message_id is not None:
            return ""
        return f"Started task #{direct_task.id}. Enoch is working on it now."

    def _run_tracked_inline_work(
        self,
        chat_id: int,
        request: str,
        *,
        source: str,
        initiated_by: str,
        trigger: str,
        session_key: str,
    ) -> str:
        if self.workflow.inspect().paused_count:
            return (
                "Enoch has paused tasks. Restore agent runtime access and use "
                "/task resume <id|all> before starting more work."
            )
        try:
            job = self.workflow.enqueue(
                chat_id,
                request,
                mode="direct",
                source=source,
                initiated_by=initiated_by,
                event_actor=initiated_by,
                trigger=trigger,
                idempotency_key=_event_idempotency_key(f"inline:{trigger}"),
                **self._profile_task_options(),
            )
        except TaskAlreadyExists as duplicate:
            return (
                f"Task #{duplicate.job.id} was already accepted for this chat update "
                f"and is {duplicate.job.status}."
            )
        except RuntimeError:
            return "Enoch cannot start that work while another task is running."
        except (OSError, ValueError):
            return "Enoch could not create a tracked task for that work."
        worker_id = f"{os.getpid()}-{uuid4().hex}"
        claimed = self.workflow.claim(job.id, worker_id, os.getpid())
        if claimed is None:
            return f"Enoch could not claim tracked task #{job.id}."
        job = claimed
        task_token = _CURRENT_TASK_ID.set(job.id)
        worker_token = _CURRENT_TASK_WORKER_ID.set(worker_id)
        regression_token = _CURRENT_REGRESSION_SIGNALS.set(())
        cancellation_event = threading.Event()
        self._task_cancellations[job.id] = cancellation_event
        deadline = _start_task_deadline(
            self.root,
            cancellation_event,
            timeout_seconds=job.timeout_seconds,
        )
        execution = RuntimeExecutionControl(
            request_id=f"task:{job.id}:attempt:{job.attempt}",
            session_key=session_key,
            timeout_seconds=deadline.timeout_seconds,
            cancellation_event=cancellation_event,
            timeout_event=deadline.expired,
            progress_callback=lambda progress: self._send_progress(
                chat_id,
                progress.elapsed_seconds,
                progress.sandbox,
            ),
        )
        completed_status = "completed"
        finished_job: TaskJob | None = None
        failure: TaskFailure | None = None
        regression_signals: tuple[TaskRegressionSignal, ...] = ()
        try:
            self._authorize_task(job)
            outcome = _coerce_work_outcome(
                self._run_direct_work(
                    chat_id,
                    request,
                    session_key=session_key,
                    execution=execution,
                )
            )
            reply = outcome.message
            reply = self._capture_task_regression_signals(reply)
            if deadline.expired.is_set():
                reply = _task_timeout_message(deadline.timeout_seconds)
                completed_status = "failed"
                failure = classify_task_failure(reply)
            elif outcome.failed:
                completed_status = "failed"
                failure = TaskFailure(
                    code=outcome.code or "unknown_failure",
                    failure_class=outcome.failure_class or "permanent",
                    retryable=outcome.retryable,
                )
        except CapabilityAuthorizationError as error:
            reply = str(error)
            completed_status = "failed"
            failure = TaskFailure(
                code="authorization_denied",
                failure_class="permanent",
                retryable=False,
            )
        except AgentRuntimeAccessUnavailable as error:
            reply = _codex_pause_warning(job.id, str(error))
            completed_status = "paused"
        except AgentRuntimeTimedOut:
            deadline.expired.set()
            reply = _task_timeout_message(deadline.timeout_seconds)
            completed_status = "failed"
            failure = classify_task_failure(reply)
        except AgentRuntimeCancelled as error:
            if deadline.expired.is_set():
                reply = _task_timeout_message(deadline.timeout_seconds)
                completed_status = "failed"
                failure = classify_task_failure(reply)
            else:
                reply = str(error)
                completed_status = "cancelled"
        except Exception as error:
            reply = f"Enoch could not complete task #{job.id}: {error}"
            completed_status = "failed"
            failure = classify_task_failure(reply)
        finally:
            deadline.cancel()
            regression_signals = _CURRENT_REGRESSION_SIGNALS.get()
            _CURRENT_REGRESSION_SIGNALS.reset(regression_token)
            _CURRENT_TASK_ID.reset(task_token)
            _CURRENT_TASK_WORKER_ID.reset(worker_token)
            self._task_cancellations.pop(job.id, None)
            if completed_status == "cancelled":
                finished_job = self.workflow.finalize(
                    job.id,
                    "cancelled",
                    result=reply,
                    event_actor="agent",
                    trigger=trigger,
                    worker_id=worker_id,
                )
            elif completed_status == "failed":
                failure = failure or classify_task_failure(reply)
                finished_job = self.workflow.finalize(
                    job.id,
                    "failed",
                    result=reply,
                    event_actor="system" if deadline.expired.is_set() else "agent",
                    trigger="task-timeout" if deadline.expired.is_set() else trigger,
                    worker_id=worker_id,
                    failure_code=failure.code,
                    failure_class=failure.failure_class,
                    retryable=False,
                )
            elif completed_status == "paused":
                finished_job = self.workflow.pause(
                    job.id,
                    result=reply,
                    event_actor="system",
                    trigger="runtime-unavailable",
                    worker_id=worker_id,
                )
            else:
                finished_job = self.workflow.finalize(
                    job.id,
                    "completed",
                    result=reply,
                    event_actor="agent",
                    trigger=trigger,
                    worker_id=worker_id,
                )
        authoritative_job = finished_job or self.workflow.find(job.id)
        if authoritative_job is None or authoritative_job.status != completed_status:
            return authoritative_job.result if authoritative_job is not None else reply
        self.effect_fence.run(
            self._apply_task_regression_signals,
            regression_signals,
            current_task_id=job.id if completed_status == "completed" else None,
            allow_resolution=completed_status == "completed",
        )
        completed = authoritative_job
        if completed_status == "completed":
            self._record_automatic_learning(completed, command=trigger, result=reply)
        return reply

    def _queue_direct_work_next(
        self,
        chat_id: int,
        request: str,
        running: TaskJob,
        *,
        context: str = "",
        context_source: str = "",
    ) -> str:
        try:
            job = self.workflow.enqueue(
                chat_id,
                request,
                mode="front",
                context=context,
                context_source=context_source,
                idempotency_key=_event_idempotency_key("direct-next"),
                **self._profile_task_options(),
            )
        except (OSError, ValueError):
            return "Enoch could not queue that /do request."
        message = self._format_work_status(
            WorkStatusMessage(
                chat_id=chat_id,
                message_id=0,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="queued",
                latest_update=f"Queued next after running task #{running.id}.",
                context=job.context,
            )
        )
        message_id = self._safe_send_message_id(
            chat_id,
            message,
            notification_key=f"task:{job.id}:status",
        )
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            self.workflow.record_status_message(job.id, message_id)
            return ""
        return f"Queued task #{job.id} to run next after task #{running.id}."

    def _start_direct_work_worker(self, job: TaskJob, *, session_key: str) -> None:
        worker = threading.Thread(
            target=self._run_direct_task_worker,
            kwargs={"job": job, "session_key": session_key},
            name=f"enoch-direct-task-{job.id}",
            daemon=True,
        )
        self._direct_workers[job.id] = worker
        worker.start()

    def _run_direct_task_worker(self, job: TaskJob, *, session_key: str) -> None:
        try:
            self._run_direct_task_job(job, session_key=session_key)
        finally:
            self._direct_workers.pop(job.id, None)

    def _run_direct_task_job(self, job: TaskJob, *, session_key: str = "") -> None:
        self._run_action_job(
            job,
            command="/do",
            session_key=session_key or f"{self._session_key(job.chat_id)}:do:{job.id}",
            start_update=f"Starting direct task #{job.id}.",
            failure_prefix=f"Enoch could not complete direct task #{job.id}",
        )

    def _start_cron_scheduler(self) -> None:
        with self._cron_scheduler_lock:
            if self._stopping:
                return
            if (
                self._cron_scheduler_thread is not None
                and self._cron_scheduler_thread.is_alive()
            ):
                return
            self._cron_scheduler_stop.clear()
            self._cron_scheduler_wake.clear()
            self._cron_scheduler_thread = threading.Thread(
                target=self._run_cron_scheduler,
                name="enoch-cron-scheduler",
                daemon=True,
            )
            self._cron_scheduler_thread.start()

    def _stop_cron_scheduler(self, timeout_seconds: float = 7.0) -> None:
        self._cron_scheduler_stop.set()
        self._cron_scheduler_wake.set()
        with self._cron_scheduler_lock:
            worker = self._cron_scheduler_thread
        if (
            worker is not None
            and worker is not threading.current_thread()
            and worker.is_alive()
        ):
            worker.join(timeout=max(0.0, timeout_seconds))
        with self._cron_scheduler_lock:
            if (
                self._cron_scheduler_thread is worker
                and (worker is None or not worker.is_alive())
            ):
                self._cron_scheduler_thread = None

    def _run_cron_scheduler(self) -> None:
        while not self._stopping and not self._cron_scheduler_stop.is_set():
            self._cron_scheduler_wake.clear()
            try:
                require_current_daemon_epoch(self.daemon_epoch, self.root)
                self._enqueue_due_cron_jobs()
                self._maybe_start_task_worker()
                wait_seconds = cron_scheduler_wait_seconds(self.root)
            except StaleDaemonEpoch:
                return
            except Exception as error:
                print(f"Enoch cron scheduler error: {error}")
                wait_seconds = 1.0
            self._cron_scheduler_wake.wait(timeout=wait_seconds)

    def stop_workers(self, timeout_seconds: float = 7.0) -> None:
        self._stopping = True
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        self._stop_cron_scheduler(
            timeout_seconds=max(0.0, deadline - time.monotonic())
        )
        for cancellation in tuple(self._task_cancellations.values()):
            cancellation.set()
        current = threading.current_thread()
        workers = [*self._direct_workers.values()]
        if self._task_worker is not None:
            workers.append(self._task_worker)
        if self._lineage_worker is not None:
            workers.append(self._lineage_worker)
        for worker in workers:
            if worker is current or not worker.is_alive():
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))

    def _run_direct_work(
        self,
        chat_id: ConversationId,
        request: str,
        *,
        context: str = "",
        session_key: str,
        execution: RuntimeExecutionControl | None = None,
    ) -> WorkOutcome:
        return self._task_workflow.run_direct_work(
            chat_id,
            request,
            context=context,
            session_key=session_key,
            execution=execution,
        )

    def _prepare_task_worktree(self, request: str) -> TaskWorktree:
        return self._task_workflow.prepare_task_worktree(request)

    def _run_forge_maintenance(self, request: ForgeMaintenanceRequest) -> str:
        return self._task_workflow.run_forge_maintenance(request)

    def _run_existing_branch_publish_with_status(
        self,
        chat_id: int,
        text: str,
        branch: str,
    ) -> str:
        return self._task_workflow.run_existing_branch_publish_with_status(
            chat_id,
            text,
            branch,
        )

    def _publish_existing_branch(self, chat_id: int, branch: str) -> str:
        return self._task_workflow.publish_existing_branch(chat_id, branch)

    def _prepare_existing_branch_task_worktree(self, branch: str) -> TaskWorktree:
        return self._task_workflow.prepare_existing_branch_task_worktree(branch)

    def _save_memory_requests(self, requests: tuple[str, ...]) -> str:
        if not requests:
            return ""
        saved = 0
        failed = 0
        for request in requests:
            try:
                remember_memory(request, root=self.root)
            except (OSError, ValueError):
                failed += 1
            else:
                saved += 1
        if saved and failed:
            return f"Saved {saved} long-term memory item(s). {failed} memory save failed."
        if saved == 1:
            return "Saved to Enoch long-term memory."
        if saved:
            return f"Saved {saved} long-term memory items."
        return "Enoch could not save that long-term memory."

    def _publish_feature_pr(
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
        return self._task_workflow.publish_feature_pr(
            chat_id,
            request,
            allowed_files,
            work_root=work_root,
            task_worktree=task_worktree,
            validation_result=validation_result,
            resume_job=resume_job,
        )

    def _record_current_publish_stage(
        self,
        stage: str,
        *,
        revision_id: str = "",
        workspace_id: str = "",
        review_id: str = "",
        review_url: str = "",
        review_published: bool | None = None,
    ) -> None:
        self._task_workflow.record_current_publish_stage(
            stage,
            revision_id=revision_id,
            workspace_id=workspace_id,
            review_id=review_id,
            review_url=review_url,
            review_published=review_published,
        )

    def _resume_task_publish(self, job: TaskJob) -> WorkOutcome:
        return self._task_workflow.resume_task_publish(job)

    def _return_to_resident_after_handoff(
        self,
        *,
        published_remotely: bool = True,
    ) -> str:
        return self._task_workflow.return_to_resident_after_handoff(
            published_remotely=published_remotely,
        )

    def _remember_resident_branch(self, fallback: str) -> str:
        if not self._resident_branch:
            self._resident_branch = fallback
        return self._resident_branch

    def _resident_branch_name(self, fallback: str = "") -> str:
        if self._resident_branch:
            return self._resident_branch
        if fallback:
            return self._remember_resident_branch(fallback)
        return self._remember_resident_branch(self._authoritative_branch_name())

    def _authoritative_branch_name(self) -> str:
        try:
            return (
                self.repository.authoritative_base(self.root).name
                or DEFAULT_BRANCH
            )
        except (RepositoryProviderError, VcsError):
            return DEFAULT_BRANCH

    def _send_step_update(self, chat_id: ConversationId | None, message: str) -> None:
        if chat_id is None:
            return
        if self._update_work_status(message):
            return
        self._safe_send_message(chat_id, f"Enoch update: {message}")

    def _deliver_message(
        self,
        chat_id: ConversationId,
        message: str,
        *,
        notification_key: str = "",
    ) -> NotificationResult:
        key = notification_key or self._notification_key(
            "send",
            chat_id,
            message,
        )
        try:
            return self.notifications.send(
                chat_id,
                message,
                idempotency_key=key,
            )
        except (OSError, ChatProviderError, CapabilityAuthorizationError) as error:
            return NotificationResult(
                delivered=False,
                error=str(error),
            )

    def _safe_send_message(
        self,
        chat_id: ConversationId,
        message: str,
        *,
        notification_key: str = "",
    ) -> str:
        result = self._deliver_message(
            chat_id,
            message,
            notification_key=notification_key,
        )
        return "" if result.delivered else result.error

    def _safe_send_message_id(
        self,
        chat_id: ConversationId,
        message: str,
        *,
        notification_key: str = "",
    ) -> MessageId | None:
        result = self._deliver_message(
            chat_id,
            message,
            notification_key=notification_key,
        )
        return result.message_id if result.delivered else None

    def _safe_edit_message(
        self,
        chat_id: ConversationId,
        message_id: MessageId,
        message: str,
        *,
        notification_key: str = "",
    ) -> None:
        key = notification_key or self._notification_key(
            "edit",
            chat_id,
            message,
            message_id=message_id,
        )
        try:
            self.notifications.edit(
                chat_id,
                message_id,
                message,
                idempotency_key=key,
            )
        except (OSError, ChatProviderError, CapabilityAuthorizationError):
            return

    def _notification_key(
        self,
        operation: str,
        chat_id: ConversationId,
        message: str,
        *,
        message_id: MessageId | None = None,
    ) -> str:
        event_key = _CURRENT_EVENT_KEY.get()
        task_id = _CURRENT_TASK_ID.get()
        if event_key:
            scope = f"inbox:{event_key}"
        elif task_id is not None:
            scope = f"task:{task_id}"
        else:
            scope = f"daemon:{self.daemon_epoch.generation}"
        payload = json.dumps(
            {
                "operation": operation,
                "chat_id": chat_id,
                "message_id": message_id,
                "message": message,
            },
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
        return f"{scope}:{operation}:{digest}"

    def _update_work_status(
        self,
        latest_update: str,
        *,
        status: str | None = None,
        review_url: str = "",
    ) -> bool:
        with self._notification_order_lock:
            task_status = _CURRENT_WORK_STATUS.get()
            if task_status is None:
                return False
            if task_status.status in {"completed", "failed", "cancelled", "regressed"}:
                return True
            if status:
                task_status.status = status
            task_status.latest_update = latest_update
            if review_url and review_url not in task_status.reviews:
                task_status.reviews.append(review_url)
            if normalize_message_id(task_status.message_id) is None:
                return True
            self._safe_edit_message(
                task_status.chat_id,
                task_status.message_id,
                self._format_work_status(task_status),
                notification_key=_task_status_notification_key(task_status),
            )
            return True

    def _safe_send_read_ack(self, chat_id: ConversationId, message_id: object) -> None:
        if not isinstance(message_id, (int, str)):
            return
        try:
            self.effect_fence.run_authorized(
                "chat.ack",
                ("chat.ack",),
                self.client.send_read_ack,
                chat_id,
                message_id,
            )
        except (OSError, ChatProviderError, CapabilityAuthorizationError) as error:
            _record_system_event(
                "chat_read_ack_failed",
                self.root,
                status="failed",
                details={
                    "provider": self.channel_name,
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "error": str(error),
                },
            )

    def _status(self, chat_id: ConversationId | None = None) -> str:
        status = status_message(
            self.identity,
            self.root,
            allowed_chat_id=_allowed_conversation_id(self.client),
            chat_id=chat_id,
            chat_provider=self.channel_name,
            profile_name=self._profile_status_name(),
            extension_summaries=tuple(
                f"{extension.name} (API v{extension.api_version})"
                for extension in self.extensions
            ),
            model_summary_fn=self.runtime.model_summary,
        )
        return "\n\n".join(
            [
                status,
                _task_status_message(
                    self.root,
                    task_status=self.workflow.inspect(),
                ),
            ]
        )

    def _mission(self, text: str) -> str:
        reply = mission_command(text, self.identity, self.root)
        if text.split(maxsplit=1)[0].lower() == "/mission" and len(text.split(maxsplit=1)) > 1:
            try:
                self.identity = load_identity(identity_file_path(self.root))
            except (OSError, ValueError, KeyError):
                pass
        return reply

    def _skills(self, text: str) -> str:
        return skills_command(text, self.root)

    def _learn(self, chat_id: int, text: str) -> str:
        request = parse_learn_request(text)
        if request is None:
            return learn_command(text, self.root)
        try:
            skill = load_published_skill(
                request.skill,
                request.agent,
                root=self.root,
            )
            local_skills = load_agent_skills(root=self.root)
            state = load_evolve_state(self.root)
            existing = load_evolve_candidates(
                self.root,
                include_inactive=True,
                theme=state.theme,
            )
            prompt = learning_assessment_prompt(
                skill,
                mission=self.identity.mission,
                current_skills=(
                    {
                        "name": item.name,
                        "version": item.version,
                        "summary": item.summary or item.description,
                    }
                    for item in local_skills.skills
                ),
                existing_candidates=(
                    {
                        "id": item.id,
                        "source": item.source,
                        "title": item.title,
                        "proposed_change": item.proposed_change,
                        "status": item.status,
                    }
                    for item in existing[:20]
                ),
            )
            raw = self._respond_isolated_evidence_turn(
                chat_id,
                prompt,
                phase="learning-assessment",
            )
            assessment = parse_learning_assessment(raw)
        except (
            AgentRuntimeError,
            CapabilityAuthorizationError,
            LearnError,
            SkillsError,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            return f"Enoch could not assess that skill: {error}"

        if not assessment.applicable:
            return "\n".join(
                [
                    f"Enoch assessed {skill.agent_name}'s {skill.name} skill as not applicable.",
                    f"Reason: {assessment.reason}",
                    "No evolution candidate was created.",
                ]
            )

        assert assessment.candidate is not None
        try:
            candidate, created = create_learning_candidate(
                skill,
                assessment.candidate,
                self.root,
                theme=state.theme,
            )
        except (OSError, ValueError) as error:
            return f"Enoch could not save that learning candidate: {error}"
        action = (
            f"Created learning candidate {candidate.id}."
            if created
            else f"Learning candidate {candidate.id} already exists."
        )
        return "\n\n".join(
            [
                (
                    f"Enoch assessed {skill.agent_name}'s {skill.name} skill "
                    "as applicable."
                ),
                f"Reason: {assessment.reason}",
                action,
                "\n".join(_format_evolve_candidate(candidate)),
                f"Next: /evolve approve {candidate.id}",
            ]
        )

    def _task(self, chat_id: int, text: str) -> str:
        command, argument = _parse_chat_command(text)
        if command != "/task" or not argument:
            return "Use /task <request> to queue background work."
        subcommand = argument.split(maxsplit=1)[0].lower()
        cancel_id = _task_cancel_id(argument)
        retry_id = _task_retry_id(argument)
        resume_target = _task_resume_target(argument)
        if subcommand == "cancel" and cancel_id is None:
            return "Use /task cancel <id> to cancel a queued task."
        if subcommand == "retry" and retry_id is None:
            return "Use /task retry <id> to retry a failed task as a new linked task."
        if subcommand == "resume" and resume_target is None:
            return "Use /task resume <id|all> to continue paused tasks."
        if cancel_id is not None:
            cancelled = self.workflow.cancel(cancel_id)
            if cancelled is None:
                return f"Enoch could not cancel task #{cancel_id}. It may be running, completed, or missing."
            message_id = self._work_status_messages.pop(cancelled.id, cancelled.status_message_id)
            if message_id is not None:
                cancelled_status = WorkStatusMessage(
                    chat_id=cancelled.chat_id,
                    message_id=message_id,
                    request=cancelled.text,
                    started_at=time.monotonic(),
                    task_id=cancelled.id,
                    status="cancelled",
                    latest_update="Cancelled before running.",
                    context=cancelled.context,
                )
                self._safe_edit_message(
                    cancelled.chat_id,
                    message_id,
                    self._format_work_status(cancelled_status),
                )
            return f"Cancelled task #{cancelled.id}."
        if retry_id is not None:
            return self._retry_task(retry_id)
        if resume_target is not None:
            return self._resume_tasks(
                str(resume_target),
                trigger="/task resume",
            )
        snapshot = self._resolve_task_context_snapshot(chat_id, argument)
        if snapshot.codex_unavailable_reason:
            return self._queue_paused_request(
                chat_id,
                argument,
                source="task",
                trigger="/task",
                reason=snapshot.codex_unavailable_reason,
            )
        if snapshot.error:
            return f"Enoch could not prepare conversation context for that task yet: {snapshot.error}"
        if snapshot.clarification:
            return f"Enoch needs one clarification before queueing that task: {snapshot.clarification}"
        try:
            job = self.workflow.enqueue(
                chat_id,
                argument,
                context=snapshot.context,
                context_source=snapshot.source,
                idempotency_key=_event_idempotency_key("task"),
                **self._profile_task_options(),
            )
        except (OSError, ValueError):
            return "Enoch could not queue that task."
        status = self.workflow.inspect()
        position = status.pending_count
        message = self._format_work_status(
            WorkStatusMessage(
                chat_id=chat_id,
                message_id=0,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="queued",
                latest_update=f"Queued at position {position}.",
                context=job.context,
            )
        )
        message_id = self._safe_send_message_id(
            chat_id,
            message,
            notification_key=f"task:{job.id}:status",
        )
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            self.workflow.record_status_message(job.id, message_id)
            return ""
        return f"Queued task #{job.id}. Enoch will work on it in the background when idle."

    def _retry_task(self, task_id: int) -> str:
        original = self.workflow.find(task_id)
        try:
            reconciled_result = (
                _reconciled_retry_result(original, self.root, review=self.review)
                if original is not None
                else ""
            )
            job = self.workflow.retry_failed(
                task_id,
                reconciled_result=reconciled_result,
            )
        except (OSError, ReviewProviderError, TaskRetryError) as error:
            return f"Enoch could not retry task #{task_id}: {error}"
        position = self.workflow.inspect().pending_count
        message = self._format_work_status(
            WorkStatusMessage(
                chat_id=job.chat_id,
                message_id=0,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="queued",
                latest_update=(
                    (
                        f"Retry of failed task #{task_id} reconciled "
                        f"{len(job.review_urls)} existing review(s)."
                    )
                    if job.review_urls
                    else f"Retry of failed task #{task_id} queued at position {position}."
                ),
                context=job.context,
            )
        )
        message_id = self._safe_send_message_id(
            job.chat_id,
            message,
            notification_key=f"task:{job.id}:status",
        )
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            self.workflow.record_status_message(job.id, message_id)
            return ""
        return f"Queued retry task #{job.id} for failed task #{task_id}."

    def _queue_paused_request(
        self,
        chat_id: int,
        request: str,
        *,
        source: str,
        trigger: str,
        reason: str,
    ) -> str:
        try:
            job = self.workflow.enqueue(
                chat_id,
                request,
                mode="front" if source == "chat-task" else "queued",
                source=source,
                initiated_by="human",
                event_actor="human",
                trigger=trigger,
                idempotency_key=_event_idempotency_key(f"paused:{trigger}"),
                **self._profile_task_options(),
            )
            paused = self.workflow.pause(
                job.id,
                result=_codex_pause_warning(job.id, reason),
                event_actor="system",
                trigger="runtime-unavailable",
            )
        except (OSError, ValueError):
            return "Enoch could not preserve that task while agent runtime access is unavailable."
        if paused is None:
            return "Enoch could not pause that task safely."
        return self._publish_paused_task(paused, reason)

    def _publish_paused_task(self, job: TaskJob, reason: str) -> str:
        warning = _codex_pause_warning(job.id, reason)
        message_id = self._safe_send_message_id(
            job.chat_id,
            self._format_work_status(
                WorkStatusMessage(
                    chat_id=job.chat_id,
                    message_id=0,
                    request=job.text,
                    started_at=time.monotonic(),
                    task_id=job.id,
                    status="paused",
                    latest_update=(
                        f"{reason} Use /task resume {job.id} when agent runtime access "
                        "is available again."
                    ),
                    context=job.context,
                )
            ),
            notification_key=f"task:{job.id}:status",
        )
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            self.workflow.record_status_message(job.id, message_id)
            return ""
        return warning

    def _resume_tasks(self, argument: str, *, trigger: str = "/task resume") -> str:
        cleaned = argument.strip().lower()
        task_id = None
        if cleaned != "all":
            try:
                task_id = int(cleaned.lstrip("#"))
            except ValueError:
                return "Use /task resume <id|all> to continue paused tasks."
        resumed = self.workflow.resume(
            task_id=task_id,
            trigger=trigger,
        )
        if not resumed:
            if task_id is not None:
                return f"Task #{task_id} is not paused."
            return "No tasks are paused for agent runtime access."
        for job in resumed:
            message_id = self._work_status_messages.get(job.id) or job.status_message_id
            if message_id is not None:
                self._work_status_messages[job.id] = message_id
                self._safe_edit_message(
                    job.chat_id,
                    message_id,
                    self._format_work_status(
                        WorkStatusMessage(
                            chat_id=job.chat_id,
                            message_id=message_id,
                            request=job.text,
                            started_at=time.monotonic(),
                            task_id=job.id,
                            status="queued",
                            latest_update="Resumed after agent runtime access was restored.",
                            context=job.context,
                        )
                    ),
                )
        self._maybe_start_task_worker()
        task_ids = ", ".join(f"#{job.id}" for job in resumed)
        noun = "task" if len(resumed) == 1 else "tasks"
        return f"Resumed {len(resumed)} {noun}: {task_ids}."

    def _capture_task_regression_signals(self, reply: str) -> str:
        result = extract_task_regression_signals(reply)
        if result.signals:
            _CURRENT_REGRESSION_SIGNALS.set(
                (*_CURRENT_REGRESSION_SIGNALS.get(), *result.signals)
            )
        return result.visible_reply

    def _apply_task_regression_signals(
        self,
        signals: tuple[TaskRegressionSignal, ...],
        *,
        current_task_id: int | None = None,
        allow_resolution: bool = True,
    ) -> None:
        for signal in signals:
            if signal.task_id == current_task_id:
                continue
            task = self.workflow.find(signal.task_id)
            if task is None:
                continue
            if task.status == "completed":
                task = self.workflow.regress(
                    signal.task_id,
                    result=signal.reason,
                    event_actor="agent",
                    trigger="agent-regression-signal",
                )
                if task is None:
                    continue
            elif task.status != "regressed":
                continue
            if not allow_resolution or not signal.resolution:
                continue
            related_task_id = signal.fix_task_id
            if signal.resolution == "forward-fixed" and related_task_id is None:
                related_task_id = current_task_id
            self.workflow.resolve_regression(
                signal.task_id,
                signal.resolution,
                result=signal.reason,
                event_actor="agent",
                trigger="agent-regression-signal",
                related_task_id=related_task_id,
            )

    def _stop_running_job(self) -> str:
        running = self.workflow.inspect().running
        if running is None:
            return "No running task to stop."
        cancellation_event = self._task_cancellations.get(running.id)
        if cancellation_event is not None:
            cancellation_event.set()
        result = "Stopped by /stop."
        cancelled = self.workflow.cancel(
            running.id,
            result=result,
            trigger="/stop",
        )
        if cancelled is None:
            return "No running task to stop."
        message_id = self._work_status_messages.pop(cancelled.id, cancelled.status_message_id)
        if message_id is not None:
            stopped_status = WorkStatusMessage(
                chat_id=cancelled.chat_id,
                message_id=message_id,
                request=cancelled.text,
                started_at=time.monotonic(),
                task_id=cancelled.id,
                status="cancelled",
                latest_update=result,
                context=cancelled.context,
            )
            self._safe_edit_message(
                cancelled.chat_id,
                message_id,
                self._format_work_status(stopped_status),
            )
        return f"Stopped task #{cancelled.id}."

    def _backlog(self, chat_id: int, text: str) -> str:
        command, argument = _parse_chat_command(text)
        if command != "/backlog":
            return _backlog_usage()
        if not argument:
            return _format_backlog_report(self.root)

        first, _separator, rest = argument.partition(" ")
        subcommand = first.lower()
        if subcommand == "cancel":
            return "Use /backlog remove <id> to remove a pending backlog item."
        if subcommand == "remove":
            item_id = _backlog_item_id(rest)
            if item_id is None:
                return "Use /backlog remove <id> to remove a pending backlog item."
            removed = remove_backlog_item(item_id, self.root)
            if removed is None:
                return f"Enoch could not remove backlog #{item_id}. It may already be promoted, removed, or missing."
            return f"Removed backlog #{removed.id}."
        if subcommand == "priority":
            item_id, priority = _backlog_priority_update(rest)
            if item_id is None or priority is None:
                return "Use /backlog priority <id> p0|p1|p2 to reprioritize a pending backlog item."
            try:
                updated = reprioritize_backlog_item(item_id, priority, self.root)
            except ValueError as error:
                return str(error)
            if updated is None:
                return f"Enoch could not reprioritize backlog #{item_id}. It may already be promoted, removed, or missing."
            return f"Backlog #{updated.id} priority is now {updated.priority}."
        if subcommand == "promote":
            item_id = _backlog_item_id(rest)
            if item_id is None:
                return "Use /backlog promote <id> to move a pending backlog item into the active task queue."
            try:
                job = self._promote_backlog_item_to_queue(item_id)
            except (OSError, ValueError, RuntimeError) as error:
                return f"Enoch could not promote backlog #{item_id}: {error}"
            if job is None:
                return f"Enoch could not promote backlog #{item_id}. It may already be promoted, removed, or missing."
            return f"Promoted backlog #{item_id} to task #{job.id}."

        try:
            priority, request = _backlog_priority_and_request(argument)
        except ValueError as error:
            return str(error)
        if not request:
            return _backlog_usage()
        snapshot = self._resolve_task_context_snapshot(chat_id, request)
        if snapshot.error:
            return f"Enoch could not prepare conversation context for that backlog item yet: {snapshot.error}"
        if snapshot.clarification:
            return f"Enoch needs one clarification before adding that to the backlog: {snapshot.clarification}"
        try:
            item = add_backlog_item(
                chat_id,
                request,
                self.root,
                priority=priority,
                context=snapshot.context,
                context_source=snapshot.source,
                idempotency_key=_event_idempotency_key("backlog-add"),
            )
        except (OSError, ValueError):
            return "Enoch could not add that backlog item."
        return f"Backlog #{item.id} [{item.priority}] saved. Enoch will promote it when the task queue is idle."

    def _evolve(self, chat_id: int, argument: str) -> str:
        parts = argument.strip().split(maxsplit=1)
        if not parts:
            return _format_evolve_report(evolve_report(self.root, refresh=False))
        subcommand = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if subcommand == "evidence":
            try:
                source = _evidence_source_selection(rest)
            except ValueError as error:
                return str(error)
            return _format_evidence_report(self.root, source=source)
        if subcommand == "scan":
            state = load_evolve_state(self.root)
            if state.mode == MODE_DISABLED:
                return (
                    "Evolve is disabled. Enable it with "
                    "/evolve config mode co-evolve before scanning."
                )
            try:
                source = _evidence_source_selection(rest)
            except ValueError as error:
                return str(error)
            results = self._scan_evidence_sources(
                chat_id,
                source=source,
                force=True,
                drain=True,
                reason="/evolve scan",
            )
            return _format_evidence_scan_results(results)
        if subcommand == "candidates":
            report = evolve_report(self.root, refresh=False)
            normalized_rest = rest.strip().lower()
            if normalized_rest not in {"", "all"}:
                return "Use /evolve candidates [all]."
            candidates = (
                load_evolve_candidates(
                    self.root,
                    include_inactive=True,
                    theme=report.state.theme,
                )
                if normalized_rest == "all"
                else report.candidates
            )
            return _format_evolve_candidates(
                candidates,
                include_inactive=normalized_rest == "all",
            )
        if subcommand == "propose":
            if rest.strip():
                return "Use /evolve propose."
            return _format_evolve_proposal(
                self._propose_evolve(chat_id, trigger="evolve-propose")
            )
        if subcommand == "config":
            return self._evolve_config(rest)
        if subcommand == "brainstorm":
            state = load_evolve_state(self.root)
            if state.mode == MODE_DISABLED:
                return "Enable co-evolve or auto-evolve mode before brainstorming."
            theme = rest.strip() or state.theme
            if not theme:
                return (
                    "Set a theme with /evolve config theme <text>, or use "
                    "/evolve brainstorm <theme>."
                )
            if rest.strip():
                state = set_evolve_theme(theme, self.root)
            try:
                creation = self._generate_brainstorm_candidates(
                    chat_id,
                    theme,
                )
            except (
                AgentRuntimeError,
                BrainstormError,
                CapabilityAuthorizationError,
                OSError,
                RuntimeError,
                SkillsError,
                TypeError,
                ValueError,
            ) as error:
                return f"Enoch could not brainstorm evolution candidates: {error}"
            report = evolve_report(self.root, refresh=False)
            if not creation.created:
                if creation.existing:
                    result = (
                        "Brainstorming found only candidate(s) that already exist: "
                        + ", ".join(candidate.id for candidate in creation.existing)
                        + "."
                    )
                else:
                    result = (
                        "Brainstorming found no sufficiently novel bounded "
                        "candidate for this theme."
                    )
            else:
                result = (
                    f"Created {len(creation.created)} theme-guided "
                    "brainstorming candidate(s)."
                )
                if creation.existing:
                    result += (
                        f" Skipped {len(creation.existing)} existing candidate(s)."
                    )
            return result + "\n\n" + _format_evolve_report(report)
        if subcommand == "remove":
            if not rest.strip():
                return "Use /evolve remove <id> [reason] to remove a self-evolution candidate."
            remove_parts = rest.strip().split(maxsplit=1)
            remove_reason = remove_parts[1] if len(remove_parts) > 1 else "human-requested-removal"
            removal_classification = ""
            removal_curation_id = ""
            removal_evidence_refs: tuple[str, ...] = ()
            reason_parts = remove_reason.split(maxsplit=1)
            requested_classification = reason_parts[0].lower()
            if requested_classification in REMOVE_CLASSIFICATIONS:
                removal_classification = requested_classification
                recorded = latest_remove_suggestion(
                    remove_parts[0],
                    self.root,
                    classification=requested_classification,
                )
                if recorded is not None:
                    curation, suggestion = recorded
                    removal_curation_id = curation.id
                    removal_evidence_refs = suggestion.evidence_refs
                    remove_reason = (
                        reason_parts[1]
                        if len(reason_parts) > 1
                        else suggestion.reason
                    )
            state = evolve_report(self.root).state
            try:
                candidate = remove_evolve_candidate(
                    remove_parts[0],
                    self.root,
                    theme=state.theme,
                    reason=remove_reason,
                    classification=removal_classification,
                    curation_id=removal_curation_id,
                    evidence_refs=removal_evidence_refs,
                )
            except ValueError as error:
                return str(error)
            return "Removed evolve candidate.\n\n" + "\n".join(_format_evolve_candidate(candidate))
        if subcommand == "approve":
            if not rest.strip():
                return "Use /evolve approve <id> to approve and queue a self-evolution candidate."
            return self._evolve_approve(rest)
        return _evolve_usage()

    def _evolve_config(self, argument: str) -> str:
        parts = argument.strip().split(maxsplit=1)
        if not parts:
            return _format_evolve_config(
                evolve_report(self.root, refresh=False)
            )
        setting = parts[0].lower()
        value = parts[1].strip() if len(parts) > 1 else ""
        if setting == "mode":
            if not value:
                return (
                    "Use /evolve config mode <disabled|co-evolve|auto-evolve>."
                )
            try:
                set_evolve_mode(value, self.root)
            except ValueError as error:
                return str(error)
        elif setting == "theme":
            if not value:
                return _format_evolve_theme(load_evolve_state(self.root))
            set_evolve_theme(value, self.root)
        elif setting in {"feedback-batch", "experience-batch"}:
            if not value:
                return f"Use /evolve config {setting} <1-100>."
            try:
                save_evidence_batch_size(
                    setting.removesuffix("-batch"),
                    value,
                    self.root,
                )
            except (TypeError, ValueError) as error:
                return str(error)
        elif setting == "schedule":
            if not value:
                return _format_evolve_config(
                    evolve_report(self.root, refresh=False)
                )
            return self._evolve_schedule(value)
        else:
            return (
                "Use /evolve config "
                "<mode|theme|feedback-batch|experience-batch|schedule> <value>."
            )
        return _format_evolve_config(
            evolve_report(self.root, refresh=False)
        )

    def _generate_brainstorm_candidates(
        self,
        chat_id: int,
        theme: str,
    ) -> BrainstormCreation:
        skills = load_agent_skills(root=self.root)
        candidates = load_evolve_candidates(
            self.root,
            include_inactive=True,
            theme=theme,
        )
        candidate_context = tuple(
            {
                "id": candidate.id,
                "source": candidate.source,
                "status": candidate.status,
                "title": candidate.title,
                "proposed_change": candidate.proposed_change,
                "theme": candidate.source_theme,
                "provenance": {
                    "source_task_id": candidate.source_task_id,
                },
            }
            for candidate in candidates[:30]
        )
        completed_work = recent_completion_evidence(
            candidate_context,
            self.root,
        )
        request = prepare_brainstorm_request(
            theme,
            self.identity.mission,
            current_skills=(
                {
                    "name": skill.name,
                    "version": skill.version,
                    "summary": skill.summary or skill.description,
                }
                for skill in skills.skills
            ),
            existing_candidates=candidate_context,
            recent_completed_work=completed_work,
        )
        response = self._respond_isolated_evidence_turn(
            chat_id,
            request.prompt,
            phase="brainstorming",
        )
        drafts = parse_brainstorm_response(
            response,
            limit=request.limit,
        )
        return create_brainstorm_candidates(
            drafts,
            self.root,
            theme=request.theme,
            context_hash=request.context_hash,
        )

    def _propose_evolve(self, chat_id: int, *, trigger: str) -> EvolveProposal:
        scan_results: tuple[EvidenceScanResult, ...] = ()
        evidence_candidates: tuple[EvolveCandidate, ...] = ()
        synthesis_error = ""
        brainstorm_status = ""
        brainstorm_created = 0
        brainstorm_existing = 0
        brainstorm_error = ""
        if load_evolve_state(self.root).mode != MODE_DISABLED:
            scan_results = self._scan_evidence_sources(
                chat_id,
                source="all",
                force=True,
                drain=True,
                reason=(
                    "evolve-scheduler"
                    if trigger == "evolve-scheduler"
                    else "/evolve propose"
                ),
            )
            try:
                evidence_candidates = synthesize_evolve_candidates_from_evidence(
                    self.root,
                    mission=self.identity.mission,
                    generator=lambda prompt: self._respond_isolated_evidence_turn(
                        chat_id,
                        prompt,
                        phase="candidate-synthesis",
                    ),
                )
            except (
                AgentRuntimeError,
                CapabilityAuthorizationError,
                EvidenceError,
                OSError,
                RuntimeError,
                TimeoutError,
                TypeError,
                ValueError,
            ) as error:
                synthesis_error = clean_text(str(error)) or error.__class__.__name__
        if trigger == "evolve-scheduler":
            scheduled_state = load_evolve_state(self.root)
            available = evolve_report(self.root).candidates
            if scheduled_state.mode != MODE_AUTO_EVOLVE:
                brainstorm_status = "explicit-only"
            elif available:
                brainstorm_status = "not-needed"
            elif not scheduled_state.theme:
                brainstorm_status = "theme-not-set"
            elif not claim_scheduled_brainstorm(
                scheduled_state.theme,
                self.root,
            ):
                brainstorm_status = "cooldown"
            else:
                try:
                    creation = self._generate_brainstorm_candidates(
                        chat_id,
                        scheduled_state.theme,
                    )
                    brainstorm_created = len(creation.created)
                    brainstorm_existing = len(creation.existing)
                    if creation.created:
                        brainstorm_status = "created"
                    elif creation.existing:
                        brainstorm_status = "existing"
                    else:
                        brainstorm_status = "no-ideas"
                except (
                    AgentRuntimeError,
                    BrainstormError,
                    CapabilityAuthorizationError,
                    OSError,
                    RuntimeError,
                    SkillsError,
                    TypeError,
                    ValueError,
                ) as error:
                    brainstorm_status = "failed"
                    brainstorm_error = (
                        clean_text(str(error)) or error.__class__.__name__
                    )
        proposal = propose_evolve(
            self.root,
            mission=self.identity.mission,
            curator=lambda prompt: self._respond_isolated_evidence_turn(
                chat_id,
                prompt,
                phase="candidate-curation",
            ),
        )
        proposal = replace(
            proposal,
            evidence_scan_results=scan_results,
            evidence_candidates_added=len(evidence_candidates),
            evidence_synthesis_error=synthesis_error,
            scheduled_brainstorm_status=brainstorm_status,
            scheduled_brainstorm_created=brainstorm_created,
            scheduled_brainstorm_existing=brainstorm_existing,
            scheduled_brainstorm_error=brainstorm_error,
        )
        event_actor = "system" if trigger == "evolve-scheduler" else "human"
        event_trigger = (
            "evolve-scheduler"
            if event_actor == "system"
            else "/evolve propose"
        )
        self._record_evolve_event(
            "checked",
            event_actor=event_actor,
            trigger=event_trigger,
            proposal=proposal,
            reason=_evolve_check_reason(proposal),
        )
        if proposal.report.state.mode == MODE_DISABLED:
            self._record_evolve_event(
                "skipped",
                event_actor=event_actor,
                trigger=event_trigger,
                proposal=proposal,
                reason="mode-disabled",
            )
        elif proposal.top_candidate is None:
            self._record_evolve_event(
                "skipped",
                event_actor=event_actor,
                trigger=event_trigger,
                proposal=proposal,
                reason=_evolve_skip_reason(proposal),
            )
        else:
            close_open_proposals(
                self.root,
                event_actor=event_actor,
                trigger=event_trigger,
                reason="superseded-by-new-proposal",
            )
            proposed_event = self._record_evolve_event(
                "proposed",
                event_actor=event_actor,
                trigger=event_trigger,
                proposal=proposal,
                candidate=proposal.top_candidate,
            )
            if proposed_event is not None:
                proposal = replace(proposal, proposal_id=proposed_event.proposal_id)
        return proposal

    def _record_evolve_event(
        self,
        event: str,
        *,
        event_actor: str,
        trigger: str,
        proposal: EvolveProposal | None = None,
        candidate: EvolveCandidate | None = None,
        task_id: int | None = None,
        approval_actor: str = "",
        retry_of_task_id: int | None = None,
        reason: str = "",
        proposal_id: str = "",
    ) -> EvolveEvent | None:
        state = proposal.report.state if proposal is not None else load_evolve_state(self.root)
        try:
            return record_evolve_event(
                event,
                self.root,
                event_actor=event_actor,
                trigger=trigger,
                mode=state.mode,
                theme=state.theme,
                candidate=candidate,
                task_id=task_id,
                approval_actor=approval_actor,
                retry_of_task_id=retry_of_task_id,
                reason=reason,
                proposal_id=proposal_id or (proposal.proposal_id if proposal is not None else ""),
                curation_id=(proposal.curation.id if proposal is not None and proposal.curation else ""),
                recommendation_kind=(
                    proposal.curation.status if proposal is not None and proposal.curation else ""
                ),
                evidence_refs=(
                    curation_evidence_refs(proposal.curation)
                    if proposal is not None
                    else ()
                ),
            )
        except (OSError, ValueError):
            return None

    def _evolve_approve(self, candidate_id: str) -> str:
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return f"Enoch needs a locked {provider_label(self.channel_name)} conversation before approving evolve work."
        state = evolve_report(self.root).state
        try:
            candidate = get_evolve_candidate(candidate_id, self.root, theme=state.theme)
        except ValueError as error:
            return str(error)
        if candidate.status != "candidate":
            return f"Evolve candidate {candidate.id} cannot be approved from status {candidate.status}."
        proposal_id = latest_open_proposal_id(candidate.id, self.root)
        self._record_evolve_event(
            "selected",
            event_actor="human",
            trigger="/evolve approve",
            candidate=candidate,
            approval_actor="human",
            proposal_id=proposal_id,
        )
        try:
            job = self.workflow.enqueue(
                chat_id,
                _evolve_task_request(candidate, state.theme),
                context=_evolve_task_context(candidate),
                context_source="evolve-approve",
                source=candidate.source,
                initiated_by="human",
                event_actor="human",
                trigger="/evolve approve",
                candidate_id=candidate.id,
                evidence_source=candidate.evidence_source or candidate.source,
                signal_actor=candidate.signal_actor,
                candidate_actor=candidate.candidate_actor,
                approval_actor="human",
                parent_candidate_id=candidate.parent_candidate_id,
                source_task_id=candidate.source_task_id,
                idempotency_key=_event_idempotency_key(
                    f"evolve-approve:{candidate.id}"
                ),
                **self._profile_task_options(),
            )
        except (OSError, ValueError):
            self._record_evolve_event(
                "skipped",
                event_actor="human",
                trigger="/evolve approve",
                candidate=candidate,
                reason="queue-failed",
                proposal_id=proposal_id,
            )
            return "Enoch could not approve and queue that evolve candidate."
        candidate = approve_evolve_candidate(
            candidate.id,
            self.root,
            theme=state.theme,
        )
        self._record_evolve_event(
            "queued",
            event_actor="human",
            trigger="/evolve approve",
            candidate=candidate,
            task_id=job.id,
            approval_actor="human",
            proposal_id=proposal_id,
        )
        return (
            f"Approved evolve candidate {candidate.id} and handed it off to "
            f"task #{job.id}.\n"
            f"Use /tasks to follow it and /task retry {job.id} if it fails.\n\n"
            + "\n".join(_format_evolve_candidate(candidate))
        )

    def _evolve_schedule(self, argument: str) -> str:
        text = _unquote_schedule_text(argument)
        if not text:
            return _format_evolve_report(evolve_report(self.root))
        parts = text.split(maxsplit=1)
        subcommand = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if subcommand in {"off", "disable", "disabled"}:
            disable_evolve_schedule(self.root)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand == "once":
            return self._evolve_schedule_once(rest)
        if subcommand == "daily":
            if not rest.strip():
                return (
                    "Use /evolve config schedule daily HH:MM to run evolve "
                    "once per day at local time."
                )
            try:
                set_evolve_daily_schedule(rest, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand == "cron":
            if not rest.strip():
                return (
                    "Use /evolve config schedule cron '30 9 * * *' to run "
                    "evolve with a cron-style schedule."
                )
            try:
                set_evolve_cron_schedule(rest, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand != "every":
            return self._apply_evolve_schedule_text(text)
        if not rest.strip():
            return (
                "Use /evolve config schedule every <interval> to set the "
                "scheduler frequency."
            )
        try:
            interval_seconds = parse_cron_interval(rest)
            set_evolve_schedule(interval_seconds, self.root)
        except ValueError as error:
            interpreted = self._apply_evolve_schedule_text(text)
            if "could not understand" not in interpreted:
                return interpreted
            return str(error)
        return _format_evolve_report(evolve_report(self.root))

    def _evolve_schedule_once(self, argument: str) -> str:
        normalized = argument.strip().lower()
        if normalized in {"a day", "per day", "daily"}:
            set_evolve_schedule(24 * 60 * 60, self.root)
            return _format_evolve_report(evolve_report(self.root))
        prefix = "a day at "
        if normalized.startswith(prefix):
            daily_time = argument.strip()[len(prefix) :].strip()
            try:
                set_evolve_daily_schedule(daily_time, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        return _evolve_usage()

    def _apply_evolve_schedule_text(self, text: str) -> str:
        normalized = text.strip().lower()
        if normalized in {"once a day", "once daily", "daily", "every day"}:
            set_evolve_schedule(24 * 60 * 60, self.root)
            return _format_evolve_report(evolve_report(self.root))
        for prefix in ("once a day at ", "daily at ", "every day at "):
            if normalized.startswith(prefix):
                daily_time = text.strip()[len(prefix) :].strip()
                try:
                    set_evolve_daily_schedule(daily_time, self.root)
                except ValueError as error:
                    return str(error)
                return _format_evolve_report(evolve_report(self.root))
        try:
            set_evolve_cron_schedule(text, self.root)
            return _format_evolve_report(evolve_report(self.root))
        except ValueError:
            pass
        try:
            interval_seconds = parse_cron_interval(text)
            set_evolve_schedule(interval_seconds, self.root)
            return _format_evolve_report(evolve_report(self.root))
        except ValueError:
            return "Enoch could not understand that schedule. Try once a day, once a day at 09:30, every 1d, or 30 9 * * *."

    def _cron(self, chat_id: int, text: str) -> str:
        command, argument = _parse_chat_command(text)
        if command != "/cron":
            return _cron_usage()
        if not argument:
            return _format_cron_report(self.root)

        first, _separator, rest = argument.partition(" ")
        subcommand = first.lower()
        if subcommand == "cancel":
            job_id = _cron_job_id(rest)
            if job_id is None:
                return "Use /cron cancel <id> to cancel a scheduled job."
            cancelled = cancel_cron_job(job_id, self.root)
            if cancelled is None:
                return f"Enoch could not cancel cron #{job_id}. It may already be cancelled or missing."
            self._cron_scheduler_wake.set()
            return f"Cancelled cron #{cancelled.id}."
        if subcommand != "every":
            return _cron_usage()

        interval_text, _space, request = rest.partition(" ")
        if not interval_text or not request.strip():
            return "Use /cron every <interval> <request> to schedule recurring work."
        try:
            interval_seconds = parse_cron_interval(interval_text)
        except ValueError as error:
            return str(error)
        snapshot = self._resolve_task_context_snapshot(chat_id, request)
        if snapshot.codex_unavailable_reason:
            return (
                "Enoch could not prepare conversation context for that scheduled "
                f"job, so no cron job was created: {snapshot.codex_unavailable_reason}"
            )
        if snapshot.error:
            return f"Enoch could not prepare conversation context for that scheduled job yet: {snapshot.error}"
        if snapshot.clarification:
            return f"Enoch needs one clarification before scheduling that job: {snapshot.clarification}"
        try:
            job = add_cron_job(
                chat_id,
                request,
                interval_seconds,
                self.root,
                context=snapshot.context,
                context_source=snapshot.source,
                idempotency_key=_event_idempotency_key("cron-add"),
            )
        except (OSError, ValueError):
            return "Enoch could not schedule that cron job."
        self._cron_scheduler_wake.set()
        return "\n".join(
            [
                f"Cron #{job.id} scheduled every {format_cron_interval(job.interval_seconds)}.",
                f"Next run: {job.next_run_at}",
            ]
        )

    def _maybe_start_task_worker(self) -> None:
        with self._task_worker_lock:
            if self._stopping:
                return
            if self._task_worker is not None and self._task_worker.is_alive():
                return
            status = self.workflow.inspect()
            if status.running is not None or status.paused_count:
                return
            if status.pending_count == 0 and self._promote_next_backlog_if_idle() is None:
                return
            self._task_worker = threading.Thread(
                target=self._run_task_worker,
                name="enoch-task-worker",
                daemon=True,
            )
            self._task_worker.start()

    def _run_task_worker(self) -> None:
        try:
            while not self._stopping:
                job = self.workflow.start_next()
                if job is None:
                    if self._promote_next_backlog_if_idle() is None:
                        return
                    job = self.workflow.start_next()
                    if job is None:
                        return
                self._run_task_job(job)
                self._cron_scheduler_wake.set()
                try:
                    self._enqueue_due_cron_jobs()
                except Exception as error:
                    print(f"Enoch cron scheduler error after task completion: {error}")
                if self.workflow.inspect().paused_count:
                    return
        except StaleDaemonEpoch:
            return

    def _promote_next_backlog_if_idle(self) -> TaskJob | None:
        status = self.workflow.inspect()
        if status.running is not None or status.pending_count > 0 or status.paused_count > 0:
            return None
        item = next_backlog_item(self.root)
        if item is None:
            return None
        return self._enqueue_backlog_item(item, event_actor="system", trigger="backlog-idle")

    def _promote_backlog_item_to_queue(self, item_id: int) -> TaskJob | None:
        item = backlog_item(item_id, self.root)
        if item is None:
            return None
        return self._enqueue_backlog_item(item, event_actor="human", trigger="/backlog promote")

    def _enqueue_backlog_item(self, item: BacklogItem, *, event_actor: str, trigger: str) -> TaskJob:
        job = self.workflow.enqueue(
            item.chat_id,
            item.text,
            context=item.context,
            context_source=item.context_source,
            source="backlog",
            initiated_by="human",
            event_actor=event_actor,
            trigger=trigger,
            idempotency_key=f"backlog:{item.id}",
            **self._profile_task_options(),
        )
        promoted = promote_backlog_item(item.id, self.root, promoted_task_id=job.id)
        if promoted is None:
            return job
        message = self._format_work_status(
            WorkStatusMessage(
                chat_id=item.chat_id,
                message_id=0,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="queued",
                latest_update=f"Promoted from backlog #{item.id} ({item.priority}).",
                context=job.context,
            )
        )
        message_id = self._safe_send_message_id(
            item.chat_id,
            message,
            notification_key=f"task:{job.id}:status",
        )
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            self.workflow.record_status_message(job.id, message_id)
        return job

    def _enqueue_due_cron_jobs(self) -> tuple[TaskJob, ...]:
        claimed = tuple(
            sorted(
                claim_due_cron_jobs(self.root),
                key=lambda cron: (cron.next_run_at, cron.id),
            )
        )
        eligible = tuple(
            cron
            for cron in claimed
            if not self._cron_task_is_outstanding(cron)
        )
        enqueued: dict[int, TaskJob] = {}
        for cron in reversed(eligible):
            try:
                job = self.workflow.enqueue(
                    cron.chat_id,
                    cron.text,
                    mode="front",
                    context=cron.context,
                    context_source=f"cron:{cron.context_source}" if cron.context_source else "cron",
                    source="task",
                    initiated_by="human",
                    event_actor="system",
                    trigger=f"cron:{cron.id}",
                    idempotency_key=f"cron:{cron.id}:{cron.claim_id}",
                    **self._profile_task_options(),
                )
            except (OSError, ValueError):
                continue
            record_cron_task(
                cron.id,
                job.id,
                self.root,
                claim_id=cron.claim_id,
            )
            enqueued[cron.id] = job

        jobs: list[TaskJob] = []
        for cron in eligible:
            job = enqueued.get(cron.id)
            if job is None:
                continue
            jobs.append(job)
            message = self._format_work_status(
                WorkStatusMessage(
                    chat_id=cron.chat_id,
                    message_id=0,
                    request=job.text,
                    started_at=time.monotonic(),
                    task_id=job.id,
                    status="queued",
                    latest_update=f"Scheduled by cron #{cron.id}.",
                    context=job.context,
                )
            )
            message_id = self._safe_send_message_id(
                cron.chat_id,
                message,
                notification_key=f"task:{job.id}:status",
            )
            if message_id is not None:
                self._work_status_messages[job.id] = message_id
                self.workflow.record_status_message(job.id, message_id)
        return tuple(jobs)

    def _cron_task_is_outstanding(self, cron: CronJob) -> bool:
        if cron.last_task_id is None:
            return False
        task = self.workflow.find(cron.last_task_id)
        return task is not None and task.status in {"pending", "running", "paused"}

    def _scan_evidence_sources(
        self,
        chat_id: ConversationId,
        *,
        source: str,
        force: bool,
        drain: bool = False,
        reason: str,
    ) -> tuple[EvidenceScanResult, ...]:
        sources = (
            ("feedback", "experience")
            if source == "all"
            else (source,)
        )
        results: list[EvidenceScanResult] = []
        for evidence_source in sources:
            while True:
                result = scan_evidence(
                    evidence_source,
                    self.root,
                    generator=lambda prompt, selected=evidence_source: (
                        self._respond_isolated_evidence_turn(
                            chat_id,
                            prompt,
                            phase=f"{selected}-scan",
                        )
                    ),
                    force=force,
                    reason=reason,
                )
                results.append(result)
                if (
                    not drain
                    or result.status != "completed"
                    or result.remaining <= 0
                ):
                    break
        return tuple(results)

    def _run_due_evidence_scans(self) -> tuple[EvidenceScanResult, ...]:
        if load_evolve_state(self.root).mode == MODE_DISABLED:
            return ()
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return ()
        if self._task_worker is not None and self._task_worker.is_alive():
            return ()
        return self._scan_evidence_sources(
            chat_id,
            source="all",
            force=False,
            reason="threshold",
        )

    def _run_due_evolve_schedule(self) -> TaskJob | None:
        claimed = claim_due_evolve_schedule(self.root)
        if claimed is None:
            return None
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            self._record_evolve_event(
                "checked",
                event_actor="system",
                trigger="evolve-scheduler",
                reason="schedule-due",
            )
            self._record_evolve_event(
                "skipped",
                event_actor="system",
                trigger="evolve-scheduler",
                reason="chat-not-locked",
            )
            acknowledge_evolve_schedule(
                claimed.schedule_claim_id,
                self.root,
            )
            return None
        if claimed.mode == MODE_DISABLED:
            self._record_evolve_event(
                "checked",
                event_actor="system",
                trigger="evolve-scheduler",
                reason="schedule-due",
            )
            self._record_evolve_event(
                "skipped",
                event_actor="system",
                trigger="evolve-scheduler",
                reason="mode-disabled",
            )
            acknowledge_evolve_schedule(
                claimed.schedule_claim_id,
                self.root,
            )
            return None
        proposal = self._propose_evolve(chat_id, trigger="evolve-scheduler")
        if proposal.top_candidate is not None:
            self._record_evolve_event(
                "skipped",
                event_actor="system",
                trigger="evolve-scheduler",
                proposal=proposal,
                candidate=proposal.top_candidate,
                reason="awaiting-human-approval",
            )
        self._safe_send_message(
            chat_id,
            "Scheduled evolve check\n\n" + _format_evolve_proposal(proposal),
            notification_key=f"evolve-schedule:{claimed.schedule_claim_id}:report",
        )
        acknowledge_evolve_schedule(
            claimed.schedule_claim_id,
            self.root,
        )
        return None

    def _run_task_job(self, job: TaskJob) -> None:
        self._run_action_job(
            job,
            command="/task",
            session_key=f"{self._session_key(job.chat_id)}:task:{job.id}",
            start_update=f"Starting queued task #{job.id}.",
            failure_prefix=f"Enoch could not complete queued task #{job.id}",
        )

    def _run_action_job(
        self,
        job: TaskJob,
        *,
        command: str,
        session_key: str,
        start_update: str,
        failure_prefix: str,
    ) -> None:
        worker_id = f"{os.getpid()}-{uuid4().hex}"
        claimed = self.workflow.claim(job.id, worker_id, os.getpid())
        if claimed is None:
            return
        job = claimed
        message_id = self._work_status_messages.get(job.id) or job.status_message_id
        created_status_message = False
        if message_id is None:
            status_message = WorkStatusMessage(
                chat_id=job.chat_id,
                message_id=0,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="running",
                latest_update=start_update,
                reviews=list(job.review_urls),
                context=job.context,
            )
            message_id = self._safe_send_message_id(
                job.chat_id,
                self._format_work_status(status_message),
                notification_key=f"task:{job.id}:status",
            )
            if message_id is not None:
                created_status_message = True
                self._work_status_messages[job.id] = message_id
                self.workflow.record_status_message(job.id, message_id)
        task_status = WorkStatusMessage(
            chat_id=job.chat_id,
            message_id=message_id or 0,
            request=job.text,
            started_at=time.monotonic(),
            task_id=job.id,
            status="running",
            latest_update=start_update,
            reviews=list(job.review_urls),
            context=job.context,
        )
        token = _CURRENT_WORK_STATUS.set(task_status)
        task_token = _CURRENT_TASK_ID.set(job.id)
        worker_token = _CURRENT_TASK_WORKER_ID.set(worker_id)
        regression_token = _CURRENT_REGRESSION_SIGNALS.set(())
        cancellation_event = threading.Event()
        self._task_cancellations[job.id] = cancellation_event
        deadline = _start_task_deadline(
            self.root,
            cancellation_event,
            timeout_seconds=job.timeout_seconds,
        )
        execution = RuntimeExecutionControl(
            request_id=f"task:{job.id}:attempt:{job.attempt}",
            session_key=session_key,
            timeout_seconds=deadline.timeout_seconds,
            cancellation_event=cancellation_event,
            timeout_event=deadline.expired,
            progress_callback=lambda progress: self._send_progress(
                job.chat_id,
                progress.elapsed_seconds,
                progress.sandbox,
            ),
        )
        if not created_status_message:
            self._update_work_status(start_update, status="running")
        completed_status = "completed"
        finished_job: TaskJob | None = None
        failure: TaskFailure | None = None
        regression_signals: tuple[TaskRegressionSignal, ...] = ()
        try:
            self._authorize_task(job)
            if job.result and job.review_urls:
                reply = job.result
                outcome = WorkOutcome.completed(reply)
            elif job.publish_stage in {
                "committed",
                "pushed",
                "pr_opened",
                "captured",
                "review_published",
            }:
                outcome = self._resume_task_publish(job)
            elif job.review_urls:
                reply = job.result
                outcome = WorkOutcome.completed(reply)
            elif not self._action_allowed():
                reply = self._action_lock_message()
                outcome = WorkOutcome.failure(
                    reply,
                    code="action_locked",
                    failure_class="permanent",
                )
            else:
                outcome = _coerce_work_outcome(
                    self._run_direct_work(
                        job.chat_id,
                        job.text,
                        context=_task_worker_context(job),
                        session_key=session_key,
                        execution=execution,
                    )
                )
            reply = outcome.message
            reply = self._capture_task_regression_signals(reply)
            if deadline.expired.is_set():
                reply = _task_timeout_message(deadline.timeout_seconds)
                completed_status = "failed"
                failure = classify_task_failure(reply)
            elif outcome.failed:
                completed_status = "failed"
                failure = TaskFailure(
                    code=outcome.code or "unknown_failure",
                    failure_class=outcome.failure_class or "permanent",
                    retryable=outcome.retryable,
                )
        except CapabilityAuthorizationError as error:
            reply = str(error)
            completed_status = "failed"
            failure = TaskFailure(
                code="authorization_denied",
                failure_class="permanent",
                retryable=False,
            )
        except AgentRuntimeAccessUnavailable as error:
            reply = _codex_pause_warning(job.id, str(error))
            completed_status = "paused"
        except AgentRuntimeTimedOut:
            deadline.expired.set()
            reply = _task_timeout_message(deadline.timeout_seconds)
            completed_status = "failed"
            failure = classify_task_failure(reply)
        except AgentRuntimeCancelled as error:
            if deadline.expired.is_set():
                reply = _task_timeout_message(deadline.timeout_seconds)
                completed_status = "failed"
                failure = classify_task_failure(reply)
            else:
                reply = str(error)
                completed_status = "cancelled"
        except Exception as error:
            reply = f"{failure_prefix}: {error}"
            completed_status = "failed"
            failure = classify_task_failure(reply)
        finally:
            deadline.cancel()
            regression_signals = _CURRENT_REGRESSION_SIGNALS.get()
            _CURRENT_REGRESSION_SIGNALS.reset(regression_token)
            _CURRENT_WORK_STATUS.reset(token)
            _CURRENT_TASK_ID.reset(task_token)
            _CURRENT_TASK_WORKER_ID.reset(worker_token)
            self._task_cancellations.pop(job.id, None)
            try:
                require_current_daemon_epoch(self.daemon_epoch, self.root)
            except StaleDaemonEpoch:
                return
            if completed_status == "cancelled":
                finished_job = self.workflow.finalize(
                    job.id,
                    "cancelled",
                    result=reply,
                    event_actor="system",
                    trigger="task-runner-cancelled",
                    worker_id=worker_id,
                )
            elif completed_status == "failed":
                failure_actor = "system" if deadline.expired.is_set() else "agent"
                failure_trigger = "task-timeout" if deadline.expired.is_set() else "task-runner"
                failure = failure or classify_task_failure(reply)
                if failure.retryable and job.attempt < job.max_attempts:
                    finished_job = self.workflow.retry_running(
                        job.id,
                        result=reply,
                        failure_code=failure.code,
                        failure_class=failure.failure_class,
                        worker_id=worker_id,
                        delay_seconds=automatic_retry_delay_seconds(job.attempt),
                        event_actor=failure_actor,
                        trigger=failure_trigger,
                    )
                    if finished_job is not None:
                        completed_status = "retrying"
                if finished_job is None:
                    finished_job = self.workflow.finalize(
                        job.id,
                        "failed",
                        result=reply,
                        event_actor=failure_actor,
                        trigger=failure_trigger,
                        worker_id=worker_id,
                        failure_code=failure.code,
                        failure_class=failure.failure_class,
                        retryable=False,
                    )
            elif completed_status == "paused":
                finished_job = self.workflow.pause(
                    job.id,
                    result=reply,
                    event_actor="system",
                    trigger="runtime-unavailable",
                    worker_id=worker_id,
                )
            else:
                finished_job = self.workflow.finalize(
                    job.id,
                    "completed",
                    result=reply,
                    worker_id=worker_id,
                )
        authoritative_job = finished_job or self.workflow.find(job.id)
        expected_status = "pending" if completed_status == "retrying" else completed_status
        if authoritative_job is None or authoritative_job.status != expected_status:
            return
        self._apply_task_regression_signals(
            regression_signals,
            current_task_id=job.id if completed_status == "completed" else None,
            allow_resolution=completed_status == "completed",
        )
        summary_job = authoritative_job
        if completed_status == "retrying":
            if task_status is not None:
                retry_token = _CURRENT_WORK_STATUS.set(task_status)
                try:
                    self._update_work_status(
                        (
                            f"Transient failure ({summary_job.failure_code}); "
                            f"retry {summary_job.attempt + 1}/{summary_job.max_attempts} scheduled."
                        ),
                        status="retrying",
                    )
                finally:
                    _CURRENT_WORK_STATUS.reset(retry_token)
            if command == "/do":
                self._maybe_start_task_worker()
            return
        if completed_status == "completed":
            self.effect_fence.run(
                _cleanup_completed_task_worktree,
                summary_job,
                self.root,
            )
            self._record_automatic_learning(summary_job, command=command, result=reply)
        if task_status is not None:
            task_status.reviews = list(summary_job.review_urls)
            final_token = _CURRENT_WORK_STATUS.set(task_status)
            try:
                self._update_work_status(
                    _final_task_status_update(completed_status),
                    status=completed_status,
                )
            finally:
                _CURRENT_WORK_STATUS.reset(final_token)
            if completed_status != "paused":
                self._work_status_messages.pop(job.id, None)
        self._safe_send_message(
            job.chat_id,
            self._format_task_final(summary_job, completed_status, reply),
            notification_key=f"task:{job.id}:final",
        )
        self._record_turn(job.chat_id, f"{command} {job.text}", reply)
        if command == "/do":
            self._maybe_start_task_worker()

    def _current_task_cancellation_event(self) -> threading.Event | None:
        task_id = _CURRENT_TASK_ID.get()
        if task_id is None:
            return None
        return self._task_cancellations.get(task_id)

    def _raise_if_current_task_cancelled(self) -> None:
        self.effect_fence.require_current()
        cancellation_event = self._current_task_cancellation_event()
        if cancellation_event is not None and cancellation_event.is_set():
            raise AgentRuntimeCancelled("Enoch cancelled the active task.")

    def _record_automatic_learning(self, job: TaskJob, *, command: str, result: str) -> None:
        try:
            self.effect_fence.run(
                record_learning_artifact,
                self.identity,
                request=job.text,
                result=result,
                root=self.root,
                task_id=job.id,
                command=command,
                context_source=job.context_source,
                pr_urls=job.review_urls,
            )
        except (OSError, ValueError):
            return

    def _ancestors(self, chat_id: int, text: str) -> str:
        return lineage_command(
            text,
            self.root,
            command_name="ancestors",
            resolve_lineage_fn=resolve_lineage,
        )

    def _inherit(self, chat_id: int, text: str) -> str:
        parts = text.split(maxsplit=2)
        subcommand = parts[1].lower() if len(parts) >= 2 else ""
        argument = parts[2].strip() if len(parts) >= 3 else ""
        if subcommand != "inbox":
            self._reconcile_lineage_adoptions()
        if not subcommand:
            return self._scan_and_queue_lineage_assessment(chat_id)
        if subcommand not in {"inbox", "inspect", "ignore"}:
            return self._adopt_lineage_candidate(chat_id, subcommand)
        reply = inherit_command(
            text,
            self.root,
            command_name="inherit",
        )
        if subcommand == "inspect":
            candidate = find_parent_inbox_candidate(argument, self.root)
            if candidate is not None:
                self._queue_session_sync(chat_id, lineage_candidate_context(candidate))
        return reply

    def _scan_and_queue_lineage_assessment(self, chat_id: int) -> str:
        try:
            active = load_lineage_assessment_queue(self.root).current
            if active is not None:
                return "\n".join(
                    [
                        (
                            "Inheritance assessment is already "
                            f"{active.status} for {active.total_count} change(s)."
                        ),
                        "No additional scan was started.",
                        "Use /inherit inbox to view the stored inbox while it runs.",
                    ]
                )
            report = refresh_lineage_inbox(self.root, scope="parent")
            candidates = lineage_assessment_candidates(report, self.root)
            if not candidates:
                return format_parent_inherit_report(report)
            job, created = enqueue_lineage_assessment(
                chat_id,
                tuple(candidate.id for candidate in candidates),
                self.root,
                new_count=report.new_count,
            )
        except (LineageError, OSError, RuntimeError, ValueError) as error:
            return f"Enoch could not scan the inheritance inbox: {error}"
        if not created:
            return (
                f"Inheritance assessment is already {job.status}. "
                "Use /inherit inbox to view the stored inbox."
            )
        return format_inheritance_scan_queued(report, job.total_count)

    def _maybe_start_lineage_worker(self) -> None:
        if self._stopping:
            return
        with self._lineage_worker_lock:
            if self._lineage_worker is not None and self._lineage_worker.is_alive():
                return
            job = claim_lineage_assessment(
                self.daemon_epoch.token,
                self.root,
            )
            if job is None:
                return
            worker = threading.Thread(
                target=self._run_lineage_assessment_worker,
                args=(job,),
                name=f"enoch-lineage-assessment-{job.id[-8:]}",
                daemon=True,
            )
            self._lineage_worker = worker
            worker.start()

    def _run_lineage_assessment_worker(
        self,
        job: LineageAssessmentJob,
    ) -> None:
        try:
            report = load_lineage_inbox_report(self.root, scope="parent")
            assess_lineage_inbox(
                report,
                self.root,
                generator=lambda prompt: self._respond_isolated_evidence_turn(
                    job.conversation_id,
                    prompt,
                    phase="lineage-assessment",
                ),
                mission=self.identity.mission,
                candidate_ids=job.candidate_ids,
                progress_callback=lambda progress: self._lineage_assessment_progress(
                    job,
                    progress,
                ),
                guard=lambda: require_current_daemon_epoch(
                    self.daemon_epoch,
                    self.root,
                ),
            )
            require_current_daemon_epoch(self.daemon_epoch, self.root)
            assessed_count, failed_count = self._lineage_assessment_counts(job)
            final_report = load_lineage_inbox_report(self.root, scope="parent")
            self._safe_send_message(
                job.conversation_id,
                format_lineage_assessment_complete(
                    final_report.candidates,
                    assessed_count=assessed_count,
                    failed_count=failed_count,
                ),
                notification_key=f"lineage-assessment:{job.id}:final",
            )
            complete_lineage_assessment(
                job.id,
                self.root,
                owner_epoch=job.owner_epoch,
                assessed_count=assessed_count,
                failed_count=failed_count,
            )
        except StaleDaemonEpoch:
            return
        except Exception as error:
            failed = fail_lineage_assessment(
                job.id,
                str(error),
                self.root,
                owner_epoch=job.owner_epoch,
            )
            if failed is not None:
                self._safe_send_message(
                    job.conversation_id,
                    "\n".join(
                        [
                            "Inheritance assessment stopped.",
                            f"Failure: {' '.join(str(error).split())[:1000]}",
                            "The inbox was preserved. Run /inherit to retry.",
                        ]
                    ),
                    notification_key=f"lineage-assessment:{job.id}:failed",
                )
        finally:
            with self._lineage_worker_lock:
                if self._lineage_worker is threading.current_thread():
                    self._lineage_worker = None

    def _lineage_assessment_progress(
        self,
        job: LineageAssessmentJob,
        progress: LineageAssessmentProgress,
    ) -> None:
        if progress.processed_count >= progress.total_count:
            return
        stride = max(1, (progress.batch_count + 3) // 4)
        if progress.batch_index != 1 and progress.batch_index % stride:
            return
        require_current_daemon_epoch(self.daemon_epoch, self.root)
        assessed_count, failed_count = self._lineage_assessment_counts(job)
        processed = min(job.total_count, assessed_count + failed_count)
        self._safe_send_message(
            job.conversation_id,
            "\n".join(
                [
                    "Inheritance assessment progress",
                    f"Processed: {processed}/{job.total_count}",
                    f"Assessed: {assessed_count}",
                    f"Failed: {failed_count}",
                    "Enoch remains available for other messages.",
                ]
            ),
            notification_key=(
                f"lineage-assessment:{job.id}:progress:{processed}"
            ),
        )

    def _lineage_assessment_counts(
        self,
        job: LineageAssessmentJob,
    ) -> tuple[int, int]:
        selected = set(job.candidate_ids)
        candidates = (
            candidate
            for candidate in load_inbox_candidates(
                self.root,
                include_inactive=True,
            )
            if candidate.id in selected
        )
        assessed_count = 0
        failed_count = 0
        for candidate in candidates:
            if candidate.assessment_status == ASSESSMENT_ASSESSED:
                assessed_count += 1
            elif candidate.assessment_status == ASSESSMENT_FAILED:
                failed_count += 1
        return assessed_count, failed_count

    def _adopt_lineage_candidate(self, chat_id: int, candidate_id: str) -> str:
        if not candidate_id:
            return "Use /inherit <change_id>."
        candidate = _find_lineage_adopt_candidate(candidate_id, self.root)
        if candidate is None:
            return f"Enoch could not find direct-parent change {candidate_id}. Run /inherit first."
        if candidate.status == STATUS_ADOPTED:
            return (
                f"Direct-parent change {candidate.id} is already adopted"
                + (
                    f" at revision {candidate.adopted_revision}."
                    if candidate.adopted_revision
                    else "."
                )
            )
        if candidate.status == STATUS_LINKED:
            return (
                f"Direct-parent change {candidate.id} is already linked to "
                f"task #{candidate.linked_task_id}."
            )
        if candidate.assessment_status != ASSESSMENT_ASSESSED:
            return (
                f"Direct-parent change {candidate.id} has not been assessed successfully. "
                "Run /inherit to let Codex assess or retry it first."
            )
        if not self._action_allowed():
            return self._action_lock_message()
        try:
            job = self.workflow.enqueue(
                chat_id,
                lineage_adaptation_request(candidate),
                context=lineage_candidate_context(candidate),
                context_source=lineage_context_source(candidate.id),
                source="inheritance",
                initiated_by="human",
                event_actor="human",
                trigger="/inherit",
                idempotency_key=_event_idempotency_key(
                    f"inherit:{candidate.id}"
                ),
                **self._profile_task_options(),
            )
        except (OSError, RuntimeError, ValueError):
            return f"Enoch could not queue inheritance task for {candidate.id}."
        try:
            link_inbox_candidate(
                candidate.id,
                job.id,
                self.root,
            )
        except LineageError as error:
            self.workflow.cancel(
                job.id,
                result=f"Could not link lineage change: {error}",
                event_actor="system",
                trigger="lineage-link-failed",
            )
            return f"Enoch could not link {candidate.id} to its task: {error}"
        return (
            f"Queued task #{job.id} to adapt direct-parent change {candidate.id} "
            "through the standard worktree, validation, commit, push, and PR workflow."
        )

    def _reconcile_lineage_adoptions(self) -> None:
        status = self.workflow.inspect()
        tasks = tuple(
            job
            for job in (
                status.running,
                *status.pending,
                *status.paused,
                *status.history,
            )
            if job is not None
        )
        reconcile_lineage_adoptions(
            self.root,
            tasks,
            review=self.review,
        )

    def _doctor(self) -> str:
        return doctor_command(
            self.root,
            run_doctor=run_immune_system,
            format_doctor=_format_doctor_result,
        )

    def _worktree(self, chat_id: ConversationId, argument: str) -> str:
        parts = argument.split()
        if not parts or (len(parts) == 1 and parts[0].lower() == "list"):
            try:
                self.authorization.require("vcs.list-worktrees", ("vcs.read",))
                states = list_task_worktrees(self.root)
            except (VcsError, CapabilityAuthorizationError) as error:
                return f"Enoch could not list task worktrees: {error}"
            return _format_task_worktrees(states, self.workflow.inspect())

        action = parts[0].lower()
        if action == "show" and len(parts) == 2:
            task_id = _positive_task_id(parts[1])
            if task_id is None:
                return worktree_usage()
            try:
                self.authorization.require("vcs.inspect-worktree", ("vcs.read",))
                state = task_worktree_state(self.root, task_id)
            except (VcsError, CapabilityAuthorizationError) as error:
                return f"Enoch could not inspect task #{task_id} worktree: {error}"
            if state is None:
                return f"Task #{task_id} has no registered task worktree."
            return _format_task_worktree(state, self.workflow.inspect())

        cleanup = action == "cleanup" and len(parts) == 2
        discard = action == "discard" and len(parts) == 3 and parts[2].lower() == "force"
        if not cleanup and not discard:
            return worktree_usage()
        task_id = _positive_task_id(parts[1])
        if task_id is None:
            return worktree_usage()
        if not self._action_allowed():
            return self._action_lock_message()
        try:
            state = task_worktree_state(self.root, task_id)
        except VcsError as error:
            return f"Enoch could not inspect task #{task_id} worktree: {error}"
        if state is None:
            return f"Task #{task_id} has no registered task worktree."
        active = _active_tasks_for_worktree(state, self.workflow.inspect())
        if active:
            labels = ", ".join(f"#{job.id} [{job.status}]" for job in active)
            return (
                f"Enoch will not remove task #{task_id} worktree because it is still "
                f"used by {labels}."
            )
        try:
            result = self.effect_fence.run_authorized(
                "vcs.remove-worktree",
                ("vcs.write",),
                remove_managed_task_worktree,
                self.root,
                task_id,
                discard=discard,
            )
        except (VcsError, CapabilityAuthorizationError) as error:
            return f"Enoch could not remove task #{task_id} worktree: {error}"
        self.effect_fence.run(
            _record_system_event,
            "task_worktree_discarded" if discard else "task_worktree_cleaned",
            self.root,
            details={
                "task_id": task_id,
                "path": str(state.path),
                "branch": state.branch,
            },
        )
        return result

    def _pr(self, chat_id: int, argument: str) -> str:
        parts = argument.split()
        if not parts or (len(parts) == 1 and parts[0].lower() == "list"):
            try:
                self.authorization.require("forge.list", ("forge.inspect",))
                reviews = self.review.list_open_reviews(self.root)
            except (ReviewProviderError, CapabilityAuthorizationError) as error:
                return f"Enoch could not list open reviews: {error}"
            return _format_open_reviews(reviews)
        if len(parts) == 2 and parts[0].lower() == "show":
            try:
                self.authorization.require("forge.inspect", ("forge.inspect",))
                review = self.review.inspect_review(
                    _review_identity(parts[1]),
                    self.root,
                )
            except (ReviewProviderError, CapabilityAuthorizationError) as error:
                return f"Enoch could not inspect that review: {error}"
            return _format_review(review)
        if len(parts) != 2 or parts[0].lower() != "merge":
            return pr_usage()
        allowed_chat_id = _allowed_conversation_id(self.client)
        if allowed_chat_id is None or allowed_chat_id != chat_id:
            return (
                "Enoch will only land a review from her locked "
                f"{provider_label(self.channel_name)} conversation."
            )
        try:
            result = self.effect_fence.run_authorized(
                "forge.land",
                ("forge.land",),
                self.review.land_review,
                ReviewLandRequest(_review_identity(parts[1])),
                root=self.root,
            )
        except (ReviewProviderError, CapabilityAuthorizationError) as error:
            return f"Enoch could not land that review: {error}"
        self._reconcile_lineage_adoptions()
        return _format_review_land_result(result)

    def _update(self) -> str:
        if not self._action_allowed():
            return self._action_lock_message()
        try:
            result = self.effect_fence.run_authorized(
                "vcs.update",
                ("vcs.inspect", "vcs.authoritative", "vcs.ancestry", "vcs.restore"),
                update_from_authoritative,
                self.root,
                repository=self.repository,
            )
        except CapabilityAuthorizationError as error:
            return str(error)
        if result.direct_action_result:
            _record_direct_action(
                "update from authoritative repository",
                result.direct_action_result,
                self.root,
            )
        if result.restart_required:
            self._restart_after_reply = True
        return result.message

    def _send_progress(self, chat_id: int, elapsed_seconds: int, sandbox: str) -> None:
        task_id = _CURRENT_TASK_ID.get()
        worker_id = _CURRENT_TASK_WORKER_ID.get()
        if task_id is not None and worker_id:
            if self.workflow.heartbeat(task_id, worker_id) is None:
                raise RuntimeError(f"Task #{task_id} lost its workflow claim.")
        mode = _sandbox_description(sandbox)
        if self._update_work_status(f"Still working after {_format_elapsed(elapsed_seconds)}: {mode}."):
            return
        self._safe_send_message(chat_id, f"Enoch is still working after {_format_elapsed(elapsed_seconds)}: {mode}.")

    def _action_allowed(self) -> bool:
        return _allowed_conversation_id(self.client) is not None

    def _action_lock_message(self) -> str:
        return _format_action_lock_message(self.channel_name)

    def _restart_from_chat(self) -> str:
        if _allowed_conversation_id(self.client) is None:
            label = provider_label(self.channel_name)
            return "\n".join(
                [
                    f"Enoch will not restart from {label} unless it is locked to one conversation.",
                    self._action_lock_message(),
                ]
            )
        self._restart_after_reply = True
        return "\n".join(
            [
                "Enoch is restarting.",
                "Daemon mode will restart after this reply is delivered.",
            ]
        )

    def _record_turn(self, chat_id: ConversationId, text: str, reply: str) -> None:
        try:
            self.effect_fence.run(
                log_conversation_turn,
                chat_id=chat_id,
                message=text,
                reply=reply,
                root=self.root,
            )
            self.effect_fence.run(ensure_long_term_memory, self.root)
        except OSError:
            return


def main(chat_provider_name: str = "") -> None:
    root = repo_root()
    identity = load_identity()
    try:
        chat_provider = load_provider("chat", root, name=chat_provider_name)
        runtime_provider = load_provider("runtime", root)
        forge_provider = load_provider("forge", root)
        profile = load_profile(root)
        extensions = load_extensions(root)
    except (
        ProviderError,
        ChatProviderError,
        ProfileError,
        AgentExtensionError,
    ) as error:
        print(str(error))
        raise SystemExit(1) from error
    selected_channel = _chat_provider_name(chat_provider)
    daemon_epoch = begin_daemon_epoch(root, provider=selected_channel)
    previous_shutdown_warning = _begin_lifecycle_run(root, provider=selected_channel)
    _record_system_event(
        "startup",
        root,
        details={
            "identity": identity.name,
            "previous_shutdown_warning": previous_shutdown_warning,
            "extensions": [extension.name for extension in extensions],
        },
    )
    bot = EnochApplication(
        identity=identity,
        root=root,
        client=chat_provider,
        previous_shutdown_warning=previous_shutdown_warning,
        runtime=runtime_provider,
        forge=forge_provider,
        profile=profile,
        extensions=extensions,
        daemon_epoch=daemon_epoch,
    )
    _install_shutdown_handlers()
    bot.start()
    provider_label = str(getattr(chat_provider, "name", "chat")).strip() or "chat"
    print(f"{identity.name} is listening on {provider_label}.")
    try:
        if _allowed_conversation_id(chat_provider) is None:
            print(
                f"{provider_label.title()} conversation lock is not set; all conversations "
                "accepted by the provider can reach Enoch."
            )
        else:
            try:
                bot.notify_startup()
            except (OSError, ChatProviderError) as error:
                print(f"Enoch could not send startup notification: {error}")
        bot.run_forever()
    except StaleDaemonEpoch as error:
        print(f"\n{identity.name} stopped because a newer daemon took ownership: {error}")
    except ShutdownRequested as shutdown:
        _notify_shutdown(bot, shutdown.reason)
        print(f"\n{identity.name} is shutting down: {shutdown.reason}.")
    except KeyboardInterrupt:
        _notify_shutdown(bot, "keyboard interrupt")
        print(f"\n{identity.name} stopped listening on {provider_label}.")


def _notify_shutdown(bot: EnochApplication, reason: str) -> None:
    bot.stop_workers()
    sent = _allowed_conversation_id(bot.client) is not None
    try:
        bot.notify_shutdown(reason)
    except (OSError, ChatProviderError) as error:
        sent = False
        print(f"Enoch could not send shutdown notification: {error}")
    _record_lifecycle_shutdown(
        bot.root,
        reason,
        shutdown_notification_sent=sent,
        provider=bot.channel_name,
    )


def _install_shutdown_handlers() -> None:
    def handle_signal(signum: int, _frame: Any) -> None:
        raise ShutdownRequested(_signal_reason(signum))

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, handle_signal)
        except (OSError, ValueError):
            continue


def _signal_reason(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _find_lineage_adopt_candidate(candidate_id: str, root: Path):
    return find_parent_inbox_candidate(candidate_id, root)


def _allowed_conversation_id(client: object) -> ConversationId | None:
    if hasattr(client, "allowed_conversation_id"):
        return getattr(client, "allowed_conversation_id")
    config = getattr(client, "config", None)
    return getattr(config, "allowed_chat_id", None)


def _chat_provider_name(client: object) -> str:
    name = str(getattr(client, "name", "")).strip().lower()
    return name or "chat"


def _event_idempotency_key(purpose: str) -> str:
    event_key = _CURRENT_EVENT_KEY.get().strip()
    normalized_purpose = " ".join(purpose.split())
    if not event_key or not normalized_purpose:
        return ""
    return f"chat:{event_key}:{normalized_purpose}"


def _evidence_source_selection(value: str) -> str:
    source = value.strip().lower() or "all"
    if source not in {*EVIDENCE_SOURCES, "all"}:
        raise ValueError(
            "Evidence source must be feedback, experience, or all."
        )
    return source


def _start_task_deadline(
    root: Path,
    cancellation_event: threading.Event,
    *,
    timeout_seconds: int | None = None,
) -> TaskDeadline:
    deadline = TaskDeadline(
        timeout_seconds=timeout_seconds or task_timeout_seconds(root),
        cancellation_event=cancellation_event,
    )
    deadline.start()
    return deadline


def _task_timeout_message(timeout_seconds: int) -> str:
    return f"Task exceeded the configured {format_task_timeout(timeout_seconds)} timeout."


def _with_replied_text_context(
    text: str,
    reply_text: str,
    *,
    provider_name: str,
) -> str:
    command, argument = _parse_chat_command(text)
    if command not in {"/do", "/task", "/backlog", "/cron"} or not argument:
        return text
    first_word = argument.split(maxsplit=1)[0].lower()
    if command == "/task" and first_word == "cancel":
        return text
    if command == "/backlog" and first_word in {"remove", "priority", "promote"}:
        return text
    if command == "/cron" and first_word == "cancel":
        return text
    if not reply_text:
        return text
    label = provider_label(provider_name)
    return "\n\n".join(
        [
            f"{command} {argument}",
            f"Context from replied {label or 'chat'} message:",
            reply_text,
        ]
    )


def _task_context_snapshot_prompt(request: str, *, provider: str = "chat") -> str:
    return "\n".join(
        [
            "Task context snapshot request:",
            "The human just created this Enoch work request:",
            request.strip(),
            "",
            f"Using only prior conversation context from this same {provider_label(provider)} session, write a concrete task brief for the worker.",
            "Include the intended outcome, relevant decisions, constraints, target files or systems, and anything explicitly ruled out.",
            "Return only the task brief.",
            f"If the request is self-contained and no prior context is needed, return exactly: {NO_EXTRA_TASK_CONTEXT}",
            f"If the prior conversation still does not make the work clear, return only: {NEEDS_CLARIFICATION_PREFIX} <one short question>",
        ]
    )


def _parse_task_context_snapshot(reply: str) -> TaskContextSnapshot:
    memory_result = extract_memory_requests(reply)
    text = memory_result.visible_reply.strip()
    edit_request = extract_edit_request(text)
    if edit_request is not None:
        text = (edit_request.visible_reply or edit_request.request).strip()
    normalized = " ".join(text.split())
    if not normalized:
        return TaskContextSnapshot()
    if normalized.upper().startswith(NEEDS_CLARIFICATION_PREFIX):
        question = normalized[len(NEEDS_CLARIFICATION_PREFIX) :].strip()
        return TaskContextSnapshot(clarification=question or "What should Enoch do?")
    if normalized.rstrip(".").casefold() == NO_EXTRA_TASK_CONTEXT.rstrip(".").casefold():
        return TaskContextSnapshot()
    return TaskContextSnapshot(
        context=_clip_activity_text(normalized, limit=3000),
        source=TASK_CONTEXT_SOURCE_CHAT,
    )


def _replied_message_text(message: dict[str, Any]) -> str:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return ""
    for key in ("text", "caption"):
        value = reply.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _review_identity(reference: str) -> ReviewIdentity:
    value = reference.strip()
    return ReviewIdentity(
        id=value,
        url=value if "://" in value else "",
    )


def _reconciled_retry_result(
    job: TaskJob,
    root: Path,
    *,
    review: ReviewProvider,
) -> str:
    logged_result = _latest_direct_action_result_for_task(job, root)
    prior_result = logged_result or job.result
    identities = []
    if job.review_id:
        identities.append(
            ReviewIdentity(
                id=job.review_id,
                url=job.review_url,
            )
        )
    for url in job.review_urls:
        if not any(candidate.url == url for candidate in identities):
            identities.append(ReviewIdentity(id=url, url=url))
    for identity in identities:
        record = review.inspect_review(identity, root)
        if record.state in {"open", "published", "landed"}:
            return prior_result or (
                f"Reconciled existing review {record.identity.id}: "
                f"{record.identity.url or record.identity.id}"
            )
    if job.revision_id:
        matching = [
            record
            for record in review.list_open_reviews(root)
            if record.versions[-1].revision.id == job.revision_id
        ]
        if matching:
            record = matching[0]
            return (
                f"Reconciled existing review {record.identity.id} for task "
                f"revision {job.revision_id}: "
                f"{record.identity.url or record.identity.id}"
            )
    return ""


def _recover_running_task_from_direct_action_log(
    root: Path,
    *,
    workflow: WorkflowEngine,
) -> TaskJob | None:
    running = workflow.inspect().running
    if running is None or workflow.worker_is_active(running):
        return None
    result = _latest_direct_action_result_for_task(running, root)
    if not result:
        return None
    if _work_reply_failed(result):
        return workflow.finalize(
            running.id,
            "failed",
            result=result,
            event_actor="system",
            trigger="recovery",
        )
    return workflow.finalize(
        running.id,
        "completed",
        result=result,
        event_actor="system",
        trigger="recovery",
    )


def _cleanup_completed_task_worktree(job: TaskJob | None, root: Path) -> None:
    if (
        job is None
        or job.status != "completed"
        or not job.workspace_path
        or not job.workspace_id
        or not Path(job.workspace_path).exists()
    ):
        return
    try:
        remove_task_worktree(
            root,
            TaskWorktree(
                task_id=job.id,
                path=Path(job.workspace_path),
                workspace_id=job.workspace_id,
                created=False,
            ),
            force_delete_branch=True,
        )
    except VcsError:
        return


def _latest_direct_action_result_for_task(job: TaskJob, root: Path) -> str:
    expected_request = _summarize_for_log(job.text)
    latest_result = ""
    try:
        paths = [
            path
            for directory in system_log_dirs(root)
            for path in sorted(directory.glob("*.jsonl"))
        ]
    except OSError:
        return ""
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if str(record.get("time") or "") < job.started_at:
                continue
            if record.get("event") != "direct_action" or record.get("status") not in (None, "ok"):
                continue
            details = record.get("details")
            if not isinstance(details, dict):
                continue
            if details.get("request") != expected_request:
                continue
            result = str(details.get("result") or "").strip()
            if result:
                latest_result = result
    return latest_result
















































def _evolve_task_request(candidate: EvolveCandidate, theme: str) -> str:
    lines = [
        f"Evolve candidate {candidate.id}: {candidate.title}",
        "",
        f"Evidence source: {candidate.evidence_source or candidate.source}",
        f"Signal actor: {candidate.signal_actor}",
        f"Candidate actor: {candidate.candidate_actor}",
        f"Evidence IDs: {', '.join(candidate.evidence_ids) or 'none'}",
        f"Evidence refs: {', '.join(candidate.evidence_refs) or 'none'}",
        f"Theme: {theme or 'not set'}",
        f"Source repository: {candidate.source_repository or 'none'}",
        f"Source revision: {candidate.source_revision or 'none'}",
        f"Source path: {candidate.source_path or 'none'}",
        f"Source version: {candidate.source_version or 'none'}",
        f"Source content hash: {candidate.source_content_hash or 'none'}",
        f"Source URL: {candidate.source_url or 'none'}",
        f"Source theme: {candidate.source_theme or 'none'}",
        f"Source context hash: {candidate.source_context_hash or 'none'}",
        f"Source created at: {candidate.source_created_at or 'none'}",
        f"Proposed change: {candidate.proposed_change}",
        f"Expected benefit: {candidate.expected_benefit}",
        f"Risk: {candidate.risk}",
        f"Test plan: {candidate.test_plan}",
        "",
        "Keep the change small, reversible, and covered by focused tests. "
        "When implementation is complete and tests pass, open a ready-for-review PR for human review; do not merge it.",
        "The worker task context contains an Evolution provenance section. "
        "Include that section verbatim in the pull request body.",
    ]
    return "\n".join(lines)


def _evolve_task_context(candidate: EvolveCandidate) -> str:
    return "\n".join(
        [
            "Evolve candidate context:",
            f"ID: {candidate.id}",
            f"Evidence source: {candidate.evidence_source or candidate.source}",
            f"Signal actor: {candidate.signal_actor}",
            f"Candidate actor: {candidate.candidate_actor}",
            f"Evidence IDs: {', '.join(candidate.evidence_ids) or 'none'}",
            f"Evidence refs: {', '.join(candidate.evidence_refs) or 'none'}",
            f"Parent candidate: {candidate.parent_candidate_id or 'none'}",
            f"Source task: {f'#{candidate.source_task_id}' if candidate.source_task_id is not None else 'none'}",
            f"Source repository: {candidate.source_repository or 'none'}",
            f"Source revision: {candidate.source_revision or 'none'}",
            f"Source path: {candidate.source_path or 'none'}",
            f"Source version: {candidate.source_version or 'none'}",
            f"Source content hash: {candidate.source_content_hash or 'none'}",
            f"Source URL: {candidate.source_url or 'none'}",
            f"Source theme: {candidate.source_theme or 'none'}",
            f"Source context hash: {candidate.source_context_hash or 'none'}",
            f"Source created at: {candidate.source_created_at or 'none'}",
            f"Score: {candidate.score}",
            f"Rationale: {candidate.rationale}",
            f"Proposed change: {candidate.proposed_change}",
            f"Expected benefit: {candidate.expected_benefit}",
            f"Risk: {candidate.risk}",
            f"Test plan: {candidate.test_plan}",
        ]
    )


def _task_worker_context(job: TaskJob) -> str:
    parts = [job.context.strip()]
    provenance = _evolution_provenance_for_job(job)
    if provenance is not None:
        parts.extend(
            [
                "Required pull request metadata:",
                format_evolution_provenance(provenance),
                "If this task opens or updates a pull request, include the Evolution provenance section verbatim in its body.",
            ]
        )
    return "\n\n".join(part for part in parts if part)


def _format_task_worktrees(
    states: tuple[TaskWorktreeState, ...],
    status: TaskQueueStatus,
) -> str:
    if not states:
        return "Task worktrees: none"
    lines = [f"Task worktrees ({len(states)}):"]
    for state in states:
        condition = "unknown" if state.inspection_error else ("clean" if state.clean else "dirty")
        branch = state.branch or "(unknown branch)"
        linked = _tasks_for_worktree(state, status)
        tasks = ", ".join(f"#{job.id} [{job.status}]" for job in linked) or "no recent task record"
        lines.extend(
            [
                f"- task path #{state.task_id} [{condition}] {branch}",
                f"  Tasks: {tasks}",
                f"  Path: {state.path}",
            ]
        )
    return "\n".join(lines)


def _format_task_worktree(
    state: TaskWorktreeState,
    status: TaskQueueStatus,
) -> str:
    condition = "unknown" if state.inspection_error else ("clean" if state.clean else "dirty")
    linked = _tasks_for_worktree(state, status)
    tasks = ", ".join(f"#{job.id} [{job.status}]" for job in linked) or "none in recent queue history"
    lines = [
        f"Task worktree #{state.task_id}",
        f"Status: {condition}",
        f"Branch: {state.branch or '(unknown)'}",
        f"Path: {state.path}",
        f"Linked tasks: {tasks}",
    ]
    if state.changed_files:
        lines.extend(["Changed files:", *(f"- {path}" for path in state.changed_files)])
    if state.inspection_error:
        lines.append(f"Inspection error: {state.inspection_error}")
    return "\n".join(lines)


def _tasks_for_worktree(
    state: TaskWorktreeState,
    status: TaskQueueStatus,
) -> tuple[TaskJob, ...]:
    jobs = [*status.pending, *status.paused, *status.history]
    if status.running is not None:
        jobs.append(status.running)
    state_path = state.path.resolve()
    linked = []
    for job in jobs:
        same_path = False
        if job.workspace_path:
            try:
                same_path = Path(job.workspace_path).expanduser().resolve() == state_path
            except OSError:
                same_path = False
        if same_path or (state.branch and job.workspace_id == state.branch):
            linked.append(job)
    return tuple(sorted(linked, key=lambda job: job.id))


def _active_tasks_for_worktree(
    state: TaskWorktreeState,
    status: TaskQueueStatus,
) -> tuple[TaskJob, ...]:
    return tuple(
        job
        for job in _tasks_for_worktree(state, status)
        if job.status in {"pending", "running", "paused", "retrying"}
    )


def _positive_task_id(value: str) -> int | None:
    try:
        task_id = int(value.lstrip("#"))
    except ValueError:
        return None
    return task_id if task_id > 0 else None


def _codex_pause_warning(task_id: int, reason: str) -> str:
    return "\n".join(
        [
            f"Task #{task_id} was paused because agent runtime access is unavailable.",
            reason.strip() or "Agent runtime access is unavailable.",
            f"When agent runtime access is available again, use /task resume {task_id}.",
        ]
    )


def _load_task_status_messages(workflow: WorkflowEngine) -> dict[int, MessageId]:
    status = workflow.inspect()
    jobs = [*status.pending]
    if status.running is not None:
        jobs.append(status.running)
    return {job.id: job.status_message_id for job in jobs if job.status_message_id is not None}


def _sync_session_activity(
    identity: Identity,
    root: Path,
    chat_id: ConversationId,
    note: str,
    *,
    runtime: AgentRuntime | None = None,
    session_key: str = "",
    effect_fence: DaemonEffectFence | None = None,
) -> None:
    runtime = runtime or FunctionAgentRuntime(
        respond_fn=respond,
        act_in_session_fn=act_in_session,
        model_summary_fn=model_summary,
        model_options_fn=codex_model_options,
        reset_usage_fn=reset_token_usage,
    )
    try:
        execution = RuntimeExecutionControl(
            request_id=f"session-sync:{chat_id}",
            session_key=session_key or f"chat:{chat_id}",
        )
        invoke = lambda control: invoke_runtime_respond(
            runtime,
            identity,
            note,
            cwd=root,
            execution=control,
        )
        if effect_fence is None:
            invoke(execution)
        else:
            effect_fence.run_runtime_authorized(
                "runtime.session-sync",
                ("runtime.respond",),
                invoke,
                execution,
            )
    except (AgentRuntimeError, CapabilityAuthorizationError, TypeError):
        return


def _record_direct_action(message: str, result: str, root: Path) -> None:
    try:
        log_system_event(
            "direct_action",
            root=root,
            details={
                "request": _summarize_for_log(message),
                "result": _summarize_for_log(result),
            },
        )
        ensure_long_term_memory(root)
    except OSError:
        return


def _record_system_event(
    event: str,
    root: Path,
    *,
    status: str = "ok",
    details: dict[str, Any] | None = None,
) -> None:
    try:
        log_system_event(event, root=root, status=status, details=details)
    except OSError:
        return


def _summarize_for_log(text: str, limit: int = 2000) -> str:
    return summarize_for_log(text, limit)


def _task_status_notification_key(status: WorkStatusMessage) -> str:
    rendered = json.dumps(
        {
            "status": status.status,
            "latest_update": status.latest_update,
            "reviews": status.reviews,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:20]
    return f"task:{status.task_id}:status-edit:{digest}"


def _format_doctor_result(result: ImmuneResult) -> str:
    return format_doctor_result(result)


def _format_publish_result(result: LocalPublishResult) -> str:
    return format_publish_result(result)


def _format_remote_publish_result(result: RemotePublishResult) -> str:
    return format_remote_publish_result(result)


def _format_pr_result(result: PullRequestResult) -> str:
    return format_pr_result(result)


def _pr_step_update(result: PullRequestResult) -> str:
    return pr_step_update(result)


def _publish_summary(result: LocalPublishResult) -> str:
    return publish_summary(result)


def _remote_publish_summary(result: RemotePublishResult) -> str:
    return remote_publish_summary(result)


def _pr_summary(result: PullRequestResult) -> str:
    return pr_summary(result)

if __name__ == "__main__":
    main()
