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

import re
import threading
from typing import Callable, Sequence

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
    "": "ask",
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


def restart_busy_message(
    display_name: str,
    task_ids: Sequence[int],
    *,
    command_prefix: str = "/",
) -> str:
    tasks = format_task_ids(task_ids)
    verb = "is" if len(task_ids) == 1 else "are"
    return "\n".join(
        [
            f"{display_name} did not restart: {tasks} {verb} still running.",
            f"{command_prefix}restart wait - restart as soon as {tasks} {_finishes(task_ids)}.",
            (
                f"{command_prefix}restart force - restart now; {tasks} "
                "pauses and resumes automatically after the restart."
            ),
        ]
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
            f"{prefix}restart - restart now when no task is running; otherwise ask first",
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
    ) -> None:
        self._busy_tasks = busy_tasks
        self._schedule = schedule
        self._record_event = record_event
        self._lock = threading.Lock()
        self._deferred_reason = ""
        self._scheduled = False

    @property
    def deferred(self) -> bool:
        with self._lock:
            return bool(self._deferred_reason) and not self._scheduled

    @property
    def pending(self) -> bool:
        """True while a restart is deferred or already scheduled; hold new work."""

        with self._lock:
            return bool(self._deferred_reason) or self._scheduled

    def busy_tasks(self) -> tuple[int, ...]:
        return tuple(self._busy_tasks())

    def defer(self, reason: str) -> tuple[int, ...]:
        """Restart once idle. Return the task ids it waits for; empty means idle now."""

        blockers = self.busy_tasks()
        if not blockers:
            return ()
        with self._lock:
            first = not self._deferred_reason and not self._scheduled
            if not self._scheduled and not self._deferred_reason:
                self._deferred_reason = reason
        if first:
            self._record_event(
                "restart_deferred",
                details={"reason": reason, "waiting_for": list(blockers)},
            )
        return blockers

    def restart_now(self, reason: str) -> bool:
        """Schedule the restart immediately; False when one is already scheduled."""

        with self._lock:
            if self._scheduled:
                return False
            self._scheduled = True
            self._deferred_reason = ""
        try:
            self._schedule()
        except BaseException:
            with self._lock:
                self._scheduled = False
            raise
        self._record_event("restart_scheduled", details={"reason": reason})
        return True

    def restart_if_idle(self) -> bool:
        """Start a deferred restart once idle. Return True while new work must wait."""

        with self._lock:
            if self._scheduled:
                return True
            reason = self._deferred_reason
        if not reason:
            return False
        if self.busy_tasks():
            return True
        try:
            self.restart_now(reason)
        except Exception as error:  # noqa: BLE001 - the next poll retries
            with self._lock:
                if not self._scheduled and not self._deferred_reason:
                    self._deferred_reason = reason
            self._record_event(
                "restart_failed",
                status="failed",
                details={"reason": reason, "error": str(error)},
            )
        return True

    def cancel(self) -> bool:
        """Drop a deferred restart. False when none is waiting."""

        with self._lock:
            if not self._deferred_reason or self._scheduled:
                return False
            reason = self._deferred_reason
            self._deferred_reason = ""
        self._record_event("restart_deferral_cancelled", details={"reason": reason})
        return True
