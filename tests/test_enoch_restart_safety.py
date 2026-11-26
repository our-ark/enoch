"""Daemon restarts never kill running tasks.

/update and /restart defer the restart while a task is running and restart once
the queue is idle. A task that a restart still interrupts is paused, resumed
automatically after the restart, and told that it was interrupted.
"""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app import restarts
from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.app.restarts import RestartCoordinator, runtime_killed_by_signal
from enoch.identity import load_identity
from enoch.operations.updater import UpdateResult
from enoch.providers import AgentRuntimeCancelled, AgentRuntimeError, ChatEvent
from enoch.runtime_access import resume_state_path
from enoch.state import StateCorruptionError
from enoch.tasks.events import load_task_events
from enoch.tasks.queue import (
    DAEMON_RESTART_PAUSE_TRIGGER,
    TaskRetryError,
    begin_next_task,
    cancel_running_task,
    cancel_task,
    complete_task,
    enqueue_task,
    retry_failed_task,
    task_queue_status,
)


_ORIGINAL_MAYBE_START = EnochApplication._maybe_start_task_worker
SIGTERM_EXIT = "Claude Code did not answer successfully: Claude Code exited with status 143."


class _Chat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edits.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


def _update_result(*, restart_required: bool = True) -> UpdateResult:
    return UpdateResult(
        message=(
            "Enoch updated to latest main and doctor passed.\n\n"
            "Restarting now. The startup notification will confirm Enoch came back."
        ),
        direct_action_result="Updated repository.",
        restart_required=restart_required,
        previous_revision_id="1111111aaaaaaa",
        revision_id="abcdef0123456789",
        authoritative_name="main",
    )


