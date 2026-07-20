from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_provider_kit import ChatProvider
from our_ark_telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramError,
    chunks,
    telegram_event,
)


class TelegramLibraryTests(unittest.TestCase):
    def test_client_implements_chat_provider_contract(self) -> None:
        client = TelegramClient(TelegramConfig(token="test", allowed_chat_id=42))

        self.assertIsInstance(client, ChatProvider)
        self.assertEqual(client.allowed_conversation_id, 42)

    def test_event_exposes_largest_photo_as_attachment(self) -> None:
        event = telegram_event(
            {
                "update_id": 8,
                "message": {
                    "message_id": 4,
                    "chat": {"id": 42},
                    "caption": "What is this?",
                    "photo": [
                        {"file_id": "small", "width": 10, "height": 10},
                        {
                            "file_id": "large",
                            "width": 640,
                            "height": 480,
                            "file_size": 1234,
                        },
                    ],
                },
            }
        )

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.cursor, 9)
        self.assertEqual(event.attachments[0].kind, "image")
        self.assertEqual(event.attachments[0].file_id, "large")
        self.assertEqual(event.attachments[0].metadata["width"], 640)

    def test_ignores_unsupported_attachment_without_text(self) -> None:
        event = telegram_event(
            {
                "update_id": 8,
                "message": {
                    "message_id": 4,
                    "chat": {"id": 42},
                    "document": {
                        "file_id": "pdf",
                        "mime_type": "application/pdf",
                    },
                },
            }
        )

        self.assertIsNone(event)

    def test_chunks_messages_and_rejects_invalid_size(self) -> None:
        self.assertEqual(chunks("abcde", 2), ["ab", "cd", "e"])
        with self.assertRaisesRegex(ValueError, "at least 1"):
            chunks("hello", 0)

    @patch("our_ark_telegram.core.request.urlopen")
    def test_download_enforces_size_limit(self, urlopen: MagicMock) -> None:
        api_response = MagicMock()
        api_response.read.return_value = (
            b'{"ok": true, "result": {"file_path": "photos/a.jpg"}}'
        )
        file_response = MagicMock()
        file_response.headers = {"Content-Length": "10"}
        file_response.read.return_value = b"0123456789"
        urlopen.return_value.__enter__.side_effect = [api_response, file_response]
        client = TelegramClient(TelegramConfig(token="secret", poll_timeout=1))

        with self.subTest("download"):
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as temp:
                destination = Path(temp) / "photo.jpg"
                client.download_file("file", destination, max_bytes=10)
                self.assertEqual(destination.read_bytes(), b"0123456789")

        oversized = MagicMock()
        oversized.headers = {"Content-Length": "11"}
        oversized.read = BytesIO(b"").read
        urlopen.return_value.__enter__.side_effect = [api_response, oversized]
        with self.assertRaisesRegex(TelegramError, "too large"):
            client.download_file("file", Path("/tmp/unused.jpg"), max_bytes=10)


if __name__ == "__main__":
    unittest.main()
