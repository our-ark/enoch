from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from enoch.app.epoch import StaleDaemonEpoch, begin_daemon_epoch
from enoch.app.notifications import NotificationDeliveryService
from enoch.providers import ChatProvider, DurableNotificationProvider


class DurableNotificationConformanceMixin:
    """Reliability checks for a durable chat-notification provider."""

    def create_notification_provider(self, root: Path) -> Any:
        raise NotImplementedError

    def fail_next_notification(self, provider: Any) -> None:
        """Arrange one retryable delivery failure on ``provider``."""
        raise NotImplementedError

    def notification_attempts(self, provider: Any, idempotency_key: str) -> int:
        raise NotImplementedError

    def test_conformance_notification_deduplicates_completed_request(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_notification_provider(root)
            self._assert_provider(provider)
            epoch = begin_daemon_epoch(root, provider="conformance-chat")
            delivery = NotificationDeliveryService(
                provider,
                provider.name,
                root,
                epoch,
            )

            first = delivery.send(42, "hello", idempotency_key="conformance:sent")
            repeated = delivery.send(
                42,
                "hello",
                idempotency_key="conformance:sent",
            )

            self.assertTrue(first.delivered)
            self.assertTrue(repeated.delivered)
            self.assertEqual(
                self.notification_attempts(provider, "conformance:sent"),
                1,
            )

    def test_conformance_notification_recovers_retryable_partial_failure(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_notification_provider(root)
            self._assert_provider(provider)
            self.fail_next_notification(provider)
            epoch = begin_daemon_epoch(root, provider="conformance-chat")
            first = NotificationDeliveryService(
                provider,
                provider.name,
                root,
                epoch,
            )
            failed = first.send(
                42,
                "recover me",
                idempotency_key="conformance:recover",
            )

            restarted_epoch = begin_daemon_epoch(
                root,
                provider="conformance-chat",
            )
            restarted = NotificationDeliveryService(
                provider,
                provider.name,
                root,
                restarted_epoch,
            )
            recovered = restarted.recover()

            self.assertFalse(failed.delivered)
            self.assertFalse(failed.terminal)
            self.assertEqual(len(recovered), 1)
            self.assertTrue(recovered[0].delivered)
            self.assertEqual(
                self.notification_attempts(provider, "conformance:recover"),
                2,
            )

    def test_conformance_notification_rejects_stale_fencing_token(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_notification_provider(root)
            self._assert_provider(provider)
            stale_epoch = begin_daemon_epoch(root, provider="conformance-chat")
            stale = NotificationDeliveryService(
                provider,
                provider.name,
                root,
                stale_epoch,
            )
            begin_daemon_epoch(root, provider="conformance-chat")

            with self.assertRaises(StaleDaemonEpoch):
                stale.send(
                    42,
                    "must not send",
                    idempotency_key="conformance:stale",
                )

            self.assertEqual(
                self.notification_attempts(provider, "conformance:stale"),
                0,
            )

    def _assert_provider(self, provider: Any) -> None:
        self.assertIsInstance(provider, ChatProvider)
        self.assertIsInstance(provider, DurableNotificationProvider)

