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
