from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Callable, Iterable
from uuid import uuid4

from enoch.evolution.sources.brainstorming import (
    BrainstormIdea,
    brainstorm_idea,
    load_brainstorm_ideas,
    save_brainstorm_ideas,
)
from enoch.evolution.curation import (
    DEFAULT_CURATION_LIMIT,
    CurationGenerator,
    REMOVE_CLASSIFICATIONS,
    SemanticCuration,
    curate_candidates,
    deterministic_fallback,
    load_curations,
    recent_completion_evidence,
    record_curation,
    sanitize_curation_text,
    with_new_candidate_ids,
)
from enoch.evolution.evidence import (
    EvidenceCandidateDraft,
    EvidenceScanResult,
    EvidenceSettings,
    link_evidence,
    load_evidence,
    load_evidence_settings,
    pending_evidence_counts,
    synthesize_evidence_candidates,
    unlinked_evidence,
)
from enoch.evolution.events import (
    latest_open_proposal_id,
    linked_proposal_id,
    record_evolve_event,
)
from enoch.learn import PeerLearningObservation, load_peer_learning_observations
from enoch.lineage.core import LineageCandidate, load_parent_inbox_candidates
from enoch.memory.paths import atomic_write, clean_text, now as current_time
from enoch.paths import private_state_path
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import TaskJob, task_queue_status
from enoch.state import StateCorruptionError, file_transaction, load_json_object


SCHEMA_VERSION = 2
CANDIDATE_SCHEMA_VERSION = 5
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
RETIRED_CANDIDATE_SOURCES = {"backlog"}
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
    schedule_claim_id: str = ""
    schedule_claimed_at: str = ""


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
    evidence_source: str = ""
    signal_actor: str = "system"
    candidate_actor: str = "agent"
    parent_candidate_id: str = ""
    source_task_id: int | None = None
    status: str = "candidate"
    score: int = 0
    base_score: int | None = None
    evidence_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvolveReport:
    state: EvolveState
    candidates: tuple[EvolveCandidate, ...]
    top_candidate: EvolveCandidate | None
    counts_by_source: dict[str, int]
    evidence_counts: dict[str, int] = field(default_factory=dict)
    pending_evidence: dict[str, int] = field(default_factory=dict)
    evidence_settings: EvidenceSettings = field(default_factory=EvidenceSettings)


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
    curation: SemanticCuration | None = None
    new_candidates: tuple[EvolveCandidate, ...] = ()
    evidence_scan_results: tuple[EvidenceScanResult, ...] = ()
    evidence_candidates_added: int = 0
    evidence_synthesis_error: str = ""


def evolve_state_path(root: Path | None = None) -> Path:
    return private_state_path("evolve.json", root)


def evolve_candidates_path(root: Path | None = None) -> Path:
    return private_state_path("evolve_candidates.json", root)


def evolve_brainstorm_fallback_path(root: Path | None = None) -> Path:
    return private_state_path("evolve_brainstorm_fallback.json", root)


def load_evolve_state(root: Path | None = None) -> EvolveState:
    with file_transaction(evolve_state_path(root)):
        return _load_evolve_state_unlocked(root)


def _load_evolve_state_unlocked(root: Path | None = None) -> EvolveState:
    path = evolve_state_path(root)
    raw = load_json_object(path)
    if not raw:
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
        schedule_claim_id=str(raw.get("schedule_claim_id") or ""),
        schedule_claimed_at=str(raw.get("schedule_claimed_at") or ""),
    )


def save_evolve_state(state: EvolveState, root: Path | None = None) -> EvolveState:
    with file_transaction(evolve_state_path(root)):
        return _save_evolve_state_unlocked(state, root)


