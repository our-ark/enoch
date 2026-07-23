from __future__ import annotations

from contextvars import ContextVar
from dataclasses import replace
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
    backlog_status,
    next_backlog_item,
    promote_backlog_item,
    remove_backlog_item,
    reprioritize_backlog_item,
)
from enoch.automatic_learning import record_learning_artifact
from enoch.brain import (
    act_in_session,
    codex_model_options,
    model_summary,
    reset_token_usage,
    respond,
)
from enoch.evolution.sources.brainstorming import generate_brainstorm_ideas
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
from enoch.config import read_section
from enoch.cron import (
    CronJob,
    add_cron_job,
    cancel_cron_job,
    claim_due_cron_jobs,
    cron_status,
    format_cron_interval,
    parse_cron_interval,
    record_cron_task,
)
from enoch.evolution.core import (
    MODE_AUTO_EVOLVE,
    MODE_CO_EVOLVE,
    MODE_DISABLED,
    EvolveCandidate,
    EvolveProposal,
    acknowledge_evolve_schedule,
    cancel_evolve_candidate_for_task,
    claim_due_evolve_schedule,
    collect_experience_candidates,
    complete_evolve_candidate_for_task,
    disable_evolve_schedule,
    evolve_report,
    fail_evolve_candidate_for_task,
    get_evolve_candidate,
    latest_failed_evolve_task,
    load_evolve_candidates,
    load_evolve_state,
    pause_evolve_candidate_for_task,
    propose_evolve,
    regress_evolve_candidate_for_task,
    remove_evolve_candidate,
    retry_evolve_candidate,
    rank_evolve_candidates,
    resolve_evolve_candidate_regression_for_task,
    resume_evolve_candidate_for_task,
    run_evolve_candidate,
    set_evolve_cron_schedule,
    set_evolve_daily_schedule,
    set_evolve_schedule,
    set_evolve_mode,
    set_evolve_theme,
)
from enoch.evolution.events import (
    EvolveEvent,
    close_open_proposals,
    latest_open_proposal_id,
    record_evolve_event,
)
from enoch.evolution.lifecycle import (
    EvolveLifecycleError,
    finalize_promoted_evolve_adoptions,
    format_reconcile_result,
    reconcile_evolve_candidate,
)
from enoch.vcs_tools import (
    VcsError,
    changed_files,
    delete_branch,
    current_branch,
    diff_summary,
    ensure_clean_worktree,
    switch_branch,
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
    EvolutionProvenance,
    LocalPublishResult,
    PullRequestCloseResult,
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
    learn_command,
    learn_skill_prompt,
    parse_learn_request,
    record_peer_learning_observation,
)
from enoch.lineage.core import (
    find_parent_inbox_candidate,
    LineageError,
    lineage_adopt_prompt,
    lineage_candidate_context,
    load_parent_inbox_candidates,
    mark_inbox_candidate,
    resolve_lineage,
)
from enoch.logs import log_conversation_turn, log_system_event, system_log_dir
from enoch.memory.prompt import memory_for_prompt
from enoch.memory.store import ensure_long_term_memory, remember_memory
from enoch.prompt_append import (
    TaskRegressionSignal,
    extract_edit_request,
    extract_memory_requests,
    extract_task_regression_signals,
    read_only_turn_prompt,
    repository_handoff_note,
    startup_context_note,
    work_request_prompt,
)
from enoch.providers.contracts import (
    AgentRuntime,
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    AgentRuntimeTimedOut,
    Attachment,
    ChatEvent,
    ChatProvider,
    ChatProviderError,
    ConversationId,
    Cursor,
    ForgeProvider,
    ForgeProviderError,
    MessageId,
    RuntimeResult,
    RuntimeExecutionControl,
    normalize_message_id,
)
from enoch.providers.forge import FunctionForgeProvider
from enoch.providers.registry import ProviderError, load_provider
from enoch.providers.runtime import (
    FunctionAgentRuntime,
    invoke_runtime_action,
    invoke_runtime_respond,
)
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
from enoch.runtime import (
    ACTION_SANDBOX_FULL_ACCESS,
    DEFAULT_BRANCH,
    WORKSPACE_WRITE_SANDBOX,
)
from enoch.tasks.queue import (
    TaskJob,
    TaskAlreadyExists,
    TaskRetryError,
    begin_direct_task,
    begin_next_task,
    cancel_task,
    cancel_running_task,
    claim_running_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    fail_task,
    pause_task,
    regress_task,
    recover_interrupted_task,
    record_task_result,
    record_task_publish_state,
    record_task_runtime_result,
    record_task_status_message,
    record_task_worktree,
    resolve_regressed_task,
    retry_failed_task,
    retry_running_task,
    resume_paused_tasks,
    task_result_has_pull_request,
    task_queue_status,
    task_worker_is_active,
)
from enoch.tasks.events import TASK_SOURCES
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
    prepare_existing_branch_worktree,
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
    existing_branch_publish_request as _existing_branch_publish_request,
    forge_maintenance_request as _forge_maintenance_request,
    parse_chat_command as _parse_chat_command,
    task_cancel_id as _task_cancel_id,
    task_resume_target as _task_resume_target,
    task_retry_id as _task_retry_id,
    unquote_schedule_text as _unquote_schedule_text,
)
from enoch.app.presentation import (
    backlog_usage as _backlog_usage,
    clip_activity_block as _clip_activity_block,
    clip_activity_text as _clip_activity_text,
    cron_usage as _cron_usage,
    evolve_usage as _evolve_usage,
    final_task_status_update as _final_task_status_update,
    format_elapsed as _format_elapsed,
    format_open_pull_requests as _format_open_pull_requests,
    format_pull_request as _format_pull_request,
    format_pull_request_merge_result as _format_pull_request_merge_result,
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
    _format_evolve_proposal,
    _format_evolve_report,
    _format_evolve_theme,
    _format_experience_report,
    _format_feedback_report,
    _format_tasks_report,
    _task_status_message,
)


