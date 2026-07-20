from __future__ import annotations

from importlib import resources
from pathlib import Path
import shutil
import subprocess
from urllib import error, request

from enoch.runtime_dependencies import activate_runtime_dependencies


activate_runtime_dependencies()

from our_ark_skill_catalog import (  # noqa: E402
    AgentSkills,
    SkillInfo,
    parse_agent_catalog,
    parse_simple_yaml,
)


class SkillsError(RuntimeError):
    pass


_parse_simple_yaml = parse_simple_yaml


def skills_command(text: str, root: Path, *, prefix: str = "/") -> str:
    target = _target_argument(text, prefix=prefix)
    try:
        agent = load_agent_skills(target, root=root)
    except SkillsError as error:
        return f"Enoch could not inspect skills: {error}"
    return format_agent_skills(agent)


def load_agent_skills(target: str = "", *, root: Path | None = None) -> AgentSkills:
    if _is_published_agent_target(target):
        return _load_published_agent_skills(target.strip().lower())

    agent_root = resolve_agent_root(target, root=root)
    identity_path = _identity_path(agent_root)
    if identity_path is None:
        if not _is_self_target(target):
            raise SkillsError(f"Could not find an identity.yaml under {agent_root}.")
        identity_text = _package_text("identity.yaml")
        package_mode = True
    else:
        identity_text = identity_path.read_text(encoding="utf-8")
        package_mode = False

    return parse_agent_catalog(
        identity_text,
        agent_root,
        lambda path: _skill_metadata_text(
            agent_root,
            path,
            package_mode=package_mode,
        ),
    )


def resolve_agent_root(target: str = "", *, root: Path | None = None) -> Path:
    current = Path(root or Path.cwd()).resolve()
    target = target.strip()
    if _is_self_target(target):
        return current
    path = Path(target).expanduser()
    if not path.is_absolute():
        path = current / path
    return path.resolve()


def _is_path_target(target: str) -> bool:
    path = Path(target).expanduser()
    return path.is_absolute() or target.startswith(("~", ".")) or "/" in target


def _is_published_agent_target(target: str) -> bool:
    return bool(target.strip()) and not _is_self_target(target) and not _is_path_target(target.strip())


def _load_published_agent_skills(name: str) -> AgentSkills:
    identity_text = _github_text(name, f"src/{name}/identity.yaml")
    agent_root = Path(f"github.com/our-ark/{name}@main")
    return parse_agent_catalog(
        identity_text,
        agent_root,
        lambda path: _skill_metadata_text(
            agent_root,
            path,
            github_agent=name,
        ),
    )


def format_agent_skills(agent: AgentSkills) -> str:
    if not agent.skills:
        return f"{agent.name} has no declared skills."

    lines = [
        f"{agent.name} skills:",
        f"Root: {agent.root}",
        "Declared skills are descriptions for human inspection, not execution permissions.",
    ]
    for index, skill in enumerate(agent.skills, start=1):
        label = f"{skill.name} ({skill.exposure})" if skill.exposure else skill.name
        lines.append(f"{index}. {label}")
        if skill.version:
            lines.append(f"   Version: {skill.version}")
        if skill.exposure:
            lines.append(f"   Exposure: {skill.exposure}")
        summary = skill.summary or skill.description
        if summary:
            lines.append(f"   Summary: {summary}")
        lines.append(f"   Inspect: {skill.path}")
    return "\n".join(lines)


def _target_argument(text: str, *, prefix: str) -> str:
    command = f"{prefix}skills" if prefix else "skills"
    stripped = text.strip()
    if stripped.lower() == command:
        return ""
    if stripped.lower().startswith(f"{command} "):
        return stripped[len(command) :].strip()
    return stripped


def _identity_path(root: Path) -> Path | None:
    candidates = [
        root / "src" / root.name / "identity.yaml",
        root / "identity.yaml",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    matches = sorted(root.glob("src/*/identity.yaml"))
    return matches[0] if matches else None


def _skill_metadata_text(
    agent_root: Path,
    path: str,
    *,
    package_mode: bool = False,
    github_agent: str = "",
) -> str | None:
    if not path:
        return None
    if github_agent:
        try:
            return _github_text(github_agent, f"{path}/skill.yaml")
        except SkillsError:
            return None
    metadata_path = agent_root / path / "skill.yaml"
    if not metadata_path.exists():
        if package_mode:
            return _package_skill_metadata_text(path)
        return None
    return metadata_path.read_text(encoding="utf-8")


def _package_skill_metadata_text(path: str) -> str | None:
    relative = _package_relative_skill_path(path)
    if not relative:
        return None
    try:
        return _package_text(f"{relative}/skill.yaml")
    except (FileNotFoundError, ModuleNotFoundError):
        return None


def _package_relative_skill_path(path: str) -> str:
    cleaned = path.strip().strip("/")
    if cleaned.startswith("src/enoch/"):
        cleaned = cleaned.removeprefix("src/enoch/")
    if not cleaned.startswith("skills/") or ".." in cleaned.split("/"):
        return ""
    return cleaned


def _package_text(relative_path: str) -> str:
    target = resources.files("enoch")
    for part in relative_path.split("/"):
        target = target.joinpath(part)
    return target.read_text(encoding="utf-8")


def _github_text(agent: str, path: str) -> str:
    gh = shutil.which("gh")
    if gh is not None:
        result = subprocess.run(
            [
                gh,
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/our-ark/{agent}/contents/{path}?ref=main",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout

    url = f"https://raw.githubusercontent.com/our-ark/{agent}/main/{path}"
    try:
        with request.urlopen(url, timeout=10) as response:
            return response.read().decode("utf-8")
    except (OSError, error.URLError, UnicodeDecodeError) as exc:
        raise SkillsError(f"Could not read published Our-Ark agent {agent} from GitHub main.") from exc


def _is_self_target(target: str) -> bool:
    return not target.strip() or target.strip().lower() in {"enoch", "self", "me"}
