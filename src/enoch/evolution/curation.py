from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from enoch.evolution.events import load_evolve_events
from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import enoch_home
from enoch.tasks.events import TaskEvent, load_recent_task_outcomes


SCHEMA_VERSION = 2
DEFAULT_CURATION_LIMIT = 24
DEFAULT_COMPLETION_EVIDENCE_LIMIT = 12
REMOVE_CLASSIFICATIONS = {
    "duplicate",
    "superseded",
    "obsolete",
    "already-resolved",
    "context-only",
    "not-actionable",
}
CurationGenerator = Callable[[str], str]

_PR_URL_PATTERN = re.compile(
    r"https://[A-Za-z0-9.-]+/[^\s]+/(?:pull|pulls|merge_requests)/\d+/?$"
)
_EVIDENCE_REF_PATTERNS = (
    re.compile(r"task:[1-9]\d*$"),
    re.compile(r"pr:https://[A-Za-z0-9.-]+/[^\s]+/(?:pull|pulls|merge_requests)/\d+/?$"),
    re.compile(r"merge:[0-9a-fA-F]{7,64}$"),
    re.compile(r"version:[A-Za-z0-9._-]{1,80}$"),
)
_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_ -]?key|access[_ -]?token|token|password|secret|authorization)"
    r"\b\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")
_CHAT_ID_PATTERN = re.compile(
    r"(?i)\b(chat|conversation|session|message)[_-]?id\b\s*[:=]\s*[^\s,;]+"
)
_MEMORY_ID_PATTERN = re.compile(r"\bmem_\d{8}_[A-Za-z0-9_-]+\b")
_MEMORY_BLOCK_PATTERN = re.compile(
    r"(?is)\[ENOCH_MEMORY_REQUEST\].*?\[/ENOCH_MEMORY_REQUEST\]"
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9:])(?:/(?:Users|home|private|tmp|var|etc|opt)/[^\s,;)\]}]+"
    r"|(?<![:/\w])/(?:[^/\s]+/)+[^\s,;)\]}]+"
    r"|[A-Za-z]:\\[^\s,;)\]}]+)"
)
_PRIVATE_STATE_PATTERN = re.compile(r"(?<![\w/])\.enoch/[^\s,;)\]}]+")
_BODY_CHANGE_PATTERN = re.compile(
    r"\b(?:add|change|create|delete|edit|fix|implement|modify|remove|replace|rewrite|"
    r"update|code|file|repository|module|function|class|schema|test|docs?|config|"
    r"prompt|journal|command|cli)\b",
    re.IGNORECASE,
)

_ACTION = r"(?:change|modify|edit|update|alter|rewrite|replace|delete|grant|revoke|expand|remove|bypass|automate|enable|disable|configure|deploy|merge|publish|rotate|expose)"
_PROTECTED = r"(?:identity|mission|secret|credential|permission|access control|merge authority|auto[- ]?merge|deployment|forge settings?|daemon configuration)"
_PROTECTED_ACTION_PATTERN = re.compile(
    rf"(?:\b{_ACTION}\b\s+(?:(?:the|our|enoch'?s|agent'?s)\s+)?\b{_PROTECTED}\b|"
    rf"\b{_PROTECTED}\b\s+(?:{_ACTION})(?:s|d|ing)?\b)",
    re.IGNORECASE | re.DOTALL,
)
_DANGEROUS_SCOPE_PATTERN = re.compile(
    r"\b(?:destructive|delete all|erase|wipe|drop database|sudo|chmod 777|deploy(?:ment|s|ed|ing)?|"
    r"auto[- ]?merge|rewrite (?:the )?entire|whole repository|unbounded)\b",
    re.IGNORECASE,
)


class CurationError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateRecommendation:
    candidate_id: str
    reason: str
    scope_guidance: str
    risk_guidance: str
    test_plan_guidance: str


