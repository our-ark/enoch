from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


MetadataReader = Callable[[str], str | None]


class CatalogError(RuntimeError):
    """Raised when a skill catalog cannot be parsed or constructed."""


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


def parse_agent_catalog(
    identity_text: str,
    root: Path,
    metadata_reader: MetadataReader | None = None,
) -> AgentSkills:
    """Build a catalog from identity YAML and optional skill metadata."""

    data = parse_simple_yaml(identity_text)
    name = str(data.get("name") or root.name).strip() or root.name
    raw_skills = data.get("skills")
    if not isinstance(raw_skills, list):
        return AgentSkills(name=name, root=root, skills=())

    skills = tuple(
        skill_info(raw, metadata_reader=metadata_reader)
        for raw in raw_skills
        if isinstance(raw, dict) and str(raw.get("name") or "").strip()
    )
    return AgentSkills(name=name, root=root, skills=skills)


def skill_info(
    raw: dict[str, object],
    metadata_reader: MetadataReader | None = None,
) -> SkillInfo:
    name = str(raw.get("name") or "").strip()
    path = str(raw.get("path") or "").strip()
    version = str(raw.get("version") or "").strip()
    description = str(raw.get("description") or "").strip()
    exposure = str(raw.get("exposure") or raw.get("visibility") or "").strip()
    metadata: dict[str, str] = {}
    if path and metadata_reader is not None:
        try:
            text = metadata_reader(path)
        except (OSError, CatalogError):
            text = None
        if text:
            metadata = string_metadata(parse_simple_yaml(text))

    return SkillInfo(
        name=name,
        path=path,
        version=version or metadata.get("version", ""),
        description=description,
        summary=metadata.get("summary", "") or metadata.get("description", ""),
        exposure=exposure or metadata.get("exposure", ""),
    )


def string_metadata(data: dict[str, object]) -> dict[str, str]:
    return {
        key: str(value).strip()
        for key, value in data.items()
        if isinstance(value, (str, int))
    }


def parse_simple_yaml(text: str) -> dict[str, object]:
    """Parse the small manifest-oriented YAML subset used by Our Ark skills."""

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
