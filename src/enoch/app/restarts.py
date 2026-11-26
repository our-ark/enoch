"""Daemon restarts that never kill running work.

Restarting the daemon ends every worker it owns, so a restart is the one path
that can lose a task halfway through. This module keeps that from happening:

1. ``RestartCoordinator`` is the single path for restarts requested by the
   daemon itself (``/update``, ``/restart`` and automatic updates). While a task
   is running, or a direct worker is alive, the restart is deferred; new work
   is held and the restart starts as soon as the queue is idle.
2. When a restart still interrupts a task (a crash, a manual kill, or
   ``/restart force``), the task is paused rather than cancelled or failed and
   resumes automatically once the daemon is back. The resumed run is told it
   was interrupted, so the interrupted step is reported, never silently
   repeated.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
import threading
from typing import Callable, Sequence

from enoch.app.epoch import DaemonEpoch, StaleDaemonEpoch, daemon_epoch_guard
from enoch.memory.paths import now as current_time
from enoch.paths import private_state_path
from enoch.state import StateCorruptionError, atomic_write, file_transaction, load_json_object
from enoch.tasks.queue import TaskJob


# Set by the daemon's SIGTERM/SIGINT handler. The runtime child can see the
# same signal before the daemon's own handler runs, so a task that died from a
# signal waits briefly for this before deciding it was not a restart.
SHUTDOWN_SIGNALLED = threading.Event()
SIGNAL_SHUTDOWN_GRACE_SECONDS = 2.0
RESTARTED_RESUME_TRIGGER = "daemon-restarted"

_SIGNAL_EXIT = re.compile(
    r"(?:status|code)\s*:?\s*(?:143|137|-15|-9)\b"
    r"|\bSIG(?:TERM|KILL)\b"
    r"|\b(?:killed|terminated) by signal\b",
    re.IGNORECASE,
)

RESTART_MODES = {
    "": "wait",
    "wait": "wait",
    "force": "force",
    "now": "force",
    "cancel": "cancel",
}


def runtime_killed_by_signal(text: str) -> bool:
    """Return whether a runtime failure says its process was killed by a signal."""

    return bool(_SIGNAL_EXIT.search(str(text or "")))


def shutdown_interrupted(
    stopping: Callable[[], bool],
    failure_text: str,
    *,
    grace_seconds: float | None = None,
) -> bool:
    """Return whether a task that just stopped was interrupted by daemon shutdown."""

    if stopping() or SHUTDOWN_SIGNALLED.is_set():
        return True
    if not runtime_killed_by_signal(failure_text):
        return False
    SHUTDOWN_SIGNALLED.wait(
        SIGNAL_SHUTDOWN_GRACE_SECONDS if grace_seconds is None else grace_seconds
    )
    return stopping() or SHUTDOWN_SIGNALLED.is_set()


def format_task_ids(task_ids: Sequence[int]) -> str:
    ids = [f"#{task_id}" for task_id in task_ids]
    if len(ids) == 1:
        return f"task {ids[0]}"
    return "tasks " + ", ".join(ids[:-1]) + f" and {ids[-1]}"


def _finishes(task_ids: Sequence[int]) -> str:
    return "finishes" if len(task_ids) == 1 else "finish"


def deferred_update_message(revision_id: str, task_ids: Sequence[int]) -> str:
    revision = revision_id.strip()[:7]
    updated = f"Updated to {revision}" if revision else "Updated"
    return f"{updated}; will restart after {format_task_ids(task_ids)} {_finishes(task_ids)}."


def deferred_restart_message(display_name: str, task_ids: Sequence[int]) -> str:
    return (
        f"{display_name} will restart after {format_task_ids(task_ids)} "
        f"{_finishes(task_ids)}."
    )


def forced_restart_message(display_name: str, task_ids: Sequence[int]) -> str:
    return "\n".join(
        [
            f"{display_name} is restarting now.",
            (
                f"{format_task_ids(task_ids).capitalize()} will pause and resume "
                "automatically after the restart."
            ),
        ]
    )


def restart_usage(prefix: str = "/") -> str:
    return "\n".join(
        [
            "Restart commands:",
            f"{prefix}restart - restart now when idle; otherwise wait for running work",
            f"{prefix}restart wait - restart as soon as the running task finishes",
            (
                f"{prefix}restart force - restart now; a running task pauses and "
                "resumes automatically after the restart"
            ),
            f"{prefix}restart cancel - drop a restart that is waiting for a task",
        ]
    )


def restart_pause_message(task_id: int, interrupted_step: str = "") -> str:
    lines = [
        f"Task #{task_id} was paused because the daemon restarted while it was running.",
    ]
    step = " ".join(interrupted_step.split())
    if step:
        lines.append(f"Interrupted during: {step}")
    lines.append(
        "It resumes automatically after the restart and checks what the "
        "interrupted step already did instead of repeating it."
    )
    return "\n".join(lines)


def restart_exhausted_message(job: TaskJob, *, command_prefix: str = "/") -> str:
    return "\n".join(
        [
            (
                f"Task #{job.id} was interrupted by a daemon restart on attempt "
                f"{job.attempt}/{job.max_attempts}, so it is not resumed automatically."
            ),
            (
                f"Use {command_prefix}task retry {job.id} to run it again as a new "
                "linked task."
            ),
        ]
    )


def restart_resumed_notice(jobs: Sequence[TaskJob]) -> str:
    ids = [job.id for job in jobs]
    return f"▶️ Resumed {format_task_ids(ids)} after the restart."


def interrupted_resume_context(job: TaskJob) -> str:
    """Tell a resumed run that an earlier attempt was cut off by a restart."""

    return "\n\n".join(
        [
            "Interrupted attempt:",
            (
                "A daemon restart interrupted an earlier attempt of this task. This run "
                "resumes it in the same task worktree. Steps from that attempt may "
                "already have happened: files edited, commits made, branches pushed, "
                "pull requests opened or updated, comments posted."
            ),
            (
                "Before changing anything, inspect the current state (working tree, "
                "commits, remote branch, open pull requests). Do not repeat a step "
                "that may already have completed. In the final reply, report which "
                "step was interrupted and what you found, then finish the remaining work."
            ),
        ]
    )


@dataclass(frozen=True)
class RestartRequest:
    reason: str
    phase: str
    epoch_token: str
    requested_at: str


def restart_state_path(root: Path | None = None) -> Path:
    return private_state_path("restart.json", root)


def _load_request(path: Path) -> RestartRequest | None:
    data = load_json_object(path)
    if data.get("schema_version", 1) != 1:
        raise StateCorruptionError(path, "unsupported restart schema version")
    request = data.get("current")
    if request is None:
        return None
    fields = ("reason", "phase", "epoch_token", "requested_at")
    if (
        not isinstance(request, dict)
        or any(not isinstance(request.get(key), str) or not request[key].strip() for key in fields)
        or request["phase"] not in {"waiting", "scheduled"}
    ):
        raise StateCorruptionError(path, "invalid restart request")
    return RestartRequest(**{key: request[key] for key in fields})


class RestartCoordinator:
    """Single path for daemon restarts: never while a task is running.

    ``busy_tasks`` returns the ids of running tasks and live direct workers,
    ``schedule`` asks the service manager to restart the daemon and
    ``record_event`` writes a system event.
    """

    def __init__(
        self,
        *,
        busy_tasks: Callable[[], tuple[int, ...]],
        schedule: Callable[[], None],
        record_event: Callable[..., None],
        root: Path | None = None,
        epoch: DaemonEpoch | None = None,
    ) -> None:
        if root is not None and epoch is None:
            raise ValueError("Persistent restart coordination requires a daemon epoch.")
        self._busy_tasks = busy_tasks
        self._schedule = schedule
        self._record_event = record_event
        self._lock = threading.Lock()
        self._root = root
        self._epoch = epoch
        self._request: RestartRequest | None = None
        if epoch is not None:
            with daemon_epoch_guard(epoch, root), file_transaction(restart_state_path(root)):
                self._request = _load_request(restart_state_path(root))

    def _set_request(self, request: RestartRequest | None) -> None:
        # Persist before changing admission or invoking the service manager.
        if self._epoch is not None:
            path = restart_state_path(self._root)
            with daemon_epoch_guard(self._epoch, self._root), file_transaction(path):
                atomic_write(path, json.dumps({
                    "schema_version": 1,
                    "current": asdict(request) if request is not None else None,
                }, indent=2, sort_keys=True) + "\n")
        self._request = request

    def complete_startup(self) -> None:
        """A successfully initialized new daemon satisfies the old restart request.

        This includes a crash during drain: the replacement already loaded the
        code on disk. Holding its recovered tasks would cause a restart loop.
        Reconstructing a coordinator within the same epoch does not clear it.
        """

        with self._lock:
            request = self._request
            if request is None or self._epoch is None or request.epoch_token == self._epoch.token:
                return
            self._set_request(None)
        self._record_event("restart_completed", details={
            "reason": request.reason,
            "previous_phase": request.phase,
            "requested_at": request.requested_at,
        })

    @property
    def deferred(self) -> bool:
        with self._lock:
            return self._request is not None and self._request.phase == "waiting"

    @property
    def pending(self) -> bool:
        """True while a restart is deferred or already scheduled; hold new work."""

        with self._lock:
            return self._request is not None

    def status_message(self) -> str:
        with self._lock:
            request = self._request
        if request is None:
            return "Restart: none pending."
        if request.phase == "scheduled":
            return f"Restart: scheduled ({request.reason}); waiting for the daemon to restart."
        blockers = self.busy_tasks()
        waiting_for = format_task_ids(blockers) if blockers else "restart scheduling"
        return (
            f"Restart: pending ({request.reason}).\n"
            f"Waiting for: {waiting_for}.\n"
            "New tasks will start after the restart."
        )

    def busy_tasks(self) -> tuple[int, ...]:
        return tuple(self._busy_tasks())

    def defer(self, reason: str) -> tuple[int, ...]:
        """Restart once idle. Return the task ids it waits for; empty means idle now."""

        blockers = self.busy_tasks()
        if not blockers:
            return ()
        with self._lock:
            first = self._request is None
            if first:
                self._set_request(RestartRequest(
                    reason=reason,
                    phase="waiting",
                    epoch_token=self._epoch.token if self._epoch is not None else "",
                    requested_at=current_time(),
                ))
        if first:
            self._record_event(
                "restart_deferred",
                details={"reason": reason, "waiting_for": list(blockers)},
            )
        return blockers

    def restart_now(self, reason: str) -> bool:
        """Schedule the restart immediately; False when one is already scheduled."""

        return self._schedule_restart(reason)

    def _schedule_restart(self, reason: str, *, expected: RestartRequest | None = None) -> bool:
        with self._lock:
            # Cancellation may win while restart_if_idle checks the workers.
            if expected is not None and self._request is not expected:
                return False
            if self._request is not None and self._request.phase == "scheduled":
                return False
            previous = self._request
            self._set_request(
                replace(previous, phase="scheduled") if previous is not None else RestartRequest(
                    reason=reason,
                    phase="scheduled",
                    epoch_token=self._epoch.token if self._epoch is not None else "",
                    requested_at=current_time(),
                )
            )
        try:
            if self._epoch is None:
                self._schedule()
            else:
                with daemon_epoch_guard(self._epoch, self._root):
                    self._schedule()
        except BaseException:
            with self._lock:
                self._set_request(previous)
            raise
        self._record_event("restart_scheduled", details={"reason": reason})
        return True

    def restart_if_idle(self) -> bool:
        """Start a deferred restart once idle. Return True while new work must wait."""

        with self._lock:
            request = self._request
            if request is not None and request.phase == "scheduled":
                return True
        if request is None:
            return False
        if self.busy_tasks():
            return True
        try:
            self._schedule_restart(request.reason, expected=request)
        except StaleDaemonEpoch:
            raise
        except Exception as error:  # noqa: BLE001 - the next poll retries
            self._record_event(
                "restart_failed",
                status="failed",
                details={"reason": request.reason, "error": str(error)},
            )
        return self.pending

    def cancel(self) -> bool:
        """Drop a deferred restart. False when none is waiting."""

        with self._lock:
            if self._request is None or self._request.phase == "scheduled":
                return False
            reason = self._request.reason
            self._set_request(None)
        self._record_event("restart_deferral_cancelled", details={"reason": reason})
        return True
