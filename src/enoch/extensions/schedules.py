from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
from typing import Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from enoch.logs import log_system_event
from enoch.paths import private_state_path
from enoch.providers.contracts import TaskRequirements
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object
from enoch.tasks.payloads import (
    ExtensionArtifactReference,
    JsonValue,
    extension_artifact_references_from_json,
    extension_artifact_references_to_json,
    normalize_extension_artifact_references,
    normalize_extension_lane,
    normalize_extension_metadata,
)


EXTENSION_SCHEDULE_API_VERSION = 1
EXTENSION_SCHEDULE_STATE_SCHEMA_VERSION = 1

ExtensionScheduleCadence = Literal["interval", "daily"]
ExtensionScheduleState = Literal["active", "paused", "disabled"]
ExtensionScheduleOperation = Literal["status", "pause", "resume", "run_now"]

_SCHEDULE_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_EXTENSION_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_DAILY_TIME = re.compile(r"(?P<hour>[01]\d|2[0-3]):(?P<minute>[0-5]\d)")
_MIN_INTERVAL_SECONDS = 60
_MAX_INTERVAL_SECONDS = 366 * 24 * 60 * 60
_MAX_REQUEST_CHARS = 4096
_MAX_CONTEXT_CHARS = 65536
_MAX_ERROR_CHARS = 2048
_MAX_RUN_NOW_KEY_CHARS = 256


class ExtensionScheduleError(RuntimeError):
    """Base error for extension schedule declarations and controls."""


class ExtensionScheduleControlError(ExtensionScheduleError):
    """A schedule operation was rejected before mutating durable state."""

    def __init__(
        self,
        code: str,
        operation: ExtensionScheduleOperation,
        schedule_name: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation
        self.schedule_name = schedule_name


@dataclass(frozen=True)
class ExtensionScheduleSpec:
    """A bounded periodic task declaration owned by one extension."""

    name: str
    request: str
    interval_seconds: int = 0
    daily_time: str = ""
    timezone: str = "UTC"
    context: str = ""
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    artifact_refs: tuple[ExtensionArtifactReference, ...] = ()
    lane: str = ""
    api_version: int = EXTENSION_SCHEDULE_API_VERSION

    def __post_init__(self) -> None:
        if self.api_version != EXTENSION_SCHEDULE_API_VERSION:
            raise ExtensionScheduleError(
                f"Extension schedule {self.name!r} uses API version "
                f"{self.api_version}; Enoch supports version "
                f"{EXTENSION_SCHEDULE_API_VERSION}."
            )
        name = _schedule_name(self.name)
        if not isinstance(self.request, str):
            raise ExtensionScheduleError("Extension schedule request must be a string.")
        request = " ".join(self.request.split())
        if not request or len(request) > _MAX_REQUEST_CHARS:
            raise ExtensionScheduleError(
                "Extension schedule request must contain 1 to 4096 characters."
            )
        if not isinstance(self.context, str):
            raise ExtensionScheduleError("Extension schedule context must be a string.")
        context = self.context.strip()
        if len(context) > _MAX_CONTEXT_CHARS:
            raise ExtensionScheduleError(
                "Extension schedule context must be 65536 characters or fewer."
            )
        interval_seconds = _whole_number(
            self.interval_seconds,
            "Extension schedule interval",
        )
        daily_time = _normalize_daily_time(self.daily_time)
        if bool(interval_seconds) == bool(daily_time):
            raise ExtensionScheduleError(
                "Extension schedules require exactly one of interval_seconds or daily_time."
            )
        if interval_seconds and not (
            _MIN_INTERVAL_SECONDS <= interval_seconds <= _MAX_INTERVAL_SECONDS
        ):
            raise ExtensionScheduleError(
                "Extension schedule intervals must be between 60 and 31622400 seconds."
            )
        timezone_name = _timezone_name(self.timezone)
        try:
            capabilities = TaskRequirements(
                self.required_capabilities
            ).capabilities
            metadata = normalize_extension_metadata(self.metadata)
            artifact_refs = normalize_extension_artifact_references(
                self.artifact_refs
            )
            lane = normalize_extension_lane(self.lane)
        except (TypeError, ValueError) as error:
            raise ExtensionScheduleError(str(error)) from error
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "request", request)
        object.__setattr__(self, "interval_seconds", interval_seconds)
        object.__setattr__(self, "daily_time", daily_time)
        object.__setattr__(self, "timezone", timezone_name)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "artifact_refs", artifact_refs)
        object.__setattr__(self, "lane", lane)

    @property
    def cadence(self) -> ExtensionScheduleCadence:
        return "interval" if self.interval_seconds else "daily"


