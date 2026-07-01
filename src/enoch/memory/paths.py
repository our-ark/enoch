from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from enoch.paths import enoch_home


def memory_dir(root: Path | None = None) -> Path:
    return enoch_home(root) / "memory"


def long_term_memory_path(root: Path | None = None) -> Path:
    return memory_dir(root) / "long_term.json"


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_text(text: str) -> str:
    return " ".join(text.split())


def normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def clip_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 14].rstrip()}\n[truncated]"


def dedupe(items: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        key = normalize(text)
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def allowed_value(value: str, allowed: set[str], default: str) -> str:
    normalized = value.strip().lower()
    return normalized if normalized in allowed else default
