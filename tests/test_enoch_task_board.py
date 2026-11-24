from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app.core import EnochApplication
from enoch.app.task_board import (
    TaskBoardState,
    load_task_board_state,
    render_task_board,
    save_task_board_state,
    task_board_path,
)
from enoch.commands import core_command, help_message
from enoch.identity import load_identity
from enoch.providers import ChatEvent, ChatProviderError, MessagePinProvider
from enoch.tasks.queue import TaskJob, TaskQueueStatus


UPDATED_AT = datetime(2026, 11, 24, 14, 5)


def _job(task_id: int, text: str, **changes) -> TaskJob:
    return TaskJob(
        id=task_id,
        chat_id=42,
        text=text,
        created_at="2026-11-24T13:00:00+00:00",
        **changes,
    )


def _status(
    *,
    running: TaskJob | None = None,
    pending: tuple[TaskJob, ...] = (),
    paused: tuple[TaskJob, ...] = (),
    history: tuple[TaskJob, ...] = (),
) -> TaskQueueStatus:
    return TaskQueueStatus(
        pending_count=len(pending),
        paused_count=len(paused),
        running=running,
        pending=pending,
        paused=paused,
        history=history,
    )


class TaskBoardRenderTests(unittest.TestCase):
    def test_empty_board_lists_every_section_as_none(self) -> None:
        rendered = render_task_board(_status(), updated_at=UPDATED_AT)

        self.assertEqual(
            rendered.text,
            "\n".join(
                [
                    "Task board",
                    "",
                    "Running:",
                    "- none",
                    "",
                    "Queued:",
                    "- none",
                    "",
                    "Paused:",
                    "- none",
                    "",
                    "Recent reviews:",
                    "- none",
                    "",
                    "Recent failures:",
                    "- none",
                    "",
                    "Updated 14:05",
                ]
            ),
        )

    def test_renders_running_queued_and_paused_sections(self) -> None:
        started = datetime(2026, 11, 24, 13, 2, tzinfo=timezone.utc)
        status = _status(
            running=_job(7, "Implement the board", status="running", started_at=started.isoformat()),
            pending=(_job(8, "Write docs"), _job(9, "Retry it", parent_task_id=3)),
            paused=(_job(5, "Paused work", status="paused"),),
        )

        text = render_task_board(status, updated_at=UPDATED_AT).text

        started_local = started.astimezone().strftime("%H:%M")
        self.assertIn(
            f"Running:\n- #7 Implement the board (started {started_local})",
            text,
        )
        self.assertIn("Queued:\n- #8 Write docs\n- #9 Retry it (retry of #3)", text)
        self.assertIn("Paused:\n- #5 Paused work", text)
        self.assertNotIn("[pending]", text)
        self.assertNotIn("elapsed", text.lower())
        self.assertNotIn(" min", text)
        self.assertTrue(text.endswith("\n\nUpdated 14:05"))

    def test_recent_reviews_show_last_five_with_links_newest_first(self) -> None:
        history = tuple(
            _job(
                index,
                f"Change {index}",
                status="completed",
                review_urls=(f"https://example.test/pull/{index}",),
            )
            for index in range(1, 8)
        ) + (_job(20, "No review", status="completed"),)

        text = render_task_board(_status(history=history), updated_at=UPDATED_AT).text

        section = text.split("Recent reviews:\n", 1)[1].split("\n\n", 1)[0]
        self.assertEqual(
            section.splitlines(),
            [
                "- #7 Change 7",
                "  https://example.test/pull/7",
                "- #6 Change 6",
                "  https://example.test/pull/6",
                "- #5 Change 5",
                "  https://example.test/pull/5",
                "- #4 Change 4",
                "  https://example.test/pull/4",
                "- #3 Change 3",
                "  https://example.test/pull/3",
            ],
        )
        self.assertNotIn("No review", text)

    def test_recent_failures_show_last_five_with_short_reason(self) -> None:
        history = tuple(
            _job(index, f"Attempt {index}", status="failed", result=f"Reason {index}\nTrace")
            for index in range(1, 7)
        ) + (
            _job(10, "Coded failure", status="failed", failure_code="timeout"),
            _job(11, "Long failure", status="failed", result="x" * 400),
            _job(12, "Cancelled", status="cancelled", result="Stopped by /stop."),
        )

        text = render_task_board(_status(history=history), updated_at=UPDATED_AT).text

        section = text.split("Recent failures:\n", 1)[1].split("\n\n", 1)[0]
        lines = section.splitlines()
        self.assertEqual(lines[0], "- #11 Long failure")
        self.assertTrue(lines[1].startswith("  Reason: xxx"))
        self.assertTrue(lines[1].endswith("…"))
        self.assertLessEqual(len(lines[1]), len("  Reason: ") + 100)
        self.assertEqual(lines[2:4], ["- #10 Coded failure", "  Reason: timeout"])
        self.assertEqual(lines[4:6], ["- #6 Attempt 6", "  Reason: Reason 6"])
        self.assertEqual(len(lines), 10)
        self.assertNotIn("Trace", text)
        self.assertNotIn("Cancelled", text)

    def test_long_board_is_truncated_within_the_limit(self) -> None:
        pending = tuple(_job(index, "queued request " * 10) for index in range(1, 80))
        history = tuple(
            _job(
                100 + index,
                "reviewed " * 10,
                status="completed",
                review_urls=(f"https://example.test/pull/{index}",),
            )
            for index in range(5)
        )

        rendered = render_task_board(
            _status(pending=pending, history=history),
            updated_at=UPDATED_AT,
            queue_command="!queue",
            max_chars=1200,
        )

        self.assertLessEqual(len(rendered.text), 1200)
        self.assertIn("more (see !queue)", rendered.text)
        self.assertIn("Recent reviews:", rendered.text)
        self.assertIn("Recent failures:", rendered.text)
        self.assertTrue(rendered.text.endswith("Updated 14:05"))

    def test_digest_ignores_the_updated_footer(self) -> None:
        status = _status(pending=(_job(1, "Work"),))

        first = render_task_board(status, updated_at=UPDATED_AT)
        later = render_task_board(status, updated_at=datetime(2026, 11, 24, 18, 30))

        self.assertNotEqual(first.text, later.text)
        self.assertEqual(first.digest, later.digest)

    def test_state_round_trips_in_private_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(load_task_board_state(root), TaskBoardState())
            save_task_board_state(
                TaskBoardState(enabled=True, chat_id=42, message_id=9, last_digest="abc"),
                root,
            )
            raw = json.loads(task_board_path(root).read_text(encoding="utf-8"))
            loaded = load_task_board_state(root)

        self.assertEqual(
            raw,
            {
                "schema_version": 1,
                "enabled": True,
                "chat_id": 42,
                "message_id": 9,
                "last_digest": "abc",
            },
        )
        self.assertEqual(loaded, TaskBoardState(True, 42, 9, "abc"))
        self.assertTrue(loaded.active)


