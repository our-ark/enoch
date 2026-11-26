from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock

from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.extensions import ExtensionRecoveryDecision, ExtensionLifecycleHooks, AgentExtension
from enoch.tasks.queue import TaskTerminalEvidence, TaskReconciliationRequest, prepare_extension_recovery
from enoch.tasks.events import load_task_events
from enoch.workflows import LocalWorkflowEngine


class ExtensionRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.epoch = begin_daemon_epoch(self.root)
        self.engine = self.new_engine()

    def new_engine(self):
        return LocalWorkflowEngine(self.root, epoch=self.epoch, worker_liveness=lambda _: False)

    def running(self, owner="research", max_attempts=3):
        self.engine.enqueue(42, "inspect artifact", context_source=f"extension:{owner}",
                            extension_metadata={owner: {"artifact": "local-report"}}, max_attempts=max_attempts)
        return self.engine.start_next()

    def configure(self, hook):
        self.engine.configure_extension_recovery({"research": hook}, timeout_seconds=0.05)

    def test_requeue_note_and_attempt_limit(self):
        job = self.running(max_attempts=2)
        seen = []

        def recover(snapshot):
            seen.append(snapshot)
            return ExtensionRecoveryDecision("requeue", "Inspect existing report first.")

        self.configure(recover)
        result = self.engine.reconcile()
        self.assertEqual(result.outcome, "extension_recovery")
        self.assertEqual(seen[0].extension_name, "research")
        self.assertEqual(seen[0].task.extension_metadata, {"research": {"artifact": "local-report"}})
        restarted = self.engine.start_next()
        self.assertEqual(restarted.id, job.id)
        self.assertEqual(restarted.attempt, 2)
        self.assertIn("Inspect existing report first.", restarted.context)
        self.engine.reconcile()
        self.assertEqual(self.engine.find(job.id).status, "failed")
        self.assertIsNone(self.engine.start_next())

    def test_hold_survives_restart_and_all_resume_paths(self):
        job = self.running()
        self.configure(lambda _: ExtensionRecoveryDecision("hold", "Check remote publication."))
        self.engine.reconcile()
        self.epoch = begin_daemon_epoch(self.root)
        self.engine = self.new_engine()
        self.assertIsNone(self.engine.recover())
        self.assertTrue(self.engine.find(job.id).needs_attention)
        for trigger in ("runtime-access-restored", "daemon-restarted", "/task resume"):
            self.assertEqual(self.engine.resume(trigger=trigger), ())
        retry = self.engine.retry_failed(job.id)
        self.assertEqual(retry.parent_task_id, job.id)
        self.assertIn("Check remote publication.", retry.context)
        self.assertEqual(self.engine.find(job.id).status, "cancelled")
        self.assertFalse(self.engine.inspect().paused)

    def test_hold_can_be_cancelled(self):
        job = self.running()
        self.configure(lambda _: ExtensionRecoveryDecision("hold"))
        self.engine.reconcile()
        self.assertEqual(self.engine.cancel(job.id).status, "cancelled")
        self.assertFalse(self.engine.inspect().paused)

    def test_hold_preserves_partial_result_for_operator_retry(self):
        job = self.running()
        self.engine.record_result(job.id, "An artifact upload may have completed.")
        self.configure(lambda _: ExtensionRecoveryDecision("hold", "Verify the upload."))
        self.engine.reconcile()
        held = self.engine.find(job.id)
        self.assertIn("An artifact upload may have completed.", held.context)
        retry = self.engine.retry_failed(job.id)
        self.assertIn("An artifact upload may have completed.", retry.context)
        self.assertIn("Verify the upload.", retry.context)

    def test_finalize_exactly_once_with_extension_provenance(self):
        for terminal in ("completed", "failed", "cancelled"):
            with self.subTest(terminal=terminal):
                job = self.running()
                hook = Mock(return_value=ExtensionRecoveryDecision(
                    "finalize", evidence=TaskTerminalEvidence(terminal, "Remote state verified.")))
                self.configure(hook)
                self.engine.recover()
                self.engine.reconcile()
                hook.assert_called_once()
                finished = self.engine.find(job.id)
                self.assertEqual(finished.status, terminal)
                self.assertEqual(finished.terminal_evidence_source, "extension-recovery:research")
                events = [event for event in load_task_events(self.root, task_id=job.id) if event.event == terminal]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].trigger, "extension-recovery:research")

    def test_exception_and_invalid_result_hold(self):
        for hook in (Mock(side_effect=ValueError("broken")), Mock(return_value=None)):
            job = self.running()
            self.configure(hook)
            self.engine.reconcile()
            self.assertTrue(self.engine.find(job.id).needs_attention)
            self.engine.cancel(job.id)

    def test_timeout_ignores_late_result(self):
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)
        job = self.running()

        def slow(_):
            release.wait(2)
            finished.set()
            return ExtensionRecoveryDecision("requeue")

        self.configure(slow)
        self.engine.reconcile()
        self.assertTrue(self.engine.find(job.id).needs_attention)
        release.set()
        self.assertTrue(finished.wait(1))
        self.assertTrue(self.engine.find(job.id).needs_attention)
        self.assertIsNone(self.engine.start_next())

    def test_crash_before_decision_leaves_durable_hold(self):
        job = self.running()
        prepare_extension_recovery(job, self.root, worker_liveness=lambda _: False)
        self.epoch = begin_daemon_epoch(self.root)
        self.engine = self.new_engine()
        hook = Mock(return_value=ExtensionRecoveryDecision("requeue"))
        self.configure(hook)
        self.engine.recover()
        hook.assert_not_called()
        self.assertTrue(self.engine.find(job.id).needs_attention)

    def test_stale_epoch_rejects_decision(self):
        job = self.running()

        def takeover(_):
            begin_daemon_epoch(self.root)
            return ExtensionRecoveryDecision("requeue")

        self.configure(takeover)
        with self.assertRaises(StaleDaemonEpoch):
            self.engine.reconcile()
        self.assertTrue(self.engine.find(job.id).needs_attention)

    def test_cancel_during_hook_wins(self):
        job = self.running()

        def cancel(_):
            self.engine.cancel(job.id)
            return ExtensionRecoveryDecision("requeue")

        self.configure(cancel)
        result = self.engine.reconcile()
        self.assertEqual(result.outcome, "conflict")
        self.assertEqual(self.engine.find(job.id).status, "cancelled")

    def test_no_hook_or_different_owner_preserves_legacy_recovery(self):
        job = self.running(owner="other")
        hook = Mock()
        self.configure(hook)
        self.assertEqual(self.engine.reconcile().outcome, "interrupted_worker_recovery")
        hook.assert_not_called()
        self.assertEqual(self.engine.find(job.id).status, "pending")

    def test_live_worker_and_mismatched_snapshot_do_not_invoke_hook(self):
        job = self.running()
        hook = Mock()
        self.configure(hook)
        result = self.engine.reconcile(TaskReconciliationRequest(job.id + 1))
        self.assertEqual(result.outcome, "conflict")
        self.engine._worker_liveness = lambda _: True
        self.engine.reconcile()
        hook.assert_not_called()

    def test_terminal_evidence_keeps_priority(self):
        job = self.running()
        # Persist a worker lease without registering this live test thread.
        from enoch.tasks.queue import claim_running_task, record_task_terminal_evidence
        claim_running_task(job.id, "dead-worker", 999999, self.root)
        record_task_terminal_evidence(job.id, "dead-worker", TaskTerminalEvidence("completed", "done"), self.root)
        hook = Mock()
        self.configure(hook)
        self.assertEqual(self.engine.reconcile().outcome, "terminal_repair")
        hook.assert_not_called()

    def test_snapshot_metadata_is_detached(self):
        job = self.running()

        def modify(snapshot):
            snapshot.task.extension_metadata["research"]["artifact"] = "mutated"
            return ExtensionRecoveryDecision("hold")

        self.configure(modify)
        self.engine.reconcile()
        self.assertEqual(self.engine.find(job.id).extension_metadata["research"]["artifact"], "local-report")

    def test_missing_hook_does_not_treat_unstructured_url_as_terminal_evidence(self):
        from enoch.tasks.queue import record_task_runtime_result, claim_running_task
        from enoch.providers import RuntimeResult
        job = self.running()
        claim_running_task(job.id, "dead-worker", 999999, self.root)
        # A URL in plain output is not proof that the task completed.
        record_task_runtime_result(job.id, RuntimeResult(
            final_text="published https://example.test/pull/1",
        ), self.root, provider="test")
        self.assertNotEqual(self.engine.reconcile().outcome, "terminal_repair")


