from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home
from enoch.providers.contracts import (
    ConversationId,
    MessageId,
    normalize_conversation_id,
    normalize_message_id,
)
from enoch.task_events import normalize_task_initiator, normalize_task_source, record_task_event


SCHEMA_VERSION = 5
DEFAULT_MAX_ATTEMPTS = 3
PULL_REQUEST_URL_PATTERN = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
_QUEUE_THREAD_LOCK = threading.RLock()


@dataclass(frozen=True)
class TaskJob:
    id: int
    chat_id: ConversationId
    text: str
    created_at: str
    started_at: str = ""
    completed_at: str = ""
    status: str = "pending"
    status_message_id: MessageId | None = None
    result: str = ""
    pr_urls: tuple[str, ...] = ()
    context: str = ""
    context_source: str = ""
    source: str = "task"
    initiated_by: str = "human"
    trigger: str = ""
    candidate_id: str = ""
    parent_task_id: int | None = None
    evidence_source: str = ""
    signal_actor: str = ""
    candidate_actor: str = ""
    approval_actor: str = ""
    parent_candidate_id: str = ""
    source_task_id: int | None = None
    worker_id: str = ""
    worker_pid: int | None = None
    worktree_path: str = ""
    branch_name: str = ""
    attempt: int = 0
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    next_attempt_at: str = ""
    failure_code: str = ""
    failure_class: str = ""
    retryable: bool = False


@dataclass(frozen=True)
class TaskQueueStatus:
    pending_count: int
    paused_count: int = 0
    running: TaskJob | None = None
    pending: tuple[TaskJob, ...] = ()
    paused: tuple[TaskJob, ...] = ()
    history: tuple[TaskJob, ...] = ()


class TaskRetryError(ValueError):
    pass


def task_queue_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "task_queue.json"


