from __future__ import annotations

from pathlib import Path
import os
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from enoch.app.epoch import begin_daemon_epoch, StaleDaemonEpoch
from enoch.conformance import CompletionGateConformanceMixin
from enoch.extensions import ExtensionWorkflow
from enoch.tasks.completion import CompletionGate, CompletionDecision
from enoch.tasks.queue import TaskTerminalEvidence
from enoch.tasks.recovery import ExtensionRecoveryDecision
from enoch.tasks.events import load_task_events
from enoch.workflows import LocalWorkflowEngine
from enoch.app.core import EnochApplication
from enoch.extensions import AgentExtension, ExtensionLifecycleHooks
from enoch.identity import load_identity
from enoch.providers import ChatEvent
from enoch.private_state import migrate_private_state
from our_ark_provider_kit import BranchlessRepositoryFixture, IndependentReviewFixture
from tests.test_enoch_application import _Chat, _Runtime


class CompletionGateTests(CompletionGateConformanceMixin, unittest.TestCase):
    def create_completion_workflow(self, root, *, epoch, clock):
        return LocalWorkflowEngine(root, epoch=epoch, clock=clock, worker_liveness=lambda _: False)

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.engine, self.now, self.parent, self.unrelated, self.child = self.completion_fixture(self.root)

    def finish_child(self, status="completed"):
        self.assertEqual(self.engine.start_next().id, self.child.id)
        self.engine.finalize(self.child.id, status)

    def test_evidence_and_confirmation_are_both_required(self):
        called = []
        self.engine.configure_completion_gates({"fixture": lambda snapshot: (called.append(snapshot) or CompletionDecision("complete"))})
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(called, [])
        self.finish_child()
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0].children[0].id, self.child.id)
        self.assertEqual(self.engine.find(self.parent.id).status, "completed")
        self.engine.reconcile()
        self.assertEqual(len(called), 1)

    def test_extension_can_extend_gate_then_confirm(self):
        decisions = iter([
            CompletionDecision("extend", gate=CompletionGate("2030-01-03T00:00:00+00:00", evidence_keys=("review", "cleanup"))),
            CompletionDecision("complete"),
        ])
        self.engine.configure_completion_gates({"fixture": lambda _: next(decisions)})
        self.finish_child()
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(self.engine.find(self.parent.id).status, "awaiting_completion")
        self.assertIsNone(self.engine.start_next())
        self.engine.record_completion_evidence(self.parent.id, "fixture", "cleanup", "done")
        self.assertEqual(self.engine.find(self.parent.id).status, "completed")

    def test_failed_child_is_visible_to_owner_and_can_fail_parent(self):
        self.engine.configure_completion_gates({"fixture": lambda snapshot: CompletionDecision("fail", snapshot.children[0].status)})
        self.finish_child("failed")
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "checked")
        self.assertEqual(self.engine.find(self.parent.id).status, "failed")
        self.assertIn("failed", self.engine.find(self.parent.id).result)

    def test_hook_error_and_invalid_result_fail_without_blocking_forever(self):
        self.finish_child()
        self.engine.configure_completion_gates({"fixture": lambda _: "not a decision"})
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(self.engine.find(self.parent.id).status, "failed")

    def test_hook_timeout_discards_late_completion(self):
        release = threading.Event()
        finished = threading.Event()
        def hook(snapshot):
            release.wait(2)
            finished.set()
            return CompletionDecision("complete")
        self.finish_child()
        self.engine.configure_completion_gates({"fixture": hook}, timeout_seconds=0.01)
        try:
            self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
            self.assertEqual(self.engine.find(self.parent.id).status, "failed")
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.engine.reconcile()
        self.assertEqual(self.engine.find(self.parent.id).status, "failed")

    def test_cancel_racing_confirmation_wins(self):
        self.finish_child()
        def hook(snapshot):
            self.engine.cancel(snapshot.task.id)
            return CompletionDecision("complete")
        self.engine.configure_completion_gates({"fixture": hook})
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(self.engine.find(self.parent.id).status, "cancelled")
        self.assertEqual(sum(job.id == self.parent.id for job in self.engine.inspect().history), 1)

    def test_epoch_takeover_during_hook_cannot_finalize(self):
        self.finish_child()
        def hook(snapshot):
            begin_daemon_epoch(self.root)
            return CompletionDecision("complete")
        self.engine.configure_completion_gates({"fixture": hook})
        with self.assertRaises(StaleDaemonEpoch):
            self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(self.engine.find(self.parent.id).status, "awaiting_completion")

    def test_evidence_is_owned_named_and_append_only(self):
        for owner, name in (("foreign", "review"), ("fixture", "unknown")):
            with self.assertRaises(ValueError):
                self.engine.record_completion_evidence(self.parent.id, owner, name, "passed")
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        with self.assertRaises(ValueError):
            self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "changed")

    def test_missing_extension_after_restart_holds_until_timeout(self):
        engine = self.create_completion_workflow(self.root, epoch=begin_daemon_epoch(self.root), clock=lambda: self.now[0])
        engine.recover()
        child = engine.start_next()
        engine.finalize(child.id, "completed")
        engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(engine.find(self.parent.id).status, "awaiting_completion")
        self.now[0] = "2030-01-03T00:00:00+00:00"
        engine.reconcile()
        self.assertEqual(engine.find(self.parent.id).status, "failed")

    def test_no_direct_admission_bypasses_gate(self):
        with self.assertRaisesRegex(RuntimeError, "completion gate"):
            self.engine.enqueue(1, "bypass", mode="direct")

    def test_extension_facade_exposes_status_and_cancel(self):
        workflow = ExtensionWorkflow.from_engine("fixture", self.engine)
        status = workflow.status(self.parent.id)
        self.assertFalse(status.terminal)
        self.assertEqual(status.completion_gate.child_task_ids, (self.child.id,))
        self.assertEqual(workflow.cancel(self.parent.id).state, "cancelled")

    def test_reject_foreign_children_and_cycles(self):
        for children in ((self.unrelated.id,), (self.child.id,), (999,)):
            with self.subTest(children=children), self.assertRaises(ValueError):
                self.engine.declare_completion_gate(self.child.id, "fixture", CompletionGate("2030-01-02T00:00:00+00:00", children))
        with self.assertRaisesRegex(ValueError, "cycles"):
            self.engine.declare_completion_gate(self.child.id, "fixture", CompletionGate("2030-01-02T00:00:00+00:00", (self.parent.id,)))

    def test_terminal_evidence_recovery_does_not_bypass_gate(self):
        self.engine.cancel(self.parent.id)
        parent = self.engine.enqueue(1, "crash parent", context_source="extension:fixture", mode="front")
        self.engine.declare_completion_gate(parent.id, "fixture", CompletionGate("2030-01-02T00:00:00+00:00", evidence_keys=("cleanup",)))
        self.engine.start_next()
        self.engine.claim(parent.id, "dead-worker", 99999999)
        self.engine.record_terminal_evidence(parent.id, "dead-worker", TaskTerminalEvidence("completed", "worker result"))
        self.engine.recover()
        job = self.engine.find(parent.id)
        self.assertEqual(job.status, "awaiting_completion")
        self.assertEqual(job.attempt, 1)
        self.assertNotIn(parent.id, [job.id for job in self.engine.inspect().history])

    def test_lane_does_not_block_declared_child_behind_unrelated_task(self):
        self.engine.cancel(self.parent.id)
        owner = ExtensionWorkflow.from_engine("fixture", self.engine)
        parent = owner.enqueue(1, "lane parent", lane="repo")
        unrelated = owner.enqueue(1, "lane unrelated", lane="repo")
        child = owner.enqueue(1, "lane child", lane="repo")
        owner.declare_completion_gate(parent.id, CompletionGate("2030-01-02T00:00:00+00:00", (child.id,)))
        while (job := self.engine.start_next()).id != parent.id:
            self.engine.finalize(job.id, "completed")
        self.engine.finalize(parent.id, "completed")
        self.assertEqual(self.engine.start_next().id, child.id)
        self.engine.finalize(child.id, "completed")
        self.assertEqual(self.engine.start_next().id, unrelated.id)

    def test_gate_type_validation(self):
        for deadline in ("", "2030-01-01", None):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                CompletionGate(deadline, (1,))
        with self.assertRaises(ValueError):
            CompletionGate("2030-01-02T00:00:00+00:00")

    def test_migration_and_idempotency_preserve_waiting_task(self):
        with patch("enoch.private_state._pid_is_alive", return_value=False):
            migrate_private_state(self.root)
        self.assertEqual(self.engine.find(self.parent.id).status, "awaiting_completion")
        self.assertEqual(self.engine.inspect().awaiting[0].completion_gate.child_task_ids, (self.child.id,))

    def test_gate_extension_cannot_create_cycle_or_reuse_same_gate(self):
        gate = self.engine.find(self.parent.id).completion_gate
        self.finish_child()
        self.engine.configure_completion_gates({"fixture": lambda _: CompletionDecision("extend", gate=gate)})
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.assertEqual(self.engine.find(self.parent.id).status, "failed")

    def test_nested_gates_complete_in_one_evidence_tick(self):
        self.engine.declare_completion_gate(self.child.id, "fixture", CompletionGate(
            "2030-01-02T00:00:00+00:00", evidence_keys=("child-review",),
        ))
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        self.finish_child()
        self.assertEqual(len(self.engine.inspect().awaiting), 2)
        self.engine.record_completion_evidence(self.child.id, "fixture", "child-review", "passed")
        self.assertEqual(self.engine.find(self.child.id).status, "completed")
        self.assertEqual(self.engine.find(self.parent.id).status, "completed")

    def test_extension_recovery_does_not_bypass_completion_gate(self):
        self.engine.cancel(self.parent.id)
        parent = self.engine.enqueue(1, "recover parent", context_source="extension:fixture", mode="front")
        self.engine.declare_completion_gate(parent.id, "fixture", CompletionGate(
            "2030-01-02T00:00:00+00:00", evidence_keys=("cleanup",),
        ))
        self.engine.configure_extension_recovery({"fixture": lambda _: ExtensionRecoveryDecision(
            "finalize", evidence=TaskTerminalEvidence("completed", "Recovered work"),
        )})
        self.engine.start_next()
        self.engine.claim(parent.id, "dead-worker", 99999999)
        self.engine.recover()
        self.assertEqual(self.engine.find(parent.id).status, "awaiting_completion")
        self.assertFalse(self.engine.inspect().paused)
        self.engine.record_completion_evidence(parent.id, "fixture", "cleanup", "done")
        self.assertEqual(self.engine.find(parent.id).status, "completed")

    def test_concurrent_engines_commit_completion_once(self):
        self.finish_child()
        self.engine.configure_completion_gates({})
        self.engine.record_completion_evidence(self.parent.id, "fixture", "review", "passed")
        other = self.create_completion_workflow(self.root, epoch=self.engine.epoch, clock=lambda: self.now[0])
        barrier = threading.Barrier(2)
        def hook(snapshot):
            barrier.wait(timeout=2)
            return CompletionDecision("complete")
        errors = []
        def reconcile(engine):
            try:
                engine.reconcile()
            except BaseException as error:
                errors.append(error)
        workers = []
        for engine in (self.engine, other):
            engine.configure_completion_gates({"fixture": hook})
            thread = threading.Thread(target=reconcile, args=(engine,))
            thread.start()
            workers.append(thread)
        for thread in workers:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.engine.find(self.parent.id).status, "completed")
        events = load_task_events(self.root, task_id=self.parent.id)
        self.assertEqual(sum(event.event == "completed" for event in events), 1)

    def test_active_worker_finishes_before_gate_deadline_is_enforced(self):
        self.engine.cancel(self.parent.id)
        parent = self.engine.enqueue(1, "long worker", context_source="extension:fixture", mode="front")
        self.engine.declare_completion_gate(parent.id, "fixture", CompletionGate(
            "2030-01-02T00:00:00+00:00", evidence_keys=("cleanup",),
        ))
        self.engine.start_next()
        self.engine.claim(parent.id, "live-worker", os.getpid())
        self.now[0] = "2030-01-03T00:00:00+00:00"
        self.engine.reconcile()
        self.assertEqual(self.engine.find(parent.id).status, "running")
        self.engine.finalize(parent.id, "completed", worker_id="live-worker")
        self.assertEqual(self.engine.find(parent.id).failure_code, "completion_gate_timeout")


