from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from enoch.app.epoch import DaemonEpoch, StaleDaemonEpoch, begin_daemon_epoch
from enoch.tasks.payloads import ExtensionArtifactReference
from enoch.workflows import (
    WORKFLOW_API_VERSION,
    WORKFLOW_FEATURE_ARTIFACT_REFERENCES,
    WORKFLOW_FEATURE_EXECUTION_LANES,
    WORKFLOW_FEATURE_STRUCTURED_METADATA,
    TaskReconciliationRequest,
    TaskTerminalEvidence,
    WorkflowEngine,
    workflow_features,
)


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
            options: dict[str, Any] = {}
            if WORKFLOW_FEATURE_EXECUTION_LANES in workflow_features(first):
                options["context_source"] = "extension:conformance"
                options["execution_lane"] = "extension:conformance:recovery"
            queued = first.enqueue(
                42,
                "recover this",
                max_attempts=2,
                **options,
            )
            started = first.start_next()
            self.assertEqual(started.id, queued.id)

            restarted = self.create_workflow(root, epoch=epoch)
            recovered = restarted.recover()

            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.id, queued.id)
            self.assertEqual(recovered.status, "pending")
            self.assertEqual(recovered.execution_lane, queued.execution_lane)
            self.assertEqual(restarted.inspect().pending_count, 1)

    def test_conformance_workflow_reconciles_terminal_crash_point_once(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = self._new_engine(root)
            running = engine.enqueue(42, "terminal crash point", mode="direct")
            claimed = engine.claim(running.id, "conformance-worker", 999_999)
            assert claimed is not None
            recorded = engine.record_terminal_evidence(
                claimed.id,
                claimed.worker_id,
                TaskTerminalEvidence(
                    status="completed",
                    result="durable terminal result",
                ),
            )
            assert recorded is not None

            repaired = engine.reconcile(
                TaskReconciliationRequest(
                    recorded.id,
                    recorded.worker_id,
                    recorded.worker_heartbeat_at,
                )
            )
            repeated = engine.reconcile()
            status = engine.inspect()

            self.assertEqual(repaired.outcome, "terminal_repair")
            self.assertEqual(repeated.outcome, "no_op")
            self.assertIsNone(status.running)
            self.assertEqual(len(status.history), 1)

    def test_conformance_workflow_reconciliation_preserves_live_worker(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = self._new_engine(root)
            running = engine.enqueue(42, "live worker", mode="direct")
            claimed = engine.claim(running.id, "conformance-worker", os.getpid())
            assert claimed is not None
            recorded = engine.record_terminal_evidence(
                claimed.id,
                claimed.worker_id,
                TaskTerminalEvidence(status="completed", result="done"),
            )
            assert recorded is not None

            result = engine.reconcile(
                TaskReconciliationRequest(
                    recorded.id,
                    recorded.worker_id,
                    recorded.worker_heartbeat_at,
                )
            )

            self.assertEqual(result.outcome, "no_op")
            self.assertIsNotNone(engine.inspect().running)
            engine.finalize(
                claimed.id,
                "completed",
                result="done",
                worker_id=claimed.worker_id,
            )

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

    def test_conformance_workflow_persists_advertised_request_features(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            epoch = self.begin_fencing_epoch(root)
            engine = self.create_workflow(root, epoch=epoch)
            features = workflow_features(engine)
            options: dict[str, Any] = {}
            if WORKFLOW_FEATURE_STRUCTURED_METADATA in features:
                options["extension_metadata"] = {
                    "request_id": "workflow-conformance",
                    "revision": 1,
                }
            if WORKFLOW_FEATURE_ARTIFACT_REFERENCES in features:
                options["extension_artifact_refs"] = (
                    ExtensionArtifactReference(
                        "input",
                        "conformance/request.json",
                        "application/json",
                    ),
                )
            if WORKFLOW_FEATURE_EXECUTION_LANES in features:
                options["execution_lane"] = (
                    "extension:conformance:workflow-conformance"
                )
            queued = engine.enqueue(
                42,
                "persist optional workflow features",
                context_source="extension:conformance",
                **options,
            )
            if options:
                with self.assertRaises(ValueError):
                    engine.enqueue(
                        42,
                        "reject unnamespaced extension payload",
                        **options,
                    )
            restarted = self.create_workflow(root, epoch=epoch)
            recovered = restarted.find(queued.id)

        self.assertIsNotNone(recovered)
        assert recovered is not None
        if WORKFLOW_FEATURE_STRUCTURED_METADATA in features:
            self.assertEqual(
                recovered.extension_metadata,
                options["extension_metadata"],
            )
        if WORKFLOW_FEATURE_ARTIFACT_REFERENCES in features:
            self.assertEqual(
                recovered.extension_artifact_refs,
                options["extension_artifact_refs"],
            )
        if WORKFLOW_FEATURE_EXECUTION_LANES in features:
            self.assertEqual(
                recovered.execution_lane,
                options["execution_lane"],
            )

    def _new_engine(self, root: Path) -> WorkflowEngine:
        epoch = self.begin_fencing_epoch(root)
        engine = self.create_workflow(root, epoch=epoch)
        self.assertIsInstance(engine, WorkflowEngine)
        return engine