from tests.test_enoch_restart_safety import _AppTestCase


class ExtensionRecoveryApplicationTests(_AppTestCase):
    def test_application_wires_hook_before_startup_recovery(self):
        from enoch.app.core import EnochApplication
        from enoch.identity import load_identity
        self.app.workflow.enqueue(42, "domain task", context_source="extension:research")
        job = self.app.workflow.start_next()
        hook = Mock(return_value=ExtensionRecoveryDecision("hold", "Check publication."))
        self.app = EnochApplication(load_identity(), self.root, self.chat, extensions=(
            AgentExtension("research", lifecycle=ExtensionLifecycleHooks(on_task_recovery=hook)),
        ))
        hook.assert_called_once()
        self.assertTrue(self.app.workflow.find(job.id).needs_attention)
        self.assertIn("operator attention", self.send("/status"))
        self.assertIn("needs attention", self.send("/queue"))
        self.assertIn("task retry", self.send(f"/task resume {job.id}"))
        self.assertIn("operator attention", self.send("/do new work"))
        self.assertEqual(self.app.runtime_access.paused_tasks(), ())
        self.assertEqual(self.app._auto_resume_paused_tasks(), ())
        self.send(f"/task retry {job.id}")
        self.assertEqual(self.app.workflow.inspect().pending[0].parent_task_id, job.id)
