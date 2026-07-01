from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Iterable

from enoch.backlog import BacklogItem, backlog_status
from enoch.lineage.core import LineageCandidate, load_parent_inbox_candidates
from enoch.memory.paths import atomic_write, clean_text, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 1
MODE_DISABLED = "disabled"
MODE_CO_EVOLVE = "co-evolve"
MODE_AUTO_EVOLVE = "auto-evolve"
MODES = {MODE_DISABLED, MODE_CO_EVOLVE, MODE_AUTO_EVOLVE}
DEFAULT_MODE = MODE_CO_EVOLVE


@dataclass(frozen=True)
class EvolveState:
    mode: str = DEFAULT_MODE
    theme: str = ""
    updated_at: str = ""
    schedule_enabled: bool = False
    schedule_interval_seconds: int = 0
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
    status: str = "candidate"
    score: int = 0


@dataclass(frozen=True)
class EvolveReport:
    state: EvolveState
    candidates: tuple[EvolveCandidate, ...]
    top_candidate: EvolveCandidate | None
    counts_by_source: dict[str, int]


def evolve_state_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve.json"


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
        schedule_next_run_at=str(raw.get("schedule_next_run_at") or ""),
        schedule_last_run_at=str(raw.get("schedule_last_run_at") or ""),
    )


def save_evolve_state(state: EvolveState, root: Path | None = None) -> EvolveState:
    normalized = EvolveState(
        mode=normalize_evolve_mode(state.mode),
        theme=clean_text(state.theme),
        updated_at=state.updated_at or current_time(),
        schedule_enabled=state.schedule_enabled and state.schedule_interval_seconds > 0,
        schedule_interval_seconds=max(0, int(state.schedule_interval_seconds)),
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
            schedule_next_run_at=_iso(current_time + timedelta(seconds=interval_seconds)),
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
    current = _coerce_utc(now) if now is not None else _utc_now()
    next_run_at = _parse_time(state.schedule_next_run_at)
    if next_run_at is None or next_run_at > current:
        return None
    claimed = state
    save_evolve_state(
        EvolveState(
            mode=state.mode,
            theme=state.theme,
            schedule_enabled=True,
            schedule_interval_seconds=state.schedule_interval_seconds,
            schedule_next_run_at=_iso(current + timedelta(seconds=state.schedule_interval_seconds)),
            schedule_last_run_at=_iso(current),
        ),
        root,
    )
    return claimed


def normalize_evolve_mode(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized in {"co", "coevolve", "co_evolve"}:
        normalized = MODE_CO_EVOLVE
    if normalized in {"auto", "autoevolve", "auto_evolve"}:
        normalized = MODE_AUTO_EVOLVE
    if normalized not in MODES:
        raise ValueError("Evolve mode must be disabled, co-evolve, or auto-evolve.")
    return normalized


def evolve_report(root: Path | None = None) -> EvolveReport:
    state = load_evolve_state(root)
    candidates = ()
    if state.mode != MODE_DISABLED:
        candidates = rank_evolve_candidates(collect_evolve_candidates(root), theme=state.theme)
    counts: dict[str, int] = {}
    for candidate in candidates:
        counts[candidate.source] = counts.get(candidate.source, 0) + 1
    return EvolveReport(
        state=state,
        candidates=candidates,
        top_candidate=candidates[0] if candidates else None,
        counts_by_source=counts,
    )


def collect_evolve_candidates(root: Path | None = None) -> tuple[EvolveCandidate, ...]:
    candidates: list[EvolveCandidate] = []
    candidates.extend(_backlog_candidates(backlog_status(root).pending))
    candidates.extend(_inheritance_candidates(load_parent_inbox_candidates(root)))
    return tuple(candidates)


def rank_evolve_candidates(
    candidates: Iterable[EvolveCandidate],
    *,
    theme: str = "",
) -> tuple[EvolveCandidate, ...]:
    scored = [_score_candidate(candidate, theme=theme) for candidate in candidates]
    return tuple(sorted(scored, key=lambda item: (-item.score, item.source, item.id)))


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
                score=relevance_score.get(item.relevance, 8),
            )
        )
    return candidates


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
    return EvolveCandidate(**{**candidate.__dict__, "score": score})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc, microsecond=0)
    return value.astimezone(timezone.utc).replace(microsecond=0)


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
