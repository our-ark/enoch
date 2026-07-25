from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from enoch.app.core import EnochApplication
from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.app.models import WorkOutcome
from enoch.identity import load_identity
from enoch.profiles import AgentProfile, CommandSpec
from enoch.providers import ChatEvent, RuntimeResult
from enoch.workflows import (
    WORKFLOW_API_VERSION,
    LocalWorkflowEngine,
    WorkflowEngine,
    WorkflowEngineError,
)


class WorkflowEngineTests(unittest.TestCase):
    def test_local_engine_implements_versioned_lifecycle_contract(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            epoch = begin_daemon_epoch(root, provider="test")
            engine = LocalWorkflowEngine(root, epoch=epoch)

            queued = engine.enqueue(42, "do the work")
            started = engine.start_next()
            assert started is not None
            claimed = engine.claim(started.id, "worker-1", 123)
            assert claimed is not None
            heartbeat = engine.heartbeat(claimed.id, "worker-1")
            completed = engine.finalize(
                claimed.id,
                "completed",
                result="done",
                worker_id="worker-1",
            )
            status = engine.inspect()

        self.assertIsInstance(engine, WorkflowEngine)
        self.assertEqual(engine.api_version, WORKFLOW_API_VERSION)
        self.assertEqual(started.id, queued.id)
        self.assertTrue(heartbeat.worker_heartbeat_at)
        self.assertEqual(completed.status, "completed")
        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].result, "done")

    def test_engine_rejects_mutation_from_stale_daemon_epoch(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = begin_daemon_epoch(root, provider="test")
            stale = LocalWorkflowEngine(root, epoch=first)
            begin_daemon_epoch(root, provider="test")

            with self.assertRaises(StaleDaemonEpoch):
                stale.enqueue(42, "must not be queued")

        self.assertEqual(stale.inspect().pending, ())

    def test_application_accepts_injected_runtime_and_workflow(self) -> None:
        def research(command):
            job = command.enqueue_task(f"Research {command.argument}")
            return f"Queued research task #{job.id}."

        profile = AgentProfile(
            name="researcher",
            commands=(
                CommandSpec(
                    name="research",
                    summary="queue research",
                    handler=research,
                ),
            ),
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workflow_root = root / "workflow-state"
            chat = _Chat()
            runtime = _Runtime()
            epoch = begin_daemon_epoch(root, provider="test")
            workflow = _RecordingWorkflow(workflow_root)
            app = EnochApplication(
                load_identity(),
                root,
                chat,
                runtime=runtime,
                profile=profile,
                daemon_epoch=epoch,
                workflow=workflow,
            )

            app.handle_event(
                ChatEvent(
                    cursor=1,
                    conversation_id=42,
                    message_id=1,
                    text="/research durable workflows",
                )
            )
            app.handle_event(
                ChatEvent(
                    cursor=2,
                    conversation_id=42,
                    message_id=2,
                    text="/queue",
                )
            )
            queue_report = chat.sent[-1][1]
            running = workflow.start_next()
            assert running is not None
            with patch.object(
                app,
                "_run_direct_work",
                return_value=WorkOutcome.completed("research complete"),
            ):
                app._run_task_job(running)
            completed = workflow.inspect().history[-1]
            core_queue_absent = not (root / ".enoch" / "task_queue.json").exists()

        self.assertIs(app.runtime, runtime)
        self.assertIs(app.workflow, workflow)
        self.assertTrue(core_queue_absent)
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.trigger, "/research")
        self.assertIn("Research durable workflows", queue_report)
        self.assertEqual(
            workflow.operations,
            ["recover", "enqueue", "claim", "finalize:completed"],
        )

    def test_application_rejects_unsupported_workflow_version(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            engine = _FutureWorkflow(root)

            with self.assertRaisesRegex(
                WorkflowEngineError,
                "uses API version",
            ):
                EnochApplication(
                    load_identity(),
                    root,
                    _Chat(),
                    workflow=engine,
                )


class _RecordingWorkflow(LocalWorkflowEngine):
    def __init__(self, root, *, epoch=None):
        super().__init__(root, epoch=epoch)
        self.operations: list[str] = []

    def enqueue(self, conversation_id, request, *, mode="queued", **options):
        self.operations.append("enqueue")
        return super().enqueue(
            conversation_id,
            request,
            mode=mode,
            **options,
        )

    def claim(self, task_id, worker_id, worker_pid):
        self.operations.append("claim")
        return super().claim(task_id, worker_id, worker_pid)

    def finalize(self, task_id, status, **options):
        self.operations.append(f"finalize:{status}")
        return super().finalize(task_id, status, **options)

    def recover(self):
        self.operations.append("recover")
        return super().recover()


class _FutureWorkflow(LocalWorkflowEngine):
    api_version = WORKFLOW_API_VERSION + 1


class _Chat:
    name = "test"
    provider_kind = "chat"
    allowed_conversation_id = 42

    def __init__(self) -> None:
        self.sent = []
        self.edited = []

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edited.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _Runtime:
    name = "fake-runtime"
    provider_kind = "runtime"

    def respond(self, identity, prompt, **kwargs):
        return RuntimeResult(final_text="fake response")

    def act(self, identity, prompt, **kwargs):
        return RuntimeResult(final_text="fake action")

    def act_in_session(self, identity, prompt, **kwargs):
        return RuntimeResult(final_text="fake action")

    def model_summary(self, root=None):
        return "fake runtime"

    def model_options(self):
        return ("fake",)

    def reset_usage(self):
        return None


if __name__ == "__main__":
    unittest.main()
