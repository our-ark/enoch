from __future__ import annotations

import re
from typing import Iterable


HELP_CALLBACK_PREFIX = "enoch:help"
HELP_NAVIGATION_MODES = frozenset({"overview", "section", "command"})
_HELP_TARGET_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_HELP_CALLBACK_RE = re.compile(
    rf"^{re.escape(HELP_CALLBACK_PREFIX)}:(?P<kind>[sc]):"
    r"(?P<target>[a-z][a-z0-9_-]{0,31})$"
)


def telegram_help_reply_markup(
    mode: str,
    entries: Iterable[tuple[str, str]],
) -> dict[str, list[list[dict[str, str]]]]:
    """Build a Telegram inline keyboard for one registry-backed help view."""

    normalized_mode = mode.strip().lower()
    if normalized_mode not in HELP_NAVIGATION_MODES:
        raise ValueError(f"Unsupported Telegram help navigation mode: {mode!r}")

    kind = "s" if normalized_mode == "overview" else "c"
    buttons = [
        {
            "text": _button_label(label),
            "callback_data": _callback_data(kind, target),
        }
        for label, target in entries
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    if normalized_mode != "overview":
        rows.append(
            [
                {
                    "text": "← All commands",
                    "callback_data": f"{HELP_CALLBACK_PREFIX}:h",
                }
            ]
        )
    return {"inline_keyboard": rows}


def telegram_help_callback_command(data: str) -> str | None:
    """Translate a validated Telegram help callback into internal command text."""

    normalized = data.strip().lower()
    if normalized == f"{HELP_CALLBACK_PREFIX}:h":
        return "/help"
    match = _HELP_CALLBACK_RE.fullmatch(normalized)
    if match is None:
        return None
    target = match.group("target")
    if match.group("kind") == "s":
        return f"/help section:{target}"
    return f"/help {target}"


def _button_label(value: str) -> str:
    label = str(value).strip()
    if not label:
        raise ValueError("Telegram help button labels cannot be empty.")
    return label


def _callback_data(kind: str, value: str) -> str:
    target = str(value).strip().lower()
    if _HELP_TARGET_RE.fullmatch(target) is None:
        raise ValueError(f"Invalid Telegram help target: {value!r}")
    return f"{HELP_CALLBACK_PREFIX}:{kind}:{target}"
