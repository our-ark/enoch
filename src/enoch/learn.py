from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Callable

from enoch.memory.paths import clean_text, now as current_time
from enoch.paths import enoch_home
from enoch.lineage.core import lineage_file, parse_lineage_parent
from enoch.skills import AgentSkills, SkillsError, _parse_simple_yaml, _published_text, load_agent_skills


MAX_SKILL_DOC_CHARS = 12000
MAX_METADATA_CHARS = 6000


class LearnError(RuntimeError):
    pass


@dataclass(frozen=True)
class PublishedSkill:
    name: str
    agent: str
    agent_name: str
    path: str
    description: str
    metadata: str
    instructions: str


@dataclass(frozen=True)
class LearnRequest:
    skill: str
    agent: str


@dataclass(frozen=True)
class PeerLearningObservation:
    id: str
    skill: str
    agent: str
    created_at: str


def peer_learning_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "learning" / "peers.jsonl"


def record_peer_learning_observation(
    request: LearnRequest,
    root: Path | None = None,
) -> PeerLearningObservation:
    skill = clean_text(request.skill)
    agent = clean_text(request.agent).lower()
    if not skill or not agent:
        raise ValueError("Peer learning requires a skill and source agent.")
    digest = hashlib.sha256(f"{agent}\0{skill.lower()}".encode("utf-8")).hexdigest()[:12]
    observation = PeerLearningObservation(
        id=f"peer-{digest}",
        skill=skill,
        agent=agent,
        created_at=current_time(),
    )
    path = peer_learning_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(observation), sort_keys=True) + "\n")
    return observation


def load_peer_learning_observations(
    root: Path | None = None,
    *,
    limit: int = 20,
) -> tuple[PeerLearningObservation, ...]:
    path = peer_learning_path(root)
    if not path.exists():
        return ()
    observations: list[PeerLearningObservation] = []
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
        if not isinstance(raw, dict):
            continue
        observation = PeerLearningObservation(
            id=clean_text(str(raw.get("id") or "")),
            skill=clean_text(str(raw.get("skill") or "")),
            agent=clean_text(str(raw.get("agent") or "")).lower(),
            created_at=str(raw.get("created_at") or ""),
        )
        if not observation.id or not observation.skill or not observation.agent or observation.id in seen:
            continue
        seen.add(observation.id)
        observations.append(observation)
        if len(observations) >= limit:
            break
    return tuple(observations)


def explore_peer_skills(
    agent: str,
    root: Path | None = None,
    *,
    loader: Callable[..., AgentSkills] = load_agent_skills,
) -> tuple[PeerLearningObservation, ...]:
    agent_name = clean_text(agent).lower()
    if not agent_name or agent_name in {"enoch", "self", "me"}:
        raise ValueError("Choose a non-parent peer agent to explore.")
    parent_path = lineage_file(root)
    if parent_path.exists():
        try:
            parent = parse_lineage_parent(parent_path.read_text(encoding="utf-8"))
        except OSError:
            parent = None
        if parent is not None:
            parent_repo_name = parent.repo.rstrip("/").removesuffix(".git").rsplit("/", 1)[-1].lower()
            if agent_name in {parent.name.lower(), parent_repo_name}:
                raise ValueError("Use inheritance, not peer learning, for the direct parent.")
    published = loader(agent_name, root=root)
    observations = [
        record_peer_learning_observation(
            LearnRequest(skill=skill.name, agent=agent_name),
            root,
        )
        for skill in published.skills
        if skill.name and skill.exposure != "hidden"
    ]
    return tuple(observations)


