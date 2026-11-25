"""Hands-off landing for one review a human explicitly asked to merge.

A merge request is the only authorization. When the review is not ready yet
(behind its base, CI running, blocked, or the forge is still computing its
state), Enoch records a pending merge here and a throttled watcher in the
daemon loop lands it once it is ready, with no further human action.

The authorization is pinned to the review head the human saw. The only other
heads Enoch accepts are the ones it created itself by updating the branch from
its base. Red CI is never landed, conflicts are never resolved, and waiting
stops after a fixed time or after a few branch updates fail in a row.

This module is provider-agnostic: it uses the review provider's optional
``forge.check-status`` and ``forge.update-branch`` operations. Forges without
check-status keep today's behavior, where reviews that are not ready are refused.
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

from enoch.app.epoch import StaleDaemonEpoch
from enoch.paths import private_state_path
from enoch.providers.contracts import (
    ConversationId,
    ForgeProviderError,
    ReviewCheckReport,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewProviderError,
    ReviewRecord,
    normalize_conversation_id,
)
from enoch.providers.authorization import CapabilityAuthorizationError
from enoch.state import atomic_write, load_json_object


SCHEMA_VERSION = 1
PENDING_MERGES_STATE_FILE = "pending_merges.json"
PENDING_MERGE_CHECK_INTERVAL_SECONDS = 60.0
PENDING_MERGE_TIMEOUT = timedelta(hours=2)
CHECK_STATUS_CAPABILITY = "forge.check-status"
UPDATE_BRANCH_CAPABILITY = "forge.update-branch"
READY_MERGE_STATES = frozenset({"clean", "unstable", "has-hooks"})
# Consecutive failed branch updates before the watcher stops and says why.
MAX_UPDATE_FAILURES = 3
_FORGE_ERRORS = (ReviewProviderError, ForgeProviderError, CapabilityAuthorizationError)


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
    update_failures: int = 0

    @property
    def key(self) -> str:
        return _review_key(self.repository, self.number, self.url or self.review_id)

    @property
    def label(self) -> str:
        return f"#{self.number}" if self.number else (self.url or self.review_id)

    def identity(self) -> ReviewIdentity:
        return ReviewIdentity(id=self.review_id, url=self.url)

    def allow(self, head: str) -> "PendingMerge":
        head = head.strip()
        if not head or head in self.allowed_head_shas:
            return self
        return replace(self, allowed_head_shas=(*self.allowed_head_shas, head))


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
    entry: PendingMerge | None
    notice: str = ""
    notice_suffix: str = ""
    events: list[tuple[str, str, dict[str, object]]] = field(default_factory=list)


def pending_merges_path(root: Path | None = None) -> Path:
    return private_state_path(PENDING_MERGES_STATE_FILE, root)


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
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        interval_seconds: float = PENDING_MERGE_CHECK_INTERVAL_SECONDS,
        timeout: timedelta = PENDING_MERGE_TIMEOUT,
    ) -> None:
        self.root = root
        self._review = review
        self._authorize = authorize
        self._run_authorized = run_authorized
        self._notify = notify
        self._record_event = record_event
        self._after_land = after_land
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic
        self.interval_seconds = interval_seconds
        self.timeout = timeout
        self._lock = threading.RLock()
        self._last_check: float | None = None
        self._last_error = ""

    # Capabilities -----------------------------------------------------

    def supported(self) -> bool:
        return self._supports(CHECK_STATUS_CAPABILITY, "inspect_review_checks")

    def _can_update(self) -> bool:
        return self._supports(UPDATE_BRANCH_CAPABILITY, "update_review_branch")

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
            self.discard(current)
            return MergePlan("reply", _ci_failed_message(number, checks, current))
        if merge_state == "behind" and not self._can_update():
            return MergePlan(
                "reply",
                f"{label} is behind {base} and must be updated before it can merge. "
                "This forge cannot update review branches automatically.",
            )
        entry = self._record_request(chat_id, current)
        if merge_state == "behind":
            entry, _error = self._update_branch(entry, head)
        reasons = _waiting_reasons(merge_state, checks if checks_current else None, base)
        entry = replace(entry, waiting_for=_board_label(merge_state, reasons))
        self._store(entry, replace_requested_at=entry.requested_at)
        return MergePlan(
            "reply",
            f"{label} isn't ready yet ({' / '.join(reasons)}). "
            "I'll merge it automatically when it is.",
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

    def list_lines(self) -> tuple[str, ...]:
        try:
            entries = self.entries()
        except Exception as error:  # noqa: BLE001 - listing must not break /pr
            return (f"Pending merges: unavailable ({error}).",)
        if not entries:
            return ()
        lines = [f"Pending merges ({len(entries)}):"]
        for entry in entries:
            lines.append(
                f"- {entry.label} ({entry.waiting_for or 'waiting'}) "
                f"requested {_short_time(entry.requested_at)}"
                + (f" {entry.url}" if entry.url else "")
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

    # Watcher -----------------------------------------------------------

    def tick(self) -> None:
        """Process pending merges at most once per interval; never raises."""

        moment = self._monotonic()
        if self._last_check is not None and moment - self._last_check < self.interval_seconds:
            return
        self._last_check = moment
        try:
            entries = self.entries()
            for entry in entries:
                self._process_safely(entry)
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
                f"✅ #{number} was already merged.",
                "already-merged",
                [("pending_merge_already_merged", "ok", _details(entry))],
            )
        if current.state != "open":
            return _done(entry, f"⏹ Not merging #{number}: it was {current.state}.", "closed")
        head = _head(current)
        if head not in entry.allowed_head_shas:
            if entry.unverified_update_from and self._verify_update(entry, head):
                entry = replace(entry.allow(head), unverified_update_from="")
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
            updated, error = self._update_branch(entry, head)
            if error and updated.update_failures >= MAX_UPDATE_FAILURES:
                return _done(
                    updated,
                    f"⏹ Not merging #{number}: could not update the branch from "
                    f"{base} ({_update_failure_reason(error)})",
                    "update-failed",
                    head=head,
                    error=error,
                    attempts=updated.update_failures,
                )
            return _Outcome(replace(updated, waiting_for=f"updating from {base}"))
        checks = self._checks(entry.identity())
        if checks.revision != head:
            return self._wait(entry, number, "waiting for CI")
        if checks.failed:
            return _done(entry, _ci_failed_message(number, checks, current), "ci-failed")
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
        requested = _parse_time(entry.requested_at)
        if requested is not None and self._now() - requested < self.timeout:
            return None
        hours = self.timeout.total_seconds() / 3600
        span = f"{hours:g} hours" if hours != 1 else "1 hour"
        return _done(
            entry,
            f"⏹ Not merging #{number}: still not ready after {span} ({label}). "
            "Ask again to merge it when it is ready.",
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
            f"✅ Merged #{number}.",
            "merged",
            [("pending_merge_landed", "ok", _details(entry, head=head))],
        )

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

    def _update_branch(self, entry: PendingMerge, head: str) -> tuple[PendingMerge, str]:
        """Update ``entry``'s review from its base, pinned to ``head``.

        Returns the new entry and the error text when the update failed.
        """

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
            # Retried on the next watcher cycle until MAX_UPDATE_FAILURES in a
            # row. The forge may still have applied the update despite the
            # error, so a direct update of ``head`` is verified, not refused.
            failures = entry.update_failures + 1
            self._record_event(
                "pending_merge_update_failed",
                status="failed",
                details=_details(entry, head=head, error=str(error), attempt=failures),
            )
            return replace(entry, unverified_update_from=head, update_failures=failures), str(error)
        new_head = str(getattr(result, "head_sha", "") or "").strip()
        self._record_event(
            "pending_merge_branch_updated",
            details=_details(entry, head=head, new_head=new_head),
        )
        if new_head:
            return replace(entry.allow(new_head), unverified_update_from="", update_failures=0), ""
        return replace(entry, unverified_update_from=head, update_failures=0), ""

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
            refreshed = any(item.key == entry.key for item in entries)
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


def _update_failure_reason(error: str) -> str:
    """Drop the forge's "Could not update PR #N from base:" lead-in."""

    reason = re.sub(r"^Could not update .+? from \S+: ", "", error.strip(), count=1)
    return reason.rstrip(".") or "unknown error"


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
        "update_failures": entry.update_failures,
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
        update_failures=_count(item.get("update_failures")),
    )


def _count(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0
