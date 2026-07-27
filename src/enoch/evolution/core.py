from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable
from uuid import uuid4

from enoch.evolution.sources.brainstorming import BrainstormCandidateDraft
from enoch.evolution.curation import (
    DEFAULT_CURATION_LIMIT,
    CurationGenerator,
    REMOVE_CLASSIFICATIONS,
    SemanticCuration,
    candidate_scope_is_safe,
    curate_candidates,
    deterministic_fallback,
    load_curations,
    recent_completion_evidence,
    record_curation,
    sanitize_curation_text,
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
from enoch.learn import LearningCandidateDraft, PublishedSkill
from enoch.memory.paths import atomic_write, clean_text, now as current_time
from enoch.paths import private_state_path
from enoch.tasks.events import load_task_events
from enoch.state import StateCorruptionError, file_transaction, load_json_object


SCHEMA_VERSION = 2
CANDIDATE_SCHEMA_VERSION = 8
MODE_DISABLED = "disabled"
MODE_CO_EVOLVE = "co-evolve"
MODE_AUTO_EVOLVE = "auto-evolve"
MODES = {MODE_DISABLED, MODE_CO_EVOLVE, MODE_AUTO_EVOLVE}
DEFAULT_MODE = MODE_CO_EVOLVE
CANDIDATE_STATUSES = {
    "candidate",
    "approved",
    "removed",
}
ACTIONABLE_CANDIDATE_STATUSES = {"candidate"}
VISIBLE_CANDIDATE_STATUSES = {"candidate"}
AUTO_BRAINSTORM_COOLDOWN_SECONDS = 24 * 60 * 60


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
    source_repository: str = ""
    source_revision: str = ""
    source_path: str = ""
    source_version: str = ""
    source_content_hash: str = ""
    source_url: str = ""
    source_theme: str = ""
    source_context_hash: str = ""
    source_created_at: str = ""
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
    curation: SemanticCuration | None = None
    scheduled_brainstorm_status: str = ""
    scheduled_brainstorm_created: int = 0
    scheduled_brainstorm_existing: int = 0
    scheduled_brainstorm_error: str = ""
    evidence_scan_results: tuple[EvidenceScanResult, ...] = ()
    evidence_candidates_added: int = 0
    evidence_synthesis_error: str = ""


@dataclass(frozen=True)
class BrainstormCreation:
    created: tuple[EvolveCandidate, ...]
    existing: tuple[EvolveCandidate, ...]


def evolve_state_path(root: Path | None = None) -> Path:
    return private_state_path("evolve.json", root)


def evolve_candidates_path(root: Path | None = None) -> Path:
    return private_state_path("evolve_candidates.json", root)


def evolve_brainstorm_schedule_path(root: Path | None = None) -> Path:
    return private_state_path("evolve_brainstorm_schedule.json", root)


def _legacy_evolve_brainstorm_schedule_path(
    root: Path | None = None,
) -> Path:
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
    curator: CurationGenerator | None = None,
    mission: str = "",
    curation_limit: int = DEFAULT_CURATION_LIMIT,
) -> EvolveProposal:
    report = evolve_report(root)
    candidates = tuple(
        candidate
        for candidate in report.candidates
        if candidate.status in ACTIONABLE_CANDIDATE_STATUSES
    )
    curation = None
    top_candidate = candidates[0] if candidates else None
    if report.state.mode != MODE_DISABLED and candidates:
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
        curation=curation,
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
            "source_repository": candidate.source_repository,
            "source_revision": candidate.source_revision,
            "source_path": candidate.source_path,
            "source_version": candidate.source_version,
            "source_content_hash": candidate.source_content_hash,
            "source_url": candidate.source_url,
            "source_theme": candidate.source_theme,
            "source_context_hash": candidate.source_context_hash,
            "source_created_at": candidate.source_created_at,
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


