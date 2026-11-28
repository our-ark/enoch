"""Automatic /update after a merge the human asked for.

A review Enoch lands into the authoritative branch of its own repository marks
an update pending (``/pr merge`` and the pending-merge watcher alike). The
daemon loop then runs the /update flow once no task is running: update, doctor,
and restart through the shared restart coordinator only when doctor passed.
Update, doctor and restart internals are mocked.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.auto_update import (
    LandedReview,
    auto_update_path,
    clear_update_pending,
    load_auto_update_state,
    mark_update_pending,
)
from enoch.app.core import CI_REPAIR_TRIGGER, EnochApplication, TaskContextSnapshot
from enoch.backlog import add_backlog_item, next_backlog_item
from enoch.commands import config_status, help_message
from enoch.identity import load_identity
from enoch.immune import DoctorCheckResult, DoctorDiagnosis, ImmuneResult
from enoch.operations import updater
from enoch.operations.updater import UpdateResult
from enoch.tasks.config import auto_update_enabled, save_auto_update
from enoch.tasks.queue import (
    begin_next_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    task_queue_status,
)
from our_ark_provider_kit import RepositoryRevision, ReviewCheck, WorkingCopyState
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_pending_merges import START, URL, _Forge, _review


# The real worker starter, before setUp replaces it with a mock.
_START_WORKER = EnochApplication._maybe_start_task_worker
FOREIGN_URL = "https://github.com/someone-else/project/pull/7"


def _updated(revision: str = "abcdef0123456789") -> UpdateResult:
    return UpdateResult(
        message="Enoch updated to latest main and doctor passed.",
        direct_action_result="Updated repository.",
        restart_required=True,
        previous_revision_id="1111111aaaaaaa",
        revision_id=revision,
        authoritative_name="main",
    )


def _doctor_failed() -> UpdateResult:
    return UpdateResult(
        message="Enoch updated to latest main, but doctor failed. I am not restarting.",
        direct_action_result="",
        previous_revision_id="1111111aaaaaaa",
        authoritative_name="main",
        doctor_failed_checks=("tests",),
    )


class _AutoUpdateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Tests drive workers by hand; never start a background thread.
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        restart = patch("enoch.app.core._schedule_daemon_restart")
        self.schedule_restart = restart.start()
        self.addCleanup(restart.stop)
        update = patch("enoch.app.core.update_from_authoritative", return_value=_updated())
        self.update = update.start()
        self.addCleanup(update.stop)
        record = patch("enoch.app.core._record_direct_action")
        record.start()
        self.addCleanup(record.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.chat = _Chat()
        self.forge = _Forge(checks=(ReviewCheck("CI / test", "success"),))

    def app(self, forge: _Forge | None = None) -> EnochApplication:
        app = EnochApplication(
            load_identity(),
            self.root,
            self.chat,
            runtime=_Runtime(),
            review=_review(forge or self.forge),
        )
        app.pending_merges._now = lambda: START
        app.pending_merges.interval_seconds = 0
        app._authoritative_repository = lambda: "our-ark/enoch"
        app._authoritative_branch_name = lambda: "main"
        app._resolve_task_context_snapshot = MagicMock(
            return_value=TaskContextSnapshot(context="", source="chat-snapshot")
        )
        app._compact_task_messages = lambda: True
        return app

    def pending(self) -> tuple[str, ...]:
        return load_auto_update_state(self.root).pending

    def texts(self) -> list[str]:
        return [text for _chat, text in self.chat.sent]

    def running_task(self):
        enqueue_task("room-1", "long running work", self.root)
        job = begin_next_task(self.root)
        assert job is not None
        return job

    def worker_starts(self, app: EnochApplication) -> bool:
        app._run_task_worker = MagicMock()
        app._task_worker = None
        _START_WORKER(app)
        if app._task_worker is None:
            return False
        app._task_worker.join(5)
        return app._run_task_worker.called


class MergeMarksUpdatePendingTests(_AutoUpdateTestCase):
    def test_direct_pr_merge_marks_update_pending(self) -> None:
        app = self.app()

        reply = app._pr("room-1", "merge 91")

        self.assertIn(": landed.", reply)
        self.assertEqual(self.pending(), ("#91",))
        # Marking never runs the update inline.
        self.update.assert_not_called()

    def test_watcher_landing_marks_update_pending(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self.app(forge)
        app._pr("room-1", "merge 91")
        self.assertEqual(self.pending(), ())

        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()

        self.assertIn("✅ Merged #91.", self.texts())
        self.assertEqual(self.pending(), ("#91",))

    def test_pr_auto_merge_policy_landing_marks_update_pending(self) -> None:
        app = self.app()
        job = enqueue_task("room-1", "build it", self.root, merge_policy="auto")
        begin_next_task(self.root)
        job = complete_task(job.id, self.root, f"Opened {URL}", review_urls=(URL,))

        app._apply_merge_policy(job)

        self.assertEqual(self.pending(), ("#91",))

    def test_already_merged_review_does_not_mark(self) -> None:
        forge = _Forge()
        forge.set(state="MERGED", merged_at="2026-11-24T19:00:00Z", merge_commit="m")
        app = self.app(forge)

        app._pr("room-1", "merge 91")

        self.assertEqual(self.pending(), ())

    def test_merges_into_other_repositories_are_ignored(self) -> None:
        forge = _Forge(checks=(ReviewCheck("CI / test", "success"),))
        forge.set(url=FOREIGN_URL, repository="someone-else/project", number=7)
        app = self.app(forge)

        reply = app._pr("room-1", f"merge {FOREIGN_URL}")

        self.assertIn(": landed.", reply)
        self.assertEqual(self.pending(), ())
        app._maybe_run_auto_update()
        self.update.assert_not_called()

    def test_merges_into_another_branch_are_ignored(self) -> None:
        app = self.app()

        app._after_requested_merge(
            LandedReview("#91", URL, "our-ark/enoch", "release/v0.7.0")
        )

        self.assertEqual(self.pending(), ())

    def test_unknown_authoritative_repository_is_ignored(self) -> None:
        app = self.app()
        app._authoritative_repository = lambda: ""

        app._after_requested_merge(LandedReview("#91", URL, "our-ark/enoch", "main"))

        self.assertEqual(self.pending(), ())


class IdleGateTests(_AutoUpdateTestCase):
    def test_no_update_or_restart_while_a_task_runs(self) -> None:
        app = self.app()
        self.running_task()
        app._pr("room-1", "merge 91")

        app._maybe_run_auto_update()
        app._maybe_run_auto_update()

        self.update.assert_not_called()
        self.schedule_restart.assert_not_called()
        self.assertEqual(self.pending(), ("#91",))

    def test_runs_update_and_restarts_once_idle(self) -> None:
        app = self.app()
        running = self.running_task()
        app._pr("room-1", "merge 91")
        app._maybe_run_auto_update()

        complete_task(running.id, self.root, "done")
        app._maybe_run_auto_update()
        app._maybe_run_auto_update()

        self.update.assert_called_once()
        self.schedule_restart.assert_called_once_with(self.root)
        self.assertEqual(self.pending(), ())
        # A successful update sends nothing; the startup message reports it.
        self.assertEqual(self.texts(), [])

    def test_live_direct_worker_blocks_the_update(self) -> None:
        app = self.app()
        app._pr("room-1", "merge 91")
        worker = MagicMock()
        worker.is_alive.return_value = True
        app._direct_workers[7] = worker

        app._maybe_run_auto_update()

        self.update.assert_not_called()

    def test_pending_update_holds_new_tasks_until_it_runs(self) -> None:
        app = self.app()
        app._pr("room-1", "merge 91")
        enqueue_task("room-1", "next feature", self.root)

        self.assertFalse(self.worker_starts(app))

        self.update.return_value = _doctor_failed()
        app._maybe_run_auto_update()
        # Doctor failed: no restart is coming, so the queue continues.
        self.assertTrue(self.worker_starts(app))

    def test_worker_loop_stops_before_the_next_task(self) -> None:
        app = self.app()
        enqueue_task("room-1", "first", self.root)
        enqueue_task("room-1", "second", self.root)
        ran: list[str] = []

        def run(job):
            ran.append(job.text)
            # The task's PR lands while it runs.
            app._after_requested_merge(LandedReview("#91", URL, "our-ark/enoch", "main"))
            complete_task(job.id, self.root, "done")

        app._run_task_job = run
        app._run_task_worker()

        self.assertEqual(ran, ["first"])
        app._maybe_run_auto_update()
        self.update.assert_called_once()
        self.schedule_restart.assert_called_once_with(self.root)

    def test_run_once_runs_the_update_after_merges_and_before_tasks(self) -> None:
        app = self.app()
        order: list[str] = []
        app.pending_merges.tick = MagicMock(side_effect=lambda: order.append("merges"))
        app._maybe_run_auto_update = MagicMock(side_effect=lambda: order.append("update"))
        app._maybe_start_task_worker = MagicMock(side_effect=lambda: order.append("tasks"))

        with patch.object(app, "_refresh_task_board"):
            app.run_once()

        self.assertEqual(order, ["merges", "update", "tasks"])

    def test_manual_update_restart_still_deferred_is_not_doubled(self) -> None:
        app = self.app()
        running = self.running_task()
        app._after_requested_merge(LandedReview("#91", URL, "our-ark/enoch", "main"))
        app.restarts.defer("update")

        complete_task(running.id, self.root, "done")
        app._maybe_run_auto_update()

        # The deferred restart runs first; the flag survives for the new daemon.
        self.update.assert_not_called()
        self.assertEqual(self.pending(), ("#91",))


class CoalescingTests(_AutoUpdateTestCase):
    def test_several_merges_produce_one_update(self) -> None:
        app = self.app()
        running = self.running_task()
        app._after_requested_merge(LandedReview("#105", URL, "our-ark/enoch", "main"))
        app._after_requested_merge(LandedReview("#106", URL, "our-ark/enoch", "main"))
        app._after_requested_merge(LandedReview("#106", URL, "our-ark/enoch", "main"))
        self.assertEqual(self.pending(), ("#105", "#106"))

        complete_task(running.id, self.root, "done")
        app._maybe_run_auto_update()
        app._maybe_run_auto_update()

        self.update.assert_called_once()
        self.schedule_restart.assert_called_once_with(self.root)
        state = load_auto_update_state(self.root)
        self.assertEqual((state.pending, state.restarted_for), ((), ("#105", "#106")))

    def test_waits_while_another_merge_on_this_repository_is_pending(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self.app(forge)
        app._after_requested_merge(LandedReview("#105", URL, "our-ark/enoch", "main"))
        app._pr("room-1", "merge 91")  # still pending

        app._maybe_run_auto_update()
        self.update.assert_not_called()
        # Waiting for the merge only admits CI fix tasks (that merge may need one).
        self.assertFalse(app._auto_update_holds_work())
        self.assertTrue(app._auto_update_admits_only_ci_fixes())

        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()
        app._maybe_run_auto_update()

        self.update.assert_called_once()
        self.assertEqual(load_auto_update_state(self.root).restarted_for, ("#105", "#91"))

    def _waiting_for_another_merge(self):
        """An update is pending for #105 while #91 (not a task's PR) still merges."""

        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self.app(forge)
        app._after_requested_merge(LandedReview("#105", URL, "our-ark/enoch", "main"))
        app._pr("room-1", "merge 91")
        self.assertIsNone(app._land_hold())
        return app, forge

    def test_waiting_for_another_merge_admits_only_ci_fix_tasks(self) -> None:
        app, _forge = self._waiting_for_another_merge()
        add_backlog_item("room-1", "backlog work", self.root)

        # Nothing is promoted from backlog, and an ordinary queued task waits.
        self.assertFalse(self.worker_starts(app))
        self.assertIsNotNone(next_backlog_item(self.root))
        self.assertEqual(task_queue_status(self.root).pending, ())
        enqueue_task("room-1", "next feature", self.root)
        self.assertFalse(self.worker_starts(app))

        # A CI fix task still starts, and the embedding API reports the hold.
        fix = enqueue_task_front(
            "room-1", "fix CI", self.root, trigger=CI_REPAIR_TRIGGER, initiated_by="agent"
        )
        self.assertTrue(self.worker_starts(app))
        ran: list[int] = []

        def run(job):
            ran.append(job.id)
            complete_task(job.id, self.root, "fixed")

        app._run_task_job = run
        result = app.resume_pending_tasks(max_tasks=5, promote_backlog=True)
        self.assertEqual((result.task_ids, result.stop_reason), ((fix.id,), "update"))
        self.assertEqual(ran, [fix.id])
        self.assertEqual(
            [job.text for job in task_queue_status(self.root).pending], ["next feature"]
        )
        self.assertIsNotNone(next_backlog_item(self.root))

    def test_backlog_task_does_not_start_while_the_update_waits_for_a_repin(self) -> None:
        # A task's PR lands at completion (auto-merge) and Enoch opens a follow-up
        # PR that is still merging (like the library re-pin). The worker loop
        # must not promote the next backlog item onto the old code; the update
        # runs first, as soon as that merge lands.
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self.app(forge)
        add_backlog_item("room-1", "next backlog item", self.root)
        enqueue_task("room-1", "task whose PR auto-merges", self.root)
        ran: list[str] = []

        def run(job):
            ran.append(job.text)
            app._after_requested_merge(LandedReview("#105", URL, "our-ark/enoch", "main"))
            app._pr("room-1", "merge 91")  # the follow-up PR, still merging
            complete_task(job.id, self.root, "done")

        app._run_task_job = run
        result = app._drain_pending_tasks(
            max_tasks=None, promote_backlog=True, reconcile_schedules=False
        )

        self.assertEqual(ran, ["task whose PR auto-merges"])
        self.assertEqual(result.stop_reason, "update")
        self.assertIsNotNone(next_backlog_item(self.root))
        status = task_queue_status(self.root)
        self.assertEqual((status.running, status.pending), (None, ()))

        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()
        app._maybe_run_auto_update()

        self.update.assert_called_once()
        self.schedule_restart.assert_called_once_with(self.root)
        self.assertEqual(load_auto_update_state(self.root).restarted_for, ("#105", "#91"))
        self.assertIsNotNone(next_backlog_item(self.root))

    def test_merge_landing_during_the_update_stays_pending(self) -> None:
        app = self.app()
        app._after_requested_merge(LandedReview("#105", URL, "our-ark/enoch", "main"))

        def update(*_args, **_kwargs):
            app._after_requested_merge(LandedReview("#106", URL, "our-ark/enoch", "main"))
            return _updated()

        self.update.side_effect = update
        app._maybe_run_auto_update()

        state = load_auto_update_state(self.root)
        self.assertEqual((state.pending, state.restarted_for), (("#106",), ("#105",)))