class TaskBoardCommandTests(unittest.TestCase):
    def test_board_is_registered_with_help(self) -> None:
        command = core_command("board")

        self.assertIsNotNone(command)
        assert command is not None
        usage = command.usage_message("/")
        self.assertIn("/board off", usage)
        self.assertIn("/board - post a task board", usage)
        self.assertIn("/board [off]", help_message())

    def test_board_is_off_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                app._refresh_task_board()

            self.assertFalse(task_board_path(root).exists())

        self.assertEqual(chat.sent, [])
        self.assertEqual(chat.edits, [])

    def test_board_command_posts_through_inbox_and_falls_back_without_pinning(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                app.handle_event(
                    ChatEvent(cursor=5, conversation_id=42, message_id=3, text="/board")
                )
            state = load_task_board_state(root)

        self.assertNotIsInstance(chat, MessagePinProvider)
        self.assertEqual(len(chat.sent), 2)
        board, reply = chat.sent
        self.assertTrue(board[1].startswith("Enoch task board\n"))
        self.assertIn("cannot pin messages", reply[1])
        self.assertIn("/board off", reply[1])
        self.assertEqual(state, TaskBoardState(True, 42, 1, state.last_digest))
        self.assertTrue(state.last_digest)

    def test_board_command_pins_when_supported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _PinningChat()
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                reply = app._task_board_command("")

        self.assertEqual(chat.pins, [(42, 1)])
        self.assertIn("posted and pinned", reply)

    def test_pin_failure_still_tracks_the_board(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _PinningChat(pin_error="not enough rights")
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                reply = app._task_board_command("")
            state = load_task_board_state(root)

        self.assertIn("could not be pinned", reply)
        self.assertIn("not enough rights", reply)
        self.assertTrue(state.active)

    def test_board_requires_a_locked_conversation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            chat.allowed_conversation_id = None
            app = EnochApplication(load_identity(), root, chat)

            reply = app._task_board_command("")

            self.assertFalse(task_board_path(root).exists())

        self.assertIn("locked to one conversation", reply)
        self.assertEqual(chat.sent, [])


class TaskBoardRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _LegacyChat()
        self.app = EnochApplication(load_identity(), self.root, self.chat)
        self.status = _status()
        inspect = patch.object(self.app.workflow, "inspect", side_effect=lambda: self.status)
        inspect.start()
        self.addCleanup(inspect.stop)

    def test_unchanged_content_is_not_edited(self) -> None:
        self.app._task_board_command("")

        for _ in range(3):
            self.app._refresh_task_board()

        self.assertEqual(self.chat.edits, [])
        self.assertEqual(len(self.chat.sent), 1)

    def test_changed_content_is_edited_exactly_once(self) -> None:
        self.app._task_board_command("")
        self.status = _status(pending=(_job(4, "New work"),))

        for _ in range(3):
            self.app._refresh_task_board()

        self.assertEqual(len(self.chat.edits), 1)
        chat_id, message_id, text = self.chat.edits[0]
        self.assertEqual((chat_id, message_id), (42, 1))
        self.assertIn("Queued:\n- #4 New work", text)
        self.assertEqual(len(self.chat.sent), 1)

    def test_board_returning_to_earlier_content_is_edited_again(self) -> None:
        self.app._task_board_command("")
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()
        self.status = _status()
        self.app._refresh_task_board()

        self.assertEqual(len(self.chat.edits), 2)
        self.assertIn("Queued:\n- none", self.chat.edits[1][2])
        self.assertTrue(load_task_board_state(self.root).active)

    def test_failed_edit_marks_board_stale_without_reposting(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = "Bad Request: message to edit not found"
        self.status = _status(pending=(_job(4, "New work"),))

        self.app._refresh_task_board()
        self.status = _status(pending=(_job(4, "New work"), _job(5, "More")))
        self.app._refresh_task_board()
        self.app._refresh_task_board()
        state = load_task_board_state(self.root)

        self.assertEqual(self.chat.edit_attempts, 1)
        self.assertEqual(len(self.chat.sent), 1)
        self.assertFalse(state.enabled)
        self.assertFalse(state.active)
        self.assertEqual(state.message_id, 1)

    def test_board_off_stops_updates(self) -> None:
        self.app._task_board_command("")

        reply = self.app._task_board_command("off")
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()

        self.assertIn("updates stopped", reply)
        self.assertEqual(self.chat.edits, [])
        self.assertFalse(load_task_board_state(self.root).enabled)
        self.assertIn("already off", self.app._task_board_command("off"))

    def test_running_board_again_replaces_the_tracked_message(self) -> None:
        self.app._task_board_command("")
        reply = self.app._task_board_command("")
        self.status = _status(pending=(_job(4, "New work"),))

        self.app._refresh_task_board()

        self.assertIn("previous board is no longer updated", reply)
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual([edit[1] for edit in self.chat.edits], [2])
        self.assertEqual(load_task_board_state(self.root).message_id, 2)

    def test_board_failure_never_breaks_the_cycle(self) -> None:
        save_task_board_state(
            TaskBoardState(enabled=True, chat_id=42, message_id=1, last_digest="old"),
            self.root,
        )
        task_board_path(self.root).write_text("{not json", encoding="utf-8")

        with patch("enoch.app.core._record_system_event") as record:
            self.app._refresh_task_board()
            self.app._refresh_task_board()

        self.assertEqual(
            [call.args[0] for call in record.call_args_list],
            ["task_board_refresh_failed"],
        )
        self.assertEqual(self.chat.edits, [])

    def test_run_once_refreshes_the_board(self) -> None:
        with patch.object(self.app, "_refresh_task_board") as refresh:
            self.app.run_once()

        refresh.assert_called_once_with()

    def test_unknown_argument_shows_usage(self) -> None:
        reply = self.app._task_board_command("sideways")

        self.assertIn("Task board commands:", reply)
        self.assertEqual(self.chat.sent, [])


class _LegacyChat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.edit_attempts = 0
        self.edit_error = ""

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edit_attempts += 1
        if self.edit_error:
            raise ChatProviderError(self.edit_error)
        self.edits.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _PinningChat(_LegacyChat):
    def __init__(self, pin_error: str = "") -> None:
        super().__init__()
        self.pins: list[tuple[int, int]] = []
        self.pin_error = pin_error

    def pin_message(self, conversation_id, message_id):
        if self.pin_error:
            raise ChatProviderError(self.pin_error)
        self.pins.append((conversation_id, message_id))


if __name__ == "__main__":
    unittest.main()
