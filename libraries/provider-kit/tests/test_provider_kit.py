from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from our_ark_provider_kit import (
    Attachment,
    ChatEvent,
    ChatProvider,
    normalize_conversation_id,
    normalize_message_id,
)


class ProviderKitTests(unittest.TestCase):
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

    def test_chat_provider_contract_is_runtime_checkable(self) -> None:
        self.assertIsInstance(FakeChatProvider(), ChatProvider)


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


if __name__ == "__main__":
    unittest.main()
