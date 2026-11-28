from email.message import Message
from io import BytesIO
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch
from urllib import parse
from urllib.error import HTTPError, URLError


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_provider_kit import (
    Attachment,
    AttachmentProvider,
    ChatProvider,
    MessageDeleteProvider,
    MessagePinProvider,
    MessageUnpinProvider,
    ProviderContractConformanceMixin,
    SilentMessageProvider,
)
from our_ark_telegram import (
    TelegramBotPeer,
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
        self.assertIsInstance(client, MessagePinProvider)
        self.assertIsInstance(client, MessageUnpinProvider)
        self.assertIsInstance(client, MessageDeleteProvider)
        self.assertIsInstance(client, SilentMessageProvider)
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

    def test_event_carries_the_replied_message_id(self) -> None:
        event = telegram_event(
            {
                "update_id": 8,
                "message": {
                    "message_id": 5,
                    "chat": {"id": 42},
                    "text": "retry it",
                    "reply_to_message": {
                        "message_id": 3,
                        "chat": {"id": 42},
                        "text": "▶️ #12 started: Fix the bug",
                    },
                },
            }
        )

        assert event is not None
        self.assertEqual(event.replied_message_id, 3)
        self.assertEqual(event.replied_text, "▶️ #12 started: Fix the bug")

    def test_event_without_a_reply_has_no_replied_message_id(self) -> None:
        plain = telegram_event(
            {
                "update_id": 8,
                "message": {"message_id": 5, "chat": {"id": 42}, "text": "hi"},
            }
        )
        malformed = telegram_event(
            {
                "update_id": 9,
                "message": {
                    "message_id": 6,
                    "chat": {"id": 42},
                    "text": "hi",
                    "reply_to_message": {"message_id": "3", "text": "x"},
                },
            }
        )

        assert plain is not None and malformed is not None
        self.assertIsNone(plain.replied_message_id)
        self.assertIsNone(malformed.replied_message_id)

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

    def test_private_bot_peer_is_strictly_authenticated_and_addressable(self) -> None:
        peer = TelegramBotPeer("worker", "@worker_agent_bot", 7000000001)
        client = TelegramClient(TelegramConfig(token="test", bot_peers=(peer,)))
        calls = []
        client._call = lambda method, payload: (
            calls.append((method, payload))
            or {"ok": True, "result": {"message_id": 17}}
        )
        event = telegram_event(
            {
                "update_id": 8,
                "message": {
                    "message_id": 4,
                    "chat": {"id": 7000000001, "type": "private"},
                    "from": {
                        "id": 7000000001,
                        "is_bot": True,
                        "username": "worker_agent_bot",
                    },
                    "text": "agent message",
                },
            }
        )
        assert event is not None

        self.assertEqual(client.peer_alias(event), "worker")
        self.assertEqual(client.send_to_peer("WORKER", "hello"), 17)
        self.assertEqual(calls[0][1]["chat_id"], "@worker_agent_bot")

        forged = dict(event.raw)
        forged["message"] = dict(event.raw["message"])
        forged["message"]["from"] = dict(event.raw["message"]["from"], id=123)
        forged_event = telegram_event(forged)
        assert forged_event is not None
        self.assertIsNone(client.peer_alias(forged_event))

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

    def test_pin_message_pins_silently(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": True}

        client._call = fake_call
        client.pin_message(42, 9)

        self.assertEqual(
            calls,
            [
                (
                    "pinChatMessage",
                    {
                        "chat_id": 42,
                        "message_id": 9,
                        "disable_notification": "true",
                    },
                )
            ],
        )

    def test_pin_message_failure_raises_provider_error(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))

        def fake_call(method, payload):
            raise TelegramError("Bad Request: not enough rights to pin a message")

        client._call = fake_call

        with self.assertRaisesRegex(TelegramError, "not enough rights"):
            client.pin_message(42, 9)

    def test_send_silent_message_disables_notification(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 77}}

        client._call = fake_call
        silent_id = client.send_silent_message(42, "Board")
        loud_id = client.send_message(42, "Reply")

        self.assertEqual((silent_id, loud_id), (77, 77))
        self.assertEqual([method for method, _ in calls], ["sendMessage", "sendMessage"])
        self.assertEqual(calls[0][1]["disable_notification"], "true")
        self.assertEqual(calls[0][1]["chat_id"], 42)
        self.assertNotIn("disable_notification", calls[1][1])

    def test_delete_message_calls_delete_message(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": True}

        client._call = fake_call
        client.delete_message(42, 9)

        self.assertEqual(calls, [("deleteMessage", {"chat_id": 42, "message_id": 9})])

    def test_delete_message_failure_raises_provider_error(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))

        def fake_call(method, payload):
            raise TelegramError("Bad Request: message can't be deleted")

        client._call = fake_call

        with self.assertRaisesRegex(TelegramError, "can't be deleted"):
            client.delete_message(42, 9)

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

    def test_unpin_message_calls_unpin_chat_message(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": True}

        client._call = fake_call
        client.unpin_message(42, 9)

        self.assertIsInstance(client, MessageUnpinProvider)
        self.assertEqual(calls, [("unpinChatMessage", {"chat_id": 42, "message_id": 9})])

    def test_unpin_message_failure_raises_provider_error(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))

        def fake_call(method, payload):
            raise TelegramError("Bad Request: message to unpin not found")

        client._call = fake_call

        with self.assertRaisesRegex(TelegramError, "to unpin not found"):
            client.unpin_message(42, 9)

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


