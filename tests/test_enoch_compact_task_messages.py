"""Per-task chat messages while the pinned task board is active.

When the board is active, a task posts one start line (never edited) and one
final message. When it is not active, the detailed status block behavior must
stay exactly as it was.
"""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app import core
from enoch.app.models import WorkOutcome
from enoch.app.presentation import (
    clip_compact_summary,
    format_compact_task_final_message,
    format_compact_task_queued,
    format_compact_task_start,
    format_task_details,
    format_task_final_message,
    short_request,
)
from enoch.app.task_board import TaskBoardState, save_task_board_state
from enoch.commands import core_command
from enoch.identity import load_identity
from enoch.providers import (
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    ChatEvent,
)
from enoch.tasks.queue import (
    TaskJob,
    begin_next_task,
    enqueue_task,
    fail_task,
    task_queue_status,
)


CONTEXT = "Private brief: Gary asked for compact task messages."
REVIEW = "https://github.com/our-ark/enoch/pull/123"


def _job(**changes) -> TaskJob:
    values = {
        "id": 41,
        "chat_id": 42,
        "text": "Simplify chat messages",
        "created_at": "2026-11-24T13:00:00+00:00",
    }
    values.update(changes)
    return TaskJob(**values)


class CompactFormatterTests(unittest.TestCase):
    def test_short_request_uses_first_line_clipped_like_the_board(self) -> None:
        self.assertEqual(short_request("\n  First   line  \nsecond line"), "First line")
        clipped = short_request("x" * 200)
        self.assertEqual(len(clipped), 80)
        self.assertTrue(clipped.endswith("…"))
        self.assertEqual(short_request(""), "")

    def test_start_line(self) -> None:
        text = format_compact_task_start(41, "Fix the bug\n\nLong details " * 5)

        self.assertEqual(text, "▶️ #41 started: Fix the bug")

    def test_queued_line(self) -> None:
        self.assertEqual(format_compact_task_queued(42), "Queued #42.")
        self.assertEqual(
            format_compact_task_queued(43, retry_of=41),
            "Queued #43 (retry of #41).",
        )

    def test_completed_with_reviews(self) -> None:
        job = _job(
            status="completed",
            result="Opened the PR.",
            review_urls=(REVIEW, "https://github.com/our-ark/enoch/pull/124"),
        )

        text = format_compact_task_final_message(job, "completed", "ignored")

        self.assertEqual(
            text,
            f"✅ #41 completed · {REVIEW} · https://github.com/our-ark/enoch/pull/124\n"
            "Opened the PR.",
        )

    def test_completed_without_review_falls_back_to_the_reply(self) -> None:
        text = format_compact_task_final_message(_job(status="completed"), "completed", "Done.")

        self.assertEqual(text, "✅ #41 completed\nDone.")

    def test_completed_without_any_summary_is_one_line(self) -> None:
        text = format_compact_task_final_message(_job(status="completed"), "completed", "")

        self.assertEqual(text, "✅ #41 completed")

    def test_summary_is_clipped_to_five_lines(self) -> None:
        summary = "\n".join(f"line {index}" for index in range(1, 10))

        clipped = clip_compact_summary(summary)

        self.assertEqual(clipped, "line 1\nline 2\nline 3\nline 4\nline 5…")

    def test_summary_is_clipped_to_five_hundred_characters(self) -> None:
        clipped = clip_compact_summary("word " * 400)

        self.assertLessEqual(len(clipped), 500)
        self.assertTrue(clipped.endswith("…"))
        self.assertEqual(clip_compact_summary("short"), "short")

    def test_failed_uses_failure_code_and_retry_hint(self) -> None:
        job = _job(id=42, status="failed", failure_code="validation_failed", result="Tests failed.")

        self.assertEqual(
            format_compact_task_final_message(job, "failed", "", command_prefix="."),
            "❌ #42 failed: validation_failed · retry with .task retry 42",
        )

    def test_failed_without_code_uses_a_short_reason(self) -> None:
        job = _job(id=42, status="failed", result=("Could not reach the forge. " * 10) + "\nmore")

        text = format_compact_task_final_message(job, "failed", "")

        reason = text.removeprefix("❌ #42 failed: ").removesuffix(" · retry with /task retry 42")
        self.assertTrue(reason.startswith("Could not reach the forge."))
        self.assertLessEqual(len(reason), 80)
        self.assertNotIn("more", reason)

    def test_cancelled(self) -> None:
        job = _job(id=43, status="cancelled", result="Stopped by /stop.")

        self.assertEqual(
            format_compact_task_final_message(job, "cancelled", ""),
            "⏹ #43 cancelled",
        )

    def test_paused_keeps_the_detailed_message(self) -> None:
        job = _job(status="paused", result="Agent runtime unavailable. Use /task resume 41.")

        self.assertEqual(
            format_compact_task_final_message(job, "paused", "", task_label="Research task"),
            format_task_final_message(job, "paused", "", task_label="Research task"),
        )

    def test_task_details_for_a_failed_task(self) -> None:
        job = _job(
            id=42,
            status="failed",
            text="Fix the bug\nwith all of these details",
            result="Validation failed in tests/test_x.py",
            failure_code="validation_failed",
            failure_class="permanent",
            attempt=1,
            max_attempts=3,
            review_urls=(REVIEW,),
            context=CONTEXT,
            context_source="chat-snapshot",
            parent_task_id=40,
        )

        self.assertEqual(
            format_task_details(job),
            "\n".join(
                [
                    "Task #42",
                    "Status: failed",
                    "Retry of: #40",
                    "",
                    "Request:",
                    "Fix the bug\nwith all of these details",
                    "",
                    "Review URLs:",
                    f"- {REVIEW}",
                    "",
                    "Failure: validation_failed (permanent)",
                    "Attempts: 1/3",
                    "Validation failed in tests/test_x.py",
                    "",
                    "Context snapshot (source: chat-snapshot):",
                    CONTEXT,
                ]
            ),
        )

    def test_task_details_without_context(self) -> None:
        text = format_task_details(_job(status="pending"))

        self.assertIn("Review URLs:\n- none", text)
        self.assertNotIn("Failure:", text)
        self.assertTrue(text.endswith("Context snapshot (source: none):\n(none)"))


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
            return_value=TaskContextSnapshot(context=CONTEXT, source="chat-snapshot"),
        )
        snapshot.start()
        self.addCleanup(snapshot.stop)
        # Tests drive jobs by hand. A real background worker would keep writing
        # into the temporary state directory while it is being removed.
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        self.start_worker = worker.start()
        self.addCleanup(worker.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _Chat()
        self.app = EnochApplication(load_identity(), self.root, self.chat)
        self._update_id = 0

    def activate_board(self) -> None:
        save_task_board_state(
            TaskBoardState(enabled=True, chat_id=42, message_id=9999, last_digest="x"),
            self.root,
        )

    def send(self, text: str) -> None:
        self._update_id += 1
        self.app.handle_event(
            ChatEvent(
                cursor=self._update_id,
                conversation_id=42,
                message_id=1000 + self._update_id,
                text=text,
            )
        )

    def queue_and_begin(self, text: str = "add queued work") -> TaskJob:
        self.send(f"/task {text}")
        job = begin_next_task(self.root)
        assert job is not None
        return job

    def texts(self) -> list[str]:
        return [text for _chat_id, text in self.chat.sent]


class ModeSelectionTests(_AppTestCase):
    def test_compact_only_while_the_board_is_active(self) -> None:
        self.assertFalse(self.app._compact_task_messages())
        save_task_board_state(TaskBoardState(enabled=False, chat_id=42, message_id=1), self.root)
        self.assertFalse(self.app._compact_task_messages())
        save_task_board_state(TaskBoardState(enabled=True, chat_id=42, message_id=None), self.root)
        self.assertFalse(self.app._compact_task_messages())
        self.activate_board()
        self.assertTrue(self.app._compact_task_messages())

    def test_unreadable_board_state_keeps_detailed_messages(self) -> None:
        self.activate_board()
        (self.root / ".enoch" / "task_board.json").write_text("{not json", encoding="utf-8")

        self.assertFalse(self.app._compact_task_messages())


class InactiveBoardTests(_AppTestCase):
    """Without an active board, messages stay byte-for-byte as before."""

    def test_task_lifecycle_messages_are_unchanged(self) -> None:
        save_task_board_state(TaskBoardState(enabled=False, chat_id=42, message_id=1), self.root)
        job = self.queue_and_begin()

        def run_task(*_args, **_kwargs):
            self.app._send_progress(42, 60, "workspace-write")
            return "Done with the task."

        with patch.object(self.app, "_run_direct_work", side_effect=run_task):
            self.app._run_task_job(job)

        queued_block = "\n".join(
            [
                "Task #1",
                "Status: queued",
                "Time: <1 minute",
                "Latest update: Queued at position 1.",
                "Reviews published:",
                "- none",
                "",
                "Request:",
                "add queued work",
                "",
                "Conversation context snapshot:",
                CONTEXT,
            ]
        )
        final = "\n".join(
            [
                "Task #1 final update",
                "Final status: completed",
                "Review URL:",
                "- none",
                "Result summary:",
                "Done with the task.",
            ]
        )
        self.assertEqual(self.texts(), [queued_block, final])
        edits = [text for _chat_id, _message_id, text in self.chat.edits]
        self.assertEqual(
            edits,
            [
                queued_block.replace("Status: queued", "Status: running").replace(
                    "Queued at position 1.", "Starting queued task #1."
                ),
                queued_block.replace("Status: queued", "Status: running").replace(
                    "Queued at position 1.",
                    "Still working after 1 minute: editing her code body.",
                ),
                queued_block.replace("Status: queued", "Status: completed").replace(
                    "Queued at position 1.", "Completed. Final summary sent below."
                ),
            ],
        )
        self.assertEqual({message_id for _chat_id, message_id, _text in self.chat.edits}, {1})

    def test_progress_without_status_message_still_sends_still_working(self) -> None:
        self.app._send_progress(42, 60, "workspace-write")

        self.assertEqual(
            self.texts(),
            ["Enoch is still working after 1 minute: editing her code body."],
        )


class CompactStartAndQueueTests(_AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_board()

    def test_task_enqueue_replies_with_one_short_line(self) -> None:
        self.send("/task add queued work\nwith a second line")

        self.assertEqual(self.texts(), ["Queued #1."])
        self.assertEqual(self.chat.edits, [])
        pending = task_queue_status(self.root).pending[0]
        self.assertIsNone(pending.status_message_id)
        self.assertEqual(pending.context, CONTEXT)

    def test_do_queued_behind_a_running_task_replies_with_one_short_line(self) -> None:
        self.queue_and_begin("existing work")

        with patch.object(self.app, "_run_direct_work") as run_direct_work:
            self.send("/do new work")

        run_direct_work.assert_not_called()
        self.assertEqual(self.texts(), ["Queued #1.", "Queued #2."])
        self.assertEqual(self.chat.edits, [])
        self.assertFalse(any(CONTEXT in text for text in self.texts()))

    def test_do_sends_only_the_start_line_and_records_it(self) -> None:
        started: list[tuple[TaskJob, str]] = []

        with patch.object(
            self.app,
            "_start_direct_work_worker",
            side_effect=lambda job, *, session_key: started.append((job, session_key)),
        ):
            self.send("/do fix the bug\nwith a lot more detail")

        self.assertEqual(self.texts(), ["▶️ #1 started: fix the bug"])
        job, session_key = started[0]
        self.assertEqual(task_queue_status(self.root).running.status_message_id, 1)

        with patch.object(self.app, "_run_direct_work", return_value="Fixed it."):
            self.app._run_direct_task_job(job, session_key=session_key)

        self.assertEqual(self.texts(), ["▶️ #1 started: fix the bug", "✅ #1 completed\nFixed it."])
        self.assertEqual(self.chat.edits, [])

    def test_task_run_sends_start_line_then_final_without_edits(self) -> None:
        job = self.queue_and_begin("add queued work " + "with many details " * 10)

        def run_task(*_args, **_kwargs):
            self.app._send_step_update(42, "Editing files.")
            self.app._send_progress(42, 60, "workspace-write")
            self.app._update_work_status("Publishing.", review_url=REVIEW)
            return f"Opened {REVIEW}"

        with patch.object(self.app, "_run_direct_work", side_effect=run_task):
            self.app._run_task_job(job)
        completed = task_queue_status(self.root).history[-1]

        self.assertEqual(
            self.texts(),
            [
                "Queued #1.",
                "▶️ #1 started: " + short_request("add queued work " + "with many details " * 10),
                f"✅ #1 completed · {REVIEW}\nOpened {REVIEW}",
            ],
        )
        self.assertEqual(self.chat.edits, [])
        self.assertFalse(any("still working" in text for text in self.texts()))
        self.assertFalse(any(CONTEXT in text for text in self.texts()))
        self.assertEqual(completed.status_message_id, 2)
        self.assertEqual(completed.review_urls, (REVIEW,))


class CompactFinalMessageTests(_AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_board()
        self.job = self.queue_and_begin()

    def run_job(self, side_effect) -> None:
        with patch.object(self.app, "_run_direct_work", side_effect=side_effect):
            self.app._run_task_job(self.job)

    def test_completed_without_review(self) -> None:
        self.run_job(lambda *_args, **_kwargs: "Done.\n" + "\n".join(f"step {n}" for n in range(9)))

        self.assertEqual(
            self.texts()[-1],
            "✅ #1 completed\nDone.\nstep 0\nstep 1\nstep 2\nstep 3…",
        )
        self.assertEqual(self.chat.edits, [])

    def test_failed(self) -> None:
        self.run_job(
            lambda *_args, **_kwargs: WorkOutcome.failure(
                "Validation failed.",
                code="validation_failed",
                failure_class="permanent",
            )
        )

        self.assertEqual(self.texts()[-1], "❌ #1 failed: validation_failed · retry with /task retry 1")
        self.assertEqual(self.chat.edits, [])

    def test_cancelled(self) -> None:
        self.run_job(AgentRuntimeCancelled("Enoch cancelled the active task."))

        self.assertEqual(self.texts()[-1], "⏹ #1 cancelled")
        self.assertEqual(self.chat.edits, [])

    def test_paused_keeps_the_detailed_message(self) -> None:
        self.run_job(AgentRuntimeAccessUnavailable("Quota exhausted."))
        paused = task_queue_status(self.root).paused[0]

        self.assertEqual(self.texts()[-1], format_task_final_message(paused, "paused", ""))
        self.assertTrue(self.texts()[-1].startswith("Task #1 paused\n"))
        self.assertIn("/task resume", self.texts()[-1])
        self.assertEqual(self.chat.edits, [])

    def test_transient_retry_sends_nothing(self) -> None:
        with patch("enoch.app.core.automatic_retry_delay_seconds", return_value=0):
            self.run_job(lambda *_args, **_kwargs: "Enoch could not continue: connection reset by peer.")
            self.assertEqual(self.texts(), ["Queued #1.", "▶️ #1 started: add queued work"])
            self.assertEqual(self.chat.edits, [])

            second = begin_next_task(self.root)
            assert second is not None
            with patch.object(self.app, "_run_direct_work", return_value="Recovered."):
                self.app._run_task_job(second)

        self.assertEqual(
            self.texts(),
            ["Queued #1.", "▶️ #1 started: add queued work", "✅ #1 completed\nRecovered."],
        )
        self.assertEqual(self.chat.edits, [])

    def test_final_message_keeps_attachments_and_rejection_note(self) -> None:
        attachment = object()

        def run_task(*_args, **_kwargs):
            core._CURRENT_OUTPUT_ATTACHMENTS.set((attachment,))
            core._CURRENT_OUTPUT_REJECTIONS.set(2)
            return "Done."

        with patch.object(self.app, "_safe_send_message", return_value="") as send:
            self.run_job(run_task)

        send.assert_called_once()
        self.assertEqual(
            send.call_args.args[1],
            "✅ #1 completed\nDone.\n\n[Attachment not sent: 2 runtime file references "
            "failed the outbound safety checks.]",
        )
        self.assertEqual(send.call_args.kwargs["attachments"], (attachment,))
        self.assertEqual(send.call_args.kwargs["notification_key"], "task:1:final")

    def test_stop_does_not_edit_the_start_line(self) -> None:
        def run_task(*_args, **_kwargs):
            self.assertEqual(self.app._stop_running_job(), "Stopped task #1.")
            raise AgentRuntimeCancelled("Enoch cancelled the active task.")

        self.run_job(run_task)

        self.assertEqual(self.chat.edits, [])
        self.assertEqual(self.texts()[-1], "⏹ #1 cancelled")


class CompactRetryAndResumeTests(_AppTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.activate_board()

    def test_manual_retry_replies_with_one_short_line(self) -> None:
        job = self.queue_and_begin()
        fail_task(job.id, self.root, result="Broke.", failure_code="validation_failed")

        self.send("/task retry 1")

        self.assertEqual(self.texts()[-1], "Queued #2 (retry of #1).")
        self.assertEqual(self.chat.edits, [])

    def test_resume_does_not_edit_the_start_line(self) -> None:
        job = self.queue_and_begin()
        with patch.object(
            self.app,
            "_run_direct_work",
            side_effect=AgentRuntimeAccessUnavailable("Quota exhausted."),
        ):
            self.app._run_task_job(job)

        self.send("/task resume 1")

        self.assertEqual(self.texts()[-1], "Resumed 1 task: #1.")
        self.assertEqual(self.chat.edits, [])
        self.start_worker.assert_called_once_with()


class TaskShowTests(_AppTestCase):
    def test_show_running_task_includes_context_snapshot(self) -> None:
        self.activate_board()
        self.queue_and_begin("add queued work " + "with details " * 10)

        self.send("/task show 1")

        reply = self.texts()[-1]
        self.assertTrue(reply.startswith("Task #1\nStatus: running\n"))
        # The full request is shown, not the clipped start-line version.
        self.assertIn("Request:\nadd queued work " + " ".join(["with details"] * 10) + "\n", reply)
        self.assertTrue(reply.endswith(f"Context snapshot (source: chat-snapshot):\n{CONTEXT}"))

    def test_show_pending_task_in_detailed_mode(self) -> None:
        self.send("/task add queued work")

        self.send("/task show #1")

        reply = self.texts()[-1]
        self.assertTrue(reply.startswith("Task #1\nStatus: pending\n"))
        self.assertIn(CONTEXT, reply)
        self.assertEqual(task_queue_status(self.root).pending_count, 1)

    def test_show_history_task_with_failure(self) -> None:
        job = self.queue_and_begin()
        fail_task(
            job.id,
            self.root,
            result="Validation failed.",
            failure_code="validation_failed",
            failure_class="permanent",
        )

        self.send("/task show 1")

        reply = self.texts()[-1]
        self.assertIn("Status: failed", reply)
        self.assertIn("Failure: validation_failed (permanent)\nAttempts: 1/", reply)
        self.assertIn("Validation failed.", reply)
        self.assertIn(CONTEXT, reply)

    def test_show_unknown_and_missing_id(self) -> None:
        self.send("/task show 99")
        self.send("/task show")
        self.send("/task show nope")

        self.assertEqual(
            self.texts(),
            [
                "Enoch has no task #99.",
                "Use /task show <id> to show a task's request and context snapshot.",
                "Use /task show <id> to show a task's request and context snapshot.",
            ],
        )
        self.assertEqual(task_queue_status(self.root).pending_count, 0)

    def test_longer_show_request_is_still_queued_as_work(self) -> None:
        self.send("/task show me the deploy logs")

        self.assertEqual(task_queue_status(self.root).pending[0].text, "show me the deploy logs")

    def test_show_is_in_task_usage(self) -> None:
        command = core_command("task")
        assert command is not None

        self.assertIn("/task show <id> - show a task's status", command.usage_message("/"))


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


if __name__ == "__main__":
    unittest.main()
