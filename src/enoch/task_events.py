from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import threading
from typing import Protocol
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - fcntl is unavailable on Windows.
    fcntl = None

from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 3
SUMMARY_LIMIT = 4000
TASK_SOURCES = {
    "backlog",
    "feedback",
    "experience",
    "inheritance",
    "learning",
    "brainstorming",
    "task",
    "chat-task",
}
TASK_INITIATORS = {"human", "agent"}
TASK_EVENT_ACTORS = {"human", "agent", "system"}
TASK_EVENT_TYPES = {
    "created",
    "queued",
    "retrying",
    "started",
    "completed",
    "failed",
    "cancelled",
    "paused",
    "resumed",
    "regressed",
    "reverted",
    "forward-fixed",
}
_TASK_EVENT_THREAD_LOCK = threading.RLock()


class TaskLike(Protocol):
    id: int
    text: str
    created_at: str
    started_at: str
    completed_at: str
    result: str
    pr_urls: tuple[str, ...]
    context_source: str
    source: str
    initiated_by: str
    trigger: str
    candidate_id: str
    parent_task_id: int | None
    evidence_source: str
    signal_actor: str
    candidate_actor: str
    approval_actor: str
    parent_candidate_id: str
    source_task_id: int | None
    attempt: int
    max_attempts: int
    next_attempt_at: str
    failure_code: str
    failure_class: str
    retryable: bool


@dataclass(frozen=True)
class TaskEvent:
    id: str
    task_id: int
    occurred_at: str
    event: str
    source: str
    initiated_by: str
    event_actor: str
    trigger: str
    request: str
    result_summary: str = ""
    context_source: str = ""
    candidate_id: str = ""
    parent_task_id: int | None = None
    evidence_source: str = ""
    signal_actor: str = ""
    candidate_actor: str = ""
    approval_actor: str = ""
    parent_candidate_id: str = ""
    source_task_id: int | None = None
    related_task_id: int | None = None
    pr_urls: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    attempt: int = 0
    max_attempts: int = 3
    next_attempt_at: str = ""
    failure_code: str = ""
    failure_class: str = ""
    retryable: bool = False


def task_event_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "task_events.jsonl"


def record_task_event(
    job: TaskLike,
    event: str,
    root: Path | None = None,
    *,
    event_actor: str,
    trigger: str = "",
    result: str = "",
    related_task_id: int | None = None,
) -> TaskEvent:
    event = clean_text(event).lower()
    event_actor = clean_text(event_actor).lower()
    if event not in TASK_EVENT_TYPES:
        raise ValueError(f"Task event must be one of: {', '.join(sorted(TASK_EVENT_TYPES))}.")
    if event_actor not in TASK_EVENT_ACTORS:
        raise ValueError(f"Task event actor must be one of: {', '.join(sorted(TASK_EVENT_ACTORS))}.")
    task_id = _positive_int(getattr(job, "id", None))
    request = clean_text(str(getattr(job, "text", "") or ""))
    if task_id is None or not request:
        raise ValueError("Task events require a positive task id and request.")
    source = normalize_task_source(str(getattr(job, "source", "") or "task"))
    initiated_by = normalize_task_initiator(str(getattr(job, "initiated_by", "") or "human"))
    summary = _clip(result or str(getattr(job, "result", "") or ""))
    occurred_at = _event_time(job, event)
    task_event = TaskEvent(
        id=f"event-{uuid4().hex}",
        task_id=task_id,
        occurred_at=occurred_at,
        event=event,
        source=source,
        initiated_by=initiated_by,
        event_actor=event_actor,
        trigger=clean_text(trigger or str(getattr(job, "trigger", "") or "")),
        request=request,
        result_summary=summary,
        context_source=clean_text(str(getattr(job, "context_source", "") or "")),
        candidate_id=clean_text(str(getattr(job, "candidate_id", "") or "")),
        parent_task_id=_positive_int(getattr(job, "parent_task_id", None)),
        evidence_source=clean_text(str(getattr(job, "evidence_source", "") or "")).lower(),
        signal_actor=_provenance_actor(getattr(job, "signal_actor", "")),
        candidate_actor=_provenance_actor(getattr(job, "candidate_actor", "")),
        approval_actor=_provenance_actor(getattr(job, "approval_actor", "")),
        parent_candidate_id=clean_text(str(getattr(job, "parent_candidate_id", "") or "")),
        source_task_id=_positive_int(getattr(job, "source_task_id", None)),
        related_task_id=_positive_int(related_task_id),
        pr_urls=_dedupe(tuple(getattr(job, "pr_urls", ()) or ())),
        changed_files=_changed_files(summary),
        attempt=max(0, _int(getattr(job, "attempt", 0))),
        max_attempts=max(1, _int(getattr(job, "max_attempts", 3), default=3)),
        next_attempt_at=str(getattr(job, "next_attempt_at", "") or "").strip(),
        failure_code=clean_text(str(getattr(job, "failure_code", "") or "")).lower(),
        failure_class=clean_text(str(getattr(job, "failure_class", "") or "")).lower(),
        retryable=bool(getattr(job, "retryable", False)),
    )
    with _task_event_transaction(root):
        path = task_event_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"schema_version": SCHEMA_VERSION, **asdict(task_event)}, sort_keys=True) + "\n")
    return task_event