class DoctorFailureTests(_AutoUpdateTestCase):
    def test_doctor_failure_means_no_restart_and_one_alert(self) -> None:
        self.update.return_value = _doctor_failed()
        app = self.app()
        app._pr("room-1", "merge 91")

        app._maybe_run_auto_update()
        app._maybe_run_auto_update()

        self.update.assert_called_once()
        self.schedule_restart.assert_not_called()
        self.assertFalse(app.restarts.pending)
        self.assertEqual(
            self.texts(),
            [
                "⚠️ Auto-update after merging #91 stopped: doctor failed (tests). "
                "Not restarting; still running the current code (1111111)."
            ],
        )
        self.assertEqual(self.pending(), ())

    def test_update_refusal_sends_one_alert_and_clears(self) -> None:
        self.update.return_value = UpdateResult(
            message="Enoch could not update: working copy has uncommitted changes.",
            direct_action_result="",
        )
        app = self.app()
        app._pr("room-1", "merge 91")

        app._maybe_run_auto_update()
        app._maybe_run_auto_update()

        self.schedule_restart.assert_not_called()
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("did not run: Enoch could not update", self.texts()[0])
        self.assertEqual(self.pending(), ())

    def test_already_current_clears_without_restart_or_message(self) -> None:
        self.update.return_value = UpdateResult(
            message="Enoch is already up to date.",
            direct_action_result="Already at authoritative revision abc.",
            previous_revision_id="abc",
            revision_id="abc",
        )
        app = self.app()
        app._pr("room-1", "merge 91")

        app._maybe_run_auto_update()

        self.schedule_restart.assert_not_called()
        self.assertEqual((self.pending(), self.texts()), ((), []))

    def test_update_exception_is_contained(self) -> None:
        self.update.side_effect = RuntimeError("disk full")
        app = self.app()
        app._pr("room-1", "merge 91")

        app._maybe_run_auto_update()

        self.schedule_restart.assert_not_called()
        self.assertEqual(self.pending(), ())
        self.assertEqual(len(self.texts()), 1)
        self.assertIn("disk full", self.texts()[0])
        self.assertFalse(app._auto_update_running)

    def test_updater_reports_failing_doctor_checks(self) -> None:
        old = RepositoryRevision("old")
        new = RepositoryRevision("new")
        repository = MagicMock()
        repository.inspect_working_copy.side_effect = [
            WorkingCopyState(old, clean=True),
            WorkingCopyState(new, clean=True),
        ]
        authoritative = MagicMock(revision=new)
        authoritative.name = "main"
        repository.authoritative_base.return_value = authoritative
        repository.repository_is_ancestor.return_value = True
        doctor = ImmuneResult(
            passed=False,
            command="doctor",
            output="",
            diagnosis=DoctorDiagnosis("failed", [], [], ""),
            checks=[
                DoctorCheckResult("Python runtime", True, "", ""),
                DoctorCheckResult("tests", False, "", "1 failed"),
                DoctorCheckResult("import smoke", False, "", "", skipped=True),
            ],
        )

        with patch.object(updater, "run_update_doctor", return_value=doctor):
            result = updater.update_from_authoritative(self.root, repository=repository)

        self.assertFalse(result.restart_required)
        self.assertEqual(result.doctor_failed_checks, ("tests",))
        self.assertEqual(result.previous_revision_id, "old")
        repository.restore_repository_revision.assert_called_with(old, self.root)


