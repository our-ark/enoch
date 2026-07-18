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


SCHEMA_VERSION = 4
EVOLVE_EVENT_TYPES = {
    "checked",
    "proposed",
    "selected",
    "queued",
    "completed",
    "failed",
    "cancelled",
    "paused",
    "resumed",
    "regressed",
    "reverted",
    "forward-fixed",
    "no-action",
    "skipped",
    "removed",
    "promoted",
    "adopted",
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
    "paused",
    "resumed",
    "regressed",
    "reverted",
    "forward-fixed",
    "no-action",
    "removed",
    "promoted",
    "adopted",
}
PROPOSAL_DISPOSITION_EVENTS = {"selected", "removed", "no-action"}
RECORDING_MODES = {"realtime", "backfill"}
_EVOLVE_EVENT_THREAD_LOCK = threading.RLock()


class CandidateLike(Protocol):
    id: str
    source: str
    initiated_by: str
    evidence_source: str
    signal_actor: str
    candidate_actor: str
    parent_candidate_id: str
    source_task_id: int | None
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
    proposal_id: str = ""
    candidate_id: str = ""
    task_id: int | None = None
    source: str = ""
    candidate_initiated_by: str = ""
    evidence_source: str = ""
    signal_actor: str = ""
    candidate_actor: str = ""
    approval_actor: str = ""
    parent_candidate_id: str = ""
    source_task_id: int | None = None
    retry_of_task_id: int | None = None
    score: int = 0
    reason: str = ""
    pr_url: str = ""
    merge_commit: str = ""
    authoritative_branch: str = ""
    promoted_at: str = ""
    verified_at: str = ""
    version: str = ""
    health_check: str = ""
    recording_mode: str = ""


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
    approval_actor: str = "",
    retry_of_task_id: int | None = None,
    reason: str = "",
    proposal_id: str = "",
    pr_url: str = "",
    merge_commit: str = "",
    authoritative_branch: str = "",
    promoted_at: str = "",
    verified_at: str = "",
    version: str = "",
    health_check: str = "",
    recording_mode: str = "",
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
    evidence_source = clean_text(
        str(getattr(candidate, "evidence_source", "") or source)
    ).lower()
    signal_actor = _actor(getattr(candidate, "signal_actor", ""))
    candidate_actor = _actor(getattr(candidate, "candidate_actor", ""))
    normalized_approval_actor = _actor(approval_actor)
    parent_candidate_id = clean_text(
        str(getattr(candidate, "parent_candidate_id", "") or "")
    )
    source_task_id = _positive_int(getattr(candidate, "source_task_id", None))
    if candidate_id:
        signal_actor = signal_actor or _legacy_signal_actor(evidence_source)
        candidate_actor = candidate_actor or "agent"
        if normalized_event in {"selected", "queued"} and not normalized_approval_actor:
            normalized_approval_actor = _legacy_approval_actor(
                clean_text(trigger),
                normalized_actor,
            )
        initiated_by = (
            candidate_actor
            if candidate_actor in {"human", "agent"}
            else initiated_by
        )
    if normalized_event in _CANDIDATE_EVENTS and not candidate_id:
        raise ValueError(f"Evolve event {normalized_event} requires a candidate.")
    if candidate_id and source not in EVOLVE_SOURCES:
        raise ValueError(
            f"Evolve source must be one of: {', '.join(sorted(EVOLVE_SOURCES))}."
        )
    if candidate_id and evidence_source not in EVOLVE_SOURCES:
        raise ValueError(
            f"Evolution evidence source must be one of: {', '.join(sorted(EVOLVE_SOURCES))}."
        )
    if candidate_id and initiated_by not in {"human", "agent"}:
        raise ValueError("Candidate initiator must be human or agent.")
    if candidate_id and signal_actor not in EVOLVE_EVENT_ACTORS:
        raise ValueError("Signal actor must be human, agent, or system.")
    if candidate_id and candidate_actor not in EVOLVE_EVENT_ACTORS:
        raise ValueError("Candidate actor must be human, agent, or system.")
    if approval_actor and not normalized_approval_actor:
        raise ValueError("Approval actor must be human, agent, or system.")
    normalized_task_id = _positive_int(task_id)
    normalized_proposal_id = clean_text(proposal_id)
    if normalized_event == "proposed" and not normalized_proposal_id:
        normalized_proposal_id = f"proposal-{uuid4().hex}"
    if normalized_event == "no-action" and not normalized_proposal_id:
        raise ValueError("Evolve event no-action requires a proposal id.")
    if (
        normalized_event
        in {"queued", "completed", "failed", "cancelled", "regressed", "reverted", "forward-fixed"}
        and normalized_task_id is None
    ):
        raise ValueError(f"Evolve event {normalized_event} requires a task id.")
    normalized_recording_mode = clean_text(recording_mode).lower()
    normalized_pr_url = clean_text(pr_url)
    normalized_merge_commit = clean_text(merge_commit)
    normalized_authoritative_branch = clean_text(authoritative_branch)
    normalized_promoted_at = clean_text(promoted_at)
    normalized_verified_at = clean_text(verified_at)
    normalized_version = clean_text(version)
    normalized_health_check = clean_text(health_check).lower()
    if normalized_event in {"promoted", "adopted"}:
        normalized_recording_mode = normalized_recording_mode or "realtime"
        normalized_verified_at = normalized_verified_at or current_time()
        if normalized_recording_mode not in RECORDING_MODES:
            raise ValueError("Lifecycle recording mode must be realtime or backfill.")
    if normalized_event == "promoted":
        if normalized_actor != "human":
            raise ValueError("Promoted evolution events require a human actor.")
        if not all(
            [
                normalized_pr_url,
                normalized_merge_commit,
                normalized_authoritative_branch,
                normalized_promoted_at,
            ]
        ):
            raise ValueError(
                "Promoted evolution events require PR URL, merge commit, "
                "authoritative branch, and promoted time."
            )
    if normalized_event == "adopted":
        if not normalized_version or normalized_health_check != "passed":
            raise ValueError(
                "Adopted evolution events require a version and a passed health check."
            )
    evolve_event = EvolveEvent(
        id=f"evolve-event-{uuid4().hex}",
        occurred_at=current_time(),
        event=normalized_event,
        event_actor=normalized_actor,
        trigger=clean_text(trigger),
        mode=clean_text(mode).lower(),
        theme=clean_text(theme),
        proposal_id=normalized_proposal_id,
        candidate_id=candidate_id,
        task_id=normalized_task_id,
        source=source,
        candidate_initiated_by=initiated_by,
        evidence_source=evidence_source,
        signal_actor=signal_actor,
        candidate_actor=candidate_actor,
        approval_actor=normalized_approval_actor,
        parent_candidate_id=parent_candidate_id,
        source_task_id=source_task_id,
        retry_of_task_id=_positive_int(retry_of_task_id),
        score=_int(getattr(candidate, "score", 0)),
        reason=_clip(clean_text(reason)),
        pr_url=normalized_pr_url,
        merge_commit=normalized_merge_commit,
        authoritative_branch=normalized_authoritative_branch,
        promoted_at=normalized_promoted_at,
        verified_at=normalized_verified_at,
        version=normalized_version,
        health_check=normalized_health_check,
        recording_mode=normalized_recording_mode,
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
    proposal_id: str = "",
) -> tuple[EvolveEvent, ...]:
    path = evolve_event_path(root)
    if not path.exists() or limit <= 0:
        return ()
    wanted_candidate = clean_text(candidate_id).lower()
    wanted_task = _positive_int(task_id)
    wanted_proposal = clean_text(proposal_id)
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
        if wanted_proposal and event.proposal_id != wanted_proposal:
            continue
        events.append(event)
        if len(events) >= limit:
            break
    events.reverse()
    return tuple(events)


