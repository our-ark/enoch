from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enoch.config import read_section


@dataclass(frozen=True)
class MemorySettings:
    long_term_prompt_max_chars: int = 8_000
    identity_prompt_max_chars: int = 4_000
    long_term_memory_text_max_chars: int = 500
    long_term_memory_subject_max_chars: int = 80


DEFAULT_MEMORY_SETTINGS = MemorySettings()


def memory_settings(root: Path | None = None) -> MemorySettings:
    section = read_section("memory", root)
    defaults = DEFAULT_MEMORY_SETTINGS
    return MemorySettings(
        long_term_prompt_max_chars=_positive_int(
            section.get("long_term_prompt_max_chars"), defaults.long_term_prompt_max_chars
        ),
        identity_prompt_max_chars=_positive_int(
            section.get("identity_prompt_max_chars"), defaults.identity_prompt_max_chars
        ),
        long_term_memory_text_max_chars=_positive_int(
            section.get("long_term_memory_text_max_chars"),
            defaults.long_term_memory_text_max_chars,
        ),
        long_term_memory_subject_max_chars=_positive_int(
            section.get("long_term_memory_subject_max_chars"),
            defaults.long_term_memory_subject_max_chars,
        ),
    )


def _positive_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
