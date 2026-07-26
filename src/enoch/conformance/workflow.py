from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from enoch.app.epoch import DaemonEpoch, StaleDaemonEpoch, begin_daemon_epoch
from enoch.workflows import WORKFLOW_API_VERSION, WorkflowEngine


class WorkflowEngineConformanceMixin:
    """Reusable lifecycle and reliability checks for a workflow engine."""

    def create_workflow(
        self,
        root: Path,
        *,
        epoch: DaemonEpoch,
    ) -> WorkflowEngine:
        raise NotImplementedError

    def begin_fencing_epoch(self, root: Path) -> DaemonEpoch:
        return begin_daemon_epoch(root, provider="conformance")

    def test_conformance_workflow_lifecycle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = self._new_engine(root)
            queued = engine.enqueue(42, "complete lifecycle")
            started = engine.start_next()
            self.assertIsNotNone(started)
            assert started is not None
            claimed = engine.claim(started.id, "conformance-worker", 999_999)
            self.assertIsNotNone(claimed)
            heartbeat = engine.heartbeat(started.id, "conformance-worker")
            completed = engine.finalize(
                started.id,
                "completed",
                result="done",
                worker_id="conformance-worker",
            )

            self.assertEqual(engine.api_version, WORKFLOW_API_VERSION)
            self.assertEqual(queued.id, started.id)
            self.assertIsNotNone(heartbeat)
            self.assertEqual(completed.status, "completed")
            self.assertIsNone(engine.inspect().running)

    def test_conformance_workflow_deduplicates_requests(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = self._new_engine(root)
            first = engine.enqueue(
                42,
                "one durable request",
                idempotency_key="conformance:duplicate",
            )
            repeated = engine.enqueue(
                42,
                "one durable request",
                idempotency_key="conformance:duplicate",
            )

            self.assertEqual(repeated.id, first.id)
            self.assertEqual(engine.inspect().pending_count, 1)

    def test_conformance_workflow_cancels_pending_work(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = self._new_engine(root)
            queued = engine.enqueue(42, "cancel this")
            cancelled = engine.cancel(queued.id)

            self.assertIsNotNone(cancelled)
            self.assertEqual(cancelled.status, "cancelled")
            self.assertEqual(engine.inspect().pending_count, 0)

    def test_conformance_workflow_recovers_after_restart(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            epoch = self.begin_fencing_epoch(root)
            first = self.create_workflow(root, epoch=epoch)
            queued = first.enqueue(42, "recover this", max_attempts=2)
            started = first.start_next()
            self.assertEqual(started.id, queued.id)

            restarted = self.create_workflow(root, epoch=epoch)
            recovered = restarted.recover()

            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, queued.id)
            self.assertEqual(recovered.status, "pending")
            self.assertEqual(restarted.inspect().pending_count, 1)

    def test_conformance_workflow_rejects_stale_fencing_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            stale_epoch = self.begin_fencing_epoch(root)
            stale = self.create_workflow(root, epoch=stale_epoch)
            self.begin_fencing_epoch(root)

            with self.assertRaises(StaleDaemonEpoch):
                stale.enqueue(42, "must be fenced")

            self.assertEqual(stale.inspect().pending_count, 0)

    def test_conformance_workflow_contains_partial_failure(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = self._new_engine(root)
            first = engine.enqueue(42, "first request")
            second = engine.enqueue(42, "second request")
            started = engine.start_next()
            self.assertEqual(started.id, first.id)
            engine.claim(started.id, "conformance-worker", 999_999)
            failed = engine.finalize(
                started.id,
                "failed",
                result="provider completed only part of the request",
                worker_id="conformance-worker",
                failure_code="partial_failure",
                failure_class="permanent",
                retryable=False,
            )
            following = engine.start_next()

            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.failure_code, "partial_failure")
            self.assertEqual(following.id, second.id)

    def _new_engine(self, root: Path) -> WorkflowEngine:
        epoch = self.begin_fencing_epoch(root)
        engine = self.create_workflow(root, epoch=epoch)
        self.assertIsInstance(engine, WorkflowEngine)
        return engine
