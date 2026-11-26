"""Opt-in task board: one chat message that is edited as task status changes.

This module is provider-agnostic. It renders the board text from the same task
queue snapshot that backs ``/queue``, plus one-line summaries of the open
reviews Enoch watches and of the pending backlog and a one-line account quota
summary, and persists which chat message is the board. Sending, editing, and
pinning stay in the application shell so they go through the durable
notification journal and the daemon effect fence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
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
from enoch.quota import _label, _number, _timestamp, quota_provider_label
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
# The quota line shows a few windows, rounded so it changes (and the board is
# edited) only when a 10% bucket or a reset time changes. Below the exact
# threshold the real percent is shown, since that is when it matters.
QUOTA_BUCKET_PERCENT = 10
QUOTA_EXACT_BELOW_PERCENT = 20
QUOTA_PRIMARY_WINDOWS = 2
QUOTA_WINDOW_LABEL_LIMIT = 16
# HH:MM alone is ambiguous for a reset more than a day away.
QUOTA_RESET_HORIZON = timedelta(hours=24)
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
    quota_line: str = "",
    held_by: str = "",
) -> RenderedTaskBoard:
    """Render the board text and a digest of everything except the footer.

    The digest ignores the ``Updated HH:MM`` footer so the caller edits the
    message only when task content changes, not every minute. ``merging``
    lists reviews waiting to be merged automatically; its section appears only
    when it is non-empty. ``open_reviews`` holds the other open reviews Enoch
    watches, with their cached status; when there are any, one ``Open PRs:``
    line lists them. ``backlog`` holds the pending backlog items; when there
    are any, one ``Up next:`` line previews them in promotion order.
    ``quota_line`` (see :func:`render_quota_line`) is shown last, just above
    the footer, when it is non-empty. ``held_by`` names the review (``#91``)
    whose pending merge holds queued tasks; the Queued heading then says so.
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
        quota_line=quota_line,
        held_by=held_by,
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
    quota_line: str = "",
    held_by: str = "",
) -> str:
    queued = (
        f"Queued (waiting for {held_by} to land)"
        if held_by and status.pending
        else "Queued"
    )
    sections = [
        _Section(
            "Running",
            [_running_item(status.running)] if status.running is not None else [],
        ),
        _Section(queued, [_task_item(job) for job in status.pending]),
        _Section("Paused", [_task_item(job) for job in status.paused]),
    ]
    if merging:
        sections.append(
            _Section("Merging", [f"- {_short(item, REQUEST_LIMIT)}" for item in merging])
        )
    backlog_forms = _backlog_line_forms(backlog) or [""]
    review_forms = _open_review_line_forms(open_reviews) or [""]
    # The one-line summaries give way before any task item: first the backlog
    # preview shrinks to a bare count, then the quota line is dropped, then
    # the open-review line shrinks to a count and finally disappears.
    candidates = [(review_forms[0], line, quota_line) for line in backlog_forms]
    if quota_line:
        candidates.append((review_forms[0], backlog_forms[-1], ""))
    candidates += [(line, backlog_forms[-1], "") for line in review_forms[1:]]
    if review_forms[0]:
        candidates.append(("", backlog_forms[-1], ""))
    for review_line, backlog_line, quota_line in candidates:
        text = _join(title, sections, queue_command, review_line, backlog_line, quota_line)
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
        text = _join(title, sections, queue_command, review_line, backlog_line, quota_line)
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
    quota_line: str = "",
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
    # The open-review, backlog and quota lines share one trailing block above
    # the footer.
    trailing = [line for line in (review_line, backlog_line, quota_line) if line]
    if trailing:
        lines.extend(["", *trailing])
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


def render_quota_line(
    provider: str,
    snapshot: Mapping | None,
    *,
    now: datetime | None = None,
) -> str:
    """Return one compact quota line for the board, or "" to omit it.

    Example: ``Quota (Claude): 5h 90% · 7d 60% left · 5h resets 20:30``.
    Remaining percentages are rounded to the nearest 10% (exact below 20%),
    so the line changes only when a bucket or a reset time changes. Errors,
    missing data, and unexpected shapes all yield "" rather than raising.
    """

    try:
        return _quota_line(provider, snapshot, now or datetime.now(timezone.utc))
    except Exception:
        return ""


@dataclass(frozen=True)
class _QuotaWindow:
    label: str
    remaining: int
    resets_at: datetime | None


def _quota_line(provider: str, snapshot: Mapping | None, now: datetime) -> str:
    if not isinstance(snapshot, Mapping) or snapshot.get("error"):
        return ""
    raw_windows = snapshot.get("windows")
    if not isinstance(raw_windows, (list, tuple)):
        return ""
    windows: list[_QuotaWindow] = []
    labels: list[str] = []
    for window in raw_windows:
        if not isinstance(window, Mapping):
            continue
        used = _number(window.get("used_percent"))
        if used is None or used < 0:
            continue  # Unknown usage is never shown as a number.
        resets_at = _timestamp(window.get("resets_at"))
        if resets_at is not None and resets_at <= now:
            continue  # The provider has not reported the new window yet.
        label = _label(window.get("label"), limit=QUOTA_WINDOW_LABEL_LIMIT)
        if not label:
            continue
        labels.append(label)
        windows.append(
            _QuotaWindow(label, _display_remaining(100 - used), resets_at)
        )
    if not windows:
        return ""
    prefix = _common_label_prefix(labels)
    windows = [
        _QuotaWindow(window.label[len(prefix):], window.remaining, window.resets_at)
        for window in windows
    ]
    # Keep the line short: the provider's primary windows, plus any other
    # window (for example a per-model one) that is nearly exhausted.
    shown = [
        window
        for index, window in enumerate(windows)
        if index < QUOTA_PRIMARY_WINDOWS or window.remaining < QUOTA_EXACT_BELOW_PERCENT
    ]
    parts = " · ".join(f"{window.label} {window.remaining}%" for window in shown)
    line = f"Quota ({_label(quota_provider_label(provider), limit=24)}): {parts} left"
    soonest = [
        window
        for window in shown
        if window.resets_at is not None and window.resets_at - now <= QUOTA_RESET_HORIZON
    ]
    if soonest:
        # The window closest to running out; ties go to the earlier reset.
        urgent = min(soonest, key=lambda window: (window.remaining, window.resets_at))
        line += f" · {urgent.label} resets {_local_clock(urgent.resets_at)}"
    return line


def _display_remaining(remaining: float) -> int:
    remaining = max(0.0, min(100.0, remaining))
    if remaining < QUOTA_EXACT_BELOW_PERCENT:
        # Round down so the board never overstates what is left.
        return math.floor(remaining)
    return int(math.floor(remaining / QUOTA_BUCKET_PERCENT + 0.5) * QUOTA_BUCKET_PERCENT)


def _common_label_prefix(labels: list[str]) -> str:
    """Return a shared ``name / `` prefix (e.g. Codex's bucket) to drop."""

    if len(labels) < 2:
        return ""
    first, _, _ = labels[0].partition(" / ")
    prefix = f"{first} / "
    if all(label.startswith(prefix) and len(label) > len(prefix) for label in labels):
        return prefix
    return ""


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
