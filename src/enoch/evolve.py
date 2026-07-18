from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Callable, Iterable

from enoch.backlog import BacklogItem, backlog_status
from enoch.automatic_learning import LearningArtifact, learning_index_path
from enoch.brainstorming import BrainstormIdea, load_brainstorm_ideas
from enoch.cron import CronJob, cron_status, format_cron_interval
from enoch.experience import ExperienceRecord, load_experience_records
from enoch.evolve_events import (
    latest_open_proposal_id,
    linked_proposal_id,
    record_evolve_event,
)
from enoch.feedback import FeedbackSignal, extract_feedback_signals
from enoch.learn import PeerLearningObservation, load_peer_learning_observations
from enoch.lineage.core import LineageCandidate, load_parent_inbox_candidates
from enoch.memory.paths import atomic_write, clean_text, now as current_time
from enoch.paths import enoch_home
from enoch.task_queue import TaskJob, task_queue_status


SCHEMA_VERSION = 1
CANDIDATE_SCHEMA_VERSION = 3
MODE_DISABLED = "disabled"
MODE_CO_EVOLVE = "co-evolve"
MODE_AUTO_EVOLVE = "auto-evolve"
MODES = {MODE_DISABLED, MODE_CO_EVOLVE, MODE_AUTO_EVOLVE}
DEFAULT_MODE = MODE_CO_EVOLVE
CANDIDATE_STATUSES = {
    "candidate",
    "running",
    "done",
    "failed",
    "cancelled",
    "regressed",
    "reverted",
    "forward-fixed",
    "removed",
}
ACTIONABLE_CANDIDATE_STATUSES = {"candidate", "failed"}
VISIBLE_CANDIDATE_STATUSES = {"candidate", "running", "failed"}
AUTO_BRAINSTORM_COOLDOWN_SECONDS = 24 * 60 * 60
FAILED_RETRY_SCORE_BONUS = 30
BrainstormFallback = Callable[[str], Iterable[object]]


@dataclass(frozen=True)
class EvolveState:
    mode: str = DEFAULT_MODE
    theme: str = ""
    updated_at: str = ""
    schedule_enabled: bool = False
    schedule_interval_seconds: int = 0
    schedule_daily_time: str = ""
    schedule_cron_expression: str = ""
    schedule_next_run_at: str = ""
    schedule_last_run_at: str = ""


@dataclass(frozen=True)
class EvolveCandidate:
    id: str
    source: str
    title: str
    rationale: str
    proposed_change: str
    expected_benefit: str
    risk: str
    test_plan: str
    initiated_by: str = "agent"
    status: str = "candidate"
    score: int = 0


@dataclass(frozen=True)
class EvolveReport:
    state: EvolveState
    candidates: tuple[EvolveCandidate, ...]
    top_candidate: EvolveCandidate | None
    counts_by_source: dict[str, int]


@dataclass(frozen=True)
class EvolveProposal:
    report: EvolveReport
    candidates: tuple[EvolveCandidate, ...]
    top_candidate: EvolveCandidate | None
    proposal_id: str = ""
    brainstorm_attempted: bool = False
    brainstorm_added: int = 0
    brainstorm_skip_reason: str = ""
    brainstorm_error: str = ""


def evolve_state_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve.json"


def evolve_candidates_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve_candidates.json"


def evolve_brainstorm_fallback_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve_brainstorm_fallback.json"


def load_evolve_state(root: Path | None = None) -> EvolveState:
    path = evolve_state_path(root)
    if not path.exists():
        return EvolveState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return EvolveState()
    if not isinstance(raw, dict):
        return EvolveState()
    mode = normalize_evolve_mode(str(raw.get("mode") or DEFAULT_MODE))
    theme = clean_text(str(raw.get("theme") or ""))
    updated_at = str(raw.get("updated_at") or "")
    return EvolveState(
        mode=mode,
        theme=theme,
        updated_at=updated_at,
        schedule_enabled=bool(raw.get("schedule_enabled", False)),
        schedule_interval_seconds=max(0, _int(raw.get("schedule_interval_seconds"), default=0)),
        schedule_daily_time=_normalize_daily_time(str(raw.get("schedule_daily_time") or ""), allow_empty=True),
        schedule_cron_expression=_normalize_cron_expression(
            str(raw.get("schedule_cron_expression") or ""),
            allow_empty=True,
        ),
        schedule_next_run_at=str(raw.get("schedule_next_run_at") or ""),
        schedule_last_run_at=str(raw.get("schedule_last_run_at") or ""),
    )


