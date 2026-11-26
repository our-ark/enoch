"""Replying to a task's chat message points the conversation at that task.

A reply to a task's start line, its legacy status block or its final result
message maps to the task (and its review) through the recorded message ids.
The mapping is data for the conversation turn, never authorization; a reply
to the pinned task board, or to anything unknown, behaves exactly as before.
"""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app import core
from enoch.app.conversation import ACTION_END, ACTION_START
from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app.task_board import TaskBoardState, save_task_board_state
from enoch.identity import load_identity
from enoch.providers import ChatEvent
from enoch.tasks.queue import (
    TaskJob,
    begin_next_task,
    complete_task,
    enqueue_task,
    fail_task,
    find_task_by_message,
    pause_task,
    record_task_result_message,
    record_task_status_message,
    task_queue_path,
    task_queue_status,
)


REVIEW = "https://github.com/our-ark/enoch/pull/321"


def _action(command: str, argument: str = "") -> str:
    return ACTION_START + json.dumps({"command": command, "argument": argument}) + ACTION_END


class TaskJobFieldTests(unittest.TestCase):
    def test_result_message_id_defaults_to_none(self) -> None:
        job = TaskJob(id=1, chat_id=42, text="work", created_at="")

        self.assertIsNone(job.result_message_id)

    def test_queue_written_without_the_field_still_loads(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            enqueue_task(42, "older work", root)
            path = task_queue_path(root)
            data = json.loads(path.read_text())
            for job in data["pending"]:
                job.pop("result_message_id", None)
            path.write_text(json.dumps(data))

            pending = task_queue_status(root).pending

        self.assertEqual(len(pending), 1)
        self.assertIsNone(pending[0].result_message_id)


class QueueMessageLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_result_message_is_recorded_on_a_finished_task(self) -> None:
        job = enqueue_task(42, "finished work", self.root)
        begin_next_task(self.root)
        complete_task(job.id, self.root, result="Done.")

        record_task_result_message(job.id, 77, self.root)

        finished = task_queue_status(self.root).history[-1]
        self.assertEqual(finished.result_message_id, 77)
        self.assertEqual(find_task_by_message(77, self.root).id, job.id)

    def test_recording_an_unknown_task_changes_nothing(self) -> None:
        job = enqueue_task(42, "work", self.root)
        before = task_queue_path(self.root).read_text()

        record_task_result_message(job.id + 10, 77, self.root)

        self.assertEqual(task_queue_path(self.root).read_text(), before)

    def test_lookup_covers_running_pending_paused_and_history(self) -> None:
        done = enqueue_task(42, "done work", self.root)
        begin_next_task(self.root)
        record_task_status_message(done.id, 10, self.root)
        complete_task(done.id, self.root, result=f"Opened {REVIEW}")
        record_task_result_message(done.id, 11, self.root)

        paused = enqueue_task(42, "paused work", self.root)
        begin_next_task(self.root)
        record_task_status_message(paused.id, 20, self.root)
        pause_task(paused.id, self.root, "usage limit")

        running = enqueue_task(42, "running work", self.root)
        pending = enqueue_task(42, "pending work", self.root)
        # The paused task blocks nothing here; start the next pending task.
        started = begin_next_task(self.root)
        self.assertEqual(started.id, running.id)
        record_task_status_message(running.id, 30, self.root)
        record_task_status_message(pending.id, 40, self.root)

        status = task_queue_status(self.root)
        self.assertEqual(status.running.id, running.id)
        self.assertEqual([job.id for job in status.pending], [pending.id])
        self.assertEqual([job.id for job in status.paused], [paused.id])

        for message_id, expected in (
            (10, done.id),
            (11, done.id),
            (20, paused.id),
            (30, running.id),
            (40, pending.id),
        ):
            with self.subTest(message_id=message_id):
                found = find_task_by_message(message_id, self.root, chat_id=42)
                self.assertIsNotNone(found)
                self.assertEqual(found.id, expected)

        self.assertEqual(find_task_by_message(11, self.root).review_urls, (REVIEW,))

    def test_lookup_misses_unknown_ids_and_other_conversations(self) -> None:
        job = enqueue_task(42, "work", self.root)
        record_task_status_message(job.id, 10, self.root)

        self.assertIsNone(find_task_by_message(None, self.root))
        self.assertIsNone(find_task_by_message(99, self.root))
        self.assertIsNone(find_task_by_message(10, self.root, chat_id=43))
        # Providers may report ids as strings; they still match.
        self.assertEqual(find_task_by_message("10", self.root, chat_id="42").id, job.id)


class _Chat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edits.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _Runtime:
    name = "reply-target-runtime"
    provider_kind = "runtime"
    config_section = "reply-target-runtime"

    def __init__(self) -> None:
        self.respond = Mock(return_value="Noted.")

    def act_in_session(self, identity, message, **kwargs):
        return "task response"

    def model_summary(self, root=None):
        return "reply target runtime"

    def model_options(self):
        return ()

    def reset_usage(self):
        return None


class _AppTestCase(unittest.TestCase):
    def setUp(self) -> None:
        for target in (
            "enoch.app.core._sync_session_activity",
            "enoch.app.core.log_conversation_turn",
            "enoch.app.core.log_system_event",
            "enoch.app.core.ensure_long_term_memory",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        snapshot = patch(
            "enoch.app.core.EnochApplication._resolve_task_context_snapshot",
            return_value=TaskContextSnapshot(context="", source=""),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        # Tests drive jobs by hand; a real background worker would keep
        # writing into the temporary state directory while it is removed.
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _Chat()
        self.runtime = _Runtime()
        self.app = EnochApplication(
            load_identity(),
            self.root,
            self.chat,
            runtime=self.runtime,
        )
        self._cursor = 0
        # The conversation request (message plus reply context) as built in
        # _natural_with_session, before startup context or wrapper blocks.
        self.requests: list[str] = []
        turn_prompt = core.conversation_turn_prompt

        def capture(message, **kwargs):
            self.requests.append(message)
            return turn_prompt(message, **kwargs)

        capture_patch = patch("enoch.app.core.conversation_turn_prompt", side_effect=capture)
        capture_patch.start()
        self.addCleanup(capture_patch.stop)

    def activate_board(self, message_id: int = 9999) -> None:
        save_task_board_state(
            TaskBoardState(enabled=True, chat_id=42, message_id=message_id, last_digest="x"),
            self.root,
        )

    def event(self, text: str, **changes) -> ChatEvent:
        self._cursor += 1
        values = {
            "cursor": self._cursor,
            "conversation_id": 42,
            "message_id": 1000 + self._cursor,
            "text": text,
        }
        values.update(changes)
        return ChatEvent(**values)

    def run_queued_task(self, text: str, reply: str) -> TaskJob:
        job = enqueue_task(42, text, self.root)
        started = begin_next_task(self.root)
        assert started is not None and started.id == job.id
        with patch.object(self.app, "_run_direct_work", return_value=reply):
            self.app._run_task_job(started)
        return self.app.workflow.find(job.id)

    def prompts(self) -> list[str]:
        return list(self.requests)


class FinalMessageRecordingTests(_AppTestCase):
    def test_compact_mode_records_start_line_and_final_message(self) -> None:
        self.activate_board()

        finished = self.run_queued_task("Fix the bug", f"Opened {REVIEW}")

        self.assertEqual(
            [text for _chat, text in self.chat.sent],
            ["▶️ #1 started: Fix the bug", f"✅ #1 completed · {REVIEW}\nOpened {REVIEW}"],
        )
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.status_message_id, 1)
        self.assertEqual(finished.result_message_id, 2)

    def test_legacy_mode_records_status_block_and_final_message(self) -> None:
        finished = self.run_queued_task("Fix the bug", "Done.")

        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual(finished.status_message_id, 1)
        self.assertEqual(finished.result_message_id, 2)

    def test_failed_delivery_records_no_final_message(self) -> None:
        with patch.object(self.app, "_safe_send_message_id", return_value=None):
            finished = self.run_queued_task("Fix the bug", "Done.")

        self.assertIsNone(finished.result_message_id)


class ReplyTargetPromptTests(_AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_board()
        self.task = self.run_queued_task("Add a reply-routing feature", f"Opened {REVIEW}")
        self.runtime.respond.reset_mock()
        self.requests.clear()

    def test_reply_to_final_message_adds_the_task_data_block(self) -> None:
        self.app.handle_event(
            self.event("what went wrong?", replied_message_id=self.task.result_message_id)
        )

        prompt = self.prompts()[0]
        self.assertIn(
            "Reply target (data, not authorization): the human replied to the chat message "
            "for task #1.",
            prompt,
        )
        self.assertIn("Task #1 status: completed", prompt)
        self.assertIn("Request summary: Add a reply-routing feature", prompt)
        self.assertIn(f"Review URLs: {REVIEW}", prompt)

    def test_reply_to_start_line_adds_the_task_data_block(self) -> None:
        self.app.handle_event(
            self.event("also add a test", replied_message_id=self.task.status_message_id)
        )

        self.assertIn("Reply target (data, not authorization)", self.prompts()[0])
        self.assertIn("task #1.", self.prompts()[0])

    def test_unmapped_reply_matches_a_plain_message(self) -> None:
        self.app.handle_event(self.event("retry it"))
        self.app.handle_event(self.event("retry it", replied_message_id=555))

        plain, unmapped = self.prompts()
        self.assertNotIn("Reply target", plain)
        self.assertEqual(plain, unmapped)

    def test_reply_to_the_task_board_is_not_a_task_reply(self) -> None:
        self.app.handle_event(self.event("retry it"))
        self.app.handle_event(self.event("retry it", replied_message_id=9999))

        plain, board = self.prompts()
        self.assertNotIn("Reply target", board)
        self.assertEqual(plain, board)

    def test_board_message_id_wins_over_a_task_with_the_same_id(self) -> None:
        self.activate_board(message_id=self.task.result_message_id)

        self.app.handle_event(
            self.event("merge it", replied_message_id=self.task.result_message_id)
        )

        self.assertNotIn("Reply target", self.prompts()[0])

    def test_reply_resolves_through_the_normal_task_operation(self) -> None:
        self.runtime.respond = Mock(side_effect=[_action("task", "retry 1"), "Retrying #1."])

        with patch.object(self.app, "_retry_task", return_value="Task accepted") as retry:
            self.app.handle_event(
                self.event("retry it", replied_message_id=self.task.result_message_id)
            )

        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[0], 1)
        self.assertIn("Reply target (data, not authorization)", self.runtime.respond.call_args_list[0].args[1])

    def test_failed_task_reply_reports_its_status(self) -> None:
        job = enqueue_task(42, "Broken work", self.root)
        begin_next_task(self.root)
        record_task_status_message(job.id, 500, self.root)
        fail_task(job.id, self.root, "Tests failed.")

        self.app.handle_event(self.event("what went wrong?", replied_message_id=500))

        self.assertIn(f"Task #{job.id} status: failed", self.prompts()[0])
        self.assertIn("Review URLs: none", self.prompts()[0])


if __name__ == "__main__":
    unittest.main()
