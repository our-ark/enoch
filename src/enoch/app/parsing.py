from __future__ import annotations

import re
from typing import Iterable

from enoch.backlog import normalize_priority
from enoch.app.models import ForgeMaintenanceRequest


def parse_chat_command(text: str) -> tuple[str, str]:
    first, _separator, rest = text.strip().partition(" ")
    if not first.startswith("/"):
        return "", text.strip()
    command = first.split("@", 1)[0].lower()
    return command, rest.strip()


def task_cancel_id(argument: str) -> int | None:
    return _task_action_id(argument, "cancel")


def task_retry_id(argument: str) -> int | None:
    return _task_action_id(argument, "retry")


def task_resume_target(argument: str) -> int | str | None:
    parts = argument.split()
    if len(parts) != 2 or parts[0].lower() != "resume":
        return None
    if parts[1].lower() == "all":
        return "all"
    return _positive_id(parts[1])


def unquote_schedule_text(text: str) -> str:
    stripped = text.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1].strip()
    return stripped


def backlog_priority_and_request(argument: str) -> tuple[str, str]:
    first, _separator, rest = argument.partition(" ")
    lowered = first.lower()
    if lowered in {"p0", "p1", "p2"}:
        return normalize_priority(lowered), rest.strip()
    if lowered.startswith("p") and lowered[:2] not in {"p0", "p1", "p2"} and lowered[1:].isdigit():
        raise ValueError("Backlog priority must be p0, p1, or p2.")
    return "p1", argument.strip()


def backlog_item_id(argument: str) -> int | None:
    value = argument.strip().split(maxsplit=1)[0] if argument.strip() else ""
    return _positive_id(value)


def cron_job_id(argument: str) -> int | None:
    value = argument.strip().split(maxsplit=1)[0] if argument.strip() else ""
    return _positive_id(value)


def backlog_priority_update(argument: str) -> tuple[int | None, str | None]:
    parts = argument.split()
    if len(parts) != 2:
        return None, None
    return backlog_item_id(parts[0]), parts[1].lower()


def forge_maintenance_request(text: str) -> ForgeMaintenanceRequest | None:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return None
    lowered = normalized.lower()
    numbers = pr_numbers(normalized)
    if not numbers:
        return None

    dedup_words = ("dedup", "duplicate", "duplicates", "重复", "重复的")
    close_words = ("close", "关闭", "关掉")
    if any(word in lowered for word in dedup_words):
        keep_number = keep_pr_number(normalized) or numbers[0]
    elif any(word in lowered for word in close_words):
        keep_number = keep_pr_number(normalized)
    else:
        return None
    close_numbers = tuple(number for number in numbers if number != keep_number)
    return ForgeMaintenanceRequest(
        close_numbers=unique_numbers(close_numbers),
        keep_number=keep_number,
    )


def pr_numbers(text: str) -> tuple[int, ...]:
    return unique_numbers(int(match) for match in re.findall(r"#(\d+)", text))


def keep_pr_number(text: str) -> int | None:
    patterns = [
        r"(?:keep|retain)\s+(?:pr\s*)?#(\d+)",
        r"(?:保留|留下)\s*#(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def unique_numbers(numbers: Iterable[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    unique: list[int] = []
    for number in numbers:
        if number <= 0 or number in seen:
            continue
        seen.add(number)
        unique.append(number)
    return tuple(unique)


def existing_branch_publish_request(text: str) -> str | None:
    normalized = " ".join(text.strip().split())
    if not normalized:
        return None
    lowered = normalized.lower()
    if "publish" not in lowered or "branch" not in lowered or "pr" not in lowered:
        return None
    patterns = [
        r"existing local branch `([^`]+)`",
        r"existing branch `([^`]+)`",
        r"branch `([^`]+)`",
        r"existing local branch ([^\s:]+)",
        r"existing branch ([^\s:]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            branch = match.group(1).strip().strip("`")
            return branch if looks_like_branch_name(branch) else None
    return None


def looks_like_branch_name(value: str) -> bool:
    if not value or value.startswith("-") or ".." in value or value.endswith(".lock"):
        return False
    return bool(re.match(r"^[A-Za-z0-9._/-]+$", value))


def _task_action_id(argument: str, action: str) -> int | None:
    parts = argument.split()
    if len(parts) != 2 or parts[0].lower() != action:
        return None
    return _positive_id(parts[1])


def _positive_id(value: str) -> int | None:
    try:
        item_id = int(value.lstrip("#"))
    except ValueError:
        return None
    return item_id if item_id > 0 else None