@dataclass(frozen=True)
class RemoveSuggestion:
    candidate_id: str
    classification: str
    reason: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class NewCandidateSuggestion:
    title: str
    rationale: str
    proposed_change: str
    expected_benefit: str
    risk: str
    test_plan: str


@dataclass(frozen=True)
class SemanticCuration:
    id: str
    created_at: str
    status: str
    input_candidate_ids: tuple[str, ...]
    input_evidence_refs: tuple[str, ...] = ()
    recommendation: CandidateRecommendation | None = None
    remove_suggestions: tuple[RemoveSuggestion, ...] = ()
    new_candidates: tuple[NewCandidateSuggestion, ...] = ()
    new_candidate_ids: tuple[str, ...] = ()
    fallback_reason: str = ""


def curation_index_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve_curations.jsonl"


def semantic_curation_prompt(
    mission: str,
    theme: str,
    candidates: Iterable[Mapping[str, object]],
    completion_evidence: Iterable[Mapping[str, object]] = (),
) -> str:
    payload = {
        "mission": sanitize_curation_text(mission, limit=2000),
        "evolution_theme": sanitize_curation_text(theme, limit=500),
        "candidates": [_prompt_candidate(item) for item in candidates],
        "recent_completed_work": [
            _prompt_completion_evidence(item) for item in completion_evidence
        ],
    }
    schema = {
        "recommended_candidate_id": "existing candidate ID or null",
        "recommendation_reason": "required when recommending",
        "scope_guidance": "bounded clarification for the recommendation",
        "risk_guidance": "risk clarification for the recommendation",
        "test_plan_guidance": "specific verification clarification",
        "remove_suggestions": [
            {
                "candidate_id": "existing candidate ID",
                "classification": "duplicate|superseded|obsolete|already-resolved|context-only|not-actionable",
                "reason": "auditable reason",
                "evidence_refs": ["task:17", "pr:https://...", "merge:e536d32", "version:..."],
            }
        ],
        "new_candidates": [
            {
                "title": "short title",
                "rationale": "why it fits mission and theme",
                "proposed_change": "small reversible change",
                "expected_benefit": "specific benefit",
                "risk": "specific bounded risk",
                "test_plan": "specific verification plan",
            }
        ],
    }
    return "\n".join(
        [
            "Curate Enoch's bounded self-evolution candidate pool semantically.",
            "Return exactly one JSON object and no prose.",
            "Recommend at most one existing candidate. Do not invent an ID.",
            "Treat provenance as immutable evidence; never rewrite or reclassify its source or actors.",
            "Suggest removals only; do not approve, queue, run, remove, merge, deploy, or change permissions.",
            "For already-resolved or superseded, cite only evidence_refs present in recent_completed_work.",
            "A completed worker task or open/unmerged PR is not authoritative evidence that a body change is resolved.",
            "Body-change resolution requires evidence labelled authoritative-body; task-completion evidence only supports genuinely non-body work.",
            "Failed, cancelled, regressed, partial, or unpromoted body work must not support already-resolved or superseded.",
            "New candidates must be concrete, small, reversible, and testable.",
            "Do not propose changes to identity, mission, secrets, credentials, permissions, access control, merge authority, deployment, forge settings, daemon configuration, or destructive behavior.",
            f"Required response schema: {json.dumps(schema, sort_keys=True)}",
            f"Curation input: {json.dumps(payload, sort_keys=True)}",
        ]
    )