@dataclass(frozen=True)
class ExtensionScheduleStatus:
    """Provider-neutral durable status for one declared schedule."""

    id: str
    extension_name: str
    name: str
    state: ExtensionScheduleState
    request: str
    cadence: ExtensionScheduleCadence
    interval_seconds: int = 0
    daily_time: str = ""
    timezone: str = "UTC"
    context: str = ""
    required_capabilities: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    artifact_refs: tuple[ExtensionArtifactReference, ...] = ()
    lane: str = ""
    spec_version: int = EXTENSION_SCHEDULE_API_VERSION
    created_at: str = ""
    updated_at: str = ""
    next_run_at: str = ""
    last_scheduled_at: str = ""
    last_run_at: str = ""
    last_task_id: int | None = None
    last_outcome: str = ""
    last_error_code: str = ""
    last_error: str = ""
    last_error_at: str = ""
    claim_id: str = ""
    claimed_at: str = ""
    claim_kind: str = ""
    claim_scheduled_for: str = ""
    run_now_id: str = ""
    run_now_key: str = ""
    run_now_history: tuple[str, ...] = ()
    paused_at: str = ""
    disabled_at: str = ""

    @property
    def claimed(self) -> bool:
        return bool(self.claim_id)


@dataclass(frozen=True)
class ExtensionSchedules:
    """Bounded control surface for schedules owned by one extension."""

    extension_name: str
    _root: Path = field(repr=False)
    _wake: Callable[[], None] = field(default=lambda: None, repr=False)
    _event_actor: str = field(default="agent", repr=False)

    def inspect(self) -> tuple[ExtensionScheduleStatus, ...]:
        return extension_schedule_status(self.extension_name, self._root)

    def status(self, name: str) -> ExtensionScheduleStatus:
        normalized = _schedule_name(name)
        status = find_extension_schedule(self.extension_name, normalized, self._root)
        if status is None:
            raise _control_error(
                "schedule_not_found",
                "status",
                normalized,
                f"Extension schedule {self.extension_name}/{normalized} does not exist.",
            )
        return status

    def pause(self, name: str) -> ExtensionScheduleStatus:
        status = pause_extension_schedule(
            self.extension_name,
            name,
            self._root,
            event_actor=self._event_actor,
        )
        self._wake()
        return status

    def resume(self, name: str) -> ExtensionScheduleStatus:
        status = resume_extension_schedule(
            self.extension_name,
            name,
            self._root,
            event_actor=self._event_actor,
        )
        self._wake()
        return status

    def run_now(
        self,
        name: str,
        *,
        idempotency_key: str = "",
    ) -> ExtensionScheduleStatus:
        status = request_extension_schedule_run(
            self.extension_name,
            name,
            self._root,
            idempotency_key=idempotency_key,
            event_actor=self._event_actor,
        )
        self._wake()
        return status


def extension_schedule_path(root: Path | None = None) -> Path:
    return private_state_path("extension_schedules.json", root)