# Synthetic, and short enough not to look like a real bot token.
SECRET_TOKEN = "123456:synthetic-secret"


def _http_error(code: int, reason: str, body: bytes) -> HTTPError:
    url = f"https://api.telegram.org/bot{SECRET_TOKEN}/editMessageText"
    return HTTPError(url, code, reason, Message(), BytesIO(body))


def _ok_response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    return response


class TelegramErrorDetailTests(unittest.TestCase):
    """Bot API rejections keep their code and description, never the token."""

    def setUp(self) -> None:
        self.client = TelegramClient(TelegramConfig(token=SECRET_TOKEN, poll_timeout=1))
        urlopen = patch("our_ark_telegram.core.request.urlopen")
        self.urlopen = urlopen.start()
        self.addCleanup(urlopen.stop)

    def _fail_with(self, *errors: BaseException) -> None:
        self.urlopen.side_effect = errors

    def _assert_no_secret(self, error: BaseException) -> None:
        for text in (str(error), repr(error), error.description if isinstance(error, TelegramError) else ""):
            self.assertNotIn(SECRET_TOKEN, text)
            self.assertNotIn("api.telegram.org", text)
        # Nothing that holds the request URL is chained or attached.
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_http_error_body_becomes_code_and_description(self) -> None:
        self._fail_with(
            _http_error(
                400,
                "Bad Request",
                b'{"ok":false,"error_code":400,'
                b'"description":"Bad Request: message to edit not found"}',
            )
        )

        with self.assertRaises(TelegramError) as caught:
            self.client.edit_message(42, 9, "Board")

        error = caught.exception
        self.assertEqual(error.error_code, 400)
        self.assertEqual(error.description, "Bad Request: message to edit not found")
        self.assertIsNone(error.retry_after)
        self.assertEqual(error.method, "editMessageText")
        self.assertEqual(
            str(error),
            "Telegram API call editMessageText failed (400): "
            "Bad Request: message to edit not found",
        )
        self._assert_no_secret(error)

    def test_rate_limit_keeps_retry_after(self) -> None:
        self._fail_with(
            _http_error(
                429,
                "Too Many Requests",
                b'{"ok":false,"error_code":429,'
                b'"description":"Too Many Requests: retry after 17",'
                b'"parameters":{"retry_after":17}}',
            )
        )

        with self.assertRaises(TelegramError) as caught:
            self.client.edit_message(42, 9, "Board")

        self.assertEqual((caught.exception.error_code, caught.exception.retry_after), (429, 17))
        self.assertIn("(429): Too Many Requests: retry after 17", str(caught.exception))
        self._assert_no_secret(caught.exception)

    def test_retry_after_is_named_when_the_description_omits_it(self) -> None:
        self._fail_with(
            _http_error(
                429,
                "Too Many Requests",
                b'{"ok":false,"error_code":429,"description":"Flood control",'
                b'"parameters":{"retry_after":5}}',
            )
        )

        with self.assertRaises(TelegramError) as caught:
            self.client.pin_message(42, 9)

        self.assertTrue(str(caught.exception).endswith("Flood control (retry after 5)"))

    def test_http_error_without_json_body_uses_the_status_line(self) -> None:
        self._fail_with(_http_error(502, "Bad Gateway", b"<html>proxy</html>"))

        with self.assertRaises(TelegramError) as caught:
            self.client.delete_message(42, 9)

        self.assertEqual(caught.exception.error_code, 502)
        self.assertEqual(
            str(caught.exception),
            "Telegram API call deleteMessage failed (502): HTTP 502 Bad Gateway",
        )
        self._assert_no_secret(caught.exception)

    def test_token_in_a_description_is_redacted(self) -> None:
        body = json.dumps(
            {
                "ok": False,
                "error_code": 401,
                "description": f"Unauthorized: https://api.telegram.org/bot{SECRET_TOKEN}/x {SECRET_TOKEN}",
            }
        ).encode("utf-8")
        self._fail_with(_http_error(401, "Unauthorized", body))

        with self.assertRaises(TelegramError) as caught:
            self.client.unpin_message(42, 9)

        self.assertEqual(caught.exception.error_code, 401)
        self.assertIn("Unauthorized: <url> <redacted>", str(caught.exception))
        self._assert_no_secret(caught.exception)

    def test_network_errors_name_the_failure_without_the_url(self) -> None:
        for failure, expected in (
            (TimeoutError("timed out"), "network error (TimeoutError: timed out)"),
            (
                URLError(ConnectionRefusedError(61, "Connection refused")),
                "network error (ConnectionRefusedError: [Errno 61] Connection refused)",
            ),
            (
                URLError(f"cannot reach https://api.telegram.org/bot{SECRET_TOKEN}/getMe"),
                "network error (URLError: cannot reach <url>)",
            ),
        ):
            with self.subTest(failure=type(failure).__name__):
                self._fail_with(failure)
                with self.assertRaises(TelegramError) as caught:
                    self.client.edit_message(42, 9, "Board")
                self.assertIn(expected, str(caught.exception))
                self.assertIsNone(caught.exception.error_code)
                self._assert_no_secret(caught.exception)

    def test_unreadable_response_is_reported_without_detail(self) -> None:
        broken = MagicMock()
        broken.read.return_value = b"not json"
        self.urlopen.return_value.__enter__.return_value = broken

        with self.assertRaises(TelegramError) as caught:
            self.client.pin_message(42, 9)

        self.assertIn("pinChatMessage failed: unreadable response", str(caught.exception))
        self._assert_no_secret(caught.exception)

    def test_ok_false_body_is_reported_with_its_code(self) -> None:
        self.urlopen.return_value.__enter__.return_value = _ok_response(
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        )

        with self.assertRaises(TelegramError) as caught:
            self.client.pin_message(42, 9)

        self.assertEqual(
            str(caught.exception),
            "Telegram API call pinChatMessage failed (400): Bad Request: chat not found",
        )

    def test_html_rejection_body_triggers_the_plain_text_fallback(self) -> None:
        sent = []

        def urlopen(req, timeout):
            sent.append(dict(parse.parse_qsl(req.data.decode("utf-8"))))
            if "parse_mode" in sent[-1]:
                raise _http_error(
                    400,
                    "Bad Request",
                    b'{"ok":false,"error_code":400,'
                    b'"description":"Bad Request: can\'t parse entities: bad tag"}',
                )
            context = MagicMock()
            context.__enter__.return_value = _ok_response(
                {"ok": True, "result": {"message_id": 11}}
            )
            return context

        self.urlopen.side_effect = urlopen

        self.assertEqual(self.client.send_message(42, "Run `doctor`."), 11)
        self.assertEqual(len(sent), 2)
        self.assertNotIn("parse_mode", sent[1])
        self.assertEqual(sent[1]["text"], "Run `doctor`.")


if __name__ == "__main__":
    unittest.main()
