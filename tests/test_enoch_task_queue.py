from pathlib import Path
import sys
import threading
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.task_queue import (
    begin_direct_task,
    begin_next_task,
    cancel_task,
    cancel_running_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    fail_task,
    recover_interrupted_task,
    record_task_result,
    record_task_status_message,
    task_result_has_pull_request,
    task_queue_status,
)
from enoch import task_queue


class EnochTaskQueueTests(unittest.TestCase):
    def test_begin_direct_task_creates_running_job_without_pending(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            running = begin_direct_task(42, "ship it now", root)
            status = task_queue_status(root)

        self.assertEqual(running.id, 1)
        self.assertEqual(running.status, "running")
        self.assertEqual(status.running, running)
        self.assertEqual(status.pending, ())

    def test_begin_direct_task_refuses_existing_running_job(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            begin_direct_task(42, "first", root)

            with self.assertRaisesRegex(RuntimeError, "already running"):
                begin_direct_task(42, "second", root)

    def test_cancel_pending_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "first task", root)
            second = enqueue_task(42, "second task", root)

            cancelled = cancel_task(first.id, root)
            status = task_queue_status(root)

        self.assertEqual(cancelled.id, first.id)
        self.assertEqual(status.pending, (second,))
        self.assertEqual(status.history[-1].id, first.id)
        self.assertEqual(status.history[-1].status, "cancelled")

    def test_enqueue_task_front_runs_before_existing_pending_tasks(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "first task", root)
            second = enqueue_task(42, "second task", root)
            urgent = enqueue_task_front(
                42,
                "urgent task",
                root,
                context="Use the urgent context.",
                context_source="chat-snapshot",
            )

            status = task_queue_status(root)
            running = begin_next_task(root)

        self.assertEqual([job.id for job in status.pending], [urgent.id, first.id, second.id])
        self.assertEqual(running.id, urgent.id)
        self.assertEqual(running.context, "Use the urgent context.")
        self.assertEqual(running.context_source, "chat-snapshot")

    def test_complete_running_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            record_task_status_message(queued.id, 2001, root)
            running = begin_next_task(root)

            complete_task(running.id, root, result="Done.")
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].status_message_id, 2001)
        self.assertEqual(status.history[-1].result, "Done.")
        self.assertEqual(status.history[-1].pr_urls, ())

    def test_queued_task_preserves_context_snapshot_through_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(
                42,
                "do it",
                root,
                context="Build the reminders feature discussed earlier.",
                context_source="chat-snapshot",
            )

            running = begin_next_task(root)
            complete_task(running.id, root, result="Done.")
            status = task_queue_status(root)

        self.assertEqual(queued.context, "Build the reminders feature discussed earlier.")
        self.assertEqual(running.context, queued.context)
        self.assertEqual(running.context_source, "chat-snapshot")
        self.assertEqual(status.history[-1].context, queued.context)
        self.assertEqual(status.history[-1].context_source, "chat-snapshot")

    def test_direct_task_preserves_context_snapshot(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            running = begin_direct_task(
                42,
                "do it now",
                root,
                context="Use the approved high reasoning config.",
                context_source="chat-snapshot",
            )
            complete_task(running.id, root, result="Done.")
            status = task_queue_status(root)

        self.assertEqual(running.context, "Use the approved high reasoning config.")
        self.assertEqual(status.history[-1].context, running.context)
        self.assertEqual(status.history[-1].context_source, "chat-snapshot")

    def test_complete_running_task_records_pr_urls_in_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)
            result = "Opened pull request: https://github.com/our-ark/enoch/pull/3"

            complete_task(running.id, root, result=result)
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].pr_urls, ("https://github.com/our-ark/enoch/pull/3",))

    def test_enqueue_during_completion_preserves_both_updates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            running = begin_direct_task(42, "running job", root)
            original_write = task_queue._write_queue
            completion_write_started = threading.Event()
            enqueue_started = threading.Event()

            def delayed_write(data, root_arg=None):
                if threading.current_thread().name == "complete-thread":
                    completion_write_started.set()
                    self.assertTrue(enqueue_started.wait(5))
                original_write(data, root_arg)

            task_queue._write_queue = delayed_write
            try:
                complete_thread = threading.Thread(
                    target=lambda: complete_task(running.id, root, result="done"),
                    name="complete-thread",
                )
                enqueue_result = []
                enqueue_thread = threading.Thread(
                    target=lambda: (enqueue_started.set(), enqueue_result.append(enqueue_task(42, "queued", root))),
                    name="enqueue-thread",
                )

                complete_thread.start()
                self.assertTrue(completion_write_started.wait(5))
                enqueue_thread.start()
                complete_thread.join(5)
                enqueue_thread.join(5)
            finally:
                task_queue._write_queue = original_write

            status = task_queue_status(root)

        self.assertFalse(complete_thread.is_alive())
        self.assertFalse(enqueue_thread.is_alive())
        self.assertEqual([job.id for job in status.history], [running.id])
        self.assertEqual([job.id for job in status.pending], [enqueue_result[0].id])

    def test_fail_running_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)

            fail_task(running.id, root, result="GitHub rejected the push.")
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "failed")
        self.assertEqual(status.history[-1].result, "GitHub rejected the push.")

    def test_cancel_running_task_moves_it_to_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            running = begin_direct_task(42, "stop me", root)

            cancelled = cancel_running_task(root, result="Stopped by /stop.")
            status = task_queue_status(root)

        self.assertEqual(cancelled.id, running.id)
        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, running.id)
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertEqual(status.history[-1].result, "Stopped by /stop.")

    def test_recover_interrupted_task_requeues_with_status_message(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "resume me", root, context="Resume with chat context.")
            record_task_status_message(queued.id, 2001, root)
            begin_next_task(root)

            recovered = recover_interrupted_task(root)
            status = task_queue_status(root)

        self.assertEqual(recovered.id, queued.id)
        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.status_message_id, 2001)
        self.assertEqual(recovered.context, "Resume with chat context.")
        self.assertEqual(status.pending[0].id, queued.id)

    def test_recover_interrupted_task_with_pr_url_completes_it(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)
            result = "Opened pull request: https://github.com/our-ark/enoch/pull/3"
            record_task_result(running.id, result, root)

            recovered = recover_interrupted_task(root)
            status = task_queue_status(root)

        self.assertEqual(recovered.id, queued.id)
        self.assertEqual(recovered.status, "completed")
        self.assertIsNone(status.running)
        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertTrue(task_result_has_pull_request(status.history[-1].result))
        self.assertEqual(status.history[-1].pr_urls, ("https://github.com/our-ark/enoch/pull/3",))


if __name__ == "__main__":
    unittest.main()