def reconcile_extension_schedules(
    declarations: dict[str, tuple[ExtensionScheduleSpec, ...]],
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> tuple[ExtensionScheduleStatus, ...]:
    """Reconcile declarative schedules and disable declarations no longer loaded."""

    current = _coerce_utc(now) if now is not None else _utc_now()
    desired: dict[str, tuple[str, ExtensionScheduleSpec]] = {}
    for raw_extension_name, specs in declarations.items():
        extension_name = _extension_name(raw_extension_name)
        for spec in tuple(specs):
            if not isinstance(spec, ExtensionScheduleSpec):
                raise ExtensionScheduleError(
                    f"Agent extension {extension_name} schedules must be "
                    "ExtensionScheduleSpec values."
                )
            schedule_id = _schedule_id(extension_name, spec.name)
            if schedule_id in desired:
                raise ExtensionScheduleError(
                    f"Duplicate extension schedule {extension_name}/{spec.name}."
                )
            desired[schedule_id] = (extension_name, spec)

    changed: list[tuple[str, str]] = []
    with _schedule_transaction(root):
        data = _load_schedule_data(root)
        existing = {item.id: item for item in _schedule_items(data)}
        reconciled: list[ExtensionScheduleStatus] = []
        for schedule_id, (extension_name, spec) in desired.items():
            prior = existing.pop(schedule_id, None)
            if prior is None:
                item = _new_status(extension_name, spec, current)
                changed.append((schedule_id, "created"))
            else:
                item, outcome = _reconcile_status(prior, spec, current)
                if outcome:
                    changed.append((schedule_id, outcome))
            reconciled.append(item)
        for prior in existing.values():
            if prior.state == "disabled":
                reconciled.append(prior)
                continue
            reconciled.append(
                replace(
                    prior,
                    state="disabled",
                    updated_at=_iso(current),
                    disabled_at=_iso(current),
                    run_now_id=(prior.run_now_id if prior.claim_id else ""),
                    run_now_key=(prior.run_now_key if prior.claim_id else ""),
                )
            )
            changed.append((prior.id, "disabled"))
        reconciled.sort(key=lambda item: item.id)
        if changed or tuple(reconciled) != tuple(_schedule_items(data)):
            _write_schedule_data(reconciled, root)

    for schedule_id, outcome in changed:
        _log_system_event(
            "agent_extension_schedule_reconciled",
            root=root,
            details={
                "schedule_id": schedule_id,
                "outcome": outcome,
                "event_actor": "system",
            },
        )
    return tuple(reconciled)


def extension_schedule_status(
    extension_name: str,
    root: Path | None = None,
) -> tuple[ExtensionScheduleStatus, ...]:
    normalized = _extension_name(extension_name)
    with _schedule_transaction(root):
        items = tuple(
            item
            for item in _schedule_items(_load_schedule_data(root))
            if item.extension_name == normalized
        )
    return items


def all_extension_schedule_statuses(
    root: Path | None = None,
) -> tuple[ExtensionScheduleStatus, ...]:
    with _schedule_transaction(root):
        return tuple(_schedule_items(_load_schedule_data(root)))


def find_extension_schedule(
    extension_name: str,
    name: str,
    root: Path | None = None,
) -> ExtensionScheduleStatus | None:
    schedule_id = _schedule_id(_extension_name(extension_name), _schedule_name(name))
    return next(
        (
            item
            for item in all_extension_schedule_statuses(root)
            if item.id == schedule_id
        ),
        None,
    )


def pause_extension_schedule(
    extension_name: str,
    name: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
    event_actor: str = "agent",
) -> ExtensionScheduleStatus:
    return _transition_schedule(
        extension_name,
        name,
        "pause",
        root,
        now=now,
        event_actor=event_actor,
    )


def resume_extension_schedule(
    extension_name: str,
    name: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
    event_actor: str = "agent",
) -> ExtensionScheduleStatus:
    return _transition_schedule(
        extension_name,
        name,
        "resume",
        root,
        now=now,
        event_actor=event_actor,
    )


def request_extension_schedule_run(
    extension_name: str,
    name: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
    idempotency_key: str = "",
    event_actor: str = "agent",
) -> ExtensionScheduleStatus:
    normalized_extension = _extension_name(extension_name)
    normalized_name = _schedule_name(name)
    schedule_id = _schedule_id(normalized_extension, normalized_name)
    key = _run_now_key(idempotency_key)
    current = _coerce_utc(now) if now is not None else _utc_now()
    changed = False
    with _schedule_transaction(root):
        items = _schedule_items(_load_schedule_data(root))
        selected = next((item for item in items if item.id == schedule_id), None)
        if selected is None:
            raise _control_error(
                "schedule_not_found",
                "run_now",
                normalized_name,
                f"Extension schedule {normalized_extension}/{normalized_name} does not exist.",
            )
        if selected.state != "active":
            raise _control_error(
                "invalid_state",
                "run_now",
                normalized_name,
                f"Extension schedule {normalized_extension}/{normalized_name} is {selected.state}.",
            )
        if key and key in selected.run_now_history:
            return selected
        if selected.claim_id or selected.run_now_id:
            if not key:
                return selected
            updated = replace(
                selected,
                updated_at=_iso(current),
                run_now_history=_append_run_now_key(
                    selected.run_now_history,
                    key,
                ),
            )
        else:
            updated = replace(
                selected,
                updated_at=_iso(current),
                run_now_id=f"run-now-{uuid4().hex}",
                run_now_key=key,
                run_now_history=_append_run_now_key(
                    selected.run_now_history,
                    key,
                ),
            )
        _replace_item(items, updated)
        _write_schedule_data(items, root)
        selected = updated
        changed = True
    if changed:
        _record_schedule_event(
            selected,
            "run_now_requested",
            root=root,
            event_actor=event_actor,
        )
    return selected


def claim_due_extension_schedules(
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> tuple[ExtensionScheduleStatus, ...]:
    current = _coerce_utc(now) if now is not None else _utc_now()
    claimed: list[ExtensionScheduleStatus] = []
    changed = False
    with _schedule_transaction(root):
        items = _schedule_items(_load_schedule_data(root))
        updated_items: list[ExtensionScheduleStatus] = []
        for item in items:
            if item.state != "active":
                updated_items.append(item)
                continue
            if item.claim_id:
                claimed.append(item)
                updated_items.append(item)
                continue
            scheduled_for = _parse_time(item.next_run_at)
            scheduled_due = scheduled_for is not None and scheduled_for <= current
            if not scheduled_due and not item.run_now_id:
                updated_items.append(item)
                continue
            kind = "scheduled" if scheduled_due else "run-now"
            occurrence = item.next_run_at if scheduled_due else item.run_now_id
            claimed_item = replace(
                item,
                updated_at=_iso(current),
                claim_id=(
                    f"extension-schedule-{item.extension_name}-{item.name}-"
                    f"{_claim_token(occurrence)}-{uuid4().hex}"
                ),
                claimed_at=_iso(current),
                claim_kind=kind,
                claim_scheduled_for=(
                    item.next_run_at if scheduled_due else _iso(current)
                ),
            )
            claimed.append(claimed_item)
            updated_items.append(claimed_item)
            changed = True
        if changed:
            _write_schedule_data(updated_items, root)
    return tuple(claimed)


def record_extension_schedule_task(
    schedule_id: str,
    task_id: int,
    root: Path | None = None,
    *,
    claim_id: str,
    now: datetime | None = None,
) -> ExtensionScheduleStatus | None:
    if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1:
        raise ValueError("Extension schedule task ID must be a positive integer.")
    updated = _acknowledge_schedule(
        schedule_id,
        claim_id,
        root,
        now=now,
        task_id=task_id,
        outcome="enqueued",
    )
    if updated is not None:
        _record_schedule_event(
            updated,
            "task_enqueued",
            root=root,
            event_actor="system",
            details={"task_id": task_id},
        )
    return updated


def record_extension_schedule_failure(
    schedule_id: str,
    root: Path | None = None,
    *,
    claim_id: str,
    code: str,
    error: str,
    now: datetime | None = None,
) -> ExtensionScheduleStatus | None:
    normalized_code = _error_code(code)
    normalized_error = " ".join(str(error).split())[:_MAX_ERROR_CHARS]
    updated = _acknowledge_schedule(
        schedule_id,
        claim_id,
        root,
        now=now,
        outcome="failed",
        error_code=normalized_code,
        error=normalized_error,
    )
    if updated is not None:
        _record_schedule_event(
            updated,
            "occurrence_failed",
            root=root,
            event_actor="system",
            status="failed",
            details={"code": normalized_code, "error": normalized_error},
        )
    return updated


def extension_schedule_wait_seconds(
    root: Path | None = None,
    *,
    now: datetime | None = None,
    max_wait_seconds: float = 5.0,
    claimed_retry_seconds: float = 1.0,
) -> float:
    if max_wait_seconds <= 0 or claimed_retry_seconds <= 0:
        raise ValueError("Schedule wait intervals must be greater than zero.")
    current = _coerce_utc(now) if now is not None else _utc_now()
    waits: list[float] = []
    for item in all_extension_schedule_statuses(root):
        if item.state != "active":
            continue
        if item.claim_id:
            waits.append(claimed_retry_seconds)
            continue
        if item.run_now_id:
            waits.append(0.0)
            continue
        next_run_at = _parse_time(item.next_run_at)
        waits.append(
            claimed_retry_seconds
            if next_run_at is None
            else max(0.0, (next_run_at - current).total_seconds())
        )
    if not waits:
        return max_wait_seconds
    return max(0.05, min(max_wait_seconds, *waits))


def _transition_schedule(
    extension_name: str,
    name: str,
    operation: Literal["pause", "resume"],
    root: Path | None,
    *,
    now: datetime | None,
    event_actor: str,
) -> ExtensionScheduleStatus:
    normalized_extension = _extension_name(extension_name)
    normalized_name = _schedule_name(name)
    schedule_id = _schedule_id(normalized_extension, normalized_name)
    current = _coerce_utc(now) if now is not None else _utc_now()
    changed = False
    with _schedule_transaction(root):
        items = _schedule_items(_load_schedule_data(root))
        selected = next((item for item in items if item.id == schedule_id), None)
        if selected is None:
            raise _control_error(
                "schedule_not_found",
                operation,
                normalized_name,
                f"Extension schedule {normalized_extension}/{normalized_name} does not exist.",
            )
        if selected.state == "disabled":
            raise _control_error(
                "invalid_state",
                operation,
                normalized_name,
                f"Extension schedule {normalized_extension}/{normalized_name} is disabled.",
            )
        target = "paused" if operation == "pause" else "active"
        if selected.state == target:
            return selected
        selected = replace(
            selected,
            state=target,
            updated_at=_iso(current),
            paused_at=_iso(current) if target == "paused" else "",
            run_now_id=(
                ""
                if target == "paused" and not selected.claim_id
                else selected.run_now_id
            ),
            run_now_key=(
                ""
                if target == "paused" and not selected.claim_id
                else selected.run_now_key
            ),
        )
        _replace_item(items, selected)
        _write_schedule_data(items, root)
        changed = True
    if changed:
        _record_schedule_event(
            selected,
            target,
            root=root,
            event_actor=event_actor,
        )
    return selected


def _acknowledge_schedule(
    schedule_id: str,
    claim_id: str,
    root: Path | None,
    *,
    now: datetime | None,
    outcome: str,
    task_id: int | None = None,
    error_code: str = "",
    error: str = "",
) -> ExtensionScheduleStatus | None:
    normalized_schedule_id = schedule_id.strip()
    normalized_claim_id = claim_id.strip()
    if not normalized_schedule_id or not normalized_claim_id:
        return None
    current = _coerce_utc(now) if now is not None else _utc_now()
    with _schedule_transaction(root):
        items = _schedule_items(_load_schedule_data(root))
        selected = next(
            (item for item in items if item.id == normalized_schedule_id),
            None,
        )
        if selected is None or selected.claim_id != normalized_claim_id:
            return None
        next_run_at = selected.next_run_at
        if selected.claim_kind == "scheduled":
            next_run_at = _iso(_next_run(selected, current))
        updated = replace(
            selected,
            updated_at=_iso(current),
            next_run_at=next_run_at,
            last_scheduled_at=selected.claim_scheduled_for,
            last_run_at=_iso(current),
            last_task_id=task_id if task_id is not None else selected.last_task_id,
            last_outcome=outcome,
            last_error_code=error_code,
            last_error=error,
            last_error_at=_iso(current) if error_code else "",
            claim_id="",
            claimed_at="",
            claim_kind="",
            claim_scheduled_for="",
            run_now_id="",
            run_now_key="",
        )
        _replace_item(items, updated)
        _write_schedule_data(items, root)
        return updated


def _new_status(
    extension_name: str,
    spec: ExtensionScheduleSpec,
    current: datetime,
) -> ExtensionScheduleStatus:
    return ExtensionScheduleStatus(
        id=_schedule_id(extension_name, spec.name),
        extension_name=extension_name,
        name=spec.name,
        state="active",
        request=spec.request,
        cadence=spec.cadence,
        interval_seconds=spec.interval_seconds,
        daily_time=spec.daily_time,
        timezone=spec.timezone,
        context=spec.context,
        required_capabilities=spec.required_capabilities,
        metadata=dict(spec.metadata),
        artifact_refs=spec.artifact_refs,
        lane=spec.lane,
        created_at=_iso(current),
        updated_at=_iso(current),
        next_run_at=_iso(_first_run(spec, current)),
    )


def _reconcile_status(
    prior: ExtensionScheduleStatus,
    spec: ExtensionScheduleSpec,
    current: datetime,
) -> tuple[ExtensionScheduleStatus, str]:
    changed = not _matches_spec(prior, spec)
    reenabled = prior.state == "disabled"
    cadence_changed = (
        prior.cadence != spec.cadence
        or prior.interval_seconds != spec.interval_seconds
        or prior.daily_time != spec.daily_time
        or prior.timezone != spec.timezone
    )
    if not changed and not reenabled:
        return prior, ""
    state: ExtensionScheduleState = "active" if reenabled else prior.state
    next_run_at = prior.next_run_at
    if (
        (reenabled and prior.claim_kind != "scheduled")
        or (cadence_changed and prior.claim_kind != "scheduled")
        or not _parse_time(next_run_at)
    ):
        next_run_at = _iso(_first_run(spec, current))
    updated = replace(
        prior,
        state=state,
        request=spec.request,
        cadence=spec.cadence,
        interval_seconds=spec.interval_seconds,
        daily_time=spec.daily_time,
        timezone=spec.timezone,
        context=spec.context,
        required_capabilities=spec.required_capabilities,
        metadata=dict(spec.metadata),
        artifact_refs=spec.artifact_refs,
        lane=spec.lane,
        spec_version=spec.api_version,
        updated_at=_iso(current),
        next_run_at=next_run_at,
        disabled_at="",
        paused_at="" if reenabled else prior.paused_at,
        claim_id=prior.claim_id,
        claimed_at=prior.claimed_at,
        claim_kind=prior.claim_kind,
        claim_scheduled_for=(
            prior.claim_scheduled_for
        ),
        run_now_id=prior.run_now_id if prior.claim_id else "",
        run_now_key=prior.run_now_key if prior.claim_id else "",
    )
    return updated, "reenabled" if reenabled else "updated"


def _matches_spec(
    status: ExtensionScheduleStatus,
    spec: ExtensionScheduleSpec,
) -> bool:
    return (
        status.spec_version == spec.api_version
        and status.request == spec.request
        and status.cadence == spec.cadence
        and status.interval_seconds == spec.interval_seconds
        and status.daily_time == spec.daily_time
        and status.timezone == spec.timezone
        and status.context == spec.context
        and status.required_capabilities == spec.required_capabilities
        and status.metadata == spec.metadata
        and status.artifact_refs == spec.artifact_refs
        and status.lane == spec.lane
    )


def _load_schedule_data(root: Path | None) -> dict:
    path = extension_schedule_path(root)
    raw = load_json_object(path, default_factory=_empty_schedule_data)
    schedules = raw.get("schedules", [])
    if not isinstance(schedules, list):
        raise StateCorruptionError(path, "expected schedules to be a list")
    parsed = [_parse_status(item) for item in schedules]
    if any(item is None for item in parsed):
        raise StateCorruptionError(path, "found an invalid extension schedule")
    typed = [item for item in parsed if item is not None]
    if len({item.id for item in typed}) != len(typed):
        raise StateCorruptionError(path, "found duplicate extension schedule identities")
    return {
        "schema_version": EXTENSION_SCHEDULE_STATE_SCHEMA_VERSION,
        "schedules": [_status_to_dict(item) for item in sorted(typed, key=lambda item: item.id)],
    }


def _write_schedule_data(
    schedules: list[ExtensionScheduleStatus] | tuple[ExtensionScheduleStatus, ...],
    root: Path | None,
) -> None:
    payload = {
        "schema_version": EXTENSION_SCHEDULE_STATE_SCHEMA_VERSION,
        "schedules": [
            _status_to_dict(item) for item in sorted(schedules, key=lambda item: item.id)
        ],
    }
    atomic_write(
        extension_schedule_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _schedule_items(data: dict) -> list[ExtensionScheduleStatus]:
    return [
        item
        for item in (_parse_status(raw) for raw in data.get("schedules", []))
        if item is not None
    ]


def _parse_status(raw: object) -> ExtensionScheduleStatus | None:
    if not isinstance(raw, dict):
        return None
    try:
        extension_name = _extension_name(raw.get("extension_name"))
        name = _schedule_name(raw.get("name"))
        schedule_id = _schedule_id(extension_name, name)
        if raw.get("id") != schedule_id:
            return None
        state = str(raw.get("state") or "")
        cadence = str(raw.get("cadence") or "")
        if state not in {"active", "paused", "disabled"}:
            return None
        if cadence not in {"interval", "daily"}:
            return None
        spec = ExtensionScheduleSpec(
            name=name,
            request=raw.get("request"),
            interval_seconds=raw.get("interval_seconds", 0),
            daily_time=raw.get("daily_time", ""),
            timezone=raw.get("timezone", "UTC"),
            context=raw.get("context", ""),
            required_capabilities=tuple(raw.get("required_capabilities", ())),
            metadata=raw.get("metadata", {}),
            artifact_refs=extension_artifact_references_from_json(
                raw.get("artifact_refs", [])
            ),
            lane=raw.get("lane", ""),
            api_version=raw.get("spec_version", EXTENSION_SCHEDULE_API_VERSION),
        )
        if cadence != spec.cadence:
            return None
        last_task_id = _optional_positive_int(raw.get("last_task_id"))
        status = ExtensionScheduleStatus(
            id=schedule_id,
            extension_name=extension_name,
            name=name,
            state=state,  # type: ignore[arg-type]
            request=spec.request,
            cadence=cadence,  # type: ignore[arg-type]
            interval_seconds=spec.interval_seconds,
            daily_time=spec.daily_time,
            timezone=spec.timezone,
            context=spec.context,
            required_capabilities=spec.required_capabilities,
            metadata=spec.metadata,
            artifact_refs=spec.artifact_refs,
            lane=spec.lane,
            spec_version=spec.api_version,
            created_at=_string(raw.get("created_at")),
            updated_at=_string(raw.get("updated_at")),
            next_run_at=_string(raw.get("next_run_at")),
            last_scheduled_at=_string(raw.get("last_scheduled_at")),
            last_run_at=_string(raw.get("last_run_at")),
            last_task_id=last_task_id,
            last_outcome=_string(raw.get("last_outcome")),
            last_error_code=_string(raw.get("last_error_code")),
            last_error=_string(raw.get("last_error")),
            last_error_at=_string(raw.get("last_error_at")),
            claim_id=_string(raw.get("claim_id")),
            claimed_at=_string(raw.get("claimed_at")),
            claim_kind=_string(raw.get("claim_kind")),
            claim_scheduled_for=_string(raw.get("claim_scheduled_for")),
            run_now_id=_string(raw.get("run_now_id")),
            run_now_key=_string(raw.get("run_now_key")),
            run_now_history=_run_now_history(raw.get("run_now_history", [])),
            paused_at=_string(raw.get("paused_at")),
            disabled_at=_string(raw.get("disabled_at")),
        )
        if (
            _parse_time(status.created_at) is None
            or _parse_time(status.next_run_at) is None
        ):
            return None
        if status.claim_kind not in {"", "scheduled", "run-now"}:
            return None
        if bool(status.claim_id) != bool(status.claim_kind):
            return None
        if status.claim_id and (
            _parse_time(status.claimed_at) is None
            or _parse_time(status.claim_scheduled_for) is None
        ):
            return None
        return status
    except (ExtensionScheduleError, TypeError, ValueError):
        return None


def _status_to_dict(status: ExtensionScheduleStatus) -> dict[str, object]:
    return {
        "id": status.id,
        "extension_name": status.extension_name,
        "name": status.name,
        "state": status.state,
        "request": status.request,
        "cadence": status.cadence,
        "interval_seconds": status.interval_seconds,
        "daily_time": status.daily_time,
        "timezone": status.timezone,
        "context": status.context,
        "required_capabilities": list(status.required_capabilities),
        "metadata": status.metadata,
        "artifact_refs": extension_artifact_references_to_json(status.artifact_refs),
        "lane": status.lane,
        "spec_version": status.spec_version,
        "created_at": status.created_at,
        "updated_at": status.updated_at,
        "next_run_at": status.next_run_at,
        "last_scheduled_at": status.last_scheduled_at,
        "last_run_at": status.last_run_at,
        "last_task_id": status.last_task_id,
        "last_outcome": status.last_outcome,
        "last_error_code": status.last_error_code,
        "last_error": status.last_error,
        "last_error_at": status.last_error_at,
        "claim_id": status.claim_id,
        "claimed_at": status.claimed_at,
        "claim_kind": status.claim_kind,
        "claim_scheduled_for": status.claim_scheduled_for,
        "run_now_id": status.run_now_id,
        "run_now_key": status.run_now_key,
        "run_now_history": list(status.run_now_history),
        "paused_at": status.paused_at,
        "disabled_at": status.disabled_at,
    }


def _first_run(spec: ExtensionScheduleSpec, current: datetime) -> datetime:
    if spec.cadence == "interval":
        return current + timedelta(seconds=spec.interval_seconds)
    return _next_daily(spec.daily_time, spec.timezone, current)


def _next_run(status: ExtensionScheduleStatus, current: datetime) -> datetime:
    if status.cadence == "daily":
        return _next_daily(status.daily_time, status.timezone, current)
    scheduled_for = _parse_time(status.claim_scheduled_for)
    if scheduled_for is None:
        return current + timedelta(seconds=status.interval_seconds)
    candidate = scheduled_for + timedelta(seconds=status.interval_seconds)
    if candidate > current:
        return candidate
    missed = int((current - candidate).total_seconds() // status.interval_seconds) + 1
    return candidate + timedelta(seconds=missed * status.interval_seconds)


def _next_daily(daily_time: str, timezone_name: str, current: datetime) -> datetime:
    zone = ZoneInfo(timezone_name)
    local_now = _coerce_utc(current).astimezone(zone)
    hour, minute = (int(part) for part in daily_time.split(":"))
    target_date = local_now.date()
    candidate = _local_candidate(target_date, hour, minute, zone)
    if candidate <= _coerce_utc(current):
        candidate = _local_candidate(
            target_date + timedelta(days=1),
            hour,
            minute,
            zone,
        )
    return candidate


def _local_candidate(
    target_date: date,
    hour: int,
    minute: int,
    zone: ZoneInfo,
) -> datetime:
    local = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
    return local.astimezone(timezone.utc).replace(microsecond=0)


def _replace_item(
    items: list[ExtensionScheduleStatus],
    updated: ExtensionScheduleStatus,
) -> None:
    for index, item in enumerate(items):
        if item.id == updated.id:
            items[index] = updated
            return
    raise RuntimeError(f"Extension schedule {updated.id} disappeared during mutation.")


def _record_schedule_event(
    schedule: ExtensionScheduleStatus,
    outcome: str,
    *,
    root: Path | None,
    event_actor: str,
    status: str = "ok",
    details: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schedule_id": schedule.id,
        "extension": schedule.extension_name,
        "schedule": schedule.name,
        "outcome": outcome,
        "next_run_at": schedule.next_run_at,
        "event_actor": _event_actor(event_actor),
    }
    if details:
        payload.update(details)
    _log_system_event(
        "agent_extension_schedule_event",
        root=root,
        status=status,
        details=payload,
    )


def _log_system_event(
    event: str,
    *,
    root: Path | None,
    status: str = "ok",
    details: dict[str, object] | None = None,
) -> None:
    try:
        log_system_event(event, root=root, status=status, details=details)
    except OSError:
        pass


def _schedule_transaction(root: Path | None):
    return file_transaction(extension_schedule_path(root))


def _empty_schedule_data() -> dict[str, object]:
    return {
        "schema_version": EXTENSION_SCHEDULE_STATE_SCHEMA_VERSION,
        "schedules": [],
    }


def _schedule_id(extension_name: str, schedule_name: str) -> str:
    return f"extension:{extension_name}:{schedule_name}"


def _schedule_name(value: object) -> str:
    if not isinstance(value, str):
        raise ExtensionScheduleError("Extension schedule name must be a string.")
    name = value.strip().lower()
    if not _SCHEDULE_NAME.fullmatch(name):
        raise ExtensionScheduleError(f"Invalid extension schedule name {value!r}.")
    return name


def _extension_name(value: object) -> str:
    if not isinstance(value, str):
        raise ExtensionScheduleError("Extension name must be a string.")
    name = value.strip().lower()
    if not _EXTENSION_NAME.fullmatch(name):
        raise ExtensionScheduleError(f"Invalid extension name {value!r}.")
    return name


def _normalize_daily_time(value: object) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ExtensionScheduleError("Extension schedule daily time must be a string.")
    daily_time = value.strip()
    if not _DAILY_TIME.fullmatch(daily_time):
        raise ExtensionScheduleError(
            "Extension schedule daily time must look like HH:MM."
        )
    return daily_time


def _timezone_name(value: object) -> str:
    if not isinstance(value, str):
        raise ExtensionScheduleError("Extension schedule timezone must be a string.")
    name = value.strip()
    if not name or len(name) > 128:
        raise ExtensionScheduleError(
            "Extension schedule timezone must contain 1 to 128 characters."
        )
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ExtensionScheduleError(
            f"Unknown extension schedule timezone {value!r}."
        ) from error
    return name


def _whole_number(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ExtensionScheduleError(f"{label} must be a whole number.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise ExtensionScheduleError(f"{label} must be a whole number.") from error
    if parsed != value:
        raise ExtensionScheduleError(f"{label} must be a whole number.")
    return parsed


def _optional_positive_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError
    parsed = int(value)
    if parsed < 1:
        raise ValueError
    return parsed


def _run_now_key(value: object) -> str:
    if not isinstance(value, str):
        raise ExtensionScheduleError("Schedule run-now idempotency key must be a string.")
    key = value.strip()
    if "\n" in key or len(key) > _MAX_RUN_NOW_KEY_CHARS:
        raise ExtensionScheduleError(
            "Schedule run-now idempotency keys must be one line and 256 characters or fewer."
        )
    return key


def _run_now_history(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ExtensionScheduleError(
            "Persisted schedule run-now history must be a list."
        )
    keys = tuple(_run_now_key(item) for item in value)
    if any(not key for key in keys) or len(keys) > 64 or len(set(keys)) != len(keys):
        raise ExtensionScheduleError(
            "Persisted schedule run-now history is invalid."
        )
    return keys


def _append_run_now_key(history: tuple[str, ...], key: str) -> tuple[str, ...]:
    if not key or key in history:
        return history
    return (*history, key)[-64:]


def _error_code(value: object) -> str:
    code = str(value or "schedule_failed").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", code):
        return "schedule_failed"
    return code


def _event_actor(value: object) -> str:
    actor = str(value or "").strip().lower()
    return actor if actor in {"human", "agent", "system"} else "agent"


def _claim_token(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-")[-64:] or "due"


def _control_error(
    code: str,
    operation: ExtensionScheduleOperation,
    schedule_name: str,
    message: str,
) -> ExtensionScheduleControlError:
    return ExtensionScheduleControlError(code, operation, schedule_name, message)


def _string(value: object) -> str:
    return str(value or "").strip()


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