def claim_scheduled_brainstorm(
    theme: str,
    root: Path | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    normalized_theme = clean_text(theme).casefold()
    if not normalized_theme:
        return False
    path = evolve_brainstorm_schedule_path(root)
    with file_transaction(path):
        raw = load_json_object(path)
        if not path.exists():
            raw = load_json_object(
                _legacy_evolve_brainstorm_schedule_path(root)
            )
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


def create_brainstorm_candidates(
    drafts: Iterable[BrainstormCandidateDraft],
    root: Path | None = None,
    *,
    theme: str,
    context_hash: str,
    created_at: str = "",
) -> BrainstormCreation:
    normalized_theme = clean_text(theme)
    normalized_context_hash = clean_text(context_hash).lower()
    if not normalized_theme:
        raise ValueError("Brainstorming candidates require a theme.")
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_context_hash):
        raise ValueError("Brainstorming candidates require a valid context hash.")
    prepared = tuple(drafts)
    if not prepared:
        return BrainstormCreation(created=(), existing=())
    for draft in prepared:
        if not candidate_scope_is_safe(draft.__dict__):
            raise ValueError(
                "Brainstorming candidate has protected or dangerous scope."
            )
    path = evolve_candidates_path(root)
    with file_transaction(path):
        stored = {
            candidate.id: candidate
            for candidate in _load_all_evolve_candidates(root)
        }
        stored_by_change = {
            _candidate_change_fingerprint(candidate.proposed_change): candidate.id
            for candidate in stored.values()
            if _candidate_change_fingerprint(candidate.proposed_change)
        }
        created_ids: list[str] = []
        existing_ids: list[str] = []
        source_created_at = clean_text(created_at) or current_time()
        for draft in prepared:
            change_fingerprint = _candidate_change_fingerprint(
                draft.proposed_change
            )
            matching_id = stored_by_change.get(change_fingerprint)
            if matching_id is not None:
                existing_ids.append(matching_id)
                continue
            candidate_id = _brainstorm_candidate_id(
                normalized_theme,
                draft.proposed_change,
            )
            if candidate_id in stored:
                existing_ids.append(candidate_id)
                continue
            candidate = EvolveCandidate(
                id=candidate_id,
                source="brainstorming",
                title=draft.title,
                rationale=draft.rationale,
                proposed_change=draft.proposed_change,
                expected_benefit=draft.expected_benefit,
                risk=draft.risk,
                test_plan=draft.test_plan,
                initiated_by="agent",
                evidence_source="brainstorming",
                signal_actor="agent",
                candidate_actor="agent",
                source_theme=normalized_theme,
                source_context_hash=normalized_context_hash,
                source_created_at=source_created_at,
                score=16,
            )
            stored[candidate.id] = candidate
            stored_by_change[change_fingerprint] = candidate.id
            created_ids.append(candidate.id)
        ranked = rank_evolve_candidates(
            stored.values(),
            theme=normalized_theme,
        )
        if created_ids:
            _write_evolve_candidates(ranked, root)
    by_id = {candidate.id: candidate for candidate in ranked}
    return BrainstormCreation(
        created=tuple(by_id[candidate_id] for candidate_id in created_ids),
        existing=tuple(by_id[candidate_id] for candidate_id in existing_ids),
    )


