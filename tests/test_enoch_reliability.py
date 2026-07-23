from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.app.inbox import (
    acknowledge_event,
    begin_event,
    complete_event,
    inbox_path,
    mark_reply_sent,
)
from enoch.app.core import EnochApplication
from enoch.backlog import add_backlog_item, backlog_status
from enoch.codex_sessions import (
    CodexSessionState,
    load_codex_session,
    save_codex_session,
)
from enoch.config import read_section, write_section_value
from enoch.cron import add_cron_job, cron_status
from enoch.channel import load_channel_cursor
from enoch.memory.store import apply_memory_candidates, load_long_term_memory
from enoch.identity import load_identity
from enoch.providers.contracts import ChatEvent
from enoch.state import StateCorruptionError
from enoch.tasks.queue import (
    cancel_task,
    enqueue_task,
    task_queue_path,
    task_queue_status,
)


class EnochReliabilityTests(unittest.TestCase):
    def test_corrupt_queue_is_preserved_and_never_replaced_with_empty_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = task_queue_path(root)
            path.parent.mkdir(parents=True)
            damaged = "{broken"
            path.write_text(damaged, encoding="utf-8")

            with self.assertRaises(StateCorruptionError):
                enqueue_task(42, "must not overwrite history", root)

            self.assertEqual(path.read_text(encoding="utf-8"), damaged)

    def test_invalid_inbox_receipt_is_preserved(self) -> None:
        event = ChatEvent(
            cursor=9,
            conversation_id=42,
            message_id=6,
            text="/status",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = inbox_path("telegram", root)
            path.parent.mkdir(parents=True)
            damaged = '{"events":{"receipt":{"status":"mystery"}}}\n'
            path.write_text(damaged, encoding="utf-8")

            with self.assertRaises(StateCorruptionError):
                begin_event("telegram", event, root)

            self.assertEqual(path.read_text(encoding="utf-8"), damaged)

    def test_task_enqueue_is_idempotent_and_history_is_not_truncated(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = enqueue_task(
                42,
                "run once",
                root,
                idempotency_key="chat:update-1:task",
            )
            duplicate = enqueue_task(
                42,
                "run once",
                root,
                idempotency_key="chat:update-1:task",
            )
            for index in range(25):
                task = enqueue_task(42, f"history task {index}", root)
                cancel_task(task.id, root)
            status = task_queue_status(root)

        self.assertEqual(duplicate.id, first.id)
        self.assertEqual(status.pending_count, 1)
        self.assertEqual(len(status.history), 25)

    def test_backlog_and_cron_creation_are_idempotent(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            backlog = add_backlog_item(
                42,
                "later",
                root,
                idempotency_key="chat:update-2:backlog",
            )
            backlog_duplicate = add_backlog_item(
                42,
                "later",
                root,
                idempotency_key="chat:update-2:backlog",
            )
            cron = add_cron_job(
                42,
                "recurring",
                60,
                root,
                idempotency_key="chat:update-3:cron",
            )
            cron_duplicate = add_cron_job(
                42,
                "recurring",
                60,
                root,
                idempotency_key="chat:update-3:cron",
            )

            self.assertEqual(backlog_duplicate.id, backlog.id)
            self.assertEqual(cron_duplicate.id, cron.id)
            self.assertEqual(backlog_status(root).pending_count, 1)
            self.assertEqual(cron_status(root).active_count, 1)

    def test_config_round_trips_comments_quotes_and_backslashes(self) -> None:
        value = 'alpha#beta "quoted" C:\\work'
        with TemporaryDirectory() as directory:
            root = Path(directory)
            write_section_value("telegram", "bot_token", value, root)

            self.assertEqual(read_section("telegram", root)["bot_token"], value)

    def test_session_read_modify_write_is_serialized(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def save(key: str) -> None:
                save_codex_session(
                    CodexSessionState(
                        key=key,
                        session_id=f"session-{key}",
                        turn_count=1,
                        created_at="2026-01-01T00:00:00+00:00",
                        updated_at="2026-01-01T00:00:00+00:00",
                    ),
                    root,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                tuple(executor.map(save, ("one", "two")))

            self.assertEqual(load_codex_session("one", root).session_id, "session-one")
            self.assertEqual(load_codex_session("two", root).session_id, "session-two")

    def test_memory_read_modify_write_is_serialized(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def remember(text: str) -> None:
                apply_memory_candidates([{"text": text}], root)

            with ThreadPoolExecutor(max_workers=2) as executor:
                tuple(executor.map(remember, ("first decision", "second decision")))

            texts = {
                str(memory["text"])
                for memory in load_long_term_memory(root)["memories"]
            }

        self.assertEqual(texts, {"first decision", "second decision"})

    def test_completed_chat_receipt_prevents_reexecution_after_restart(self) -> None:
        event = ChatEvent(
            cursor=10,
            conversation_id=42,
            message_id=7,
            text="/task run once",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            receipt = begin_event("telegram", event, root)
            completed = complete_event(
                "telegram",
                receipt.key,
                root,
                reply="Queued task #1.",
                logged_input=event.text,
            )
            mark_reply_sent("telegram", receipt.key, root)
            acknowledge_event("telegram", receipt.key, root)

            replayed = begin_event("telegram", event, root)

        self.assertFalse(completed.reply_sent)
        self.assertTrue(replayed.completed)
        self.assertTrue(replayed.reply_sent)
        self.assertEqual(replayed.attempts, 1)

    def test_reply_delivery_failure_retries_without_reexecuting_handler(self) -> None:
        event = ChatEvent(
            cursor=12,
            conversation_id=42,
            message_id=9,
            text="/status",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _FlakyChat()
            app = EnochApplication(load_identity(), root, chat)
            with patch.object(
                app,
                "_dispatch_chat_event",
                return_value=("healthy", event.text),
            ) as dispatch:
                app.handle_event(event)
                self.assertIsNone(load_channel_cursor("telegram", root))
                app.handle_event(event)
                self.assertEqual(load_channel_cursor("telegram", root), 12)

        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(chat.attempts, 2)
        self.assertEqual(chat.sent, [(42, "healthy")])

    def test_poison_chat_event_is_quarantined_after_three_attempts(self) -> None:
        event = ChatEvent(
            cursor=11,
            conversation_id=42,
            message_id=8,
            text="/broken",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _Chat()
            app = EnochApplication(load_identity(), root, chat)
            with patch("builtins.print"), patch.object(
                app,
                "_dispatch_chat_event",
                side_effect=ValueError("broken command payload"),
            ) as dispatch:
                app.handle_event(event)
                app.handle_event(event)
                app.handle_event(event)
                app.handle_event(event)

        self.assertEqual(dispatch.call_count, 3)
        self.assertEqual(len(chat.sent), 1)
        self.assertIn("skipped this update after three failed", chat.sent[0][1])


class _Chat:
    name = "telegram"
    allowed_conversation_id = 42

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, conversation_id: int, text: str):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def send_read_ack(self, _conversation_id: int, _message_id: int) -> None:
        return None


class _FlakyChat(_Chat):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def send_message(self, conversation_id: int, text: str):
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("temporary Telegram failure")
        return super().send_message(conversation_id, text)


if __name__ == "__main__":
    unittest.main()