def load_open_proposals(root: Path | None = None) -> tuple[EvolveEvent, ...]:
    proposed: dict[str, EvolveEvent] = {}
    closed: set[str] = set()
    for event in load_evolve_events(root):
        if event.event == "proposed" and event.proposal_id:
            proposed[event.proposal_id] = event
        elif event.event in PROPOSAL_DISPOSITION_EVENTS and event.proposal_id:
            closed.add(event.proposal_id)
    return tuple(
        event
        for proposal_id, event in proposed.items()
        if proposal_id not in closed
        and not proposal_id.startswith("legacy-proposal-")
    )


def latest_open_proposal_id(
    candidate_id: str,
    root: Path | None = None,
) -> str:
    normalized_candidate_id = clean_text(candidate_id).lower()
    for event in reversed(load_open_proposals(root)):
        if event.candidate_id.lower() == normalized_candidate_id:
            return event.proposal_id
    return ""


def linked_proposal_id(
    root: Path | None = None,
    *,
    candidate_id: str = "",
    task_id: int | None = None,
) -> str:
    normalized_candidate_id = clean_text(candidate_id).lower()
    normalized_task_id = _positive_int(task_id)
    for event in reversed(load_evolve_events(root)):
        if not event.proposal_id:
            continue
        if normalized_task_id is not None and event.task_id == normalized_task_id:
            return event.proposal_id
        if normalized_candidate_id and event.candidate_id.lower() == normalized_candidate_id:
            if event.event in {"selected", "queued"}:
                return event.proposal_id
    return ""