def _save_evolve_state_unlocked(
    state: EvolveState,
    root: Path | None = None,
) -> EvolveState:
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
        schedule_claim_id=state.schedule_claim_id,
        schedule_claimed_at=state.schedule_claimed_at,
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
        "schedule_claim_id": normalized.schedule_claim_id,
        "schedule_claimed_at": normalized.schedule_claimed_at,
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
    path = evolve_state_path(root)
    with file_transaction(path):
        state = _load_evolve_state_unlocked(root)
        if not state.schedule_enabled or state.schedule_interval_seconds <= 0:
            return None
        if state.schedule_claim_id:
            return state
        current_source = now if now is not None else _local_now()
        current = _coerce_utc(current_source)
        next_run_at = _parse_time(state.schedule_next_run_at)
        if next_run_at is None or next_run_at > current:
            return None
        claimed = EvolveState(
            **{
                **state.__dict__,
                "schedule_claim_id": f"evolve-{uuid4().hex}",
                "schedule_claimed_at": _iso(current),
            }
        )
        _save_evolve_state_unlocked(claimed, root)
        return claimed


def acknowledge_evolve_schedule(
    claim_id: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> EvolveState | None:
    path = evolve_state_path(root)
    with file_transaction(path):
        state = _load_evolve_state_unlocked(root)
        if not claim_id.strip() or state.schedule_claim_id != claim_id.strip():
            return None
        current_source = now if now is not None else _local_now()
        current = _coerce_utc(current_source)
        acknowledged = EvolveState(
            **{
                **state.__dict__,
                "schedule_next_run_at": _iso(
                    _next_scheduled_run(state, current_source)
                ),
                "schedule_last_run_at": state.schedule_claimed_at or _iso(current),
                "schedule_claim_id": "",
                "schedule_claimed_at": "",
            }
        )
        return _save_evolve_state_unlocked(acknowledged, root)


def normalize_evolve_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized not in MODES:
        raise ValueError("Evolve mode must be disabled, co-evolve, or auto-evolve.")
    return normalized


def evolve_report(
    root: Path | None = None,
    *,
    refresh: bool = True,
) -> EvolveReport:
    state = load_evolve_state(root)
    candidates = ()
    if state.mode != MODE_DISABLED:
        candidates = (
            sync_evolve_candidates(root, theme=state.theme)
            if refresh
            else load_evolve_candidates(root, theme=state.theme)
        )
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.source] = counts.get(candidate.source, 0) + 1
    evidence_counts = {"feedback": 0, "experience": 0}
    for signal in load_evidence(root, include_inactive=True):
        evidence_counts[signal.source] = evidence_counts.get(signal.source, 0) + 1
    return EvolveReport(
        state=state,
        candidates=candidates,
        top_candidate=candidates[0] if candidates else None,
        counts_by_source=counts,
        evidence_counts=evidence_counts,
        pending_evidence=pending_evidence_counts(root),
        evidence_settings=load_evidence_settings(root),
    )


