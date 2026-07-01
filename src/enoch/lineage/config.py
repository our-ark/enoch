from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enoch.config import read_section


DEFAULT_IMPORTANT_TITLE_WORDS = (
    "bug",
    "fix",
    "security",
    "doctor",
    "restart",
    "runtime",
    "crash",
    "rollback",
    "token",
    "permission",
)
DEFAULT_IMPORTANT_FILE_PREFIXES = (
    "src/enoch/brain.py",
    "src/enoch/config.py",
    "src/enoch/daemon.py",
    "src/enoch/github/workflow.py",
    "src/enoch/immune.py",
    "src/enoch/telegram/bot.py",
)


@dataclass(frozen=True)
class LineageSettings:
    important_title_words: tuple[str, ...] = DEFAULT_IMPORTANT_TITLE_WORDS
    important_file_prefixes: tuple[str, ...] = DEFAULT_IMPORTANT_FILE_PREFIXES


def lineage_settings(root: Path | None = None) -> LineageSettings:
    section = read_section("lineage", root)
    return LineageSettings(
        important_title_words=_csv_values(
            section.get("important_title_words"),
            DEFAULT_IMPORTANT_TITLE_WORDS,
            lower=True,
        ),
        important_file_prefixes=_csv_values(
            section.get("important_file_prefixes"),
            DEFAULT_IMPORTANT_FILE_PREFIXES,
        ),
    )


def _csv_values(value: str | None, default: tuple[str, ...], *, lower: bool = False) -> tuple[str, ...]:
    if value is None:
        return default
    items: list[str] = []
    for raw_item in value.split(","):
        item = raw_item.strip()
        if lower:
            item = item.lower()
        if item and item not in items:
            items.append(item)
    return tuple(items) if items else default
