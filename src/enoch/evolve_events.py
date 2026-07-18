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


SCHEMA_VERSION = 1
EVOLVE_EVENT_TYPES = {
    "checked",
    "proposed",
    "selected",
    "queued",
    "completed",
    "failed",
    "cancelled",
    "skipped",
    "removed",
}
EVOLVE_EVENT_ACTORS = {"human", "agent", "system"}
EVOLVE_SOURCES = {
    "backlog",
    "feedback",
    "experience",
    "inheritance",
    "learning",
    "brainstorming",
}
_CANDIDATE_EVENTS = {
    "proposed",
    "selected",
    "queued",
    "completed",
    "failed",
    "cancelled",
    "removed",
}
_EVOLVE_EVENT_THREAD_LOCK = threading.RLock()


class CandidateLike(Protocol):
    id: str
    source: str
    initiated_by: str
    score: int


@dataclass(frozen=True)
class EvolveEvent:
    id: str
    occurred_at: str
    event: str
    event_actor: str
    trigger: str
    mode: str
    theme: str
    candidate_id: str = ""
    task_id: int | None = None
    source: str = ""
    candidate_initiated_by: str = ""
    score: int = 0
    reason: str = ""


def evolve_event_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve_events.jsonl"


def record_evolve_event(
    event: str,
    root: Path | None = None,
    *,
    event_actor: str,
    trigger: str,
    mode: str = "",
    theme: str = "",
    candidate: CandidateLike | None = None,
    task_id: int | None = None,
    reason: str = "",
) -> EvolveEvent:
    normalized_event = clean_text(event).lower()
    normalized_actor = clean_text(event_actor).lower()
    if normalized_event not in EVOLVE_EVENT_TYPES:
        raise ValueError(
            f"Evolve event must be one of: {', '.join(sorted(EVOLVE_EVENT_TYPES))}."
        )
    if normalized_actor not in EVOLVE_EVENT_ACTORS:
        raise ValueError(
            f"Evolve event actor must be one of: {', '.join(sorted(EVOLVE_EVENT_ACTORS))}."
        )
    candidate_id = clean_text(str(getattr(candidate, "id", "") or ""))
    source = clean_text(str(getattr(candidate, "source", "") or "")).lower()
    initiated_by = clean_text(
        str(getattr(candidate, "initiated_by", "") or "")
    ).lower()
    if normalized_event in _CANDIDATE_EVENTS and not candidate_id:
        raise ValueError(f"Evolve event {normalized_event} requires a candidate.")
    if candidate_id and source not in EVOLVE_SOURCES:
        raise ValueError(
            f"Evolve source must be one of: {', '.join(sorted(EVOLVE_SOURCES))}."
        )
    if candidate_id and initiated_by not in {"human", "agent"}:
        raise ValueError("Candidate initiator must be human or agent.")
    normalized_task_id = _positive_int(task_id)
    if (
        normalized_event in {"queued", "completed", "failed", "cancelled"}
        and normalized_task_id is None
    ):
        raise ValueError(f"Evolve event {normalized_event} requires a task id.")
    evolve_event = EvolveEvent(
        id=f"evolve-event-{uuid4().hex}",
        occurred_at=current_time(),
        event=normalized_event,
        event_actor=normalized_actor,
        trigger=clean_text(trigger),
        mode=clean_text(mode).lower(),
        theme=clean_text(theme),
        candidate_id=candidate_id,
        task_id=normalized_task_id,
        source=source,
        candidate_initiated_by=initiated_by,
        score=_int(getattr(candidate, "score", 0)),
        reason=_clip(clean_text(reason)),
    )
    with _evolve_event_transaction(root):
        path = evolve_event_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"schema_version": SCHEMA_VERSION, **asdict(evolve_event)},
                    sort_keys=True,
                )
                + "\n"
            )
    return evolve_event


def load_evolve_events(
    root: Path | None = None,
    *,
    limit: int = 5000,
    candidate_id: str = "",
    task_id: int | None = None,
) -> tuple[EvolveEvent, ...]:
    path = evolve_event_path(root)
    if not path.exists() or limit <= 0:
        return ()
    wanted_candidate = clean_text(candidate_id).lower()
    wanted_task = _positive_int(task_id)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    events: list[EvolveEvent] = []
    for line in reversed(lines):
        event = _event_from_line(line)
        if event is None:
            continue
        if wanted_candidate and event.candidate_id.lower() != wanted_candidate:
            continue
        if wanted_task is not None and event.task_id != wanted_task:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    events.reverse()
    return tuple(events)


def _event_from_line(line: str) -> EvolveEvent | None:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    event = clean_text(str(raw.get("event") or "")).lower()
    actor = clean_text(str(raw.get("event_actor") or "")).lower()
    candidate_id = clean_text(str(raw.get("candidate_id") or ""))
    source = clean_text(str(raw.get("source") or "")).lower()
    initiated_by = clean_text(str(raw.get("candidate_initiated_by") or "")).lower()
    task_id = _positive_int(raw.get("task_id"))
    if event not in EVOLVE_EVENT_TYPES or actor not in EVOLVE_EVENT_ACTORS:
        return None
    if event in _CANDIDATE_EVENTS and not candidate_id:
        return None
    if candidate_id and (
        source not in EVOLVE_SOURCES or initiated_by not in {"human", "agent"}
    ):
        return None
    if event in {"queued", "completed", "failed", "cancelled"} and task_id is None:
        return None
    occurred_at = str(raw.get("occurred_at") or "")
    legacy_id = f"legacy-evolve-event-{event}-{candidate_id or task_id or occurred_at}"
    return EvolveEvent(
        id=clean_text(str(raw.get("id") or "")) or legacy_id,
        occurred_at=occurred_at,
        event=event,
        event_actor=actor,
        trigger=clean_text(str(raw.get("trigger") or "")),
        mode=clean_text(str(raw.get("mode") or "")).lower(),
        theme=clean_text(str(raw.get("theme") or "")),
        candidate_id=candidate_id,
        task_id=task_id,
        source=source,
        candidate_initiated_by=initiated_by,
        score=_int(raw.get("score")),
        reason=_clip(clean_text(str(raw.get("reason") or ""))),
    )


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clip(value: str, limit: int = 1000) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


@contextmanager
def _evolve_event_transaction(root: Path | None = None):
    path = evolve_event_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with _EVOLVE_EVENT_THREAD_LOCK:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