def propose_evolve(
    root: Path | None = None,
    *,
    brainstormer: BrainstormFallback | None = None,
    curator: CurationGenerator | None = None,
    mission: str = "",
    curation_limit: int = DEFAULT_CURATION_LIMIT,
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
    curation = None
    new_candidates: tuple[EvolveCandidate, ...] = ()
    top_candidate = candidates[0] if candidates else None
    if report.state.mode != MODE_DISABLED:
        bounded = _select_curation_candidates(candidates, limit=curation_limit)
        snapshots = tuple(_candidate_curation_snapshot(candidate) for candidate in bounded)
        if curator is None:
            curation = deterministic_fallback(snapshots, reason="curator-unavailable")
        else:
            completion_evidence: tuple[dict[str, object], ...] = ()
            try:
                completion_evidence = recent_completion_evidence(snapshots, root)
            except (OSError, RuntimeError, TimeoutError, ValueError) as evidence_error:
                detail = clean_text(str(evidence_error)) or evidence_error.__class__.__name__
                curation = deterministic_fallback(
                    snapshots,
                    reason=f"completion-evidence-unavailable: {detail}",
                )
            if curation is None:
                try:
                    curation = curate_candidates(
                        mission=mission,
                        theme=report.state.theme,
                        candidates=snapshots,
                        completion_evidence=completion_evidence,
                        generator=curator,
                    )
                except (OSError, RuntimeError, TimeoutError, ValueError) as curation_error:
                    reason = clean_text(str(curation_error)) or curation_error.__class__.__name__
                    refs = (
                        ref
                        for item in completion_evidence
                        for ref in item.get("evidence_refs", ())
                    )
                    curation = deterministic_fallback(
                        snapshots,
                        reason=reason,
                        evidence_refs=refs,
                    )
        if curation.status == "llm" and curation.new_candidates:
            ideas = tuple(
                brainstorm_idea(
                    theme=report.state.theme or "mission-aligned",
                    mission=mission,
                    **suggestion.__dict__,
                )
                for suggestion in curation.new_candidates
            )
            save_brainstorm_ideas(ideas, root)
            report = evolve_report(root)
            candidates = tuple(
                candidate
                for candidate in report.candidates
                if candidate.status in ACTIONABLE_CANDIDATE_STATUSES
            )
            new_ids = tuple(idea.id for idea in ideas)
            new_candidates = tuple(candidate for candidate in candidates if candidate.id in new_ids)
            curation = with_new_candidate_ids(curation, new_ids)
        if curation.recommendation is None:
            top_candidate = None
        else:
            top_candidate = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.id == curation.recommendation.candidate_id
                ),
                None,
            )
        record_curation(curation, root)
    return EvolveProposal(
        report=report,
        candidates=candidates,
        top_candidate=top_candidate,
        brainstorm_attempted=attempted,
        brainstorm_added=added,
        brainstorm_skip_reason=skip_reason,
        brainstorm_error=error,
        curation=curation,
        new_candidates=new_candidates,
    )


def _candidate_curation_snapshot(candidate: EvolveCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "source": candidate.source,
        "title": _bounded_curation_text(candidate.title),
        "rationale": _bounded_curation_text(candidate.rationale),
        "proposed_change": _bounded_curation_text(candidate.proposed_change),
        "expected_benefit": _bounded_curation_text(candidate.expected_benefit),
        "risk": _bounded_curation_text(candidate.risk),
        "test_plan": _bounded_curation_text(candidate.test_plan),
        "status": candidate.status,
        "deterministic_score": candidate.score,
        "provenance": {
            "evidence_source": candidate.evidence_source or candidate.source,
            "evidence_ids": list(candidate.evidence_ids),
            "evidence_refs": list(candidate.evidence_refs),
            "signal_actor": candidate.signal_actor,
            "candidate_actor": candidate.candidate_actor,
            "parent_candidate_id": candidate.parent_candidate_id,
            "source_task_id": candidate.source_task_id,
        },
    }


def _select_curation_candidates(
    candidates: tuple[EvolveCandidate, ...],
    *,
    limit: int,
) -> tuple[EvolveCandidate, ...]:
    """Keep deterministic order while retaining source diversity in bounded context."""
    bounded_limit = max(1, limit)
    if len(candidates) <= bounded_limit:
        return candidates
    first_source_indexes: dict[str, int] = {}
    for index, candidate in enumerate(candidates):
        first_source_indexes.setdefault(candidate.source, index)
    selected = set(tuple(first_source_indexes.values())[:bounded_limit])
    for index in range(len(candidates)):
        if len(selected) >= bounded_limit:
            break
        selected.add(index)
    return tuple(candidates[index] for index in sorted(selected))


def _bounded_curation_text(value: str, *, limit: int = 1200) -> str:
    return sanitize_curation_text(value, limit=limit)


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
    with file_transaction(path):
        raw = load_json_object(path)
        raw_attempts = raw.get("attempts") if raw else None
        if raw_attempts is not None and not isinstance(raw_attempts, dict):
            raise StateCorruptionError(path, "expected attempts to be an object")
        attempts = {
            clean_text(str(key)).casefold(): str(value)
            for key, value in (raw_attempts or {}).items()
            if clean_text(str(key))
        }
        current = _coerce_utc(now) if now is not None else _utc_now()
        previous = _parse_time(attempts.get(normalized_theme, ""))
        if previous is not None and current - previous < timedelta(
            seconds=AUTO_BRAINSTORM_COOLDOWN_SECONDS
        ):
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
    candidates.extend(_inheritance_candidates(load_parent_inbox_candidates(root)))
    candidates.extend(_peer_learning_candidates(load_peer_learning_observations(root)))
    candidates.extend(_brainstorm_candidates(load_brainstorm_ideas(root, theme=theme)))
    return tuple(candidates)


