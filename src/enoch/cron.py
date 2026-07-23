from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
from uuid import uuid4

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home
from enoch.providers.contracts import ConversationId, normalize_conversation_id
from enoch.state import StateCorruptionError, file_transaction, load_json_object


SCHEMA_VERSION = 3
_INTERVAL_PATTERN = re.compile(
    r"^\s*(?P<count>\d+)\s*(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CronJob:
    id: int
    chat_id: ConversationId
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
    claim_id: str = ""
    claimed_at: str = ""
    idempotency_key: str = ""


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
    chat_id: ConversationId,
    text: str,
    interval_seconds: int,
    root: Path | None = None,
    *,
    context: str = "",
    context_source: str = "",
    now: datetime | None = None,
    idempotency_key: str = "",
) -> CronJob:
    cleaned = " ".join(text.split())
    if not cleaned:
        raise ValueError("Cron text is required.")
    if interval_seconds <= 0:
        raise ValueError("Cron interval must be greater than zero.")
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        normalized_key = idempotency_key.strip()
        if normalized_key:
            existing = next(
                (
                    job
                    for job in [*_active_jobs(data), *_history_jobs(data)]
                    if job.idempotency_key == normalized_key
                ),
                None,
            )
            if existing is not None:
                return existing
        job = CronJob(
            id=_next_id(data),
            chat_id=chat_id,
            text=cleaned,
            interval_seconds=interval_seconds,
            created_at=_iso(current),
            next_run_at=_iso(current + timedelta(seconds=interval_seconds)),
            context=context.strip(),
            context_source=context_source.strip(),
            idempotency_key=normalized_key,
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
        data["history"] = [_job_to_dict(job) for job in history]
        _write_cron(data, root)
        return cancelled


def claim_due_cron_jobs(root: Path | None = None, *, now: datetime | None = None) -> tuple[CronJob, ...]:
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _cron_transaction(root):
        data = _load_cron(root)
        claimed: list[CronJob] = []
        active: list[CronJob] = []
        for job in _active_jobs(data):
            if job.claim_id:
                claimed.append(job)
                active.append(job)
                continue
            next_run_at = _parse_time(job.next_run_at)
            if next_run_at is None or next_run_at > current:
                active.append(job)
                continue
            claimed_job = _replace_job(
                job,
                claim_id=f"cron-{job.id}-{uuid4().hex}",
                claimed_at=_iso(current),
            )
            claimed.append(claimed_job)
            active.append(claimed_job)
        if claimed:
            data["active"] = [_job_to_dict(job) for job in active]
            _write_cron(data, root)
        return tuple(claimed)


def record_cron_task(
    cron_id: int,
    task_id: int,
    root: Path | None = None,
    *,
    claim_id: str = "",
    now: datetime | None = None,
) -> CronJob | None:
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
                    changes.update(
                        {
                            "last_run_at": job.claimed_at or _iso(current),
                            "next_run_at": _iso(
                                current + timedelta(seconds=job.interval_seconds)
                            ),
                            "claim_id": "",
                            "claimed_at": "",
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
    raw = load_json_object(path, default_factory=_empty_cron)
    for key in ("active", "history"):
        if key in raw and not isinstance(raw[key], list):
            raise StateCorruptionError(path, f"expected {key} to be a list")
        if any(_parse_job(item) is None for item in raw.get(key, [])):
            raise StateCorruptionError(path, f"found an invalid cron job in {key}")
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
    chat_id = normalize_conversation_id(raw.get("chat_id"))
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
    claim_id = str(raw.get("claim_id") or "").strip()
    claimed_at = str(raw.get("claimed_at") or "").strip()
    idempotency_key = str(raw.get("idempotency_key") or "").strip()
    if job_id <= 0 or chat_id is None or interval_seconds <= 0 or not text:
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
        claim_id=claim_id,
        claimed_at=claimed_at,
        idempotency_key=idempotency_key,
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
        "claim_id": job.claim_id,
        "claimed_at": job.claimed_at,
        "idempotency_key": job.idempotency_key,
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
        "claim_id": job.claim_id,
        "claimed_at": job.claimed_at,
        "idempotency_key": job.idempotency_key,
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