def curate_candidates(
    *,
    mission: str,
    theme: str,
    candidates: Iterable[Mapping[str, object]],
    completion_evidence: Iterable[Mapping[str, object]] = (),
    generator: CurationGenerator,
) -> SemanticCuration:
    snapshots = tuple(candidates)
    evidence = tuple(completion_evidence)
    candidate_ids = tuple(clean_text(str(item.get("id") or "")) for item in snapshots)
    known = {candidate_id: item for candidate_id, item in zip(candidate_ids, snapshots) if candidate_id}
    known_evidence = _known_evidence(evidence)
    response = generator(semantic_curation_prompt(mission, theme, snapshots, evidence))
    payload = _json_object(response)
    if not isinstance(payload, dict):
        raise CurationError("semantic curator returned malformed JSON")
    expected_keys = {
        "recommended_candidate_id",
        "recommendation_reason",
        "scope_guidance",
        "risk_guidance",
        "test_plan_guidance",
        "remove_suggestions",
        "new_candidates",
    }
    if set(payload) != expected_keys:
        raise CurationError("semantic curator returned an invalid schema")

    recommendation = _recommendation(payload, known)
    remove_suggestions = _remove_suggestions(
        payload.get("remove_suggestions"),
        known,
        known_evidence,
    )
    new_candidates = _new_candidates(payload.get("new_candidates"))
    if recommendation is not None and any(
        item.candidate_id == recommendation.candidate_id for item in remove_suggestions
    ):
        raise CurationError("semantic curator both recommended and suggested removing one candidate")
    if recommendation is None and not remove_suggestions and not new_candidates:
        raise CurationError("semantic curator returned no valid result")
    return SemanticCuration(
        id=_curation_id(),
        created_at=current_time(),
        status="llm",
        input_candidate_ids=candidate_ids,
        input_evidence_refs=tuple(known_evidence),
        recommendation=recommendation,
        remove_suggestions=remove_suggestions,
        new_candidates=new_candidates,
    )


def deterministic_fallback(
    candidates: Iterable[Mapping[str, object]],
    *,
    reason: str,
    evidence_refs: Iterable[str] = (),
) -> SemanticCuration:
    snapshots = tuple(candidates)
    ids = tuple(clean_text(str(item.get("id") or "")) for item in snapshots)
    recommendation = None
    for candidate in snapshots:
        if candidate_scope_is_safe(candidate):
            candidate_id = clean_text(str(candidate.get("id") or ""))
            if candidate_id:
                recommendation = CandidateRecommendation(
                    candidate_id=candidate_id,
                    reason="Highest deterministic pre-ranked safe candidate.",
                    scope_guidance="Review and keep the implementation bounded before approval.",
                    risk_guidance="Use the candidate's recorded risk and human review.",
                    test_plan_guidance=clean_text(str(candidate.get("test_plan") or "")),
                )
                break
    created_at = current_time()
    return SemanticCuration(
        id=_curation_id(),
        created_at=created_at,
        status="deterministic-fallback",
        input_candidate_ids=ids,
        input_evidence_refs=_valid_evidence_refs(evidence_refs),
        recommendation=recommendation,
        fallback_reason=sanitize_curation_text(reason, limit=300),
    )


def with_new_candidate_ids(
    curation: SemanticCuration,
    candidate_ids: Iterable[str],
) -> SemanticCuration:
    return SemanticCuration(
        **{
            **curation.__dict__,
            "new_candidate_ids": tuple(clean_text(value) for value in candidate_ids if clean_text(value)),
        }
    )


def record_curation(curation: SemanticCuration, root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        **asdict(curation),
    }
    path = curation_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def recent_completion_evidence(
    candidates: Iterable[Mapping[str, object]],
    root: Path | None = None,
    *,
    limit: int = DEFAULT_COMPLETION_EVIDENCE_LIMIT,
) -> tuple[dict[str, object], ...]:
    """Build bounded, redacted completion evidence from structured journals."""
    if limit <= 0:
        return ()
    snapshots = tuple(candidates)
    outcomes = load_recent_task_outcomes(root, limit=max(limit * 4, limit))
    lifecycle = load_evolve_events(root)
    promoted_by_task = {
        event.task_id: event
        for event in lifecycle
        if event.event == "promoted" and event.task_id is not None
    }
    adopted_by_task = {
        event.task_id: event
        for event in lifecycle
        if event.event == "adopted" and event.task_id is not None
    }
    evidence: list[dict[str, object]] = []
    for outcome in outcomes:
        if outcome.event != "completed":
            continue
        promoted = promoted_by_task.get(outcome.task_id)
        adopted = adopted_by_task.get(outcome.task_id)
        evidence.append(
            _completion_evidence_payload(
                outcome,
                snapshots,
                promoted=promoted,
                adopted=adopted,
            )
        )
    return tuple(evidence[-limit:])


