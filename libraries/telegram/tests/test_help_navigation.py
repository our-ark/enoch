import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
PROVIDER_KIT = ROOT.parent / "provider-kit"
sys.path.insert(0, str(PROVIDER_KIT / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_telegram import (
    TelegramClient,
    TelegramConfig,
    TelegramError,
    telegram_event,
    telegram_help_callback_command,
    telegram_help_reply_markup,
)


class TelegramHelpNavigationTests(unittest.TestCase):
    def test_renderer_builds_overview_section_and_command_keyboards(self) -> None:
        overview = telegram_help_reply_markup(
            "overview",
            (
                ("Common", "common"),
                ("Work", "work"),
                ("Evolve", "evolve"),
            ),
        )
        self.assertEqual(
            overview,
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "Common",
                            "callback_data": "enoch:help:s:common",
                        },
                        {
                            "text": "Work",
                            "callback_data": "enoch:help:s:work",
                        },
                    ],
                    [
                        {
                            "text": "Evolve",
                            "callback_data": "enoch:help:s:evolve",
                        }
                    ],
                ]
            },
        )

        section = telegram_help_reply_markup(
            "section",
            (("/do", "do"), ("/task", "task"), ("/queue", "queue")),
        )
        self.assertEqual(
            section["inline_keyboard"][-1],
            [
                {
                    "text": "← All commands",
                    "callback_data": "enoch:help:h",
                }
            ],
        )
        self.assertEqual(
            section["inline_keyboard"][0],
            [
                {"text": "/do", "callback_data": "enoch:help:c:do"},
                {"text": "/task", "callback_data": "enoch:help:c:task"},
            ],
        )

        command = telegram_help_reply_markup("command", ())
        self.assertEqual(
            command,
            {
                "inline_keyboard": [
                    [
                        {
                            "text": "← All commands",
                            "callback_data": "enoch:help:h",
                        }
                    ]
                ]
            },
        )

    def test_client_attaches_navigation_once_at_delivery_time(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True, "result": {"message_id": 9}}

        client._call = fake_call
        client.prepare_help_navigation(
            "overview",
            (("Common", "common"), ("Work", "work")),
        )

        client.send_message(42, "Enoch commands:")
        client.send_message(42, "ordinary response")

        markup = json.loads(calls[0][1]["reply_markup"])
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "Common")
        self.assertNotIn("reply_markup", calls[1][1])

    def test_callback_updates_become_help_commands(self) -> None:
        update = {
            "update_id": 8,
            "callback_query": {
                "id": "callback-1",
                "data": "enoch:help:s:work",
                "message": {
                    "message_id": 4,
                    "chat": {"id": 42},
                    "text": "Enoch commands:",
                },
            },
        }

        event = telegram_event(update)

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.cursor, 9)
        self.assertEqual(event.conversation_id, 42)
        self.assertEqual(event.message_id, 4)
        self.assertEqual(event.text, "/help section:work")
        self.assertEqual(
            telegram_help_callback_command("enoch:help:h"),
            "/help",
        )
        self.assertIsNone(
            telegram_help_callback_command("enoch:help:c:../../restart")
        )

    def test_receive_acknowledges_recognized_callback(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))
        calls = []
        update = {
            "update_id": 8,
            "callback_query": {
                "id": "callback-1",
                "data": "enoch:help:c:cron",
                "message": {
                    "message_id": 4,
                    "chat": {"id": 42},
                    "text": "Work commands:",
                },
            },
        }

        def fake_call(method, payload):
            calls.append((method, payload))
            if method == "getUpdates":
                return {"ok": True, "result": [update]}
            return {"ok": True, "result": True}

        client._call = fake_call

        events = client.receive()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, "/help cron")
        self.assertEqual(
            calls[1],
            (
                "answerCallbackQuery",
                {"callback_query_id": "callback-1"},
            ),
        )

    def test_repeated_button_edit_treats_unchanged_message_as_success(self) -> None:
        client = TelegramClient(TelegramConfig(token="test"))

        def fake_call(_method, _payload):
            raise TelegramError(
                "Bad Request: message is not modified: "
                "specified new message content and reply markup are exactly the same"
            )

        client._call = fake_call
        client.prepare_help_navigation("command", ())

        client.edit_message(42, 4, "Cron commands:")


if __name__ == "__main__":
    unittest.main()
