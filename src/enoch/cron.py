from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


SCHEMA_VERSION = 1
HISTORY_LIMIT = 20
_CRON_THREAD_LOCK = threading.RLock()
_INTERVAL_PATTERN = re.compile(
    r"^\s*(?P<count>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CronJob:
    id: int
    chat_id: int
    text: str
    interval_seconds: int
    created_at: str
    next_run_at: str
    last_run_at: str = ""
    completed_at: str = ""
    status: str = "active"
    last_task_id: int | None = None
    context: str = ""
    context_source: str = ""


@dataclass(frozen=True)
class CronStatus:
    active_count: int
    active: tuple[CronJob, ...] = ()
    history: tuple[CronJob, ...] = ()


def cron_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "cron.json"


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
    chat_id: int,
    text: str,
    interval_seconds: int,
    root: Path | None = None,
    *,
    context: str = "",
    context_source: str = "",
    now: datetime | None = None,
) -> CronJob:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Cron text is required.")
    if interval_seconds <= 0:
        raise ValueError("Cron interval must be greater than zero.")
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        job = CronJob(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            interval_seconds=interval_seconds,
            created_at=_iso(current),
            next_run_at=_iso(current + timedelta(seconds=interval_seconds)),
            context=context.strip(),
            context_source=context_source.strip(),
        )
        active = data.setdefault("active", [])
        active.append(_job_to_dict(job))
        data["next_id"] = job.id + 1
        _write_cron(data, root)
        return job


def cancel_cron_job(job_id: int, root: Path | None = None) -> CronJob | None:
    with _cron_transaction(root):
        data = _load_cron(root)
        kept: list[CronJob] = []
        cancelled: CronJob | None = None
        for job in _active_jobs(data):
            if job.id == job_id and cancelled is None:
                cancelled = _replace_job(job, status="cancelled", completed_at=current_time())
            else:
                kept.append(job)
        if cancelled is None:
            return None
        history = _history_jobs(data)
        history.append(cancelled)
        data["active"] = [_job_to_dict(job) for job in kept]
        data["history"] = [_job_to_dict(job) for job in history[-HISTORY_LIMIT:]]
        _write_cron(data, root)
        return cancelled


def claim_due_cron_jobs(root: Path | None = None, *, now: datetime | None = None) -> tuple[CronJob, ...]:
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        claimed: list[CronJob] = []
        active: list[CronJob] = []
        for job in _active_jobs(data):
            next_run_at = _parse_time(job.next_run_at)
            if next_run_at is None or next_run_at > current:
                active.append(job)
                continue
            claimed.append(job)
            active.append(
                _replace_job(
                    job,
                    last_run_at=_iso(current),
                    next_run_at=_iso(current + timedelta(seconds=job.interval_seconds)),
                )
            )
        if claimed:
            data["active"] = [_job_to_dict(job) for job in active]
            _write_cron(data, root)
        return tuple(claimed)


def record_cron_task(cron_id: int, task_id: int, root: Path | None = None) -> CronJob | None:
    with _cron_transaction(root):
        data = _load_cron(root)
        active: list[CronJob] = []
        changed: CronJob | None = None
        for job in _active_jobs(data):
            if job.id == cron_id and changed is None:
                job = _replace_job(job, last_task_id=task_id)
                changed = job
            active.append(job)
        if changed is None:
            return None
        data["active"] = [_job_to_dict(job) for job in active]
        _write_cron(data, root)
        return changed


def cron_status(root: Path | None = None) -> CronStatus:
    with _cron_transaction(root):
        data = _load_cron(root)
        active = tuple(_active_jobs(data))
        return CronStatus(
            active_count=len(active),
            active=active,
            history=tuple(_history_jobs(data)),
        )


def _load_cron(root: Path | None = None) -> dict:
    path = cron_path(root)
    if not path.exists():
        return _empty_cron()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_cron()
    if not isinstance(raw, dict):
        return _empty_cron()
    active_jobs = _active_jobs(raw)
    history_jobs = _history_jobs(raw)
    next_id = _int(raw.get("next_id"), default=1)
    max_id = max([job.id for job in active_jobs], default=0)
    if history_jobs:
        max_id = max(max_id, max(job.id for job in history_jobs))
    return {
        "schema_version": SCHEMA_VERSION,
        "next_id": max(next_id, max_id + 1),
        "active": [_job_to_dict(job) for job in active_jobs],
        "history": [_job_to_dict(job) for job in history_jobs[-HISTORY_LIMIT:]],
    }


def _write_cron(data: dict, root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "next_id": _next_id(data),
        "active": [_job_to_dict(job) for job in _active_jobs(data)],
        "history": [_job_to_dict(job) for job in _history_jobs(data)][-HISTORY_LIMIT:],
    }
    atomic_write(cron_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


@contextmanager
def _cron_transaction(root: Path | None = None):
    path = cron_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _CRON_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)


def _active_jobs(data: dict) -> list[CronJob]:
    raw = data.get("active")
    if not isinstance(raw, list):
        return []
    jobs = [_parse_job(job) for job in raw]
    return [job for job in jobs if job is not None and job.status == "active"]


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
    chat_id = _int(raw.get("chat_id"))
    text = str(raw.get("text") or "").strip()
    interval_seconds = _int(raw.get("interval_seconds"))
    created_at = str(raw.get("created_at") or "").strip()
    next_run_at = str(raw.get("next_run_at") or "").strip()
    last_run_at = str(raw.get("last_run_at") or "").strip()
    completed_at = str(raw.get("completed_at") or "").strip()
    status = str(raw.get("status") or "").strip() or "active"
    last_task_id = _optional_int(raw.get("last_task_id"))
    context = str(raw.get("context") or "").strip()
    context_source = str(raw.get("context_source") or "").strip()
    if job_id <= 0 or chat_id <= 0 or interval_seconds <= 0 or not text:
        return None
    return CronJob(
        id=job_id,
        chat_id=chat_id,
        text=text,
        interval_seconds=interval_seconds,
        created_at=created_at,
        next_run_at=next_run_at,
        last_run_at=last_run_at,
        completed_at=completed_at,
        status=status,
        last_task_id=last_task_id,
        context=context,
        context_source=context_source,
    )


def _job_to_dict(job: CronJob | None) -> dict:
    if job is None:
        return {}
    return {
        "id": job.id,
        "chat_id": job.chat_id,
        "text": job.text,
        "interval_seconds": job.interval_seconds,
        "created_at": job.created_at,
        "next_run_at": job.next_run_at,
        "last_run_at": job.last_run_at,
        "completed_at": job.completed_at,
        "status": job.status,
        "last_task_id": job.last_task_id,
        "context": job.context,
        "context_source": job.context_source,
    }


def _replace_job(job: CronJob, **changes: object) -> CronJob:
    values = {
        "id": job.id,
        "chat_id": job.chat_id,
        "text": job.text,
        "interval_seconds": job.interval_seconds,
        "created_at": job.created_at,
        "next_run_at": job.next_run_at,
        "last_run_at": job.last_run_at,
        "completed_at": job.completed_at,
        "status": job.status,
        "last_task_id": job.last_task_id,
        "context": job.context,
        "context_source": job.context_source,
    }
    values.update(changes)
    return CronJob(**values)


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