def enqueue_task(
    chat_id: ConversationId,
    text: str,
    root: Path | None = None,
    *,
    context: str = "",
    context_source: str = "",
    source: str = "task",
    initiated_by: str = "human",
    event_actor: str = "human",
    trigger: str = "/task",
    candidate_id: str = "",
    parent_task_id: int | None = None,
    evidence_source: str = "",
    signal_actor: str = "",
    candidate_actor: str = "",
    approval_actor: str = "",
    parent_candidate_id: str = "",
    source_task_id: int | None = None,
) -> TaskJob:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Task text is required.")
    source = normalize_task_source(source)
    initiated_by = normalize_task_initiator(initiated_by)
    with _queue_transaction(root):
        data = _load_queue(root)
        job = TaskJob(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            created_at=current_time(),
            context=context.strip(),
            context_source=context_source.strip(),
            source=source,
            initiated_by=initiated_by,
            trigger=trigger.strip(),
            candidate_id=candidate_id.strip(),
            parent_task_id=_positive_int(parent_task_id),
            evidence_source=evidence_source.strip().lower(),
            signal_actor=_normalize_provenance_actor(signal_actor),
            candidate_actor=_normalize_provenance_actor(candidate_actor),
            approval_actor=_normalize_provenance_actor(approval_actor),
            parent_candidate_id=parent_candidate_id.strip(),
            source_task_id=_positive_int(source_task_id),
        )
        pending = data.setdefault("pending", [])
        pending.append(_job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(job, "created", root, event_actor=event_actor, trigger=trigger)
        _record_task_event_safely(job, "queued", root, event_actor=event_actor, trigger=trigger)
        return job


def retry_failed_task(
    task_id: int,
    root: Path | None = None,
    *,
    reconciled_result: str = "",
    event_actor: str = "human",
    trigger: str = "/task retry",
) -> TaskJob:
    with _queue_transaction(root):
        data = _load_queue(root)
        original = next(
            (
                job
                for job in _history_jobs(data)
                if job.id == task_id and job.status == "failed"
            ),
            None,
        )
        if original is None:
            raise TaskRetryError(
                f"Task #{task_id} is not a failed task available for retry."
            )
        existing_retries = [
            job
            for job in [
                *_pending_jobs(data),
                *_paused_jobs(data),
                *(
                    [_parse_job(data.get("running"))]
                    if _parse_job(data.get("running")) is not None
                    else []
                ),
                *_history_jobs(data),
            ]
            if job.parent_task_id == original.id
            and job.status != "cancelled"
        ]
        if existing_retries:
            latest = max(existing_retries, key=lambda job: job.id)
            if latest.status == "failed":
                raise TaskRetryError(
                    f"Task #{original.id} was already retried as failed task "
                    f"#{latest.id}. Retry task #{latest.id} instead."
                )
            raise TaskRetryError(
                f"Task #{original.id} already has retry task #{latest.id} "
                f"in status {latest.status}."
            )
        artifact_result = reconciled_result.strip()
        job = TaskJob(
            id=_next_id(data),
            chat_id=original.chat_id,
            text=original.text,
            created_at=current_time(),
            context=original.context,
            context_source=original.context_source,
            source=original.source,
            initiated_by="human",
            trigger=trigger,
            candidate_id=original.candidate_id,
            parent_task_id=original.id,
            evidence_source=original.evidence_source,
            signal_actor=original.signal_actor,
            candidate_actor=original.candidate_actor,
            approval_actor="human" if original.candidate_id else "",
            parent_candidate_id=original.parent_candidate_id,
            source_task_id=original.source_task_id,
            result=artifact_result,
            pr_urls=_pull_request_urls(artifact_result),
            worktree_path=original.worktree_path,
            branch_name=original.branch_name,
        )
        pending = data.setdefault("pending", [])
        pending.append(_job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(
            job,
            "created",
            root,
            event_actor=event_actor,
            trigger=trigger,
        )
        _record_task_event_safely(
            job,
            "queued",
            root,
            event_actor=event_actor,
            trigger=trigger,
        )
        return job


def enqueue_task_front(
    chat_id: ConversationId,
    text: str,
    root: Path | None = None,
    *,
    context: str = "",
    context_source: str = "",
    source: str = "chat-task",
    initiated_by: str = "human",
    event_actor: str = "human",
    trigger: str = "/do",
    candidate_id: str = "",
    parent_task_id: int | None = None,
    evidence_source: str = "",
    signal_actor: str = "",
    candidate_actor: str = "",
    approval_actor: str = "",
    parent_candidate_id: str = "",
    source_task_id: int | None = None,
) -> TaskJob:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Task text is required.")
    source = normalize_task_source(source)
    initiated_by = normalize_task_initiator(initiated_by)
    with _queue_transaction(root):
        data = _load_queue(root)
        job = TaskJob(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            created_at=current_time(),
            context=context.strip(),
            context_source=context_source.strip(),
            source=source,
            initiated_by=initiated_by,
            trigger=trigger.strip(),
            candidate_id=candidate_id.strip(),
            parent_task_id=_positive_int(parent_task_id),
            evidence_source=evidence_source.strip().lower(),
            signal_actor=_normalize_provenance_actor(signal_actor),
            candidate_actor=_normalize_provenance_actor(candidate_actor),
            approval_actor=_normalize_provenance_actor(approval_actor),
            parent_candidate_id=parent_candidate_id.strip(),
            source_task_id=_positive_int(source_task_id),
        )
        pending = data.setdefault("pending", [])
        pending.insert(0, _job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(job, "created", root, event_actor=event_actor, trigger=trigger)
        _record_task_event_safely(job, "queued", root, event_actor=event_actor, trigger=trigger)
        return job


def begin_direct_task(
    chat_id: ConversationId,
    text: str,
    root: Path | None = None,
    *,
    context: str = "",
    context_source: str = "",
    source: str = "chat-task",
    initiated_by: str = "human",
    event_actor: str = "human",
    trigger: str = "/do",
    candidate_id: str = "",
    parent_task_id: int | None = None,
    evidence_source: str = "",
    signal_actor: str = "",
    candidate_actor: str = "",
    approval_actor: str = "",
    parent_candidate_id: str = "",
    source_task_id: int | None = None,
) -> TaskJob:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Task text is required.")
    source = normalize_task_source(source)
    initiated_by = normalize_task_initiator(initiated_by)
    with _queue_transaction(root):
        data = _load_queue(root)
        if _parse_job(data.get("running")) is not None:
            raise RuntimeError("A task is already running.")
        job = TaskJob(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            created_at=current_time(),
            started_at=current_time(),
            status="running",
            context=context.strip(),
            context_source=context_source.strip(),
            source=source,
            initiated_by=initiated_by,
            trigger=trigger.strip(),
            candidate_id=candidate_id.strip(),
            parent_task_id=_positive_int(parent_task_id),
            evidence_source=evidence_source.strip().lower(),
            signal_actor=_normalize_provenance_actor(signal_actor),
            candidate_actor=_normalize_provenance_actor(candidate_actor),
            approval_actor=_normalize_provenance_actor(approval_actor),
            parent_candidate_id=parent_candidate_id.strip(),
            source_task_id=_positive_int(source_task_id),
            attempt=1,
        )
        data["running"] = _job_to_dict(job)
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(job, "created", root, event_actor=event_actor, trigger=trigger)
        _record_task_event_safely(job, "started", root, event_actor="system", trigger="task-runner")
        return job


def record_task_status_message(
    task_id: int,
    message_id: MessageId,
    root: Path | None = None,
) -> None:
    with _queue_transaction(root):
        data = _load_queue(root)
        pending = []
        changed = False
        for job in _pending_jobs(data):
            if job.id == task_id:
                job = _replace_job(job, status_message_id=message_id)
                changed = True
            pending.append(_job_to_dict(job))
        paused = []
        for job in _paused_jobs(data):
            if job.id == task_id:
                job = _replace_job(job, status_message_id=message_id)
                changed = True
            paused.append(_job_to_dict(job))
        running = _parse_job(data.get("running"))
        if running is not None and running.id == task_id:
            running = _replace_job(running, status_message_id=message_id)
            changed = True
        if not changed:
            return
        data["pending"] = pending
        data["paused"] = paused
        data["running"] = _job_to_dict(running) if running is not None else None
        _write_queue(data, root)


def begin_next_task(root: Path | None = None) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        if _parse_job(data.get("running")) is not None:
            return None
        pending = _pending_jobs(data)
        if not pending:
            return None
        ready_index = next(
            (index for index, candidate in enumerate(pending) if _task_is_due(candidate)),
            None,
        )
        if ready_index is None:
            return None
        job = pending[ready_index]
        running = _replace_job(
            job,
            started_at=current_time(),
            completed_at="",
            status="running",
            attempt=job.attempt + 1,
            next_attempt_at="",
            failure_code="",
            failure_class="",
            retryable=False,
        )
        data["pending"] = [
            _job_to_dict(item)
            for index, item in enumerate(pending)
            if index != ready_index
        ]
        data["running"] = _job_to_dict(running)
        _write_queue(data, root)
        _record_task_event_safely(running, "started", root, event_actor="system", trigger="task-runner")
        return running


def complete_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "agent",
    trigger: str = "task-runner",
    worker_id: str = "",
) -> TaskJob | None:
    return _finish_running_task(
        task_id,
        "completed",
        root,
        result=result,
        event_actor=event_actor,
        trigger=trigger,
        worker_id=worker_id,
    )


def fail_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "agent",
    trigger: str = "task-runner",
    worker_id: str = "",
    failure_code: str = "",
    failure_class: str = "",
    retryable: bool = False,
) -> TaskJob | None:
    return _finish_running_task(
        task_id,
        "failed",
        root,
        result=result,
        event_actor=event_actor,
        trigger=trigger,
        worker_id=worker_id,
        failure_code=failure_code,
        failure_class=failure_class,
        retryable=retryable,
    )


def retry_running_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    failure_code: str,
    failure_class: str,
    worker_id: str = "",
    delay_seconds: int = 0,
    event_actor: str = "agent",
    trigger: str = "task-runner",
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if (
            running is None
            or running.id != task_id
            or (worker_id and running.worker_id != worker_id)
            or running.attempt >= running.max_attempts
        ):
            return None
        retry_at = _retry_at(delay_seconds)
        recovered = _replace_job(
            running,
            status="pending",
            started_at="",
            completed_at="",
            result=result,
            pr_urls=_merge_pr_urls(running.pr_urls, _pull_request_urls(result)),
            worker_id="",
            worker_pid=None,
            next_attempt_at=retry_at,
            failure_code=failure_code.strip(),
            failure_class=failure_class.strip(),
            retryable=True,
        )
        data["running"] = None
        data["pending"] = [
            _job_to_dict(recovered),
            *[_job_to_dict(job) for job in _pending_jobs(data)],
        ]
        _write_queue(data, root)
        _record_task_event_safely(
            recovered,
            "retrying",
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
        )
        _record_task_event_safely(
            recovered,
            "queued",
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
        )
        return recovered


def claim_running_task(
    task_id: int,
    worker_id: str,
    worker_pid: int,
    root: Path | None = None,
) -> TaskJob | None:
    cleaned_worker_id = worker_id.strip()
    if not cleaned_worker_id or worker_pid <= 0:
        raise ValueError("A worker id and process id are required.")
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if running is None or running.id != task_id:
            return None
        if (
            running.worker_id
            and running.worker_id != cleaned_worker_id
            and task_worker_is_active(running)
        ):
            return None
        claimed = _replace_job(
            running,
            worker_id=cleaned_worker_id,
            worker_pid=worker_pid,
        )
        data["running"] = _job_to_dict(claimed)
        _write_queue(data, root)
        return claimed


def record_task_worktree(
    task_id: int,
    worker_id: str,
    worktree_path: Path,
    branch_name: str,
    root: Path | None = None,
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if (
            running is None
            or running.id != task_id
            or not worker_id
            or running.worker_id != worker_id
        ):
            return None
        updated = _replace_job(
            running,
            worktree_path=str(worktree_path.resolve()),
            branch_name=branch_name.strip(),
        )
        data["running"] = _job_to_dict(updated)
        _write_queue(data, root)
        return updated


def task_worker_is_active(job: TaskJob) -> bool:
    if not job.worker_id or job.worker_pid is None:
        return False
    try:
        os.kill(job.worker_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def pause_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "system",
    trigger: str = "codex-unavailable",
    worker_id: str = "",
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        pending = _pending_jobs(data)
        running = _parse_job(data.get("running"))
        paused_job: TaskJob | None = None
        kept: list[TaskJob] = []
        for job in pending:
            if job.id == task_id and paused_job is None:
                paused_job = job
            else:
                kept.append(job)
        if running is not None and running.id == task_id:
            if worker_id and running.worker_id != worker_id:
                return None
            paused_job = running
            running = None
        if paused_job is None:
            return None
        paused_job = _replace_job(
            paused_job,
            status="paused",
            completed_at="",
            result=result or paused_job.result,
            worker_id="",
            worker_pid=None,
        )
        paused = [job for job in _paused_jobs(data) if job.id != task_id]
        paused.append(paused_job)
        data["pending"] = [_job_to_dict(job) for job in kept]
        data["running"] = _job_to_dict(running) if running is not None else None
        data["paused"] = [_job_to_dict(job) for job in paused]
        _write_queue(data, root)
        _record_task_event_safely(
            paused_job,
            "paused",
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
        )
        return paused_job


def resume_paused_tasks(
    root: Path | None = None,
    *,
    task_id: int | None = None,
    event_actor: str = "human",
    trigger: str = "/resume",
) -> tuple[TaskJob, ...]:
    with _queue_transaction(root):
        data = _load_queue(root)
        paused = _paused_jobs(data)
        if not paused:
            return ()
        selected = [
            job
            for job in paused
            if task_id is None or job.id == task_id
        ]
        if not selected:
            return ()
        selected_ids = {job.id for job in selected}
        resumed = tuple(
            _replace_job(
                job,
                status="pending",
                started_at="",
                completed_at="",
                worker_id="",
                worker_pid=None,
            )
            for job in selected
        )
        data["paused"] = [
            _job_to_dict(job)
            for job in paused
            if job.id not in selected_ids
        ]
        data["pending"] = [
            *[_job_to_dict(job) for job in resumed],
            *[_job_to_dict(job) for job in _pending_jobs(data)],
        ]
        _write_queue(data, root)
        for job in resumed:
            _record_task_event_safely(
                job,
                "resumed",
                root,
                event_actor=event_actor,
                trigger=trigger,
                result="Resumed after Codex access was restored.",
            )
        return resumed


def regress_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "agent",
    trigger: str = "agent-regression-signal",
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        history = _history_jobs(data)
        regressed: TaskJob | None = None
        updated: list[TaskJob] = []
        for job in history:
            if job.id == task_id and regressed is None:
                if job.status != "completed":
                    return None
                regressed = _replace_job(
                    job,
                    status="regressed",
                    completed_at=current_time(),
                    result=result or job.result,
                )
                updated.append(regressed)
            else:
                updated.append(job)
        if regressed is None:
            return None
        data["history"] = [_job_to_dict(job) for job in updated[-20:]]
        _write_queue(data, root)
        _record_task_event_safely(
            regressed,
            "regressed",
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
        )
        return regressed


def resolve_regressed_task(
    task_id: int,
    resolution: str,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "agent",
    trigger: str = "agent-regression-signal",
    related_task_id: int | None = None,
) -> TaskJob | None:
    normalized_resolution = resolution.strip().lower()
    if normalized_resolution not in {"reverted", "forward-fixed"}:
        raise ValueError("Regression resolution must be reverted or forward-fixed.")
    normalized_related_task_id = _positive_int(related_task_id)
    with _queue_transaction(root):
        data = _load_queue(root)
        if normalized_resolution == "forward-fixed":
            related = _find_task(data, normalized_related_task_id)
            if (
                related is None
                or related.id == task_id
                or related.status != "completed"
            ):
                return None
        history = _history_jobs(data)
        resolved: TaskJob | None = None
        updated: list[TaskJob] = []
        for job in history:
            if job.id == task_id and resolved is None:
                if job.status != "regressed":
                    return None
                resolved = _replace_job(
                    job,
                    status=normalized_resolution,
                    completed_at=current_time(),
                    result=result or job.result,
                )
                updated.append(resolved)
            else:
                updated.append(job)
        if resolved is None:
            return None
        data["history"] = [_job_to_dict(job) for job in updated[-20:]]
        _write_queue(data, root)
        _record_task_event_safely(
            resolved,
            normalized_resolution,
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
            related_task_id=normalized_related_task_id,
        )
        return resolved


def revert_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "human",
    trigger: str = "revert",
    related_task_id: int | None = None,
) -> TaskJob | None:
    current = _find_task_in_status(task_id, root)
    if current is None:
        return None
    if current.status == "completed":
        current = regress_task(
            task_id,
            root,
            result=result,
            event_actor=event_actor,
            trigger=trigger,
        )
    if current is None or current.status != "regressed":
        return None
    return resolve_regressed_task(
        task_id,
        "reverted",
        root,
        result=result,
        event_actor=event_actor,
        trigger=trigger,
        related_task_id=related_task_id,
    )


def record_task_result(task_id: int, result: str, root: Path | None = None) -> None:
    with _queue_transaction(root):
        data = _load_queue(root)
        pending = []
        changed = False
        for job in _pending_jobs(data):
            if job.id == task_id:
                job = _replace_job(
                    job,
                    result=result,
                    pr_urls=_merge_pr_urls(job.pr_urls, _pull_request_urls(result)),
                )
                changed = True
            pending.append(_job_to_dict(job))
        running = _parse_job(data.get("running"))
        if running is not None and running.id == task_id:
            running = _replace_job(
                running,
                result=result,
                pr_urls=_merge_pr_urls(running.pr_urls, _pull_request_urls(result)),
            )
            changed = True
        if not changed:
            return
        data["pending"] = pending
        data["running"] = _job_to_dict(running) if running is not None else None
        _write_queue(data, root)


def _finish_running_task(
    task_id: int,
    status: str,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str,
    trigger: str,
    worker_id: str = "",
    failure_code: str = "",
    failure_class: str = "",
    retryable: bool = False,
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if running is None or running.id != task_id:
            return None
        if worker_id and running.worker_id != worker_id:
            return None
        history = _history_jobs(data)
        finished = _replace_job(
            running,
            status=status,
            completed_at=current_time(),
            result=result,
            pr_urls=_merge_pr_urls(running.pr_urls, _pull_request_urls(result)),
            worker_id="",
            worker_pid=None,
            failure_code=failure_code.strip(),
            failure_class=failure_class.strip(),
            retryable=retryable,
            next_attempt_at="",
        )
        history.append(finished)
        data["running"] = None
        data["history"] = [_job_to_dict(job) for job in history[-20:]]
        _write_queue(data, root)
        _record_task_event_safely(
            finished,
            status,
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
        )
        return finished


def cancel_task(
    task_id: int,
    root: Path | None = None,
    *,
    event_actor: str = "human",
    trigger: str = "/task cancel",
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        pending = _pending_jobs(data)
        kept: list[TaskJob] = []
        cancelled: TaskJob | None = None
        for job in pending:
            if job.id == task_id and cancelled is None:
                cancelled = _replace_job(job, status="cancelled", completed_at=current_time())
            else:
                kept.append(job)
        paused_kept: list[TaskJob] = []
        for job in _paused_jobs(data):
            if job.id == task_id and cancelled is None:
                cancelled = _replace_job(job, status="cancelled", completed_at=current_time())
            else:
                paused_kept.append(job)
        if cancelled is None:
            return None
        history = _history_jobs(data)
        history.append(cancelled)
        data["pending"] = [_job_to_dict(job) for job in kept]
        data["paused"] = [_job_to_dict(job) for job in paused_kept]
        data["history"] = [_job_to_dict(job) for job in history[-20:]]
        _write_queue(data, root)
        _record_task_event_safely(
            cancelled,
            "cancelled",
            root,
            event_actor=event_actor,
            trigger=trigger,
        )
        return cancelled


def cancel_running_task(
    root: Path | None = None,
    result: str = "Stopped by /stop.",
    *,
    event_actor: str = "human",
    trigger: str = "/stop",
    expected_task_id: int | None = None,
    worker_id: str = "",
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if running is None:
            return None
        if expected_task_id is not None and running.id != expected_task_id:
            return None
        if worker_id and running.worker_id != worker_id:
            return None
        cancelled = _replace_job(
            running,
            status="cancelled",
            completed_at=current_time(),
            result=result,
            pr_urls=_merge_pr_urls(running.pr_urls, _pull_request_urls(result)),
            worker_id="",
            worker_pid=None,
        )
        history = _history_jobs(data)
        history.append(cancelled)
        data["running"] = None
        data["history"] = [_job_to_dict(job) for job in history[-20:]]
        _write_queue(data, root)
        _record_task_event_safely(
            cancelled,
            "cancelled",
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
        )
        return cancelled


def recover_interrupted_task(root: Path | None = None) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if running is None:
            return None
        if task_worker_is_active(running):
            return None
        if _job_has_pull_request(running):
            completed = _replace_job(
                running,
                status="completed",
                completed_at=current_time(),
                worker_id="",
                worker_pid=None,
            )
            history = _history_jobs(data)
            history.append(completed)
            data["running"] = None
            data["history"] = [_job_to_dict(job) for job in history[-20:]]
            _write_queue(data, root)
            _record_task_event_safely(
                completed,
                "completed",
                root,
                event_actor="system",
                trigger="recovery",
                result=completed.result,
            )
            return completed
        if running.attempt >= running.max_attempts:
            failed = _replace_job(
                running,
                status="failed",
                completed_at=current_time(),
                result=(
                    f"Task worker was interrupted and automatic recovery exhausted "
                    f"{running.max_attempts} attempts."
                ),
                worker_id="",
                worker_pid=None,
                failure_code="worker_interrupted",
                failure_class="transient",
                retryable=False,
                next_attempt_at="",
            )
            history = _history_jobs(data)
            history.append(failed)
            data["running"] = None
            data["history"] = [_job_to_dict(job) for job in history[-20:]]
            _write_queue(data, root)
            _record_task_event_safely(
                failed,
                "failed",
                root,
                event_actor="system",
                trigger="recovery-exhausted",
                result=failed.result,
            )
            return failed
        recovered = _replace_job(
            running,
            status="pending",
            started_at="",
            worker_id="",
            worker_pid=None,
            next_attempt_at=_retry_at(0),
            failure_code="worker_interrupted",
            failure_class="transient",
            retryable=True,
        )
        data["running"] = None
        data["pending"] = [_job_to_dict(recovered), *[_job_to_dict(job) for job in _pending_jobs(data)]]
        _write_queue(data, root)
        _record_task_event_safely(
            recovered,
            "retrying",
            root,
            event_actor="system",
            trigger="recovery",
            result="Task worker was interrupted.",
        )
        _record_task_event_safely(
            recovered,
            "queued",
            root,
            event_actor="system",
            trigger="recovery",
            result="Task worker was interrupted.",
        )
        return recovered


def task_queue_status(root: Path | None = None) -> TaskQueueStatus:
    with _queue_transaction(root):
        data = _load_queue(root)
        pending = tuple(_pending_jobs(data))
        paused = tuple(_paused_jobs(data))
        return TaskQueueStatus(
            pending_count=len(pending),
            paused_count=len(paused),
            running=_parse_job(data.get("running")),
            pending=pending,
            paused=paused,
            history=tuple(_history_jobs(data)),
        )


def task_result_has_pull_request(result: str) -> bool:
    return bool(PULL_REQUEST_URL_PATTERN.search(result))


def _job_has_pull_request(job: TaskJob) -> bool:
    return bool(job.pr_urls) or task_result_has_pull_request(job.result)


def _load_queue(root: Path | None = None) -> dict:
    path = task_queue_path(root)
    if not path.exists():
        return _empty_queue()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_queue()
    if not isinstance(raw, dict):
        return _empty_queue()
    pending = [_job_to_dict(job) for job in _pending_jobs(raw)]
    paused_jobs = _paused_jobs(raw)
    paused = [_job_to_dict(job) for job in paused_jobs]
    running = _parse_job(raw.get("running"))
    next_id = _int(raw.get("next_id"), default=1)
    max_id = max([job["id"] for job in pending], default=0)
    if running is not None:
        max_id = max(max_id, running.id)
    if paused_jobs:
        max_id = max(max_id, max(job.id for job in paused_jobs))
    history_jobs = _history_jobs(raw)
    history = [_job_to_dict(job) for job in history_jobs]
    if history_jobs:
        max_id = max(max_id, max(job.id for job in history_jobs))
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": max(next_id, max_id + 1),
        "pending": pending,
        "paused": paused,
        "running": _job_to_dict(running) if running is not None else None,
        "history": history[-20:],
    }


def _write_queue(data: dict, root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "next_id": _next_id(data),
        "pending": [_job_to_dict(job) for job in _pending_jobs(data)],
        "paused": [_job_to_dict(job) for job in _paused_jobs(data)],
        "running": _job_to_dict(_parse_job(data.get("running"))) if _parse_job(data.get("running")) else None,
        "history": [_job_to_dict(job) for job in _history_jobs(data)][-20:],
    }
    atomic_write(task_queue_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


@contextmanager
def _queue_transaction(root: Path | None = None):
    path = task_queue_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _QUEUE_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _pending_jobs(data: dict) -> list[TaskJob]:
    raw = data.get("pending")
    if not isinstance(raw, list):
        return []
    jobs = [_parse_job(item) for item in raw]
    return [job for job in jobs if job is not None]


def _paused_jobs(data: dict) -> list[TaskJob]:
    raw = data.get("paused")
    if not isinstance(raw, list):
        return []
    jobs = [_parse_job(item) for item in raw]
    return [job for job in jobs if job is not None]


def _history_jobs(data: dict) -> list[TaskJob]:
    raw = data.get("history")
    if not isinstance(raw, list):
        return []
    jobs = [_parse_job(item) for item in raw]
    return [job for job in jobs if job is not None]


def _find_task(data: dict, task_id: int | None) -> TaskJob | None:
    if task_id is None:
        return None
    running = _parse_job(data.get("running"))
    for job in [
        *(_pending_jobs(data)),
        *(_paused_jobs(data)),
        *([running] if running is not None else []),
        *(_history_jobs(data)),
    ]:
        if job.id == task_id:
            return job
    return None


def _find_task_in_status(task_id: int, root: Path | None = None) -> TaskJob | None:
    with _queue_transaction(root):
        return _find_task(_load_queue(root), task_id)


def _parse_job(raw: object) -> TaskJob | None:
    if not isinstance(raw, dict):
        return None
    task_id = _int(raw.get("id"))
    chat_id = normalize_conversation_id(raw.get("chat_id"))
    text = str(raw.get("text") or "").strip()
    created_at = str(raw.get("created_at") or "").strip()
    started_at = str(raw.get("started_at") or "").strip()
    completed_at = str(raw.get("completed_at") or "").strip()
    status = str(raw.get("status") or "").strip() or "pending"
    status_message_id = normalize_message_id(raw.get("status_message_id"))
    result = str(raw.get("result") or "").strip()
    pr_urls = _parse_pr_urls(raw.get("pr_urls"))
    pr_urls = _merge_pr_urls(pr_urls, _pull_request_urls(result))
    context = str(raw.get("context") or "").strip()
    context_source = str(raw.get("context_source") or "").strip()
    try:
        source = normalize_task_source(str(raw.get("source") or "task"))
        initiated_by = normalize_task_initiator(str(raw.get("initiated_by") or "human"))
    except ValueError:
        source = "task"
        initiated_by = "human"
    trigger = str(raw.get("trigger") or "").strip()
    candidate_id = str(raw.get("candidate_id") or "").strip()
    parent_task_id = _positive_int(raw.get("parent_task_id"))
    evidence_source = str(raw.get("evidence_source") or "").strip().lower()
    signal_actor = _normalize_provenance_actor(str(raw.get("signal_actor") or ""))
    candidate_actor = _normalize_provenance_actor(str(raw.get("candidate_actor") or ""))
    approval_actor = _normalize_provenance_actor(str(raw.get("approval_actor") or ""))
    parent_candidate_id = str(raw.get("parent_candidate_id") or "").strip()
    source_task_id = _positive_int(raw.get("source_task_id"))
    worker_id = str(raw.get("worker_id") or "").strip()
    worker_pid = _optional_int(raw.get("worker_pid"))
    worktree_path = str(raw.get("worktree_path") or "").strip()
    branch_name = str(raw.get("branch_name") or "").strip()
    attempt_default = 1 if status in {"running", "paused"} else 0
    attempt = max(0, _int(raw.get("attempt"), default=attempt_default))
    max_attempts = max(1, _int(raw.get("max_attempts"), default=DEFAULT_MAX_ATTEMPTS))
    next_attempt_at = str(raw.get("next_attempt_at") or "").strip()
    failure_code = str(raw.get("failure_code") or "").strip()
    failure_class = str(raw.get("failure_class") or "").strip()
    retryable = bool(raw.get("retryable", False))
    if candidate_id:
        evidence_source = evidence_source or source
        signal_actor = signal_actor or _legacy_signal_actor(evidence_source)
        candidate_actor = candidate_actor or "agent"
        approval_actor = approval_actor or _legacy_approval_actor(trigger, context_source, initiated_by)
    if task_id <= 0 or chat_id is None or not text:
        return None
    return TaskJob(
        id=task_id,
        chat_id=chat_id,
        text=text,
        created_at=created_at,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        status_message_id=status_message_id,
        result=result,
        pr_urls=pr_urls,
        context=context,
        context_source=context_source,
        source=source,
        initiated_by=initiated_by,
        trigger=trigger,
        candidate_id=candidate_id,
        parent_task_id=parent_task_id,
        evidence_source=evidence_source,
        signal_actor=signal_actor,
        candidate_actor=candidate_actor,
        approval_actor=approval_actor,
        parent_candidate_id=parent_candidate_id,
        source_task_id=source_task_id,
        worker_id=worker_id,
        worker_pid=worker_pid,
        worktree_path=worktree_path,
        branch_name=branch_name,
        attempt=attempt,
        max_attempts=max_attempts,
        next_attempt_at=next_attempt_at,
        failure_code=failure_code,
        failure_class=failure_class,
        retryable=retryable,
    )


def _job_to_dict(job: TaskJob | None) -> dict:
    if job is None:
        return {}
    return {
        "id": job.id,
        "chat_id": job.chat_id,
        "text": job.text,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "status": job.status,
        "status_message_id": job.status_message_id,
        "result": job.result,
        "pr_urls": list(job.pr_urls),
        "context": job.context,
        "context_source": job.context_source,
        "source": job.source,
        "initiated_by": job.initiated_by,
        "trigger": job.trigger,
        "candidate_id": job.candidate_id,
        "parent_task_id": job.parent_task_id,
        "evidence_source": job.evidence_source,
        "signal_actor": job.signal_actor,
        "candidate_actor": job.candidate_actor,
        "approval_actor": job.approval_actor,
        "parent_candidate_id": job.parent_candidate_id,
        "source_task_id": job.source_task_id,
        "worker_id": job.worker_id,
        "worker_pid": job.worker_pid,
        "worktree_path": job.worktree_path,
        "branch_name": job.branch_name,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "next_attempt_at": job.next_attempt_at,
        "failure_code": job.failure_code,
        "failure_class": job.failure_class,
        "retryable": job.retryable,
    }


def _replace_job(job: TaskJob, **changes: object) -> TaskJob:
    values = {
        "id": job.id,
        "chat_id": job.chat_id,
        "text": job.text,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "status": job.status,
        "status_message_id": job.status_message_id,
        "result": job.result,
        "pr_urls": job.pr_urls,
        "context": job.context,
        "context_source": job.context_source,
        "source": job.source,
        "initiated_by": job.initiated_by,
        "trigger": job.trigger,
        "candidate_id": job.candidate_id,
        "parent_task_id": job.parent_task_id,
        "evidence_source": job.evidence_source,
        "signal_actor": job.signal_actor,
        "candidate_actor": job.candidate_actor,
        "approval_actor": job.approval_actor,
        "parent_candidate_id": job.parent_candidate_id,
        "source_task_id": job.source_task_id,
        "worker_id": job.worker_id,
        "worker_pid": job.worker_pid,
        "worktree_path": job.worktree_path,
        "branch_name": job.branch_name,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "next_attempt_at": job.next_attempt_at,
        "failure_code": job.failure_code,
        "failure_class": job.failure_class,
        "retryable": job.retryable,
    }
    values.update(changes)
    return TaskJob(**values)


def _next_id(data: dict) -> int:
    return max(1, _int(data.get("next_id"), default=1))


def _int(value: object, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed


def _optional_int(value: object) -> int | None:
    parsed = _int(value)
    return parsed if parsed > 0 else None


def _normalize_provenance_actor(value: str) -> str:
    normalized = " ".join(value.split()).lower()
    return normalized if normalized in {"human", "agent", "system"} else ""


def _legacy_signal_actor(source: str) -> str:
    if source in {"backlog", "feedback", "learning"}:
        return "human"
    if source in {"inheritance", "brainstorming"}:
        return "agent"
    return "system"


def _legacy_approval_actor(trigger: str, context_source: str, initiated_by: str) -> str:
    if trigger.startswith("/evolve ") or context_source in {"evolve-approve", "evolve-retry"}:
        return "human"
    if context_source == "evolve-scheduler":
        return "agent"
    return initiated_by


def _positive_int(value: object) -> int | None:
    parsed = _int(value)
    return parsed if parsed > 0 else None


def _retry_at(delay_seconds: int) -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        + timedelta(seconds=max(0, delay_seconds))
    ).isoformat()


def _task_is_due(job: TaskJob) -> bool:
    if not job.next_attempt_at:
        return True
    try:
        due = datetime.fromisoformat(job.next_attempt_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= datetime.now(timezone.utc)


def _pull_request_urls(result: str) -> tuple[str, ...]:
    return tuple(PULL_REQUEST_URL_PATTERN.findall(result))


def _record_task_event_safely(
    job: TaskJob,
    event: str,
    root: Path | None,
    *,
    event_actor: str,
    trigger: str,
    result: str = "",
    related_task_id: int | None = None,
) -> None:
    try:
        record_task_event(
            job,
            event,
            root,
            event_actor=event_actor,
            trigger=trigger,
            result=result,
            related_task_id=related_task_id,
        )
    except (OSError, ValueError):
        return


def _parse_pr_urls(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    urls = []
    for item in raw:
        url = str(item or "").strip()
        if PULL_REQUEST_URL_PATTERN.fullmatch(url):
            urls.append(url)
    return tuple(urls)


def _merge_pr_urls(*groups: tuple[str, ...]) -> tuple[str, ...]:
    merged: list[str] = []
    for group in groups:
        for url in group:
            if url not in merged:
                merged.append(url)
    return tuple(merged)


def _empty_queue() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": 1,
        "pending": [],
        "paused": [],
        "running": None,
        "history": [],
    }
