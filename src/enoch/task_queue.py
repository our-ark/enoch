from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home
from enoch.task_events import normalize_task_initiator, normalize_task_source, record_task_event


SCHEMA_VERSION = 2
PULL_REQUEST_URL_PATTERN = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")
_QUEUE_THREAD_LOCK = threading.RLock()


@dataclass(frozen=True)
class TaskJob:
    id: int
    chat_id: int
    text: str
    created_at: str
    started_at: str = ""
    completed_at: str = ""
    status: str = "pending"
    status_message_id: int | None = None
    result: str = ""
    pr_urls: tuple[str, ...] = ()
    context: str = ""
    context_source: str = ""
    source: str = "task"
    initiated_by: str = "human"
    trigger: str = ""
    candidate_id: str = ""
    parent_task_id: int | None = None


@dataclass(frozen=True)
class TaskQueueStatus:
    pending_count: int
    paused_count: int = 0
    running: TaskJob | None = None
    pending: tuple[TaskJob, ...] = ()
    paused: tuple[TaskJob, ...] = ()
    history: tuple[TaskJob, ...] = ()


def task_queue_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "task_queue.json"


def enqueue_task(
    chat_id: int,
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
        )
        pending = data.setdefault("pending", [])
        pending.append(_job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(job, "created", root, event_actor=event_actor, trigger=trigger)
        _record_task_event_safely(job, "queued", root, event_actor=event_actor, trigger=trigger)
        return job


def enqueue_task_front(
    chat_id: int,
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
        )
        pending = data.setdefault("pending", [])
        pending.insert(0, _job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(job, "created", root, event_actor=event_actor, trigger=trigger)
        _record_task_event_safely(job, "queued", root, event_actor=event_actor, trigger=trigger)
        return job


def begin_direct_task(
    chat_id: int,
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
        )
        data["running"] = _job_to_dict(job)
        data["next_id"] = job.id + 1
        _write_queue(data, root)
        _record_task_event_safely(job, "created", root, event_actor=event_actor, trigger=trigger)
        _record_task_event_safely(job, "started", root, event_actor="system", trigger="task-runner")
        return job


def record_task_status_message(task_id: int, message_id: int, root: Path | None = None) -> None:
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
        job = pending[0]
        running = TaskJob(
            id=job.id,
            chat_id=job.chat_id,
            text=job.text,
            created_at=job.created_at,
            started_at=current_time(),
            status="running",
            status_message_id=job.status_message_id,
            result=job.result,
            pr_urls=job.pr_urls,
            context=job.context,
            context_source=job.context_source,
            source=job.source,
            initiated_by=job.initiated_by,
            trigger=job.trigger,
            candidate_id=job.candidate_id,
            parent_task_id=job.parent_task_id,
        )
        data["pending"] = [_job_to_dict(item) for item in pending[1:]]
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
) -> None:
    _finish_running_task(
        task_id,
        "completed",
        root,
        result=result,
        event_actor=event_actor,
        trigger=trigger,
    )


def fail_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "agent",
    trigger: str = "task-runner",
) -> None:
    _finish_running_task(
        task_id,
        "failed",
        root,
        result=result,
        event_actor=event_actor,
        trigger=trigger,
    )


def pause_task(
    task_id: int,
    root: Path | None = None,
    result: str = "",
    *,
    event_actor: str = "system",
    trigger: str = "codex-unavailable",
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
            paused_job = running
            running = None
        if paused_job is None:
            return None
        paused_job = _replace_job(
            paused_job,
            status="paused",
            completed_at="",
            result=result or paused_job.result,
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
    event_actor: str = "human",
    trigger: str = "/resume",
) -> tuple[TaskJob, ...]:
    with _queue_transaction(root):
        data = _load_queue(root)
        paused = _paused_jobs(data)
        if not paused:
            return ()
        resumed = tuple(
            _replace_job(
                job,
                status="pending",
                started_at="",
                completed_at="",
            )
            for job in paused
        )
        data["paused"] = []
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
) -> None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if running is not None and running.id == task_id:
            history = _history_jobs(data)
            finished = _replace_job(
                running,
                status=status,
                completed_at=current_time(),
                result=result,
                pr_urls=_merge_pr_urls(running.pr_urls, _pull_request_urls(result)),
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
) -> TaskJob | None:
    with _queue_transaction(root):
        data = _load_queue(root)
        running = _parse_job(data.get("running"))
        if running is None:
            return None
        cancelled = _replace_job(
            running,
            status="cancelled",
            completed_at=current_time(),
            result=result,
            pr_urls=_merge_pr_urls(running.pr_urls, _pull_request_urls(result)),
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
        if _job_has_pull_request(running):
            completed = _replace_job(running, status="completed", completed_at=current_time())
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
        recovered = _replace_job(running, status="pending", started_at="")
        data["running"] = None
        data["pending"] = [_job_to_dict(recovered), *[_job_to_dict(job) for job in _pending_jobs(data)]]
        _write_queue(data, root)
        _record_task_event_safely(recovered, "queued", root, event_actor="system", trigger="recovery")
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
    chat_id = _int(raw.get("chat_id"))
    text = str(raw.get("text") or "").strip()
    created_at = str(raw.get("created_at") or "").strip()
    started_at = str(raw.get("started_at") or "").strip()
    completed_at = str(raw.get("completed_at") or "").strip()
    status = str(raw.get("status") or "").strip() or "pending"
    status_message_id = _optional_int(raw.get("status_message_id"))
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
    if task_id <= 0 or not isinstance(chat_id, int) or not text:
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


def _positive_int(value: object) -> int | None:
    parsed = _int(value)
    return parsed if parsed > 0 else None


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