def load_published_skill(
    skill: str,
    agent: str,
    *,
    root: Path | None = None,
) -> PublishedSkill:
    skill_name = skill.strip()
    agent_name = agent.strip().lower()
    if not skill_name or not agent_name:
        raise LearnError("Use /learn <skill> from <agent>.")

    try:
        identity_text = _published_text(agent_name, f"src/{agent_name}/identity.yaml", root=root)
    except SkillsError as error:
        raise LearnError(f"Could not read published Our-Ark agent {agent_name}.") from error

    identity = _parse_simple_yaml(identity_text)
    declared_agent_name = str(identity.get("name") or agent_name).strip() or agent_name
    raw_skills = identity.get("skills")
    if not isinstance(raw_skills, list):
        raise LearnError(f"{declared_agent_name} has no declared skills.")

    matching_skill = _find_skill(raw_skills, skill_name)
    if matching_skill is None:
        raise LearnError(f"{declared_agent_name} does not declare skill {skill_name}.")

    path = str(matching_skill.get("path") or "").strip()
    if not path:
        raise LearnError(f"{declared_agent_name}'s {skill_name} skill has no path.")

    metadata = _optional_published_text(agent_name, f"{path}/skill.yaml", root=root)
    instructions = _optional_published_text(agent_name, f"{path}/SKILL.md", root=root)
    description = str(matching_skill.get("description") or "").strip()
    if not metadata and not instructions and not description:
        raise LearnError(f"{declared_agent_name}'s {skill_name} skill has no learnable description.")

    return PublishedSkill(
        name=str(matching_skill.get("name") or skill_name).strip() or skill_name,
        agent=agent_name,
        agent_name=declared_agent_name,
        path=path,
        description=description,
        metadata=metadata,
        instructions=instructions,
    )


def learn_skill_prompt(text: str, *, root: Path | None = None) -> str:
    request = parse_learn_request(text)
    if request is None:
        raise LearnError("Use /learn <skill> from <agent>.")
    skill = load_published_skill(request.skill, request.agent, root=root)
    return "\n\n".join(
        [
            f"Consider whether Enoch should learn the published skill {skill.name} from {skill.agent_name}.",
            "Learning means adapting the useful idea into Enoch's own body. Do not copy the source agent blindly.",
            "If the skill should be adapted, return a concise Enoch edit request using the normal edit-request marker.",
            "If it should not be adapted, explain why and do not request an edit.",
            "",
            f"Source: our-ark/{skill.agent}@main via configured forge",
            f"Skill path: {skill.path}",
            f"Declared description: {skill.description or '(none)'}",
            "skill.yaml:",
            _clip(skill.metadata or "(missing)", MAX_METADATA_CHARS),
            "SKILL.md:",
            _clip(skill.instructions or "(missing)", MAX_SKILL_DOC_CHARS),
        ]
    ).strip()


def learn_command(text: str, root: Path, *, prefix: str = "/") -> str:
    request = parse_learn_request(text, prefix=prefix)
    command = f"{prefix}learn" if prefix else "learn"
    if request is None:
        return f"Use {command} <skill> from <agent>."
    try:
        skill = load_published_skill(request.skill, request.agent, root=root)
    except LearnError as error:
        return f"Enoch could not inspect that skill: {error}"
    return format_published_skill(skill)


def parse_learn_request(text: str, *, prefix: str = "/") -> LearnRequest | None:
    command = f"{prefix}learn" if prefix else "learn"
    stripped = text.strip()
    if stripped.lower() == command:
        return None
    if stripped.lower().startswith(f"{command} "):
        stripped = stripped[len(command) :].strip()
    parts = stripped.split()
    if len(parts) != 3 or parts[1].lower() != "from":
        return None
    return LearnRequest(skill=parts[0], agent=parts[2])


def format_published_skill(skill: PublishedSkill) -> str:
    return "\n".join(
        [
            f"Enoch inspected {skill.agent_name}'s {skill.name} skill.",
            f"Source: our-ark/{skill.agent}@main via configured forge",
            f"Path: {skill.path}",
            f"skill.yaml: {len(skill.metadata)} chars",
            f"SKILL.md: {len(skill.instructions)} chars",
            "In chat, /learn <skill> from <agent> asks Enoch whether to adapt it.",
        ]
    )


def _find_skill(raw_skills: list[object], skill_name: str) -> dict[str, object] | None:
    for raw in raw_skills:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if name.lower() == skill_name.lower():
            return raw
    return None


def _optional_published_text(agent: str, path: str, *, root: Path | None = None) -> str:
    try:
        return _published_text(agent, path, root=root)
    except SkillsError:
        return ""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n[truncated]"
