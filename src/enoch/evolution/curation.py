from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 1
DEFAULT_CURATION_LIMIT = 24
REMOVE_CLASSIFICATIONS = {
    "duplicate",
    "superseded",
    "obsolete",
    "already-resolved",
    "context-only",
    "not-actionable",
}
CurationGenerator = Callable[[str], str]

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
) -> str:
    payload = {
        "mission": _clip(clean_text(mission), 2000),
        "evolution_theme": _clip(clean_text(theme), 500),
        "candidates": list(candidates),
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
    generator: CurationGenerator,
) -> SemanticCuration:
    snapshots = tuple(candidates)
    candidate_ids = tuple(clean_text(str(item.get("id") or "")) for item in snapshots)
    known = {candidate_id: item for candidate_id, item in zip(candidate_ids, snapshots) if candidate_id}
    response = generator(semantic_curation_prompt(mission, theme, snapshots))
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
    remove_suggestions = _remove_suggestions(payload.get("remove_suggestions"), known)
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
        recommendation=recommendation,
        remove_suggestions=remove_suggestions,
        new_candidates=new_candidates,
    )


def deterministic_fallback(
    candidates: Iterable[Mapping[str, object]],
    *,
    reason: str,
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
        recommendation=recommendation,
        fallback_reason=_clip(clean_text(reason), 300),
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
) -> tuple[RemoveSuggestion, ...]:
    if not isinstance(raw_items, list):
        raise CurationError("remove_suggestions must be a JSON array")
    suggestions: list[RemoveSuggestion] = []
    seen: set[str] = set()
    for raw in raw_items[:20]:
        if not isinstance(raw, dict) or set(raw) != {"candidate_id", "classification", "reason"}:
            raise CurationError("remove suggestion has an invalid schema")
        candidate_id = clean_text(str(raw.get("candidate_id") or ""))
        classification = clean_text(str(raw.get("classification") or "")).lower()
        reason = _required_text(raw, "reason")
        if candidate_id not in known:
            raise CurationError("semantic curator suggested removing an unknown candidate ID")
        if classification not in REMOVE_CLASSIFICATIONS:
            raise CurationError("semantic curator returned an invalid removal classification")
        if candidate_id not in seen:
            suggestions.append(RemoveSuggestion(candidate_id, classification, reason))
            seen.add(candidate_id)
    return tuple(suggestions)


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
    value = clean_text(str(payload.get(key) or ""))
    if not value:
        raise CurationError(f"semantic curator omitted {key}")
    if len(value) > 1000:
        raise CurationError(f"semantic curator exceeded the {key} limit")
    return value


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
            RemoveSuggestion(**item)
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
            recommendation=recommendation,
            remove_suggestions=remove,
            new_candidates=new,
            new_candidate_ids=tuple(str(item) for item in raw.get("new_candidate_ids", [])),
            fallback_reason=clean_text(str(raw.get("fallback_reason") or "")),
        )
    except (TypeError, ValueError):
        return None
