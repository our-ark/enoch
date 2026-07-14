from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Callable

from enoch.identity import Identity, identity_file_path, load_identity
from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import enoch_home


SCHEMA_VERSION = 1
BrainstormGenerator = Callable[[str], str]
_PROTECTED_SCOPE_PATTERN = re.compile(
    r"\b(?:identity|mission|secret|credential|permission|merge authority|auto[- ]?merge|deployment|destructive)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class BrainstormIdea:
    id: str
    theme: str
    mission: str
    title: str
    rationale: str
    proposed_change: str
    expected_benefit: str
    risk: str
    test_plan: str
    created_at: str


def brainstorm_index_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "evolve_brainstorms.jsonl"


def generate_brainstorm_ideas(
    theme: str,
    root: Path | None = None,
    *,
    mission: str = "",
    generator: BrainstormGenerator | None = None,
    limit: int = 3,
) -> tuple[BrainstormIdea, ...]:
    cleaned_theme = clean_text(theme)
    if not cleaned_theme:
        raise ValueError("Set an evolution theme before brainstorming.")
    cleaned_mission = clean_text(mission) or clean_text(_identity_for_root(root).mission)
    prompt = brainstorm_prompt(cleaned_theme, cleaned_mission, limit=limit)
    raw = (generator or _default_generator(root))(prompt)
    ideas = _parse_brainstorm_response(raw, cleaned_theme, cleaned_mission, limit=limit)
    if not ideas:
        raise ValueError("The brainstorming model did not return any valid bounded evolution ideas.")
    _append_ideas(ideas, root)
    return ideas


def load_brainstorm_ideas(
    root: Path | None = None,
    *,
    theme: str = "",
    limit: int = 20,
) -> tuple[BrainstormIdea, ...]:
    path = brainstorm_index_path(root)
    if not path.exists():
        return ()
    wanted_theme = clean_text(theme).lower()
    ideas: list[BrainstormIdea] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    for line in reversed(lines):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        idea = _idea_from_json(raw)
        if idea is None or idea.id in seen:
            continue
        if wanted_theme and idea.theme.lower() != wanted_theme:
            continue
        seen.add(idea.id)
        ideas.append(idea)
        if len(ideas) >= limit:
            break
    return tuple(ideas)


def brainstorm_prompt(theme: str, mission: str, *, limit: int = 3) -> str:
    return "\n".join(
        [
            "Brainstorm bounded improvements to Enoch's own repository body.",
            f"Mission: {mission}",
            f"Current evolution theme: {theme}",
            f"Return at most {max(1, limit)} ideas as a JSON array and no prose.",
            "Each object must contain: title, rationale, proposed_change, expected_benefit, risk, test_plan.",
            "Ideas must be small, reversible, testable, and implementable on a feature branch.",
            "Do not propose changing identity, mission, secrets, permissions, merge authority, deployment, or destructive behavior.",
            "Prefer concrete system improvements over vague goals or model-training proposals.",
        ]
    )


def _default_generator(root: Path | None) -> BrainstormGenerator:
    def generate(prompt: str) -> str:
        from enoch.brain import respond

        return respond(load_identity(), prompt, cwd=root)

    return generate


def _parse_brainstorm_response(
    response: str,
    theme: str,
    mission: str,
    *,
    limit: int,
) -> tuple[BrainstormIdea, ...]:
    payload = _json_payload(response)
    if not isinstance(payload, list):
        return ()
    created_at = current_time()
    ideas: list[BrainstormIdea] = []
    for raw in payload[: max(1, limit)]:
        if not isinstance(raw, dict):
            continue
        fields = {
            key: clean_text(str(raw.get(key) or ""))
            for key in (
                "title",
                "rationale",
                "proposed_change",
                "expected_benefit",
                "risk",
                "test_plan",
            )
        }
        if not fields["title"] or not fields["proposed_change"] or not fields["test_plan"]:
            continue
        if _PROTECTED_SCOPE_PATTERN.search(f"{fields['title']} {fields['proposed_change']}"):
            continue
        ideas.append(
            BrainstormIdea(
                id=_idea_id(theme, fields["title"]),
                theme=theme,
                mission=mission,
                created_at=created_at,
                **fields,
            )
        )
    return tuple(ideas)


def _json_payload(response: str) -> object:
    stripped = response.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
    if fenced:
        stripped = fenced.group(1).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("[")
        end = stripped.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            return None


def _append_ideas(ideas: tuple[BrainstormIdea, ...], root: Path | None) -> None:
    path = brainstorm_index_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for idea in ideas:
            handle.write(json.dumps({"schema_version": SCHEMA_VERSION, **asdict(idea)}, sort_keys=True) + "\n")


def _idea_from_json(raw: object) -> BrainstormIdea | None:
    if not isinstance(raw, dict):
        return None
    values = {
        field: clean_text(str(raw.get(field) or ""))
        for field in BrainstormIdea.__dataclass_fields__
    }
    if not values["id"] or not values["theme"] or not values["title"] or not values["proposed_change"]:
        return None
    return BrainstormIdea(**values)


def _idea_id(theme: str, title: str) -> str:
    digest = hashlib.sha256(f"{theme.lower()}\0{title.lower()}".encode("utf-8")).hexdigest()[:12]
    return f"brainstorm-{digest}"


def _identity_for_root(root: Path | None) -> Identity:
    if root is not None:
        path = identity_file_path(root)
        if path.exists():
            return load_identity(path)
    return load_identity()
