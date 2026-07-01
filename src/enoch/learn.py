from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enoch.skills import SkillsError, _github_text, _parse_simple_yaml


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


def load_published_skill(skill: str, agent: str) -> PublishedSkill:
    skill_name = skill.strip()
    agent_name = agent.strip().lower()
    if not skill_name or not agent_name:
        raise LearnError("Use /learn <skill> from <agent>.")

    try:
        identity_text = _github_text(agent_name, f"src/{agent_name}/identity.yaml")
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

    metadata = _optional_github_text(agent_name, f"{path}/skill.yaml")
    instructions = _optional_github_text(agent_name, f"{path}/SKILL.md")
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
    skill = load_published_skill(request.skill, request.agent)
    return "\n\n".join(
        [
            f"Consider whether Enoch should learn the published skill {skill.name} from {skill.agent_name}.",
            "Learning means adapting the useful idea into Enoch's own body. Do not copy the source agent blindly.",
            "If the skill should be adapted, return a concise Enoch edit request using the normal edit-request marker.",
            "If it should not be adapted, explain why and do not request an edit.",
            "",
            f"Source: github.com/our-ark/{skill.agent}@main",
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
        skill = load_published_skill(request.skill, request.agent)
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
            f"Source: github.com/our-ark/{skill.agent}@main",
            f"Path: {skill.path}",
            f"skill.yaml: {len(skill.metadata)} chars",
            f"SKILL.md: {len(skill.instructions)} chars",
            "In Telegram, /learn <skill> from <agent> asks Enoch whether to adapt it.",
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


def _optional_github_text(agent: str, path: str) -> str:
    try:
        return _github_text(agent, path)
    except SkillsError:
        return ""


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}\n[truncated]"
