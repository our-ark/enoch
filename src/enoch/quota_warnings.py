"""Background, durable threshold warnings for the account windows in /quota."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Callable, Mapping
from uuid import uuid4

from enoch.app.epoch import StaleDaemonEpoch
from enoch.app.notifications import DELIVERED, TERMINAL_FAILURE, notification_record
from enoch.config import read_section
from enoch.paths import private_state_path
from enoch.quota import _number, _timestamp, format_quota, quota_provider_label
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object


THRESHOLDS = (10, 5, 1)
NOTIFICATION_PREFIX = "quota-warning:"
DEFAULT_POLL_SECONDS = 60
RESET_TOLERANCE_SECONDS = 60


@dataclass(frozen=True)
class QuotaWarningSettings:
    enabled: bool = True
    poll_interval_seconds: int = DEFAULT_POLL_SECONDS


def warning_settings(root: Path) -> QuotaWarningSettings:
    settings = read_section("quota", root)
    enabled = settings.get("warnings_enabled", "true").strip().lower() not in {"false", "off", "no", "0"}
    try:
        interval = int(settings.get("poll_interval_seconds", str(DEFAULT_POLL_SECONDS)))
    except ValueError:
        interval = DEFAULT_POLL_SECONDS
    if not 60 <= interval <= 3600:
        interval = DEFAULT_POLL_SECONDS
    return QuotaWarningSettings(enabled, interval)


def warning_state_path(root: Path) -> Path:
    return private_state_path("quota_warnings.json", root)


def _load_state(path: Path) -> dict:
    data = load_json_object(path, default_factory=lambda: {"schema_version": 1, "windows": {}})
    if data.get("schema_version") != 1 or not isinstance(data.get("windows"), dict):
        raise StateCorruptionError(path, "invalid quota warning state")
    for item in data["windows"].values():
        if (not isinstance(item, dict) or not isinstance(item.get("cycle"), str)
                or item.get("notified") not in (None, *THRESHOLDS)
                or (item.get("pending") is not None and not isinstance(item["pending"], dict))):
            raise StateCorruptionError(path, "invalid quota window state")
    return data


def prepare_warnings(
    root: Path, provider: str, snapshot: Mapping, *, channel: str, conversation_id,
    display_name: str, now: datetime,
) -> list[dict]:
    """Persist immutable notification intents before sending; coalesce crossed tiers."""
    if snapshot.get("error"):
        return []
    windows = snapshot.get("windows", ())
    if not isinstance(windows, (list, tuple)):
        return []
    path = warning_state_path(root)
    with file_transaction(path):
        data = _load_state(path)
        intents = []
        seen = set()
        for window in windows:
            if not isinstance(window, Mapping):
                continue
            used = _number(window.get("used_percent"))
            if used is None or used < 0:
                continue
            reset = _timestamp(window.get("resets_at"))
            if reset is not None and reset <= now:
                continue  # Never warn from a window the provider has already expired.
            remaining = max(0, min(100, 100 - used))
            reset_at = int(reset.timestamp()) if reset else None
            window_id = window.get("id") or window.get("label")
            if not isinstance(window_id, str) or not window_id.strip():
                continue
            key = hashlib.sha256(json.dumps(
                [channel, conversation_id, provider, window_id], separators=(",", ":"),
            ).encode()).hexdigest()[:32]
            if key in seen:
                continue
            seen.add(key)
            previous = data["windows"].get(key)
            entry = previous or {"cycle": uuid4().hex, "notified": None, "pending": None, "reset_at": reset_at}
            if pending := entry.get("pending"):
                # Reconcile the crash gap between durable delivery and our state update.
                receipt = notification_record(channel, pending["key"], root)
                if receipt is not None and receipt.status == DELIVERED:
                    entry["notified"] = pending["threshold"]
                    entry["pending"] = None
                elif receipt is not None and receipt.status == TERMINAL_FAILURE:
                    pending["terminal"] = True
            old_reset = entry.get("reset_at")
            new_cycle = False
            if reset_at is not None and old_reset is not None:
                # Ignore subsecond/jitter changes; allow an explicit early quota reset.
                new_cycle = (reset_at - old_reset > RESET_TOLERANCE_SECONDS
                             or (reset_at != old_reset and old_reset <= now.timestamp()))
            elif reset_at is None and old_reset is None:
                # With no reset metadata, a recovery above 10% is the only rearm signal.
                last = entry.get("remaining")
                new_cycle = last is not None and last <= 10 < remaining
            elif reset_at is None and old_reset <= now.timestamp() and remaining > 10:
                new_cycle = True  # Fresh recovery after expiry, even without new reset metadata.
            if new_cycle:
                entry = {"cycle": uuid4().hex, "notified": None, "pending": None, "reset_at": reset_at}
            elif old_reset is None and reset_at is not None:
                entry["reset_at"] = reset_at
            entry["remaining"] = remaining
            entry["last_seen_at"] = now.isoformat()
            data["windows"][key] = entry
            tier = min((threshold for threshold in THRESHOLDS if remaining <= threshold), default=None)
            if tier is None:
                entry["pending"] = None
                continue
            if reset_at is None and old_reset is not None and old_reset <= now.timestamp():
                continue  # A temporarily missing timestamp cannot prove a new window.
            if entry["notified"] is not None and tier >= entry["notified"]:
                continue
            pending = entry.get("pending")
            if pending is not None and tier > pending["threshold"]:
                continue  # Do not retry an urgent warning after usage has recovered.
            if pending is None or pending.get("threshold") != tier:
                report = format_quota(quota_provider_label(provider), {
                    "plan": snapshot.get("plan"), "source": snapshot.get("source", "account quota"),
                    "windows": [window],
                }, now=now)
                pending = {
                    "key": f"{NOTIFICATION_PREFIX}{key}:{entry['cycle']}:{tier}",
                    "threshold": tier,
                    "text": f"⚠️ {display_name} quota warning (≤{tier}% remaining)\n{report}",
                    "terminal": False,
                }
                entry["pending"] = pending
            if not pending.get("terminal"):
                intents.append({"window_key": key, "conversation_id": conversation_id, **pending})
        data["last_checked_at"] = now.isoformat()
        atomic_write(path, json.dumps(data, indent=2) + "\n")
        return intents


def record_warning_result(root: Path, intent: dict, *, delivered: bool, terminal: bool) -> None:
    path = warning_state_path(root)
    with file_transaction(path):
        data = _load_state(path)
        entry = data["windows"].get(intent["window_key"])
        if not entry or not entry.get("pending") or entry["pending"]["key"] != intent["key"]:
            return
        if delivered:
            entry["notified"] = intent["threshold"]
            entry["pending"] = None
        elif terminal:
            entry["pending"]["terminal"] = True
        atomic_write(path, json.dumps(data, indent=2) + "\n")


class QuotaWarningMonitor:
    """One cancellable worker, separate from chat polling and the task scheduler."""

    def __init__(self, root: Path, *, channel: str, display_name: str,
                 collect: Callable, destination: Callable, deliver: Callable,
                 require_current: Callable, guard: Callable) -> None:
        self.root = root
        self.channel = channel
        self.display_name = display_name
        self.collect = collect
        self.destination = destination
        self.deliver = deliver
        self.require_current = require_current
        self.guard = guard
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="enoch-quota-warnings", daemon=True)
            self._thread.start()

    def stop(self, timeout_seconds: float = 7) -> None:
        self._stop.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0, timeout_seconds))

    def check_once(self, *, now: datetime | None = None) -> int:
        self.require_current()
        conversation_id = self.destination()
        if self._stop.is_set() or conversation_id is None or not warning_settings(self.root).enabled:
            return 0
        count = 0
        for provider, snapshot in self.collect():
            if (self._stop.is_set() or self.destination() != conversation_id
                    or not warning_settings(self.root).enabled):
                break
            current = now or datetime.now(timezone.utc)
            intents = self.guard(prepare_warnings, self.root, provider, snapshot,
                                 channel=self.channel, conversation_id=conversation_id,
                                 display_name=self.display_name, now=current)
            for intent in intents:
                if (self._stop.is_set() or self.destination() != conversation_id
                        or not warning_settings(self.root).enabled):
                    return count
                self.require_current()
                result = self.deliver(conversation_id, intent["text"], notification_key=intent["key"])
                self.guard(record_warning_result, self.root, intent,
                           delivered=result.delivered, terminal=result.terminal)
                count += int(result.delivered)
        return count

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except StaleDaemonEpoch:
                return
            except Exception:
                # Account/transport errors must not leak secrets or flood the user's chat.
                print("Quota warning check failed; will retry at the next interval.")
            try:
                interval = warning_settings(self.root).poll_interval_seconds
            except Exception:
                interval = DEFAULT_POLL_SECONDS
            self._stop.wait(interval)