def create_learning_candidate(
    skill: PublishedSkill,
    draft: LearningCandidateDraft,
    root: Path | None = None,
    *,
    theme: str = "",
) -> tuple[EvolveCandidate, bool]:
    draft_payload = {
        "title": draft.title,
        "rationale": draft.rationale,
        "proposed_change": draft.proposed_change,
        "expected_benefit": draft.expected_benefit,
        "risk": draft.risk,
        "test_plan": draft.test_plan,
    }
    if not candidate_scope_is_safe(draft_payload):
        raise ValueError("Learning candidate has protected or dangerous scope.")
    digest = hashlib.sha256(
        (
            f"{skill.repository}\0{skill.revision}\0{skill.path}\0"
            f"{skill.content_hash}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    candidate_id = f"learning-skill-{digest}"
    stored = {candidate.id: candidate for candidate in _load_all_evolve_candidates(root)}
    existing = stored.get(candidate_id)
    if existing is not None:
        return _score_candidate(existing, theme=theme), False
    candidate = EvolveCandidate(
        id=candidate_id,
        source="learning",
        title=draft.title,
        rationale=draft.rationale,
        proposed_change=draft.proposed_change,
        expected_benefit=draft.expected_benefit,
        risk=draft.risk,
        test_plan=draft.test_plan,
        initiated_by="agent",
        evidence_source="learning",
        signal_actor="human",
        candidate_actor="agent",
        source_repository=skill.repository,
        source_revision=skill.revision,
        source_path=skill.path,
        source_version=skill.version,
        source_content_hash=skill.content_hash,
        source_url=skill.url,
        score=22,
    )
    stored[candidate.id] = candidate
    ranked = rank_evolve_candidates(stored.values(), theme=theme)
    _write_evolve_candidates(ranked, root)
    saved = next(item for item in ranked if item.id == candidate.id)
    return saved, True


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
    merged: dict[str, EvolveCandidate] = {}
    retired: list[tuple[EvolveCandidate, str]] = []
    for candidate_id, candidate in stored.items():
        retirement_reason = _candidate_retirement_reason(candidate)
        if retirement_reason and candidate.status in ACTIONABLE_CANDIDATE_STATUSES:
            candidate = EvolveCandidate(**{**candidate.__dict__, "status": "removed"})
            retired.append((candidate, retirement_reason))
        merged[candidate_id] = candidate
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
    if candidate.source == "backlog":
        return "backlog-is-not-evolution-evidence"
    if candidate.source == "inheritance":
        return "inheritance-now-uses-its-own-human-governed-workflow"
    if candidate.source in {"feedback", "experience"} and not candidate.evidence_ids:
        return "legacy-hardcoded-evidence-pathway-retired"
    if candidate.source == "learning" and candidate.id.startswith("learning-peer-"):
        return "legacy-peer-learning-observation-retired"
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


def approve_evolve_candidate(
    candidate_id: str,
    root: Path | None = None,
    *,
    theme: str = "",
) -> EvolveCandidate:
    candidate = get_evolve_candidate(candidate_id, root, theme=theme)
    if candidate.status != "candidate":
        raise ValueError(
            f"Evolve candidate {candidate.id} cannot be approved from status "
            f"{candidate.status}."
        )
    return _set_candidate_status(candidate.id, "approved", root, theme=theme)


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
    runtime_task: object | None = None,
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
        "source_repository": candidate.source_repository,
        "source_revision": candidate.source_revision,
        "source_path": candidate.source_path,
        "source_version": candidate.source_version,
        "source_content_hash": candidate.source_content_hash,
        "source_url": candidate.source_url,
        "source_theme": candidate.source_theme,
        "source_context_hash": candidate.source_context_hash,
        "source_created_at": candidate.source_created_at,
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
    if status in {
        "running",
        "done",
        "failed",
        "cancelled",
        "regressed",
        "reverted",
        "forward-fixed",
    }:
        status = "approved"
    if status not in CANDIDATE_STATUSES:
        status = "candidate"
    source = clean_text(str(raw.get("source") or "unknown")) or "unknown"
    if source in {"task-history", "cron"}:
        source = "experience"
    if source == "learning" and not candidate_id.startswith(
        ("learning-peer-", "learning-skill-")
    ):
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
        source_repository=clean_text(str(raw.get("source_repository") or "")),
        source_revision=clean_text(str(raw.get("source_revision") or "")),
        source_path=clean_text(str(raw.get("source_path") or "")),
        source_version=clean_text(str(raw.get("source_version") or "")),
        source_content_hash=clean_text(
            str(raw.get("source_content_hash") or "")
        ),
        source_url=clean_text(str(raw.get("source_url") or "")),
        source_theme=clean_text(str(raw.get("source_theme") or "")),
        source_context_hash=clean_text(
            str(raw.get("source_context_hash") or "")
        ),
        source_created_at=clean_text(str(raw.get("source_created_at") or "")),
        status=status,
        score=score,
        base_score=_int(raw.get("base_score"), default=score),
        evidence_ids=_string_tuple(raw.get("evidence_ids")),
        evidence_refs=_string_tuple(raw.get("evidence_refs")),
    )


def _candidate_matches_id(candidate: EvolveCandidate, candidate_id: str) -> bool:
    normalized = candidate_id.strip().lower().lstrip("#")
    return candidate.id.lower() == normalized or candidate.id.lower().split("-", 1)[-1] == normalized


def _candidate_status_order(status: str) -> int:
    return {
        "candidate": 0,
        "approved": 1,
        "removed": 2,
    }.get(status, 0)


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


def _brainstorm_candidate_id(theme: str, proposed_change: str) -> str:
    digest = hashlib.sha256(
        (
            f"{clean_text(theme).casefold()}\0"
            f"{clean_text(proposed_change).casefold()}"
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"brainstorm-{digest}"


def _candidate_change_fingerprint(proposed_change: str) -> str:
    return clean_text(proposed_change).casefold()


def _score_candidate(candidate: EvolveCandidate, *, theme: str) -> EvolveCandidate:
    base_score = (
        candidate.base_score
        if candidate.base_score is not None
        else candidate.score
    )
    score = base_score + 25
    text = " ".join([candidate.title, candidate.rationale, candidate.proposed_change]).lower()
    theme_words = {word for word in clean_text(theme).lower().split() if len(word) >= 4}
    normalized_theme = clean_text(theme).casefold()
    exact_source_theme = bool(
        normalized_theme
        and clean_text(candidate.source_theme).casefold() == normalized_theme
    )
    if exact_source_theme or (
        theme_words and any(word in text for word in theme_words)
    ):
        score += 20
    if candidate.source == "experience":
        score += 12
    if candidate.source == "learning":
        score += 6
    if candidate.source == "feedback":
        score += 10
    if candidate.source == "brainstorming":
        score += 4
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
