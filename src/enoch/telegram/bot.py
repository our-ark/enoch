from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import signal
import threading
import time
from typing import Any

from enoch.backlog import (
    BacklogItem,
    add_backlog_item,
    backlog_item,
    backlog_status,
    next_backlog_item,
    normalize_priority,
    promote_backlog_item,
    remove_backlog_item,
    reprioritize_backlog_item,
)
from enoch.automatic_learning import record_learning_artifact
from enoch.brain import BrainCancelled, BrainError, act_in_session, model_summary, reset_token_usage, respond
from enoch.brainstorming import generate_brainstorm_ideas
from enoch.command_surface import (
    ACTION_MODE_CONVERSATION,
    ACTION_MODE_FULL_ACCESS,
    action_mode as _action_mode,
    action_mode_description as _action_mode_description,
    action_mode_label as _action_mode_label,
    save_action_mode as _save_action_mode,
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
from enoch.evolve import (
    MODE_AUTO_EVOLVE,
    MODE_CO_EVOLVE,
    MODE_DISABLED,
    EvolveCandidate,
    EvolveReport,
    EvolveState,
    cancel_evolve_candidate_for_task,
    claim_due_evolve_schedule,
    complete_evolve_candidate_for_task,
    disable_evolve_schedule,
    evolve_report,
    fail_evolve_candidate_for_task,
    get_evolve_candidate,
    load_evolve_candidates,
    reject_evolve_candidate,
    run_evolve_candidate,
    select_evolve_candidate,
    set_evolve_cron_schedule,
    set_evolve_daily_schedule,
    set_evolve_schedule,
    set_evolve_mode,
    set_evolve_theme,
)
from enoch.git_tools import (
    GitError,
    changed_files,
    create_branch,
    delete_branch,
    current_branch,
    diff_summary,
    ensure_clean_worktree,
    switch_branch,
)
from enoch.formatting import (
    format_doctor_result,
    format_pr_result,
    format_telegram_publish_result,
    format_telegram_remote_publish_result,
    pr_step_update,
    pr_summary,
    publish_summary,
    remote_publish_summary,
    summarize_for_log,
)
from enoch.github.workflow import (
    LocalPublishResult,
    PublishError,
    PullRequestCloseResult,
    PullRequestResult,
    RemotePublishResult,
    close_pull_request,
    create_pull_request,
    feature_title,
    prepare_local_publish,
    push_current_branch,
)
from enoch.identity import Identity, identity_file_path, load_identity
from enoch.immune import ImmuneResult, run_immune_system
from enoch.learn import (
    LearnError,
    explore_peer_skills,
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
    extract_edit_request,
    extract_memory_requests,
    read_only_turn_prompt,
    repository_handoff_note,
    startup_context_note,
    work_request_prompt,
)
from enoch.runtime import (
    ACTION_SANDBOX_FULL_ACCESS,
    ACTION_SANDBOX_READ_ONLY,
    DEFAULT_BRANCH,
    PROTECTED_BRANCHES,
    WORKSPACE_WRITE_SANDBOX,
)
from enoch.skills import SkillsError
from enoch.task_queue import (
    TaskJob,
    begin_direct_task,
    begin_next_task,
    cancel_task,
    cancel_running_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    fail_task,
    recover_interrupted_task,
    record_task_result,
    record_task_status_message,
    task_result_has_pull_request,
    task_queue_status,
)
from enoch.telegram.client import TelegramClient, TelegramError, load_config
from enoch.commands import (
    action_lock_message as _action_lock_message,
    doctor_command,
    help_message as _help_message,
    identity_summary,
    inherit_command,
    lineage_command,
    mission_command,
    skills_command,
    status_message,
)
from enoch.telegram.lifecycle import (
    begin_lifecycle_run as _begin_lifecycle_run,
    load_telegram_offset as _load_telegram_offset,
    next_update_offset as _next_update_offset,
    record_lifecycle_shutdown as _record_lifecycle_shutdown,
    save_telegram_offset as _save_telegram_offset,
    shutdown_message as _shutdown_message,
    startup_message as _startup_message,
)
from enoch.update_tools import (
    ensure_local_main_current as _ensure_local_main_current,
    schedule_daemon_restart as _schedule_daemon_restart,
    schedule_daemon_stop as _schedule_daemon_stop,
)
from enoch.updater import update_from_main


class ShutdownRequested(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class WorkStatusMessage:
    chat_id: int
    message_id: int
    request: str
    started_at: float
    task_id: int | None = None
    status: str = "queued"
    latest_update: str = "Queued."
    prs: list[str] = field(default_factory=list)
    context: str = ""


@dataclass(frozen=True)
class GithubMaintenanceRequest:
    close_numbers: tuple[int, ...]
    keep_number: int | None = None


@dataclass(frozen=True)
class TaskContextSnapshot:
    context: str = ""
    source: str = ""
    clarification: str = ""
    error: str = ""


TASK_CONTEXT_SOURCE_CHAT = "chat-snapshot"
NEEDS_CLARIFICATION_PREFIX = "NEEDS_CLARIFICATION:"
NO_EXTRA_TASK_CONTEXT = "No extra context needed."
_CURRENT_WORK_STATUS: ContextVar[WorkStatusMessage | None] = ContextVar("enoch_work_status", default=None)
_CURRENT_TASK_ID: ContextVar[int | None] = ContextVar("enoch_task_id", default=None)


class EnochTelegramBot:
    def __init__(
        self,
        identity: Identity,
        root: Path,
        client: TelegramClient,
        previous_shutdown_warning: str = "",
    ) -> None:
        self.identity = identity
        self.root = root
        self.client = client
        self.previous_shutdown_warning = previous_shutdown_warning
        self.offset: int | None = _load_telegram_offset(root)
        self._restart_after_reply = False
        self._stop_after_reply = False
        self._pending_session_syncs: list[tuple[int, str]] = []
        self._task_worker: threading.Thread | None = None
        self._task_cancellations: dict[int, threading.Event] = {}
        _recover_running_task_from_direct_action_log(root)
        recover_interrupted_task(root)
        self._work_status_messages: dict[int, int] = _load_task_status_messages(root)

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except (OSError, TelegramError) as error:
                print(f"Enoch Telegram polling error: {error}")
                time.sleep(5)

    def notify_startup(self) -> None:
        chat_id = self.client.config.allowed_chat_id
        if chat_id is None:
            return
        self.client.send_message(
            chat_id,
            _startup_message(self.identity, self.root, self.previous_shutdown_warning),
        )
        _sync_session_activity(self.identity, self.root, chat_id, startup_context_note(memory_for_prompt(self.root)))

    def notify_shutdown(self, reason: str) -> None:
        _record_system_event("shutdown", self.root, details={"reason": reason})
        chat_id = self.client.config.allowed_chat_id
        if chat_id is None:
            return
        self.client.send_message(chat_id, _shutdown_message(self.identity, self.root, reason))

    def run_once(self) -> None:
        for update in self.client.get_updates(self.offset):
            self.handle_update(update)
        self._enqueue_due_cron_jobs()
        self._run_due_evolve_schedule()
        self._maybe_start_task_worker()

    def handle_update(self, update: dict[str, Any]) -> None:
        next_offset = _next_update_offset(update)
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        text = str(message.get("text") or "").strip()
        if not isinstance(chat_id, int) or not text:
            self._remember_update_offset(next_offset)
            return
        if not self._chat_allowed(chat_id):
            self._remember_update_offset(next_offset)
            return

        self._safe_send_read_ack(chat_id, message_id)
        reset_token_usage()
        command, argument = _parse_telegram_command(text)
        work_text = _with_replied_message_context(text, message)
        if command == "/start":
            reply = "Use /help to see available commands."
        elif command == "/help":
            reply = _help_message(argument)
        elif command == "/ancestors":
            reply = self._ancestors(chat_id, text)
        elif command == "/inherit":
            reply = self._inherit(chat_id, text)
        elif command == "/mission":
            reply = self._mission(text)
        elif command == "/skills":
            reply = self._skills(text)
        elif command == "/learn":
            reply = self._learn(chat_id, text)
        elif command == "/do":
            reply = self._do(chat_id, work_text)
        elif command == "/task":
            reply = self._task(chat_id, work_text)
        elif command == "/tasks":
            reply = _format_tasks_report(self.root)
        elif command == "/stop":
            reply = self._stop_running_job()
        elif command in {"/backlog", "/backlogs"}:
            reply = self._backlog(chat_id, work_text)
        elif command in {"/cron", "/crons"}:
            reply = self._cron(chat_id, work_text)
        elif command == "/evolve":
            reply = self._evolve(chat_id, argument)
        elif command == "/mode":
            reply = self._mode(text)
        elif command == "/shutdown":
            reply = self._shutdown_from_telegram()
        elif command == "/self":
            reply = identity_summary(self.identity, self.root)
        elif command == "/status":
            reply = self._status(chat_id)
        elif command == "/doctor":
            reply = self._doctor()
        elif command == "/update":
            reply = self._update()
            self._queue_session_sync(
                chat_id,
                _activity_sync_note(
                    "User ran /update.",
                    f"Result: {_clip_activity_text(reply)}",
                ),
            )
        elif command == "/restart":
            reply = self._restart_from_telegram()
        else:
            reply = self._natural(chat_id, text)

        send_error = self._safe_send_message(chat_id, reply) if reply else ""
        logged_reply = reply
        if send_error:
            logged_reply = "\n\n".join([reply, f"Telegram send failed: {send_error}"])
        self._record_turn(chat_id, text, logged_reply)
        self._flush_session_syncs()
        self._remember_update_offset(next_offset)
        if self._restart_after_reply:
            self._restart_after_reply = False
            _schedule_daemon_restart(self.root)
        if self._stop_after_reply:
            self._stop_after_reply = False
            _schedule_daemon_stop(self.root)
            raise ShutdownRequested("Telegram /shutdown")

    def _remember_update_offset(self, offset: int | None) -> None:
        if offset is None:
            return
        self.offset = offset
        _save_telegram_offset(offset, self.root)

    def _chat_allowed(self, chat_id: int) -> bool:
        allowed = self.client.config.allowed_chat_id
        return allowed is None or allowed == chat_id

    def _respond_read_only_turn(self, chat_id: int, text: str, *, session_key: str | None = None) -> str:
        try:
            return respond(
                self.identity,
                read_only_turn_prompt(text),
                cwd=self.root,
                session_key=session_key or f"telegram:{chat_id}",
            )
        except BrainError as error:
            return str(error)

    def _queue_session_sync(self, chat_id: int | None, note: str) -> None:
        if chat_id is None or not note.strip():
            return
        self._pending_session_syncs.append((chat_id, note.strip()))

    def _flush_session_syncs(self) -> None:
        pending = self._pending_session_syncs
        self._pending_session_syncs = []
        for chat_id, note in pending:
            _sync_session_activity(self.identity, self.root, chat_id, note)

    def _natural(self, chat_id: int, text: str) -> str:
        return self._natural_with_session(chat_id, text, session_key=f"telegram:{chat_id}")

    def _natural_with_session(self, chat_id: int, text: str, *, session_key: str) -> str:
        reply = self._respond_read_only_turn(chat_id, text, session_key=session_key)
        memory_result = extract_memory_requests(reply)
        reply = memory_result.visible_reply
        edit_request = extract_edit_request(reply)
        if edit_request is not None:
            reply = edit_request.visible_reply
        memory_note = self._save_memory_requests(memory_result.requests)
        return "\n\n".join(part for part in [reply, memory_note] if part)

    def _do(self, chat_id: int, text: str) -> str:
        command, argument = _parse_telegram_command(text)
        if command != "/do" or not argument:
            return "Use /do <request> to run work now."
        if not self._action_allowed():
            return _action_lock_message()
        running = task_queue_status(self.root).running
        snapshot = self._resolve_task_context_snapshot(chat_id, argument)
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

    def _resolve_task_context_snapshot(self, chat_id: int, request: str) -> TaskContextSnapshot:
        try:
            reply = respond(
                self.identity,
                _task_context_snapshot_prompt(request),
                cwd=self.root,
                session_key=f"telegram:{chat_id}",
            )
        except BrainError as error:
            return TaskContextSnapshot(error=str(error))
        return _parse_task_context_snapshot(reply)

    def _run_direct_work_with_status(
        self,
        chat_id: int,
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
            session_key = f"telegram:{chat_id}:do:{direct_task.id}"
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
        message_id = self._safe_send_message_id(chat_id, _format_work_status_message(status_message))
        if message_id is not None:
            self._work_status_messages[direct_task.id] = message_id
            record_task_status_message(direct_task.id, message_id, self.root)
        self._start_direct_work_worker(direct_task, session_key=session_key)
        if message_id is not None:
            return ""
        return f"Started task #{direct_task.id}. Enoch is working on it now."

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
            )
        except (OSError, ValueError):
            return "Enoch could not queue that /do request."
        message = _format_work_status_message(
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
            target=self._run_direct_task_job,
            kwargs={"job": job, "session_key": session_key},
            name=f"enoch-direct-task-{job.id}",
            daemon=True,
        )
        worker.start()

    def _run_direct_task_job(self, job: TaskJob, *, session_key: str = "") -> None:
        self._run_action_job(
            job,
            command="/do",
            session_key=session_key or f"telegram:{job.chat_id}:do:{job.id}",
            start_update=f"Starting direct task #{job.id}.",
            failure_prefix=f"Enoch could not complete direct task #{job.id}",
        )

    def _run_direct_work(self, chat_id: int, request: str, *, context: str = "", session_key: str) -> str:
        github_maintenance = _github_maintenance_request(request)
        if github_maintenance is not None:
            return self._run_github_maintenance(github_maintenance)

        publish_branch = _existing_branch_publish_request(request)
        if publish_branch is not None:
            return self._publish_existing_branch(chat_id, publish_branch)

        try:
            sandbox = _action_sandbox(self.root)
            self._send_step_update(chat_id, "Preparing a fresh branch.")
            branch_note = self._ensure_action_branch(request)
            branch = current_branch(self.root)
            before_action = _worktree_snapshot(self.root)
            self._send_step_update(chat_id, "Working.")
            result = act_in_session(
                self.identity,
                work_request_prompt(_work_request_with_context(request, context)),
                cwd=self.root,
                sandbox=sandbox,
                session_key=session_key,
                progress_callback=lambda elapsed, sandbox: self._send_progress(chat_id, elapsed, sandbox),
                cancellation_event=self._current_task_cancellation_event(),
            )
            memory_result = extract_memory_requests(result)
            result = memory_result.visible_reply
            memory_note = self._save_memory_requests(memory_result.requests)
            _record_direct_action(request, result, self.root)
            action_files = tuple(sorted(_changed_files_or_empty(self.root)))
            after_action = _worktree_snapshot(self.root)
        except BrainCancelled:
            raise
        except (BrainError, GitError, OSError) as error:
            return f"Enoch could not complete the requested work yet: {error}"

        parts = [branch_note, result or "Enoch completed the requested work.", memory_note]
        if before_action == after_action:
            try:
                cleanup = self._return_to_main_and_delete_branch(branch)
                parts.append("No files changed.")
                parts.append(cleanup)
            except GitError as error:
                parts.append(f"Enoch could not clean up the temporary branch: {error}")
            return "\n\n".join(part for part in parts if part)

        self._send_step_update(chat_id, "Running doctor.")
        doctor = run_immune_system(self.root)
        parts.append(_format_doctor_result(doctor))
        self._send_step_update(chat_id, "Doctor passed." if doctor.passed else "Doctor failed.")
        if not doctor.passed:
            parts.append("I did not open a PR because doctor failed. Enoch is still on the feature branch for inspection.")
            return "\n\n".join(part for part in parts if part)

        parts.append(self._publish_feature_pr(chat_id, request, action_files))
        return "\n\n".join(part for part in parts if part)

    def _run_github_maintenance(self, request: "GithubMaintenanceRequest") -> str:
        self._update_work_status("Updating GitHub pull requests.")
        results = [
            close_pull_request(
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
            message_id = self._safe_send_message_id(chat_id, _format_work_status_message(status_message))
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
        original_branch = current_branch(self.root)
        outputs: list[str] = []
        try:
            ensure_clean_worktree(self.root)
            self._send_step_update(chat_id, f"Switching to branch {branch}.")
            switch_branch(branch, self.root)

            self._send_step_update(chat_id, f"Pushing branch {branch}.")
            pushed = push_current_branch(root=self.root)
            outputs.append(_format_remote_publish_result(pushed))
            self._send_step_update(chat_id, f"Pushed branch {pushed.branch}.")

            self._send_step_update(chat_id, "Opening a pull request.")
            pr = create_pull_request(root=self.root)
            outputs.append(_format_pr_result(pr))
            if pr.url:
                self._update_work_status(_pr_step_update(pr), pr_url=pr.url)
                _record_current_task_result("\n\n".join(outputs), self.root)
            self._send_step_update(chat_id, _pr_step_update(pr))

            self._send_step_update(chat_id, "Returning local checkout to main.")
            switch_branch(DEFAULT_BRANCH, self.root)
            outputs.append(f"Enoch switched local checkout back to {DEFAULT_BRANCH}.")
            self._send_step_update(chat_id, "Local checkout is back on main.")
            if pr.url:
                self._queue_session_sync(chat_id, repository_handoff_note(pr.branch, pr.url))
        except (GitError, PublishError) as error:
            failure = f"Enoch could not publish existing branch {branch}: {error}"
            self._send_step_update(chat_id, failure)
            try:
                if current_branch(self.root) != original_branch:
                    switch_branch(original_branch, self.root)
            except GitError:
                pass
            return "\n\n".join([*outputs, failure]) if outputs else failure
        return "\n\n".join(outputs)

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
    ) -> str:
        outputs: list[str] = []
        summaries: list[str] = []
        try:
            self._send_step_update(chat_id, "Committing the change.")
            commit = prepare_local_publish(
                feature_title(request),
                root=self.root,
                allowed_files=allowed_files,
            )
            outputs.append(_format_publish_result(commit))
            summaries.append(_publish_summary(commit))
            self._send_step_update(chat_id, f"Committed {commit.commit_sha}.")

            self._send_step_update(chat_id, "Pushing the branch to GitHub.")
            pushed = push_current_branch(root=self.root)
            outputs.append(_format_remote_publish_result(pushed))
            summaries.append(_remote_publish_summary(pushed))
            self._send_step_update(chat_id, f"Pushed branch {pushed.branch}.")

            self._send_step_update(chat_id, "Opening a pull request.")
            pr = create_pull_request(root=self.root)
            outputs.append(_format_pr_result(pr))
            summaries.append(_pr_summary(pr))
            if pr.url:
                self._update_work_status(_pr_step_update(pr), pr_url=pr.url)
                _record_current_task_result("\n\n".join(outputs), self.root)
            self._send_step_update(chat_id, _pr_step_update(pr))

            self._send_step_update(chat_id, "Returning local checkout to main.")
            handoff = self._return_to_main_after_handoff()
            outputs.append(handoff)
            summaries.append(handoff)
            self._send_step_update(chat_id, "Local checkout is back on main.")
            if pr.url:
                self._queue_session_sync(chat_id, repository_handoff_note(pr.branch, pr.url))
        except (GitError, PublishError) as error:
            failure = f"Enoch could not publish this edit as a pull request: {error}"
            self._send_step_update(chat_id, failure)
            return "\n\n".join([*outputs, failure]) if outputs else failure

        _record_direct_action(f"published edit as pull request: {request}", "\n\n".join(summaries), self.root)
        reply = "\n\n".join(outputs)
        self._queue_session_sync(
            chat_id,
            _activity_sync_note(
                f"Enoch published an edit request as a pull request: {request}",
                f"Final workflow summary: {_clip_activity_text(summaries[-1]) if summaries else 'none'}",
                f"Result: {_clip_activity_text(reply)}",
            ),
        )
        return reply

    def _return_to_main_after_handoff(self) -> str:
        branch = current_branch(self.root)
        if branch == DEFAULT_BRANCH:
            return f"Local checkout is already on {DEFAULT_BRANCH}."
        ensure_clean_worktree(self.root)
        switch_branch(DEFAULT_BRANCH, self.root)
        cleanup = _delete_local_branch_if_enabled(branch, self.root)
        if cleanup:
            return "\n".join(
                [
                    f"Enoch switched local checkout back to {DEFAULT_BRANCH}.",
                    cleanup,
                    "The change remains on the pushed GitHub branch.",
                ]
            )
        return (
            f"Enoch switched local checkout back to {DEFAULT_BRANCH}. "
            "The change remains on the pushed GitHub branch."
        )

    def _return_to_main_and_delete_branch(self, branch: str) -> str:
        if branch == DEFAULT_BRANCH:
            return f"Local checkout is already on {DEFAULT_BRANCH}."
        ensure_clean_worktree(self.root)
        switch_branch(DEFAULT_BRANCH, self.root)
        cleanup = _delete_local_branch_if_enabled(branch, self.root)
        if cleanup:
            return "\n".join([f"Enoch switched local checkout back to {DEFAULT_BRANCH}.", cleanup])
        return f"Enoch switched local checkout back to {DEFAULT_BRANCH}."

    def _send_step_update(self, chat_id: int | None, message: str) -> None:
        if chat_id is None:
            return
        if self._update_work_status(message):
            return
        self._safe_send_message(chat_id, f"Enoch update: {message}")

    def _safe_send_message(self, chat_id: int, message: str) -> str:
        try:
            self.client.send_message(chat_id, message)
        except (OSError, TelegramError) as error:
            return str(error)
        return ""

    def _safe_send_message_id(self, chat_id: int, message: str) -> int | None:
        try:
            return self.client.send_message(chat_id, message)
        except (OSError, TelegramError):
            return None

    def _safe_edit_message(self, chat_id: int, message_id: int, message: str) -> None:
        try:
            self.client.edit_message(chat_id, message_id, message)
        except (OSError, TelegramError):
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
        self._safe_edit_message(
            task_status.chat_id,
            task_status.message_id,
            _format_work_status_message(task_status),
        )
        return True

    def _safe_send_read_ack(self, chat_id: int, message_id: object) -> None:
        if not isinstance(message_id, int):
            return
        try:
            self.client.send_read_ack(chat_id, message_id)
        except (OSError, TelegramError) as error:
            _record_system_event(
                "telegram_read_ack_failed",
                self.root,
                status="failed",
                details={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "error": str(error),
                },
            )

    def _status(self, chat_id: int | None = None) -> str:
        status = status_message(
            self.identity,
            self.root,
            allowed_chat_id=self.client.config.allowed_chat_id,
            chat_id=chat_id,
            model_summary_fn=model_summary,
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
            return "\n\n".join(part for part in [visible, memory_note, _action_lock_message()] if part)

        edit_result = self._run_direct_work(
            chat_id,
            edit_request.request,
            session_key=f"telegram:{chat_id}",
        )
        return "\n\n".join(part for part in [visible, memory_note, edit_result] if part)

    def _task(self, chat_id: int, text: str) -> str:
        command, argument = _parse_telegram_command(text)
        if command != "/task" or not argument:
            return "Use /task <request> to queue background work."
        cancel_id = _task_cancel_id(argument)
        if argument.split(maxsplit=1)[0].lower() == "cancel" and cancel_id is None:
            return "Use /task cancel <id> to cancel a queued task."
        if cancel_id is not None:
            cancelled = cancel_task(cancel_id, self.root)
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
                self._safe_edit_message(cancelled.chat_id, message_id, _format_work_status_message(cancelled_status))
            return f"Cancelled task #{cancelled.id}."
        snapshot = self._resolve_task_context_snapshot(chat_id, argument)
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
            )
        except (OSError, ValueError):
            return "Enoch could not queue that task."
        status = task_queue_status(self.root)
        position = status.pending_count
        message = _format_work_status_message(
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
            self._safe_edit_message(cancelled.chat_id, message_id, _format_work_status_message(stopped_status))
        return f"Stopped task #{cancelled.id}."

    def _backlog(self, chat_id: int, text: str) -> str:
        command, argument = _parse_telegram_command(text)
        if command == "/backlogs":
            return _format_backlog_report(self.root)
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
                return "Use /evolve theme <text> to set Enoch's current evolution theme."
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
            except (BrainError, OSError, ValueError) as error:
                return f"Enoch could not brainstorm evolution candidates: {error}"
            report = evolve_report(self.root)
            return f"Added {len(ideas)} theme-guided brainstorming candidate(s).\n\n" + _format_evolve_report(report)
        if subcommand == "explore":
            agent = rest.strip()
            if not agent:
                return "Use /evolve explore <agent> to inspect a non-parent agent's published skills."
            try:
                observations = explore_peer_skills(agent, self.root)
            except (OSError, SkillsError, ValueError) as error:
                return f"Enoch could not explore that peer agent: {error}"
            report = evolve_report(self.root)
            return f"Added {len(observations)} peer-learning candidate(s) from {agent}.\n\n" + _format_evolve_report(report)
        if subcommand in {"candidate", "candidates"}:
            report = evolve_report(self.root)
            include_inactive = rest.strip().lower() in {"all", "inactive"}
            candidates = (
                load_evolve_candidates(self.root, include_inactive=True, theme=report.state.theme)
                if include_inactive
                else report.candidates
            )
            return _format_evolve_candidates(candidates, include_inactive=include_inactive)
        if subcommand == "select":
            if not rest.strip():
                return "Use /evolve select <id> to select a self-evolution candidate."
            state = evolve_report(self.root).state
            try:
                candidate = select_evolve_candidate(rest, self.root, theme=state.theme)
            except ValueError as error:
                return str(error)
            return "Selected evolve candidate.\n\n" + "\n".join(_format_evolve_candidate(candidate))
        if subcommand == "reject":
            if not rest.strip():
                return "Use /evolve reject <id> to reject a self-evolution candidate."
            state = evolve_report(self.root).state
            try:
                candidate = reject_evolve_candidate(rest, self.root, theme=state.theme)
            except ValueError as error:
                return str(error)
            return "Rejected evolve candidate.\n\n" + "\n".join(_format_evolve_candidate(candidate))
        if subcommand == "run":
            if not rest.strip():
                return "Use /evolve run <id> to queue a self-evolution candidate."
            return self._evolve_run(rest)
        if subcommand == "schedule":
            return self._evolve_schedule(rest)
        return _evolve_usage()

    def _evolve_run(self, candidate_id: str) -> str:
        chat_id = self.client.config.allowed_chat_id
        if chat_id is None:
            return "Enoch needs a locked Telegram chat before queueing evolve work."
        state = evolve_report(self.root).state
        try:
            candidate = get_evolve_candidate(candidate_id, self.root, theme=state.theme)
        except ValueError as error:
            return str(error)
        try:
            job = enqueue_task(
                chat_id,
                _evolve_task_request(candidate, state.theme),
                self.root,
                context=_evolve_task_context(candidate),
                context_source="evolve-run",
            )
        except (OSError, ValueError):
            return "Enoch could not queue that evolve candidate."
        candidate = run_evolve_candidate(candidate.id, self.root, theme=state.theme)
        return f"Queued evolve candidate {candidate.id} as task #{job.id}.\n\n" + "\n".join(
            _format_evolve_candidate(candidate)
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
        command, argument = _parse_telegram_command(text)
        if command == "/crons":
            return _format_cron_report(self.root)
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
        if self._task_worker is not None and self._task_worker.is_alive():
            return
        status = task_queue_status(self.root)
        if status.running is not None:
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
        while True:
            job = begin_next_task(self.root)
            if job is None:
                if self._promote_next_backlog_if_idle() is None:
                    return
                job = begin_next_task(self.root)
                if job is None:
                    return
            self._run_task_job(job)

    def _promote_next_backlog_if_idle(self) -> TaskJob | None:
        status = task_queue_status(self.root)
        if status.running is not None or status.pending_count > 0:
            return None
        item = next_backlog_item(self.root)
        if item is None:
            return None
        return self._enqueue_backlog_item(item)

    def _promote_backlog_item_to_queue(self, item_id: int) -> TaskJob | None:
        item = backlog_item(item_id, self.root)
        if item is None:
            return None
        return self._enqueue_backlog_item(item)

    def _enqueue_backlog_item(self, item: BacklogItem) -> TaskJob:
        job = enqueue_task(
            item.chat_id,
            item.text,
            self.root,
            context=item.context,
            context_source=item.context_source,
        )
        promoted = promote_backlog_item(item.id, self.root, promoted_task_id=job.id)
        if promoted is None:
            return job
        message = _format_work_status_message(
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
                    context_source=cron.context_source or "cron",
                )
            except (OSError, ValueError):
                continue
            record_cron_task(cron.id, job.id, self.root)
            jobs.append(job)
            message = _format_work_status_message(
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
        chat_id = self.client.config.allowed_chat_id
        if chat_id is None or claimed.mode == MODE_DISABLED:
            return None
        report = evolve_report(self.root)
        if claimed.mode == MODE_CO_EVOLVE:
            self._safe_send_message(chat_id, "Scheduled evolve check\n\n" + _format_evolve_report(report))
            return None
        if claimed.mode != MODE_AUTO_EVOLVE or report.top_candidate is None:
            return None
        candidate = report.top_candidate
        request = _evolve_task_request(candidate, report.state.theme)
        context = _evolve_task_context(candidate)
        try:
            job = enqueue_task(
                chat_id,
                request,
                self.root,
                context=context,
                context_source="evolve-scheduler",
            )
        except (OSError, ValueError):
            return None
        run_evolve_candidate(candidate.id, self.root, theme=report.state.theme)
        message = _format_work_status_message(
            WorkStatusMessage(
                chat_id=chat_id,
                message_id=0,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="queued",
                latest_update=f"Scheduled by evolve {claimed.mode}.",
                context=job.context,
            )
        )
        message_id = self._safe_send_message_id(chat_id, message)
        if message_id is not None:
            self._work_status_messages[job.id] = message_id
            record_task_status_message(job.id, message_id, self.root)
        return job

    def _run_task_job(self, job: TaskJob) -> None:
        self._run_action_job(
            job,
            command="/task",
            session_key=f"telegram:{job.chat_id}:task:{job.id}",
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
        message_id = self._work_status_messages.get(job.id) or job.status_message_id
        task_status = (
            WorkStatusMessage(
                chat_id=job.chat_id,
                message_id=message_id,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="running",
                prs=list(job.pr_urls),
                context=job.context,
            )
            if message_id is not None
            else None
        )
        token = _CURRENT_WORK_STATUS.set(task_status)
        task_token = _CURRENT_TASK_ID.set(job.id)
        cancellation_event = threading.Event()
        self._task_cancellations[job.id] = cancellation_event
        if task_status is not None:
            self._update_work_status(start_update, status="running")
        else:
            self._send_step_update(job.chat_id, start_update)
        completed_status = "completed"
        try:
            if task_result_has_pull_request(job.result):
                reply = job.result
            elif not self._action_allowed():
                reply = _action_lock_message()
                completed_status = "failed"
            else:
                reply = self._run_direct_work(job.chat_id, job.text, context=job.context, session_key=session_key)
            if _work_reply_failed(reply):
                completed_status = "failed"
        except BrainCancelled as error:
            reply = str(error)
            completed_status = "cancelled"
        except Exception as error:
            reply = f"{failure_prefix}: {error}"
            completed_status = "failed"
        finally:
            _CURRENT_WORK_STATUS.reset(token)
            _CURRENT_TASK_ID.reset(task_token)
            self._task_cancellations.pop(job.id, None)
            if completed_status == "cancelled":
                cancel_running_task(self.root, result=reply)
                cancel_evolve_candidate_for_task(job, self.root)
            elif completed_status == "failed":
                fail_task(job.id, self.root, result=reply)
                fail_evolve_candidate_for_task(job, self.root)
            else:
                complete_task(job.id, self.root, result=reply)
                complete_evolve_candidate_for_task(job, self.root)
        completed_job = _history_task(job.id, self.root)
        summary_job = completed_job or job
        if completed_status == "completed":
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
            self._work_status_messages.pop(job.id, None)
        self._safe_send_message(
            job.chat_id,
            _format_task_final_message(summary_job, completed_status, reply),
        )
        self._record_turn(job.chat_id, f"{command} {job.text}", reply)
        if command == "/do":
            self._maybe_start_task_worker()

    def _current_task_cancellation_event(self) -> threading.Event | None:
        task_id = _CURRENT_TASK_ID.get()
        if task_id is None:
            return None
        return self._task_cancellations.get(task_id)

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
            return "\n\n".join(part for part in [visible, memory_note, _action_lock_message()] if part)

        edit_result = self._run_direct_work(
            chat_id,
            edit_request.request,
            session_key=f"telegram:{chat_id}",
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

    def _update(self) -> str:
        if not self._action_allowed():
            return _action_lock_message()
        result = update_from_main(self.root)
        if result.direct_action_result:
            _record_direct_action("update from main", result.direct_action_result, self.root)
        if result.restart_required:
            self._restart_after_reply = True
        return result.message

    def _ensure_action_branch(self, request_text: str) -> str | None:
        branch = current_branch(self.root)
        ensure_clean_worktree(self.root)
        if branch not in PROTECTED_BRANCHES:
            switch_branch(DEFAULT_BRANCH, self.root)
        _ensure_local_main_current(self.root)
        branch_name = _branch_name(request_text)
        create_branch(branch_name, self.root)
        if branch in PROTECTED_BRANCHES:
            return f"Enoch created and switched to branch {branch_name} before editing."
        return f"Enoch switched from {branch} to main, then created branch {branch_name} before editing."

    def _send_progress(self, chat_id: int, elapsed_seconds: int, sandbox: str) -> None:
        mode = _sandbox_description(sandbox)
        if self._update_work_status(f"Still working after {_format_elapsed(elapsed_seconds)}: {mode}."):
            return
        self._safe_send_message(chat_id, f"Enoch is still working after {_format_elapsed(elapsed_seconds)}: {mode}.")

    def _action_allowed(self) -> bool:
        return self.client.config.allowed_chat_id is not None and _action_mode(self.root) == ACTION_MODE_FULL_ACCESS

    def _mode(self, text: str) -> str:
        if self.client.config.allowed_chat_id is None:
            return _action_lock_message()
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            return _mode_status(self.root)
        choice = parts[1].strip().lower()
        if choice == "chat":
            next_mode = ACTION_MODE_CONVERSATION
        elif choice == "work":
            next_mode = ACTION_MODE_FULL_ACCESS
        else:
            return _mode_usage(self.root)
        _save_action_mode(next_mode, self.root)
        return "\n".join(
            [
                f"Enoch mode: {_mode_name(next_mode)}.",
                _action_mode_description(next_mode),
            ]
        )

    def _shutdown_from_telegram(self) -> str:
        if self.client.config.allowed_chat_id is None:
            return "\n".join(
                [
                    "Enoch will not shut down from Telegram unless Telegram is locked to one chat.",
                    "Run `bin/enoch setup-chat <chat_id>` locally, then restart Enoch.",
                ]
            )
        self._stop_after_reply = True
        return "\n".join(
            [
                "Enoch is closing.",
                "Daemon mode will stop after this reply is delivered.",
            ]
        )

    def _restart_from_telegram(self) -> str:
        if self.client.config.allowed_chat_id is None:
            return "\n".join(
                [
                    "Enoch will not restart from Telegram unless Telegram is locked to one chat.",
                    "Run `bin/enoch setup-chat <chat_id>` locally, then restart Enoch.",
                ]
            )
        self._restart_after_reply = True
        return "\n".join(
            [
                "Enoch is restarting.",
                "Daemon mode will restart after this reply is delivered.",
            ]
        )

    def _record_turn(self, chat_id: int, text: str, reply: str) -> None:
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


def main() -> None:
    root = Path.cwd()
    identity = load_identity()
    try:
        config = load_config(root=root)
    except TelegramError as error:
        print(str(error))
        raise SystemExit(1) from error
    previous_shutdown_warning = _begin_lifecycle_run(root)
    _record_system_event(
        "startup",
        root,
        details={
            "identity": identity.name,
            "previous_shutdown_warning": previous_shutdown_warning,
        },
    )
    bot = EnochTelegramBot(
        identity=identity,
        root=root,
        client=TelegramClient(config),
        previous_shutdown_warning=previous_shutdown_warning,
    )
    _install_shutdown_handlers()
    print(f"{identity.name} is listening on Telegram.")
    try:
        if config.allowed_chat_id is None:
            print(
                "Telegram chat lock is not set; all chats with the bot token are accepted. "
                "Send /status, then run `bin/enoch setup-chat <chat_id>` locally."
            )
        else:
            try:
                bot.notify_startup()
            except (OSError, TelegramError) as error:
                print(f"Enoch could not send Telegram startup notification: {error}")
        bot.run_forever()
    except ShutdownRequested as shutdown:
        _notify_shutdown(bot, shutdown.reason)
        print(f"\n{identity.name} is shutting down: {shutdown.reason}.")
    except KeyboardInterrupt:
        _notify_shutdown(bot, "keyboard interrupt")
        print(f"\n{identity.name} stopped listening on Telegram.")


def _notify_shutdown(bot: EnochTelegramBot, reason: str) -> None:
    sent = bot.client.config.allowed_chat_id is not None
    try:
        bot.notify_shutdown(reason)
    except (OSError, TelegramError) as error:
        sent = False
        print(f"Enoch could not send Telegram shutdown notification: {error}")
    _record_lifecycle_shutdown(bot.root, reason, shutdown_notification_sent=sent)


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


def _parse_telegram_command(text: str) -> tuple[str, str]:
    first, _separator, rest = text.strip().partition(" ")
    if not first.startswith("/"):
        return "", text.strip()
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def _action_sandbox(root: Path) -> str:
    if _action_mode(root) == ACTION_MODE_CONVERSATION:
        return ACTION_SANDBOX_READ_ONLY
    return ACTION_SANDBOX_FULL_ACCESS


def _mode_status(root: Path) -> str:
    mode = _action_mode(root)
    return "\n".join(
        [
            f"Enoch mode: {_mode_name(mode)}.",
            _action_mode_description(mode),
            "",
            "Use /mode chat or /mode work.",
        ]
    )


def _mode_usage(root: Path) -> str:
    return "\n".join(
        [
            "Use /mode chat or /mode work.",
            "",
            _mode_status(root),
        ]
    )


def _mode_name(mode: str) -> str:
    if mode == ACTION_MODE_CONVERSATION:
        return "chat"
    return "work"


def _sandbox_description(sandbox: str) -> str:
    if sandbox == WORKSPACE_WRITE_SANDBOX:
        return "editing her code body"
    if sandbox == ACTION_SANDBOX_FULL_ACCESS:
        return "working with full filesystem access"
    return "thinking in read-only mode"


def _format_elapsed(elapsed_seconds: int) -> str:
    if elapsed_seconds < 60:
        return f"{elapsed_seconds} second(s)"
    minutes = elapsed_seconds // 60
    seconds = elapsed_seconds % 60
    if seconds == 0:
        return f"{minutes} minute(s)"
    return f"{minutes} minute(s) {seconds} second(s)"


def _worktree_snapshot(root: Path) -> str:
    try:
        return diff_summary(root)
    except GitError:
        return ""


def _changed_files_or_empty(root: Path) -> tuple[str, ...]:
    try:
        return tuple(changed_files(root))
    except GitError:
        return ()


def _delete_local_branch_if_enabled(branch: str, root: Path) -> str:
    if not _cleanup_local_branches(root):
        return ""
    if not branch or branch == DEFAULT_BRANCH:
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


def _with_replied_message_context(text: str, message: dict[str, Any]) -> str:
    command, argument = _parse_telegram_command(text)
    if command not in {"/do", "/task", "/backlog", "/cron"} or not argument:
        return text
    first_word = argument.split(maxsplit=1)[0].lower()
    if command == "/task" and first_word == "cancel":
        return text
    if command == "/backlog" and first_word in {"remove", "priority", "promote"}:
        return text
    if command == "/cron" and first_word == "cancel":
        return text
    reply_text = _replied_message_text(message)
    if not reply_text:
        return text
    return "\n\n".join(
        [
            f"{command} {argument}",
            "Context from replied Telegram message:",
            reply_text,
        ]
    )


def _task_context_snapshot_prompt(request: str) -> str:
    return "\n".join(
        [
            "Task context snapshot request:",
            "The human just created this Enoch work request:",
            request.strip(),
            "",
            "Using only prior conversation context from this same Telegram session, write a concrete task brief for the worker.",
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


def _recover_running_task_from_direct_action_log(root: Path) -> None:
    running = task_queue_status(root).running
    if running is None:
        return
    result = _latest_direct_action_result_for_task(running, root)
    if not result:
        return
    if _work_reply_failed(result):
        fail_task(running.id, root, result=result)
        fail_evolve_candidate_for_task(running, root)
    else:
        complete_task(running.id, root, result=result)
        complete_evolve_candidate_for_task(running, root)


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


def _task_status_message(root: Path) -> str:
    status = task_queue_status(root)
    backlog = backlog_status(root)
    cron = cron_status(root)
    lines = ["Tasks:"]
    if status.running is None:
        lines.append("- running: none")
    else:
        lines.append(f"- running: #{status.running.id} {_clip_activity_text(status.running.text, limit=80)}")
    lines.append(f"- queued: {status.pending_count}")
    lines.append(f"- backlog: {backlog.pending_count}")
    lines.append(f"- cron: {cron.active_count}")
    return "\n".join(lines)


def _format_tasks_report(root: Path) -> str:
    status = task_queue_status(root)
    backlog = backlog_status(root)
    cron = cron_status(root)
    lines = ["Tasks:"]
    if status.running is None:
        lines.append("Running: none")
    else:
        lines.append(f"Running: {_format_task_list_item(status.running)}")

    lines.append("")
    lines.append("Queued:")
    if status.pending:
        lines.extend(f"- {_format_task_list_item(job)}" for job in status.pending)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Recent history:")
    if status.history:
        lines.extend(f"- {_format_task_list_item(job)}" for job in status.history[-10:])
    else:
        lines.append("- none")
    lines.append("")
    lines.append(f"Backlog: {backlog.pending_count}")
    lines.append(f"Cron: {cron.active_count}")
    return "\n".join(lines)


def _format_task_list_item(job: TaskJob) -> str:
    item = f"#{job.id} [{job.status}] {_clip_activity_text(job.text, limit=120)}"
    if not job.pr_urls:
        return item
    label = "PR" if len(job.pr_urls) == 1 else "PRs"
    return f"{item} ({label}: {', '.join(job.pr_urls)})"


def _format_backlog_report(root: Path) -> str:
    status = backlog_status(root)
    lines = ["Backlog:"]
    lines.append("")
    lines.append("Pending:")
    if status.pending:
        lines.extend(f"- {_format_backlog_list_item(item)}" for item in status.pending)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Recent history:")
    if status.history:
        lines.extend(f"- {_format_backlog_list_item(item)}" for item in status.history[-10:])
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_backlog_list_item(item: BacklogItem) -> str:
    label = f"#{item.id} [{item.priority} {item.status}] {_clip_activity_text(item.text, limit=120)}"
    if item.promoted_task_id is None:
        return label
    return f"{label} (task #{item.promoted_task_id})"


def _format_cron_report(root: Path) -> str:
    status = cron_status(root)
    lines = ["Cron:"]
    lines.append("")
    lines.append("Active:")
    if status.active:
        lines.extend(f"- {_format_cron_list_item(job)}" for job in status.active)
    else:
        lines.append("- none")

    lines.append("")
    lines.append("Recent history:")
    if status.history:
        lines.extend(f"- {_format_cron_list_item(job)}" for job in status.history[-10:])
    else:
        lines.append("- none")
    return "\n".join(lines)


def _format_cron_list_item(job: CronJob) -> str:
    label = (
        f"#{job.id} [{job.status}] every {format_cron_interval(job.interval_seconds)} "
        f"next {job.next_run_at} {_clip_activity_text(job.text, limit=100)}"
    )
    if job.last_task_id is None:
        return label
    return f"{label} (last task #{job.last_task_id})"


def _format_evolve_report(report: EvolveReport) -> str:
    state = report.state
    lines = [
        "Evolve:",
        f"Mode: {state.mode}",
        f"Theme: {state.theme or 'not set'}",
        f"Schedule: {_format_evolve_schedule(state)}",
        "",
        "Candidate counts:",
    ]
    if report.counts_by_source:
        for source in sorted(report.counts_by_source):
            lines.append(f"- {source}: {report.counts_by_source[source]}")
    else:
        lines.append("- none")
    lines.extend(["", "Top candidate:"])
    if report.top_candidate is None:
        lines.append("- none")
    else:
        lines.extend(_format_evolve_candidate(report.top_candidate))
    lines.extend(["", f"Next action: {_evolve_next_action(report)}"])
    return "\n".join(lines)


def _format_evolve_schedule(state: EvolveState) -> str:
    if not state.schedule_enabled or state.schedule_interval_seconds <= 0:
        return "off"
    next_run = state.schedule_next_run_at or "unknown"
    last_run = f"; last {state.schedule_last_run_at}" if state.schedule_last_run_at else ""
    if state.schedule_daily_time:
        return f"daily {state.schedule_daily_time}; next {next_run}{last_run}"
    if state.schedule_cron_expression:
        return f"cron {state.schedule_cron_expression}; next {next_run}{last_run}"
    return f"every {format_cron_interval(state.schedule_interval_seconds)}; next {next_run}{last_run}"


def _format_evolve_candidate(candidate: EvolveCandidate) -> list[str]:
    return [
        f"- {candidate.id} [{candidate.status} {candidate.source}] {_clip_activity_text(candidate.title, limit=100)}",
        f"  Score: {candidate.score}",
        f"  Rationale: {_clip_activity_text(candidate.rationale, limit=180)}",
        f"  Proposed change: {_clip_activity_text(candidate.proposed_change, limit=180)}",
        f"  Test plan: {_clip_activity_text(candidate.test_plan, limit=180)}",
    ]


def _format_evolve_candidates(candidates: tuple[EvolveCandidate, ...], *, include_inactive: bool = False) -> str:
    title = "Evolve candidates"
    if include_inactive:
        title += " (all)"
    lines = [f"{title}:"]
    if not candidates:
        lines.append("- none")
        return "\n".join(lines)
    for candidate in candidates[:10]:
        lines.extend(_format_evolve_candidate(candidate))
    if len(candidates) > 10:
        lines.append(f"- {len(candidates) - 10} more")
    return "\n".join(lines)


def _evolve_next_action(report: EvolveReport) -> str:
    if report.state.mode == MODE_DISABLED:
        return "disabled; Enoch will not collect or rank self-evolution candidates."
    if report.top_candidate is None:
        return "no candidate yet."
    if report.state.mode == MODE_AUTO_EVOLVE:
        return "select this bounded candidate, then queue or run work only after guardrails pass."
    return "propose this candidate and wait for human approval before changing code."


def _evolve_task_request(candidate: EvolveCandidate, theme: str) -> str:
    lines = [
        f"Evolve selected candidate {candidate.id}: {candidate.title}",
        "",
        f"Source: {candidate.source}",
        f"Theme: {theme or 'not set'}",
        f"Proposed change: {candidate.proposed_change}",
        f"Expected benefit: {candidate.expected_benefit}",
        f"Risk: {candidate.risk}",
        f"Test plan: {candidate.test_plan}",
        "",
        "Keep the change small, reversible, and covered by focused tests. Open a PR for human review; do not merge it.",
    ]
    return "\n".join(lines)


def _evolve_task_context(candidate: EvolveCandidate) -> str:
    return "\n".join(
        [
            "Scheduled evolve candidate context:",
            f"ID: {candidate.id}",
            f"Source: {candidate.source}",
            f"Score: {candidate.score}",
            f"Rationale: {candidate.rationale}",
            f"Proposed change: {candidate.proposed_change}",
            f"Expected benefit: {candidate.expected_benefit}",
            f"Risk: {candidate.risk}",
            f"Test plan: {candidate.test_plan}",
        ]
    )


def _history_task(task_id: int, root: Path) -> TaskJob | None:
    for job in reversed(task_queue_status(root).history):
        if job.id == task_id:
            return job
    return None


def _task_result_text(job: TaskJob, result: str) -> str:
    return job.result or result or "No result summary was recorded."


def _final_task_status_update(final_status: str) -> str:
    if final_status == "failed":
        return "Failed. Final summary sent below."
    if final_status == "cancelled":
        return "Cancelled. Final summary sent below."
    return "Completed. Final summary sent below."


def _format_task_final_message(job: TaskJob, final_status: str, result: str) -> str:
    prs = job.pr_urls or ("none",)
    return "\n".join(
        [
            f"Task #{job.id} final update",
            f"Final status: {final_status}",
            "PR URL:",
            *[f"- {pr}" for pr in prs],
            "Result summary:",
            _clip_activity_block(_task_result_text(job, result), limit=1200),
        ]
    )


def _format_work_status_message(status: WorkStatusMessage) -> str:
    elapsed = _format_elapsed(max(0, int(time.monotonic() - status.started_at)))
    prs = status.prs or ["none"]
    title = f"Task #{status.task_id}" if status.task_id is not None else "Work status"
    lines = [
        title,
        f"Status: {status.status}",
        f"Time: {elapsed}",
        f"Latest update: {status.latest_update}",
        "PRs created:",
        *[f"- {pr}" for pr in prs],
        "",
        "Request:",
        _clip_activity_text(status.request, limit=1200),
    ]
    if status.context:
        lines.extend(
            [
                "",
                "Conversation context snapshot:",
                _clip_activity_text(status.context, limit=1200),
            ]
        )
    return "\n".join(lines)


def _task_cancel_id(argument: str) -> int | None:
    parts = argument.split()
    if len(parts) != 2 or parts[0].lower() != "cancel":
        return None
    try:
        task_id = int(parts[1].lstrip("#"))
    except ValueError:
        return None
    return task_id if task_id > 0 else None


def _backlog_usage() -> str:
    return "\n".join(
        [
            "Use /backlog [p0|p1|p2] <request> to save deferred work.",
            "Use /backlog remove <id> to remove a pending backlog item.",
            "Use /backlog priority <id> p0|p1|p2 to reprioritize a pending backlog item.",
            "Use /backlog promote <id> to move a pending backlog item into the active task queue.",
        ]
    )


def _cron_usage() -> str:
    return "\n".join(
        [
            "Use /cron every <interval> <request> to schedule recurring work.",
            "Intervals can be like 10m, 2h, or 1d.",
            "Use /cron cancel <id> to cancel a scheduled job.",
            "Use /cron to show scheduled jobs.",
        ]
    )


def _evolve_usage() -> str:
    return "\n".join(
        [
            "Use /evolve to show Enoch's self-evolution status.",
            "Use /evolve mode <mode> to set self-evolution behavior.",
            "Modes: disabled, co-evolve, auto-evolve.",
            "Use /evolve theme <text> to set the current evolution theme.",
            "Use /evolve brainstorm to generate bounded candidates under the current theme.",
            "Use /evolve explore <agent> to discover skills from a non-parent agent.",
            "Use /evolve candidates to show current candidates.",
            "Use /evolve select <id> to select a candidate.",
            "Use /evolve run <id> to queue a candidate as a task.",
            "Use /evolve reject <id> to reject a candidate.",
            "Use /evolve schedule <text> to let Enoch interpret common schedule text.",
        ]
    )


def _unquote_schedule_text(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1].strip()
    return stripped


def _backlog_priority_and_request(argument: str) -> tuple[str, str]:
    first, _separator, rest = argument.partition(" ")
    lowered = first.lower()
    if lowered in {"p0", "p1", "p2"}:
        return normalize_priority(lowered), rest.strip()
    if lowered.startswith("p") and lowered[:2] not in {"p0", "p1", "p2"} and lowered[1:].isdigit():
        raise ValueError("Backlog priority must be p0, p1, or p2.")
    return "p1", argument.strip()


def _backlog_item_id(argument: str) -> int | None:
    value = argument.strip().split(maxsplit=1)[0] if argument.strip() else ""
    try:
        item_id = int(value.lstrip("#"))
    except ValueError:
        return None
    return item_id if item_id > 0 else None


def _cron_job_id(argument: str) -> int | None:
    value = argument.strip().split(maxsplit=1)[0] if argument.strip() else ""
    try:
        job_id = int(value.lstrip("#"))
    except ValueError:
        return None
    return job_id if job_id > 0 else None


def _backlog_priority_update(argument: str) -> tuple[int | None, str | None]:
    parts = argument.split()
    if len(parts) != 2:
        return None, None
    item_id = _backlog_item_id(parts[0])
    return item_id, parts[1].lower()


def _github_maintenance_request(text: str) -> GithubMaintenanceRequest | None:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return None
    lowered = normalized.lower()
    numbers = _pr_numbers(normalized)
    if not numbers:
        return None

    dedup_words = ("dedup", "duplicate", "duplicates", "重复", "重复的")
    close_words = ("close", "关闭", "关掉")
    if any(word in lowered for word in dedup_words):
        keep_number = _keep_pr_number(normalized) or numbers[0]
        close_numbers = tuple(number for number in numbers if number != keep_number)
        return GithubMaintenanceRequest(close_numbers=_unique_numbers(close_numbers), keep_number=keep_number)
    if any(word in lowered for word in close_words):
        keep_number = _keep_pr_number(normalized)
        close_numbers = tuple(number for number in numbers if number != keep_number)
        return GithubMaintenanceRequest(close_numbers=_unique_numbers(close_numbers), keep_number=keep_number)
    return None


def _pr_numbers(text: str) -> tuple[int, ...]:
    return _unique_numbers(int(match) for match in re.findall(r"#(\d+)", text))


def _keep_pr_number(text: str) -> int | None:
    patterns = [
        r"(?:keep|retain)\s+(?:pr\s*)?#(\d+)",
        r"(?:保留|留下)\s*#(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _unique_numbers(numbers) -> tuple[int, ...]:
    seen: set[int] = set()
    unique: list[int] = []
    for number in numbers:
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        unique.append(number)
    return tuple(unique)


def _duplicate_close_comment(keep_number: int | None) -> str:
    if keep_number is None:
        return "Closing this pull request from a Enoch maintenance job."
    return f"Closing as a duplicate of #{keep_number}. Keeping #{keep_number} as the canonical PR for this change."


def _format_pr_close_results(results: list[PullRequestCloseResult], keep_number: int | None) -> str:
    if not results:
        return "Enoch could not close any pull requests: no duplicate PR numbers were found."
    lines = ["Enoch updated GitHub pull requests."]
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
        return "Enoch could not complete every GitHub PR update.\n\n" + "\n".join(lines)
    return "\n".join(lines)


def _existing_branch_publish_request(text: str) -> str | None:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return None
    lowered = normalized.lower()
    if "publish" not in lowered or "branch" not in lowered or "pr" not in lowered:
        return None
    patterns = [
        r"existing local branch `([^`]+)`",
        r"existing branch `([^`]+)`",
        r"branch `([^`]+)`",
        r"existing local branch ([^\s:]+)",
        r"existing branch ([^\s:]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            branch = match.group(1).strip().strip("`")
            return branch if _looks_like_branch_name(branch) else None
    return None


def _looks_like_branch_name(value: str) -> bool:
    if not value or value.startswith("-") or ".." in value or value.endswith(".lock"):
        return False
    return bool(re.match(r"^[A-Za-z0-9._/-]+$", value))


def _load_task_status_messages(root: Path) -> dict[int, int]:
    status = task_queue_status(root)
    jobs = [*status.pending]
    if status.running is not None:
        jobs.append(status.running)
    return {job.id: job.status_message_id for job in jobs if job.status_message_id is not None}


def _sync_session_activity(identity: Identity, root: Path, chat_id: int, note: str) -> None:
    try:
        respond(
            identity,
            note,
            cwd=root,
            session_key=f"telegram:{chat_id}",
        )
    except BrainError:
        return


def _clip_activity_text(text: str, limit: int = 700) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 15].rstrip()} [truncated]"


def _clip_activity_block(text: str, limit: int = 700) -> str:
    lines = []
    previous_blank = False
    for raw_line in text.strip().splitlines():
        line = " ".join(raw_line.split())
        if not line:
            if lines and not previous_blank:
                lines.append("")
            previous_blank = True
            continue
        lines.append(line)
        previous_blank = False
    cleaned = "\n".join(lines).strip()
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[: limit - 15].rstrip()} [truncated]"


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
    return format_telegram_publish_result(result)


def _format_remote_publish_result(result: RemotePublishResult) -> str:
    return format_telegram_remote_publish_result(result)


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


def _branch_name(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = slug[:40].strip("-") or "telegram-code"
    return f"enoch/{int(time.time())}-{slug}"


if __name__ == "__main__":
    main()