def synthesize_evolve_candidates_from_evidence(
    root: Path | None = None,
    *,
    mission: str,
    generator: CurationGenerator,
    limit: int = 5,
) -> tuple[EvolveCandidate, ...]:
    state = load_evolve_state(root)
    if state.mode == MODE_DISABLED:
        return ()
    signals = unlinked_evidence(root)
    if not signals:
        return ()
    stored = _load_all_evolve_candidates(root)
    drafts = synthesize_evidence_candidates(
        signals,
        mission=mission,
        theme=state.theme,
        existing_candidates=(_candidate_to_json(candidate) for candidate in stored),
        generator=generator,
        limit=limit,
    )
    if not drafts:
        return ()
    existing = {candidate.id: candidate for candidate in stored}
    created: list[EvolveCandidate] = []
    for draft in drafts:
        if draft.id in existing:
            # Recover cleanly if a previous process wrote the candidate but
            # stopped before appending the evidence linkage update.
            link_evidence(draft.evidence_ids, draft.id, root)
            continue
        candidate = _candidate_from_evidence_draft(draft, root)
        existing[candidate.id] = candidate
        created.append(candidate)
    if not created:
        return ()
    ranked = rank_evolve_candidates(existing.values(), theme=state.theme)
    _write_evolve_candidates(ranked, root)
    for candidate in created:
        link_evidence(candidate.evidence_ids, candidate.id, root)
    created_ids = {candidate.id for candidate in created}
    return tuple(candidate for candidate in ranked if candidate.id in created_ids)


def _candidate_from_evidence_draft(
    draft: EvidenceCandidateDraft,
    root: Path | None,
) -> EvolveCandidate:
    task_events = load_task_events(root)
    event_task_ids = {
        event.id: event.task_id
        for event in task_events
    }
    referenced_task_ids = [
        int(ref.removeprefix("task:"))
        for ref in draft.evidence_refs
        if ref.startswith("task:") and ref.removeprefix("task:").isdigit()
    ]
    referenced_task_ids.extend(
        event_task_ids[ref.removeprefix("task-event:")]
        for ref in draft.evidence_refs
        if ref.startswith("task-event:")
        and ref.removeprefix("task-event:") in event_task_ids
    )
    source_task_id = referenced_task_ids[0] if referenced_task_ids else None
    parent_candidate_id = next(
        (
            event.candidate_id
            for event in reversed(task_events)
            if event.task_id == source_task_id and event.candidate_id
        ),
        "",
    )
    return EvolveCandidate(
        id=draft.id,
        source=draft.source,
        title=draft.title,
        rationale=draft.rationale,
        proposed_change=draft.proposed_change,
        expected_benefit=draft.expected_benefit,
        risk=draft.risk,
        test_plan=draft.test_plan,
        initiated_by="agent",
        evidence_source=draft.source,
        signal_actor="human" if draft.source == "feedback" else "system",
        candidate_actor="agent",
        parent_candidate_id=parent_candidate_id,
        source_task_id=source_task_id,
        score=draft.score,
        evidence_ids=draft.evidence_ids,
        evidence_refs=draft.evidence_refs,
    )