def save_evolve_state(state: EvolveState, root: Path | None = None) -> EvolveState:
    normalized = EvolveState(
        mode=normalize_evolve_mode(state.mode),
        theme=clean_text(state.theme),
        updated_at=state.updated_at or current_time(),
        schedule_enabled=state.schedule_enabled
        and (
            state.schedule_interval_seconds > 0
            or bool(_normalize_daily_time(state.schedule_daily_time, allow_empty=True))
            or bool(_normalize_cron_expression(state.schedule_cron_expression, allow_empty=True))
        ),
        schedule_interval_seconds=max(0, int(state.schedule_interval_seconds)),
        schedule_daily_time=_normalize_daily_time(state.schedule_daily_time, allow_empty=True),
        schedule_cron_expression=_normalize_cron_expression(state.schedule_cron_expression, allow_empty=True),
        schedule_next_run_at=state.schedule_next_run_at,
        schedule_last_run_at=state.schedule_last_run_at,
    )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "mode": normalized.mode,
        "theme": normalized.theme,
        "updated_at": normalized.updated_at,
        "schedule_enabled": normalized.schedule_enabled,
        "schedule_interval_seconds": normalized.schedule_interval_seconds,
        "schedule_daily_time": normalized.schedule_daily_time,
        "schedule_cron_expression": normalized.schedule_cron_expression,
        "schedule_next_run_at": normalized.schedule_next_run_at,
        "schedule_last_run_at": normalized.schedule_last_run_at,
    }
    atomic_write(evolve_state_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return normalized


def set_evolve_mode(mode: str, root: Path | None = None) -> EvolveState:
    current = load_evolve_state(root)
    return save_evolve_state(
        EvolveState(
            mode=normalize_evolve_mode(mode),
            theme=current.theme,
            schedule_enabled=current.schedule_enabled,
            schedule_interval_seconds=current.schedule_interval_seconds,
            schedule_daily_time=current.schedule_daily_time,
            schedule_cron_expression=current.schedule_cron_expression,
            schedule_next_run_at=current.schedule_next_run_at,
            schedule_last_run_at=current.schedule_last_run_at,
        ),
        root,
    )


def set_evolve_theme(theme: str, root: Path | None = None) -> EvolveState:
    current = load_evolve_state(root)
    return save_evolve_state(
        EvolveState(
            mode=current.mode,
            theme=clean_text(theme),
            schedule_enabled=current.schedule_enabled,
            schedule_interval_seconds=current.schedule_interval_seconds,
            schedule_daily_time=current.schedule_daily_time,
            schedule_cron_expression=current.schedule_cron_expression,
            schedule_next_run_at=current.schedule_next_run_at,
            schedule_last_run_at=current.schedule_last_run_at,
        ),
        root,
    )


def set_evolve_schedule(
    interval_seconds: int,
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> EvolveState:
    if interval_seconds <= 0:
        raise ValueError("Evolve schedule interval must be greater than zero.")
    current = load_evolve_state(root)
    current_time = _coerce_utc(now) if now is not None else _utc_now()
    return save_evolve_state(
        EvolveState(
            mode=current.mode,
            theme=current.theme,
            schedule_enabled=True,
            schedule_interval_seconds=interval_seconds,
            schedule_daily_time="",
            schedule_cron_expression="",
            schedule_next_run_at=_iso(current_time + timedelta(seconds=interval_seconds)),
            schedule_last_run_at=current.schedule_last_run_at,
        ),
        root,
    )


def set_evolve_daily_schedule(
    daily_time: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> EvolveState:
    normalized_time = _normalize_daily_time(daily_time)
    hour, minute = _daily_time_parts(normalized_time)
    current = load_evolve_state(root)
    current_time = _coerce_local(now) if now is not None else _local_now()
    return save_evolve_state(
        EvolveState(
            mode=current.mode,
            theme=current.theme,
            schedule_enabled=True,
            schedule_interval_seconds=24 * 60 * 60,
            schedule_daily_time=normalized_time,
            schedule_cron_expression=f"{minute} {hour} * * *",
            schedule_next_run_at=_iso(_next_daily_run(normalized_time, current_time)),
            schedule_last_run_at=current.schedule_last_run_at,
        ),
        root,
    )


def set_evolve_cron_schedule(
    expression: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> EvolveState:
    normalized_expression = _normalize_cron_expression(expression)
    current = load_evolve_state(root)
    current_time = _coerce_local(now) if now is not None else _local_now()
    return save_evolve_state(
        EvolveState(
            mode=current.mode,
            theme=current.theme,
            schedule_enabled=True,
            schedule_interval_seconds=24 * 60 * 60,
            schedule_daily_time="",
            schedule_cron_expression=normalized_expression,
            schedule_next_run_at=_iso(_next_cron_run(normalized_expression, current_time)),
            schedule_last_run_at=current.schedule_last_run_at,
        ),
        root,
    )


def disable_evolve_schedule(root: Path | None = None) -> EvolveState:
    current = load_evolve_state(root)
    return save_evolve_state(EvolveState(mode=current.mode, theme=current.theme), root)


def claim_due_evolve_schedule(root: Path | None = None, *, now: datetime | None = None) -> EvolveState | None:
    state = load_evolve_state(root)
    if not state.schedule_enabled or state.schedule_interval_seconds <= 0:
        return None
    current_source = now if now is not None else _local_now()
    current = _coerce_utc(current_source)
    next_run_at = _parse_time(state.schedule_next_run_at)
    if next_run_at is None or next_run_at > current:
        return None
    claimed = state
    next_run_at = _next_scheduled_run(state, current_source)
    save_evolve_state(
        EvolveState(
            mode=state.mode,
            theme=state.theme,
            schedule_enabled=True,
            schedule_interval_seconds=state.schedule_interval_seconds,
            schedule_daily_time=state.schedule_daily_time,
            schedule_cron_expression=state.schedule_cron_expression,
            schedule_next_run_at=_iso(next_run_at),
            schedule_last_run_at=_iso(current),
        ),
        root,
    )
    return claimed


def normalize_evolve_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in {"co", "coevolve", "co_evolve"}:
        normalized = MODE_CO_EVOLVE
    if normalized in {"auto", "autoevolve", "auto_evolve", "auto-evovle", "auto_evovle", "autoevovle"}:
        normalized = MODE_AUTO_EVOLVE
    if normalized not in MODES:
        raise ValueError("Evolve mode must be disabled, co-evolve, or auto-evolve.")
    return normalized


def evolve_report(root: Path | None = None) -> EvolveReport:
    state = load_evolve_state(root)
    candidates = ()
    if state.mode != MODE_DISABLED:
        candidates = sync_evolve_candidates(root, theme=state.theme)
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.source] = counts.get(candidate.source, 0) + 1
    return EvolveReport(
        state=state,
        candidates=candidates,
        top_candidate=candidates[0] if candidates else None,
        counts_by_source=counts,
    )


def propose_evolve(
    root: Path | None = None,
    *,
    brainstormer: BrainstormFallback | None = None,
    now: datetime | None = None,
) -> EvolveProposal:
    report = evolve_report(root)
    candidates = tuple(
        candidate
        for candidate in report.candidates
        if candidate.status in ACTIONABLE_CANDIDATE_STATUSES
    )
    attempted = False
    added = 0
    skip_reason = ""
    error = ""
    if report.state.mode != MODE_DISABLED and not candidates:
        if any(candidate.status == "running" for candidate in report.candidates):
            skip_reason = "candidate-running"
        elif not report.state.theme:
            skip_reason = "theme-not-set"
        elif brainstormer is None:
            skip_reason = "brainstormer-unavailable"
        elif not _claim_auto_brainstorm(report.state.theme, root, now=now):
            skip_reason = "cooldown"
        else:
            attempted = True
            try:
                added = len(tuple(brainstormer(report.state.theme)))
            except (OSError, RuntimeError, ValueError) as brainstorm_error:
                error = clean_text(str(brainstorm_error)) or brainstorm_error.__class__.__name__
            report = evolve_report(root)
            candidates = tuple(
                candidate
                for candidate in report.candidates
                if candidate.status in ACTIONABLE_CANDIDATE_STATUSES
            )
    return EvolveProposal(
        report=report,
        candidates=candidates,
        top_candidate=candidates[0] if candidates else None,
        brainstorm_attempted=attempted,
        brainstorm_added=added,
        brainstorm_skip_reason=skip_reason,
        brainstorm_error=error,
    )


def _claim_auto_brainstorm(
    theme: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    normalized_theme = clean_text(theme).casefold()
    if not normalized_theme:
        return False
    path = evolve_brainstorm_fallback_path(root)
    attempts: dict[str, str] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        raw_attempts = raw.get("attempts") if isinstance(raw, dict) else None
        if isinstance(raw_attempts, dict):
            attempts = {
                clean_text(str(key)).casefold(): str(value)
                for key, value in raw_attempts.items()
                if clean_text(str(key))
            }
    current = _coerce_utc(now) if now is not None else _utc_now()
    previous = _parse_time(attempts.get(normalized_theme, ""))
    if previous is not None and current - previous < timedelta(seconds=AUTO_BRAINSTORM_COOLDOWN_SECONDS):
        return False
    attempts[normalized_theme] = _iso(current)
    payload = {
        "schema_version": 1,
        "attempts": attempts,
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return True


def collect_evolve_candidates(
    root: Path | None = None,
    *,
    theme: str = "",
) -> tuple[EvolveCandidate, ...]:
    candidates: list[EvolveCandidate] = []
    candidates.extend(_backlog_candidates(backlog_status(root).pending))
    candidates.extend(_feedback_candidates(extract_feedback_signals(root)))
    candidates.extend(_inheritance_candidates(load_parent_inbox_candidates(root)))
    candidates.extend(collect_experience_candidates(root))
    candidates.extend(_peer_learning_candidates(load_peer_learning_observations(root)))
    candidates.extend(_brainstorm_candidates(load_brainstorm_ideas(root, theme=theme)))
    return tuple(candidates)


def collect_experience_candidates(root: Path | None = None) -> tuple[EvolveCandidate, ...]:
    candidates: list[EvolveCandidate] = []
    records = load_experience_records(root)
    candidates.extend(_experience_record_candidates(records))
    recorded_task_ids = {record.task_id for record in records}
    candidates.extend(
        _task_history_candidates(
            job for job in task_queue_status(root).history if job.id not in recorded_task_ids
        )
    )
    candidates.extend(_repeated_success_candidates(records))
    candidates.extend(_cron_candidates(cron_status(root).active))
    candidates.extend(_learning_candidates(_load_learning_artifacts(root)))
    return tuple(candidates)


def sync_evolve_candidates(root: Path | None = None, *, theme: str = "") -> tuple[EvolveCandidate, ...]:
    stored = {candidate.id: candidate for candidate in _load_all_evolve_candidates(root)}
    collected = collect_evolve_candidates(root, theme=theme)
    collected_ids = {candidate.id for candidate in collected}
    merged: dict[str, EvolveCandidate] = {
        candidate_id: candidate
        for candidate_id, candidate in stored.items()
        if not (
            candidate.source == "brainstorming"
            and candidate.status in VISIBLE_CANDIDATE_STATUSES
            and candidate_id not in collected_ids
        )
    }
    for candidate in collected:
        previous = stored.get(candidate.id)
        status = previous.status if previous is not None else candidate.status
        merged[candidate.id] = EvolveCandidate(**{**candidate.__dict__, "status": status})
    ranked = rank_evolve_candidates(merged.values(), theme=theme)
    _write_evolve_candidates(ranked, root)
    return tuple(candidate for candidate in ranked if candidate.status in VISIBLE_CANDIDATE_STATUSES)


def load_evolve_candidates(
    root: Path | None = None,
    *,
    include_inactive: bool = False,
    theme: str = "",
) -> tuple[EvolveCandidate, ...]:
    candidates = rank_evolve_candidates(_load_all_evolve_candidates(root), theme=theme)
    if include_inactive:
        return candidates
    return tuple(candidate for candidate in candidates if candidate.status in VISIBLE_CANDIDATE_STATUSES)


def get_evolve_candidate(candidate_id: str, root: Path | None = None, *, theme: str = "") -> EvolveCandidate:
    candidates = list(sync_evolve_candidates(root, theme=theme))
    candidates.extend(candidate for candidate in _load_all_evolve_candidates(root) if candidate.status not in VISIBLE_CANDIDATE_STATUSES)
    for candidate in candidates:
        if _candidate_matches_id(candidate, candidate_id):
            return _score_candidate(candidate, theme=theme)
    raise ValueError(f"No evolve candidate found for {candidate_id}.")


def remove_evolve_candidate(
    candidate_id: str,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "human",
    trigger: str = "/evolve remove",
) -> EvolveCandidate:
    candidate = get_evolve_candidate(candidate_id, root, theme=theme)
    if candidate.status not in ACTIONABLE_CANDIDATE_STATUSES:
        raise ValueError(f"Evolve candidate {candidate.id} cannot be removed from status {candidate.status}.")
    removed = _set_candidate_status(candidate.id, "removed", root, theme=theme)
    _record_candidate_event_safely(
        "removed",
        removed,
        root,
        event_actor=event_actor,
        trigger=trigger,
        theme=theme,
        proposal_id=latest_open_proposal_id(removed.id, root),
    )
    return removed


def run_evolve_candidate(candidate_id: str, root: Path | None = None, *, theme: str = "") -> EvolveCandidate:
    candidate = get_evolve_candidate(candidate_id, root, theme=theme)
    if candidate.status != "candidate":
        raise ValueError(f"Evolve candidate {candidate.id} cannot run from status {candidate.status}.")
    return _set_candidate_status(candidate.id, "running", root, theme=theme)


def retry_evolve_candidate(candidate_id: str, root: Path | None = None, *, theme: str = "") -> EvolveCandidate:
    candidate = get_evolve_candidate(candidate_id, root, theme=theme)
    if candidate.status != "failed":
        raise ValueError(f"Evolve candidate {candidate.id} cannot retry from status {candidate.status}.")
    return _set_candidate_status(candidate.id, "running", root, theme=theme)


def latest_failed_evolve_task(
    candidate_id: str,
    root: Path | None = None,
) -> TaskJob | None:
    normalized_id = candidate_id.strip().lower().lstrip("#")
    matches = [
        job
        for job in task_queue_status(root).history
        if job.status == "failed"
        and _evolve_candidate_id_from_task(job).lower() == normalized_id
    ]
    return max(matches, key=lambda job: job.id, default=None)


def complete_evolve_candidate(candidate_id: str, root: Path | None = None, *, theme: str = "") -> EvolveCandidate:
    return _set_candidate_status(candidate_id, "done", root, theme=theme)


def fail_evolve_candidate(candidate_id: str, root: Path | None = None, *, theme: str = "") -> EvolveCandidate:
    return _set_candidate_status(candidate_id, "failed", root, theme=theme)


def cancel_evolve_candidate(candidate_id: str, root: Path | None = None, *, theme: str = "") -> EvolveCandidate:
    return _set_candidate_status(candidate_id, "cancelled", root, theme=theme)


def complete_evolve_candidate_for_task(
    job: TaskJob,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "agent",
    trigger: str = "task-runner",
    reason: str = "",
) -> EvolveCandidate | None:
    candidate_id = _evolve_candidate_id_from_task(job)
    if not candidate_id:
        return None
    try:
        candidate = complete_evolve_candidate(candidate_id, root, theme=theme)
    except ValueError:
        return None
    _record_candidate_event_safely(
        "completed",
        candidate,
        root,
        event_actor=event_actor,
        trigger=trigger,
        theme=theme,
        task_id=job.id,
        reason=reason,
    )
    return candidate


def fail_evolve_candidate_for_task(
    job: TaskJob,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "agent",
    trigger: str = "task-runner",
    reason: str = "",
) -> EvolveCandidate | None:
    candidate_id = _evolve_candidate_id_from_task(job)
    if not candidate_id:
        return None
    try:
        candidate = fail_evolve_candidate(candidate_id, root, theme=theme)
    except ValueError:
        return None
    _record_candidate_event_safely(
        "failed",
        candidate,
        root,
        event_actor=event_actor,
        trigger=trigger,
        theme=theme,
        task_id=job.id,
        reason=reason,
    )
    return candidate


def cancel_evolve_candidate_for_task(
    job: TaskJob,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "human",
    trigger: str = "/stop",
    reason: str = "",
) -> EvolveCandidate | None:
    candidate_id = _evolve_candidate_id_from_task(job)
    if not candidate_id:
        return None
    try:
        candidate = cancel_evolve_candidate(candidate_id, root, theme=theme)
    except ValueError:
        return None
    _record_candidate_event_safely(
        "cancelled",
        candidate,
        root,
        event_actor=event_actor,
        trigger=trigger,
        theme=theme,
        task_id=job.id,
        reason=reason,
    )
    return candidate


def pause_evolve_candidate_for_task(
    job: TaskJob,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "system",
    trigger: str = "codex-unavailable",
    reason: str = "",
) -> EvolveCandidate | None:
    return _record_evolve_candidate_task_event(
        job,
        "paused",
        root,
        theme=theme,
        event_actor=event_actor,
        trigger=trigger,
        reason=reason,
    )


def resume_evolve_candidate_for_task(
    job: TaskJob,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "human",
    trigger: str = "/resume",
    reason: str = "",
) -> EvolveCandidate | None:
    return _record_evolve_candidate_task_event(
        job,
        "resumed",
        root,
        theme=theme,
        event_actor=event_actor,
        trigger=trigger,
        reason=reason,
    )


def _record_evolve_candidate_task_event(
    job: TaskJob,
    event: str,
    root: Path | None,
    *,
    theme: str,
    event_actor: str,
    trigger: str,
    reason: str,
) -> EvolveCandidate | None:
    candidate_id = _evolve_candidate_id_from_task(job)
    if not candidate_id:
        return None
    try:
        candidate = get_evolve_candidate(candidate_id, root, theme=theme)
    except ValueError:
        return None
    _record_candidate_event_safely(
        event,
        candidate,
        root,
        event_actor=event_actor,
        trigger=trigger,
        theme=theme,
        task_id=job.id,
        reason=reason,
    )
    return candidate


def regress_evolve_candidate_for_task(
    job: TaskJob,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "agent",
    trigger: str = "agent-regression-signal",
    reason: str = "",
) -> EvolveCandidate | None:
    return _transition_evolve_candidate_for_task(
        job,
        "regressed",
        root,
        theme=theme,
        event_actor=event_actor,
        trigger=trigger,
        reason=reason,
    )


def resolve_evolve_candidate_regression_for_task(
    job: TaskJob,
    resolution: str,
    root: Path | None = None,
    *,
    theme: str = "",
    event_actor: str = "agent",
    trigger: str = "agent-regression-signal",
    reason: str = "",
) -> EvolveCandidate | None:
    normalized_resolution = resolution.strip().lower()
    if normalized_resolution not in {"reverted", "forward-fixed"}:
        raise ValueError("Evolve regression resolution must be reverted or forward-fixed.")
    return _transition_evolve_candidate_for_task(
        job,
        normalized_resolution,
        root,
        theme=theme,
        event_actor=event_actor,
        trigger=trigger,
        reason=reason,
    )


def _transition_evolve_candidate_for_task(
    job: TaskJob,
    status: str,
    root: Path | None,
    *,
    theme: str,
    event_actor: str,
    trigger: str,
    reason: str,
) -> EvolveCandidate | None:
    candidate_id = _evolve_candidate_id_from_task(job)
    if not candidate_id:
        return None
    try:
        candidate = _set_candidate_status(candidate_id, status, root, theme=theme)
    except ValueError:
        return None
    _record_candidate_event_safely(
        status,
        candidate,
        root,
        event_actor=event_actor,
        trigger=trigger,
        theme=theme,
        task_id=job.id,
        reason=reason,
    )
    return candidate


def _record_candidate_event_safely(
    event: str,
    candidate: EvolveCandidate,
    root: Path | None,
    *,
    event_actor: str,
    trigger: str,
    theme: str,
    task_id: int | None = None,
    reason: str = "",
    proposal_id: str = "",
) -> None:
    state = load_evolve_state(root)
    linked_id = proposal_id or linked_proposal_id(
        root,
        candidate_id=candidate.id,
        task_id=task_id,
    )
    try:
        record_evolve_event(
            event,
            root,
            event_actor=event_actor,
            trigger=trigger,
            mode=state.mode,
            theme=theme or state.theme,
            candidate=candidate,
            task_id=task_id,
            reason=reason,
            proposal_id=linked_id,
        )
    except (OSError, ValueError):
        return


def rank_evolve_candidates(
    candidates: Iterable[EvolveCandidate],
    *,
    theme: str = "",
) -> tuple[EvolveCandidate, ...]:
    scored = [_score_candidate(candidate, theme=theme) for candidate in candidates]
    return tuple(sorted(scored, key=lambda item: (_candidate_status_order(item.status), -item.score, item.source, item.id)))


def _load_all_evolve_candidates(root: Path | None = None) -> tuple[EvolveCandidate, ...]:
    path = evolve_candidates_path(root)
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    if not isinstance(raw, dict):
        return ()
    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list):
        return ()
    candidates = []
    for raw_candidate in raw_candidates:
        if isinstance(raw_candidate, dict):
            candidate = _candidate_from_json(raw_candidate)
            if candidate is not None:
                candidates.append(candidate)
    return tuple(candidates)


def _write_evolve_candidates(candidates: Iterable[EvolveCandidate], root: Path | None = None) -> None:
    payload = {
        "schema_version": CANDIDATE_SCHEMA_VERSION,
        "updated_at": current_time(),
        "candidates": [_candidate_to_json(candidate) for candidate in candidates],
    }
    atomic_write(evolve_candidates_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _set_candidate_status(
    candidate_id: str,
    status: str,
    root: Path | None = None,
    *,
    theme: str = "",
) -> EvolveCandidate:
    if status not in CANDIDATE_STATUSES:
        raise ValueError(f"Evolve candidate status must be one of: {', '.join(sorted(CANDIDATE_STATUSES))}.")
    candidates = list(sync_evolve_candidates(root, theme=theme))
    inactive = [candidate for candidate in _load_all_evolve_candidates(root) if candidate.status not in VISIBLE_CANDIDATE_STATUSES]
    candidates.extend(inactive)
    for index, candidate in enumerate(candidates):
        if _candidate_matches_id(candidate, candidate_id):
            updated = EvolveCandidate(**{**candidate.__dict__, "status": status})
            candidates[index] = updated
            ranked = rank_evolve_candidates(candidates, theme=theme)
            _write_evolve_candidates(ranked, root)
            return _score_candidate(updated, theme=theme)
    raise ValueError(f"No evolve candidate found for {candidate_id}.")


def _candidate_to_json(candidate: EvolveCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "source": candidate.source,
        "title": candidate.title,
        "rationale": candidate.rationale,
        "proposed_change": candidate.proposed_change,
        "expected_benefit": candidate.expected_benefit,
        "risk": candidate.risk,
        "test_plan": candidate.test_plan,
        "initiated_by": candidate.initiated_by if candidate.initiated_by in {"human", "agent"} else "agent",
        "status": candidate.status if candidate.status in CANDIDATE_STATUSES else "candidate",
        "score": int(candidate.score),
    }


def _candidate_from_json(raw: dict[str, object]) -> EvolveCandidate | None:
    candidate_id = clean_text(str(raw.get("id") or ""))
    title = clean_text(str(raw.get("title") or ""))
    if not candidate_id or not title:
        return None
    status = clean_text(str(raw.get("status") or "candidate")).lower()
    if status == "selected":
        status = "candidate"
    if status == "rejected":
        status = "removed"
    if status not in CANDIDATE_STATUSES:
        status = "candidate"
    source = clean_text(str(raw.get("source") or "unknown")) or "unknown"
    if source in {"task-history", "cron"}:
        source = "experience"
    if source == "learning" and not candidate_id.startswith("learning-peer-"):
        source = "experience"
    initiated_by = clean_text(str(raw.get("initiated_by") or "")).lower()
    if initiated_by not in {"human", "agent"}:
        initiated_by = _candidate_initiator(source)
    return EvolveCandidate(
        id=candidate_id,
        source=source,
        title=title,
        rationale=clean_text(str(raw.get("rationale") or "")),
        proposed_change=clean_text(str(raw.get("proposed_change") or "")),
        expected_benefit=clean_text(str(raw.get("expected_benefit") or "")),
        risk=clean_text(str(raw.get("risk") or "")),
        test_plan=clean_text(str(raw.get("test_plan") or "")),
        initiated_by=initiated_by,
        status=status,
        score=_int(raw.get("score"), default=0),
    )


def _candidate_matches_id(candidate: EvolveCandidate, candidate_id: str) -> bool:
    normalized = candidate_id.strip().lower().lstrip("#")
    return candidate.id.lower() == normalized or candidate.id.lower().split("-", 1)[-1] == normalized


def _evolve_candidate_id_from_task(job: TaskJob) -> str:
    if job.candidate_id:
        return job.candidate_id
    if job.context_source not in {"evolve-approve", "evolve-retry", "evolve-run", "evolve-scheduler"}:
        return ""
    for raw_line in job.context.splitlines():
        label, separator, value = raw_line.partition(":")
        if separator and label.strip().lower() == "id":
            return clean_text(value)
    return ""


def _candidate_status_order(status: str) -> int:
    return {
        "running": 0,
        "candidate": 1,
        "done": 2,
        "failed": 1,
        "cancelled": 4,
        "regressed": 5,
        "reverted": 6,
        "forward-fixed": 7,
        "removed": 8,
    }.get(status, 1)


def _candidate_initiator(source: str) -> str:
    return "human" if source in {"backlog", "feedback", "learning"} else "agent"


def _backlog_candidates(items: Iterable[BacklogItem]) -> list[EvolveCandidate]:
    priority_score = {"p0": 35, "p1": 25, "p2": 15}
    candidates = []
    for item in items:
        candidates.append(
            EvolveCandidate(
                id=f"backlog-{item.id}",
                source="backlog",
                title=item.text,
                rationale=f"Pending {item.priority} backlog item.",
                proposed_change=item.text,
                expected_benefit="Completes deferred human-visible work that may improve Enoch's body or workflow.",
                risk="Backlog item may need clarification before implementation.",
                test_plan="Run focused tests for the changed behavior and Enoch doctor if code changes.",
                initiated_by="human",
                score=priority_score.get(item.priority, 10),
            )
        )
    return candidates


def _inheritance_candidates(items: Iterable[LineageCandidate]) -> list[EvolveCandidate]:
    relevance_score = {"high": 32, "medium": 22, "low": 8}
    candidates = []
    for item in items:
        candidates.append(
            EvolveCandidate(
                id=f"inheritance-{item.id}",
                source="inheritance",
                title=item.title,
                rationale=f"Direct-parent change from {item.repo}; relevance {item.relevance}. {item.reason}",
                proposed_change=f"Inspect and adapt direct-parent change {item.id}.",
                expected_benefit="Keeps Enoch aligned with useful parent improvements without blindly copying them.",
                risk="Parent change may not apply cleanly to Enoch or may duplicate existing behavior.",
                test_plan="Inspect changed files, adapt only relevant pieces, then run affected tests.",
                initiated_by="agent",
                score=relevance_score.get(item.relevance, 8),
            )
        )
    return candidates


def _feedback_candidates(items: Iterable[FeedbackSignal]) -> list[EvolveCandidate]:
    kind_score = {"complaint": 34, "correction": 32, "repeated-request": 28, "preference": 24}
    candidates = []
    for item in items:
        title = item.message if len(item.message) <= 120 else f"{item.message[:117].rstrip()}..."
        candidates.append(
            EvolveCandidate(
                id=f"feedback-{item.id}",
                source="feedback",
                title=f"Respond to {item.kind}: {title}",
                rationale=(
                    f"Conversation feedback classified as {item.kind}; observed {item.occurrences} time(s)."
                ),
                proposed_change=(
                    "Inspect the conversation context and make the smallest durable body or workflow change that "
                    "addresses this feedback without encoding private conversation content."
                ),
                expected_benefit="Turns explicit human feedback into an accountable, testable improvement candidate.",
                risk="Heuristic feedback extraction can misclassify ordinary conversation; confirm intent before editing.",
                test_plan="Add or update focused tests for the affected behavior and verify the feedback is addressed.",
                initiated_by="human",
                score=kind_score.get(item.kind, 20) + min(item.occurrences, 3) * 2,
            )
        )
    return candidates


def _task_history_candidates(items: Iterable[TaskJob]) -> list[EvolveCandidate]:
    candidates = []
    for item in items:
        if item.status not in {"failed", "cancelled", "regressed", "reverted"}:
            continue
        if item.status == "cancelled" and not item.started_at:
            continue
        result = clean_text(item.result)
        is_failed = item.status == "failed"
        is_regression = item.status in {"regressed", "reverted"}
        candidates.append(
            EvolveCandidate(
                id=f"task-{item.id}",
                source="experience",
                title=f"Improve reliability after {item.status} task #{item.id}: {item.text}",
                rationale=(
                    f"Task #{item.id} ended as {item.status}."
                    + (f" Result: {result}" if result else "")
                ),
                proposed_change=(
                    "Inspect the task request, result, and surrounding workflow; add a small fix or guardrail that "
                    "prevents similar work from failing again."
                ),
                expected_benefit="Turns recent operational friction into a concrete reliability improvement.",
                risk="The original task may have failed for transient reasons, so avoid broad changes without evidence.",
                test_plan="Reproduce the failed or cancelled path if possible, then run focused tests around the changed workflow.",
                initiated_by="agent",
                score=40 if is_regression else (30 if is_failed else 18),
            )
        )
    return candidates


def _experience_record_candidates(items: Iterable[ExperienceRecord]) -> list[EvolveCandidate]:
    candidates = []
    for item in items:
        is_actionable_regression = item.regressed and item.regression_resolution != "forward-fixed"
        if item.outcome not in {"failed", "cancelled"} and not is_actionable_regression:
            continue
        if item.outcome == "cancelled" and not item.started:
            continue
        is_failed = item.outcome == "failed"
        outcome = item.regression_resolution or "regressed" if is_actionable_regression else item.outcome
        candidates.append(
            EvolveCandidate(
                id=f"task-{item.task_id}",
                source="experience",
                title=f"Improve reliability after {outcome} task #{item.task_id}: {item.request}",
                rationale=(
                    f"Experience journal recorded task #{item.task_id} as {outcome}."
                    + (f" Result: {clean_text(item.result_summary)}" if item.result_summary else "")
                ),
                proposed_change=(
                    "Inspect the recorded task experience and add the smallest fix or guardrail that prevents "
                    "similar work from failing again."
                ),
                expected_benefit="Turns a durable operational experience into a concrete reliability improvement.",
                risk="The task may have failed for transient reasons, so confirm the lesson before changing behavior.",
                test_plan="Reproduce the recorded failure if possible, then run focused tests around the changed workflow.",
                initiated_by="agent",
                score=42 if is_actionable_regression else (30 if is_failed else 18),
            )
        )
    return candidates


def _repeated_success_candidates(items: Iterable[ExperienceRecord]) -> list[EvolveCandidate]:
    grouped: dict[str, list[ExperienceRecord]] = {}
    for item in items:
        if item.outcome != "completed":
            continue
        if item.context_source.startswith("cron") or item.context_source in {
            "evolve-approve",
            "evolve-run",
            "evolve-scheduler",
        }:
            continue
        key = clean_text(item.request).lower()
        grouped.setdefault(key, []).append(item)
    candidates = []
    for key, records in grouped.items():
        if len(records) < 2:
            continue
        latest = records[0]
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
        candidates.append(
            EvolveCandidate(
                id=f"experience-repeat-{digest}",
                source="experience",
                title=f"Review repeated successful workflow: {latest.request}",
                rationale=f"The experience journal records this workflow completing successfully {len(records)} times.",
                proposed_change=(
                    "Inspect the successful runs and decide whether a reusable command, skill, template, or automation "
                    "would preserve the proven pattern without overfitting."
                ),
                expected_benefit="Converts repeated successful work into reusable capability when the evidence supports it.",
                risk="Repeated requests may still differ in important context; do not automate away necessary judgment.",
                test_plan="Add focused tests for any extracted reusable behavior and verify existing task execution remains intact.",
                initiated_by="agent",
                score=18 + min(len(records), 5),
            )
        )
    return candidates


def _cron_candidates(items: Iterable[CronJob]) -> list[EvolveCandidate]:
    candidates = []
    for item in items:
        cadence = format_cron_interval(item.interval_seconds)
        candidates.append(
            EvolveCandidate(
                id=f"cron-{item.id}",
                source="experience",
                title=f"Review recurring workflow #{item.id}: {item.text}",
                rationale=(
                    f"Active cron job runs every {cadence}; next run {item.next_run_at or 'unknown'}."
                    + (f" Last task #{item.last_task_id}." if item.last_task_id is not None else "")
                ),
                proposed_change=(
                    "Inspect whether the recurring request is still useful, has enough context, and should be made "
                    "safer or more observable."
                ),
                expected_benefit="Keeps scheduled automation aligned with current needs instead of letting stale jobs drift.",
                risk="Recurring jobs are user-facing automation; changes should not silently disable or broaden their behavior.",
                test_plan="Run cron parsing/status tests and verify the scheduled request still renders clearly in Telegram.",
                initiated_by="agent",
                score=12,
            )
        )
    return candidates


def _learning_candidates(items: Iterable[LearningArtifact]) -> list[EvolveCandidate]:
    candidates = []
    for item in items:
        skills = ", ".join(item.skill_names) or "unknown"
        candidates.append(
            EvolveCandidate(
                id=f"learning-{item.id}",
                source="experience",
                title=f"Turn learned skill work into reusable behavior: {skills}",
                rationale=f"Learning artifact from task {item.task_id or 'unknown'} recorded skill work for {skills}.",
                proposed_change=(
                    "Inspect the learning artifact and decide whether Enoch should adapt docs, tests, or command behavior "
                    "from that successful skill change."
                ),
                expected_benefit="Promotes successful skill work into deliberate self-improvement instead of passive archive data.",
                risk="The artifact may be too context-specific to generalize; adapt only reusable pieces.",
                test_plan="Run skill discovery tests and focused tests for any adapted skill behavior.",
                initiated_by="agent",
                score=20,
            )
        )
    return candidates


def _peer_learning_candidates(items: Iterable[PeerLearningObservation]) -> list[EvolveCandidate]:
    candidates = []
    for item in items:
        candidates.append(
            EvolveCandidate(
                id=f"learning-{item.id}",
                source="learning",
                title=f"Explore and adapt {item.agent}'s {item.skill} skill",
                rationale=f"A non-parent agent skill was inspected from {item.agent} at {item.created_at or 'unknown time'}.",
                proposed_change=(
                    f"Re-inspect {item.agent}'s published {item.skill} skill, adapt only mission-relevant ideas, "
                    "and preserve Enoch-specific behavior."
                ),
                expected_benefit="Allows horizontal capability learning without treating a peer as an ancestor.",
                risk="The peer skill may be incompatible, stale, or too specific to the source agent.",
                test_plan="Verify skill discovery and run focused tests for every adapted behavior.",
                initiated_by="human",
                score=22,
            )
        )
    return candidates


def _brainstorm_candidates(items: Iterable[BrainstormIdea]) -> list[EvolveCandidate]:
    return [
        EvolveCandidate(
            id=item.id,
            source="brainstorming",
            title=item.title,
            rationale=f"Theme-guided LLM idea for '{item.theme}'. {item.rationale}",
            proposed_change=item.proposed_change,
            expected_benefit=item.expected_benefit,
            risk=item.risk,
            test_plan=item.test_plan,
            initiated_by="agent",
            score=16,
        )
        for item in items
    ]


def _load_learning_artifacts(root: Path | None = None, *, limit: int = 10) -> tuple[LearningArtifact, ...]:
    path = learning_index_path(root)
    if not path.exists():
        return ()
    artifacts = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        artifact = _learning_artifact_from_json(raw)
        if artifact is not None:
            artifacts.append(artifact)
        if len(artifacts) >= limit:
            break
    return tuple(artifacts)


def _learning_artifact_from_json(raw: object) -> LearningArtifact | None:
    if not isinstance(raw, dict):
        return None
    artifact_id = clean_text(str(raw.get("id") or ""))
    request = clean_text(str(raw.get("request") or ""))
    if not artifact_id or not request:
        return None
    return LearningArtifact(
        id=artifact_id,
        artifact_type=clean_text(str(raw.get("artifact_type") or "skill")) or "skill",
        source_agent=clean_text(str(raw.get("source_agent") or "")),
        created_at=str(raw.get("created_at") or ""),
        task_id=_optional_int(raw.get("task_id")),
        command=clean_text(str(raw.get("command") or "")),
        request=request,
        result_summary=clean_text(str(raw.get("result_summary") or "")),
        pr_urls=_string_tuple(raw.get("pr_urls")),
        changed_files=_string_tuple(raw.get("changed_files")),
        skill_names=_string_tuple(raw.get("skill_names")),
        context_source=clean_text(str(raw.get("context_source") or "")),
    )


def _score_candidate(candidate: EvolveCandidate, *, theme: str) -> EvolveCandidate:
    score = candidate.score + 25
    text = " ".join([candidate.title, candidate.rationale, candidate.proposed_change]).lower()
    theme_words = {word for word in clean_text(theme).lower().split() if len(word) >= 4}
    if theme_words and any(word in text for word in theme_words):
        score += 20
    if candidate.source == "backlog":
        score += 10
    if candidate.source == "inheritance":
        score += 8
    if candidate.source == "experience":
        score += 12
    if candidate.source == "learning":
        score += 6
    if candidate.source == "feedback":
        score += 10
    if candidate.source == "brainstorming":
        score += 4
    if candidate.status == "failed":
        score += FAILED_RETRY_SCORE_BONUS
    return EvolveCandidate(**{**candidate.__dict__, "score": score})


def _optional_int(value: object) -> int | None:
    parsed = _int(value, default=0)
    return parsed if parsed > 0 else None


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    output = []
    for item in value:
        cleaned = clean_text(str(item or ""))
        if cleaned:
            output.append(cleaned)
    return tuple(output)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _local_now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc, microsecond=0)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def _coerce_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.astimezone().replace(microsecond=0)
    return value.replace(microsecond=0)


def _iso(value: datetime) -> str:
    return _coerce_utc(value).isoformat()


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _coerce_utc(parsed)


def _int(value: object, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _next_scheduled_run(state: EvolveState, current: datetime) -> datetime:
    if state.schedule_daily_time:
        return _next_daily_run(state.schedule_daily_time, current)
    if state.schedule_cron_expression:
        return _next_cron_run(state.schedule_cron_expression, current)
    return current + timedelta(seconds=state.schedule_interval_seconds)


def _next_daily_run(daily_time: str, current: datetime) -> datetime:
    hour, minute = _daily_time_parts(daily_time)
    local_current = _coerce_local(current)
    candidate = local_current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_current:
        candidate += timedelta(days=1)
    return candidate


def _normalize_daily_time(value: str, *, allow_empty: bool = False) -> str:
    cleaned = value.strip()
    if not cleaned and allow_empty:
        return ""
    hour, minute = _daily_time_parts(cleaned)
    return f"{hour:02d}:{minute:02d}"


def _daily_time_parts(value: str) -> tuple[int, int]:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError("Evolve daily schedule time must look like HH:MM.")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError as error:
        raise ValueError("Evolve daily schedule time must look like HH:MM.") from error
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Evolve daily schedule time must use 00:00 through 23:59.")
    return hour, minute


def _next_cron_run(expression: str, current: datetime) -> datetime:
    minute, hour = _cron_daily_parts(expression)
    local_current = _coerce_local(current)
    candidate = local_current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_current:
        candidate += timedelta(days=1)
    return candidate


def _normalize_cron_expression(value: str, *, allow_empty: bool = False) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned and allow_empty:
        return ""
    minute, hour = _cron_daily_parts(cleaned)
    return f"{minute} {hour} * * *"


def _cron_daily_parts(value: str) -> tuple[int, int]:
    parts = value.strip().split()
    if len(parts) != 5:
        raise ValueError("Evolve cron schedule must look like: minute hour * * *.")
    minute_text, hour_text, day_of_month, month, day_of_week = parts
    if (day_of_month, month, day_of_week) != ("*", "*", "*"):
        raise ValueError("Evolve cron schedule currently supports daily expressions like: 30 9 * * *.")
    try:
        minute = int(minute_text)
        hour = int(hour_text)
    except ValueError as error:
        raise ValueError("Evolve cron schedule minute and hour must be whole numbers.") from error
    if minute < 0 or minute > 59 or hour < 0 or hour > 23:
        raise ValueError("Evolve cron schedule minute/hour must be within 0-59 and 0-23.")
    return minute, hour