class CompletionApplicationTests(unittest.TestCase):
    def test_application_waits_runs_children_and_announces_final_completion(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            extension = AgentExtension(name="fixture", lifecycle=ExtensionLifecycleHooks(
                on_task_completion=lambda _: CompletionDecision("complete"),
            ))
            app = EnochApplication(
                load_identity(), root, chat, runtime=_Runtime(),
                repository=BranchlessRepositoryFixture(), review=IndependentReviewFixture(),
                extensions=(extension,),
            )
            owner = ExtensionWorkflow.from_engine("fixture", app.workflow)
            parent = owner.enqueue("room-1", "Parent work")
            unrelated = app.workflow.enqueue("room-1", "Unrelated work")
            child = owner.enqueue("room-1", "Review work")
            owner.declare_completion_gate(parent.id, CompletionGate("2030-01-02T00:00:00+00:00", (child.id,), ("review",)))
            with patch.object(app, "_watch_published_reviews") as watch, patch.object(app, "_apply_merge_policy_safely", return_value="") as merge:
                first = app.resume_pending_tasks(max_tasks=5)
                self.assertNotIn(parent.id, [call.args[0].id for call in watch.call_args_list])
                self.assertNotIn(parent.id, [call.args[0].id for call in merge.call_args_list])
            self.assertEqual(first.task_ids, (parent.id, child.id))
            self.assertEqual(first.stop_reason, "completion")
            self.assertEqual(owner.status(parent.id).state, "awaiting_completion")
            self.assertEqual(app.workflow.find(unrelated.id).status, "pending")
            app.handle_event(ChatEvent(cursor="queue", conversation_id="room-1", message_id="queue", text="/queue"))
            self.assertIn("Awaiting completion", chat.sent[-1][1])
            self.assertIn("missing evidence: review", chat.sent[-1][1])
            owner.record_completion_evidence(parent.id, "review", "review:passed")
            self.assertEqual(owner.status(parent.id).state, "completed")
            with patch.object(app, "_maybe_start_task_worker"), patch.object(app, "_maybe_start_lineage_worker"), \
                 patch.object(app, "_watch_published_reviews") as watch, \
                 patch.object(app, "_apply_merge_policy_safely", return_value="") as merge:
                app.run_once()
                count = len(chat.sent)
                app.run_once()
                self.assertEqual([call.args[0].id for call in watch.call_args_list], [parent.id])
                self.assertEqual([call.args[0].id for call in merge.call_args_list], [parent.id])
            self.assertEqual(len(chat.sent), count)
            self.assertIsNotNone(app.workflow.find(parent.id).result_message_id)
            self.assertEqual(app.resume_pending_tasks().task_ids, (unrelated.id,))

    def test_unregistered_completion_hook_is_rejected(self):
        with TemporaryDirectory() as temp:
            engine = LocalWorkflowEngine(Path(temp))
            task = engine.enqueue(1, "parent", context_source="extension:fixture")
            with self.assertRaisesRegex(ValueError, "register"):
                engine.declare_completion_gate(task.id, "fixture", CompletionGate("2030-01-02T00:00:00+00:00", evidence_keys=("review",)))


if __name__ == "__main__":
    unittest.main()
