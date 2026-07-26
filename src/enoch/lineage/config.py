from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from enoch.config import read_section


DEFAULT_ASSESSMENT_BATCH_SIZE = 10
DEFAULT_MAX_DIFF_CHARS = 12_000
DEFAULT_SCAN_LIMIT = 500


@dataclass(frozen=True)
class LineageSettings:
    assessment_batch_size: int = DEFAULT_ASSESSMENT_BATCH_SIZE
    max_diff_chars: int = DEFAULT_MAX_DIFF_CHARS
    scan_limit: int = DEFAULT_SCAN_LIMIT


def lineage_settings(root: Path | None = None) -> LineageSettings:
    section = read_section("lineage", root)
    return LineageSettings(
        assessment_batch_size=_bounded_int(
            section.get("assessment_batch_size"),
            DEFAULT_ASSESSMENT_BATCH_SIZE,
            minimum=1,
            maximum=20,
        ),
        max_diff_chars=_bounded_int(
            section.get("max_diff_chars"),
            DEFAULT_MAX_DIFF_CHARS,
            minimum=1_000,
            maximum=50_000,
        ),
        scan_limit=_bounded_int(
            section.get("scan_limit"),
            DEFAULT_SCAN_LIMIT,
            minimum=20,
            maximum=5_000,
        ),
    )


def _bounded_int(
    value: str | None,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    try:
        parsed = int(str(value or "").strip())
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))
