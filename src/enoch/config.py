from __future__ import annotations

from pathlib import Path

from enoch.paths import enoch_home


def config_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "config.yaml"


def read_config(root: Path | None = None) -> dict[str, dict[str, str]]:
    path = config_path(root)
    if not path.exists():
        return {}
    return _parse_simple_yaml(path.read_text(encoding="utf-8"))


def read_section(name: str, root: Path | None = None) -> dict[str, str]:
    return read_config(root).get(name, {})


def write_section_value(
    section: str,
    key: str,
    value: str | None,
    root: Path | None = None,
) -> None:
    path = config_path(root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []

    section_index = _find_section(lines, section)
    formatted = f"  {key}: {_quote_yaml(value)}" if value is not None else ""
    if section_index is None:
        if value is None:
            return
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"{section}:", formatted])
    else:
        insert_at = _section_end(lines, section_index)
        key_index = _find_key(lines, section_index, insert_at, key)
        if key_index is None:
            if value is not None:
                lines.insert(insert_at, formatted)
        elif value is None:
            del lines[key_index]
        else:
            lines[key_index] = formatted

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _parse_simple_yaml(text: str) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith((" ", "\t")) and line.endswith(":"):
            current = line[:-1].strip()
            data.setdefault(current, {})
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[current][key.strip()] = _clean_value(value)
    return data


def _find_section(lines: list[str], section: str) -> int | None:
    for index, raw_line in enumerate(lines):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.startswith((" ", "\t")) and line.endswith(":") and line[:-1].strip() == section:
            return index
    return None


def _section_end(lines: list[str], section_index: int) -> int:
    for index in range(section_index + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.startswith((" ", "\t")):
            return index
    return len(lines)


def _find_key(lines: list[str], start: int, end: int, key: str) -> int | None:
    for index in range(start + 1, end):
        line = lines[index].split("#", 1)[0].rstrip()
        if not line.startswith((" ", "\t")) or ":" not in line:
            continue
        found, _separator, _value = line.partition(":")
        if found.strip() == key:
            return index
    return None


def _quote_yaml(value: str | None) -> str:
    if value is None:
        return ""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _clean_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
