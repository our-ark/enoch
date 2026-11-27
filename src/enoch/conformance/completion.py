"""Optional completion gate conformance for workflow providers."""
from pathlib import Path
from tempfile import TemporaryDirectory

from enoch.app.epoch import begin_daemon_epoch, StaleDaemonEpoch
from enoch.tasks.completion import CompletionGate, CompletionDecision


class CompletionGateConformanceMixin:
    def create_completion_workflow(self, root, *, epoch, clock):
        raise NotImplementedError

    def completion_fixture(self, root):
        now = ["2030-01-01T00:00:00+00:00"]
        epoch = begin_daemon_epoch(root)
        engine = self.create_completion_workflow(root, epoch=epoch, clock=lambda: now[0])
        engine.configure_completion_gates({"fixture": lambda _: CompletionDecision("complete")})
        parent = engine.enqueue(1, "parent", context_source="extension:fixture")
        unrelated = engine.enqueue(1, "unrelated")
        child = engine.enqueue(1, "child", context_source="extension:fixture")
        engine.declare_completion_gate(parent.id, "fixture", CompletionGate(
            "2030-01-02T00:00:00+00:00", (child.id,), ("review",),
        ))
        engine.start_next()
        engine.finalize(parent.id, "completed", result="Worker finished")
        return engine, now, parent, unrelated, child

    def test_conformance_completion_blocks_unrelated_and_runs_child(self):
        with TemporaryDirectory() as temp:
            engine, _, parent, unrelated, child = self.completion_fixture(Path(temp))
            self.assertEqual(engine.find(parent.id).status, "awaiting_completion")
            self.assertEqual(engine.start_next().id, child.id)
            engine.finalize(child.id, "completed")
            self.assertIsNone(engine.start_next())
            engine.record_completion_evidence(parent.id, "fixture", "review", "review:passed")
            self.assertEqual(engine.find(parent.id).status, "completed")
            self.assertEqual(engine.start_next().id, unrelated.id)
            engine.reconcile()
            self.assertEqual(sum(job.id == parent.id for job in engine.inspect().history), 1)

    def test_conformance_completion_survives_restart(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            old, now, parent, _, child = self.completion_fixture(root)
            epoch = begin_daemon_epoch(root)
            engine = self.create_completion_workflow(root, epoch=epoch, clock=lambda: now[0])
            engine.configure_completion_gates({"fixture": lambda _: CompletionDecision("complete")})
            engine.recover()
            self.assertEqual(engine.find(parent.id).status, "awaiting_completion")
            self.assertEqual(engine.find(parent.id).attempt, 1)
            self.assertEqual(engine.start_next().id, child.id)
            with self.assertRaises(StaleDaemonEpoch):
                old.record_completion_evidence(parent.id, "fixture", "review", "late")

    def test_conformance_completion_timeout(self):
        with TemporaryDirectory() as temp:
            engine, now, parent, unrelated, _ = self.completion_fixture(Path(temp))
            now[0] = "2030-01-03T00:00:00+00:00"
            engine.reconcile()
            self.assertEqual(engine.find(parent.id).status, "failed")
            self.assertEqual(engine.find(parent.id).failure_code, "completion_gate_timeout")
            self.assertEqual(engine.start_next().id, unrelated.id)

    def test_conformance_completion_cancel_detaches_children(self):
        with TemporaryDirectory() as temp:
            engine, _, parent, unrelated, child = self.completion_fixture(Path(temp))
            engine.cancel(parent.id)
            self.assertEqual(engine.find(parent.id).status, "cancelled")
            self.assertEqual(engine.find(child.id).status, "pending")
            self.assertEqual(engine.start_next().id, unrelated.id)
            engine.reconcile()
            self.assertEqual(engine.find(parent.id).status, "cancelled")