def sync_evolve_candidates(root: Path | None = None, *, theme: str = "") -> tuple[EvolveCandidate, ...]:
    stored = {candidate.id: candidate for candidate in _load_all_evolve_candidates(root)}
    collected = collect_evolve_candidates(root, theme=theme)
    collected_ids = {candidate.id for candidate in collected}
    merged: dict[str, EvolveCandidate] = {}
    retired: list[tuple[EvolveCandidate, str]] = []
    for candidate_id, candidate in stored.items():
        if (
            candidate.source == "brainstorming"
            and candidate.status in VISIBLE_CANDIDATE_STATUSES
            and candidate_id not in collected_ids
        ):
            continue
        retirement_reason = _candidate_retirement_reason(candidate)
        if retirement_reason and candidate.status in ACTIONABLE_CANDIDATE_STATUSES:
            candidate = EvolveCandidate(**{**candidate.__dict__, "status": "removed"})
            retired.append((candidate, retirement_reason))
        merged[candidate_id] = candidate
    for candidate in collected:
        previous = stored.get(candidate.id)
        status = previous.status if previous is not None else candidate.status
        merged[candidate.id] = EvolveCandidate(**{**candidate.__dict__, "status": status})
    ranked = rank_evolve_candidates(merged.values(), theme=theme)
    _write_evolve_candidates(ranked, root)
    for candidate, reason in retired:
        record_evolve_event(
            "removed",
            root,
            event_actor="system",
            trigger="candidate-source-retirement",
            candidate=candidate,
            reason=reason,
            proposal_id=latest_open_proposal_id(candidate.id, root),
        )
    return tuple(candidate for candidate in ranked if candidate.status in VISIBLE_CANDIDATE_STATUSES)


def _candidate_retirement_reason(candidate: EvolveCandidate) -> str:
    if candidate.source in RETIRED_CANDIDATE_SOURCES:
        return "backlog-is-not-evolution-evidence"
    if candidate.source in {"feedback", "experience"} and not candidate.evidence_ids:
        return "legacy-hardcoded-evidence-pathway-retired"
    return ""


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
    reason: str = "human-requested-removal",
    classification: str = "",
    curation_id: str = "",
    evidence_refs: tuple[str, ...] = (),
) -> EvolveCandidate:
    normalized_actor = clean_text(event_actor).lower()
    if normalized_actor != "human":
        raise ValueError("Only a human can remove an evolve candidate.")
    normalized_classification = clean_text(classification).lower()
    if normalized_classification and normalized_classification not in REMOVE_CLASSIFICATIONS:
        raise ValueError("Unknown evolve candidate removal classification.")
    normalized_curation_id = clean_text(curation_id)
    normalized_refs = tuple(clean_text(ref) for ref in evidence_refs if clean_text(ref))
    if normalized_curation_id or normalized_refs:
        matching = next(
            (
                suggestion
                for curation in load_curations(root)
                if curation.id == normalized_curation_id
                for suggestion in curation.remove_suggestions
                if suggestion.candidate_id.lower() == candidate_id.strip().lower().lstrip("#")
                and suggestion.classification == normalized_classification
                and suggestion.evidence_refs == normalized_refs
            ),
            None,
        )
        if matching is None:
            raise ValueError("Removal evidence does not match the recorded curation suggestion.")
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
        reason=reason,
        curation_id=normalized_curation_id,
        removal_classification=normalized_classification,
        evidence_refs=normalized_refs,
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
        retry_of_task_id=job.parent_task_id,
        reason=reason,
        runtime_task=job,
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
        retry_of_task_id=job.parent_task_id,
        reason=reason,
        runtime_task=job,
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
        retry_of_task_id=job.parent_task_id,
        reason=reason,
        runtime_task=job,
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
    trigger: str = "/task resume",
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
        retry_of_task_id=job.parent_task_id,
        reason=reason,
        runtime_task=job,
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
        retry_of_task_id=job.parent_task_id,
        reason=reason,
        runtime_task=job,
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
    retry_of_task_id: int | None = None,
    reason: str = "",
    proposal_id: str = "",
    curation_id: str = "",
    removal_classification: str = "",
    evidence_refs: tuple[str, ...] = (),
    runtime_task: TaskJob | None = None,
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
            retry_of_task_id=retry_of_task_id,
            reason=reason,
            proposal_id=linked_id,
            curation_id=curation_id,
            removal_classification=removal_classification,
            evidence_refs=evidence_refs,
            runtime_task=runtime_task,
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
    raw = load_json_object(path)
    if not raw:
        return ()
    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list):
        raise StateCorruptionError(path, "expected candidates to be a list")
    candidates = []
    for raw_candidate in raw_candidates:
        candidate = _candidate_from_json(raw_candidate) if isinstance(raw_candidate, dict) else None
        if candidate is None:
            raise StateCorruptionError(path, "found an invalid evolve candidate")
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
        "initiated_by": candidate.candidate_actor
        if candidate.candidate_actor in {"human", "agent"}
        else "agent",
        "evidence_source": candidate.evidence_source or candidate.source,
        "signal_actor": candidate.signal_actor,
        "candidate_actor": candidate.candidate_actor,
        "parent_candidate_id": candidate.parent_candidate_id,
        "source_task_id": candidate.source_task_id,
        "evidence_ids": list(candidate.evidence_ids),
        "evidence_refs": list(candidate.evidence_refs),
        "status": candidate.status if candidate.status in CANDIDATE_STATUSES else "candidate",
        "score": int(candidate.score),
        "base_score": (
            int(candidate.base_score)
            if candidate.base_score is not None
            else int(candidate.score)
        ),
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
    legacy_initiated_by = clean_text(str(raw.get("initiated_by") or "")).lower()
    if legacy_initiated_by not in {"human", "agent"}:
        legacy_initiated_by = _candidate_initiator(source)
    evidence_source = clean_text(str(raw.get("evidence_source") or source)).lower()
    if evidence_source not in {
        "backlog",
        "feedback",
        "experience",
        "inheritance",
        "learning",
        "brainstorming",
    }:
        evidence_source = source
    signal_actor = _provenance_actor(
        raw.get("signal_actor"),
        default=_candidate_signal_actor(evidence_source),
    )
    candidate_actor = _provenance_actor(raw.get("candidate_actor"), default="agent")
    initiated_by = (
        candidate_actor
        if candidate_actor in {"human", "agent"}
        else legacy_initiated_by
    )
    score = _int(raw.get("score"), default=0)
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
        evidence_source=evidence_source,
        signal_actor=signal_actor,
        candidate_actor=candidate_actor,
        parent_candidate_id=clean_text(str(raw.get("parent_candidate_id") or "")),
        source_task_id=_positive_int(raw.get("source_task_id")),
        status=status,
        score=score,
        base_score=_int(raw.get("base_score"), default=score),
        evidence_ids=_string_tuple(raw.get("evidence_ids")),
        evidence_refs=_string_tuple(raw.get("evidence_refs")),
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
    return "agent"