def close_open_proposals(
    root: Path | None = None,
    *,
    event_actor: str,
    trigger: str,
    reason: str,
) -> tuple[EvolveEvent, ...]:
    closed = []
    for proposal in load_open_proposals(root):
        closed.append(
            record_evolve_event(
                "no-action",
                root,
                event_actor=event_actor,
                trigger=trigger,
                mode=proposal.mode,
                theme=proposal.theme,
                candidate=_CandidateSnapshot(
                    id=proposal.candidate_id,
                    source=proposal.source,
                    initiated_by=proposal.candidate_initiated_by,
                    evidence_source=proposal.evidence_source,
                    signal_actor=proposal.signal_actor,
                    candidate_actor=proposal.candidate_actor,
                    parent_candidate_id=proposal.parent_candidate_id,
                    source_task_id=proposal.source_task_id,
                    score=proposal.score,
                ),
                reason=reason,
                proposal_id=proposal.proposal_id,
            )
        )
    return tuple(closed)


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
    evidence_source = clean_text(str(raw.get("evidence_source") or source)).lower()
    if evidence_source not in EVOLVE_SOURCES:
        evidence_source = source
    signal_actor = _actor(raw.get("signal_actor"))
    candidate_actor = _actor(raw.get("candidate_actor"))
    approval_actor = _actor(raw.get("approval_actor"))
    task_id = _positive_int(raw.get("task_id"))
    if event not in EVOLVE_EVENT_TYPES or actor not in EVOLVE_EVENT_ACTORS:
        return None
    if event in _CANDIDATE_EVENTS and not candidate_id:
        return None
    if candidate_id and (
        source not in EVOLVE_SOURCES or initiated_by not in {"human", "agent"}
    ):
        return None
    if candidate_id:
        signal_actor = signal_actor or _legacy_signal_actor(evidence_source)
        candidate_actor = candidate_actor or "agent"
        if event in {"selected", "queued"} and not approval_actor:
            approval_actor = _legacy_approval_actor(
                clean_text(str(raw.get("trigger") or "")),
                actor,
            )
    if event in {
        "queued",
        "completed",
        "failed",
        "cancelled",
        "regressed",
        "reverted",
        "forward-fixed",
    } and task_id is None:
        return None
    recording_mode = clean_text(str(raw.get("recording_mode") or "")).lower()
    if event in {"promoted", "adopted"} and recording_mode not in RECORDING_MODES:
        return None
    if event == "promoted" and (
        actor != "human"
        or not clean_text(str(raw.get("pr_url") or ""))
        or not clean_text(str(raw.get("merge_commit") or ""))
        or not clean_text(str(raw.get("authoritative_branch") or ""))
        or not clean_text(str(raw.get("promoted_at") or ""))
        or not clean_text(str(raw.get("verified_at") or ""))
    ):
        return None
    if event == "adopted" and (
        not clean_text(str(raw.get("version") or ""))
        or clean_text(str(raw.get("health_check") or "")).lower() != "passed"
        or not clean_text(str(raw.get("verified_at") or ""))
    ):
        return None
    occurred_at = str(raw.get("occurred_at") or "")
    event_id = clean_text(str(raw.get("id") or ""))
    legacy_id = f"legacy-evolve-event-{event}-{candidate_id or task_id or occurred_at}"
    proposal_id = clean_text(str(raw.get("proposal_id") or ""))
    if event == "proposed" and not proposal_id:
        proposal_id = f"legacy-proposal-{event_id or legacy_id}"
    if event == "no-action" and not proposal_id:
        return None
    return EvolveEvent(
        id=event_id or legacy_id,
        occurred_at=occurred_at,
        event=event,
        event_actor=actor,
        trigger=clean_text(str(raw.get("trigger") or "")),
        mode=clean_text(str(raw.get("mode") or "")).lower(),
        theme=clean_text(str(raw.get("theme") or "")),
        proposal_id=proposal_id,
        candidate_id=candidate_id,
        task_id=task_id,
        source=source,
        candidate_initiated_by=initiated_by,
        evidence_source=evidence_source,
        signal_actor=signal_actor,
        candidate_actor=candidate_actor,
        approval_actor=approval_actor,
        parent_candidate_id=clean_text(str(raw.get("parent_candidate_id") or "")),
        source_task_id=_positive_int(raw.get("source_task_id")),
        retry_of_task_id=_positive_int(raw.get("retry_of_task_id")),
        score=_int(raw.get("score")),
        reason=_clip(clean_text(str(raw.get("reason") or ""))),
        pr_url=clean_text(str(raw.get("pr_url") or "")),
        merge_commit=clean_text(str(raw.get("merge_commit") or "")),
        authoritative_branch=clean_text(
            str(raw.get("authoritative_branch") or "")
        ),
        promoted_at=clean_text(str(raw.get("promoted_at") or "")),
        verified_at=clean_text(str(raw.get("verified_at") or "")),
        version=clean_text(str(raw.get("version") or "")),
        health_check=clean_text(str(raw.get("health_check") or "")).lower(),
        recording_mode=recording_mode,
    )


@dataclass(frozen=True)
class _CandidateSnapshot:
    id: str
    source: str
    initiated_by: str
    evidence_source: str
    signal_actor: str
    candidate_actor: str
    parent_candidate_id: str
    source_task_id: int | None
    score: int


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


def _actor(value: object) -> str:
    actor = clean_text(str(value or "")).lower()
    return actor if actor in EVOLVE_EVENT_ACTORS else ""


def _legacy_signal_actor(source: str) -> str:
    if source in {"backlog", "feedback", "learning"}:
        return "human"
    if source in {"inheritance", "brainstorming"}:
        return "agent"
    return "system"


def _legacy_approval_actor(trigger: str, event_actor: str) -> str:
    if trigger == "evolve-scheduler":
        return "agent"
    return event_actor


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