class SettingTests(_AutoUpdateTestCase):
    def test_setting_defaults_on_and_persists(self) -> None:
        self.assertTrue(auto_update_enabled(self.root))
        self.assertFalse(save_auto_update(False, self.root))
        self.assertFalse(auto_update_enabled(self.root))
        self.assertTrue(save_auto_update(True, self.root))

    def test_setting_off_never_marks_or_runs(self) -> None:
        save_auto_update(False, self.root)
        app = self.app()

        app._pr("room-1", "merge 91")
        app._maybe_run_auto_update()

        self.assertEqual(self.pending(), ())
        self.update.assert_not_called()

    def test_update_auto_command(self) -> None:
        app = self.app()
        self.assertEqual(
            app._update_from_chat("room-1", "auto"),
            "Auto-update after merge: on (default). Change it with /update auto on|off.",
        )
        app._after_requested_merge(LandedReview("#91", URL, "our-ark/enoch", "main"))

        reply = app._update_from_chat("room-1", "auto off")

        self.assertTrue(reply.startswith("Auto-update is off."), reply)
        self.assertFalse(auto_update_enabled(self.root))
        # Turning it off drops a pending update.
        self.assertEqual(self.pending(), ())
        self.assertIn("Auto-update is on.", app._update_from_chat("room-1", "auto on"))
        self.assertIn("Use /update auto on|off", app._update_from_chat("room-1", "auto maybe"))
        self.assertIn("locked", app._update_from_chat("room-2", "auto off"))
        self.assertTrue(auto_update_enabled(self.root))
        self.assertIn("Update commands:", app._update_from_chat("room-1", "now"))
        self.update.assert_not_called()

    def test_config_and_help_show_the_setting(self) -> None:
        self.assertIn("- Auto-update after merge: on (default)", config_status(self.root, runtime=_Runtime()))
        save_auto_update(False, self.root)
        status = config_status(self.root, runtime=_Runtime())
        self.assertIn("- Auto-update after merge: off", status)
        self.assertIn("/update auto on|off", status)
        help_text = help_message("update")
        self.assertIn("/update auto on|off", help_text)
        self.assertIn("doctor fails", help_text)

    def test_manual_update_clears_the_pending_flag(self) -> None:
        app = self.app()
        app._after_requested_merge(LandedReview("#91", URL, "our-ark/enoch", "main"))

        app._update_from_chat("room-1")

        self.update.assert_called_once()
        self.assertEqual(self.pending(), ())


