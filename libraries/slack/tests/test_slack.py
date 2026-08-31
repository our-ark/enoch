from pathlib import Path
from types import SimpleNamespace
import json
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(REPOSITORY / "src"))
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_provider_kit import ChatProvider, ProviderContractConformanceMixin
from our_ark_slack import (
    SlackClient,
    SlackConfig,
    SlackError,
    load_config,
    setup_provider,
    slack_event,
    slack_message_chunks,
)


class _WebClient:
    def __init__(self) -> None:
        self.calls = []
        self.next_ts = 1

    def chat_postMessage(self, **kwargs):
        self.calls.append(("chat.postMessage", kwargs))
        value = f"1700000000.{self.next_ts:06d}"
        self.next_ts += 1
        return {"ok": True, "ts": value}

    def chat_update(self, **kwargs):
        self.calls.append(("chat.update", kwargs))
        return {"ok": True, "ts": kwargs["ts"]}

    def reactions_add(self, **kwargs):
        self.calls.append(("reactions.add", kwargs))
        return {"ok": True}


class _SocketClient:
    def __init__(self, state_dir: Path | None = None) -> None:
        self.socket_mode_request_listeners = []
        self.responses = []
        self.connected = False
        self.closed = False
        self.state_dir = state_dir
        self.spooled_before_ack = False

    def connect(self) -> None:
        self.connected = True

    def send_socket_mode_response(self, response) -> None:
        if self.state_dir is not None:
            self.spooled_before_ack = any(self.state_dir.glob("event-*.json"))
        self.responses.append(response)

    def close(self) -> None:
        self.closed = True


