from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any

from enoch.command_surface import action_mode, action_mode_label
from enoch.identity import Identity
from enoch.paths import enoch_home
from enoch.update_tools import current_head, main_pull_summary


def startup_message(
    identity: Identity,
    root: Path | None = None,
    previous_shutdown_warning: str = "",
) -> str:
    lines = [
        f"{identity.name} restarted and is listening on Telegram.",
        "Startup notification: daemon is running.",
        f"Action mode: {action_mode_label(action_mode(root))}.",
        main_pull_summary(root),
    ]
    if previous_shutdown_warning:
        lines.append(previous_shutdown_warning)
    lines.append("Use /help to see available commands.")
    return "\n".join(lines)


def shutdown_message(identity: Identity, root: Path | None = None, reason: str = "shutdown") -> str:
    return "\n".join(
        [
            f"{identity.name} is shutting down.",
            f"Reason: {reason}.",
            f"Action mode: {action_mode_label(action_mode(root))}.",
            "Telegram bridge is closing.",
        ]
    )


def lifecycle_state_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "telegram_lifecycle.json"


def telegram_offset_path(root: Path | None = None) -> Path:
    return enoch_home(root) / "telegram_offset.json"


def next_update_offset(update: dict[str, Any]) -> int | None:
    try:
        return int(update["update_id"]) + 1
    except (KeyError, TypeError, ValueError):
        return None


def load_telegram_offset(root: Path | None = None) -> int | None:
    path = telegram_offset_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        offset = int(data.get("offset"))
    except (AttributeError, TypeError, ValueError):
        return None
    return offset if offset >= 0 else None


def save_telegram_offset(offset: int, root: Path | None = None) -> None:
    path = telegram_offset_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}, indent=2), encoding="utf-8")


def begin_lifecycle_run(root: Path | None = None) -> str:
    previous = load_lifecycle_state(root)
    warning = previous_shutdown_warning(previous)
    save_lifecycle_state(
        {
            "status": "running",
            "started_at": int(time.time()),
            "started_head": _current_head_or_empty(root),
            "pid": os.getpid(),
        },
        root,
    )
    return warning


def record_lifecycle_shutdown(
    root: Path | None,
    reason: str,
    *,
    shutdown_notification_sent: bool,
) -> None:
    save_lifecycle_state(
        {
            "status": "stopped",
            "stopped_at": int(time.time()),
            "reason": reason,
            "shutdown_notification_sent": shutdown_notification_sent,
        },
        root,
    )


def load_lifecycle_state(root: Path | None = None) -> dict[str, Any]:
    path = lifecycle_state_path(root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_lifecycle_state(data: dict[str, Any], root: Path | None = None) -> None:
    path = lifecycle_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _current_head_or_empty(root: Path | None = None) -> str:
    try:
        return current_head(root)
    except Exception:
        return ""


def previous_shutdown_warning(previous: dict[str, Any]) -> str:
    status = str(previous.get("status") or "")
    if status == "running":
        return "Previous shutdown: unexpected; Enoch could not send the normal shutdown message."
    if status == "stopped" and not bool(previous.get("shutdown_notification_sent")):
        return "Previous shutdown: Enoch stopped, but could not send the normal shutdown message."
    return ""
