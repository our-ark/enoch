from io import BytesIO
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_provider_kit import (
    Attachment,
    AttachmentProvider,
    ChatProvider,
    ProviderContractConformanceMixin,
)
from our_ark_telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramError,
    chunks,
    telegram_event,
)


class TelegramLibraryTests(ProviderContractConformanceMixin, unittest.TestCase):
    provider_kind = "chat"
    provider_protocol = ChatProvider

    def create_provider(self, root: Path) -> TelegramClient:
        del root
        return TelegramClient(TelegramConfig(token="conformance", allowed_chat_id=42))

    def test_client_implements_chat_provider_contract(self) -> None:
        client = TelegramClient(TelegramConfig(token="test", allowed_chat_id=42))

        self.assertIsInstance(client, ChatProvider)
        self.assertIsInstance(client, AttachmentProvider)
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

    def test_send_renders_html_only_in_transport_payload(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 9}}

        client._call = fake_call
        original = "Run `bin/enoch doctor` and keep <output>."

        message_id = client.send_message(42, original)

        self.assertEqual(message_id, 9)
        self.assertEqual(original, "Run `bin/enoch doctor` and keep <output>.")
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["chat_id"], 42)
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")
        self.assertEqual(
            calls[0][1]["text"],
            "Run <code>bin/enoch doctor</code> and keep &lt;output&gt;.",
        )

    def test_edit_uses_the_same_display_only_renderer(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True}

        client._call = fake_call
        client.edit_message(42, 9, "Status: **completed**")

        self.assertEqual(
            calls,
            [
                (
                    "editMessageText",
                    {
                        "chat_id": 42,
                        "message_id": 9,
                        "text": "<b>Status:</b> <b>completed</b>",
                        "parse_mode": "HTML",
                    },
                )
            ],
        )

    def test_format_rejection_falls_back_to_original_plain_text(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            if payload.get("parse_mode"):
                raise TelegramError("Bad Request: can't parse entities")
            return {"ok": True, "result": {"message_id": 11}}

        client._call = fake_call

        message_id = client.send_message(42, "Run `doctor`.")

        self.assertEqual(message_id, 11)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["text"], "Run `doctor`.")
        self.assertNotIn("parse_mode", calls[1][1])

    def test_delivery_failure_is_not_retried_as_plain_text(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            raise TelegramError("Telegram API call sendMessage failed.")

        client._call = fake_call

        with self.assertRaisesRegex(TelegramError, "sendMessage failed"):
            client.send_message(42, "**hello**")

        self.assertEqual(len(calls), 1)

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

    def test_attachment_download_delegates_to_file_transport(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        attachment = Attachment(kind="image", file_id="file-1", mime_type="image/jpeg")
        destination = Path("image.jpg")

        with patch.object(client, "download_file") as download:
            client.download_attachment(attachment, destination, max_bytes=1024)

        download.assert_called_once_with("file-1", destination, max_bytes=1024)


if __name__ == "__main__":
    unittest.main()
