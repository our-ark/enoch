"""Hands-off landing for one review a human explicitly asked to merge.

A merge request is the only authorization. When the review is not ready yet
(behind its base, CI running, blocked, or the forge is still computing its
state), Enoch records a pending merge here and a throttled watcher in the
daemon loop lands it once it is ready, with no further human action.

The authorization is pinned to the review head the human saw. The only other
heads Enoch accepts are the ones it created itself: updates of the branch from
its base, and pushes by its own CI fix tasks for this merge (they are covered
by the original request and listed in the final message). Conflicts are never
resolved, and waiting stops after a fixed time.

When CI fails on a head that already contains the base tip, the watcher
re-runs infrastructure failures once and otherwise queues a fix task at the
front of the task queue (at most ``MAX_MERGE_FIX_ATTEMPTS`` per merge). The
timeout is paused while a fix task is queued or running, up to an overall cap.

The same controller watches CI on reviews that Enoch's tasks publish but nobody
asked to merge: it reports green CI once, and repairs red CI the same way, but
never merges them. Once that CI watch settles, a review on Enoch's own
(authoritative) repository stays tracked until it is merged or closed: the
watcher keeps a cached board status for it (ready, CI, failed, or blocked) that
the task board reads without calling the forge.

This module is provider-agnostic: it uses the review provider's optional
``forge.check-status``, ``forge.update-branch`` and ``forge.ci-repair``
operations. Forges without check-status keep today's behavior, where reviews
that are not ready are refused; forges without ci-repair stop on red CI.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

from enoch.app.ci_repair import (
    TERMINAL_TASK_STATES,
    CiRepair,
    FixAttempt,
    FixCommit,
    ci_fix_task_brief,
    ci_repair_from_json,
    ci_repair_to_json,
    classify_check_failures,
    format_fix_commits,
    load_ci_fix_result,
)
from enoch.app.epoch import StaleDaemonEpoch
from enoch.paths import private_state_path
from enoch.providers.contracts import (
    ConversationId,
    ForgeProviderError,
    ReviewCheckFailure,
    ReviewCheckReport,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewProviderError,
    ReviewRecord,
    ReviewRevisionComparison,
    normalize_conversation_id,
)
from enoch.providers.authorization import CapabilityAuthorizationError
from enoch.state import atomic_write, load_json_object


SCHEMA_VERSION = 1
PENDING_MERGES_STATE_FILE = "pending_merges.json"
CI_WATCHES_STATE_FILE = "ci_watches.json"
PENDING_MERGE_CHECK_INTERVAL_SECONDS = 60.0
PENDING_MERGE_TIMEOUT = timedelta(hours=2)
# Overall cap for one pending merge, including time paused for fix tasks.
PENDING_MERGE_MAX_WAIT = timedelta(hours=6)
CI_WATCH_TIMEOUT = timedelta(hours=6)
# After its CI watch settles, a review is tracked for the board until it is
# merged or closed, re-checked at most this often, and dropped after a cap.
TRACKED_REVIEW_CHECK_INTERVAL_SECONDS = 180.0
TRACKED_REVIEW_TIMEOUT = timedelta(days=14)
# Open reviews listed once to learn which watched reviews are on Enoch's own
# (authoritative) repository; only those appear on the board.
OPEN_REVIEW_LIST_LIMIT = 50
# Cached board status of a watched review; the task board maps it to a marker.
BOARD_READY = "ready"  # all checks green and the head contains the base tip
BOARD_CI = "ci"  # CI running, or behind / updating from its base
BOARD_FAILED = "failed"  # CI failed on a head that contains the base tip
BOARD_BLOCKED = "blocked"  # conflicts, draft, or blocked for a non-CI reason
# Automatic fix tasks per pending merge, counted across head changes.
MAX_MERGE_FIX_ATTEMPTS = 2
# A review nobody asked to merge gets one re-run or one fix task.
MAX_WATCH_FIX_ATTEMPTS = 1
CHECK_STATUS_CAPABILITY = "forge.check-status"
UPDATE_BRANCH_CAPABILITY = "forge.update-branch"
CI_REPAIR_CAPABILITY = "forge.ci-repair"
READY_MERGE_STATES = frozenset({"clean", "unstable", "has-hooks"})
_FORGE_ERRORS = (ReviewProviderError, ForgeProviderError, CapabilityAuthorizationError)
_MERGE_FIX_GOAL = (
    "A merge of it is pending and lands automatically once CI is green, "
    "including your fix commits."
)
_WATCH_FIX_GOAL = "Nobody has asked to merge it yet; just make its CI green."


@dataclass(frozen=True)
class PendingMerge:
    number: int
    url: str
    repository: str
    review_id: str
    authorized_head_sha: str
    allowed_head_shas: tuple[str, ...]
    requested_at: str
    chat_id: ConversationId
    base_branch: str = ""
    waiting_for: str = ""
    blocked_notified: bool = False
    # Head of an update this daemon requested but could not verify yet.
    unverified_update_from: str = ""
    repair: CiRepair = CiRepair()

    @property
    def key(self) -> str:
        return _review_key(self.repository, self.number, self.url or self.review_id)

    @property
    def label(self) -> str:
        return f"#{self.number}" if self.number else (self.url or self.review_id)

    @property
    def started_at(self) -> str:
        return self.requested_at

    def identity(self) -> ReviewIdentity:
        return ReviewIdentity(id=self.review_id, url=self.url)

    def allow(self, head: str) -> "PendingMerge":
        head = head.strip()
        if not head or head in self.allowed_head_shas:
            return self
        return replace(self, allowed_head_shas=(*self.allowed_head_shas, head))


@dataclass(frozen=True)
class WatchedReview:
    """A review an Enoch task published; its CI is watched until it settles."""

    number: int
    url: str
    repository: str
    review_id: str
    chat_id: ConversationId
    task_id: int
    started_at: str
    waiting_for: str = ""
    repair: CiRepair = CiRepair()
    # True once the CI watch has reported (or timed out); the review is then
    # only tracked for the board, with no more notices or repairs.
    settled: bool = False
    # Cached BOARD_* status from the last watcher check; "" before the first.
    board_status: str = ""
    # Whether the review is on Enoch's authoritative repository; None until
    # the watcher has checked.
    authoritative: bool | None = None

    @property
    def key(self) -> str:
        return _review_key(self.repository, self.number, self.url or self.review_id)

    @property
    def label(self) -> str:
        return f"#{self.number}" if self.number else (self.url or self.review_id)

    def identity(self) -> ReviewIdentity:
        return ReviewIdentity(id=self.review_id, url=self.url)


@dataclass(frozen=True)
class BoardReview:
    """One open review for the task board's ``Open PRs`` line."""

    number: int
    status: str


@dataclass(frozen=True)
class MergePlan:
    """What ``/pr merge`` should do for one inspected review.

    ``land``: land now, pinned to ``expected_revision`` when it is set.
    ``reply``: answer with ``message`` (pending, or refused) without landing.
    """

    action: str
    message: str = ""
    expected_revision: str = ""


@dataclass
class _Outcome:
    entry: Any
    notice: str = ""
    notice_suffix: str = ""
    events: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)


