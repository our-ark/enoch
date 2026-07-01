from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import shutil
import subprocess
from urllib import error, request


class SkillsError(RuntimeError):
    pass


@dataclass(frozen=True)
class SkillInfo:
    name: str
    path: str
    version: str = ""
    description: str = ""
    summary: str = ""
    exposure: str = ""


@dataclass(frozen=True)
class AgentSkills:
    name: str
    root: Path
    skills: tuple[SkillInfo, ...]


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
        data = _parse_simple_yaml(_package_text("identity.yaml"))
        package_mode = True
    else:
        data = _parse_simple_yaml(identity_path.read_text(encoding="utf-8"))
        package_mode = False

    name = str(data.get("name") or agent_root.name).strip() or agent_root.name
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        return AgentSkills(name=name, root=agent_root, skills=())

    skills = tuple(
        _skill_info(raw, agent_root, package_mode=package_mode)
        for raw in raw_skills
        if isinstance(raw, dict) and str(raw.get("name") or "").strip()
    )
    return AgentSkills(name=name, root=agent_root, skills=skills)


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
    data = _parse_simple_yaml(identity_text)
    agent_name = str(data.get("name") or name).strip() or name
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        return AgentSkills(name=agent_name, root=Path(f"github.com/our-ark/{name}@main"), skills=())

    skills = tuple(
        _skill_info(raw, Path(f"github.com/our-ark/{name}@main"), github_agent=name)
        for raw in raw_skills
        if isinstance(raw, dict) and str(raw.get("name") or "").strip()
    )
    return AgentSkills(name=agent_name, root=Path(f"github.com/our-ark/{name}@main"), skills=skills)


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


def _skill_info(
    raw: dict[str, object],
    agent_root: Path,
    *,
    package_mode: bool = False,
    github_agent: str = "",
) -> SkillInfo:
    name = str(raw.get("name") or "").strip()
    path = str(raw.get("path") or "").strip()
    version = str(raw.get("version") or "").strip()
    description = str(raw.get("description") or "").strip()
    exposure = str(raw.get("exposure") or raw.get("visibility") or "").strip()
    metadata = _skill_metadata(agent_root, path, package_mode=package_mode, github_agent=github_agent)
    return SkillInfo(
        name=name,
        path=path,
        version=version or metadata.get("version", ""),
        description=description,
        summary=metadata.get("summary", "") or metadata.get("description", ""),
        exposure=exposure or metadata.get("exposure", ""),
    )


def _skill_metadata(
    agent_root: Path,
    path: str,
    *,
    package_mode: bool = False,
    github_agent: str = "",
) -> dict[str, str]:
    if not path:
        return {}
    if github_agent:
        try:
            data = _parse_simple_yaml(_github_text(github_agent, f"{path}/skill.yaml"))
        except SkillsError:
            return {}
        return {key: str(value).strip() for key, value in data.items() if isinstance(value, (str, int))}
    metadata_path = agent_root / path / "skill.yaml"
    if not metadata_path.exists():
        if package_mode:
            return _package_skill_metadata(path)
        return {}
    data = _parse_simple_yaml(metadata_path.read_text(encoding="utf-8"))
    return {key: str(value).strip() for key, value in data.items() if isinstance(value, (str, int))}


def _package_skill_metadata(path: str) -> dict[str, str]:
    relative = _package_relative_skill_path(path)
    if not relative:
        return {}
    try:
        text = _package_text(f"{relative}/skill.yaml")
    except (FileNotFoundError, ModuleNotFoundError):
        return {}
    data = _parse_simple_yaml(text)
    return {key: str(value).strip() for key, value in data.items() if isinstance(value, (str, int))}


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


def _parse_simple_yaml(text: str) -> dict[str, object]:
    data: dict[str, object] = {}
    current_key: str | None = None
    current_list: list[dict[str, str] | str] | None = None
    current_item: dict[str, str] | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0:
            key, value = _split_key_value(line)
            current_key = key
            current_item = None
            if value:
                data[key] = _clean_value(value)
                current_list = None
            else:
                current_list = []
                data[key] = current_list
            continue
        if current_key is None:
            continue
        if line.startswith("- "):
            if current_list is None:
                current_list = []
                data[current_key] = current_list
            item = line[2:].strip()
            if ":" in item:
                key, value = _split_key_value(item)
                current_item = {key: _clean_value(value)}
                current_list.append(current_item)
            else:
                current_item = None
                current_list.append(_clean_value(item))
            continue
        if current_item is not None and ":" in line:
            key, value = _split_key_value(line)
            current_item[key] = _clean_value(value)
    return data


def _split_key_value(line: str) -> tuple[str, str]:
    key, separator, value = line.partition(":")
    if not separator:
        return line.strip(), ""
    return key.strip(), value.strip()


def _clean_value(value: str) -> str:
    return value.strip().strip('"').strip("'")
