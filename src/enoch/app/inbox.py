from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from enoch.memory.paths import atomic_write, now as current_time
from enoch.paths import enoch_home
from enoch.providers.contracts import ChatEvent, Cursor
from enoch.state import StateCorruptionError, file_transaction, load_json_object


SCHEMA_VERSION = 1
MAX_EVENT_ATTEMPTS = 3


@dataclass(frozen=True)
class InboxReceipt:
    key: str
    status: str
    attempts: int
    cursor: Cursor | None
    reply: str = ""
    logged_input: str = ""
    reply_sent: bool = False
    error: str = ""

    @property
    def completed(self) -> bool:
        return self.status in {"completed", "acknowledged"}

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_EVENT_ATTEMPTS


def inbox_path(provider: str, root: Path | None = None) -> Path:
    safe_provider = "".join(
        character if character.isalnum() or character in "._-" else "-"
        for character in provider.strip().lower()
    ).strip("-.") or "chat"
    return enoch_home(root) / "channels" / safe_provider / "inbox.json"


def event_key(provider: str, event: ChatEvent) -> str:
    identity = {
        "provider": provider.strip().lower() or "chat",
        "cursor": event.cursor,
        "conversation_id": event.conversation_id,
        "message_id": event.message_id,
        "text": event.text,
        "replied_text": event.replied_text,
        "attachments": [
            {
                "kind": attachment.kind,
                "file_id": attachment.file_id,
                "filename": attachment.filename,
            }
            for attachment in event.attachments
        ],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def begin_event(
    provider: str,
    event: ChatEvent,
    root: Path | None = None,
) -> InboxReceipt:
    path = inbox_path(provider, root)
    key = event_key(provider, event)
    with file_transaction(path):
        data = _load_inbox(path)
        receipts = data["events"]
        existing = _receipt(key, receipts.get(key))
        if existing is not None and existing.completed:
            return existing
        attempts = (existing.attempts if existing is not None else 0) + 1
        receipt = InboxReceipt(
            key=key,
            status="processing",
            attempts=attempts,
            cursor=event.cursor,
        )
        receipts[key] = _receipt_to_json(receipt)
        _write_inbox(path, data)
        return receipt


def complete_event(
    provider: str,
    key: str,
    root: Path | None = None,
    *,
    reply: str,
    logged_input: str,
) -> InboxReceipt:
    return _update_receipt(
        provider,
        key,
        root,
        status="completed",
        reply=reply,
        logged_input=logged_input,
        error="",
    )


def mark_reply_sent(
    provider: str,
    key: str,
    root: Path | None = None,
) -> InboxReceipt:
    return _update_receipt(provider, key, root, reply_sent=True)


def acknowledge_event(
    provider: str,
    key: str,
    root: Path | None = None,
) -> InboxReceipt:
    return _update_receipt(provider, key, root, status="acknowledged")


def fail_event(
    provider: str,
    key: str,
    error: str,
    root: Path | None = None,
) -> InboxReceipt:
    return _update_receipt(
        provider,
        key,
        root,
        status="failed",
        error=" ".join(error.split())[:2000],
    )


def _update_receipt(
    provider: str,
    key: str,
    root: Path | None,
    **changes: Any,
) -> InboxReceipt:
    path = inbox_path(provider, root)
    with file_transaction(path):
        data = _load_inbox(path)
        existing = _receipt(key, data["events"].get(key))
        if existing is None:
            raise StateCorruptionError(path, f"missing inbox receipt {key}")
        values = {**existing.__dict__, **changes}
        receipt = InboxReceipt(**values)
        data["events"][key] = _receipt_to_json(receipt)
        _write_inbox(path, data)
        return receipt


def _load_inbox(path: Path) -> dict[str, Any]:
    data = load_json_object(
        path,
        default_factory=lambda: {"schema_version": SCHEMA_VERSION, "events": {}},
    )
    events = data.get("events")
    if not isinstance(events, dict):
        raise StateCorruptionError(path, "expected events to be an object")
    for key, raw in events.items():
        if not isinstance(key, str) or _receipt(key, raw) is None:
            raise StateCorruptionError(path, "found an invalid inbox receipt")
    return {"schema_version": SCHEMA_VERSION, "events": events}


def _write_inbox(path: Path, data: dict[str, Any]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": current_time(),
        "events": data["events"],
    }
    atomic_write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _receipt(key: str, raw: object) -> InboxReceipt | None:
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "")
    if status not in {"processing", "completed", "failed", "acknowledged"}:
        return None
    attempts = _positive_int(raw.get("attempts"))
    cursor = raw.get("cursor")
    if isinstance(cursor, bool) or not isinstance(cursor, (int, str, type(None))):
        cursor = None
    return InboxReceipt(
        key=key,
        status=status,
        attempts=attempts,
        cursor=cursor,
        reply=str(raw.get("reply") or ""),
        logged_input=str(raw.get("logged_input") or ""),
        reply_sent=bool(raw.get("reply_sent", False)),
        error=str(raw.get("error") or ""),
    )


def _receipt_to_json(receipt: InboxReceipt) -> dict[str, Any]:
    return {
        "status": receipt.status,
        "attempts": receipt.attempts,
        "cursor": receipt.cursor,
        "reply": receipt.reply,
        "logged_input": receipt.logged_input,
        "reply_sent": receipt.reply_sent,
        "error": receipt.error,
    }


def _positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)
