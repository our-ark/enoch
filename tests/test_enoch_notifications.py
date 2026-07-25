from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app.core import EnochApplication
from enoch.app.execution_context import CURRENT_WORK_STATUS
from enoch.app.models import WorkStatusMessage
from enoch.app.epoch import (
    StaleDaemonEpoch,
    begin_daemon_epoch,
    daemon_epoch_guard,
)
from enoch.app.notifications import (
    DELIVERED,
    PENDING,
    RETRYABLE_FAILURE,
    TERMINAL_FAILURE,
    NotificationDeliveryService,
    claim_notification,
    notification_record,
    persist_notification_intent,
)
from enoch.app.inbox import inbox_path
from enoch.identity import load_identity
from enoch.providers import (
    ChatEvent,
    NotificationCapabilities,
    NotificationDeliveryError,
    NotificationIntent,
    NotificationReceipt,
)


class EnochNotificationTests(unittest.TestCase):
    def test_daemon_epoch_fences_previous_owner(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = begin_daemon_epoch(root, provider="test")
            second = begin_daemon_epoch(root, provider="test")

            with self.assertRaises(StaleDaemonEpoch):
                with daemon_epoch_guard(first, root):
                    pass
            with daemon_epoch_guard(second, root):
                current = True

        self.assertTrue(current)
        self.assertEqual(second.generation, first.generation + 1)

    def test_repeated_send_returns_durable_receipt_without_duplicate(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            epoch = begin_daemon_epoch(root, provider="test")
            delivery = NotificationDeliveryService(chat, "test", root, epoch)

            first = delivery.send(42, "hello", idempotency_key="reply-1")
            repeated = delivery.send(42, "hello", idempotency_key="reply-1")
            record = notification_record("test", "reply-1", root)

        self.assertTrue(first.delivered)
        self.assertTrue(repeated.delivered)
        self.assertEqual(chat.sent, [(42, "hello")])
        assert record is not None
        self.assertEqual(record.status, DELIVERED)
        self.assertEqual(record.receipt_message_id, 1)

    def test_intent_is_persisted_as_pending_before_it_is_claimed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            intent = NotificationIntent(
                idempotency_key="reply-pending",
                operation="send",
                conversation_id=42,
                text="hello",
            )

            pending = persist_notification_intent("test", intent, root)
            stored = notification_record("test", intent.idempotency_key, root)

        self.assertEqual(pending.status, PENDING)
        self.assertEqual(pending.attempts, 0)
        self.assertEqual(stored, pending)

    def test_pending_intent_is_delivered_during_restart_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            first_epoch = begin_daemon_epoch(root, provider="test")
            intent = NotificationIntent(
                idempotency_key="reply-pending-recovery",
                operation="send",
                conversation_id=42,
                text="hello",
                daemon_epoch=first_epoch.token,
            )
            persist_notification_intent("test", intent, root)

            second_epoch = begin_daemon_epoch(root, provider="test")
            recovery = NotificationDeliveryService(chat, "test", root, second_epoch)
            results = recovery.recover()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].delivered)
        self.assertEqual(chat.sent, [(42, "hello")])

    def test_intent_persistence_failure_happens_before_provider_call(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            epoch = begin_daemon_epoch(root, provider="test")
            delivery = NotificationDeliveryService(chat, "test", root, epoch)

            with patch(
                "enoch.app.notifications.persist_notification_intent",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    delivery.send(42, "hello", idempotency_key="reply-local-failure")

        self.assertEqual(chat.sent, [])

    def test_retryable_failure_is_bounded_and_can_recover(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _FlakyLegacyChat()
            epoch = begin_daemon_epoch(root, provider="test")
            delivery = NotificationDeliveryService(chat, "test", root, epoch)

            failed = delivery.send(42, "hello", idempotency_key="reply-2")
            completed = delivery.send(42, "hello", idempotency_key="reply-2")

        self.assertFalse(failed.delivered)
        self.assertFalse(failed.terminal)
        self.assertTrue(completed.delivered)
        self.assertEqual(chat.attempts, 2)
        self.assertEqual(chat.sent, [(42, "hello")])

    def test_retryable_delivery_stops_after_three_attempts(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _AlwaysFailChat()
            epoch = begin_daemon_epoch(root, provider="test")
            delivery = NotificationDeliveryService(chat, "test", root, epoch)

            first = delivery.send(42, "hello", idempotency_key="reply-bounded")
            second = delivery.send(42, "hello", idempotency_key="reply-bounded")
            third = delivery.send(42, "hello", idempotency_key="reply-bounded")
            repeated = delivery.send(42, "hello", idempotency_key="reply-bounded")

        self.assertFalse(first.terminal)
        self.assertFalse(second.terminal)
        self.assertTrue(third.terminal)
        self.assertTrue(repeated.terminal)
        self.assertEqual(chat.attempts, 3)

    def test_retryable_failure_is_replayed_during_restart_recovery(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _FlakyLegacyChat()
            first_epoch = begin_daemon_epoch(root, provider="test")
            first = NotificationDeliveryService(chat, "test", root, first_epoch)

            failed = first.send(42, "hello", idempotency_key="reply-retry-recovery")
            self.assertEqual(failed.record.status, RETRYABLE_FAILURE)

            second_epoch = begin_daemon_epoch(root, provider="test")
            second = NotificationDeliveryService(chat, "test", root, second_epoch)
            recovered = second.recover()

        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].delivered)
        self.assertEqual(chat.sent, [(42, "hello")])

    def test_crash_after_provider_success_reconciles_without_second_send(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _DurableChat()
            first_epoch = begin_daemon_epoch(root, provider="test")
            first = NotificationDeliveryService(chat, "test", root, first_epoch)

            with patch(
                "enoch.app.notifications.complete_notification",
                side_effect=OSError("simulated receipt persistence crash"),
            ):
                with self.assertRaisesRegex(OSError, "receipt persistence"):
                    first.send(42, "hello", idempotency_key="reply-3")

            second_epoch = begin_daemon_epoch(root, provider="test")
            second = NotificationDeliveryService(chat, "test", root, second_epoch)
            recovered = second.recover()
            record = notification_record("test", "reply-3", root)

        self.assertEqual(chat.delivery_calls, 1)
        self.assertEqual(chat.reconcile_calls, 1)
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].delivered)
        assert record is not None
        self.assertEqual(record.status, DELIVERED)

    def test_ambiguous_provider_timeout_replays_only_with_idempotency(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _AmbiguousDurableChat()
            epoch = begin_daemon_epoch(root, provider="test")
            delivery = NotificationDeliveryService(chat, "test", root, epoch)

            timed_out = delivery.send(42, "hello", idempotency_key="reply-timeout")
            replayed = delivery.send(42, "hello", idempotency_key="reply-timeout")

        self.assertFalse(timed_out.delivered)
        self.assertFalse(timed_out.terminal)
        self.assertTrue(replayed.delivered)
        self.assertEqual(chat.delivery_calls, 2)
        self.assertEqual(chat.sent, [(42, "hello")])

    def test_cancelled_delivery_is_terminal_and_not_replayed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _CancelledChat()
            epoch = begin_daemon_epoch(root, provider="test")
            delivery = NotificationDeliveryService(chat, "test", root, epoch)

            cancelled = delivery.send(42, "hello", idempotency_key="reply-cancelled")
            repeated = delivery.send(42, "hello", idempotency_key="reply-cancelled")

        self.assertTrue(cancelled.terminal)
        self.assertTrue(repeated.terminal)
        self.assertEqual(chat.attempts, 1)

    def test_legacy_ambiguous_delivery_fails_closed_after_restart(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            first_epoch = begin_daemon_epoch(root, provider="test")
            intent = NotificationIntent(
                idempotency_key="reply-4",
                operation="send",
                conversation_id=42,
                text="hello",
                daemon_epoch=first_epoch.token,
            )
            claim_notification("test", intent, first_epoch, root)
            chat.send_message(42, "hello")

            second_epoch = begin_daemon_epoch(root, provider="test")
            second = NotificationDeliveryService(chat, "test", root, second_epoch)
            recovered = second.recover()
            record = notification_record("test", "reply-4", root)

        self.assertEqual(chat.sent, [(42, "hello")])
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0].terminal)
        assert record is not None
        self.assertEqual(record.status, TERMINAL_FAILURE)
        self.assertIn("not resent", record.error)

    def test_stale_service_cannot_send_or_edit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            first_epoch = begin_daemon_epoch(root, provider="test")
            stale = NotificationDeliveryService(chat, "test", root, first_epoch)
            begin_daemon_epoch(root, provider="test")

            with self.assertRaises(StaleDaemonEpoch):
                stale.send(42, "hello", idempotency_key="reply-5")
            with self.assertRaises(StaleDaemonEpoch):
                stale.edit(42, 7, "updated", idempotency_key="edit-5")

        self.assertEqual(chat.sent, [])
        self.assertEqual(chat.edits, [])

    def test_stale_application_cannot_begin_or_mutate_an_inbox_event(self) -> None:
        event = ChatEvent(
            cursor=11,
            conversation_id=42,
            message_id=8,
            text="/status",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            stale = EnochApplication(load_identity(), root, chat)
            EnochApplication(load_identity(), root, chat)

            with self.assertRaises(StaleDaemonEpoch):
                stale.handle_event(event)

            path = inbox_path("test", root)
            self.assertFalse(path.exists())

    def test_inbox_replay_after_local_crash_does_not_repeat_reply(self) -> None:
        event = ChatEvent(
            cursor=11,
            conversation_id=42,
            message_id=8,
            text="/status",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            first = EnochApplication(load_identity(), root, chat)
            with (
                patch.object(
                    first,
                    "_dispatch_chat_event",
                    return_value=("healthy", "/status"),
                ),
                patch(
                    "enoch.app.core.mark_reply_sent",
                    side_effect=OSError("simulated inbox receipt crash"),
                ),
            ):
                with self.assertRaisesRegex(OSError, "inbox receipt"):
                    first.handle_event(event)

            second = EnochApplication(load_identity(), root, chat)
            second.handle_event(event)

        self.assertEqual(chat.sent, [(42, "healthy")])

    def test_late_progress_cannot_overwrite_terminal_status(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            app = EnochApplication(load_identity(), root, chat)
            status = WorkStatusMessage(
                chat_id=42,
                message_id=7,
                request="work",
                started_at=0,
                task_id=1,
                status="completed",
                latest_update="Completed.",
            )
            token = CURRENT_WORK_STATUS.set(status)
            try:
                handled = app._update_work_status("late progress")
            finally:
                CURRENT_WORK_STATUS.reset(token)

        self.assertTrue(handled)
        self.assertEqual(status.latest_update, "Completed.")
        self.assertEqual(chat.edits, [])


class _LegacyChat:
    name = "test"
    provider_kind = "chat"
    allowed_conversation_id = 42

    def __init__(self) -> None:
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


class _FlakyLegacyChat(_LegacyChat):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def send_message(self, conversation_id, text):
        self.attempts += 1
        if self.attempts == 1:
            raise OSError("temporary provider failure")
        return super().send_message(conversation_id, text)


class _AlwaysFailChat(_LegacyChat):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def send_message(self, conversation_id, text):
        self.attempts += 1
        raise OSError("temporary provider failure")


class _DurableChat(_LegacyChat):
    notification_capabilities = NotificationCapabilities(
        idempotent_delivery=True,
        reconciliation=True,
    )

    def __init__(self) -> None:
        super().__init__()
        self.receipts: dict[str, NotificationReceipt] = {}
        self.delivery_calls = 0
        self.reconcile_calls = 0

    def deliver_notification(self, intent):
        self.delivery_calls += 1
        if intent.idempotency_key not in self.receipts:
            message_id = len(self.receipts) + 1
            self.sent.append((intent.conversation_id, intent.text))
            self.receipts[intent.idempotency_key] = NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="delivered",
                message_id=message_id,
                provider_reference=f"test:{message_id}",
            )
        return self.receipts[intent.idempotency_key]

    def reconcile_notification(self, intent):
        self.reconcile_calls += 1
        return self.receipts.get(
            intent.idempotency_key,
            NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="not_found",
            ),
        )


class _AmbiguousDurableChat(_DurableChat):
    def deliver_notification(self, intent):
        self.delivery_calls += 1
        if intent.idempotency_key not in self.receipts:
            message_id = len(self.receipts) + 1
            self.sent.append((intent.conversation_id, intent.text))
            self.receipts[intent.idempotency_key] = NotificationReceipt(
                idempotency_key=intent.idempotency_key,
                status="delivered",
                message_id=message_id,
            )
            raise NotificationDeliveryError(
                "provider timed out after accepting the request",
                retryable=True,
                ambiguous=True,
            )
        return self.receipts[intent.idempotency_key]


class _CancelledChat(_LegacyChat):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    def send_message(self, conversation_id, text):
        self.attempts += 1
        raise NotificationDeliveryError(
            "delivery cancelled",
            retryable=False,
            ambiguous=False,
        )


if __name__ == "__main__":
    unittest.main()