def _prompt_candidate(candidate: Mapping[str, object]) -> dict[str, object]:
    provenance = candidate.get("provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    return {
        "id": _clip(clean_text(str(candidate.get("id") or "")), 200),
        "source": _clip(clean_text(str(candidate.get("source") or "")), 80),
        "title": sanitize_curation_text(candidate.get("title"), limit=1200),
        "rationale": sanitize_curation_text(candidate.get("rationale"), limit=1200),
        "proposed_change": sanitize_curation_text(
            candidate.get("proposed_change"),
            limit=1200,
        ),
        "expected_benefit": sanitize_curation_text(
            candidate.get("expected_benefit"),
            limit=1200,
        ),
        "risk": sanitize_curation_text(candidate.get("risk"), limit=1200),
        "test_plan": sanitize_curation_text(candidate.get("test_plan"), limit=1200),
        "status": _clip(clean_text(str(candidate.get("status") or "")), 40),
        "deterministic_score": _bounded_int(candidate.get("deterministic_score")),
        "provenance": {
            "evidence_source": _clip(
                clean_text(str(provenance.get("evidence_source") or "")),
                80,
            ),
            "signal_actor": _clip(
                clean_text(str(provenance.get("signal_actor") or "")),
                40,
            ),
            "candidate_actor": _clip(
                clean_text(str(provenance.get("candidate_actor") or "")),
                40,
            ),
            "parent_candidate_id": _clip(
                clean_text(str(provenance.get("parent_candidate_id") or "")),
                200,
            ),
            "source_task_id": _positive_int(provenance.get("source_task_id")),
        },
    }


def _prompt_completion_evidence(evidence: Mapping[str, object]) -> dict[str, object]:
    direct_ids = evidence.get("direct_candidate_ids")
    direct_ids = direct_ids if isinstance(direct_ids, (list, tuple)) else ()
    changed_files = evidence.get("changed_files")
    changed_files = changed_files if isinstance(changed_files, (list, tuple)) else ()
    pr_urls = evidence.get("pr_urls")
    pr_urls = pr_urls if isinstance(pr_urls, (list, tuple)) else ()
    return {
        "task_id": _positive_int(evidence.get("task_id")),
        "completed_at": sanitize_curation_text(
            evidence.get("completed_at"),
            limit=80,
        ),
        "completion_kind": _clip(
            clean_text(str(evidence.get("completion_kind") or "")),
            80,
        ),
        "resolution_authority": _clip(
            clean_text(str(evidence.get("resolution_authority") or "")),
            40,
        ),
        "request_summary": sanitize_curation_text(
            evidence.get("request_summary"),
            limit=600,
        ),
        "result_summary": sanitize_curation_text(
            evidence.get("result_summary"),
            limit=800,
        ),
        "direct_candidate_ids": [
            _clip(clean_text(str(value or "")), 200)
            for value in direct_ids[:12]
            if clean_text(str(value or ""))
        ],
        "parent_task_id": _positive_int(evidence.get("parent_task_id")),
        "source_task_id": _positive_int(evidence.get("source_task_id")),
        "pr_urls": list(_safe_pr_urls(str(value) for value in pr_urls)),
        "changed_files": list(_safe_changed_files(str(value) for value in changed_files)),
        "merge_commit": _safe_revision(str(evidence.get("merge_commit") or "")),
        "authoritative_branch": _safe_branch(
            str(evidence.get("authoritative_branch") or "")
        ),
        "promoted_at": sanitize_curation_text(
            evidence.get("promoted_at"),
            limit=80,
        ),
        "authoritative_version": _safe_version(
            str(evidence.get("authoritative_version") or "")
        ),
        "verified_at": sanitize_curation_text(
            evidence.get("verified_at"),
            limit=80,
        ),
        "evidence_refs": list(_valid_evidence_refs(evidence.get("evidence_refs", ()))),
    }