TASK_CONTEXT_SOURCE_CHAT = "chat-snapshot"
NEEDS_CLARIFICATION_PREFIX = "NEEDS_CLARIFICATION:"
NO_EXTRA_TASK_CONTEXT = "No extra context needed."
_CURRENT_WORK_STATUS: ContextVar[WorkStatusMessage | None] = ContextVar("enoch_work_status", default=None)
_CURRENT_TASK_ID: ContextVar[int | None] = ContextVar("enoch_task_id", default=None)
_CURRENT_TASK_WORKER_ID: ContextVar[str] = ContextVar("enoch_task_worker_id", default="")
_CURRENT_EVENT_KEY: ContextVar[str] = ContextVar("enoch_event_key", default="")
_CURRENT_REGRESSION_SIGNALS: ContextVar[tuple[TaskRegressionSignal, ...]] = ContextVar(
    "enoch_regression_signals",
    default=(),
)
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
        profile: AgentProfile | None = None,
    ) -> None:
        self.identity = identity
        self.root = root
        self.client = client
        self.channel_name = _chat_provider_name(client)
        self._forge_injected = forge is not None
        self.runtime = runtime or FunctionAgentRuntime(
            respond_fn=lambda *args, **kwargs: respond(*args, **kwargs),
            act_in_session_fn=lambda *args, **kwargs: act_in_session(*args, **kwargs),
            model_summary_fn=lambda root=None: model_summary(root),
            model_options_fn=lambda: codex_model_options(),
            reset_usage_fn=lambda: reset_token_usage(),
        )
        self.forge = forge or FunctionForgeProvider(
            close_fn=lambda *args, **kwargs: close_pull_request(*args, **kwargs),
            create_fn=lambda **kwargs: create_pull_request(**kwargs),
            inspect_fn=lambda *args, **kwargs: inspect_pull_request(*args, **kwargs),
            inspect_merge_fn=lambda *args, **kwargs: inspect_pull_request_merge(*args, **kwargs),
            list_fn=lambda *args, **kwargs: list_open_pull_requests(*args, **kwargs),
            merge_fn=lambda *args, **kwargs: merge_pull_request(*args, **kwargs),
        )
        self.profile = profile or AgentProfile(name="enoch")
        self._validate_profile_commands()
        self.previous_shutdown_warning = previous_shutdown_warning
        self.offset: Cursor | None = _load_provider_cursor(self.channel_name, root)
        self._restart_after_reply = False
        self._pending_session_syncs: list[tuple[int, str]] = []
        self._task_worker: threading.Thread | None = None
        self._direct_workers: dict[int, threading.Thread] = {}
        self._task_cancellations: dict[int, threading.Event] = {}
        self._stopping = False
        self._resident_branch = instance_branch(root)
        recovered = _recover_running_task_from_direct_action_log(root)
        if recovered is None:
            recovered = recover_interrupted_task(root)
        _cleanup_completed_task_worktree(recovered, root)
        self._work_status_messages: dict[int, MessageId] = _load_task_status_messages(root)
        self._run_profile_hook("on_initialize")

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except ShutdownRequested:
                raise
            except Exception as error:
                print(f"Enoch {provider_label(self.channel_name)} polling error: {error}")
                time.sleep(5)

    def notify_startup(self) -> None:
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return
        self.client.send_message(
            chat_id,
            _startup_message(
                self.identity,
                self.root,
                self.previous_shutdown_warning,
                provider=self.channel_name,
            ),
        )
        _sync_session_activity(
            self.identity,
            self.root,
            chat_id,
            startup_context_note(memory_for_prompt(self.root)),
            runtime=self.runtime,
            session_key=self._session_key(chat_id),
        )
        self._run_profile_hook("on_startup")

    def notify_shutdown(self, reason: str) -> None:
        self._run_profile_hook("on_shutdown")
        _record_system_event("shutdown", self.root, details={"reason": reason})
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return
        self.client.send_message(
            chat_id,
            _shutdown_message(self.identity, self.root, reason, provider=self.channel_name),
        )

    def run_once(self) -> None:
        self._run_profile_hook("before_run")
        try:
            recovered = _recover_running_task_from_direct_action_log(self.root)
            if recovered is None:
                recovered = recover_interrupted_task(self.root)
            _cleanup_completed_task_worktree(recovered, self.root)
            for event in self.client.receive(self.offset):
                self.handle_event(event)
            self._enqueue_due_cron_jobs()
            self._run_due_evolve_schedule()
            self._maybe_start_task_worker()
        finally:
            self._run_profile_hook("after_run")

    def handle_event(self, event: ChatEvent) -> None:
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
        registered_command = core_command(command) if command else None
        if profile_command is not None:
            reply = self._run_profile_command(profile_command, event, command, argument)
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
            send_error = (
                self._safe_send_message(event.conversation_id, receipt.reply)
                if receipt.reply
                else ""
            )
            if send_error:
                _record_system_event(
                    "chat_reply_failed",
                    self.root,
                    status="failed",
                    details={
                        "provider": self.channel_name,
                        "chat_id": event.conversation_id,
                        "message_id": event.message_id,
                        "error": send_error,
                    },
                )
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

    def _help(self, topic: str) -> str:
        profile_command = self.profile.command(topic) if topic.strip() else None
        if profile_command is not None:
            return profile_command.usage or (
                f"{profile_command.command} - {profile_command.summary}"
            )
        core_help = _help_message(topic, chat_provider=self.channel_name)
        if topic.strip() or not self.profile.commands:
            return core_help
        profile_help = [
            f"{self.profile.help_heading}:",
            *(
                f"{spec.command} - {spec.summary}"
                for spec in self.profile.commands
            ),
        ]
        return "\n\n".join([core_help, "\n".join(profile_help)])

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
            "queue": lambda: _format_tasks_report(self.root),
            "stop": self._stop_running_job,
            "backlog": lambda: self._backlog(chat_id, work_text),
            "cron": lambda: self._cron(chat_id, work_text),
            "feedback": lambda: _format_feedback_report(self.root),
            "experience": lambda: _format_experience_report(self.root),
            "propose": lambda: _format_evolve_proposal(
                self._propose_evolve(chat_id, trigger="propose-fallback")
            ),
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

        def queue(request: str, context: str) -> TaskJob:
            nonlocal enqueue_index
            enqueue_index += 1
            return enqueue_task(
                event.conversation_id,
                request,
                self.root,
                context=context,
                context_source=f"profile:{self.profile.name}" if context else "",
                source="task",
                initiated_by="human",
                event_actor="human",
                trigger=command,
                idempotency_key=_event_idempotency_key(
                    f"profile:{self.profile.name}:{command}:{enqueue_index}"
                ),
                **self._profile_task_options(),
            )

        context = CommandContext(
            identity=self.identity,
            root=self.root,
            conversation_id=event.conversation_id,
            event=event,
            command=command,
            argument=argument,
            runtime=self.runtime,
            forge=self.forge,
            _enqueue=queue,
        )
        try:
            return str(spec.handler(context))
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

    def _profile_task_options(self) -> dict[str, int]:
        return self.profile.workflow.task_options()

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
                    chat=self.client,
                    runtime=self.runtime,
                    forge=self.forge,
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

    def _session_key(self, chat_id: ConversationId) -> str:
        provider = _chat_provider_name(self.client)
        return f"{provider}:{chat_id}"

    def _respond_read_only_turn(
        self,
        chat_id: ConversationId,
        text: str,
        *,
        session_key: str | None = None,
    ) -> str:
        resolved_session_key = session_key or self._session_key(chat_id)
        try:
            return invoke_runtime_respond(
                self.runtime,
                self.identity,
                self._profile_prompt(
                    read_only_turn_prompt(text),
                    purpose="conversation",
                    chat_id=chat_id,
                ),
                cwd=self.root,
                execution=RuntimeExecutionControl(
                    request_id=f"conversation:{chat_id}",
                    session_key=resolved_session_key,
                ),
            ).final_text
        except (AgentRuntimeError, TypeError) as error:
            return str(error)

    def _respond_to_image(
        self,
        chat_id: ConversationId,
        image: Attachment,
        caption: str,
    ) -> str:
        try:
            with temporary_image_attachment(
                self.client,
                image,
                self.root,
                channel_name=self.channel_name,
            ) as image_path:
                return invoke_runtime_respond(
                    self.runtime,
                    self.identity,
                    self._profile_prompt(
                        image_prompt(caption, self.channel_name),
                        purpose="image",
                        chat_id=chat_id,
                    ),
                    cwd=self.root,
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
        queue_status = task_queue_status(self.root)
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
            reply = invoke_runtime_respond(
                self.runtime,
                self.identity,
                self._profile_prompt(
                    _task_context_snapshot_prompt(request, provider=self.channel_name),
                    purpose="task-context",
                    chat_id=chat_id,
                ),
                cwd=self.root,
                execution=RuntimeExecutionControl(
                    request_id=f"task-context:{chat_id}",
                    session_key=self._session_key(chat_id),
                ),
            ).final_text
        except AgentRuntimeAccessUnavailable as error:
            return TaskContextSnapshot(codex_unavailable_reason=str(error))
        except (AgentRuntimeError, TypeError) as error:
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
            direct_task = begin_direct_task(
                chat_id,
                request,
                self.root,
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
            running = task_queue_status(self.root).running
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
        )
        if message_id is not None:
            self._work_status_messages[direct_task.id] = message_id
            record_task_status_message(direct_task.id, message_id, self.root)
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
        if task_queue_status(self.root).paused_count:
            return (
                "Enoch has paused tasks. Restore agent runtime access and use "
                "/task resume <id|all> before starting more work."
            )
        try:
            job = begin_direct_task(
                chat_id,
                request,
                self.root,
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
        claimed = claim_running_task(job.id, worker_id, os.getpid(), self.root)
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
                finished_job = cancel_running_task(
                    self.root,
                    result=reply,
                    event_actor="agent",
                    trigger=trigger,
                    expected_task_id=job.id,
                    worker_id=worker_id,
                )
            elif completed_status == "failed":
                failure = failure or classify_task_failure(reply)
                finished_job = fail_task(
                    job.id,
                    self.root,
                    result=reply,
                    event_actor="system" if deadline.expired.is_set() else "agent",
                    trigger="task-timeout" if deadline.expired.is_set() else trigger,
                    worker_id=worker_id,
                    failure_code=failure.code,
                    failure_class=failure.failure_class,
                    retryable=False,
                )
            elif completed_status == "paused":
                finished_job = pause_task(
                    job.id,
                    self.root,
                    result=reply,
                    event_actor="system",
                    trigger="codex-unavailable",
                    worker_id=worker_id,
                )
                if finished_job is not None:
                    pause_evolve_candidate_for_task(
                        finished_job,
                        self.root,
                        event_actor="system",
                        trigger="codex-unavailable",
                        reason=reply,
                    )
            else:
                finished_job = complete_task(
                    job.id,
                    self.root,
                    result=reply,
                    event_actor="agent",
                    trigger=trigger,
                    worker_id=worker_id,
                )
        authoritative_job = finished_job or _task_by_id(job.id, self.root)
        if authoritative_job is None or authoritative_job.status != completed_status:
            return authoritative_job.result if authoritative_job is not None else reply
        self._apply_task_regression_signals(
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
            job = enqueue_task_front(
                chat_id,
                request,
                self.root,
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
        message_id = self._safe_send_message_id(chat_id, message)
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            record_task_status_message(job.id, message_id, self.root)
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

    def stop_workers(self, timeout_seconds: float = 7.0) -> None:
        self._stopping = True
        for cancellation in tuple(self._task_cancellations.values()):
            cancellation.set()
        current = threading.current_thread()
        workers = [*self._direct_workers.values()]
        if self._task_worker is not None:
            workers.append(self._task_worker)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
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
        self._raise_if_current_task_cancelled()
        forge_maintenance = _forge_maintenance_request(request)
        if forge_maintenance is not None:
            reply = self._run_forge_maintenance(forge_maintenance)
            self._raise_if_current_task_cancelled()
            return WorkOutcome.completed(reply)

        publish_branch = _existing_branch_publish_request(request)
        if publish_branch is not None:
            reply = self._publish_existing_branch(chat_id, publish_branch)
            self._raise_if_current_task_cancelled()
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
            sandbox = _action_sandbox(self.root)
            self._send_step_update(chat_id, "Preparing an isolated task worktree.")
            task_worktree = self._prepare_task_worktree(request)
            work_root = task_worktree.path
            branch_note = (
                f"Enoch prepared isolated worktree {work_root} on branch "
                f"{task_worktree.branch} from the latest task base."
            )
            self._send_step_update(chat_id, "Working.")
            runtime_result = invoke_runtime_action(
                self.runtime,
                self.identity,
                self._profile_prompt(
                    work_request_prompt(
                        _work_request_with_context(request, context),
                        remote_review=bool(
                            getattr(self.forge, "supports_remote_review", True)
                        ),
                    ),
                    purpose="task",
                    chat_id=chat_id,
                ),
                cwd=work_root,
                sandbox=sandbox,
                execution=execution
                or RuntimeExecutionControl(
                    request_id=f"task:{_CURRENT_TASK_ID.get() or 'inline'}",
                    session_key=session_key,
                    cancellation_event=self._current_task_cancellation_event(),
                    progress_callback=lambda progress: self._send_progress(
                        chat_id,
                        progress.elapsed_seconds,
                        progress.sandbox,
                    ),
                ),
                state_root=self.root,
            )
            _record_current_task_runtime_result(
                runtime_result,
                provider=self.runtime.name,
                root=self.root,
            )
            result = runtime_result.final_text
            self._raise_if_current_task_cancelled()
            result = self._capture_task_regression_signals(result)
            memory_result = extract_memory_requests(result)
            result = memory_result.visible_reply
            memory_note = self._save_memory_requests(memory_result.requests)
            _record_direct_action(request, result, self.root)
            action_files = tuple(sorted(_changed_files_or_empty(work_root)))
        except AgentRuntimeCancelled:
            raise
        except AgentRuntimeTimedOut:
            raise
        except AgentRuntimeAccessUnavailable:
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
                cleanup = remove_task_worktree(
                    self.root,
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

        self._send_step_update(chat_id, "Running doctor.")
        self._raise_if_current_task_cancelled()
        doctor = run_immune_system(work_root)
        self._raise_if_current_task_cancelled()
        parts.append(_format_doctor_result(doctor))
        self._send_step_update(chat_id, "Doctor passed." if doctor.passed else "Doctor failed.")
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

        self._raise_if_current_task_cancelled()
        self._record_current_publish_stage("validated")
        publish_outcome = _coerce_work_outcome(
            self._publish_feature_pr(
                chat_id,
                request,
                action_files,
                work_root=work_root,
                task_worktree=task_worktree,
                validation_result=doctor,
            )
        )
        self._raise_if_current_task_cancelled()
        return replace(
            publish_outcome,
            message="\n\n".join(
                part for part in [*parts, publish_outcome.message] if part
            ),
            completed_stages=tuple(
                dict.fromkeys(("edited", "validated", *publish_outcome.completed_stages))
            ),
        )

    def _prepare_task_worktree(self, request: str) -> TaskWorktree:
        task_id = _CURRENT_TASK_ID.get()
        worker_id = _CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            raise VcsError("Task worktree preparation requires an owned running task.")
        job = _task_by_id(task_id, self.root)
        if job is None or job.status != "running" or job.worker_id != worker_id:
            raise VcsError(f"Task #{task_id} no longer owns its execution lease.")
        base = _task_branch_base(self.root)
        worktree = prepare_task_worktree(
            self.root,
            task_id,
            request,
            start_point=base,
            resident_branch=self._resident_branch_name(),
            created_at=job.created_at,
            existing_path=job.worktree_path,
            existing_branch=job.branch_name,
        )
        recorded = record_task_worktree(
            task_id,
            worker_id,
            worktree.path,
            worktree.branch,
            self.root,
        )
        if recorded is None:
            raise VcsError(f"Task #{task_id} lost its execution lease while preparing its worktree.")
        return worktree

    def _run_forge_maintenance(self, request: "ForgeMaintenanceRequest") -> str:
        self._update_work_status("Updating pull requests.")
        results = [
            self.forge.close_pull_request(
                number,
                root=self.root,
                comment=_duplicate_close_comment(request.keep_number) if request.keep_number else None,
            )
            for number in request.close_numbers
        ]
        return _format_pr_close_results(results, request.keep_number)

    def _run_existing_branch_publish_with_status(self, chat_id: int, text: str, branch: str) -> str:
        status_message = _CURRENT_WORK_STATUS.get()
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
            message_id = self._safe_send_message_id(
                chat_id,
                self._format_work_status(status_message),
            )
            if message_id is not None:
                status_message.message_id = message_id
                token = _CURRENT_WORK_STATUS.set(status_message)
        try:
            result = self._publish_existing_branch(chat_id, branch)
            if status_message is not None and status_message.message_id:
                self._update_work_status(_clip_activity_text(result, limit=800), status="completed")
                return ""
            return result
        finally:
            if token is not None:
                _CURRENT_WORK_STATUS.reset(token)

    def _publish_existing_branch(self, chat_id: int, branch: str) -> str:
        resident_branch = self._resident_branch_name()
        outputs: list[str] = []
        try:
            self._send_step_update(chat_id, f"Preparing an isolated worktree for {branch}.")
            task_worktree = self._prepare_existing_branch_task_worktree(branch)
            work_root = task_worktree.path
            ensure_clean_worktree(work_root)

            self._send_step_update(chat_id, f"Handing off branch {branch}.")
            pushed = push_current_branch(root=work_root)
            outputs.append(_format_remote_publish_result(pushed))
            self._send_step_update(
                chat_id,
                (
                    f"Pushed branch {pushed.branch}."
                    if pushed.pushed
                    else f"Kept branch {pushed.branch} locally."
                ),
            )

            self._send_step_update(chat_id, "Preparing the review handoff.")
            pr = _create_pull_request_for_current_task(
                work_root,
                self.root,
                forge=self.forge,
            )
            outputs.append(_format_pr_result(pr))
            if pr.url:
                self._update_work_status(_pr_step_update(pr), pr_url=pr.url)
                _record_current_task_result("\n\n".join(outputs), self.root)
            self._send_step_update(chat_id, _pr_step_update(pr))

            self._send_step_update(chat_id, "Cleaning up the isolated task worktree.")
            outputs.append(
                remove_task_worktree(
                    self.root,
                    task_worktree,
                    delete_local_branch=False,
                )
            )
            self._send_step_update(chat_id, f"Resident checkout remains on {resident_branch}.")
            if pr.url:
                self._queue_session_sync(
                    chat_id,
                    repository_handoff_note(
                        pr.branch,
                        pr.url,
                        resident_branch,
                        self._authoritative_branch_name(),
                    ),
                )
        except (VcsError, ForgeProviderError) as error:
            failure = f"Enoch could not publish existing branch {branch}: {error}"
            self._send_step_update(chat_id, failure)
            return "\n\n".join([*outputs, failure]) if outputs else failure
        return "\n\n".join(outputs)

    def _prepare_existing_branch_task_worktree(self, branch: str) -> TaskWorktree:
        task_id = _CURRENT_TASK_ID.get()
        worker_id = _CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            raise VcsError("Branch publishing requires an owned running task.")
        job = _task_by_id(task_id, self.root)
        if job is None or job.status != "running" or job.worker_id != worker_id:
            raise VcsError(f"Task #{task_id} no longer owns its execution lease.")
        worktree = prepare_existing_branch_worktree(
            self.root,
            task_id,
            branch,
            existing_path=job.worktree_path,
        )
        recorded = record_task_worktree(
            task_id,
            worker_id,
            worktree.path,
            worktree.branch,
            self.root,
        )
        if recorded is None:
            raise VcsError(f"Task #{task_id} lost its execution lease while preparing its worktree.")
        return worktree

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
        publish_root = work_root or self.root
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
            or (
                stage == "pushed"
                and candidate == "committed"
            )
            or (
                stage == "pr_opened"
                and candidate in {"committed", "pushed"}
            )
        ]
        try:
            if stage not in {"committed", "pushed", "pr_opened"}:
                self._send_step_update(chat_id, "Committing the change.")
                commit = prepare_local_publish(
                    feature_title(request),
                    root=publish_root,
                    allowed_files=allowed_files,
                    validation_result=validation_result,
                )
                commit_sha = commit.commit_sha
                remote_branch = commit.branch
                outputs.append(_format_publish_result(commit))
                summaries.append(_publish_summary(commit))
                completed_stages.append("committed")
                self._record_current_publish_stage(
                    "committed",
                    commit_sha=commit_sha,
                    remote_branch=remote_branch,
                )
                self._send_step_update(chat_id, f"Committed {commit.commit_sha}.")
                stage = "committed"
            else:
                outputs.append(
                    f"Resuming publish workflow after commit {commit_sha or 'unknown'}."
                )

            if stage not in {"pushed", "pr_opened"}:
                self._send_step_update(
                    chat_id,
                    "Handing off the branch to the configured forge.",
                )
                pushed = push_current_branch(root=publish_root)
                remote_branch = pushed.branch
                pushed_remotely = pushed.pushed
                outputs.append(_format_remote_publish_result(pushed))
                summaries.append(_remote_publish_summary(pushed))
                completed_stages.append("pushed")
                self._record_current_publish_stage(
                    "pushed",
                    commit_sha=commit_sha,
                    remote_branch=remote_branch,
                    published_remotely=pushed_remotely,
                )
                self._send_step_update(
                    chat_id,
                    (
                        f"Pushed branch {pushed.branch}."
                        if pushed.pushed
                        else f"Kept branch {pushed.branch} locally."
                    ),
                )
                stage = "pushed"

            if stage != "pr_opened":
                self._send_step_update(chat_id, "Preparing the review handoff.")
                pr = _create_pull_request_for_current_task(
                    publish_root,
                    self.root,
                    forge=self.forge,
                )
                outputs.append(_format_pr_result(pr))
                summaries.append(_pr_summary(pr))
                self._send_step_update(chat_id, _pr_step_update(pr))
                if bool(getattr(self.forge, "supports_remote_review", True)) and (
                    not pr.created or not pr.url
                ):
                    detail = pr.note or pr.fallback_url or "the forge did not return a PR URL"
                    failure = (
                        "Enoch pushed the task branch but could not open its pull request. "
                        f"The worktree and branch were preserved for retry. {detail}"
                    )
                    self._send_step_update(chat_id, failure)
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
                    self._record_current_publish_stage(
                        "pr_opened",
                        commit_sha=commit_sha,
                        remote_branch=remote_branch,
                        pr_url=pr_url,
                        published_remotely=pushed_remotely,
                    )
                    self._update_work_status(_pr_step_update(pr), pr_url=pr_url)
                    _record_current_task_result("\n\n".join(outputs), self.root)
                    stage = "pr_opened"
            elif pr_url:
                outputs.append(f"Pull request already opened: {pr_url}")

            resident_branch = self._resident_branch_name()
            if task_worktree is not None:
                self._send_step_update(chat_id, "Cleaning up the isolated task worktree.")
                handoff = remove_task_worktree(
                    self.root,
                    task_worktree,
                    delete_local_branch=pushed_remotely,
                    force_delete_branch=pushed_remotely,
                )
            else:
                self._send_step_update(chat_id, f"Returning local checkout to {resident_branch}.")
                handoff = self._return_to_resident_after_handoff(
                    published_remotely=pushed_remotely,
                )
            outputs.append(handoff)
            summaries.append(handoff)
            self._send_step_update(chat_id, f"Resident checkout remains on {resident_branch}.")
            if pr_url:
                self._queue_session_sync(
                    chat_id,
                    repository_handoff_note(
                        remote_branch,
                        pr_url,
                        resident_branch,
                        self._authoritative_branch_name(),
                    ),
                )
        except (VcsError, ForgeProviderError) as error:
            failure = f"Enoch could not publish this edit as a pull request: {error}"
            self._send_step_update(chat_id, failure)
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
        _record_direct_action(action, "\n\n".join(summaries), self.root)
        reply = "\n\n".join(outputs)
        self._queue_session_sync(
            chat_id,
            _activity_sync_note(
                f"Enoch {action}",
                f"Final workflow summary: {_clip_activity_text(summaries[-1]) if summaries else 'none'}",
                f"Result: {_clip_activity_text(reply)}",
            ),
        )
        return WorkOutcome.completed(
            reply,
            completed_stages=tuple(dict.fromkeys(completed_stages)),
            commit_sha=commit_sha,
            remote_branch=remote_branch,
            pr_url=pr_url,
        )

    def _record_current_publish_stage(
        self,
        stage: str,
        *,
        commit_sha: str = "",
        remote_branch: str = "",
        pr_url: str = "",
        published_remotely: bool | None = None,
    ) -> None:
        task_id = _CURRENT_TASK_ID.get()
        worker_id = _CURRENT_TASK_WORKER_ID.get()
        if task_id is None or not worker_id:
            return
        recorded = record_task_publish_state(
            task_id,
            worker_id,
            self.root,
            stage=stage,
            commit_sha=commit_sha,
            remote_branch=remote_branch,
            pr_url=pr_url,
            published_remotely=published_remotely,
        )
        if recorded is None:
            raise VcsError(
                f"Task #{task_id} lost its execution lease while recording publish stage {stage}."
            )

    def _resume_task_publish(self, job: TaskJob) -> WorkOutcome:
        if not job.worktree_path or not job.branch_name:
            return WorkOutcome.failure(
                f"Task #{job.id} cannot resume publishing because its worktree metadata is missing.",
                code="worktree_precondition",
            )
        worktree = TaskWorktree(
            task_id=job.id,
            path=Path(job.worktree_path),
            branch=job.branch_name,
            created=False,
        )
        return self._publish_feature_pr(
            job.chat_id,
            job.text,
            (),
            work_root=worktree.path,
            task_worktree=worktree,
            resume_job=job,
        )

    def _return_to_resident_after_handoff(self, *, published_remotely: bool = True) -> str:
        branch = current_branch(self.root)
        resident_branch = self._resident_branch_name(branch)
        if branch == resident_branch:
            return f"Local checkout is already on {resident_branch}."
        ensure_clean_worktree(self.root)
        switch_branch(resident_branch, self.root)
        cleanup = ""
        if published_remotely:
            cleanup = _delete_local_branch_if_enabled(
                branch,
                self.root,
                protected_branch=resident_branch,
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
            return _authoritative_branch_name(self.root) or DEFAULT_BRANCH
        except VcsError:
            return DEFAULT_BRANCH

    def _send_step_update(self, chat_id: ConversationId | None, message: str) -> None:
        if chat_id is None:
            return
        if self._update_work_status(message):
            return
        self._safe_send_message(chat_id, f"Enoch update: {message}")

    def _safe_send_message(self, chat_id: ConversationId, message: str) -> str:
        try:
            self.client.send_message(chat_id, message)
        except (OSError, ChatProviderError) as error:
            return str(error)
        return ""

    def _safe_send_message_id(
        self,
        chat_id: ConversationId,
        message: str,
    ) -> MessageId | None:
        try:
            return self.client.send_message(chat_id, message)
        except (OSError, ChatProviderError):
            return None

    def _safe_edit_message(
        self,
        chat_id: ConversationId,
        message_id: MessageId,
        message: str,
    ) -> None:
        try:
            self.client.edit_message(chat_id, message_id, message)
        except (OSError, ChatProviderError):
            return

    def _update_work_status(self, latest_update: str, *, status: str | None = None, pr_url: str = "") -> bool:
        task_status = _CURRENT_WORK_STATUS.get()
        if task_status is None:
            return False
        if status:
            task_status.status = status
        task_status.latest_update = latest_update
        if pr_url and pr_url not in task_status.prs:
            task_status.prs.append(pr_url)
        if normalize_message_id(task_status.message_id) is None:
            return True
        self._safe_edit_message(
            task_status.chat_id,
            task_status.message_id,
            self._format_work_status(task_status),
        )
        return True

    def _safe_send_read_ack(self, chat_id: ConversationId, message_id: object) -> None:
        if not isinstance(message_id, (int, str)):
            return
        try:
            self.client.send_read_ack(chat_id, message_id)
        except (OSError, ChatProviderError) as error:
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
            model_summary_fn=self.runtime.model_summary,
        )
        return "\n\n".join([status, _task_status_message(self.root)])

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
            prompt = learn_skill_prompt(text, root=self.root)
        except LearnError as error:
            return f"Enoch could not inspect that skill: {error}"
        try:
            record_peer_learning_observation(request, self.root)
        except (OSError, ValueError):
            pass

        reply = self._respond_read_only_turn(chat_id, prompt)
        memory_result = extract_memory_requests(reply)
        reply = memory_result.visible_reply
        memory_note = self._save_memory_requests(memory_result.requests)
        edit_request = extract_edit_request(reply)
        if edit_request is None:
            return "\n\n".join(part for part in [reply, memory_note] if part)

        visible = edit_request.visible_reply
        if not self._action_allowed():
            return "\n\n".join(part for part in [visible, memory_note, self._action_lock_message()] if part)

        edit_result = self._run_tracked_inline_work(
            chat_id,
            edit_request.request,
            source="learning",
            initiated_by="human",
            trigger="/learn",
            session_key=self._session_key(chat_id),
        )
        return "\n\n".join(part for part in [visible, memory_note, edit_result] if part)

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
            cancelled = cancel_task(cancel_id, self.root)
            if cancelled is None:
                return f"Enoch could not cancel task #{cancel_id}. It may be running, completed, or missing."
            cancel_evolve_candidate_for_task(
                cancelled,
                self.root,
                event_actor="human",
                trigger="/task cancel",
                reason="Cancelled before running.",
            )
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
            job = enqueue_task(
                chat_id,
                argument,
                self.root,
                context=snapshot.context,
                context_source=snapshot.source,
                idempotency_key=_event_idempotency_key("task"),
                **self._profile_task_options(),
            )
        except (OSError, ValueError):
            return "Enoch could not queue that task."
        status = task_queue_status(self.root)
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
        message_id = self._safe_send_message_id(chat_id, message)
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            record_task_status_message(job.id, message_id, self.root)
            return ""
        return f"Queued task #{job.id}. Enoch will work on it in the background when idle."

    def _retry_task(self, task_id: int) -> str:
        original = _task_by_id(task_id, self.root)
        candidate = None
        proposal_id = ""
        if original is not None and original.candidate_id:
            state = evolve_report(self.root).state
            try:
                candidate = get_evolve_candidate(
                    original.candidate_id,
                    self.root,
                    theme=state.theme,
                )
            except ValueError as error:
                return f"Enoch could not retry task #{task_id}: {error}"
            if candidate.status != "failed":
                return (
                    f"Enoch could not retry task #{task_id}: evolve candidate "
                    f"{candidate.id} is in status {candidate.status}, not failed."
                )
            proposal_id = latest_open_proposal_id(candidate.id, self.root)
        try:
            reconciled_result = (
                _reconciled_retry_result(original, self.root, forge=self.forge)
                if original is not None
                else ""
            )
            job = retry_failed_task(
                task_id,
                self.root,
                reconciled_result=reconciled_result,
            )
        except (OSError, ForgeProviderError, TaskRetryError) as error:
            return f"Enoch could not retry task #{task_id}: {error}"
        if candidate is not None:
            self._record_evolve_event(
                "selected",
                event_actor="human",
                trigger="/task retry",
                candidate=candidate,
                approval_actor="human",
                proposal_id=proposal_id,
            )
            try:
                candidate = retry_evolve_candidate(
                    candidate.id,
                    self.root,
                    theme=evolve_report(self.root).state.theme,
                )
            except ValueError as error:
                cancel_task(job.id, self.root)
                return f"Enoch could not retry task #{task_id}: {error}"
            self._record_evolve_event(
                "queued",
                event_actor="human",
                trigger="/task retry",
                candidate=candidate,
                task_id=job.id,
                approval_actor="human",
                retry_of_task_id=task_id,
                reason=f"retry-of-task-{task_id}",
                proposal_id=proposal_id,
            )
        position = task_queue_status(self.root).pending_count
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
                        f"{len(job.pr_urls)} existing PR(s)."
                    )
                    if job.pr_urls
                    else f"Retry of failed task #{task_id} queued at position {position}."
                ),
                context=job.context,
            )
        )
        message_id = self._safe_send_message_id(job.chat_id, message)
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            record_task_status_message(job.id, message_id, self.root)
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
            enqueue = enqueue_task_front if source == "chat-task" else enqueue_task
            job = enqueue(
                chat_id,
                request,
                self.root,
                source=source,
                initiated_by="human",
                event_actor="human",
                trigger=trigger,
                idempotency_key=_event_idempotency_key(f"paused:{trigger}"),
                **self._profile_task_options(),
            )
            paused = pause_task(
                job.id,
                self.root,
                result=_codex_pause_warning(job.id, reason),
                event_actor="system",
                trigger="codex-unavailable",
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
        )
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            record_task_status_message(job.id, message_id, self.root)
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
        resumed = resume_paused_tasks(
            self.root,
            task_id=task_id,
            trigger=trigger,
        )
        if not resumed:
            if task_id is not None:
                return f"Task #{task_id} is not paused."
            return "No tasks are paused for agent runtime access."
        for job in resumed:
            resume_evolve_candidate_for_task(
                job,
                self.root,
                event_actor="human",
                trigger=trigger,
                reason="User resumed after restoring agent runtime access.",
            )
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
            task = _task_by_id(signal.task_id, self.root)
            if task is None:
                continue
            if task.status == "completed":
                task = regress_task(
                    signal.task_id,
                    self.root,
                    result=signal.reason,
                    event_actor="agent",
                    trigger="agent-regression-signal",
                )
                if task is None:
                    continue
                regress_evolve_candidate_for_task(
                    task,
                    self.root,
                    event_actor="agent",
                    trigger="agent-regression-signal",
                    reason=signal.reason,
                )
            elif task.status != "regressed":
                continue
            if not allow_resolution or not signal.resolution:
                continue
            related_task_id = signal.fix_task_id
            if signal.resolution == "forward-fixed" and related_task_id is None:
                related_task_id = current_task_id
            resolved = resolve_regressed_task(
                signal.task_id,
                signal.resolution,
                self.root,
                result=signal.reason,
                event_actor="agent",
                trigger="agent-regression-signal",
                related_task_id=related_task_id,
            )
            if resolved is None:
                continue
            reason = signal.reason
            if signal.resolution == "forward-fixed" and related_task_id is not None:
                reason = f"Forward-fixed by task #{related_task_id}. {reason}"
            resolve_evolve_candidate_regression_for_task(
                resolved,
                signal.resolution,
                self.root,
                event_actor="agent",
                trigger="agent-regression-signal",
                reason=reason,
            )

    def _stop_running_job(self) -> str:
        running = task_queue_status(self.root).running
        if running is None:
            return "No running task to stop."
        cancellation_event = self._task_cancellations.get(running.id)
        if cancellation_event is not None:
            cancellation_event.set()
        result = "Stopped by /stop."
        cancelled = cancel_running_task(self.root, result=result)
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
            return _format_evolve_report(evolve_report(self.root))
        subcommand = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if subcommand in {MODE_DISABLED, MODE_CO_EVOLVE, MODE_AUTO_EVOLVE, "co", "auto", "auto-evovle"}:
            try:
                set_evolve_mode(subcommand, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand == "mode":
            if not rest.strip():
                return "Use /evolve mode <mode> to set self-evolution behavior. Modes: disabled, co-evolve, auto-evolve."
            try:
                set_evolve_mode(rest, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand == "theme":
            if not rest.strip():
                return _format_evolve_theme(load_evolve_state(self.root))
            set_evolve_theme(rest, self.root)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand == "brainstorm":
            state = evolve_report(self.root).state
            if state.mode == MODE_DISABLED:
                return "Enable co-evolve or auto-evolve mode before brainstorming."
            if not state.theme:
                return "Set a theme with /evolve theme <text> before brainstorming."
            try:
                ideas = generate_brainstorm_ideas(
                    state.theme,
                    self.root,
                    mission=self.identity.mission,
                    generator=lambda prompt: self._respond_read_only_turn(chat_id, prompt),
                )
            except (AgentRuntimeError, OSError, ValueError) as error:
                return f"Enoch could not brainstorm evolution candidates: {error}"
            report = evolve_report(self.root)
            return f"Added {len(ideas)} theme-guided brainstorming candidate(s).\n\n" + _format_evolve_report(report)
        if subcommand == "list":
            report = evolve_report(self.root)
            include_inactive = rest.strip().lower() in {"all", "inactive"}
            candidates = (
                load_evolve_candidates(self.root, include_inactive=True, theme=report.state.theme)
                if include_inactive
                else report.candidates
            )
            return _format_evolve_candidates(candidates, include_inactive=include_inactive)
        if subcommand == "remove":
            if not rest.strip():
                return "Use /evolve remove <id> [reason] to remove a self-evolution candidate."
            remove_parts = rest.strip().split(maxsplit=1)
            remove_reason = remove_parts[1] if len(remove_parts) > 1 else "human-requested-removal"
            state = evolve_report(self.root).state
            try:
                candidate = remove_evolve_candidate(
                    remove_parts[0],
                    self.root,
                    theme=state.theme,
                    reason=remove_reason,
                )
            except ValueError as error:
                return str(error)
            return "Removed evolve candidate.\n\n" + "\n".join(_format_evolve_candidate(candidate))
        if subcommand == "approve":
            if not rest.strip():
                return "Use /evolve approve <id> to approve and queue a self-evolution candidate."
            return self._evolve_approve(rest)
        if subcommand == "retry":
            if not rest.strip():
                return "Use /evolve retry <id> to retry a failed self-evolution candidate."
            return self._evolve_retry(rest)
        if subcommand == "reconcile":
            reconcile_parts = rest.split()
            recording_mode = "realtime"
            if reconcile_parts and reconcile_parts[-1].lower() in {
                "backfill",
                "--backfill",
            }:
                recording_mode = "backfill"
                reconcile_parts.pop()
            if len(reconcile_parts) != 1:
                return (
                    "Use /evolve reconcile <id> [backfill] to verify human promotion "
                    "of a completed candidate."
                )
            try:
                reconcile_kwargs: dict[str, object] = {
                    "recording_mode": recording_mode,
                }
                if self._forge_injected:
                    reconcile_kwargs["forge"] = self.forge
                result = reconcile_evolve_candidate(
                    reconcile_parts[0],
                    self.root,
                    **reconcile_kwargs,
                )
            except EvolveLifecycleError as error:
                return f"Enoch could not reconcile evolution promotion: {error}"
            return format_reconcile_result(result)
        if subcommand == "schedule":
            return self._evolve_schedule(rest)
        return _evolve_usage()

    def _propose_evolve(self, chat_id: int, *, trigger: str) -> EvolveProposal:
        proposal = propose_evolve(
            self.root,
            mission=self.identity.mission,
            curator=lambda prompt: self._respond_read_only_turn(
                chat_id,
                prompt,
                session_key=f"{self._session_key(chat_id)}:{trigger}:curation",
            ),
        )
        event_actor = "system" if trigger == "evolve-scheduler" else "human"
        event_trigger = "evolve-scheduler" if event_actor == "system" else "/propose"
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
            job = enqueue_task(
                chat_id,
                _evolve_task_request(candidate, state.theme),
                self.root,
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
        candidate = run_evolve_candidate(candidate.id, self.root, theme=state.theme)
        self._record_evolve_event(
            "queued",
            event_actor="human",
            trigger="/evolve approve",
            candidate=candidate,
            task_id=job.id,
            approval_actor="human",
            proposal_id=proposal_id,
        )
        return f"Approved evolve candidate {candidate.id} and queued task #{job.id}.\n\n" + "\n".join(
            _format_evolve_candidate(candidate)
        )

    def _evolve_retry(self, candidate_id: str) -> str:
        chat_id = _allowed_conversation_id(self.client)
        if chat_id is None:
            return f"Enoch needs a locked {provider_label(self.channel_name)} conversation before retrying evolve work."
        state = evolve_report(self.root).state
        try:
            candidate = get_evolve_candidate(candidate_id, self.root, theme=state.theme)
        except ValueError as error:
            return str(error)
        if candidate.status != "failed":
            return f"Evolve candidate {candidate.id} cannot retry from status {candidate.status}."
        failed_job = latest_failed_evolve_task(candidate.id, self.root)
        if failed_job is None:
            return f"Enoch could not find a failed task to retry for evolve candidate {candidate.id}."
        proposal_id = latest_open_proposal_id(candidate.id, self.root)
        self._record_evolve_event(
            "selected",
            event_actor="human",
            trigger="/evolve retry",
            candidate=candidate,
            approval_actor="human",
            proposal_id=proposal_id,
        )
        try:
            job = enqueue_task(
                chat_id,
                _evolve_task_request(candidate, state.theme),
                self.root,
                context=_evolve_task_context(candidate),
                context_source="evolve-retry",
                source=candidate.source,
                initiated_by="human",
                event_actor="human",
                trigger="/evolve retry",
                candidate_id=candidate.id,
                parent_task_id=failed_job.id,
                evidence_source=candidate.evidence_source or candidate.source,
                signal_actor=candidate.signal_actor,
                candidate_actor=candidate.candidate_actor,
                approval_actor="human",
                parent_candidate_id=candidate.parent_candidate_id,
                source_task_id=candidate.source_task_id,
                idempotency_key=_event_idempotency_key(
                    f"evolve-retry:{candidate.id}"
                ),
                **self._profile_task_options(),
            )
        except (OSError, ValueError):
            self._record_evolve_event(
                "skipped",
                event_actor="human",
                trigger="/evolve retry",
                candidate=candidate,
                reason="queue-failed",
                proposal_id=proposal_id,
            )
            return "Enoch could not retry and queue that evolve candidate."
        candidate = retry_evolve_candidate(candidate.id, self.root, theme=state.theme)
        self._record_evolve_event(
            "queued",
            event_actor="human",
            trigger="/evolve retry",
            candidate=candidate,
            task_id=job.id,
            approval_actor="human",
            retry_of_task_id=failed_job.id,
            reason=f"retry-of-task-{failed_job.id}",
            proposal_id=proposal_id,
        )
        return (
            f"Retrying evolve candidate {candidate.id} as task #{job.id}, "
            f"linked to failed task #{failed_job.id}.\n\n"
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
                return "Use /evolve schedule daily HH:MM to run evolve once per day at local time."
            try:
                set_evolve_daily_schedule(rest, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand == "cron":
            if not rest.strip():
                return "Use /evolve schedule cron '30 9 * * *' to run evolve with a cron-style schedule."
            try:
                set_evolve_cron_schedule(rest, self.root)
            except ValueError as error:
                return str(error)
            return _format_evolve_report(evolve_report(self.root))
        if subcommand != "every":
            return self._apply_evolve_schedule_text(text)
        if not rest.strip():
            return "Use /evolve schedule every <interval> to set the scheduler frequency."
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
        return "\n".join(
            [
                f"Cron #{job.id} scheduled every {format_cron_interval(job.interval_seconds)}.",
                f"Next run: {job.next_run_at}",
            ]
        )

    def _maybe_start_task_worker(self) -> None:
        if self._stopping:
            return
        if self._task_worker is not None and self._task_worker.is_alive():
            return
        status = task_queue_status(self.root)
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
        while not self._stopping:
            job = begin_next_task(self.root)
            if job is None:
                if self._promote_next_backlog_if_idle() is None:
                    return
                job = begin_next_task(self.root)
                if job is None:
                    return
            self._run_task_job(job)
            if task_queue_status(self.root).paused_count:
                return

    def _promote_next_backlog_if_idle(self) -> TaskJob | None:
        status = task_queue_status(self.root)
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
        job = enqueue_task(
            item.chat_id,
            item.text,
            self.root,
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
        message_id = self._safe_send_message_id(item.chat_id, message)
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            record_task_status_message(job.id, message_id, self.root)
        return job

    def _enqueue_due_cron_jobs(self) -> tuple[TaskJob, ...]:
        jobs: list[TaskJob] = []
        for cron in claim_due_cron_jobs(self.root):
            try:
                job = enqueue_task(
                    cron.chat_id,
                    cron.text,
                    self.root,
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
            message_id = self._safe_send_message_id(cron.chat_id, message)
            if message_id is not None:
                self._work_status_messages[job.id] = message_id
                record_task_status_message(job.id, message_id, self.root)
        return tuple(jobs)

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
            wait_reason = (
                "retry-requires-human"
                if proposal.top_candidate.status == "failed"
                else "awaiting-human-approval"
            )
            self._record_evolve_event(
                "skipped",
                event_actor="system",
                trigger="evolve-scheduler",
                proposal=proposal,
                candidate=proposal.top_candidate,
                reason=wait_reason,
            )
        self._safe_send_message(chat_id, "Scheduled evolve check\n\n" + _format_evolve_proposal(proposal))
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
        claimed = claim_running_task(job.id, worker_id, os.getpid(), self.root)
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
                prs=list(job.pr_urls),
                context=job.context,
            )
            message_id = self._safe_send_message_id(
                job.chat_id,
                self._format_work_status(status_message),
            )
            if message_id is not None:
                created_status_message = True
                self._work_status_messages[job.id] = message_id
                record_task_status_message(job.id, message_id, self.root)
        task_status = WorkStatusMessage(
            chat_id=job.chat_id,
            message_id=message_id or 0,
            request=job.text,
            started_at=time.monotonic(),
            task_id=job.id,
            status="running",
            latest_update=start_update,
            prs=list(job.pr_urls),
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
            if job.publish_stage in {"committed", "pushed", "pr_opened"}:
                outcome = self._resume_task_publish(job)
            elif task_result_has_pull_request(job.result):
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
            if completed_status == "cancelled":
                finished_job = cancel_running_task(
                    self.root,
                    result=reply,
                    event_actor="system",
                    trigger="task-runner-cancelled",
                    expected_task_id=job.id,
                    worker_id=worker_id,
                )
                if finished_job is not None:
                    cancel_evolve_candidate_for_task(
                        finished_job,
                        self.root,
                        event_actor="human",
                        trigger="/stop",
                        reason=reply,
                    )
            elif completed_status == "failed":
                failure_actor = "system" if deadline.expired.is_set() else "agent"
                failure_trigger = "task-timeout" if deadline.expired.is_set() else "task-runner"
                failure = failure or classify_task_failure(reply)
                if failure.retryable and job.attempt < job.max_attempts:
                    finished_job = retry_running_task(
                        job.id,
                        self.root,
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
                    finished_job = fail_task(
                        job.id,
                        self.root,
                        result=reply,
                        event_actor=failure_actor,
                        trigger=failure_trigger,
                        worker_id=worker_id,
                        failure_code=failure.code,
                        failure_class=failure.failure_class,
                        retryable=False,
                    )
                if finished_job is not None and completed_status == "failed":
                    fail_evolve_candidate_for_task(
                        finished_job,
                        self.root,
                        event_actor=failure_actor,
                        trigger=failure_trigger,
                        reason=reply,
                    )
            elif completed_status == "paused":
                finished_job = pause_task(
                    job.id,
                    self.root,
                    result=reply,
                    event_actor="system",
                    trigger="codex-unavailable",
                    worker_id=worker_id,
                )
                if finished_job is not None:
                    pause_evolve_candidate_for_task(
                        finished_job,
                        self.root,
                        event_actor="system",
                        trigger="codex-unavailable",
                        reason=reply,
                    )
            else:
                finished_job = complete_task(
                    job.id,
                    self.root,
                    result=reply,
                    worker_id=worker_id,
                )
                if finished_job is not None:
                    complete_evolve_candidate_for_task(
                        finished_job,
                        self.root,
                        event_actor="agent",
                        trigger="task-runner",
                        reason=reply,
                    )
        authoritative_job = finished_job or _task_by_id(job.id, self.root)
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
            _cleanup_completed_task_worktree(summary_job, self.root)
            self._record_automatic_learning(summary_job, command=command, result=reply)
        if task_status is not None:
            task_status.prs = list(summary_job.pr_urls)
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
        cancellation_event = self._current_task_cancellation_event()
        if cancellation_event is not None and cancellation_event.is_set():
            raise AgentRuntimeCancelled("Enoch cancelled the active task.")

    def _record_automatic_learning(self, job: TaskJob, *, command: str, result: str) -> None:
        try:
            record_learning_artifact(
                self.identity,
                request=job.text,
                result=result,
                root=self.root,
                task_id=job.id,
                command=command,
                context_source=job.context_source,
                pr_urls=job.pr_urls,
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
        if subcommand == "all":
            inherit_command("/inherit show", self.root, command_name="inherit")
            candidates = load_parent_inbox_candidates(self.root)
            if not candidates:
                return "No pending direct-parent changes to inherit."
            replies = [self._adopt_lineage_candidate(chat_id, candidate.id) for candidate in candidates]
            return "\n\n".join(replies)
        if subcommand and subcommand not in {"show", "changes", "inbox", "refresh", "inspect", "ignore"}:
            return self._adopt_lineage_candidate(chat_id, subcommand)
        reply = inherit_command(text, self.root, command_name="inherit")
        if subcommand == "inspect":
            candidate = find_parent_inbox_candidate(argument, self.root)
            if candidate is not None:
                self._queue_session_sync(chat_id, lineage_candidate_context(candidate))
        return reply

    def _adopt_lineage_candidate(self, chat_id: int, candidate_id: str) -> str:
        if not candidate_id:
            return "Use /inherit <change_id>."
        candidate = _find_lineage_adopt_candidate(candidate_id, self.root)
        if candidate is None:
            return f"Enoch could not find direct-parent change {candidate_id}. Run /inherit first."

        reply = self._respond_read_only_turn(chat_id, lineage_adopt_prompt(candidate))
        memory_result = extract_memory_requests(reply)
        reply = memory_result.visible_reply
        memory_note = self._save_memory_requests(memory_result.requests)
        edit_request = extract_edit_request(reply)
        if edit_request is None:
            try:
                marked = mark_inbox_candidate(
                    candidate.id,
                    "ignored",
                    self.root,
                    note=_clip_activity_text(reply),
                )
                status_note = f"Marked ancestor change {marked.id} as skipped."
            except LineageError as error:
                status_note = f"Enoch could not update ancestor change status: {error}"
            return "\n\n".join(part for part in [reply, memory_note, status_note] if part)

        visible = edit_request.visible_reply
        if not self._action_allowed():
            return "\n\n".join(part for part in [visible, memory_note, self._action_lock_message()] if part)

        edit_result = self._run_tracked_inline_work(
            chat_id,
            edit_request.request,
            source="inheritance",
            initiated_by="human",
            trigger="/inherit",
            session_key=self._session_key(chat_id),
        )
        try:
            marked = mark_inbox_candidate(
                candidate.id,
                "adopted",
                self.root,
                note=_clip_activity_text(edit_result),
            )
            status_note = f"Marked ancestor change {marked.id} as adopted."
        except LineageError as error:
            status_note = f"Enoch could not update ancestor change status: {error}"
        return "\n\n".join(part for part in [visible, memory_note, edit_result, status_note] if part)

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
                states = list_task_worktrees(self.root)
            except VcsError as error:
                return f"Enoch could not list task worktrees: {error}"
            return _format_task_worktrees(states, self.root)

        action = parts[0].lower()
        if action == "show" and len(parts) == 2:
            task_id = _positive_task_id(parts[1])
            if task_id is None:
                return worktree_usage()
            try:
                state = task_worktree_state(self.root, task_id)
            except VcsError as error:
                return f"Enoch could not inspect task #{task_id} worktree: {error}"
            if state is None:
                return f"Task #{task_id} has no registered task worktree."
            return _format_task_worktree(state, self.root)

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
        active = _active_tasks_for_worktree(state, self.root)
        if active:
            labels = ", ".join(f"#{job.id} [{job.status}]" for job in active)
            return (
                f"Enoch will not remove task #{task_id} worktree because it is still "
                f"used by {labels}."
            )
        try:
            result = remove_managed_task_worktree(
                self.root,
                task_id,
                discard=discard,
            )
        except VcsError as error:
            return f"Enoch could not remove task #{task_id} worktree: {error}"
        _record_system_event(
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
                pull_requests = self.forge.list_open_pull_requests(self.root)
            except ForgeProviderError as error:
                return f"Enoch could not list open pull requests: {error}"
            return _format_open_pull_requests(pull_requests)
        if len(parts) == 2 and parts[0].lower() == "show":
            try:
                pull_request = self.forge.inspect_pull_request(parts[1], self.root)
            except ForgeProviderError as error:
                return f"Enoch could not inspect that pull request: {error}"
            return _format_pull_request(pull_request)
        if len(parts) != 2 or parts[0].lower() != "merge":
            return pr_usage()
        allowed_chat_id = _allowed_conversation_id(self.client)
        if allowed_chat_id is None or allowed_chat_id != chat_id:
            return (
                "Enoch will only merge a pull request from her locked "
                f"{provider_label(self.channel_name)} conversation."
            )
        try:
            result = self.forge.merge_pull_request(parts[1], self.root)
        except ForgeProviderError as error:
            return f"Enoch could not merge that pull request: {error}"
        return _format_pull_request_merge_result(result)

    def _update(self) -> str:
        if not self._action_allowed():
            return self._action_lock_message()
        result = update_from_authoritative(self.root)
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
            log_conversation_turn(
                chat_id=chat_id,
                message=text,
                reply=reply,
                root=self.root,
            )
            ensure_long_term_memory(self.root)
        except OSError:
            return


def main(chat_provider_name: str = "") -> None:
    root = Path.cwd()
    identity = load_identity()
    try:
        chat_provider = load_provider("chat", root, name=chat_provider_name)
        runtime_provider = load_provider("runtime", root)
        forge_provider = load_provider("forge", root)
        profile = load_profile(root)
    except (ProviderError, ChatProviderError, ProfileError) as error:
        print(str(error))
        raise SystemExit(1) from error
    selected_channel = _chat_provider_name(chat_provider)
    previous_shutdown_warning = _begin_lifecycle_run(root, provider=selected_channel)
    try:
        adopted = finalize_promoted_evolve_adoptions(root)
    except (OSError, ValueError, VcsError, EvolveLifecycleError):
        adopted = ()
    _record_system_event(
        "startup",
        root,
        details={
            "identity": identity.name,
            "previous_shutdown_warning": previous_shutdown_warning,
            "adopted_evolutions": [event.candidate_id for event in adopted],
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
    )
    _install_shutdown_handlers()
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


def _action_sandbox(_root: Path) -> str:
    return ACTION_SANDBOX_FULL_ACCESS


def _sandbox_description(sandbox: str) -> str:
    if sandbox == WORKSPACE_WRITE_SANDBOX:
        return "editing her code body"
    if sandbox == ACTION_SANDBOX_FULL_ACCESS:
        return "working with full filesystem access"
    return "thinking in read-only mode"


def _changed_files_or_empty(root: Path) -> tuple[str, ...]:
    try:
        return tuple(changed_files(root))
    except VcsError:
        return ()


def _delete_local_branch_if_enabled(
    branch: str,
    root: Path,
    *,
    protected_branch: str = "",
) -> str:
    if not _cleanup_local_branches(root):
        return ""
    if not branch or branch in {DEFAULT_BRANCH, protected_branch}:
        return ""
    delete_branch(branch, root, force=True)
    return f"Deleted local branch {branch}."


def _cleanup_local_branches(root: Path) -> bool:
    value = read_section("git", root).get("cleanup_local_branches", "").strip().lower()
    if not value:
        return True
    return value not in {"0", "false", "no", "off"}


def _activity_sync_note(*lines: str) -> str:
    body = "\n".join(f"- {line.strip()}" for line in lines if line.strip())
    return "\n".join(
        [
            "Internal Enoch activity sync.",
            "Record this as factual recent context for future recall. Do not treat it as a new user request.",
            body,
        ]
    )


def _work_reply_failed(reply: str) -> bool:
    normalized = reply.strip().lower()
    return (
        normalized.startswith("enoch could not ")
        or "i did not open a pr because doctor failed" in normalized
        or "doctor failed:" in normalized
    )


def _coerce_work_outcome(value: WorkOutcome | str) -> WorkOutcome:
    if isinstance(value, WorkOutcome):
        return value
    message = str(value)
    if _work_reply_failed(message):
        failure = classify_task_failure(message)
        return WorkOutcome.failure(
            message,
            code=failure.code,
            failure_class=failure.failure_class,
            retryable=failure.retryable,
        )
    return WorkOutcome.completed(message)


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


def _work_request_with_context(request: str, context: str) -> str:
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


def _replied_message_text(message: dict[str, Any]) -> str:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return ""
    for key in ("text", "caption"):
        value = reply.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _record_current_task_result(result: str, root: Path) -> None:
    task_status = _CURRENT_WORK_STATUS.get()
    task_id = task_status.task_id if task_status is not None and task_status.task_id is not None else _CURRENT_TASK_ID.get()
    if task_id is None:
        return
    record_task_result(task_id, result, root)


def _record_current_task_runtime_result(
    result: RuntimeResult,
    *,
    provider: str,
    root: Path,
) -> None:
    task_status = _CURRENT_WORK_STATUS.get()
    task_id = (
        task_status.task_id
        if task_status is not None and task_status.task_id is not None
        else _CURRENT_TASK_ID.get()
    )
    if task_id is None:
        return
    record_task_runtime_result(task_id, result, root, provider=provider)


def _reconciled_retry_result(
    job: TaskJob,
    root: Path,
    *,
    forge: ForgeProvider | None = None,
) -> str:
    forge = forge or FunctionForgeProvider(
        close_fn=close_pull_request,
        create_fn=create_pull_request,
        inspect_fn=inspect_pull_request,
        inspect_merge_fn=inspect_pull_request_merge,
        list_fn=list_open_pull_requests,
        merge_fn=merge_pull_request,
    )
    candidates = []
    logged_result = _latest_direct_action_result_for_task(job, root)
    if logged_result:
        candidates.append(logged_result)
    if job.result and job.result not in candidates:
        candidates.append(job.result)
    for result in candidates:
        urls = re.findall(
            r"https://[^\s]+/(?:pull|pulls|merge_requests)/\d+",
            result,
        )
        for url in urls:
            pull_request = forge.inspect_pull_request(url, root)
            if (
                pull_request.state == "OPEN"
                or pull_request.state == "MERGED"
                or pull_request.merged_at
            ):
                return result
    if job.branch_name:
        matching = [
            pull_request
            for pull_request in forge.list_open_pull_requests(root)
            if pull_request.head_branch == job.branch_name
        ]
        if matching:
            pull_request = matching[0]
            return (
                f"Reconciled existing PR #{pull_request.number} for task "
                f"branch {job.branch_name}: {pull_request.url}"
            )
    return ""


def _recover_running_task_from_direct_action_log(root: Path) -> TaskJob | None:
    running = task_queue_status(root).running
    if running is None or task_worker_is_active(running):
        return None
    result = _latest_direct_action_result_for_task(running, root)
    if not result:
        return None
    if _work_reply_failed(result):
        recovered = fail_task(running.id, root, result=result, event_actor="system", trigger="recovery")
        if recovered is not None:
            fail_evolve_candidate_for_task(
                recovered,
                root,
                event_actor="system",
                trigger="recovery",
                reason=result,
            )
        return recovered
    else:
        recovered = complete_task(
            running.id,
            root,
            result=result,
            event_actor="system",
            trigger="recovery",
        )
        if recovered is not None:
            complete_evolve_candidate_for_task(
                recovered,
                root,
                event_actor="system",
                trigger="recovery",
                reason=result,
            )
        return recovered


def _cleanup_completed_task_worktree(job: TaskJob | None, root: Path) -> None:
    if (
        job is None
        or job.status != "completed"
        or not job.worktree_path
        or not job.branch_name
        or not Path(job.worktree_path).exists()
    ):
        return
    try:
        remove_task_worktree(
            root,
            TaskWorktree(
                task_id=job.id,
                path=Path(job.worktree_path),
                branch=job.branch_name,
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
        paths = sorted(system_log_dir(root).glob("*.jsonl"))
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
        f"Theme: {theme or 'not set'}",
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
            f"Parent candidate: {candidate.parent_candidate_id or 'none'}",
            f"Source task: {f'#{candidate.source_task_id}' if candidate.source_task_id is not None else 'none'}",
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


def _evolution_provenance_for_job(job: TaskJob) -> EvolutionProvenance | None:
    if not job.candidate_id:
        return None
    return EvolutionProvenance(
        candidate_id=job.candidate_id,
        evidence_source=job.evidence_source or job.source,
        signal_actor=job.signal_actor or _legacy_candidate_signal_actor(job.source),
        candidate_actor=job.candidate_actor or "agent",
        approval_actor=job.approval_actor or _legacy_task_approval_actor(job),
        task_id=job.id,
        parent_candidate_id=job.parent_candidate_id,
        source_task_id=job.source_task_id,
        retry_of_task_id=job.parent_task_id,
    )


def _legacy_candidate_signal_actor(source: str) -> str:
    if source in {"backlog", "feedback", "learning"}:
        return "human"
    if source in {"inheritance", "brainstorming"}:
        return "agent"
    return "system"


def _legacy_task_approval_actor(job: TaskJob) -> str:
    if job.trigger.startswith("/evolve ") or job.context_source in {"evolve-approve", "evolve-retry"}:
        return "human"
    if job.context_source == "evolve-scheduler":
        return "agent"
    return job.initiated_by


def _create_pull_request_for_current_task(
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
    task_id = _CURRENT_TASK_ID.get()
    job = _task_by_id(task_id, state_root) if task_id is not None else None
    provenance = _evolution_provenance_for_job(job) if job is not None else None
    if provenance is None:
        return forge.create_pull_request(root=work_root)
    return forge.create_pull_request(root=work_root, evolution_provenance=provenance)


def _history_task(task_id: int, root: Path) -> TaskJob | None:
    for job in reversed(task_queue_status(root).history):
        if job.id == task_id:
            return job
    return None


def _task_by_id(task_id: int, root: Path) -> TaskJob | None:
    status = task_queue_status(root)
    jobs = [*status.pending, *status.paused, *status.history]
    if status.running is not None:
        jobs.append(status.running)
    return next((job for job in jobs if job.id == task_id), None)


def _format_task_worktrees(states: tuple[TaskWorktreeState, ...], root: Path) -> str:
    if not states:
        return "Task worktrees: none"
    lines = [f"Task worktrees ({len(states)}):"]
    for state in states:
        condition = "unknown" if state.inspection_error else ("clean" if state.clean else "dirty")
        branch = state.branch or "(unknown branch)"
        linked = _tasks_for_worktree(state, root)
        tasks = ", ".join(f"#{job.id} [{job.status}]" for job in linked) or "no recent task record"
        lines.extend(
            [
                f"- task path #{state.task_id} [{condition}] {branch}",
                f"  Tasks: {tasks}",
                f"  Path: {state.path}",
            ]
        )
    return "\n".join(lines)


def _format_task_worktree(state: TaskWorktreeState, root: Path) -> str:
    condition = "unknown" if state.inspection_error else ("clean" if state.clean else "dirty")
    linked = _tasks_for_worktree(state, root)
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


def _tasks_for_worktree(state: TaskWorktreeState, root: Path) -> tuple[TaskJob, ...]:
    status = task_queue_status(root)
    jobs = [*status.pending, *status.paused, *status.history]
    if status.running is not None:
        jobs.append(status.running)
    state_path = state.path.resolve()
    linked = []
    for job in jobs:
        same_path = False
        if job.worktree_path:
            try:
                same_path = Path(job.worktree_path).expanduser().resolve() == state_path
            except OSError:
                same_path = False
        if same_path or (state.branch and job.branch_name == state.branch):
            linked.append(job)
    return tuple(sorted(linked, key=lambda job: job.id))


def _active_tasks_for_worktree(state: TaskWorktreeState, root: Path) -> tuple[TaskJob, ...]:
    return tuple(
        job
        for job in _tasks_for_worktree(state, root)
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


def _duplicate_close_comment(keep_number: int | None) -> str:
    if keep_number is None:
        return "Closing this pull request from a Enoch maintenance job."
    return f"Closing as a duplicate of #{keep_number}. Keeping #{keep_number} as the canonical PR for this change."


def _format_pr_close_results(results: list[PullRequestCloseResult], keep_number: int | None) -> str:
    if not results:
        return "Enoch could not close any pull requests: no duplicate PR numbers were found."
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
            lines.append(f"- #{result.number}: failed ({result.note or 'unknown error'})")
    if failed:
        return "Enoch could not complete every pull request update.\n\n" + "\n".join(lines)
    return "\n".join(lines)


def _load_task_status_messages(root: Path) -> dict[int, MessageId]:
    status = task_queue_status(root)
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
) -> None:
    runtime = runtime or FunctionAgentRuntime(
        respond_fn=respond,
        act_in_session_fn=act_in_session,
        model_summary_fn=model_summary,
        model_options_fn=codex_model_options,
        reset_usage_fn=reset_token_usage,
    )
    try:
        invoke_runtime_respond(
            runtime,
            identity,
            note,
            cwd=root,
            execution=RuntimeExecutionControl(
                request_id=f"session-sync:{chat_id}",
                session_key=session_key or f"chat:{chat_id}",
            ),
        )
    except (AgentRuntimeError, TypeError):
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
