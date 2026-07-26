from pathlib import Path
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from our_ark_provider_kit import (
    AgentContextError,
    AgentRuntimeCancelled,
    AgentRuntimeTimedOut,
    Attachment,
    AttachmentProvider,
    AuthorizationDecision,
    AuthorizationRequest,
    ChatEvent,
    ChatProvider,
    DurableNotificationProvider,
    NotificationCapabilities,
    NotificationIntent,
    NotificationReceipt,
    ProviderCapabilities,
    RuntimeExecutionControl,
    RuntimeProgress,
    TaskRequirements,
    agent_context,
    normalize_conversation_id,
    normalize_message_id,
)


class ProviderKitTests(unittest.TestCase):
    def test_resolves_agent_context_from_manifest_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src" / "noah").mkdir(parents=True)
            (root / "genesis.toml").write_text(
                'schema_version = 1\npackage = "noah"\n',
                encoding="utf-8",
            )
            (root / "src" / "noah" / "identity.yaml").write_text(
                'name: "Noah"\n',
                encoding="utf-8",
            )

            context = agent_context(root)

        self.assertEqual(context.package, "noah")
        self.assertEqual(context.body_root, root.resolve())
        self.assertEqual(context.name, "Noah")
        self.assertEqual(context.env_prefix, "NOAH")
        self.assertEqual(context.private_directory, ".noah")
        self.assertEqual(context.service_slug, "noah")

    def test_rejects_roots_without_an_agent_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with unittest.mock.patch.dict(os.environ, {"OUR_ARK_AGENT_PACKAGE": ""}):
                with self.assertRaisesRegex(AgentContextError, "Could not find genesis.toml"):
                    agent_context(Path(directory))

    def test_normalizes_provider_identifiers(self) -> None:
        self.assertEqual(normalize_conversation_id(42), 42)
        self.assertEqual(normalize_conversation_id(" chat-1 "), "chat-1")
        self.assertEqual(normalize_message_id(" message-1 "), "message-1")
        self.assertIsNone(normalize_conversation_id(True))
        self.assertIsNone(normalize_conversation_id(0))
        self.assertIsNone(normalize_conversation_id("  "))

    def test_chat_event_carries_provider_neutral_attachments(self) -> None:
        attachment = Attachment(
            kind="image",
            file_id="provider-file",
            mime_type="image/jpeg",
            size=123,
            metadata={"width": 640},
        )

        event = ChatEvent(
            cursor=10,
            conversation_id="chat",
            text="hello",
            attachments=(attachment,),
        )

        self.assertEqual(event.attachments, (attachment,))
        self.assertEqual(event.attachments[0].metadata["width"], 640)

    def test_capability_contracts_normalize_and_validate_requirements(self) -> None:
        capabilities = ProviderCapabilities(
            provider_kind="runtime",
            capabilities=frozenset({"runtime.respond", "runtime.execute"}),
        )
        requirements = TaskRequirements(
            capabilities=("runtime.execute", "runtime.execute"),
            reason="run task",
        )
        request = AuthorizationRequest(
            action="task_execute",
            requirements=requirements,
            provider_capabilities=(capabilities,),
        )
        decision = AuthorizationDecision(allowed=True)

        self.assertTrue(capabilities.supports("runtime.execute"))
        self.assertEqual(requirements.capabilities, ("runtime.execute",))
        self.assertEqual(request.action, "task-execute")
        self.assertTrue(decision.allowed)

        with self.assertRaisesRegex(ValueError, "another provider kind"):
            ProviderCapabilities(
                provider_kind="runtime",
                capabilities=frozenset({"forge.publish"}),
            )

    def test_chat_provider_contract_is_runtime_checkable(self) -> None:
        self.assertIsInstance(FakeChatProvider(), ChatProvider)
        self.assertIsInstance(FakeChatProvider(), AttachmentProvider)
        self.assertIsInstance(FakeDurableChatProvider(), DurableNotificationProvider)

    def test_notification_contract_validates_operations_and_versions(self) -> None:
        intent = NotificationIntent(
            idempotency_key="task:1:final",
            operation="send",
            conversation_id=42,
            text="done",
            daemon_epoch="epoch-1",
        )
        receipt = NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="delivered",
            message_id=7,
        )

        self.assertEqual(intent.operation, "send")
        self.assertEqual(receipt.message_id, 7)
        with self.assertRaisesRegex(ValueError, "require a message id"):
            NotificationIntent(
                idempotency_key="edit-1",
                operation="edit",
                conversation_id=42,
                text="progress",
            )
        with self.assertRaisesRegex(ValueError, "idempotency key is required"):
            NotificationReceipt(
                idempotency_key=" ",
                status="delivered",
            )

    def test_runtime_progress_is_provider_neutral_and_normalized(self) -> None:
        progress = RuntimeProgress(
            elapsed_seconds=-4,
            stage=" Working ",
            message=" preparing ",
            sandbox=" workspace-write ",
        )

        self.assertEqual(progress.elapsed_seconds, 0)
        self.assertEqual(progress.stage, "working")
        self.assertEqual(progress.message, "preparing")
        self.assertEqual(progress.sandbox, "workspace-write")

    def test_runtime_execution_distinguishes_timeout_from_cancellation(self) -> None:
        cancellation = threading.Event()
        timeout = threading.Event()
        cancellation.set()
        timeout.set()
        execution = RuntimeExecutionControl(
            cancellation_event=cancellation,
            timeout_event=timeout,
        )

        with self.assertRaises(AgentRuntimeTimedOut):
            execution.raise_if_stopped()

        timeout.clear()
        with self.assertRaises(AgentRuntimeCancelled):
            execution.raise_if_stopped()


class FakeChatProvider:
    name = "fake"
    provider_kind = "chat"
    allowed_conversation_id = "human"

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        return "sent"

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        return None

    def download_attachment(self, attachment, destination, *, max_bytes):
        return None


class FakeDurableChatProvider(FakeChatProvider):
    notification_capabilities = NotificationCapabilities(
        idempotent_delivery=True,
        reconciliation=True,
    )

    def deliver_notification(self, intent):
        return NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="delivered",
            message_id="sent",
        )

    def reconcile_notification(self, intent):
        return NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="not_found",
        )


if __name__ == "__main__":
    unittest.main()
