from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Origin:
    ark: str
    created_by: str
    born_in_repo: str


@dataclass(frozen=True)
class Body:
    package: str
    source_path: str
    identity_file: str


@dataclass(frozen=True)
class Identity:
    name: str
    kind: str
    role: str
    generation: int
    ancestor: str | None
    origin: Origin
    mission: str
    principles: list[str]
    body: Body


def load_identity(path: Path | None = None) -> Identity:
    """Load Enoch's versioned identity from her body."""
    text = _read_identity_text(path)
    data = _parse_enoch_yaml(text)

    return Identity(
        name=str(data["name"]),
        kind=str(data["kind"]),
        role=str(data["role"]),
        generation=int(data["generation"]),
        ancestor=str(data["ancestor"]) if data.get("ancestor") else None,
        origin=Origin(**data["origin"]),
        mission=str(data["mission"]),
        principles=list(data["principles"]),
        body=Body(**data["body"]),
    )


def _read_identity_text(path: Path | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")

    identity = resources.files("enoch").joinpath("identity.yaml")
    return identity.read_text(encoding="utf-8")


def identity_file_path(root: Path | None = None) -> Path:
    return Path.cwd() / "src" / "enoch" / "identity.yaml" if root is None else root / "src" / "enoch" / "identity.yaml"


def update_mission(mission: str, root: Path | None = None) -> str:
    cleaned = " ".join(mission.split())
    if not cleaned:
        raise ValueError("Mission cannot be empty.")

    path = identity_file_path(root)
    lines = path.read_text(encoding="utf-8").splitlines()
    updated: list[str] = []
    replaced = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("mission:"):
            updated.extend(["mission: >", f"  {cleaned}"])
            replaced = True
            index += 1
            while index < len(lines):
                old_line = lines[index]
                if not old_line.strip():
                    updated.append(old_line)
                    index += 1
                    break
                if old_line == old_line.lstrip(" "):
                    break
                index += 1
            continue
        updated.append(line)
        index += 1

    if not replaced:
        raise ValueError("Identity file does not contain a mission field.")
    path.write_text("\n".join(updated) + "\n", encoding="utf-8")
    return cleaned


def _parse_enoch_yaml(text: str) -> dict[str, Any]:
    """Parse the small YAML subset used by Enoch's identity file."""
    data: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[Any] | None = None
    current_map: dict[str, str] | None = None
    current_list_map: dict[str, str] | None = None
    folded_key: str | None = None
    folded_lines: list[str] = []

    def finish_folded() -> None:
        nonlocal folded_key, folded_lines
        if folded_key is not None:
            data[folded_key] = " ".join(line.strip() for line in folded_lines).strip()
            folded_key = None
            folded_lines = []

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()

        if folded_key is not None and indent > 0:
            folded_lines.append(line)
            continue

        finish_folded()

        if indent == 0:
            key, value = _split_key_value(line)
            current_key = key
            current_list = None
            current_map = None
            current_list_map = None

            if value == ">":
                folded_key = key
                folded_lines = []
            elif value == "":
                if key in {"origin", "body"}:
                    current_map = {}
                    data[key] = current_map
                elif key == "principles":
                    current_list = []
                    data[key] = current_list
                else:
                    data[key] = {}
            else:
                data[key] = _scalar(value)
            continue

        if current_key is None:
            raise ValueError(f"Unexpected nested line: {raw_line}")

        if line.startswith("- "):
            if current_list is None:
                current_list = []
                data[current_key] = current_list

            item = line[2:].strip()
            if ":" in item:
                key, value = _split_key_value(item)
                current_list_map = {key: str(_scalar(value))}
                current_list.append(current_list_map)
            else:
                current_list_map = None
                current_list.append(item)
            continue

        key, value = _split_key_value(line)
        if current_list_map is not None:
            current_list_map[key] = str(_scalar(value))
            continue

        if current_map is None:
            current_map = {}
            data[current_key] = current_map
        current_map[key] = str(_scalar(value))

    finish_folded()
    return data


def _split_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise ValueError(f"Expected key/value line: {line}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def _scalar(value: str) -> str | int:
    if value.isdigit():
        return int(value)
    return value
