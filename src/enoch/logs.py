from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from enoch.paths import artifact_path, artifact_read_paths
from enoch.providers.contracts import ConversationId


def conversation_log_dir(root: Path | None = None) -> Path:
    return artifact_path(Path("logs") / "conversations", root)


def system_log_dir(root: Path | None = None) -> Path:
    return artifact_path(Path("logs") / "system", root)


def daemon_log_dir(root: Path | None = None) -> Path:
    return artifact_path(Path("logs") / "daemon", root)


def conversation_log_dirs(root: Path | None = None) -> tuple[Path, ...]:
    return artifact_read_paths(Path("logs") / "conversations", root)


def system_log_dirs(root: Path | None = None) -> tuple[Path, ...]:
    return artifact_read_paths(Path("logs") / "system", root)


def daemon_log_dirs(root: Path | None = None) -> tuple[Path, ...]:
    return artifact_read_paths(Path("logs") / "daemon", root)


def conversation_log_path(root: Path | None = None, *, when: datetime | None = None) -> Path:
    return conversation_log_dir(root) / f"{_day(when)}.jsonl"


def system_log_path(root: Path | None = None, *, when: datetime | None = None) -> Path:
    return system_log_dir(root) / f"{_day(when)}.jsonl"


def log_conversation_turn(
    *,
    chat_id: ConversationId,
    message: str,
    reply: str,
    root: Path | None = None,
    channel: str = "chat",
    details: dict[str, Any] | None = None,
) -> Path:
    timestamp = _now()
    record = {
        "time": timestamp.isoformat(),
        "channel": channel,
        "chat_id": chat_id,
        "message": message,
        "reply": reply,
    }
    if details:
        record["details"] = details
    path = conversation_log_path(root, when=timestamp)
    _append_jsonl(path, record)
    return path


def log_system_event(
    event: str,
    *,
    root: Path | None = None,
    status: str = "ok",
    details: dict[str, Any] | None = None,
) -> Path:
    timestamp = _now()
    record = {
        "time": timestamp.isoformat(),
        "event": event,
        "status": status,
        "details": details or {},
    }
    path = system_log_path(root, when=timestamp)
    _append_jsonl(path, record)
    return path


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _day(when: datetime | None = None) -> str:
    timestamp = when or _now()
    return timestamp.date().isoformat()
