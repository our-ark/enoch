from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from enoch.logs import conversation_log_dirs
from enoch.providers.contracts import ConversationId


def recent_conversation_turns(
    *,
    root: Path,
    chat_id: ConversationId | None,
    limit: int = 40,
) -> list[dict[str, str]]:
    if chat_id is None:
        return []
    records: list[dict[str, str]] = []
    for directory in conversation_log_dirs(root):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.jsonl"))[-4:]:
            records.extend(_turns_from_log(path, chat_id))
    return list(reversed(records[-limit:]))


def _turns_from_log(path: Path, chat_id: ConversationId) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if str(record.get("chat_id") or "") != str(chat_id):
            continue
        message = str(record.get("message") or "").strip()
        reply = str(record.get("reply") or "").strip()
        if not message and not reply:
            continue
        turns.append(
            {
                "time": str(record.get("time") or ""),
                "message": message[:4000],
                "reply": reply[:16000],
            }
        )
    return turns


def query_token(path: str) -> str:
    parsed = urlparse(path)
    values = parse_qs(parsed.query).get("token", [])
    return values[0].strip() if values else ""


def json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, indent=2).encode("utf-8")