class PersistenceTests(_AutoUpdateTestCase):
    def test_pending_flag_survives_a_restart_and_runs_in_the_new_daemon(self) -> None:
        self.app()._pr("room-1", "merge 91")
        self.assertTrue(auto_update_path(self.root).exists())

        restarted = self.app()
        self.assertEqual(self.pending(), ("#91",))
        restarted._maybe_run_auto_update()

        self.update.assert_called_once()
        self.schedule_restart.assert_called_once_with(self.root)
        self.assertEqual(self.pending(), ())

    def test_startup_message_names_the_new_revision_once(self) -> None:
        app = self.app()
        app._after_requested_merge(LandedReview("#105", URL, "our-ark/enoch", "main"))
        app._after_requested_merge(LandedReview("#106", URL, "our-ark/enoch", "main"))
        app._maybe_run_auto_update()

        restarted = self.app()
        restarted.notify_startup()

        startup = self.texts()[-1]
        self.assertIn("Auto-updated to abcdef0 after merging #105 and #106.", startup)
        self.assertEqual(load_auto_update_state(self.root).restarted_into, "")
        self.app().notify_startup()
        self.assertNotIn("Auto-updated", self.texts()[-1])

    def test_state_helpers_clear_only_covered_reviews(self) -> None:
        mark_update_pending("#1", "t1", self.root)
        mark_update_pending("#2", "t2", self.root)
        state = clear_update_pending(("#1",), self.root)
        self.assertEqual((state.pending, state.requested_at), (("#2",), "t1"))
        state = clear_update_pending(None, self.root)
        self.assertEqual((state.pending, state.requested_at), ((), ""))


if __name__ == "__main__":
    unittest.main()