def _completion_evidence_payload(
    outcome: TaskEvent,
    candidates: tuple[Mapping[str, object], ...],
    *,
    promoted: object | None,
    adopted: object | None,
) -> dict[str, object]:
    pr_urls = _safe_pr_urls(
        (
            *outcome.pr_urls,
            str(getattr(promoted, "pr_url", "") or ""),
        )
    )
    changed_files = _safe_changed_files(outcome.changed_files)
    runtime_reason = clean_text(outcome.runtime_completion_reason).lower()
    partial = bool(runtime_reason and runtime_reason != "completed")
    merge_commit = _safe_revision(str(getattr(promoted, "merge_commit", "") or ""))
    version = _safe_version(str(getattr(adopted, "version", "") or ""))
    body_change = bool(
        changed_files
        or pr_urls
        or outcome.commit_sha
        or outcome.publish_stage in {"committed", "pushed", "pr_opened"}
    )
    if partial:
        completion_kind = "partial"
        authority = "none"
    elif promoted is not None and merge_commit:
        completion_kind = "authoritative-body-change"
        authority = "authoritative-body"
    elif body_change:
        completion_kind = "open-or-unpromoted-body-change"
        authority = "none"
    else:
        completion_kind = "completed-no-body-change"
        authority = "task-completion"

    linked = {
        clean_text(outcome.candidate_id),
        clean_text(outcome.parent_candidate_id),
        clean_text(str(getattr(promoted, "candidate_id", "") or "")),
    }
    for candidate in candidates:
        candidate_id = clean_text(str(candidate.get("id") or ""))
        provenance = candidate.get("provenance")
        source_task_id = (
            provenance.get("source_task_id")
            if isinstance(provenance, Mapping)
            else None
        )
        if candidate_id and _positive_int(source_task_id) == outcome.task_id:
            linked.add(candidate_id)
    linked.discard("")

    refs = [f"task:{outcome.task_id}"]
    refs.extend(f"pr:{url}" for url in pr_urls)
    if merge_commit:
        refs.append(f"merge:{merge_commit}")
    if version:
        refs.append(f"version:{version}")
    return {
        "task_id": outcome.task_id,
        "completed_at": _clip(clean_text(outcome.occurred_at), 80),
        "completion_kind": completion_kind,
        "resolution_authority": authority,
        "request_summary": sanitize_curation_text(outcome.request, limit=600),
        "result_summary": sanitize_curation_text(outcome.result_summary, limit=800),
        "direct_candidate_ids": sorted(linked)[:12],
        "parent_task_id": outcome.parent_task_id,
        "source_task_id": outcome.source_task_id,
        "pr_urls": list(pr_urls),
        "changed_files": list(changed_files),
        "merge_commit": merge_commit,
        "authoritative_branch": _safe_branch(
            str(getattr(promoted, "authoritative_branch", "") or "")
        ),
        "promoted_at": _clip(
            clean_text(str(getattr(promoted, "promoted_at", "") or "")),
            80,
        ),
        "authoritative_version": version,
        "verified_at": _clip(
            clean_text(
                str(
                    getattr(adopted, "verified_at", "")
                    or getattr(promoted, "verified_at", "")
                    or ""
                )
            ),
            80,
        ),
        "evidence_refs": list(_valid_evidence_refs(refs)),
    }


