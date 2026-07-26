from pathlib import Path
import threading
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.core import EnochApplication
from enoch.app.effects import DaemonEffectFence
from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.app.models import WorkOutcome
from enoch.identity import load_identity
from enoch.providers import RuntimeExecutionControl, RuntimeResult


class DaemonEffectFenceTests(unittest.TestCase):
    def test_stale_application_rejects_runtime_and_forge_effects(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = _Runtime()
            forge = MagicMock()
            stale = EnochApplication(
                load_identity(),
                root,
                _Chat(),
                runtime=runtime,
                forge=forge,
            )
            EnochApplication(load_identity(), root, _Chat())

            with self.assertRaises(StaleDaemonEpoch):
                stale._respond_read_only_turn(42, "hello")
            with self.assertRaises(StaleDaemonEpoch):
                stale._pr(42, "merge 12")

        self.assertEqual(runtime.respond_calls, 0)
        forge.merge_pull_request.assert_not_called()

    def test_runtime_is_cancelled_when_a_new_daemon_takes_over(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = begin_daemon_epoch(root, provider="test")
            fence = DaemonEffectFence(
                root,
                first,
                monitor_interval_seconds=0.01,
            )
            started = threading.Event()
            cancellation = threading.Event()
            errors: list[BaseException] = []

            def runtime(control: RuntimeExecutionControl) -> RuntimeResult:
                started.set()
                control.cancellation_event.wait(timeout=2)
                return RuntimeResult(final_text="stopped")

            def invoke() -> None:
                try:
                    fence.run_runtime(
                        runtime,
                        RuntimeExecutionControl(cancellation_event=cancellation),
                    )
                except BaseException as error:
                    errors.append(error)

            worker = threading.Thread(target=invoke)
            worker.start()
            self.assertTrue(started.wait(timeout=1))
            begin_daemon_epoch(root, provider="test")
            worker.join(timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertTrue(cancellation.is_set())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StaleDaemonEpoch)

    def test_stale_task_cannot_finalize_or_send_final_notification(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _Chat()
            app = EnochApplication(load_identity(), root, chat, runtime=_Runtime())
            queued = app.workflow.enqueue(42, "do the work")
            job = app.workflow.start_next()
            assert job is not None

            def takeover(*args, **kwargs):
                begin_daemon_epoch(root, provider="test")
                return WorkOutcome.completed("done")

            with patch.object(app, "_run_direct_work", side_effect=takeover):
                app._run_task_job(job)
            status = app.workflow.inspect()

        self.assertEqual(job.id, queued.id)
        self.assertIsNotNone(status.running)
        self.assertEqual(status.running.id, job.id)
        self.assertEqual(status.history, ())
        self.assertEqual(len(chat.sent), 1)
        self.assertIn("Status: running", chat.sent[0][1])

    def test_bounded_effect_completes_before_takeover(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = begin_daemon_epoch(root, provider="test")
            fence = DaemonEffectFence(root, first)
            effect_started = threading.Event()
            release_effect = threading.Event()
            effect_finished = threading.Event()
            takeover_finished = threading.Event()

            def effect() -> None:
                effect_started.set()
                release_effect.wait(timeout=2)
                effect_finished.set()

            effect_worker = threading.Thread(target=lambda: fence.run(effect))
            effect_worker.start()
            self.assertTrue(effect_started.wait(timeout=1))

            def takeover() -> None:
                begin_daemon_epoch(root, provider="test")
                takeover_finished.set()

            takeover_worker = threading.Thread(target=takeover)
            takeover_worker.start()
            self.assertFalse(takeover_finished.wait(timeout=0.05))
            release_effect.set()
            effect_worker.join(timeout=2)
            takeover_worker.join(timeout=2)

            with self.assertRaises(StaleDaemonEpoch):
                fence.run(lambda: None)

        self.assertTrue(effect_finished.is_set())
        self.assertTrue(takeover_finished.is_set())


class _Chat:
    name = "test"
    provider_kind = "chat"
    allowed_conversation_id = 42

    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    def receive(self, cursor=None):
        return ()

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        return None


class _Runtime:
    name = "fake-runtime"
    provider_kind = "runtime"

    def __init__(self) -> None:
        self.respond_calls = 0

    def respond(self, identity, prompt, **kwargs):
        self.respond_calls += 1
        return RuntimeResult(final_text="fake response")

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
