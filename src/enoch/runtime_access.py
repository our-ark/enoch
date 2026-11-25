"""Automatic resume of tasks paused because agent runtime access ran out.

When a runtime reports a plan/usage limit, the running task is paused (never
failed) and the queue stops. This module decides when access is back and
resumes the paused tasks without a human:

1. Wait until the reset time, when one is known. It comes from the runtime
   error (structured ``resets_at`` or text such as
   ``resets 3:20pm (America/Los_Angeles)``) or from the account quota windows.
2. Verify access before resuming. Account quota data is checked first; when it
   is unavailable or inconclusive, a tiny runtime probe is used instead.
3. When access is still unavailable, back off (5 minutes doubling to 1 hour)
   so checks never run in a tight loop.

State is durable so a daemon restart keeps the absolute reset time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import json
from pathlib import Path
import re
import threading
from typing import Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from enoch.app.epoch import StaleDaemonEpoch
from enoch.paths import private_state_path
from enoch.providers import AgentRuntimeAccessUnavailable
from enoch.quota import _number, _timestamp
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object


STATE_FILE = "runtime_access_resume.json"
SCHEMA_VERSION = 1
# Wait a little past the advertised reset so the provider has rolled over.
RESET_GRACE = timedelta(minutes=1)
# Back-off between access checks while access stays unavailable.
INITIAL_BACKOFF = timedelta(minutes=5)
MAX_BACKOFF = timedelta(hours=1)
# Never check more often than this, even when a reset time is imminent.
MIN_CHECK_INTERVAL = timedelta(minutes=1)
# A pause this soon after an automatic resume continues the back-off.
REPEAT_PAUSE_WINDOW = timedelta(hours=1)
# Reset times further out than this are treated as unparseable noise.
MAX_RESET_HORIZON = timedelta(days=8)
MONITOR_POLL_SECONDS = 60.0

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
UNKNOWN = "unknown"

# Host-side safety net for runtime errors that were not raised as
# AgentRuntimeAccessUnavailable. Narrow on purpose: plan/usage limits only.
_USAGE_LIMIT_PATTERNS = (
    re.compile(r"\bhit your [^.\n]{0,40}?\blimits?\b"),
    re.compile(
        r"\b\d+(?:\.\d+)?(?:[- ]?(?:hours?|hrs?|h|days?|d|weeks?|w)) "
        r"(?:usage |session |rate |plan )?limits?\b"
    ),
    re.compile(r"\blimits?\b[^\n]{0,80}?\bresets?\b"),
    re.compile(r"\b(?:usage|session|weekly|daily|monthly|plan) limits?\b"),
    re.compile(r"\bout of (?:extra )?usage\b"),
)

_MONTHS = {
    name: index
    for index, names in enumerate(
        (
            ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"),
            ("may",), ("jun", "june"), ("jul", "july"), ("aug", "august"),
            ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_ISO_RESET = re.compile(
    r"\bresets?\s+(?:at\s+|on\s+)?"
    r"(?P<iso>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2}))",
    re.IGNORECASE,
)
_CLOCK_RESET = re.compile(
    r"\bresets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<month>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap]\.?\s?m\b\.?)?"
    r"(?:\s*\((?P<zone>[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*)\))?",
    re.IGNORECASE,
)
_EPOCH_SUFFIX = re.compile(r"limit reached\|(?P<epoch>\d{10,13})\b", re.IGNORECASE)


def is_usage_limit_text(text: str) -> bool:
    """Return whether a runtime error text describes a plan/usage limit."""

    lowered = " ".join(str(text or "").lower().replace("’", "'").split())
    return any(pattern.search(lowered) for pattern in _USAGE_LIMIT_PATTERNS)


def parse_reset_time(
    text: str,
    *,
    now: datetime,
    default_zone: tzinfo | None = None,
) -> datetime | None:
    """Parse an absolute reset time from a runtime limit message.

    ``now`` is when the message was produced: a bare clock time such as
    ``resets 3:20pm (America/Los_Angeles)`` means its next occurrence.
    """

    cleaned = " ".join(str(text or "").replace("’", "'").split())
    if not cleaned:
        return None
    now = _aware(now)
    candidate = _parse_iso_reset(cleaned) or _parse_epoch_reset(cleaned)
    if candidate is None:
        candidate = _parse_clock_reset(cleaned, now=now, default_zone=default_zone)
    if candidate is None:
        return None
    candidate = candidate.astimezone(timezone.utc)
    if candidate > now + MAX_RESET_HORIZON:
        return None
    return candidate


def _parse_iso_reset(text: str) -> datetime | None:
    match = _ISO_RESET.search(text)
    if match is None:
        return None
    try:
        parsed = datetime.fromisoformat(match.group("iso").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _parse_epoch_reset(text: str) -> datetime | None:
    match = _EPOCH_SUFFIX.search(text)
    if match is None:
        return None
    seconds = int(match.group("epoch"))
    if seconds > 10_000_000_000:
        seconds //= 1000
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_clock_reset(
    text: str,
    *,
    now: datetime,
    default_zone: tzinfo | None,
) -> datetime | None:
    for match in _CLOCK_RESET.finditer(text):
        minute_text = match.group("minute")
        ampm = (match.group("ampm") or "").lower().replace(".", "").replace(" ", "")
        if not ampm and minute_text is None:
            continue  # "resets 5" is not a clock time.
        hour = int(match.group("hour"))
        minute = int(minute_text or 0)
        if minute > 59:
            continue
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        elif hour > 23:
            continue
        zone = _zone(match.group("zone")) or default_zone or _local_zone()
        local_now = now.astimezone(zone)
        month_text = (match.group("month") or "").lower()
        if month_text:
            month = _MONTHS.get(month_text)
            if month is None:
                continue
            try:
                candidate = datetime(
                    local_now.year, month, int(match.group("day")), hour, minute, tzinfo=zone,
                )
            except ValueError:
                continue
            if candidate < local_now - timedelta(days=1):
                try:
                    candidate = candidate.replace(year=candidate.year + 1)
                except ValueError:
                    continue
            return candidate
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # A reset a few minutes in the past is the window that just rolled
        # over; anything older is the next day's occurrence.
        if candidate < local_now - timedelta(minutes=15):
            candidate += timedelta(days=1)
        return candidate
    return None


def _zone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    if name.upper() in {"UTC", "GMT", "Z"}:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _local_zone() -> tzinfo:
    return datetime.now().astimezone().tzinfo or timezone.utc


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def backoff_delay(failed_checks: int) -> timedelta:
    """5, 10, 20, 40 minutes, then at most one check per hour."""

    steps = max(0, int(failed_checks))
    if steps >= 8:
        return MAX_BACKOFF
    return min(MAX_BACKOFF, INITIAL_BACKOFF * (2**steps))


@dataclass(frozen=True)
class AccessCheck:
    verdict: str
    resets_at: datetime | None = None
    source: str = ""


def quota_access_check(snapshot: object, *, now: datetime) -> AccessCheck:
    """Interpret an account quota snapshot.

    Returns UNKNOWN when the data cannot decide, so the caller probes instead.
    Model-scoped windows (``7d / Opus``) alone do not block every model, so
    they also defer to the probe.
    """

    if not isinstance(snapshot, Mapping) or snapshot.get("error"):
        return AccessCheck(UNKNOWN, source="quota")
    windows = snapshot.get("windows")
    if not isinstance(windows, (list, tuple)) or not windows:
        return AccessCheck(UNKNOWN, source="quota")
    readable = 0
    account_blocked: list[datetime | None] = []
    model_blocked = False
    for window in windows:
        if not isinstance(window, Mapping):
            continue
        used = _number(window.get("used_percent"))
        if used is None or used < 0:
            continue
        readable += 1
        reset = _timestamp(window.get("resets_at"))
        if used < 100 or (reset is not None and reset <= now):
            continue
        if " / " in str(window.get("label") or ""):
            model_blocked = True
        else:
            account_blocked.append(reset)
    if account_blocked:
        known = [reset for reset in account_blocked if reset is not None]
        return AccessCheck(UNAVAILABLE, max(known) if known else None, source="quota")
    if readable == 0 or model_blocked:
        return AccessCheck(UNKNOWN, source="quota")
    return AccessCheck(AVAILABLE, source="quota")


def resume_state_path(root: Path) -> Path:
    return private_state_path(STATE_FILE, root)


def _empty_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "resets_at": None,
        "next_check_at": None,
        "failed_checks": 0,
        "last_resumed_at": None,
    }


def _load_state(path: Path) -> dict:
    data = load_json_object(path, default_factory=_empty_state)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise StateCorruptionError(path, "invalid runtime access resume state")
    state = _empty_state()
    state.update({key: data.get(key) for key in state if key in data})
    if not isinstance(state["failed_checks"], int) or state["failed_checks"] < 0:
        state["failed_checks"] = 0
    return state


def _save_state(path: Path, state: Mapping) -> None:
    atomic_write(path, json.dumps(dict(state), indent=2, sort_keys=True) + "\n")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value is not None else None


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return _aware(parsed)


class RuntimeAccessResumer:
    """Resume runtime-paused tasks once access returns, with bounded checks.

    The collaborators are callables so the policy is testable without a
    daemon: ``paused_tasks`` lists tasks paused for runtime access,
    ``quota`` returns the active runtime's account snapshot (or None),
    ``probe`` performs a minimal runtime turn (raising
    AgentRuntimeAccessUnavailable while access is still blocked), ``resume``
    resumes every paused task in original order and ``notify`` reports them.
    """

    def __init__(
        self,
        root: Path,
        *,
        paused_tasks: Callable[[], Sequence[object]],
        quota: Callable[[], object],
        probe: Callable[[], object],
        resume: Callable[[], Sequence[object]],
        notify: Callable[[Sequence[object]], object],
        require_current: Callable[[], object] = lambda: None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self.paused_tasks = paused_tasks
        self.quota = quota
        self.probe = probe
        self.resume = resume
        self.notify = notify
        self.require_current = require_current
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    # State -----------------------------------------------------------------

    def record_pause(
        self,
        reason: str = "",
        *,
        resets_at: datetime | None = None,
        now: datetime | None = None,
    ) -> datetime:
        """Remember a runtime-access pause; return the first check time."""

        now = _aware(now or self.clock())
        reset = _aware(resets_at) if isinstance(resets_at, datetime) else None
        if reset is None:
            reset = parse_reset_time(reason, now=now)
        path = resume_state_path(self.root)
        with self._lock, file_transaction(path):
            state = _load_state(path)
            last_resumed = _parse_iso(state.get("last_resumed_at"))
            existing_check = _parse_iso(state.get("next_check_at"))
            existing_reset = _parse_iso(state.get("resets_at"))
            if existing_check is None:
                # A new pause cycle. Pausing again soon after an automatic
                # resume means access was not really back: keep backing off.
                if last_resumed is not None and now - last_resumed < REPEAT_PAUSE_WINDOW:
                    state["failed_checks"] = int(state["failed_checks"]) + 1
                else:
                    state["failed_checks"] = 0
            if reset is not None and (existing_reset is None or reset > existing_reset):
                existing_reset = reset
            state["resets_at"] = _iso(existing_reset)
            next_check = now + backoff_delay(int(state["failed_checks"]))
            if existing_reset is not None:
                next_check = max(existing_reset + RESET_GRACE, now + MIN_CHECK_INTERVAL)
            if existing_check is not None and existing_check > next_check:
                next_check = existing_check
            state["next_check_at"] = _iso(next_check)
            _save_state(path, state)
        self._wake.set()
        return next_check

    def next_check_at(self) -> datetime | None:
        with self._lock:
            return _parse_iso(_load_state(resume_state_path(self.root)).get("next_check_at"))

    # Policy ----------------------------------------------------------------

    def check_once(self, *, now: datetime | None = None) -> tuple[object, ...]:
        """Run at most one access check; return the tasks it resumed."""

        self.require_current()
        now = _aware(now or self.clock())
        path = resume_state_path(self.root)
        with self._lock:
            if not tuple(self.paused_tasks()):
                self._clear_cycle(path)
                return ()
            with file_transaction(path):
                state = _load_state(path)
            next_check = _parse_iso(state.get("next_check_at"))
            if next_check is None:
                # Paused before this monitor knew (e.g. by an older daemon).
                # A bare clock time in that old message cannot be anchored to
                # a date any more, so rely on back-off plus verification.
                self.record_pause("", now=now)
                return ()
            if now < next_check:
                return ()
        # Quota reads and probes can take a while; do not hold the lock.
        check = self._check_access(now)
        with self._lock:
            if check.verdict != AVAILABLE:
                self._record_failed_check(path, check, now)
                return ()
            self.require_current()
            resumed = tuple(self.resume())
            with file_transaction(path):
                state = _load_state(path)
                state.update(
                    resets_at=None,
                    next_check_at=None,
                    last_resumed_at=_iso(now),
                )
                _save_state(path, state)
        if resumed:
            self.notify(resumed)
        return resumed

    def _check_access(self, now: datetime) -> AccessCheck:
        try:
            quota = quota_access_check(self.quota(), now=now)
        except StaleDaemonEpoch:
            raise
        except Exception:
            quota = AccessCheck(UNKNOWN, source="quota")
        if quota.verdict != UNKNOWN:
            return quota
        try:
            self.probe()
        except StaleDaemonEpoch:
            raise
        except AgentRuntimeAccessUnavailable as error:
            reset = getattr(error, "resets_at", None)
            if not isinstance(reset, datetime):
                reset = parse_reset_time(str(error), now=now)
            return AccessCheck(UNAVAILABLE, reset, source="probe")
        except Exception:
            return AccessCheck(UNKNOWN, source="probe")
        return AccessCheck(AVAILABLE, source="probe")

    def _record_failed_check(self, path: Path, check: AccessCheck, now: datetime) -> None:
        with file_transaction(path):
            state = _load_state(path)
            failed = int(state["failed_checks"]) + 1
            state["failed_checks"] = failed
            next_check = now + backoff_delay(failed)
            reset = check.resets_at
            if reset is not None and reset > now:
                state["resets_at"] = _iso(reset)
                next_check = max(reset + RESET_GRACE, now + MIN_CHECK_INTERVAL)
            else:
                state["resets_at"] = None
            state["next_check_at"] = _iso(next_check)
            _save_state(path, state)

    def _clear_cycle(self, path: Path) -> None:
        if not path.exists():
            return
        with file_transaction(path):
            state = _load_state(path)
            if state.get("next_check_at") is None and state.get("resets_at") is None:
                return
            state.update(resets_at=None, next_check_at=None)
            _save_state(path, state)

    # Thread ----------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="enoch-runtime-access-resume", daemon=True,
            )
            self._thread.start()

    def stop(self, timeout_seconds: float = 7) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout_seconds))

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.check_once()
            except StaleDaemonEpoch:
                return
            except Exception as error:
                print(f"Enoch runtime access resume check failed: {type(error).__name__}")
            wait = MONITOR_POLL_SECONDS
            try:
                next_check = self.next_check_at()
            except Exception:
                next_check = None
            if next_check is not None:
                seconds = (next_check - self.clock()).total_seconds()
                wait = max(1.0, min(MONITOR_POLL_SECONDS, seconds))
            self._wake.wait(wait)
