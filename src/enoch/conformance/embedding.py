from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from enoch.app.epoch import begin_daemon_epoch
from enoch.embedding import (
    EMBEDDING_API_VERSION,
    EmbeddedApplication,
    EvolveCommandResult,
    PendingTaskExecutionResult,
    StaleDaemonEpoch,
)


class EmbeddingConformanceMixin:
    """Hosts supply an isolated application and a hermetic pending-task fixture."""

    embedding_conversation_id = "room-1"

    def create_embedded_application(self, root: Path) -> EmbeddedApplication:
        raise NotImplementedError

    def enqueue_embedding_task(self, app: EmbeddedApplication) -> int:
        raise NotImplementedError

    def test_conformance_embedding_command_result(self) -> None:
        with TemporaryDirectory() as temp:
            app = self.create_embedded_application(Path(temp))
            self.assertIsInstance(app, EmbeddedApplication)
            self.assertEqual(app.embedding_api_version, EMBEDDING_API_VERSION)
            result = app.handle_evolve_command(self.embedding_conversation_id)
            self.assertIsInstance(result, EvolveCommandResult)
            self.assertEqual(result.conversation_id, self.embedding_conversation_id)
            self.assertTrue(result.reply)

    def test_conformance_embedding_idle(self) -> None:
        with TemporaryDirectory() as temp:
            app = self.create_embedded_application(Path(temp))
            self.assertEqual(
                app.resume_pending_tasks(), PendingTaskExecutionResult((), "idle"),
            )

    def test_conformance_embedding_bounded_execution(self) -> None:
        with TemporaryDirectory() as temp:
            app = self.create_embedded_application(Path(temp))
            first = self.enqueue_embedding_task(app)
            second = self.enqueue_embedding_task(app)
            self.assertEqual(
                app.resume_pending_tasks(), PendingTaskExecutionResult((first,), "limit"),
            )
            self.assertEqual(
                app.resume_pending_tasks(), PendingTaskExecutionResult((second,), "limit"),
            )

    def test_conformance_embedding_stale_epoch(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            app = self.create_embedded_application(root)
            begin_daemon_epoch(root)
            with self.assertRaises(StaleDaemonEpoch):
                app.handle_evolve_command(self.embedding_conversation_id)
            with self.assertRaises(StaleDaemonEpoch):
                app.resume_pending_tasks()


__all__ = ["EmbeddingConformanceMixin"]
