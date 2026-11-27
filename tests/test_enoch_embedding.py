from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from enoch.app.core import EnochApplication
from enoch.app.epoch import begin_daemon_epoch
from enoch.backlog import add_backlog_item
from enoch.conformance import EmbeddingConformanceMixin
from enoch.embedding import (
    EmbeddingConversationDenied,
    EvolveCommandResult,
    PendingTaskExecutionResult,
    StaleDaemonEpoch,
)
from enoch.evolution.core import create_brainstorm_candidates, load_evolve_state
from enoch.evolution.sources.brainstorming import BrainstormCandidateDraft
from enoch.identity import load_identity
from our_ark_provider_kit import BranchlessRepositoryFixture, IndependentReviewFixture
from tests.test_enoch_application import _Chat, _Runtime


class EmbeddingTests(EmbeddingConformanceMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.app = self.create_embedded_application(self.root)

    def create_embedded_application(self, root):
        app = EnochApplication(
            load_identity(), root, _Chat(), runtime=_Runtime(),
            repository=BranchlessRepositoryFixture(), review=IndependentReviewFixture(),
        )
        return app

    def enqueue_embedding_task(self, app):
        return app.workflow.enqueue("room-1", "Inspect the local fixture.").id

    def complete(self, job):
        self.app.workflow.finalize(job.id, "completed", result="Done.")

    def test_command_returns_reply_without_sending_it_or_starting_worker(self):
        with patch.object(self.app, "_maybe_start_task_worker") as worker:
            result = self.app.handle_evolve_command("room-1", "config mode co-evolve")
        self.assertIsInstance(result, EvolveCommandResult)
        self.assertEqual(load_evolve_state(self.root).mode, "co-evolve")
        self.assertEqual(self.app.client.sent, [])
        worker.assert_not_called()

    def test_wrong_conversation_cannot_mutate_evolve(self):
        before = load_evolve_state(self.root)
        with self.assertRaises(EmbeddingConversationDenied):
            self.app.handle_evolve_command("other", "config mode co-evolve")
        self.assertEqual(load_evolve_state(self.root), before)

    def test_command_rechecks_epoch_even_when_handler_returns_normally(self):
        def takeover(*args):
            begin_daemon_epoch(self.root)
            return "Handled runtime error"
        with patch.object(self.app, "_evolve", side_effect=takeover):
            with self.assertRaises(StaleDaemonEpoch):
                self.app.handle_evolve_command("room-1", "propose")

    def test_pending_execution_has_no_implicit_background_work(self):
        first = self.enqueue_embedding_task(self.app)
        second = self.enqueue_embedding_task(self.app)
        add_backlog_item("room-1", "Do this later", self.root)
        forbidden = [
            "_promote_next_backlog_if_idle", "_enqueue_due_cron_jobs",
            "_enqueue_due_extension_schedules", "_run_due_evolve_schedule",
            "_run_due_evidence_scans", "_maybe_start_lineage_worker",
        ]
        with ExitStack() as stack:
            for name in forbidden:
                stack.enter_context(patch.object(self.app, name, side_effect=AssertionError(name)))
            stack.enter_context(patch.object(self.app.client, "receive", side_effect=AssertionError("poll")))
            stack.enter_context(patch.object(self.app, "_run_task_job", side_effect=self.complete))
            result = self.app.resume_pending_tasks(max_tasks=3)
        self.assertEqual(result, PendingTaskExecutionResult((first, second), "idle"))
        self.assertFalse(self.app._cron_scheduler_wake.is_set())
        self.assertIsNone(self.app._task_worker)

    def test_backlog_is_promoted_only_with_opt_in(self):
        add_backlog_item("room-1", "Do this later", self.root)
        with patch.object(self.app, "_run_task_job", side_effect=self.complete):
            self.assertEqual(self.app.resume_pending_tasks().task_ids, ())
            result = self.app.resume_pending_tasks(promote_backlog=True)
        self.assertEqual(len(result.task_ids), 1)
        self.assertEqual(result.stop_reason, "limit")

    def test_schedule_reconciliation_requires_opt_in(self):
        self.enqueue_embedding_task(self.app)
        with patch.object(self.app, "_run_task_job", side_effect=self.complete), \
             patch.object(self.app, "_enqueue_due_cron_jobs") as cron, \
             patch.object(self.app, "_enqueue_due_extension_schedules") as extension:
            self.app.resume_pending_tasks(reconcile_schedules=True)
        cron.assert_called_once()
        extension.assert_called_once()
        self.assertTrue(self.app._cron_scheduler_wake.is_set())

    def test_real_task_execution_uses_existing_workflow_and_runtime(self):
        task_id = self.enqueue_embedding_task(self.app)
        result = self.app.resume_pending_tasks()
        job = self.app.workflow.find(task_id)
        self.assertEqual(result.task_ids, (task_id,))
        self.assertEqual(job.status, "completed", job.result)
        self.assertTrue(self.app.client.sent)

    def test_approved_evolve_candidate_executes_through_public_entry_points(self):
        creation = create_brainstorm_candidates(
            (BrainstormCandidateDraft(
                title="Make provenance visible", rationale="Keep changes accountable.",
                proposed_change="Show source details in candidate reports.",
                expected_benefit="Improves review quality.", risk="Adds output.",
                test_plan="Add report tests.",
            ),), self.root, theme="accountable evolution", context_hash="c" * 64,
        )
        candidate = creation.created[0]
        reply = self.app.handle_evolve_command("room-1", f"approve {candidate.id}")
        self.assertIn("Approved evolve candidate", reply.reply)
        pending = self.app.workflow.inspect().pending
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].candidate_id, candidate.id)
        self.assertEqual(pending[0].approval_actor, "human")
        result = self.app.resume_pending_tasks()
        self.assertEqual(result.task_ids, (pending[0].id,))
        job = self.app.workflow.find(pending[0].id)
        self.assertEqual(job.status, "completed", job.result)
        self.assertEqual(job.candidate_id, candidate.id)

    def test_stale_command_cannot_change_configuration(self):
        before = load_evolve_state(self.root)
        begin_daemon_epoch(self.root)
        with self.assertRaises(StaleDaemonEpoch):
            self.app.handle_evolve_command("room-1", "config mode co-evolve")
        self.assertEqual(load_evolve_state(self.root), before)

    def test_paused_task_blocks_pending_execution_without_resuming_it(self):
        first = self.enqueue_embedding_task(self.app)
        self.app.workflow.start_next()
        self.app.workflow.pause(first, result="Quota exhausted")
        second = self.enqueue_embedding_task(self.app)
        with patch.object(self.app, "_run_task_job") as run:
            result = self.app.resume_pending_tasks()
        self.assertEqual(result, PendingTaskExecutionResult((), "paused"))
        run.assert_not_called()
        self.assertEqual(self.app.workflow.find(first).status, "paused")
        self.assertEqual(self.app.workflow.find(second).status, "pending")

    def test_pausing_during_task_stops_drain(self):
        first = self.enqueue_embedding_task(self.app)
        second = self.enqueue_embedding_task(self.app)
        with patch.object(self.app, "_run_task_job", side_effect=lambda job: self.app.workflow.pause(job.id)):
            result = self.app.resume_pending_tasks(max_tasks=2)
        self.assertEqual(result, PendingTaskExecutionResult((first,), "paused"))
        self.assertEqual(self.app.workflow.find(second).status, "pending")

    def test_existing_worker_prevents_second_owner(self):
        self.app._task_worker = threading.current_thread()
        self.assertEqual(self.app.resume_pending_tasks(), PendingTaskExecutionResult((), "busy"))
        self.assertIs(self.app._task_worker, threading.current_thread())

    def test_running_task_prevents_second_owner(self):
        self.enqueue_embedding_task(self.app)
        self.app.workflow.start_next()
        self.assertEqual(self.app.resume_pending_tasks(), PendingTaskExecutionResult((), "busy"))

    def test_embedded_execution_reserves_worker_slot(self):
        self.enqueue_embedding_task(self.app)
        def run(job):
            self.assertEqual(self.app.resume_pending_tasks().stop_reason, "busy")
            self.app._maybe_start_task_worker()
            self.assertIs(self.app._task_worker, threading.current_thread())
            self.complete(job)
        with patch.object(self.app, "_run_task_job", side_effect=run):
            self.app.resume_pending_tasks()
        self.assertIsNone(self.app._task_worker)

    def test_takeover_during_execution_propagates_and_releases_worker(self):
        self.enqueue_embedding_task(self.app)
        with patch.object(self.app, "_run_task_job", side_effect=lambda job: begin_daemon_epoch(self.root)):
            with self.assertRaises(StaleDaemonEpoch):
                self.app.resume_pending_tasks()
        self.assertIsNone(self.app._task_worker)

    def test_stale_epoch_does_not_change_pending_queue(self):
        task_id = self.enqueue_embedding_task(self.app)
        begin_daemon_epoch(self.root)
        with self.assertRaises(StaleDaemonEpoch):
            self.app.resume_pending_tasks()
        self.assertEqual(self.app.workflow.find(task_id).status, "pending")

    def test_worker_exception_releases_reservation(self):
        self.enqueue_embedding_task(self.app)
        with patch.object(self.app, "_run_task_job", side_effect=ValueError("fixture")):
            with self.assertRaisesRegex(ValueError, "fixture"):
                self.app.resume_pending_tasks()
        self.assertIsNone(self.app._task_worker)

    def test_restart_update_and_review_holds(self):
        self.enqueue_embedding_task(self.app)
        with patch.object(self.app.restarts, "restart_if_idle", return_value=True):
            self.assertEqual(self.app.resume_pending_tasks().stop_reason, "restart")
        with patch.object(self.app, "_auto_update_holds_work", return_value=True):
            self.assertEqual(self.app.resume_pending_tasks().stop_reason, "update")
        with patch.object(self.app, "_land_hold", return_value=object()):
            self.assertEqual(self.app.resume_pending_tasks().stop_reason, "review")
        self.app._stopping = True
        self.assertEqual(self.app.resume_pending_tasks().stop_reason, "stopping")

    def test_invalid_options_are_rejected_before_queue_changes(self):
        for value in (True, 0, -1, 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.app.resume_pending_tasks(max_tasks=value)
        with self.assertRaises(TypeError):
            self.app.resume_pending_tasks(promote_backlog="false")
        with self.assertRaises(TypeError):
            self.app.resume_pending_tasks(reconcile_schedules=1)
        with self.assertRaises(TypeError):
            self.app.handle_evolve_command("room-1", None)


if __name__ == "__main__":
    unittest.main()