def sanitize_curation_text(value: object, *, limit: int) -> str:
    text = str(value or "")
    text = _MEMORY_BLOCK_PATTERN.sub("[memory-block-redacted]", text)
    text = _SECRET_PATTERN.sub(r"\1=[redacted]", text)
    text = _BEARER_PATTERN.sub("Bearer [redacted]", text)
    text = _CHAT_ID_PATTERN.sub(r"\1_id=[redacted]", text)
    text = _MEMORY_ID_PATTERN.sub("[memory-ref-redacted]", text)
    text = _ABSOLUTE_PATH_PATTERN.sub("[local-path-redacted]", text)
    text = _PRIVATE_STATE_PATTERN.sub("[private-state-redacted]", text)
    return _clip(clean_text(text), limit)


def _safe_pr_urls(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = clean_text(str(value or "")).rstrip("/")
        if cleaned and _PR_URL_PATTERN.fullmatch(cleaned) and cleaned not in output:
            output.append(cleaned)
        if len(output) >= 4:
            break
    return tuple(output)


def _safe_changed_files(values: Iterable[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = clean_text(str(value or "")).replace("\\", "/")
        if (
            not cleaned
            or cleaned.startswith(("/", "../", ".enoch/"))
            or "/../" in cleaned
            or re.match(r"^[A-Za-z]:/", cleaned)
        ):
            continue
        cleaned = _clip(cleaned, 240)
        if cleaned not in output:
            output.append(cleaned)
        if len(output) >= 40:
            break
    return tuple(output)


def _safe_revision(value: str) -> str:
    cleaned = clean_text(value)
    return cleaned if re.fullmatch(r"[0-9a-fA-F]{7,64}", cleaned) else ""


def _safe_version(value: str) -> str:
    cleaned = clean_text(value)
    return cleaned if re.fullmatch(r"[A-Za-z0-9._-]{1,80}", cleaned) else ""


def _safe_branch(value: str) -> str:
    cleaned = clean_text(value)
    return cleaned if re.fullmatch(r"[A-Za-z0-9._/-]{1,160}", cleaned) else ""


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(-1_000_000, min(parsed, 1_000_000))


def _valid_evidence_refs(values: Iterable[object]) -> tuple[str, ...]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        ref = clean_text(str(value or ""))
        if (
            not ref
            or ref in seen
            or not any(pattern.fullmatch(ref) for pattern in _EVIDENCE_REF_PATTERNS)
        ):
            continue
        seen.add(ref)
        output.append(ref)
        if len(output) >= 64:
            break
    return tuple(output)


def load_curations(root: Path | None = None, *, limit: int = 100) -> tuple[SemanticCuration, ...]:
    path = curation_index_path(root)
    if not path.exists() or limit <= 0:
        return ()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    items: list[SemanticCuration] = []
    for line in reversed(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = _curation_from_json(raw)
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    items.reverse()
    return tuple(items)


def curation_evidence_refs(curation: SemanticCuration | None) -> tuple[str, ...]:
    if curation is None:
        return ()
    return _valid_evidence_refs(
        ref
        for suggestion in curation.remove_suggestions
        for ref in suggestion.evidence_refs
    )


def latest_remove_suggestion(
    candidate_id: str,
    root: Path | None = None,
    *,
    classification: str = "",
) -> tuple[SemanticCuration, RemoveSuggestion] | None:
    wanted_id = clean_text(candidate_id).lower().lstrip("#")
    wanted_classification = clean_text(classification).lower()
    for curation in reversed(load_curations(root)):
        for suggestion in curation.remove_suggestions:
            if suggestion.candidate_id.lower() != wanted_id:
                continue
            if (
                wanted_classification
                and suggestion.classification != wanted_classification
            ):
                continue
            return curation, suggestion
    return None


def candidate_scope_is_safe(candidate: Mapping[str, object]) -> bool:
    text = " ".join(
        clean_text(str(candidate.get(field) or ""))
        for field in ("title", "proposed_change")
    )
    return not _unsafe_scope(text)


def _recommendation(
    payload: Mapping[str, object],
    known: Mapping[str, Mapping[str, object]],
) -> CandidateRecommendation | None:
    raw_id = payload.get("recommended_candidate_id")
    if raw_id is None:
        return None
    candidate_id = clean_text(str(raw_id))
    if not candidate_id or candidate_id not in known:
        raise CurationError("semantic curator recommended an unknown candidate ID")
    fields = {
        "reason": _required_text(payload, "recommendation_reason"),
        "scope_guidance": _required_text(payload, "scope_guidance"),
        "risk_guidance": _required_text(payload, "risk_guidance"),
        "test_plan_guidance": _required_text(payload, "test_plan_guidance"),
    }
    guidance = " ".join(
        fields[key] for key in ("scope_guidance", "risk_guidance", "test_plan_guidance")
    )
    if not candidate_scope_is_safe(known[candidate_id]) or _unsafe_scope(guidance):
        raise CurationError("semantic curator recommended protected or dangerous scope")
    return CandidateRecommendation(candidate_id=candidate_id, **fields)


def _remove_suggestions(
    raw_items: object,
    known: Mapping[str, Mapping[str, object]],
    known_evidence: Mapping[str, Mapping[str, object]],
) -> tuple[RemoveSuggestion, ...]:
    if not isinstance(raw_items, list):
        raise CurationError("remove_suggestions must be a JSON array")
    suggestions: list[RemoveSuggestion] = []
    seen: set[str] = set()
    for raw in raw_items[:20]:
        expected = {"candidate_id", "classification", "reason", "evidence_refs"}
        legacy = expected - {"evidence_refs"}
        if (
            not isinstance(raw, dict)
            or frozenset(raw) not in {frozenset(expected), frozenset(legacy)}
        ):
            raise CurationError("remove suggestion has an invalid schema")
        candidate_id = clean_text(str(raw.get("candidate_id") or ""))
        classification = clean_text(str(raw.get("classification") or "")).lower()
        reason = _required_text(raw, "reason")
        evidence_refs = _response_evidence_refs(raw.get("evidence_refs", ()), known_evidence)
        if candidate_id not in known:
            raise CurationError("semantic curator suggested removing an unknown candidate ID")
        if classification not in REMOVE_CLASSIFICATIONS:
            raise CurationError("semantic curator returned an invalid removal classification")
        if classification in {"already-resolved", "superseded"}:
            if not evidence_refs:
                raise CurationError(
                    f"{classification} removal suggestion requires completion evidence"
                )
            if not _supports_resolution(
                known[candidate_id],
                evidence_refs,
                known_evidence,
            ):
                raise CurationError(
                    f"{classification} removal suggestion lacks authoritative evidence"
                )
        if candidate_id not in seen:
            suggestions.append(
                RemoveSuggestion(candidate_id, classification, reason, evidence_refs)
            )
            seen.add(candidate_id)
    return tuple(suggestions)


def _known_evidence(
    evidence: Iterable[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    known: dict[str, Mapping[str, object]] = {}
    for item in evidence:
        refs = _valid_evidence_refs(item.get("evidence_refs", ()))
        for ref in refs:
            known.setdefault(ref, item)
    return known


def _response_evidence_refs(
    raw: object,
    known: Mapping[str, Mapping[str, object]],
) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        raise CurationError("remove suggestion evidence_refs must be a JSON array")
    refs = _valid_evidence_refs(raw)
    if len(refs) != len(raw):
        raise CurationError("remove suggestion contains a malformed evidence ref")
    if any(ref not in known for ref in refs):
        raise CurationError("remove suggestion cites unknown completion evidence")
    return refs


def _supports_resolution(
    candidate: Mapping[str, object],
    refs: Iterable[str],
    known: Mapping[str, Mapping[str, object]],
) -> bool:
    authorities = {
        clean_text(str(known[ref].get("resolution_authority") or ""))
        for ref in refs
        if ref in known
    }
    if "authoritative-body" in authorities:
        return True
    if _candidate_requires_body_change(candidate):
        return False
    return "task-completion" in authorities


def _candidate_requires_body_change(candidate: Mapping[str, object]) -> bool:
    text = " ".join(
        clean_text(str(candidate.get(field) or ""))
        for field in ("title", "proposed_change", "test_plan")
    )
    return bool(_BODY_CHANGE_PATTERN.search(text))


def _new_candidates(raw_items: object) -> tuple[NewCandidateSuggestion, ...]:
    if not isinstance(raw_items, list):
        raise CurationError("new_candidates must be a JSON array")
    expected = {"title", "rationale", "proposed_change", "expected_benefit", "risk", "test_plan"}
    suggestions: list[NewCandidateSuggestion] = []
    for raw in raw_items[:3]:
        if not isinstance(raw, dict) or set(raw) != expected:
            raise CurationError("new candidate has an invalid schema")
        fields = {field: _required_text(raw, field) for field in expected}
        if len(fields["title"]) > 160 or any(len(value) > 1000 for value in fields.values()):
            raise CurationError("new candidate exceeds bounded field limits")
        if _unsafe_scope(" ".join(fields.values())):
            raise CurationError("new candidate has protected or dangerous scope")
        suggestions.append(NewCandidateSuggestion(**fields))
    return tuple(suggestions)


def _required_text(payload: Mapping[str, object], key: str) -> str:
    raw_value = clean_text(str(payload.get(key) or ""))
    if not raw_value:
        raise CurationError(f"semantic curator omitted {key}")
    if len(raw_value) > 1000:
        raise CurationError(f"semantic curator exceeded the {key} limit")
    return sanitize_curation_text(raw_value, limit=1000)


def _unsafe_scope(text: str) -> bool:
    return bool(_PROTECTED_ACTION_PATTERN.search(text) or _DANGEROUS_SCOPE_PATTERN.search(text))


def _json_object(response: object) -> object:
    if not isinstance(response, str):
        return None
    stripped = response.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _curation_id() -> str:
    return "curation-" + uuid4().hex


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def _curation_from_json(raw: object) -> SemanticCuration | None:
    if not isinstance(raw, dict):
        return None
    try:
        recommendation_raw = raw.get("recommendation")
        recommendation = (
            CandidateRecommendation(**recommendation_raw)
            if isinstance(recommendation_raw, dict)
            else None
        )
        remove = tuple(
            RemoveSuggestion(
                candidate_id=clean_text(str(item.get("candidate_id") or "")),
                classification=clean_text(
                    str(item.get("classification") or "")
                ).lower(),
                reason=clean_text(str(item.get("reason") or "")),
                evidence_refs=_valid_evidence_refs(item.get("evidence_refs", ())),
            )
            for item in raw.get("remove_suggestions", [])
            if isinstance(item, dict)
        )
        new = tuple(
            NewCandidateSuggestion(**item)
            for item in raw.get("new_candidates", [])
            if isinstance(item, dict)
        )
        return SemanticCuration(
            id=clean_text(str(raw.get("id") or "")),
            created_at=str(raw.get("created_at") or ""),
            status=clean_text(str(raw.get("status") or "")),
            input_candidate_ids=tuple(str(item) for item in raw.get("input_candidate_ids", [])),
            input_evidence_refs=_valid_evidence_refs(raw.get("input_evidence_refs", ())),
            recommendation=recommendation,
            remove_suggestions=remove,
            new_candidates=new,
            new_candidate_ids=tuple(str(item) for item in raw.get("new_candidate_ids", [])),
            fallback_reason=clean_text(str(raw.get("fallback_reason") or "")),
        )
    except (TypeError, ValueError):
        return None