@dataclass(frozen=True)
class _RepairStep:
    """What one red-CI repair decision did: ``rerun``, ``fix`` or ``exhausted``."""

    action: str
    repair: CiRepair
    task_id: int = 0
    reason: str = ""


def pending_merges_path(root: Path | None = None) -> Path:
    return private_state_path(PENDING_MERGES_STATE_FILE, root)


def ci_watches_path(root: Path | None = None) -> Path:
    return private_state_path(CI_WATCHES_STATE_FILE, root)


def load_ci_watches(root: Path | None = None) -> tuple[WatchedReview, ...]:
    data = load_json_object(ci_watches_path(root))
    raw = data.get("watches")
    entries = []
    for item in raw if isinstance(raw, list) else []:
        entry = _watch_from_json(item)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def save_ci_watches(entries: tuple[WatchedReview, ...], root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "watches": [_watch_to_json(entry) for entry in entries],
    }
    atomic_write(
        ci_watches_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def load_pending_merges(root: Path | None = None) -> tuple[PendingMerge, ...]:
    data = load_json_object(pending_merges_path(root))
    raw = data.get("merges")
    entries = []
    for item in raw if isinstance(raw, list) else []:
        entry = _entry_from_json(item)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def save_pending_merges(entries: tuple[PendingMerge, ...], root: Path | None = None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "merges": [_entry_to_json(entry) for entry in entries],
    }
    atomic_write(
        pending_merges_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


class PendingMergeController:
    """Records merge requests that are not ready and lands them when they are."""

    def __init__(
        self,
        root: Path,
        *,
        review: Callable[[], Any],
        authorize: Callable[[str, tuple[str, ...]], None],
        run_authorized: Callable[..., Any],
        notify: Callable[[ConversationId, str, str], None],
        record_event: Callable[..., None],
        after_land: Callable[[], None] = lambda: None,
        queue_fix: Callable[[ConversationId, str, str], int] | None = None,
        task_status: Callable[[int], str | None] = lambda _task_id: None,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        interval_seconds: float = PENDING_MERGE_CHECK_INTERVAL_SECONDS,
        timeout: timedelta = PENDING_MERGE_TIMEOUT,
        max_wait: timedelta = PENDING_MERGE_MAX_WAIT,
        watch_timeout: timedelta = CI_WATCH_TIMEOUT,
        tracked_interval_seconds: float = TRACKED_REVIEW_CHECK_INTERVAL_SECONDS,
        tracked_timeout: timedelta = TRACKED_REVIEW_TIMEOUT,
    ) -> None:
        self.root = root
        self._review = review
        self._authorize = authorize
        self._run_authorized = run_authorized
        self._notify = notify
        self._record_event = record_event
        self._after_land = after_land
        # Hook for queuing one CI fix task at the front of the task queue:
        # (chat_id, request, idempotency_key) -> task id.
        self._queue_fix = queue_fix
        self._task_status = task_status
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self.interval_seconds = interval_seconds
        self.timeout = timeout
        self.max_wait = max_wait
        self.watch_timeout = watch_timeout
        self.tracked_interval_seconds = tracked_interval_seconds
        self.tracked_timeout = tracked_timeout
        # Monotonic time of the last check of each tracked (settled) review.
        self._tracked_checks: dict[str, float] = {}
        self._lock = threading.RLock()
        self._last_check: float | None = None
        self._last_error = ""

    # Capabilities -----------------------------------------------------

    def supported(self) -> bool:
        return self._supports(CHECK_STATUS_CAPABILITY, "inspect_review_checks")

    def _can_update(self) -> bool:
        return self._supports(UPDATE_BRANCH_CAPABILITY, "update_review_branch")

    def _can_repair(self) -> bool:
        return self._queue_fix is not None and all(
            self._supports(CI_REPAIR_CAPABILITY, method)
            for method in (
                "inspect_review_check_failures",
                "rerun_failed_review_checks",
                "compare_review_revisions",
            )
        )

    def _supports(self, capability: str, method: str) -> bool:
        review = self._review()
        declared = getattr(review, "capabilities", None)
        if callable(declared):
            declared = declared()
        supports = getattr(declared, "supports", None)
        return (
            callable(supports)
            and bool(supports(capability))
            and callable(getattr(review, method, None))
        )

    # Merge requests ---------------------------------------------------

    def plan(
        self,
        chat_id: ConversationId,
        current: ReviewRecord,
    ) -> MergePlan:
        """Decide how to handle a human's merge request for ``current``.

        Closed, draft and conflicting reviews, and every request on a forge
        without check-status, use the existing landing path, so the forge's
        own refusal messages still apply.
        """

        if not self.supported() or _terminal_refusal(current):
            return MergePlan("land")
        head = _head(current)
        number = _number(current)
        label = f"PR #{number}" if number else f"Review {current.identity.id}"
        base = _base_branch(current) or "its base branch"
        merge_state = _signal(current, "merge-state")
        checks = self._checks(current.identity)
        checks_current = checks.revision == head
        if (
            merge_state in READY_MERGE_STATES
            and _signal(current, "mergeable") == "mergeable"
            and checks_current
            and checks.green
        ):
            self.discard(current)
            return MergePlan("land", expected_revision=head)
        if checks_current and checks.failed and merge_state != "behind":
            if not self._can_repair():
                self.discard(current)
                return MergePlan("reply", _ci_failed_message(number, checks, current))
            return self._plan_repair(chat_id, current, checks, label)
        if merge_state == "behind" and not self._can_update():
            return MergePlan(
                "reply",
                f"{label} is behind {base} and must be updated before it can merge. "
                "This forge cannot update review branches automatically.",
            )
        entry = self._record_request(chat_id, current)
        if merge_state == "behind":
            entry = self._update_branch(entry, head)
        reasons = _waiting_reasons(merge_state, checks if checks_current else None, base)
        entry = replace(entry, waiting_for=_board_label(merge_state, reasons))
        self._store(entry, replace_requested_at=entry.requested_at)
        return MergePlan(
            "reply",
            f"{label} isn't ready yet ({' / '.join(reasons)}). "
            "I'll merge it automatically when it is.",
        )

    def _plan_repair(
        self,
        chat_id: ConversationId,
        current: ReviewRecord,
        checks: ReviewCheckReport,
        label: str,
    ) -> MergePlan:
        """Record the request for a red review and start repairing it right away."""

        entry = self._record_request(chat_id, current)
        number = entry.number
        fixing = entry.repair.active_attempt
        if fixing is not None:
            self._store(
                replace(entry, waiting_for=_fixing_label(fixing)),
                replace_requested_at=entry.requested_at,
            )
            return MergePlan(
                "reply",
                f"{label} CI failed ({_failed_names(checks)}); task #{fixing.task_id} is "
                "fixing it. I'll merge it automatically when it is green.",
            )
        outcome = self._repair_merge(entry, number, _head(current), checks, current)
        for name, status, details in outcome.events:
            self._record_event(name, status=status, details=details)
        if outcome.entry is None:
            self._remove(entry)
            return MergePlan("reply", outcome.notice)
        self._store(outcome.entry, replace_requested_at=entry.requested_at)
        if outcome.notice:
            return MergePlan("reply", outcome.notice)
        names = _failed_names(checks)
        waiting = outcome.entry.waiting_for or "waiting"
        return MergePlan(
            "reply",
            f"{label} CI failed ({names}); {waiting}. "
            "I'll merge it automatically when it is green.",
        )

    def discard(self, review: ReviewRecord) -> bool:
        """Forget any pending merge for ``review`` (it is landing or refused)."""

        key = _record_key(review)
        with self._lock:
            entries = self._load()
            kept = tuple(entry for entry in entries if entry.key != key)
            if len(kept) == len(entries):
                return False
            save_pending_merges(kept, self.root)
            return True

    def cancel(self, reference: str) -> str:
        reference = reference.strip()
        with self._lock:
            entries = self._load()
            matches = [entry for entry in entries if _matches(entry, reference)]
            if not matches:
                return f"No pending merge for {reference}."
            if len(matches) > 1:
                return (
                    f"More than one pending merge matches {reference}; "
                    "use the full review URL."
                )
            target = matches[0]
            save_pending_merges(
                tuple(entry for entry in entries if entry.key != target.key),
                self.root,
            )
        self._record_event(
            "pending_merge_cancelled",
            details=_details(target, reason="cancelled by request"),
        )
        return f"Cancelled the pending merge for {target.label}. It will not be merged automatically."

    def entries(self) -> tuple[PendingMerge, ...]:
        with self._lock:
            return self._load()

    def watches(self) -> tuple[WatchedReview, ...]:
        with self._lock:
            return load_ci_watches(self.root)

    def list_lines(self) -> tuple[str, ...]:
        try:
            entries = self.entries()
        except Exception as error:  # noqa: BLE001 - listing must not break /pr
            return (f"Pending merges: unavailable ({error}).",)
        lines = []
        if entries:
            lines.append(f"Pending merges ({len(entries)}):")
        for entry in entries:
            fixes = format_fix_commits(entry.repair)
            lines.append(
                f"- {entry.label} ({entry.waiting_for or 'waiting'}) "
                f"requested {_short_time(entry.requested_at)}"
                + (f"; {fixes}" if fixes else "")
                + (f" {entry.url}" if entry.url else "")
            )
        try:
            merging = {entry.key for entry in entries}
            watches = tuple(
                watch
                for watch in self.watches()
                if watch.key not in merging and not watch.settled
            )
        except Exception:  # noqa: BLE001 - listing must not break /pr
            watches = ()
        if watches:
            lines.append(f"Watching CI ({len(watches)}):")
            for watch in watches:
                lines.append(
                    f"- {watch.label} ({watch.waiting_for or 'waiting for CI'}) "
                    f"from task #{watch.task_id}"
                    + (f" {watch.url}" if watch.url else "")
                )
        return tuple(lines)

    def board_items(self) -> tuple[str, ...]:
        try:
            entries = self.entries()
        except Exception:  # noqa: BLE001 - the board must still render
            return ()
        return tuple(
            f"{entry.label} ({entry.waiting_for or 'waiting'})" for entry in entries
        )

    def board_reviews(self) -> tuple[BoardReview, ...]:
        """Open reviews Enoch watches on its own repository, from cached state.

        Reads only the watcher's state file and never calls the forge. Reviews
        with a pending merge are left out; the board shows them on its
        ``Merging`` line.
        """

        try:
            merging = {entry.key for entry in self.entries()}
            watches = self.watches()
        except Exception:  # noqa: BLE001 - the board must still render
            return ()
        reviews: dict[int, BoardReview] = {}
        for watch in watches:
            if watch.authoritative is not True or not watch.number or watch.key in merging:
                continue
            reviews[watch.number] = BoardReview(watch.number, watch.board_status or BOARD_CI)
        return tuple(reviews[number] for number in sorted(reviews))

    def watch_review(self, chat_id: ConversationId, url: str, task_id: int) -> bool:
        """Watch CI on a review task ``task_id`` published; False when already watched."""

        url = url.strip()
        if not url or not self.supported():
            return False
        repository, number = _parse_review_url(url)
        entry = WatchedReview(
            number=number,
            url=url,
            repository=repository,
            review_id=url,
            chat_id=chat_id,
            task_id=task_id,
            started_at=_format_time(self._now()),
        )
        with self._lock:
            watches = load_ci_watches(self.root)
            previous = next((item for item in watches if item.key == entry.key), None)
            if previous is not None and not previous.settled:
                return False
            if previous is not None:
                # A new task published to a review that was only tracked: watch
                # its CI again, keeping what the board already knows about it.
                entry = replace(
                    entry,
                    board_status=previous.board_status,
                    authoritative=previous.authoritative,
                )
            save_ci_watches(
                (*(item for item in watches if item.key != entry.key), entry),
                self.root,
            )
            self._tracked_checks.pop(entry.key, None)
        self._record_event("ci_watch_started", details=_watch_details(entry))
        return True

    # Watcher -----------------------------------------------------------

    def tick(self) -> None:
        """Process pending merges and CI watches at most once per interval; never raises."""

        moment = self._monotonic()
        if self._last_check is not None and moment - self._last_check < self.interval_seconds:
            return
        self._last_check = moment
        try:
            entries = self.entries()
            for entry in entries:
                self._process_safely(entry)
            self._classify_watches()
            for watch in self.watches():
                self._process_watch_safely(watch)
            self._last_error = ""
        except StaleDaemonEpoch:
            raise
        except Exception as error:  # noqa: BLE001 - never raise into run_once
            self._report_error("pending_merges_failed", error)

    def _process_safely(self, entry: PendingMerge) -> None:
        try:
            outcome = self._process(entry)
        except StaleDaemonEpoch:
            raise
        except Exception as error:  # noqa: BLE001 - one review must not stop the rest
            self._report_error("pending_merge_check_failed", error, entry)
            return
        for name, status, details in outcome.events:
            self._record_event(name, status=status, details=details)
        if outcome.entry is None:
            if not self._remove(entry):
                return  # refreshed or cancelled meanwhile
        elif outcome.entry != entry and not self._store(
            outcome.entry, replace_requested_at=entry.requested_at
        ):
            return
        if outcome.notice:
            suffix = outcome.notice_suffix or "notice"
            self._notify(
                entry.chat_id,
                outcome.notice,
                f"pending-merge:{entry.key}:{entry.requested_at}:{suffix}",
            )

    def _process(self, entry: PendingMerge) -> _Outcome:
        current = self._inspect(entry.identity())
        number = entry.number or _number(current)
        if current.state == "landed" or current.landed_revision is not None:
            return _Outcome(
                None,
                f"✅ #{number} was already merged{_fix_suffix(entry.repair)}.",
                "already-merged",
                [("pending_merge_already_merged", "ok", _details(entry))],
            )
        if current.state != "open":
            return _done(entry, f"⏹ Not merging #{number}: it was {current.state}.", "closed")
        entry, stopped = self._settle_fix_attempt(entry, number)
        if stopped is not None:
            return stopped
        head = _head(current)
        fixing = entry.repair.active_attempt
        if head not in entry.allowed_head_shas:
            if entry.unverified_update_from and self._verify_update(entry, head):
                entry = replace(entry.allow(head), unverified_update_from="")
            elif fixing is not None:
                # Decide once the fix task reports which head it pushed.
                return self._wait(entry, number, _fixing_label(fixing))
            else:
                return _done(
                    entry,
                    f"⏹ Not merging #{number}: new commits were pushed after your "
                    "merge request. Ask again to merge the new version.",
                    "foreign-head",
                    head=head,
                )
        if current.draft:
            return _done(entry, f"⏹ Not merging #{number}: it was marked as a draft.", "draft")
        merge_state = _signal(current, "merge-state")
        if _signal(current, "mergeable") == "conflicting" or merge_state == "dirty":
            return _done(entry, f"❌ Not merging #{number}: it has merge conflicts.", "conflicts")
        if fixing is not None:
            # Never update or land under a running fix task; it pushes on this head.
            return self._wait(entry, number, _fixing_label(fixing))
        base = _base_branch(current) or entry.base_branch or "its base branch"
        if merge_state == "behind":
            expired = self._expired(entry, number, f"behind {base}")
            if expired is not None:
                return expired
            if not self._can_update():
                return _done(
                    entry,
                    f"⏹ Not merging #{number}: it is behind {base} and this forge "
                    "cannot update review branches.",
                    "cannot-update",
                )
            updated = self._update_branch(entry, head)
            return _Outcome(replace(updated, waiting_for=f"updating from {base}"))
        checks = self._checks(entry.identity())
        if checks.revision != head:
            return self._wait(entry, number, "waiting for CI")
        if checks.failed:
            return self._repair_merge(entry, number, head, checks, current)
        if checks.pending:
            return self._wait(entry, number, "waiting for CI")
        if merge_state in READY_MERGE_STATES and _signal(current, "mergeable") == "mergeable":
            return self._land(entry, number, head)
        if merge_state == "blocked":
            waiting = self._wait(entry, number, "blocked, not by CI")
            if waiting.entry is not None and not entry.blocked_notified:
                waiting.entry = replace(waiting.entry, blocked_notified=True)
                waiting.notice = (
                    f"⏸ #{number} is blocked for a non-CI reason (for example a required "
                    "review or unresolved conversations). I'll keep waiting and merge it "
                    "when that clears."
                )
                waiting.notice_suffix = "blocked"
            return waiting
        return self._wait(entry, number, "waiting for merge status")

    def _wait(self, entry: PendingMerge, number: int, label: str) -> _Outcome:
        expired = self._expired(entry, number, label)
        if expired is not None:
            return expired
        return _Outcome(replace(entry, waiting_for=label))

    def _expired(self, entry: PendingMerge, number: int, label: str) -> _Outcome | None:
        """Stop after ``timeout`` of active waiting, or ``max_wait`` overall.

        Time with a fix task queued or running does not count toward ``timeout``.
        """

        requested = _parse_time(entry.requested_at)
        if requested is None:
            limit = self.timeout
        else:
            now = self._now()
            elapsed = now - requested
            if elapsed >= self.max_wait:
                limit = self.max_wait
            elif entry.repair.pause_started_at:
                return None
            elif elapsed - timedelta(seconds=entry.repair.paused_seconds) < self.timeout:
                return None
            else:
                limit = self.timeout
        fixes = entry.repair.task_ids
        tasks = (
            f" Fix task{'s' if len(fixes) != 1 else ''}: "
            + ", ".join(f"#{task_id}" for task_id in fixes)
            + "."
            if fixes
            else ""
        )
        return _done(
            entry,
            f"⏹ Not merging #{number}: still not ready after {_span(limit)} ({label}). "
            f"Ask again to merge it when it is ready.{tasks}",
            "timeout",
        )

    def _land(self, entry: PendingMerge, number: int, head: str) -> _Outcome:
        with self._lock:
            if not any(
                item.key == entry.key and item.requested_at == entry.requested_at
                for item in self._load()
            ):
                return _Outcome(entry)  # cancelled meanwhile; _remove will no-op
        review = self._review()
        self._run_authorized(
            "forge.land",
            ("forge.land",),
            review.land_review,
            ReviewLandRequest(entry.identity(), expected_revision=head),
            root=self.root,
        )
        try:
            self._after_land()
        except StaleDaemonEpoch:
            raise
        except Exception as error:  # noqa: BLE001 - the merge already happened
            self._report_error("pending_merge_after_land_failed", error, entry)
        return _Outcome(
            None,
            f"✅ Merged #{number}{_fix_suffix(entry.repair)}.",
            "merged",
            [
                (
                    "pending_merge_landed",
                    "ok",
                    _details(entry, head=head, fix_tasks=list(entry.repair.task_ids)),
                )
            ],
        )

    # CI repair -------------------------------------------------------------

    def _repair_merge(
        self,
        entry: PendingMerge,
        number: int,
        head: str,
        checks: ReviewCheckReport,
        current: ReviewRecord,
    ) -> _Outcome:
        """Re-run, fix, or give up on red CI for a pending merge."""

        if not self._can_repair():
            return _done(entry, _ci_failed_message(number, checks, current), "ci-failed")
        base = _base_branch(current) or entry.base_branch
        if base and not self._compare(entry.identity(), base, head).descends:
            # The forge's merge state has not caught up with a moved base yet;
            # the head is updated from the base before anything is fixed.
            return self._wait(entry, number, f"waiting for {base} to settle")
        names = _failed_names(checks)
        step = self._repair_step(
            identity=entry.identity(),
            repair=entry.repair,
            head=head,
            number=number,
            url=entry.url,
            base=base,
            chat_id=entry.chat_id,
            checks=checks,
            limit=MAX_MERGE_FIX_ATTEMPTS,
            goal=_MERGE_FIX_GOAL,
            key=f"{entry.key}:{entry.requested_at}",
        )
        entry = replace(entry, repair=step.repair)
        if step.action == "rerun":
            outcome = self._wait(entry, number, "re-running failed CI jobs")
            outcome.events.append(
                ("pending_merge_ci_rerun", "ok", _details(entry, head=head, reason=step.reason))
            )
            return outcome
        if step.action == "exhausted":
            tasks = ", ".join(f"#{task_id}" for task_id in entry.repair.task_ids)
            return _done(
                entry,
                f"❌ Not merging #{number}: CI still fails ({names}) after "
                f"{len(entry.repair.attempts)} automatic fix attempt"
                f"{'s' if len(entry.repair.attempts) != 1 else ''} (task"
                f"{'s' if len(entry.repair.attempts) != 1 else ''} {tasks}).",
                "fix-attempts-exhausted",
                head=head,
            )
        entry = replace(entry, waiting_for=f"fixing CI (task #{step.task_id})")
        return _Outcome(
            entry,
            f"🔧 #{number} CI failed ({names}); started task #{step.task_id} to fix it, "
            "will merge when green.",
            f"fix-{step.task_id}",
            [
                (
                    "pending_merge_fix_queued",
                    "ok",
                    _details(entry, head=head, task_id=step.task_id, checks=names),
                )
            ],
        )

    def _repair_step(
        self,
        *,
        identity: ReviewIdentity,
        repair: CiRepair,
        head: str,
        number: int,
        url: str,
        base: str,
        chat_id: ConversationId,
        checks: ReviewCheckReport,
        limit: int,
        goal: str,
        key: str,
    ) -> _RepairStep:
        failures = self._failures(identity, head)
        classification = classify_check_failures(failures)
        if classification.infrastructure and head not in repair.reruns:
            repair = replace(repair, reruns=(*repair.reruns, head))
            try:
                self._rerun(identity, head)
            except _FORGE_ERRORS as error:
                self._record_event(
                    "ci_rerun_failed",
                    status="failed",
                    details={"review": identity.url or identity.id, "head": head, "error": str(error)},
                )
            else:
                return _RepairStep("rerun", repair, reason=classification.reason)
        if len(repair.attempts) >= limit:
            return _RepairStep("exhausted", repair, reason=classification.reason)
        assert self._queue_fix is not None  # guarded by _can_repair
        check_names = tuple(dict.fromkeys(check.name for check in checks.failed))
        brief = ci_fix_task_brief(
            number=number,
            url=url,
            head=head,
            base_branch=base,
            failures=failures,
            check_names=check_names,
            goal=goal,
        )
        task_id = int(
            self._queue_fix(chat_id, brief, f"ci-fix:{key}:{len(repair.attempts) + 1}")
        )
        started = _format_time(self._now())
        attempt = FixAttempt(
            task_id=task_id,
            head=head,
            checks=check_names,
            started_at=started,
        )
        return _RepairStep(
            "fix",
            replace(repair, attempts=(*repair.attempts, attempt), pause_started_at=started),
            task_id=task_id,
            reason=classification.reason,
        )

    def _settle_fix_attempt(self, entry: Any, number: int) -> tuple[Any, _Outcome | None]:
        """Apply the active fix task's reported result, once it has one or has ended.

        A reported push is accepted only when it starts from the head the task
        was asked to fix, that head is authorized (for merges), and the forge
        confirms the pushed head descends from it. Returns the updated entry and,
        when the failure reproduces on the base, the outcome that stops.
        """

        attempt = entry.repair.active_attempt
        if attempt is None:
            return entry, None
        result = load_ci_fix_result(attempt.task_id, self.root)
        if result is None:
            if self._task_status(attempt.task_id) not in TERMINAL_TASK_STATES:
                return entry, None
            return self._finish_attempt(entry, replace(attempt, state="no-push")), None
        if result.outcome == "main-failure":
            entry = self._finish_attempt(
                entry, replace(attempt, state="main-failure", review_url=result.review_url)
            )
            return entry, self._main_failure_outcome(entry, number, attempt, result.review_url)
        if result.outcome != "pushed":
            return self._finish_attempt(entry, replace(attempt, state="no-push")), None
        authorized = (
            attempt.head in entry.allowed_head_shas
            if isinstance(entry, PendingMerge)
            else True
        )
        comparison = (
            self._compare(entry.identity(), attempt.head, result.head_sha)
            if authorized and result.previous_head_sha == attempt.head
            else None
        )
        if comparison is None or not comparison.descends or comparison.revision != result.head_sha:
            self._record_event(
                "ci_fix_push_rejected",
                status="failed",
                details={
                    "review": entry.url or entry.review_id,
                    "task_id": attempt.task_id,
                    "head": attempt.head,
                    "reported_previous_head": result.previous_head_sha,
                    "reported_head": result.head_sha,
                },
            )
            return self._finish_attempt(entry, replace(attempt, state="no-push")), None
        pushed = replace(
            attempt,
            state="pushed",
            pushed_head=result.head_sha,
            commits=tuple(
                FixCommit(commit.revision, commit.subject) for commit in comparison.commits
            ),
        )
        entry = self._finish_attempt(entry, pushed)
        if isinstance(entry, PendingMerge):
            entry = entry.allow(result.head_sha)
        self._record_event(
            "ci_fix_push_accepted",
            details={
                "review": entry.url or entry.review_id,
                "task_id": attempt.task_id,
                "head": result.head_sha,
                "commits": [commit.revision for commit in pushed.commits],
            },
        )
        return entry, None

    def _finish_attempt(self, entry: Any, attempt: FixAttempt) -> Any:
        repair = entry.repair.replace_attempt(attempt)
        started = _parse_time(repair.pause_started_at)
        if started is not None:
            paused = max(0.0, (self._now() - started).total_seconds())
            repair = replace(
                repair,
                paused_seconds=repair.paused_seconds + paused,
                pause_started_at="",
            )
        return replace(entry, repair=repair)

    def _main_failure_outcome(
        self,
        entry: Any,
        number: int,
        attempt: FixAttempt,
        review_url: str,
    ) -> _Outcome:
        fix = review_url or "a separate review"
        if isinstance(entry, PendingMerge):
            base = entry.base_branch or "the base branch"
            return _done(
                entry,
                f"⏹ Not merging #{number}: its CI failure also happens on {base}. "
                f"Task #{attempt.task_id} opened {fix} to fix {base}; that fix needs "
                f"its own merge. Ask again to merge #{number} after it lands.",
                "main-failure",
                task_id=attempt.task_id,
                fix_review=review_url,
            )
        return _Outcome(
            None,
            f"⚠️ PR #{number} CI failure also happens on the base branch. "
            f"Task #{attempt.task_id} opened {fix} to fix it there.",
            "main-failure",
            [("ci_watch_stopped", "ok", _watch_details(entry, reason="main-failure"))],
        )

    # Watched reviews (published by tasks, not requested to merge) -----------

    def _process_watch_safely(self, watch: WatchedReview) -> None:
        if watch.settled and not self._tracked_check_due(watch):
            return
        try:
            outcome = self._process_watch(watch)
        except StaleDaemonEpoch:
            raise
        except Exception as error:  # noqa: BLE001 - one review must not stop the rest
            self._report_error("ci_watch_check_failed", error, None)
            return
        for name, status, details in outcome.events:
            self._record_event(name, status=status, details=details)
        if outcome.entry is None:
            if not self._remove_watch(watch):
                return
        elif outcome.entry != watch and not self._store_watch(outcome.entry, watch.started_at):
            return
        if outcome.notice:
            self._notify(
                watch.chat_id,
                outcome.notice,
                f"ci-watch:{watch.key}:{watch.started_at}:{outcome.notice_suffix or 'notice'}",
            )

    def _process_watch(self, watch: WatchedReview) -> _Outcome:
        if any(entry.key == watch.key for entry in self.entries()):
            # A requested merge owns this review and reports its own result.
            # Check it again right away once that merge is gone.
            self._tracked_checks.pop(watch.key, None)
            if watch.waiting_for == "merge pending":
                return _Outcome(watch)
            return _Outcome(replace(watch, waiting_for="merge pending"))
        current = self._inspect(watch.identity())
        if current.state != "open" or current.landed_revision is not None:
            self._tracked_checks.pop(watch.key, None)
            return _stop_watch(watch, "landed" if current.state == "landed" else current.state)
        started = _parse_time(watch.started_at)
        if watch.settled:
            self._tracked_checks[watch.key] = self._monotonic()
            if watch.authoritative is False:
                return _stop_watch(watch, "other-repository")
            if started is None or self._now() - started >= self.tracked_timeout:
                return _stop_watch(watch, "tracking-timeout")
            checks = self._checks(watch.identity())
            return _Outcome(self._with_board_status(watch, current, checks))
        if started is None or self._now() - started >= self.watch_timeout:
            outcome = _stop_watch(watch, "timeout")
            checks = self._checks(watch.identity())
        else:
            checks = self._checks(watch.identity())
            outcome = self._advance_watch(watch, current, checks)
        if outcome.entry is None:
            if watch.authoritative is False:
                return outcome  # not on the board, so nothing left to track
            # The CI watch is done; keep tracking the open review for the board.
            outcome.entry = replace(watch, settled=True, waiting_for="")
            self._tracked_checks[watch.key] = self._monotonic()
        outcome.entry = self._with_board_status(outcome.entry, current, checks)
        return outcome

    def _advance_watch(
        self,
        watch: WatchedReview,
        current: ReviewRecord,
        checks: ReviewCheckReport,
    ) -> _Outcome:
        """Report or repair CI on a watched review; ``None`` entry when settled."""

        number = watch.number or _number(current)
        watch, stopped = self._settle_fix_attempt(watch, number)
        if stopped is not None:
            return stopped
        fixing = watch.repair.active_attempt
        if fixing is not None:
            return _Outcome(replace(watch, waiting_for=_fixing_label(fixing)))
        head = _head(current)
        if checks.revision != head or checks.pending:
            return _Outcome(replace(watch, waiting_for="waiting for CI"))
        names = _failed_names(checks)
        if not checks.failed:
            return _Outcome(
                None,
                f"✅ PR #{number} CI passed. Ready to merge.",
                "green",
                [("ci_watch_stopped", "ok", _watch_details(watch, reason="green", head=head))],
            )
        if not self._can_repair():
            return _Outcome(
                None,
                f"❌ PR #{number} CI failed ({names}).",
                "red",
                [("ci_watch_stopped", "ok", _watch_details(watch, reason="red", head=head))],
            )
        if (
            current.draft
            or _signal(current, "mergeable") == "conflicting"
            or _signal(current, "merge-state") == "dirty"
        ):
            reason = "it is a draft" if current.draft else "it has merge conflicts"
            return _Outcome(
                None,
                f"❌ PR #{number} CI failed ({names}). Not fixing it automatically: {reason}.",
                "red-unfixable",
                [("ci_watch_stopped", "ok", _watch_details(watch, reason="unfixable", head=head))],
            )
        step = self._repair_step(
            identity=watch.identity(),
            repair=watch.repair,
            head=head,
            number=number,
            url=watch.url,
            base=_base_branch(current),
            chat_id=watch.chat_id,
            checks=checks,
            limit=MAX_WATCH_FIX_ATTEMPTS,
            goal=_WATCH_FIX_GOAL,
            key=f"watch:{watch.key}:{watch.started_at}",
        )
        watch = replace(watch, repair=step.repair)
        if step.action == "rerun":
            return _Outcome(
                replace(watch, waiting_for="re-running failed CI jobs"),
                events=[("ci_watch_rerun", "ok", _watch_details(watch, head=head, reason=step.reason))],
            )
        if step.action == "exhausted":
            tasks = ", ".join(f"#{task_id}" for task_id in watch.repair.task_ids)
            detail = f" after an automatic fix attempt (task {tasks})" if tasks else " after a re-run"
            return _Outcome(
                None,
                f"❌ PR #{number} CI still fails ({names}){detail}.",
                "exhausted",
                [("ci_watch_stopped", "ok", _watch_details(watch, reason="exhausted", head=head))],
            )
        return _Outcome(
            replace(watch, waiting_for=f"fixing CI (task #{step.task_id})"),
            f"🔧 PR #{number} CI failed ({names}); started task #{step.task_id} to fix it.",
            f"fix-{step.task_id}",
            [("ci_watch_fix_queued", "ok", _watch_details(watch, head=head, task_id=step.task_id))],
        )

    # Board status of watched reviews ---------------------------------------

    def _tracked_check_due(self, watch: WatchedReview) -> bool:
        last = self._tracked_checks.get(watch.key)
        return last is None or self._monotonic() - last >= self.tracked_interval_seconds

    def _with_board_status(
        self,
        watch: WatchedReview,
        current: ReviewRecord,
        checks: ReviewCheckReport,
    ) -> WatchedReview:
        status = self._board_status(watch, current, checks)
        return watch if status == watch.board_status else replace(watch, board_status=status)

    def _board_status(
        self,
        watch: WatchedReview,
        current: ReviewRecord,
        checks: ReviewCheckReport,
    ) -> str:
        """Classify a watched review the way the merge watcher judges readiness.

        Ready needs green checks on the current head and a forge merge state
        that would land (so the head contains the base tip). A review behind
        its base counts as CI (updating), and so does red CI on a head that
        does not contain the base tip yet: right after the base moves the
        forge can briefly report the old head's failures.
        """

        merge_state = _signal(current, "merge-state")
        mergeable = _signal(current, "mergeable")
        if current.draft or mergeable == "conflicting" or merge_state == "dirty":
            return BOARD_BLOCKED
        if merge_state == "behind":
            return BOARD_CI
        head = _head(current)
        if checks.revision != head or checks.pending:
            return BOARD_CI
        if checks.failed:
            behind = self._head_missing_base_tip(watch, current, head)
            if behind is None:
                return watch.board_status or BOARD_CI
            return BOARD_CI if behind else BOARD_FAILED
        if merge_state in READY_MERGE_STATES and mergeable == "mergeable":
            return BOARD_READY
        if merge_state == "blocked":
            return BOARD_BLOCKED
        # The forge is still computing the merge state; keep the last status.
        return watch.board_status or BOARD_CI

    def _head_missing_base_tip(
        self,
        watch: WatchedReview,
        current: ReviewRecord,
        head: str,
    ) -> bool | None:
        """True when ``head`` lacks the base tip; None when that is unknown now."""

        base = _base_branch(current)
        if not base or not self._supports(CI_REPAIR_CAPABILITY, "compare_review_revisions"):
            return False
        try:
            return not self._compare(watch.identity(), base, head).descends
        except _FORGE_ERRORS:
            return None

    def _classify_watches(self) -> None:
        """Learn once which watched reviews are on Enoch's authoritative repository."""

        if all(watch.authoritative is not None for watch in self.watches()):
            return
        try:
            self._authorize("forge.list", ("forge.inspect",))
            records = self._review().list_open_reviews(self.root, limit=OPEN_REVIEW_LIST_LIMIT)
            keys = {_record_key(record) for record in records}
        except StaleDaemonEpoch:
            raise
        except Exception as error:  # noqa: BLE001 - retried on the next cycle
            self._report_error("ci_watch_repository_check_failed", error)
            return
        with self._lock:
            watches = load_ci_watches(self.root)
            updated = tuple(
                replace(watch, authoritative=watch.key in keys)
                if watch.authoritative is None
                else watch
                for watch in watches
            )
            if updated != watches:
                save_ci_watches(updated, self.root)

    # Forge operations ----------------------------------------------------

    def _inspect(self, identity: ReviewIdentity) -> ReviewRecord:
        self._authorize("forge.inspect", ("forge.inspect",))
        return self._review().inspect_review(identity, self.root)

    def _checks(self, identity: ReviewIdentity) -> ReviewCheckReport:
        self._authorize("forge.check-status", ("forge.inspect", CHECK_STATUS_CAPABILITY))
        report = self._review().inspect_review_checks(identity, self.root)
        if not isinstance(report, ReviewCheckReport):
            raise ReviewProviderError("Forge returned invalid review check data.")
        return report

    def _update_branch(self, entry: PendingMerge, head: str) -> PendingMerge:
        """Update ``entry``'s review from its base, pinned to ``head``."""

        try:
            result = self._run_authorized(
                "forge.update-branch",
                ("forge.land", UPDATE_BRANCH_CAPABILITY),
                self._review().update_review_branch,
                entry.identity(),
                expected_revision=head,
                root=self.root,
            )
        except _FORGE_ERRORS as error:
            # Retried on the next watcher cycle; the timeout bounds the retries.
            self._record_event(
                "pending_merge_update_failed",
                status="failed",
                details=_details(entry, head=head, error=str(error)),
            )
            return entry
        new_head = str(getattr(result, "head_sha", "") or "").strip()
        self._record_event(
            "pending_merge_branch_updated",
            details=_details(entry, head=head, new_head=new_head),
        )
        if new_head:
            return replace(entry.allow(new_head), unverified_update_from="")
        return replace(entry, unverified_update_from=head)

    def _failures(self, identity: ReviewIdentity, head: str) -> tuple[ReviewCheckFailure, ...]:
        self._authorize("forge.check-status", ("forge.inspect", CI_REPAIR_CAPABILITY))
        return tuple(
            self._review().inspect_review_check_failures(identity, revision=head, root=self.root)
        )

    def _rerun(self, identity: ReviewIdentity, head: str) -> None:
        self._run_authorized(
            "forge.rerun-checks",
            ("forge.inspect", CI_REPAIR_CAPABILITY),
            self._review().rerun_failed_review_checks,
            identity,
            revision=head,
            root=self.root,
        )

    def _compare(
        self,
        identity: ReviewIdentity,
        base: str,
        head: str,
    ) -> ReviewRevisionComparison:
        self._authorize("forge.inspect", ("forge.inspect", CI_REPAIR_CAPABILITY))
        result = self._review().compare_review_revisions(
            identity,
            base=base,
            revision=head,
            root=self.root,
        )
        if not isinstance(result, ReviewRevisionComparison):
            raise ReviewProviderError("Forge returned invalid revision comparison data.")
        return result

    def _verify_update(self, entry: PendingMerge, head: str) -> bool:
        self._authorize("forge.inspect", ("forge.inspect", UPDATE_BRANCH_CAPABILITY))
        verify = getattr(self._review(), "verify_review_branch_update", None)
        if not callable(verify):
            return False
        return bool(
            verify(
                entry.identity(),
                previous_revision=entry.unverified_update_from,
                revision=head,
                root=self.root,
            )
        )

    # State ----------------------------------------------------------------

    def _load(self) -> tuple[PendingMerge, ...]:
        return load_pending_merges(self.root)

    def _record_request(self, chat_id: ConversationId, current: ReviewRecord) -> PendingMerge:
        head = _head(current)
        entry = PendingMerge(
            number=_number(current),
            url=current.identity.url,
            repository=str(current.identity.metadata.get("repository") or "").strip(),
            review_id=current.identity.url or current.identity.id,
            authorized_head_sha=head,
            allowed_head_shas=(head,),
            requested_at=_format_time(self._now()),
            chat_id=chat_id,
            base_branch=_base_branch(current),
        )
        with self._lock:
            entries = self._load()
            previous = next((item for item in entries if item.key == entry.key), None)
            refreshed = previous is not None
            if previous is not None:
                # A repeated request keeps the fix-attempt budget and any running
                # fix task, so its push is still recognized.
                entry = replace(entry, repair=previous.repair)
                attempt = previous.repair.active_attempt
                if attempt is not None:
                    entry = entry.allow(attempt.head)
            save_pending_merges(
                (*(item for item in entries if item.key != entry.key), entry),
                self.root,
            )
        self._record_event(
            "pending_merge_refreshed" if refreshed else "pending_merge_requested",
            details=_details(entry, head=head),
        )
        return entry

    def _store(self, entry: PendingMerge, *, replace_requested_at: str) -> bool:
        """Replace the stored entry if it is still the same request."""

        with self._lock:
            entries = self._load()
            if not any(
                item.key == entry.key and item.requested_at == replace_requested_at
                for item in entries
            ):
                return False
            save_pending_merges(
                tuple(entry if item.key == entry.key else item for item in entries),
                self.root,
            )
            return True

    def _store_watch(self, watch: WatchedReview, started_at: str) -> bool:
        with self._lock:
            watches = load_ci_watches(self.root)
            if not any(
                item.key == watch.key and item.started_at == started_at for item in watches
            ):
                return False
            save_ci_watches(
                tuple(watch if item.key == watch.key else item for item in watches),
                self.root,
            )
            return True

    def _remove_watch(self, watch: WatchedReview) -> bool:
        with self._lock:
            watches = load_ci_watches(self.root)
            kept = tuple(
                item
                for item in watches
                if not (item.key == watch.key and item.started_at == watch.started_at)
            )
            if len(kept) == len(watches):
                return False
            save_ci_watches(kept, self.root)
            return True

    def _remove(self, entry: PendingMerge) -> bool:
        with self._lock:
            entries = self._load()
            kept = tuple(
                item
                for item in entries
                if not (item.key == entry.key and item.requested_at == entry.requested_at)
            )
            if len(kept) == len(entries):
                return False
            save_pending_merges(kept, self.root)
            return True

    def _report_error(
        self,
        event: str,
        error: Exception,
        entry: PendingMerge | None = None,
    ) -> None:
        message = f"{type(error).__name__}: {error}"
        if message == self._last_error:
            return  # log each distinct failure once instead of every cycle
        self._last_error = message
        details: dict[str, object] = {"error": message}
        if entry is not None:
            details.update(_details(entry))
        self._record_event(event, status="failed", details=details)


def _done(entry: PendingMerge, notice: str, reason: str, **extra: object) -> _Outcome:
    return _Outcome(
        None,
        notice,
        reason,
        [("pending_merge_stopped", "ok", _details(entry, reason=reason, **extra))],
    )


def _terminal_refusal(record: ReviewRecord) -> bool:
    return (
        record.state != "open"
        or record.draft
        or _signal(record, "mergeable") == "conflicting"
        or _signal(record, "merge-state") == "dirty"
    )


def _waiting_reasons(
    merge_state: str,
    checks: ReviewCheckReport | None,
    base: str,
) -> list[str]:
    reasons = []
    if merge_state == "behind":
        reasons.append(f"behind {base}")
    if checks is None or checks.pending or merge_state == "behind":
        reasons.append("CI running")
    elif merge_state == "blocked":
        reasons.append("blocked, not by CI")
    if not reasons:
        reasons.append("merge status not ready")
    return reasons


def _board_label(merge_state: str, reasons: list[str]) -> str:
    if merge_state == "behind":
        return reasons[0].replace("behind", "updating from", 1)
    if "CI running" in reasons:
        return "waiting for CI"
    return reasons[0]


def _ci_failed_message(number: int, checks: ReviewCheckReport, record: ReviewRecord) -> str:
    names = ", ".join(dict.fromkeys(check.name for check in checks.failed))
    label = f"#{number}" if number else record.identity.id
    return f"❌ Not merging {label}: CI failed ({names})."


def _failed_names(checks: ReviewCheckReport) -> str:
    return ", ".join(dict.fromkeys(check.name for check in checks.failed))


def _fixing_label(attempt: FixAttempt) -> str:
    return f"fixing CI (task #{attempt.task_id})"


def _fix_suffix(repair: CiRepair) -> str:
    """`` (+1 fix commit by task #61: abc1234 Subject)`` or empty."""

    fixes = format_fix_commits(repair)
    return f" ({fixes})" if fixes else ""


def _span(limit: timedelta) -> str:
    hours = limit.total_seconds() / 3600
    return f"{hours:g} hours" if hours != 1 else "1 hour"


def _stop_watch(watch: WatchedReview, reason: str) -> _Outcome:
    return _Outcome(None, events=[("ci_watch_stopped", "ok", _watch_details(watch, reason=reason))])


def _watch_details(watch: WatchedReview, **extra: object) -> dict[str, object]:
    details: dict[str, object] = {
        "number": watch.number,
        "url": watch.url,
        "repository": watch.repository,
        "task_id": watch.task_id,
        "started_at": watch.started_at,
        "fix_tasks": list(watch.repair.task_ids),
    }
    details.update(extra)
    return details


_REVIEW_URL = re.compile(r"^https?://[^/]+/(?P<repository>.+?)/(?:-/)?(?:pull|pulls|merge_requests)/(?P<number>\d+)/?$")


def _parse_review_url(url: str) -> tuple[str, int]:
    match = _REVIEW_URL.match(url.strip())
    if match is None:
        return "", 0
    return match.group("repository"), int(match.group("number"))


def _signal(record: ReviewRecord, name: str) -> str:
    for signal in record.signals:
        if signal.name == name:
            return signal.status
    return ""


def _head(record: ReviewRecord) -> str:
    return record.versions[-1].revision.id


def _number(record: ReviewRecord) -> int:
    try:
        return int(record.identity.metadata.get("number") or 0)
    except (TypeError, ValueError):
        return 0


def _base_branch(record: ReviewRecord) -> str:
    return str(record.identity.metadata.get("base_branch") or "").strip()


def _record_key(record: ReviewRecord) -> str:
    return _review_key(
        str(record.identity.metadata.get("repository") or "").strip(),
        _number(record),
        record.identity.url or record.identity.id,
    )


def _review_key(repository: str, number: int, fallback: str) -> str:
    if repository and number:
        return f"{repository.casefold()}#{number}"
    return fallback.strip().rstrip("/").casefold()


def _matches(entry: PendingMerge, reference: str) -> bool:
    cleaned = reference.strip().lstrip("#")
    if cleaned.isdigit():
        return entry.number == int(cleaned)
    normalized = cleaned.rstrip("/").casefold()
    return normalized in {
        entry.url.rstrip("/").casefold(),
        entry.review_id.rstrip("/").casefold(),
        entry.key,
    }


def _details(entry: PendingMerge, **extra: object) -> dict[str, object]:
    details: dict[str, object] = {
        "number": entry.number,
        "url": entry.url,
        "repository": entry.repository,
        "authorized_head_sha": entry.authorized_head_sha,
        "allowed_head_shas": list(entry.allowed_head_shas),
        "requested_at": entry.requested_at,
    }
    details.update(extra)
    return details


def _format_time(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _short_time(value: str) -> str:
    parsed = _parse_time(value)
    if parsed is None:
        return "at an unknown time"
    return f"at {parsed.astimezone().strftime('%H:%M')}"


def _entry_to_json(entry: PendingMerge) -> dict[str, object]:
    return {
        "number": entry.number,
        "url": entry.url,
        "repository": entry.repository,
        "review_id": entry.review_id,
        "authorized_head_sha": entry.authorized_head_sha,
        "allowed_head_shas": list(entry.allowed_head_shas),
        "requested_at": entry.requested_at,
        "chat_id": entry.chat_id,
        "base_branch": entry.base_branch,
        "waiting_for": entry.waiting_for,
        "blocked_notified": entry.blocked_notified,
        "unverified_update_from": entry.unverified_update_from,
        "repair": ci_repair_to_json(entry.repair),
    }


def _entry_from_json(item: object) -> PendingMerge | None:
    if not isinstance(item, dict):
        return None
    chat_id = normalize_conversation_id(item.get("chat_id"))
    authorized = str(item.get("authorized_head_sha") or "").strip()
    review_id = str(item.get("review_id") or item.get("url") or "").strip()
    requested_at = str(item.get("requested_at") or "").strip()
    if chat_id is None or not authorized or not review_id or not requested_at:
        return None
    raw_allowed = item.get("allowed_head_shas")
    allowed = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (raw_allowed if isinstance(raw_allowed, list) else [authorized])
            if str(value).strip()
        )
    )
    try:
        number = int(item.get("number") or 0)
    except (TypeError, ValueError):
        number = 0
    return PendingMerge(
        number=number,
        url=str(item.get("url") or "").strip(),
        repository=str(item.get("repository") or "").strip(),
        review_id=review_id,
        authorized_head_sha=authorized,
        allowed_head_shas=allowed or (authorized,),
        requested_at=requested_at,
        chat_id=chat_id,
        base_branch=str(item.get("base_branch") or "").strip(),
        waiting_for=str(item.get("waiting_for") or "").strip(),
        blocked_notified=item.get("blocked_notified") is True,
        unverified_update_from=str(item.get("unverified_update_from") or "").strip(),
        repair=ci_repair_from_json(item.get("repair")),
    )


def _watch_to_json(entry: WatchedReview) -> dict[str, object]:
    return {
        "number": entry.number,
        "url": entry.url,
        "repository": entry.repository,
        "review_id": entry.review_id,
        "chat_id": entry.chat_id,
        "task_id": entry.task_id,
        "started_at": entry.started_at,
        "waiting_for": entry.waiting_for,
        "repair": ci_repair_to_json(entry.repair),
        "settled": entry.settled,
        "board_status": entry.board_status,
        "authoritative": entry.authoritative,
    }


def _watch_from_json(item: object) -> WatchedReview | None:
    if not isinstance(item, dict):
        return None
    chat_id = normalize_conversation_id(item.get("chat_id"))
    review_id = str(item.get("review_id") or item.get("url") or "").strip()
    started_at = str(item.get("started_at") or "").strip()
    if chat_id is None or not review_id or not started_at:
        return None
    try:
        number = int(item.get("number") or 0)
        task_id = int(item.get("task_id") or 0)
    except (TypeError, ValueError):
        return None
    return WatchedReview(
        number=number,
        url=str(item.get("url") or "").strip(),
        repository=str(item.get("repository") or "").strip(),
        review_id=review_id,
        chat_id=chat_id,
        task_id=task_id,
        started_at=started_at,
        waiting_for=str(item.get("waiting_for") or "").strip(),
        repair=ci_repair_from_json(item.get("repair")),
        settled=item.get("settled") is True,
        board_status=_board_status_from_json(item.get("board_status")),
        authoritative=(
            item["authoritative"] if isinstance(item.get("authoritative"), bool) else None
        ),
    )


def _board_status_from_json(value: object) -> str:
    status = str(value or "").strip()
    return status if status in {BOARD_READY, BOARD_CI, BOARD_FAILED, BOARD_BLOCKED} else ""