def _candidate_signal_actor(source: str) -> str:
    if source in {"backlog", "feedback", "learning"}:
        return "human"
    if source in {"inheritance", "brainstorming"}:
        return "agent"
    return "system"


def _provenance_actor(value: object, *, default: str) -> str:
    actor = clean_text(str(value or "")).lower()
    return actor if actor in {"human", "agent", "system"} else default


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
                evidence_source="inheritance",
                signal_actor="agent",
                candidate_actor="agent",
                score=relevance_score.get(item.relevance, 8),
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
                initiated_by="agent",
                evidence_source="learning",
                signal_actor="human",
                candidate_actor="agent",
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
            evidence_source="brainstorming",
            signal_actor="agent",
            candidate_actor="agent",
            score=16,
        )
        for item in items
    ]


def _score_candidate(candidate: EvolveCandidate, *, theme: str) -> EvolveCandidate:
    base_score = (
        candidate.base_score
        if candidate.base_score is not None
        else candidate.score
    )
    score = base_score + 25
    text = " ".join([candidate.title, candidate.rationale, candidate.proposed_change]).lower()
    theme_words = {word for word in clean_text(theme).lower().split() if len(word) >= 4}
    if theme_words and any(word in text for word in theme_words):
        score += 20
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
    return EvolveCandidate(
        **{
            **candidate.__dict__,
            "score": score,
            "base_score": base_score,
        }
    )


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


def _positive_int(value: object) -> int | None:
    parsed = _int(value, default=0)
    return parsed if parsed > 0 else None


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
