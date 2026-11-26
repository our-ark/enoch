from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from enoch.config import read_section, write_section_value


DEFAULT_TASK_TIMEOUT_SECONDS = 10 * 60
MIN_TASK_TIMEOUT_SECONDS = 60
MAX_TASK_TIMEOUT_SECONDS = 2 * 60 * 60
_DURATION_PATTERN = re.compile(
    r"^\s*(?P<count>\d+)\s*"
    r"(?P<unit>s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TaskSettings:
    timeout_seconds: int = DEFAULT_TASK_TIMEOUT_SECONDS
    uses_default_timeout: bool = True


def task_settings(root: Path | None = None) -> TaskSettings:
    raw = read_section("task", root).get("timeout_seconds", "").strip()
    try:
        timeout = int(raw)
    except (TypeError, ValueError):
        return TaskSettings()
    if not MIN_TASK_TIMEOUT_SECONDS <= timeout <= MAX_TASK_TIMEOUT_SECONDS:
        return TaskSettings()
    return TaskSettings(timeout_seconds=timeout, uses_default_timeout=False)


def task_timeout_seconds(root: Path | None = None) -> int:
    return task_settings(root).timeout_seconds


def parse_task_timeout(value: str) -> int:
    match = _DURATION_PATTERN.match(value)
    if match is None:
        raise ValueError("Task timeout must look like 10m, 30m, or 2h.")
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit.startswith("s"):
        multiplier = 1
    elif unit.startswith("m"):
        multiplier = 60
    else:
        multiplier = 60 * 60
    timeout = count * multiplier
    if not MIN_TASK_TIMEOUT_SECONDS <= timeout <= MAX_TASK_TIMEOUT_SECONDS:
        raise ValueError("Task timeout must be between 1m and 2h.")
    return timeout


def save_task_timeout(timeout_seconds: int | None, root: Path | None = None) -> TaskSettings:
    if (
        timeout_seconds is not None
        and not MIN_TASK_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TASK_TIMEOUT_SECONDS
    ):
        raise ValueError("Task timeout must be between 1m and 2h.")
    write_section_value(
        "task",
        "timeout_seconds",
        str(timeout_seconds) if timeout_seconds is not None else None,
        root,
    )
    return task_settings(root)


PR_AUTO_MERGE_SECTION = "pr"
PR_AUTO_MERGE_KEY = "auto_merge"


def pr_auto_merge_enabled(root: Path | None = None) -> bool:
    """Whether a task's single published review is merged when ready (default off).

    Only the explicit stored value ``on`` enables it; anything else, including
    a missing or unreadable setting, means off.
    """

    try:
        raw = read_section(PR_AUTO_MERGE_SECTION, root).get(PR_AUTO_MERGE_KEY, "")
    except (OSError, ValueError):
        return False
    return str(raw).strip().lower() == "on"


def save_pr_auto_merge(enabled: bool, root: Path | None = None) -> bool:
    write_section_value(
        PR_AUTO_MERGE_SECTION,
        PR_AUTO_MERGE_KEY,
        "on" if enabled else "off",
        root,
    )
    return pr_auto_merge_enabled(root)


def format_pr_auto_merge(enabled: bool) -> str:
    return "on" if enabled else "off"


AUTO_UPDATE_SECTION = "update"
AUTO_UPDATE_KEY = "auto"


def auto_update_enabled(root: Path | None = None) -> bool:
    """Whether a merge Enoch lands on request is followed by /update (default on).

    Only the explicit stored value ``off`` disables it; a missing setting means
    on. An unreadable setting also means off, so a damaged config never
    restarts the daemon by itself.
    """

    try:
        raw = read_section(AUTO_UPDATE_SECTION, root).get(AUTO_UPDATE_KEY, "")
    except (OSError, ValueError):
        return False
    return str(raw).strip().lower() != "off"


def save_auto_update(enabled: bool, root: Path | None = None) -> bool:
    write_section_value(
        AUTO_UPDATE_SECTION,
        AUTO_UPDATE_KEY,
        "on" if enabled else "off",
        root,
    )
    return auto_update_enabled(root)


def format_task_timeout(seconds: int) -> str:
    if seconds % (60 * 60) == 0:
        return f"{seconds // (60 * 60)}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


BOARD_SECTION = "board"
BOARD_REPOST_KEY = "repost"
BOARD_REPOST_INTERVAL_KEY = "repost_interval_seconds"
BOARD_REPOST_THRESHOLD_KEY = "repost_after_messages"
DEFAULT_BOARD_REPOST_INTERVAL_SECONDS = 60 * 60
DEFAULT_BOARD_REPOST_THRESHOLD = 10
MIN_BOARD_REPOST_INTERVAL_SECONDS = 5 * 60
MAX_BOARD_REPOST_INTERVAL_SECONDS = 24 * 60 * 60
MIN_BOARD_REPOST_THRESHOLD = 1
MAX_BOARD_REPOST_THRESHOLD = 1000


@dataclass(frozen=True)
class BoardRepostSettings:
    """When the task board is posted again so it stays near the chat's end.

    Every ``interval_seconds`` Enoch checks how many chat messages appeared
    after the board; when there are more than ``threshold``, it posts a fresh
    board and retires the old one.
    """

    enabled: bool = True
    interval_seconds: int = DEFAULT_BOARD_REPOST_INTERVAL_SECONDS
    threshold: int = DEFAULT_BOARD_REPOST_THRESHOLD
    uses_defaults: bool = True


def board_repost_settings(root: Path | None = None) -> BoardRepostSettings:
    """Read the re-post setting (default on, 1h, more than 10 messages).

    Out-of-range values fall back to their defaults. An unreadable config
    means off, so a damaged config never posts messages by itself.
    """

    try:
        section = read_section(BOARD_SECTION, root)
    except (OSError, ValueError):
        return BoardRepostSettings(enabled=False, uses_defaults=False)
    switch = str(section.get(BOARD_REPOST_KEY, "")).strip().lower()
    interval = _bounded_int(
        section.get(BOARD_REPOST_INTERVAL_KEY),
        MIN_BOARD_REPOST_INTERVAL_SECONDS,
        MAX_BOARD_REPOST_INTERVAL_SECONDS,
    )
    threshold = _bounded_int(
        section.get(BOARD_REPOST_THRESHOLD_KEY),
        MIN_BOARD_REPOST_THRESHOLD,
        MAX_BOARD_REPOST_THRESHOLD,
    )
    return BoardRepostSettings(
        enabled=switch != "off",
        interval_seconds=interval or DEFAULT_BOARD_REPOST_INTERVAL_SECONDS,
        threshold=threshold or DEFAULT_BOARD_REPOST_THRESHOLD,
        uses_defaults=not switch and interval is None and threshold is None,
    )


def save_board_repost_settings(
    *,
    enabled: bool,
    interval_seconds: int | None = None,
    threshold: int | None = None,
    root: Path | None = None,
) -> BoardRepostSettings:
    """Store the switch; ``None`` keeps the stored interval or threshold."""

    if interval_seconds is not None and not (
        MIN_BOARD_REPOST_INTERVAL_SECONDS
        <= interval_seconds
        <= MAX_BOARD_REPOST_INTERVAL_SECONDS
    ):
        raise ValueError("Board re-post interval must be between 5m and 24h.")
    if threshold is not None and not (
        MIN_BOARD_REPOST_THRESHOLD <= threshold <= MAX_BOARD_REPOST_THRESHOLD
    ):
        raise ValueError(
            f"Board re-post message count must be between {MIN_BOARD_REPOST_THRESHOLD} "
            f"and {MAX_BOARD_REPOST_THRESHOLD}."
        )
    write_section_value(BOARD_SECTION, BOARD_REPOST_KEY, "on" if enabled else "off", root)
    if interval_seconds is not None:
        write_section_value(
            BOARD_SECTION, BOARD_REPOST_INTERVAL_KEY, str(interval_seconds), root,
        )
    if threshold is not None:
        write_section_value(BOARD_SECTION, BOARD_REPOST_THRESHOLD_KEY, str(threshold), root)
    return board_repost_settings(root)


def reset_board_repost_settings(root: Path | None = None) -> BoardRepostSettings:
    for key in (BOARD_REPOST_KEY, BOARD_REPOST_INTERVAL_KEY, BOARD_REPOST_THRESHOLD_KEY):
        write_section_value(BOARD_SECTION, key, None, root)
    return board_repost_settings(root)


def parse_board_repost_interval(value: str) -> int:
    match = _DURATION_PATTERN.match(value)
    if match is None:
        raise ValueError("Board re-post interval must look like 30m, 1h, or 2h.")
    count = int(match.group("count"))
    unit = match.group("unit").lower()
    if unit.startswith("s"):
        multiplier = 1
    elif unit.startswith("m"):
        multiplier = 60
    else:
        multiplier = 60 * 60
    interval = count * multiplier
    if not MIN_BOARD_REPOST_INTERVAL_SECONDS <= interval <= MAX_BOARD_REPOST_INTERVAL_SECONDS:
        raise ValueError("Board re-post interval must be between 5m and 24h.")
    return interval


def parse_board_repost_threshold(value: str) -> int:
    try:
        threshold = int(value.strip())
    except ValueError:
        raise ValueError("Board re-post message count must be a whole number, like 10.") from None
    if not MIN_BOARD_REPOST_THRESHOLD <= threshold <= MAX_BOARD_REPOST_THRESHOLD:
        raise ValueError(
            f"Board re-post message count must be between {MIN_BOARD_REPOST_THRESHOLD} "
            f"and {MAX_BOARD_REPOST_THRESHOLD}."
        )
    return threshold


def format_board_repost(settings: BoardRepostSettings) -> str:
    """One-line summary, e.g. ``every 1h after more than 10 new messages``."""

    if not settings.enabled:
        return "off"
    summary = (
        f"every {format_task_timeout(settings.interval_seconds)} after more than "
        f"{settings.threshold} new message{'s' if settings.threshold != 1 else ''}"
    )
    return f"{summary} (default)" if settings.uses_defaults else summary


def _bounded_int(raw: object, minimum: int, maximum: int) -> int | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None
