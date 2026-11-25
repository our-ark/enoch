"""Opt-in task board: one chat message that is edited as task status changes.

This module is provider-agnostic. It renders the board text from the same task
queue snapshot that backs ``/queue``, plus one-line summaries of the open
reviews Enoch watches and of the pending backlog, and persists which chat
message is the board. Sending, editing, and pinning stay in the application
shell so they go through the durable notification journal and the daemon
effect fence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from enoch.app.pending_merges import (
    BOARD_BLOCKED,
    BOARD_CI,
    BOARD_FAILED,
    BOARD_READY,
    BoardReview,
)
from enoch.backlog import BacklogItem, promotion_order
from enoch.paths import private_state_path
from enoch.providers.contracts import (
    ConversationId,
    MessageId,
    normalize_conversation_id,
    normalize_message_id,
)
from enoch.state import atomic_write, load_json_object
from enoch.tasks.queue import TaskJob, TaskQueueStatus


SCHEMA_VERSION = 1
TASK_BOARD_STATE_FILE = "task_board.json"
# Conservative across chat providers; Telegram's hard limit is 4096 characters.
TASK_BOARD_MAX_CHARS = 3000
REQUEST_LIMIT = 80
# The backlog preview is a single line: a few items with short labels.
BACKLOG_PREVIEW_ITEMS = 2
BACKLOG_LABEL_LIMIT = 36
BACKLOG_SHORT_LABEL_LIMIT = 18
# The open-review line is a single line: a few reviews, then "+N more".
OPEN_REVIEWS_MAX_ITEMS = 5
OPEN_REVIEWS_PREVIEW_ITEMS = 4
OPEN_REVIEWS_SHORT_ITEMS = 2
# Marker per cached watcher status, and the order groups appear in: problems
# first, then ready reviews, then reviews still waiting for CI.
_OPEN_REVIEW_MARKERS = {
    BOARD_FAILED: "❌ CI failed",
    BOARD_BLOCKED: "⚠️ blocked",
    BOARD_READY: "✅ ready",
    BOARD_CI: "⏳ CI",
}
_OPEN_REVIEW_ORDER = {BOARD_FAILED: 0, BOARD_BLOCKED: 0, BOARD_READY: 1, BOARD_CI: 2}
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])")


@dataclass(frozen=True)
class TaskBoardState:
    enabled: bool = False
    chat_id: ConversationId | None = None
    message_id: MessageId | None = None
    last_digest: str = ""

    @property
    def active(self) -> bool:
        """True when the board should be kept up to date."""

        return self.enabled and self.chat_id is not None and self.message_id is not None


@dataclass(frozen=True)
class RenderedTaskBoard:
    text: str
    digest: str


def task_board_path(root: Path | None = None) -> Path:
    return private_state_path(TASK_BOARD_STATE_FILE, root)


def load_task_board_state(root: Path | None = None) -> TaskBoardState:
    data = load_json_object(task_board_path(root))
    return TaskBoardState(
        enabled=data.get("enabled") is True,
        chat_id=normalize_conversation_id(data.get("chat_id")),
        message_id=normalize_message_id(data.get("message_id")),
        last_digest=str(data.get("last_digest") or ""),
    )


def save_task_board_state(state: TaskBoardState, root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "enabled": state.enabled,
        "chat_id": state.chat_id,
        "message_id": state.message_id,
        "last_digest": state.last_digest,
    }
    atomic_write(
        task_board_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def render_task_board(
    status: TaskQueueStatus,
    *,
    updated_at: datetime,
    title: str = "Task board",
    queue_command: str = "/queue",
    max_chars: int = TASK_BOARD_MAX_CHARS,
    merging: tuple[str, ...] = (),
    open_reviews: Iterable[BoardReview] = (),
    backlog: Iterable[BacklogItem] = (),
) -> RenderedTaskBoard:
    """Render the board text and a digest of everything except the footer.

    The digest ignores the ``Updated HH:MM`` footer so the caller edits the
    message only when task content changes, not every minute. ``merging``
    lists reviews waiting to be merged automatically; its section appears only
    when it is non-empty. ``open_reviews`` holds the other open reviews Enoch
    watches, with their cached status; when there are any, one ``Open PRs:``
    line lists them. ``backlog`` holds the pending backlog items; when there
    are any, one ``Up next:`` line previews them in promotion order.
    """

    footer = f"Updated {_local_clock(updated_at)}"
    body = _render_body(
        status,
        title=title,
        queue_command=queue_command,
        max_chars=max_chars - len(footer) - 2,
        merging=merging,
        open_reviews=_open_review_order(open_reviews),
        backlog=promotion_order(backlog),
    )
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return RenderedTaskBoard(text=f"{body}\n\n{footer}", digest=digest)


def _render_body(
    status: TaskQueueStatus,
    *,
    title: str,
    queue_command: str,
    max_chars: int,
    merging: tuple[str, ...] = (),
    open_reviews: tuple[BoardReview, ...] = (),
    backlog: tuple[BacklogItem, ...] = (),
) -> str:
    sections = [
        _Section(
            "Running",
            [_running_item(status.running)] if status.running is not None else [],
        ),
        _Section("Queued", [_task_item(job) for job in status.pending]),
        _Section("Paused", [_task_item(job) for job in status.paused]),
    ]
    if merging:
        sections.append(
            _Section("Merging", [f"- {_short(item, REQUEST_LIMIT)}" for item in merging])
        )
    backlog_forms = _backlog_line_forms(backlog) or [""]
    review_forms = _open_review_line_forms(open_reviews) or [""]
    # The one-line summaries give way before any task item: first the backlog
    # preview shrinks to a bare count, then the open-review line shrinks to a
    # count and finally disappears.
    candidates = [(review_forms[0], line) for line in backlog_forms]
    candidates += [(line, backlog_forms[-1]) for line in review_forms[1:]]
    if review_forms[0]:
        candidates.append(("", backlog_forms[-1]))
    for review_line, backlog_line in candidates:
        text = _join(title, sections, queue_command, review_line, backlog_line)
        if len(text) <= max_chars:
            break
    # Drop items from the longest section until the board fits, noting how
    # many were hidden so the reader knows to use /queue for the full list.
    while len(text) > max_chars:
        candidate = max(sections, key=lambda section: len(section.shown))
        if len(candidate.shown) <= 1:
            break
        candidate.shown.pop()
        candidate.hidden += 1
        text = _join(title, sections, queue_command, review_line, backlog_line)
    if len(text) > max_chars:
        text = text[: max(0, max_chars - 1)].rstrip() + "…"
    return text


@dataclass
class _Section:
    heading: str
    shown: list[str]
    hidden: int = 0


def _join(
    title: str,
    sections: list[_Section],
    queue_command: str,
    review_line: str = "",
    backlog_line: str = "",
) -> str:
    lines = [title]
    for section in sections:
        lines.extend(["", f"{section.heading}:"])
        if not section.shown:
            lines.append("- none")
            continue
        lines.extend(section.shown)
        if section.hidden:
            lines.append(f"- … {section.hidden} more (see {queue_command})")
    summaries = [line for line in (review_line, backlog_line) if line]
    if summaries:
        lines.extend(["", *summaries])
    return "\n".join(lines)


def _open_review_order(reviews: Iterable[BoardReview]) -> tuple[BoardReview, ...]:
    """Known statuses only, one per number: problems, then ready, then CI."""

    unique = {review.number: review for review in reviews if review.status in _OPEN_REVIEW_MARKERS}
    return tuple(
        sorted(
            unique.values(),
            key=lambda review: (_OPEN_REVIEW_ORDER[review.status], review.number),
        )
    )


def _open_review_line_forms(reviews: tuple[BoardReview, ...]) -> list[str]:
    """Return the ``Open PRs:`` line from fullest to shortest, or none if empty."""

    if not reviews:
        return []
    shown = (
        len(reviews)
        if len(reviews) <= OPEN_REVIEWS_MAX_ITEMS
        else OPEN_REVIEWS_PREVIEW_ITEMS
    )
    forms = [_open_review_line(reviews, shown)]
    if shown > OPEN_REVIEWS_SHORT_ITEMS:
        forms.append(_open_review_line(reviews, OPEN_REVIEWS_SHORT_ITEMS))
    forms.append(f"Open PRs: {len(reviews)}")
    return forms


def _open_review_line(reviews: tuple[BoardReview, ...], shown: int) -> str:
    parts = [
        f"#{review.number} {_OPEN_REVIEW_MARKERS[review.status]}" for review in reviews[:shown]
    ]
    hidden = len(reviews) - shown
    if hidden:
        parts.append(f"+{hidden} more")
    return "Open PRs: " + " · ".join(parts)


def _backlog_line_forms(items: tuple[BacklogItem, ...]) -> list[str]:
    """Return the ``Up next:`` line from fullest to shortest, or none if empty."""

    if not items:
        return []
    return [
        _backlog_line(items, label_limit=BACKLOG_LABEL_LIMIT),
        _backlog_line(items, label_limit=BACKLOG_SHORT_LABEL_LIMIT),
        _backlog_line(items, label_limit=0),
    ]


def _backlog_line(items: tuple[BacklogItem, ...], *, label_limit: int) -> str:
    head = f"Up next: {len(items)} in backlog"
    if label_limit <= 0:
        return head
    shown = items[:BACKLOG_PREVIEW_ITEMS]
    parts = [
        f"#{item.id} [{item.priority}] {_short(_first_sentence(item.text), label_limit)}"
        for item in shown
    ]
    hidden = len(items) - len(shown)
    if hidden:
        parts.append(f"+{hidden} more")
    return f"{head} ({', '.join(parts)})"


def _first_sentence(text: str) -> str:
    cleaned = " ".join(text.split())
    sentence = _SENTENCE_BREAK.split(cleaned, maxsplit=1)[0].rstrip(".。")
    return sentence or cleaned


def _running_item(job: TaskJob) -> str:
    item = _task_item(job)
    started = _parse_timestamp(job.started_at)
    if started is None:
        return item
    return f"{item} (started {_local_clock(started)})"


def _task_item(job: TaskJob) -> str:
    item = f"- #{job.id} {_short(job.text, REQUEST_LIMIT)}"
    if job.parent_task_id is not None:
        item += f" (retry of #{job.parent_task_id})"
    return item


def _short(text: str, limit: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _parse_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _local_clock(moment: datetime) -> str:
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%H:%M")