def load_task_events(
    root: Path | None = None,
    *,
    limit: int = 5000,
    task_id: int | None = None,
) -> tuple[TaskEvent, ...]:
    path = task_event_path(root)
    if not path.exists() or limit <= 0:
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    events: list[TaskEvent] = []
    for line in reversed(lines):
        event = _event_from_line(line)
        if event is None or (task_id is not None and event.task_id != task_id):
            continue
        events.append(event)
        if len(events) >= limit:
            break
    events.reverse()
    return tuple(events)


def normalize_task_source(value: str) -> str:
    normalized = clean_text(value).lower()
    if normalized not in TASK_SOURCES:
        raise ValueError(f"Task source must be one of: {', '.join(sorted(TASK_SOURCES))}.")
    return normalized


def normalize_task_initiator(value: str) -> str:
    normalized = clean_text(value).lower()
    if normalized not in TASK_INITIATORS:
        raise ValueError(f"Task initiator must be one of: {', '.join(sorted(TASK_INITIATORS))}.")
    return normalized


def _event_from_line(line: str) -> TaskEvent | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    task_id = _positive_int(raw.get("task_id"))
    event = clean_text(str(raw.get("event") or "")).lower()
    request = clean_text(str(raw.get("request") or ""))
    try:
        source = normalize_task_source(str(raw.get("source") or ""))
        initiated_by = normalize_task_initiator(str(raw.get("initiated_by") or ""))
    except ValueError:
        return None
    event_actor = clean_text(str(raw.get("event_actor") or "")).lower()
    if (
        task_id is None
        or event not in TASK_EVENT_TYPES
        or event_actor not in TASK_EVENT_ACTORS
        or not request
    ):
        return None
    trigger = clean_text(str(raw.get("trigger") or ""))
    context_source = clean_text(str(raw.get("context_source") or ""))
    candidate_id = clean_text(str(raw.get("candidate_id") or ""))
    evidence_source = (
        clean_text(str(raw.get("evidence_source") or source)).lower()
        if candidate_id
        else ""
    )
    signal_actor = _provenance_actor(raw.get("signal_actor"))
    candidate_actor = _provenance_actor(raw.get("candidate_actor"))
    approval_actor = _provenance_actor(raw.get("approval_actor"))
    if candidate_id:
        signal_actor = signal_actor or _legacy_signal_actor(evidence_source)
        candidate_actor = candidate_actor or "agent"
        approval_actor = approval_actor or _legacy_approval_actor(
            trigger,
            context_source,
            initiated_by,
        )
    return TaskEvent(
        id=clean_text(str(raw.get("id") or "")) or f"legacy-event-{task_id}-{event}",
        task_id=task_id,
        occurred_at=str(raw.get("occurred_at") or ""),
        event=event,
        source=source,
        initiated_by=initiated_by,
        event_actor=event_actor,
        trigger=trigger,
        request=request,
        result_summary=str(raw.get("result_summary") or "").strip(),
        context_source=context_source,
        candidate_id=candidate_id,
        parent_task_id=_positive_int(raw.get("parent_task_id")),
        evidence_source=evidence_source,
        signal_actor=signal_actor,
        candidate_actor=candidate_actor,
        approval_actor=approval_actor,
        parent_candidate_id=clean_text(str(raw.get("parent_candidate_id") or "")),
        source_task_id=_positive_int(raw.get("source_task_id")),
        related_task_id=_positive_int(raw.get("related_task_id")),
        pr_urls=_string_tuple(raw.get("pr_urls")),
        changed_files=_string_tuple(raw.get("changed_files")),
        attempt=max(0, _int(raw.get("attempt"))),
        max_attempts=max(1, _int(raw.get("max_attempts"), default=3)),
        next_attempt_at=str(raw.get("next_attempt_at") or "").strip(),
        failure_code=clean_text(str(raw.get("failure_code") or "")).lower(),
        failure_class=clean_text(str(raw.get("failure_class") or "")).lower(),
        retryable=raw.get("retryable") is True,
    )


def _event_time(job: TaskLike, event: str) -> str:
    if event == "created":
        value = str(getattr(job, "created_at", "") or "")
    elif event == "started":
        value = str(getattr(job, "started_at", "") or "")
    elif event in {"completed", "failed", "cancelled", "regressed", "reverted", "forward-fixed"}:
        value = str(getattr(job, "completed_at", "") or "")
    else:
        value = ""
    return value or current_time()


def _changed_files(result: str) -> tuple[str, ...]:
    files: list[str] = []
    in_files = False
    for raw_line in result.splitlines():
        line = raw_line.strip()
        if line == "Files:":
            in_files = True
            continue
        if in_files and line.startswith("- "):
            files.append(line[2:].strip())
            continue
        if in_files and line:
            in_files = False
    return _dedupe(tuple(files))


def _clip(text: str) -> str:
    cleaned = text.strip()
    if len(cleaned) <= SUMMARY_LIMIT:
        return cleaned
    return f"{cleaned[:SUMMARY_LIMIT].rstrip()}\n\n[truncated]"


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        output.append(cleaned)
    return tuple(output)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return _dedupe(tuple(str(item) for item in value))


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _provenance_actor(value: object) -> str:
    actor = clean_text(str(value or "")).lower()
    return actor if actor in TASK_EVENT_ACTORS else ""


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


@contextmanager
def _task_event_transaction(root: Path | None = None):
    path = task_event_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _TASK_EVENT_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