class SlackLibraryTests(ProviderContractConformanceMixin, unittest.TestCase):
    provider_kind = "chat"
    provider_protocol = ChatProvider

    def create_provider(self, root: Path) -> SlackClient:
        return SlackClient(
            SlackConfig("xoxb-test", "xapp-test", "D123", "U123"),
            root / "intake",
            web_client=_WebClient(),
            socket_factory=lambda _web: _SocketClient(),
        )

    def test_client_implements_chat_contract_with_explicit_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = self.create_provider(Path(directory))

        self.assertIsInstance(client, ChatProvider)
        self.assertEqual(client.allowed_conversation_id, "D123")
        self.assertEqual(client.command_prefix, "/enoch ")
        self.assertEqual(
            client.capabilities.capabilities,
            frozenset({"chat.receive", "chat.send", "chat.edit", "chat.ack"}),
        )

    def test_normalizes_direct_messages_and_strips_channel_mentions(self) -> None:
        direct = slack_event(
            "events_api",
            {
                "type": "event_callback",
                "event_id": "Ev1",
                "event": {
                    "type": "message",
                    "channel": "D123",
                    "user": "U123",
                    "ts": "1700.1",
                    "text": "/status",
                },
            },
            cursor=7,
            allowed_conversation_id="D123",
            allowed_user_id="U123",
        )
        mention = slack_event(
            "events_api",
            {
                "type": "event_callback",
                "event_id": "Ev2",
                "event": {
                    "type": "app_mention",
                    "channel": "C123",
                    "user": "U123",
                    "ts": "1700.2",
                    "text": "<@UBOT>, /queue",
                },
            },
            cursor=8,
        )

        assert direct is not None and mention is not None
        self.assertEqual(direct.cursor, 7)
        self.assertEqual(direct.conversation_id, "D123")
        self.assertEqual(direct.message_id, "1700.1")
        self.assertEqual(direct.text, "/status")
        self.assertEqual(mention.text, "/queue")

    def test_rejects_wrong_user_conversation_and_bot_events(self) -> None:
        base = {
            "type": "event_callback",
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "ts": "1700.1",
                "text": "hello",
            },
        }

        self.assertIsNone(
            slack_event(
                "events_api",
                base,
                cursor=1,
                allowed_conversation_id="D999",
            )
        )
        self.assertIsNone(
            slack_event(
                "events_api",
                base,
                cursor=1,
                allowed_user_id="U999",
            )
        )
        bot = json.loads(json.dumps(base))
        bot["event"]["bot_id"] = "B123"
        self.assertIsNone(slack_event("events_api", bot, cursor=1))
        changed = json.loads(json.dumps(base))
        changed["event"]["subtype"] = "message_changed"
        self.assertIsNone(slack_event("events_api", changed, cursor=1))

    def test_enoch_slash_command_maps_to_existing_command_surface(self) -> None:
        payload = {
            "command": "/enoch",
            "text": "task add investigate retries",
            "channel_id": "D123",
            "user_id": "U123",
            "trigger_id": "trigger-1",
        }
        event = slack_event(
            "slash_commands",
            payload,
            cursor=3,
            allowed_conversation_id="D123",
            allowed_user_id="U123",
        )
        help_event = slack_event(
            "slash_commands",
            {**payload, "text": ""},
            cursor=4,
        )

        assert event is not None and help_event is not None
        self.assertEqual(event.text, "/task add investigate retries")
        self.assertEqual(help_event.text, "/help")
        self.assertIsNone(help_event.message_id)

    def test_persists_before_ack_and_replays_once_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "intake"
            socket = _SocketClient(state_dir)
            first = SlackClient(
                SlackConfig("xoxb-test", "xapp-test", "D123", "U123"),
                state_dir,
                web_client=_WebClient(),
                socket_factory=lambda _web: socket,
            )
            request = _request(
                payload={
                    "type": "event_callback",
                    "event_id": "Ev-durable",
                    "token": "verification-secret",
                    "event": {
                        "type": "message",
                        "channel": "D123",
                        "user": "U123",
                        "ts": "1700.1",
                        "text": "hello",
                    },
                }
            )

            first._handle_request(socket, request)
            first._handle_request(socket, request)
            records = list(state_dir.glob("event-*.json"))
            persisted = records[0].read_text(encoding="utf-8")
            first.close()

            restarted = SlackClient(
                SlackConfig("xoxb-test", "xapp-test", "D123", "U123"),
                state_dir,
                web_client=_WebClient(),
                socket_factory=lambda _web: _SocketClient(),
            )
            events = restarted.receive(None)

        self.assertTrue(socket.spooled_before_ack)
        self.assertEqual(len(socket.responses), 2)
        self.assertEqual(len(records), 1)
        self.assertNotIn("verification-secret", persisted)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].cursor, 1)
        self.assertEqual(events[0].text, "hello")

    def test_slash_spool_does_not_retain_response_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "intake"
            socket = _SocketClient(state_dir)
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test"),
                state_dir,
                web_client=_WebClient(),
                socket_factory=lambda _web: socket,
            )
            client._handle_request(
                socket,
                _request(
                    request_type="slash_commands",
                    payload={
                        "command": "/enoch",
                        "text": "help",
                        "channel_id": "D123",
                        "user_id": "U123",
                        "trigger_id": "trigger-1",
                        "token": "secret",
                        "response_url": "https://hooks.slack.test/secret",
                    },
                ),
            )
            persisted = next(state_dir.glob("event-*.json")).read_text(encoding="utf-8")

        self.assertNotIn("secret", persisted)
        self.assertNotIn("response_url", persisted)

    def test_persistence_failure_is_not_acknowledged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "intake"
            socket = _SocketClient(state_dir)
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test"),
                state_dir,
                web_client=_WebClient(),
                socket_factory=lambda _web: socket,
            )
            with patch(
                "our_ark_slack.core._atomic_json",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaisesRegex(OSError, "disk full"):
                    client._handle_request(socket, _request())

        self.assertEqual(socket.responses, [])

    def test_send_edit_and_ack_use_slack_native_message_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            web = _WebClient()
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test"),
                Path(directory),
                web_client=web,
                socket_factory=lambda _web: _SocketClient(),
            )
            message_id = client.send_message("D123", "**completed**")
            client.edit_message("D123", message_id or "", "Status: **done**")
            client.send_read_ack("D123", "1700.5")

        self.assertEqual(message_id, "1700000000.000001")
        self.assertEqual(
            web.calls,
            [
                (
                    "chat.postMessage",
                    {"channel": "D123", "markdown_text": "**completed**"},
                ),
                (
                    "chat.update",
                    {
                        "channel": "D123",
                        "ts": "1700000000.000001",
                        "markdown_text": "Status: **done**",
                    },
                ),
                (
                    "reactions.add",
                    {"channel": "D123", "timestamp": "1700.5", "name": "eyes"},
                ),
            ],
        )

    def test_message_chunking_prefers_text_boundaries(self) -> None:
        self.assertEqual(
            slack_message_chunks("first paragraph\n\nsecond paragraph", 18),
            ["first paragraph\n\n", "second paragraph"],
        )
        with self.assertRaisesRegex(ValueError, "at least 1"):
            slack_message_chunks("hello", 0)

    def test_long_progress_update_stays_in_one_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            web = _WebClient()
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test"),
                Path(directory),
                web_client=web,
            )
            client.edit_message("D123", "1700.1", "x" * 13_000)

        self.assertEqual(len(web.calls), 1)
        self.assertEqual(web.calls[0][0], "chat.update")
        rendered = web.calls[0][1]["markdown_text"]
        self.assertEqual(len(rendered), 12_000)
        self.assertTrue(rendered.endswith("[Message truncated to fit Slack.]"))

    def test_socket_connection_is_lazy_and_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket = _SocketClient()
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test"),
                Path(directory),
                web_client=_WebClient(),
                socket_factory=lambda _web: socket,
            )
            self.assertFalse(socket.connected)
            client._ensure_connected()
            self.assertTrue(socket.connected)
            self.assertEqual(len(socket.socket_mode_request_listeners), 1)
            client.close()

        self.assertTrue(socket.closed)

    def test_api_errors_are_provider_errors(self) -> None:
        class FailingWeb(_WebClient):
            def chat_postMessage(self, **kwargs):
                del kwargs
                return {"ok": False, "error": "not_authed"}

        with tempfile.TemporaryDirectory() as directory:
            client = SlackClient(
                SlackConfig("xoxb-test", "xapp-test"),
                Path(directory),
                web_client=FailingWeb(),
            )
            with self.assertRaisesRegex(SlackError, "not_authed"):
                client.send_message("D123", "hello")


