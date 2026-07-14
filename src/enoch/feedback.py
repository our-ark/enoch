from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Iterable

from enoch.logs import conversation_log_dir
from enoch.memory.paths import clean_text


@dataclass(frozen=True)
class FeedbackSignal:
    id: str
    kind: str
    message: str
    occurrences: int
    first_seen_at: str = ""
    last_seen_at: str = ""


_CORRECTION_PATTERNS = (
    re.compile(r"\b(?:no|not quite|actually|instead|rather than|remove|delete|fix)\b", re.IGNORECASE),
    re.compile(r"(?:不对|不是|不要|删掉|删除|改成|修复|应该是|其实)"),
)
_PREFERENCE_PATTERNS = (
    re.compile(r"\b(?:i prefer|i want|i would like|please keep|please avoid)\b", re.IGNORECASE),
    re.compile(r"(?:我希望|我想|我更喜欢|最好|尽量|不太希望)"),
)
_COMPLAINT_PATTERNS = (
    re.compile(r"\b(?:bug|broken|failed|doesn't work|does not work|missing|lost|too slow)\b", re.IGNORECASE),
    re.compile(r"(?:有问题|坏了|失败|不工作|没了|丢了|太慢|报错)"),
)


def extract_feedback_signals(
    root: Path | None = None,
    *,
    max_records: int = 500,
) -> tuple[FeedbackSignal, ...]:
    records = list(_conversation_records(root))[-max_records:]
    messages = [clean_text(str(record.get("message") or "")) for record in records]
    counts = Counter(_feedback_key(message) for message in messages if _eligible_message(message))
    grouped: dict[str, list[dict[str, object]]] = {}
    for record, message in zip(records, messages):
        if not _eligible_message(message):
            continue
        grouped.setdefault(_feedback_key(message), []).append(record)

    signals: list[FeedbackSignal] = []
    for key, matching_records in grouped.items():
        message = clean_text(str(matching_records[-1].get("message") or ""))
        kind = _feedback_kind(message, occurrences=counts[key])
        if not kind:
            continue
        timestamps = [str(record.get("time") or "") for record in matching_records]
        signals.append(
            FeedbackSignal(
                id=_signal_id(key),
                kind=kind,
                message=message,
                occurrences=counts[key],
                first_seen_at=timestamps[0] if timestamps else "",
                last_seen_at=timestamps[-1] if timestamps else "",
            )
        )
    return tuple(sorted(signals, key=lambda item: (item.last_seen_at, item.id), reverse=True))


def _conversation_records(root: Path | None) -> Iterable[dict[str, object]]:
    directory = conversation_log_dir(root)
    if not directory.exists():
        return ()
    records: list[dict[str, object]] = []
    for path in sorted(directory.glob("*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                records.append(raw)
    return records


def _eligible_message(message: str) -> bool:
    return bool(message) and not message.startswith("/") and len(message) >= 4


def _feedback_key(message: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff]+", " ", message.lower())
    return clean_text(normalized)


def _feedback_kind(message: str, *, occurrences: int) -> str:
    if any(pattern.search(message) for pattern in _COMPLAINT_PATTERNS):
        return "complaint"
    if any(pattern.search(message) for pattern in _CORRECTION_PATTERNS):
        return "correction"
    if any(pattern.search(message) for pattern in _PREFERENCE_PATTERNS):
        return "preference"
    if occurrences >= 2:
        return "repeated-request"
    return ""


def _signal_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
