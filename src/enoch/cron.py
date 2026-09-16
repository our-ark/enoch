from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import private_state_path
from enoch.providers.contracts import ConversationId, normalize_conversation_id
from enoch.schedules import (
    next_daily_run,
    next_interval_run,
    normalize_daily_time,
    normalize_timezone,
)
from enoch.state import StateCorruptionError, file_transaction, load_json_object


SCHEMA_VERSION = 5
_INTERVAL_PATTERN = re.compile(
    r"^\s*(?P<count>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CronJob:
    """An interval or local-calendar recurring task schedule.

    ``next_run_at`` is the next anchored target time. ``last_scheduled_at`` is
    the target represented by the most recently admitted occurrence, while
    ``last_run_at`` is when that occurrence was actually handed to the task
    queue. Missed target times are coalesced into one immediate occurrence.
    """

    id: int
    chat_id: ConversationId
    text: str
    interval_seconds: int
    created_at: str
    next_run_at: str
    last_scheduled_at: str = ""
    last_run_at: str = ""
    completed_at: str = ""
    status: str = "active"
    last_task_id: int | None = None
    context: str = ""
    context_source: str = ""
    claim_id: str = ""
    claimed_at: str = ""
    idempotency_key: str = ""
    cadence: str = "interval"
    daily_time: str = ""
    timezone: str = "UTC"
    claim_kind: str = ""
    claim_scheduled_for: str = ""
    run_now_id: str = ""
    run_now_key: str = ""
    run_now_history: tuple[str, ...] = ()
    paused_at: str = ""


@dataclass(frozen=True)
class CronStatus:
    """The active storage collection includes paused jobs, counted separately."""

    active_count: int
    active: tuple[CronJob, ...] = ()
    history: tuple[CronJob, ...] = ()
    paused_count: int = 0


def cron_path(root: Path | None = None) -> Path:
    return private_state_path("cron.json", root)


def parse_cron_interval(value: str) -> int:
    match = _INTERVAL_PATTERN.match(value)
    if match is None:
        raise ValueError("Cron interval must look like 10m, 2h, or 1d.")
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if count <= 0:
        raise ValueError("Cron interval must be greater than zero.")
    if unit.startswith("s"):
        multiplier = 1
    elif unit.startswith("m"):
        multiplier = 60
    elif unit.startswith("h"):
        multiplier = 60 * 60
    else:
        multiplier = 24 * 60 * 60
    return count * multiplier


def format_cron_interval(seconds: int) -> str:
    if seconds > 0 and seconds % (24 * 60 * 60) == 0:
        return f"{seconds // (24 * 60 * 60)}d"
    if seconds > 0 and seconds % (60 * 60) == 0:
        return f"{seconds // (60 * 60)}h"
    if seconds > 0 and seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def add_cron_job(
    chat_id: ConversationId,
    text: str,
    interval_seconds: int = 0,
    root: Path | None = None,
    *,
    context: str = "",
    context_source: str = "",
    now: datetime | None = None,
    idempotency_key: str = "",
    cadence: str = "interval",
    daily_time: str = "",
    timezone: str = "UTC",
) -> CronJob:
    chat_id = normalize_conversation_id(chat_id)
    if chat_id is None:
        raise ValueError("Cron requires a bound chat; no destination will be guessed.")
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Cron text is required.")
    if cadence not in {"interval", "daily"}:
        raise ValueError("Cron cadence must be interval or daily.")
    daily_time = normalize_daily_time(daily_time, label="Cron")
    timezone = normalize_timezone(timezone, label="Cron")
    if cadence == "interval" and interval_seconds <= 0:
        raise ValueError("Cron interval must be greater than zero.")
    if cadence == "daily" and not daily_time:
        raise ValueError("Cron daily time is required (HH:MM).")
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        normalized_key = idempotency_key.strip()
        if normalized_key:
            existing = next(
                (
                    job
                    for job in [*_active_jobs(data), *_history_jobs(data)]
                    if job.idempotency_key == normalized_key and job.chat_id == chat_id
                ),
                None,
            )
            if existing is not None:
                return existing
        job = CronJob(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            interval_seconds=interval_seconds if cadence == "interval" else 0,
            cadence=cadence,
            daily_time=daily_time if cadence == "daily" else "",
            timezone=timezone,
            created_at=_iso(current),
            next_run_at=_iso(
                next_daily_run(daily_time, timezone, current, label="Cron")
                if cadence == "daily"
                else next_interval_run(None, interval_seconds, current)
            ),
            context=context.strip(),
            context_source=context_source.strip(),
            idempotency_key=normalized_key,
        )
        active = data.setdefault("active", [])
        active.append(_job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_cron(data, root)
        return job


def cancel_cron_job(
    job_id: int, root: Path | None = None, *, chat_id: ConversationId | None = None,
) -> CronJob | None:
    with _cron_transaction(root):
        data = _load_cron(root)
        kept: list[CronJob] = []
        cancelled: CronJob | None = None
        for job in _active_jobs(data):
            if job.id == job_id and _in_chat(job, chat_id) and cancelled is None:
                cancelled = _replace_job(job, status="cancelled", completed_at=current_time())
            else:
                kept.append(job)
        if cancelled is None:
            return None
        history = _history_jobs(data)
        history.append(cancelled)
        data["active"] = [_job_to_dict(job) for job in kept]
        data["history"] = [_job_to_dict(job) for job in history]
        _write_cron(data, root)
        return cancelled


def find_cron_job(
    job_id: int, root: Path | None = None, *, chat_id: ConversationId | None = None,
) -> CronJob | None:
    status = cron_status(root, chat_id=chat_id)
    return next((job for job in (*status.active, *status.history) if job.id == job_id), None)


def pause_cron_job(
    job_id: int, root: Path | None = None, *, now: datetime | None = None,
    chat_id: ConversationId | None = None,
) -> CronJob | None:
    return _transition_cron_job(job_id, "paused", root, now=now, chat_id=chat_id)


def resume_cron_job(
    job_id: int, root: Path | None = None, *, now: datetime | None = None,
    chat_id: ConversationId | None = None,
) -> CronJob | None:
    return _transition_cron_job(job_id, "active", root, now=now, chat_id=chat_id)


def _transition_cron_job(
    job_id: int, target: str, root: Path | None, *, now: datetime | None,
    chat_id: ConversationId | None,
) -> CronJob | None:
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        jobs = _active_jobs(data)
        for index, job in enumerate(jobs):
            if job.id != job_id or not _in_chat(job, chat_id):
                continue
            if job.status == target:
                return job
            updated = _replace_job(
                job, status=target, paused_at=_iso(current) if target == "paused" else "",
                run_now_id="" if target == "paused" and not job.claim_id else job.run_now_id,
                run_now_key="" if target == "paused" and not job.claim_id else job.run_now_key,
            )
            jobs[index] = updated
            data["active"] = [_job_to_dict(item) for item in jobs]
            _write_cron(data, root)
            return updated
    return None


def request_cron_job_run(
    job_id: int, root: Path | None = None, *, now: datetime | None = None,
    idempotency_key: str = "", chat_id: ConversationId | None = None,
) -> CronJob | None:
    """Coalesce run-now requests, remembering the last 64 receipt keys."""
    key = _run_now_key(idempotency_key)
    with _cron_transaction(root):
        data = _load_cron(root)
        jobs = _active_jobs(data)
        for index, job in enumerate(jobs):
            if job.id != job_id or not _in_chat(job, chat_id):
                continue
            if job.status != "active":
                raise ValueError(f"Cron #{job.id} is {job.status}; resume it before run-now.")
            if key and key in job.run_now_history:
                return job
            history = (*job.run_now_history, key)[-64:] if key else job.run_now_history
            updated = _replace_job(
                job,
                run_now_id=job.run_now_id if job.claim_id or job.run_now_id else f"run-now-{uuid4().hex}",
                run_now_key=job.run_now_key if job.claim_id or job.run_now_id else key,
                run_now_history=history,
            )
            jobs[index] = updated
            data["active"] = [_job_to_dict(item) for item in jobs]
            _write_cron(data, root)
            return updated
    return None


def claim_due_cron_jobs(root: Path | None = None, *, now: datetime | None = None) -> tuple[CronJob, ...]:
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        claimed: list[CronJob] = []
        active: list[CronJob] = []
        for job in _active_jobs(data):
            if job.status != "active":
                active.append(job)
                continue
            if job.claim_id:
                claimed.append(job)
                active.append(job)
                continue
            next_run_at = _parse_time(job.next_run_at)
            scheduled_due = next_run_at is not None and next_run_at <= current
            if not scheduled_due and not job.run_now_id:
                active.append(job)
                continue
            claimed_job = _replace_job(
                job,
                claim_id=f"cron-{job.id}-{uuid4().hex}",
                claimed_at=_iso(current),
                claim_kind="scheduled" if scheduled_due else "run-now",
                claim_scheduled_for=job.next_run_at if scheduled_due else _iso(current),
            )
            claimed.append(claimed_job)
            active.append(claimed_job)
        if claimed:
            data["active"] = [_job_to_dict(job) for job in active]
            _write_cron(data, root)
        return tuple(claimed)


@contextmanager
def cron_task_admission(
    cron_id: int, claim_id: str, root: Path | None = None,
) -> Iterator[CronJob | None]:
    """Fence task admission against pause, cancellation, and stale claims.

    Keep the cron transaction held while checking outstanding work and making
    the task durable. A lifecycle change either wins before this check or waits
    for admission. Rejected claims remain untouched for resume/recovery.

    Queue operations must acquire their own locks after this cron lock. Release
    this guard before acknowledging with ``record_cron_task`` (cron transactions
    are not reentrant) or sending notifications. Acknowledgement can safely
    follow a pause because the task is already durable at that point.
    """
    with _cron_transaction(root):
        yield next(
            (
                job for job in _active_jobs(_load_cron(root))
                if job.id == cron_id and job.status == "active"
                and claim_id and job.claim_id == claim_id
            ),
            None,
        )


def record_cron_task(
    cron_id: int,
    task_id: int,
    root: Path | None = None,
    *,
    claim_id: str = "",
    now: datetime | None = None,
) -> CronJob | None:
    """Acknowledge one claimed occurrence after its task is durable.

    Interval schedules are fixed-rate rather than fixed-delay: the following
    target is calculated from the acknowledged occurrence's scheduled target,
    skipping missed slots until the first target strictly after ``now``. Daily
    jobs advance to the next local target; run-now leaves the target unchanged.
    """

    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        active: list[CronJob] = []
        changed: CronJob | None = None
        for job in _active_jobs(data):
            if job.id == cron_id and changed is None:
                if claim_id and job.claim_id != claim_id:
                    active.append(job)
                    continue
                changes: dict[str, object] = {"last_task_id": task_id}
                if job.claim_id and claim_id:
                    scheduled_for = _parse_time(job.claim_scheduled_for)
                    changes.update(
                        {
                            "last_scheduled_at": (
                                _iso(scheduled_for)
                                if scheduled_for is not None
                                else job.next_run_at
                            ),
                            "last_run_at": _iso(current),
                            "next_run_at": (
                                _iso(_next_run(job, current))
                                if job.claim_kind == "scheduled" else job.next_run_at
                            ),
                            "claim_id": "",
                            "claimed_at": "",
                            "claim_kind": "",
                            "claim_scheduled_for": "",
                            "run_now_id": "",
                            "run_now_key": "",
                        }
                    )
                job = _replace_job(job, **changes)
                changed = job
            active.append(job)
        if changed is None:
            return None
        data["active"] = [_job_to_dict(job) for job in active]
        _write_cron(data, root)
        return changed


def cron_status(
    root: Path | None = None, *, chat_id: ConversationId | None = None,
) -> CronStatus:
    with _cron_transaction(root):
        data = _load_cron(root)
        active = tuple(job for job in _active_jobs(data) if _in_chat(job, chat_id))
        return CronStatus(
            active_count=sum(job.status == "active" for job in active),
            paused_count=sum(job.status == "paused" for job in active),
            active=active,
            history=tuple(job for job in _history_jobs(data) if _in_chat(job, chat_id)),
        )


def cron_scheduler_wait_seconds(
    root: Path | None = None,
    *,
    now: datetime | None = None,
    max_wait_seconds: float = 5.0,
    claimed_retry_seconds: float = 1.0,
) -> float:
    """Return a bounded independent-scheduler sleep interval."""

    if max_wait_seconds <= 0 or claimed_retry_seconds <= 0:
        raise ValueError("Cron scheduler wait intervals must be greater than zero.")
    current = _coerce_utc(now) if now is not None else _utc_now()
    waits: list[float] = []
    for job in cron_status(root).active:
        if job.status != "active":
            continue
        if job.claim_id:
            waits.append(claimed_retry_seconds)
            continue
        if job.run_now_id:
            return 0.0
        next_run_at = _parse_time(job.next_run_at)
        if next_run_at is None:
            waits.append(claimed_retry_seconds)
            continue
        waits.append(max(0.0, (next_run_at - current).total_seconds()))
    if not waits:
        return max_wait_seconds
    return max(0.05, min(max_wait_seconds, *waits))


def _load_cron(root: Path | None = None) -> dict:
    path = cron_path(root)
    raw = load_json_object(path, default_factory=_empty_cron)
    version = raw.get("schema_version", 1)
    if type(version) is not int or not 1 <= version <= SCHEMA_VERSION:
        raise StateCorruptionError(path, "unsupported cron schema version")
    for key in ("active", "history"):
        if key in raw and not isinstance(raw[key], list):
            raise StateCorruptionError(path, f"expected {key} to be a list")
        if any(
            isinstance(item, dict) and normalize_conversation_id(item.get("chat_id")) is None
            for item in raw.get(key, [])
        ):
            raise StateCorruptionError(path, "cron job is missing its bound chat; no destination will be guessed")
        if any(_parse_job(item) is None for item in raw.get(key, [])):
            raise StateCorruptionError(path, f"found an invalid cron job in {key}")
    active_jobs = _active_jobs(raw)
    history_jobs = _history_jobs(raw)
    ids = [job.id for job in (*active_jobs, *history_jobs)]
    if len(set(ids)) != len(ids):
        raise StateCorruptionError(path, "duplicate cron job IDs")
    if "next_id" in raw and (type(raw["next_id"]) is not int or raw["next_id"] < 1):
        raise StateCorruptionError(path, "expected a positive integer next_id")
    next_id = _int(raw.get("next_id"), default=1)
    max_id = max([job.id for job in active_jobs], default=0)
    if history_jobs:
        max_id = max(max_id, max(job.id for job in history_jobs))
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": max(next_id, max_id + 1),
        "active": [_job_to_dict(job) for job in active_jobs],
        "history": [_job_to_dict(job) for job in history_jobs],
    }


def _write_cron(data: dict, root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "next_id": _next_id(data),
        "active": [_job_to_dict(job) for job in _active_jobs(data)],
        "history": [_job_to_dict(job) for job in _history_jobs(data)],
    }
    atomic_write(cron_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _cron_transaction(root: Path | None = None):
    return file_transaction(cron_path(root))


def _active_jobs(data: dict) -> list[CronJob]:
    raw = data.get("active")
    if not isinstance(raw, list):
        return []
    jobs = [_parse_job(job) for job in raw]
    return [job for job in jobs if job is not None]


def _history_jobs(data: dict) -> list[CronJob]:
    raw = data.get("history")
    if not isinstance(raw, list):
        return []
    jobs = [_parse_job(job) for job in raw]
    return [job for job in jobs if job is not None]


def _parse_job(raw: object) -> CronJob | None:
    if not isinstance(raw, dict):
        return None
    job_id = _int(raw.get("id"))
    chat_id = normalize_conversation_id(raw.get("chat_id"))
    text = str(raw.get("text") or "").strip()
    interval_seconds = _int(raw.get("interval_seconds"))
    created_at = str(raw.get("created_at") or "").strip()
    next_run_at = str(raw.get("next_run_at") or "").strip()
    last_scheduled_at = str(raw.get("last_scheduled_at") or "").strip()
    last_run_at = str(raw.get("last_run_at") or "").strip()
    completed_at = str(raw.get("completed_at") or "").strip()
    status = str(raw.get("status") or "").strip() or "active"
    last_task_id = _optional_int(raw.get("last_task_id"))
    context = str(raw.get("context") or "").strip()
    context_source = str(raw.get("context_source") or "").strip()
    claim_id = str(raw.get("claim_id") or "").strip()
    claimed_at = str(raw.get("claimed_at") or "").strip()
    idempotency_key = str(raw.get("idempotency_key") or "").strip()
    cadence = raw.get("cadence", "interval")
    try:
        daily_time = normalize_daily_time(raw.get("daily_time", ""), label="Cron")
        timezone_name = normalize_timezone(raw.get("timezone", "UTC"), label="Cron")
        run_now_key = _run_now_key(raw.get("run_now_key", ""))
        raw_history = raw.get("run_now_history", [])
        if not isinstance(raw_history, list):
            return None
        run_now_history = tuple(_run_now_key(key) for key in raw_history)
    except ValueError:
        return None
    if (
        job_id <= 0 or chat_id is None or not text
        or cadence not in ("interval", "daily")
        or (cadence == "interval" and interval_seconds <= 0)
        or (cadence == "daily" and (not daily_time or interval_seconds != 0))
        or status not in {"active", "paused", "cancelled"}
        or _parse_time(created_at) is None or _parse_time(next_run_at) is None
        or len(run_now_history) > 64 or any(not key for key in run_now_history)
        or len(set(run_now_history)) != len(run_now_history)
    ):
        return None
    # Schema <=4 claims represented scheduled occurrences without explicit kind.
    claim_kind = str(raw.get("claim_kind", "scheduled" if claim_id else ""))
    claim_scheduled_for = str(raw.get("claim_scheduled_for", next_run_at if claim_id else ""))
    if (
        claim_kind not in {"", "scheduled", "run-now"}
        or bool(claim_id) != bool(claim_kind)
        or (claim_id and (_parse_time(claimed_at) is None or _parse_time(claim_scheduled_for) is None))
    ):
        return None
    return CronJob(
        id=job_id,
        chat_id=chat_id,
        text=text,
        interval_seconds=interval_seconds,
        created_at=created_at,
        next_run_at=next_run_at,
        last_scheduled_at=last_scheduled_at,
        last_run_at=last_run_at,
        completed_at=completed_at,
        status=status,
        last_task_id=last_task_id,
        context=context,
        context_source=context_source,
        claim_id=claim_id,
        claimed_at=claimed_at,
        idempotency_key=idempotency_key,
        cadence=cadence,
        daily_time=daily_time,
        timezone=timezone_name,
        claim_kind=claim_kind,
        claim_scheduled_for=claim_scheduled_for,
        run_now_id=str(raw.get("run_now_id") or "").strip(),
        run_now_key=run_now_key,
        run_now_history=run_now_history,
        paused_at=str(raw.get("paused_at") or "").strip(),
    )


def _job_to_dict(job: CronJob | None) -> dict:
    if job is None:
        return {}
    values = asdict(job)
    values["run_now_history"] = list(job.run_now_history)
    return values


def _replace_job(job: CronJob, **changes: object) -> CronJob:
    return replace(job, **changes)


def _next_run(job: CronJob, current: datetime) -> datetime:
    if job.cadence == "daily":
        return next_daily_run(job.daily_time, job.timezone, current, label="Cron")
    return next_interval_run(_parse_time(job.claim_scheduled_for), job.interval_seconds, current)


def _in_chat(job: CronJob, chat_id: ConversationId | None) -> bool:
    return chat_id is None or job.chat_id == normalize_conversation_id(chat_id)


def _run_now_key(value: object) -> str:
    if not isinstance(value, str) or "\n" in value or len(value.strip()) > 256:
        raise ValueError("Cron run-now idempotency keys must be one line and 256 characters or fewer.")
    return value.strip()


def _parse_time(value: str) -> datetime | None:
    if not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _coerce_utc(parsed)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return _coerce_utc(value).isoformat()


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


def _empty_cron() -> dict:
    return {"schema_version": SCHEMA_VERSION, "next_id": 1, "active": [], "history": []}