class SlackIntegrationTests(unittest.TestCase):
    def test_loads_agent_specific_environment_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "genesis.toml").write_text(
                'schema_version = 1\npackage = "enoch"\n',
                encoding="utf-8",
            )
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                "slack:\n"
                '  bot_token: "saved-bot"\n'
                '  app_token: "saved-app"\n'
                '  allowed_conversation_id: "D123"\n'
                '  allowed_user_id: "U123"\n',
                encoding="utf-8",
            )
            with patch.dict(
                "os.environ",
                {
                    "ENOCH_SLACK_BOT_TOKEN": "env-bot",
                    "ENOCH_SLACK_APP_TOKEN": "env-app",
                },
                clear=False,
            ):
                loaded = load_config(root)

        self.assertEqual(loaded.bot_token, "env-bot")
        self.assertEqual(loaded.app_token, "env-app")
        self.assertEqual(loaded.allowed_conversation_id, "D123")
        self.assertEqual(loaded.allowed_user_id, "U123")

    def test_setup_saves_tokens_and_security_locks_without_echoing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "genesis.toml").write_text(
                'schema_version = 1\npackage = "enoch"\n',
                encoding="utf-8",
            )
            token_result = setup_provider("bot-token xoxb-secret", root)
            setup_provider("app-token xapp-secret", root)
            setup_provider("conversation D123", root)
            setup_provider("user U123", root)
            status = setup_provider("show", root)
            saved = (root / ".enoch" / "config.yaml").read_text(encoding="utf-8")

        self.assertNotIn("xoxb-secret", token_result)
        self.assertIn('bot_token: "xoxb-secret"', saved)
        self.assertIn('app_token: "xapp-secret"', saved)
        self.assertIn('allowed_conversation_id: "D123"', saved)
        self.assertIn('allowed_user_id: "U123"', saved)
        self.assertIn("- bot token: saved", status)
        self.assertIn("- app token: saved", status)


def _request(
    *,
    request_type: str = "events_api",
    payload: dict | None = None,
):
    return SimpleNamespace(
        type=request_type,
        envelope_id="envelope-1",
        payload=payload
        or {
            "type": "event_callback",
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "channel": "D123",
                "user": "U123",
                "ts": "1700.1",
                "text": "hello",
            },
        },
    )


if __name__ == "__main__":
    unittest.main()