class _AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target in (
            "enoch.app.core._sync_session_activity",
            "enoch.app.core.log_conversation_turn",
            "enoch.app.core.log_system_event",
            "enoch.app.core.ensure_long_term_memory",
            "enoch.app.core._record_direct_action",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        snapshot = patch(
            "enoch.app.core.EnochApplication._resolve_task_context_snapshot",
            return_value=TaskContextSnapshot(context="brief", source="chat-snapshot"),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        # Tests drive workers by hand; never start a background thread.
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        self.start_worker = worker.start()
        self.addCleanup(worker.stop)
        restart = patch("enoch.app.core._schedule_daemon_restart")
        self.schedule_restart = restart.start()
        self.addCleanup(restart.stop)
        update = patch(
            "enoch.app.core.update_from_authoritative",
            return_value=_update_result(),
        )
        self.update = update.start()
        self.addCleanup(update.stop)
        self.addCleanup(restarts.SHUTDOWN_SIGNALLED.clear)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _Chat()
        self.app = self.new_app()
        self._update_id = 0

    def new_app(self) -> EnochApplication:
        return EnochApplication(load_identity(), self.root, self.chat)

    def send(self, text: str) -> str:
        self._update_id += 1
        before = len(self.chat.sent)
        self.app.handle_event(
            ChatEvent(
                cursor=self._update_id,
                conversation_id=42,
                message_id=1000 + self._update_id,
                text=text,
            )
        )
        return "\n".join(text for _chat_id, text in self.chat.sent[before:])

    def texts(self) -> list[str]:
        return [text for _chat_id, text in self.chat.sent]

    def queue(self, *requests: str) -> None:
        for request in requests:
            self.send(f"/task {request}")

    def start_running(self):
        job = begin_next_task(self.root)
        assert job is not None
        return job

    def maybe_start(self) -> None:
        _ORIGINAL_MAYBE_START(self.app)


class UpdateDefersRestartTests(_AppTestCase):
    def test_update_with_running_task_defers_restart_with_one_line(self) -> None:
        self.queue("long running work", "queued behind it")
        running = self.start_running()

        reply = self.send("/update")

        self.assertEqual(reply, f"Updated to abcdef0; will restart after task #{running.id} finishes.")
        self.update.assert_called_once()
        self.schedule_restart.assert_not_called()
        self.assertTrue(self.app.restarts.deferred)

        # Still running: polling neither restarts nor starts more work.
        self.maybe_start()
        self.schedule_restart.assert_not_called()
        self.assertIsNone(self.app._task_worker)

    def test_deferred_restart_runs_once_the_queue_is_idle(self) -> None:
        self.queue("long running work", "queued behind it")
        running = self.start_running()
        self.send("/update")

        complete_task(running.id, self.root, "done")
        self.maybe_start()
        self.maybe_start()

        self.schedule_restart.assert_called_once_with(self.root)
        # The queued task waits for the restarted daemon.
        self.assertIsNone(self.app._task_worker)
        self.assertEqual([job.id for job in task_queue_status(self.root).pending], [2])

    def test_worker_stops_after_running_task_and_restarts(self) -> None:
        self.queue("first", "second")
        started: list[str] = []

        def run_direct_work(_chat_id, request, **_kwargs):
            started.append(request)
            if len(started) == 1:
                # /update arrives while the first task is running.
                self.assertEqual(
                    self.send("/update"),
                    "Updated to abcdef0; will restart after task #1 finishes.",
                )
                self.schedule_restart.assert_not_called()
            return "done"

        with patch.object(self.app, "_run_direct_work", side_effect=run_direct_work):
            self.app._run_task_worker()

        self.assertEqual(started, ["first"])
        self.schedule_restart.assert_called_once_with(self.root)
        status = task_queue_status(self.root)
        self.assertEqual([job.status for job in status.history], ["completed"])
        self.assertEqual([job.id for job in status.pending], [2])

    def test_live_direct_worker_blocks_restart(self) -> None:
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.app._direct_workers[7] = worker

        reply = self.send("/update")

        self.assertEqual(reply, "Updated to abcdef0; will restart after task #7 finishes.")
        self.maybe_start()
        self.schedule_restart.assert_not_called()
        worker.is_alive.return_value = False
        self.maybe_start()
        self.schedule_restart.assert_called_once_with(self.root)

    def test_update_when_idle_restarts_after_reply_as_before(self) -> None:
        reply = self.send("/update")

        self.assertIn("doctor passed", reply)
        self.assertIn("Restarting now.", reply)
        self.schedule_restart.assert_called_once_with(self.root)
        self.assertFalse(self.app.restarts.deferred)

    def test_update_without_restart_never_defers(self) -> None:
        self.update.return_value = _update_result(restart_required=False)
        self.queue("work")
        self.start_running()

        reply = self.send("/update")

        self.assertIn("doctor passed", reply)
        self.assertFalse(self.app.restarts.pending)
        self.schedule_restart.assert_not_called()

    def test_scheduled_restart_holds_new_work(self) -> None:
        self.send("/update")
        self.schedule_restart.assert_called_once_with(self.root)
        self.queue("arrives after the restart was scheduled")

        self.maybe_start()

        self.assertIsNone(self.app._task_worker)
        self.assertEqual(len(task_queue_status(self.root).pending), 1)


class RestartCommandTests(_AppTestCase):
    def test_restart_with_running_task_defaults_to_wait(self) -> None:
        self.queue("work")
        self.start_running()

        reply = self.send("/restart")

        self.assertEqual(reply, "Enoch will restart after task #1 finishes.")
        self.schedule_restart.assert_not_called()
        self.assertTrue(self.app.restarts.deferred)
        self.maybe_start()
        self.assertIsNone(self.app._task_worker)

    def test_restart_wait_defers_until_idle(self) -> None:
        self.queue("work")
        running = self.start_running()

        reply = self.send("/restart wait")

        self.assertEqual(reply, "Enoch will restart after task #1 finishes.")
        self.schedule_restart.assert_not_called()
        complete_task(running.id, self.root, "done")
        self.maybe_start()
        self.schedule_restart.assert_called_once_with(self.root)

    def test_restart_cancel_drops_a_waiting_restart(self) -> None:
        self.queue("work")
        running = self.start_running()
        self.send("/restart wait")

        self.assertEqual(self.send("/restart cancel"), "Enoch will not restart after the running task.")
        complete_task(running.id, self.root, "done")
        self.maybe_start()
        self.schedule_restart.assert_not_called()
        self.assertEqual(self.send("/restart cancel"), "Enoch has no restart waiting for a task.")

    def test_restart_force_restarts_now_and_says_the_task_pauses(self) -> None:
        self.queue("work")
        self.start_running()

        reply = self.send("/restart force")

        self.assertIn("restarting now", reply)
        self.assertIn("Task #1 will pause and resume automatically", reply)
        self.schedule_restart.assert_called_once_with(self.root)

    def test_restart_when_idle_is_unchanged(self) -> None:
        reply = self.send("/restart")

        self.assertEqual(
            reply,
            "Enoch is restarting.\nDaemon mode will restart after this reply is delivered.",
        )
        self.schedule_restart.assert_called_once_with(self.root)

    def test_restart_rejects_unknown_mode(self) -> None:
        reply = self.send("/restart sometime")

        self.assertIn("/restart wait", reply)
        self.schedule_restart.assert_not_called()


class DurableRestartApplicationTests(_AppTestCase):
    def request(self):
        return json.loads(restarts.restart_state_path(self.root).read_text())["current"]

    def test_status_shows_reason_and_current_blockers_then_cancellation(self) -> None:
        self.queue("work")
        running = self.start_running()
        worker = MagicMock()
        worker.is_alive.return_value = True
        self.app._direct_workers[7] = worker
        self.send("/update")

        status = self.send("/status")
        self.assertIn("Restart: pending (update)", status)
        self.assertIn("Waiting for: tasks #1 and #7", status)
        complete_task(running.id, self.root, "done")
        self.assertIn("Waiting for: task #7", self.send("/status"))
        self.send("/restart cancel")
        self.assertIn("Restart: none pending", self.send("/status"))
        self.assertIsNone(self.request())

    def test_status_shows_scheduled_restart(self) -> None:
        self.send("/restart")
        self.assertIn("Restart: scheduled", self.send("/status"))
        self.assertEqual(self.request()["phase"], "scheduled")

    def test_new_direct_work_is_not_started_during_restart(self) -> None:
        self.send("/restart")
        with patch.object(self.app, "_run_direct_work_with_status") as run:
            reply = self.send("/do new work")
        self.assertIn("waiting to restart", reply)
        self.assertIn("/task <request>", reply)
        run.assert_not_called()

    def test_crash_during_drain_releases_hold_and_resumes_queue(self) -> None:
        self.queue("interrupted", "next")
        self.start_running()
        self.send("/restart")
        self.assertEqual(self.request()["phase"], "waiting")
        previous_epoch = self.request()["epoch_token"]

        self.app = self.new_app()

        self.assertNotEqual(previous_epoch, self.app.daemon_epoch.token)
        self.assertIsNone(self.request())
        self.assertFalse(self.app.restarts.pending)
        self.schedule_restart.assert_not_called()
        started = []

        def run_work(_chat, request, **_kwargs):
            started.append(request)
            return "done"

        with patch.object(self.app, "_run_direct_work", side_effect=run_work):
            self.app._run_task_worker()
        self.assertEqual(started, ["interrupted", "next"])
        self.assertEqual(len(task_queue_status(self.root).history), 2)

    def test_successful_drained_restart_runs_pending_tasks_after_startup(self) -> None:
        self.queue("first", "next")
        running = self.start_running()
        self.send("/update")
        complete_task(running.id, self.root, "done")
        self.maybe_start()
        self.assertEqual(self.request()["phase"], "scheduled")
        self.assertEqual([job.id for job in task_queue_status(self.root).pending], [2])

        self.app = self.new_app()
        self.assertIsNone(self.request())
        with patch.object(self.app, "_run_direct_work", return_value="done") as run:
            self.app._run_task_worker()
        self.assertEqual(run.call_args.args[1], "next")
        self.schedule_restart.assert_called_once()

    def test_failed_startup_preserves_request_for_next_successful_startup(self) -> None:
        self.queue("work")
        self.start_running()
        self.send("/restart")
        saved = self.request()
        with patch.object(
            EnochApplication, "_run_profile_hook", side_effect=RuntimeError("startup failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                self.new_app()
        self.assertEqual(self.request(), saved)
        self.app = self.new_app()
        self.assertIsNone(self.request())
        self.assertFalse(self.app.restarts.pending)

    def test_cancel_releases_queue_without_restart(self) -> None:
        self.queue("work", "next")
        running = self.start_running()
        self.send("/restart")
        self.send("/restart cancel")
        complete_task(running.id, self.root, "done")
        with patch.object(self.app, "_run_direct_work", return_value="done") as run:
            self.app._run_task_worker()
        self.assertEqual(run.call_args.args[1], "next")
        self.assertIsNone(self.request())
        self.schedule_restart.assert_not_called()


class InterruptedTaskTests(_AppTestCase):
    def run_interrupted(self, error: Exception, *, signal_only: bool = False) -> None:
        job = self.start_running()

        def run_direct_work(*_args, **_kwargs):
            if signal_only:
                # The runtime saw SIGTERM; the daemon's handler marked shutdown.
                restarts.SHUTDOWN_SIGNALLED.set()
            else:
                # stop_workers() marks the daemon stopping, then cancels tasks.
                self.app._stopping = True
            raise error

        with patch.object(self.app, "_run_direct_work", side_effect=run_direct_work):
            self.app._run_task_job(job)

    def test_shutdown_cancellation_pauses_instead_of_cancelling(self) -> None:
        self.queue("work")

        self.run_interrupted(AgentRuntimeCancelled("Agent runtime request was cancelled."))

        status = task_queue_status(self.root)
        self.assertEqual(status.history, ())
        self.assertEqual([job.id for job in status.paused], [1])
        paused = status.paused[0]
        self.assertEqual(paused.pause_trigger, DAEMON_RESTART_PAUSE_TRIGGER)
        self.assertIn("daemon restarted", paused.result)
        self.assertIn("resumes automatically after the restart", paused.result)
        self.assertEqual(load_task_events(self.root, task_id=1)[-1].event, "paused")
        self.assertEqual(load_task_events(self.root, task_id=1)[-1].trigger, "daemon-restart")
        # Restart pauses never start the runtime-access back-off cycle.
        self.assertFalse(resume_state_path(self.root).exists())

    def test_runtime_killed_by_restart_signal_pauses_instead_of_failing(self) -> None:
        self.queue("work")

        self.run_interrupted(AgentRuntimeError(SIGTERM_EXIT), signal_only=True)

        status = task_queue_status(self.root)
        self.assertEqual(status.history, ())
        self.assertEqual(status.paused[0].pause_trigger, DAEMON_RESTART_PAUSE_TRIGGER)

    def test_signal_exit_without_shutdown_still_fails(self) -> None:
        self.queue("work")
        job = self.start_running()

        with (
            patch.object(restarts, "SIGNAL_SHUTDOWN_GRACE_SECONDS", 0.01),
            patch.object(self.app, "_run_direct_work", side_effect=AgentRuntimeError(SIGTERM_EXIT)),
        ):
            self.app._run_task_job(job)

        status = task_queue_status(self.root)
        self.assertEqual(status.paused, ())
        self.assertTrue(status.pending or status.history)
        self.assertNotIn(
            DAEMON_RESTART_PAUSE_TRIGGER,
            [event.trigger for event in load_task_events(self.root, task_id=1)],
        )

    def test_paused_task_resumes_after_restart_and_reports_interruption(self) -> None:
        self.queue("work", "next")
        self.run_interrupted(AgentRuntimeCancelled("Agent runtime request was cancelled."))

        self.app = self.new_app()  # the restarted daemon

        status = task_queue_status(self.root)
        self.assertEqual(status.paused, ())
        self.assertEqual([job.id for job in status.pending], [1, 2])
        events = load_task_events(self.root, task_id=1)
        self.assertEqual(events[-1].event, "resumed")
        self.assertEqual(events[-1].trigger, "daemon-restarted")
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].result_summary, "Resumed after the daemon restarted.")

        self.app.run_once()
        self.assertIn("▶️ Resumed task #1 after the restart.", self.texts())
        sent = len(self.chat.sent)
        self.app.run_once()
        self.assertEqual(len(self.chat.sent), sent, "the resume notice is sent once")

        contexts: list[str] = []

        def run_direct_work(*_args, context="", **_kwargs):
            contexts.append(context)
            return "Checked the interrupted step and finished."

        resumed = self.start_running()
        self.assertEqual(resumed.id, 1)
        with patch.object(self.app, "_run_direct_work", side_effect=run_direct_work):
            self.app._run_task_job(resumed)
        self.assertIn("Interrupted attempt:", contexts[0])
        self.assertIn("Do not repeat a step", contexts[0])
        self.assertEqual(task_queue_status(self.root).history[0].status, "completed")

    def test_crashed_running_task_is_paused_and_resumed_after_restart(self) -> None:
        self.queue("work")
        self.start_running()  # the daemon dies with this task running

        self.app = self.new_app()

        status = task_queue_status(self.root)
        self.assertIsNone(status.running)
        self.assertEqual(status.paused, ())
        self.assertEqual([job.id for job in status.pending], [1])
        self.assertEqual(status.pending[0].pause_trigger, DAEMON_RESTART_PAUSE_TRIGGER)
        triggers = [(event.event, event.trigger) for event in load_task_events(self.root, task_id=1)]
        self.assertIn(("paused", "daemon-restart"), triggers)
        self.assertEqual(triggers[-1], ("resumed", "daemon-restarted"))

    def test_interruption_on_last_attempt_fails_retryable(self) -> None:
        self.send("/task work")
        for _attempt in range(2):
            self.run_interrupted(AgentRuntimeCancelled("Agent runtime request was cancelled."))
            self.app._stopping = False
            self.app = self.new_app()

        self.run_interrupted(AgentRuntimeCancelled("Agent runtime request was cancelled."))

        status = task_queue_status(self.root)
        self.assertEqual(status.paused, ())
        failed = status.history[0]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.attempt, failed.max_attempts)
        self.assertEqual(failed.failure_code, "worker_interrupted")
        self.assertTrue(failed.retryable)
        self.assertIn("task retry 1", failed.result)

        self.app._stopping = False
        self.app = self.new_app()
        self.assertEqual(task_queue_status(self.root).pending, (), "never resumed automatically")
        self.send("/task retry 1")
        retry = task_queue_status(self.root).pending[0]
        self.assertEqual(retry.parent_task_id, 1)

    def test_user_stop_still_cancels(self) -> None:
        self.queue("work")
        job = self.start_running()

        def run_direct_work(*_args, **_kwargs):
            raise AgentRuntimeCancelled("Stopped by /stop.")

        with patch.object(self.app, "_run_direct_work", side_effect=run_direct_work):
            self.app._run_task_job(job)

        self.assertEqual(task_queue_status(self.root).history[0].status, "cancelled")


class RetryCancelledTaskTests(_AppTestCase):
    def test_queue_retry_accepts_cancelled_task(self) -> None:
        original = enqueue_task(42, "cancelled before running", self.root)
        cancel_task(original.id, self.root)

        retry = retry_failed_task(original.id, self.root)

        self.assertEqual(retry.parent_task_id, original.id)
        self.assertEqual(retry.text, original.text)
        self.assertEqual(retry.status, "pending")
        with self.assertRaisesRegex(TaskRetryError, f"already has retry task #{retry.id}"):
            retry_failed_task(original.id, self.root)

    def test_task_retry_command_accepts_task_cancelled_by_the_system(self) -> None:
        self.queue("work")
        running = self.start_running()
        cancel_running_task(
            self.root,
            result="Agent runtime request was cancelled.",
            event_actor="system",
            trigger="task-runner-cancelled",
            expected_task_id=running.id,
        )

        reply = self.send(f"/task retry {running.id}")

        self.assertNotIn("could not retry", reply)
        retry = task_queue_status(self.root).pending[0]
        self.assertEqual(retry.parent_task_id, running.id)
        self.assertIn(f"cancelled task #{running.id}", reply)

    def test_retry_of_cancelled_task_gets_cancelled_context(self) -> None:
        self.queue("work")
        running = self.start_running()
        cancel_running_task(self.root, result="stopped", expected_task_id=running.id)
        self.send(f"/task retry {running.id}")
        retry = self.start_running()
        contexts: list[str] = []

        def run_direct_work(*_args, context="", **_kwargs):
            contexts.append(context)
            return "done"

        with patch.object(self.app, "_run_direct_work", side_effect=run_direct_work):
            self.app._run_task_job(retry)

        self.assertIn("Previous cancelled attempt:", contexts[0])

    def test_retry_still_refuses_pending_task(self) -> None:
        queued = enqueue_task(42, "still queued", self.root)

        with self.assertRaisesRegex(TaskRetryError, "not a failed task"):
            retry_failed_task(queued.id, self.root)


class DurableRestartCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.epoch = begin_daemon_epoch(self.root)
        self.busy = (3,)
        self.schedule = MagicMock()
        self.events = MagicMock()
        self.coordinator = self.new_coordinator()

    def new_coordinator(self):
        return RestartCoordinator(
            root=self.root, epoch=self.epoch,
            busy_tasks=lambda: self.busy, schedule=self.schedule, record_event=self.events,
        )

    def request(self):
        return json.loads(restarts.restart_state_path(self.root).read_text())["current"]

    def test_reconstruction_preserves_request_within_same_epoch(self) -> None:
        self.coordinator.defer("update")
        saved = self.request()
        restored = self.new_coordinator()
        restored.complete_startup()
        self.assertTrue(restored.deferred)
        self.assertTrue(restored.restart_if_idle())
        self.assertEqual(self.request(), saved)
        self.assertEqual(saved["epoch_token"], self.epoch.token)
        self.assertTrue(saved["requested_at"])
        self.schedule.assert_not_called()

    def test_new_epoch_clears_old_drain_only_after_successful_startup(self) -> None:
        self.coordinator.defer("update")
        self.epoch = begin_daemon_epoch(self.root)
        restored = self.new_coordinator()
        self.assertTrue(restored.pending)
        self.assertIsNotNone(self.request())
        restored.complete_startup()
        restored.complete_startup()
        self.assertFalse(restored.pending)
        self.assertIsNone(self.request())
        self.assertEqual(
            [call.args[0] for call in self.events.call_args_list],
            ["restart_deferred", "restart_completed"],
        )
        self.schedule.assert_not_called()

    def test_cancel_is_durable(self) -> None:
        self.coordinator.defer("restart")
        self.assertTrue(self.coordinator.cancel())
        self.assertFalse(self.new_coordinator().pending)
        self.assertIsNone(self.request())

    def test_cancel_winning_idle_check_does_not_resurrect_restart(self) -> None:
        self.coordinator.defer("restart")

        def finish_and_cancel():
            self.coordinator.cancel()
            return ()

        with patch.object(self.coordinator, "busy_tasks", side_effect=finish_and_cancel):
            self.assertFalse(self.coordinator.restart_if_idle())
        self.assertIsNone(self.request())
        self.schedule.assert_not_called()

    def test_schedule_is_persisted_before_service_call_and_cleared_after_restart(self) -> None:
        self.schedule.side_effect = lambda: self.assertEqual(self.request()["phase"], "scheduled")
        self.coordinator.restart_now("force")
        self.assertFalse(self.new_coordinator().restart_now("duplicate"))
        self.epoch = begin_daemon_epoch(self.root)
        restored = self.new_coordinator()
        restored.complete_startup()
        self.assertIsNone(self.request())
        self.assertFalse(restored.restart_if_idle())
        self.schedule.assert_called_once()

    def test_schedule_failure_keeps_durable_wait_for_retry(self) -> None:
        self.coordinator.defer("update")
        self.busy = ()
        self.schedule.side_effect = OSError("service unavailable")
        self.assertTrue(self.coordinator.restart_if_idle())
        self.assertEqual(self.request()["phase"], "waiting")
        restored = self.new_coordinator()
        self.assertTrue(restored.deferred)
        self.schedule.side_effect = None
        self.assertTrue(restored.restart_if_idle())
        self.assertEqual(self.request()["phase"], "scheduled")

    def test_failed_persistence_does_not_schedule_or_create_memory_only_hold(self) -> None:
        with patch.object(restarts, "atomic_write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.coordinator.defer("update")
            with self.assertRaisesRegex(OSError, "disk full"):
                self.coordinator.restart_now("force")
        self.assertFalse(self.coordinator.pending)
        self.schedule.assert_not_called()

    def test_stale_daemon_cannot_clear_new_daemon_request(self) -> None:
        self.coordinator.defer("update")
        self.epoch = begin_daemon_epoch(self.root)
        replacement = self.new_coordinator()
        replacement.complete_startup()
        replacement.defer("restart")
        saved = self.request()
        with self.assertRaises(StaleDaemonEpoch):
            self.coordinator.cancel()
        self.assertEqual(self.request(), saved)

    def test_stale_daemon_cannot_schedule_drain(self) -> None:
        self.coordinator.defer("update")
        self.epoch = begin_daemon_epoch(self.root)
        self.busy = ()
        with self.assertRaises(StaleDaemonEpoch):
            self.coordinator.restart_if_idle()
        self.schedule.assert_not_called()

    def test_epoch_change_between_persistence_and_service_call_is_fenced(self) -> None:
        save = self.coordinator._set_request

        def save_then_replace(request):
            save(request)
            begin_daemon_epoch(self.root)

        with patch.object(self.coordinator, "_set_request", side_effect=save_then_replace):
            with self.assertRaises(StaleDaemonEpoch):
                self.coordinator.restart_now("restart")
        self.schedule.assert_not_called()

    def test_invalid_state_is_not_silently_discarded(self) -> None:
        path = restarts.restart_state_path(self.root)
        for raw in ('{"schema_version": 2}', '{"current": {"phase": "waiting"}}', 'not json'):
            with self.subTest(raw=raw):
                path.write_text(raw)
                with self.assertRaises(StateCorruptionError):
                    self.new_coordinator()
                self.assertEqual(path.read_text(), raw)


class RestartCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.busy: tuple[int, ...] = (3,)
        self.events: list[str] = []
        self.schedule = MagicMock()
        self.coordinator = RestartCoordinator(
            busy_tasks=lambda: self.busy,
            schedule=self.schedule,
            record_event=lambda event, **_kwargs: self.events.append(event),
        )

    def test_defer_is_idempotent_and_restarts_once(self) -> None:
        self.assertEqual(self.coordinator.defer("update"), (3,))
        self.assertEqual(self.coordinator.defer("update"), (3,))
        self.assertTrue(self.coordinator.restart_if_idle())
        self.schedule.assert_not_called()

        self.busy = ()
        self.assertTrue(self.coordinator.restart_if_idle())
        self.assertTrue(self.coordinator.restart_if_idle())

        self.schedule.assert_called_once_with()
        self.assertEqual(self.events, ["restart_deferred", "restart_scheduled"])

    def test_defer_when_idle_leaves_the_restart_to_the_caller(self) -> None:
        self.busy = ()

        self.assertEqual(self.coordinator.defer("update"), ())
        self.assertFalse(self.coordinator.pending)
        self.assertFalse(self.coordinator.restart_if_idle())

    def test_failed_schedule_is_retried_on_the_next_poll(self) -> None:
        self.coordinator.defer("update")
        self.busy = ()
        self.schedule.side_effect = [OSError("service manager unavailable"), None]

        self.assertTrue(self.coordinator.restart_if_idle())
        self.assertTrue(self.coordinator.deferred)
        self.assertTrue(self.coordinator.restart_if_idle())

        self.assertEqual(self.schedule.call_count, 2)
        self.assertFalse(self.coordinator.deferred)
        self.assertTrue(self.coordinator.pending)

    def test_signal_exit_detection(self) -> None:
        self.assertTrue(runtime_killed_by_signal(SIGTERM_EXIT))
        self.assertTrue(runtime_killed_by_signal("process exited with code -9"))
        self.assertFalse(runtime_killed_by_signal("Claude Code exited with status 1."))
        self.assertFalse(runtime_killed_by_signal("tests failed: 143 passed, 2 failed"))


if __name__ == "__main__":
    unittest.main()
