from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.commands import config_command
from enoch.config import read_section
from enoch.daemon import daemon_paths, plist_bytes
from enoch.git_tools import run_git
from enoch.identity import load_identity
from enoch.immune import _runtime_provider_check
from enoch.providers import (
    Attachment,
    ChatEvent,
    ProviderHealth,
    available_providers,
    load_provider,
    provider_name,
    register_provider,
)
from enoch.telegram.bot import EnochApplication
from enoch.task_queue import enqueue_task, record_task_status_message, task_queue_status


class _Result:
    returncode = 0
    stdout = "custom vcs"
    stderr = ""


class _Vcs:
    name = "test-vcs"
    provider_kind = "vcs"

    def __init__(self) -> None:
        self.calls = []

    def run(self, args, root=None):
        self.calls.append((args, root))
        return _Result()


class _Runtime:
    name = "test-runtime"
    provider_kind = "runtime"
    config_section = "test-runtime"

    def __init__(self) -> None:
        self.messages = []
        self.reset_count = 0

    def respond(self, identity, message, **kwargs):
        self.messages.append((identity, message, kwargs))
        return "Hello from a plugin runtime."

    def act_in_session(self, identity, message, **kwargs):
        return "Plugin work completed."

    def model_summary(self, root=None):
        return "AI model: plugin-model"

    def model_options(self):
        return ()

    def reset_usage(self):
        self.reset_count += 1

    def health(self, root=None):
        return ProviderHealth(
            name="test runtime",
            passed=True,
            command="test-runtime doctor",
            summary="ready",
        )


class _Chat:
    name = "test-chat"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.sent = []
        self.acks = []
        self.downloaded = []
        self.events = []
        self.cursors = []

    @property
    def allowed_conversation_id(self):
        return "room-1"

    def receive(self, cursor=None):
        self.cursors.append(cursor)
        events, self.events = self.events, []
        return events

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return "message-2"

    def edit_message(self, conversation_id, message_id, text):
        return None

    def send_read_ack(self, conversation_id, message_id):
        self.acks.append((conversation_id, message_id))

    def download_attachment(self, attachment, destination, *, max_bytes):
        self.downloaded.append((attachment.file_id, max_bytes))
        destination.write_bytes(b"\xff\xd8\xffplugin-image")


class _EntryPoint:
    name = "chat.entry-chat"

    def load(self):
        return lambda _root=None: _Chat()


class _EntryPoints(list):
    def select(self, *, group):
        return self if group == "enoch.providers" else ()


class EnochProviderTests(unittest.TestCase):
    def test_defaults_preserve_existing_stack(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            self.assertEqual(provider_name("chat", root), "telegram")
            self.assertEqual(provider_name("runtime", root), "codex")
            self.assertEqual(provider_name("vcs", root), "git")
            self.assertEqual(provider_name("forge", root), "github")

    def test_registered_provider_can_be_selected_from_instance_config(self) -> None:
        provider = _Vcs()
        register_provider("vcs", "test-vcs", lambda _root=None: provider, replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  vcs: test-vcs\n", encoding="utf-8")

            result = run_git(["status", "--short"], root)

        self.assertEqual(result.stdout, "custom vcs")
        self.assertEqual(provider.calls, [(["status", "--short"], root)])

    def test_config_lists_and_selects_installed_providers(self) -> None:
        register_provider("runtime", "test-runtime", lambda _root=None: _Runtime(), replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)

            status = config_command("/config providers", root)
            changed = config_command("/config provider runtime test-runtime", root)

            self.assertIn("runtime: codex", status)
            self.assertIn("test-runtime", status)
            self.assertIn("runtime provider set to test-runtime", changed)
            self.assertEqual(provider_name("runtime", root), "test-runtime")

    @patch.dict("os.environ", {}, clear=True)
    def test_config_sets_shows_and_resets_codex_executable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "Codex Runtime" / "codex"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)

            changed = config_command(
                f'/config runtime codex executable "{executable}"',
                root,
            )
            shown = config_command("/config runtime codex executable", root)
            configured = read_section("codex", root)
            reset = config_command("/config runtime codex executable auto", root)

            self.assertEqual(configured["executable"], str(executable))
            self.assertIn(f"Executable: {executable}", changed)
            self.assertIn("Source: Enoch config codex.executable", shown)
            self.assertIn("reset to automatic discovery", reset)
            self.assertNotIn("executable", read_section("codex", root))

    @patch.dict("os.environ", {}, clear=True)
    def test_doctor_reports_configured_codex_executable_source(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "codex"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text(
                f'codex:\n  executable: "{executable}"\n',
                encoding="utf-8",
            )

            check = _runtime_provider_check(root)

        self.assertTrue(check.passed)
        self.assertIn(str(executable), check.summary)
        self.assertIn("source: Enoch config codex.executable", check.summary)

    def test_doctor_uses_selected_runtime_health(self) -> None:
        register_provider("runtime", "test-runtime", lambda _root=None: _Runtime(), replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  runtime: test-runtime\n", encoding="utf-8")

            check = _runtime_provider_check(root)

        self.assertTrue(check.passed)
        self.assertEqual(check.name, "test runtime")
        self.assertEqual(check.command, "test-runtime doctor")

    def test_normalized_chat_event_uses_runtime_without_telegram_payloads(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            runtime = _Runtime()
            bot = EnochApplication(load_identity(), root, chat, runtime=runtime)
            event = ChatEvent(
                cursor=2,
                conversation_id="room-1",
                message_id="message-1",
                text="hello",
            )

            with (
                patch("enoch.telegram.bot.log_conversation_turn"),
                patch("enoch.telegram.bot.ensure_long_term_memory"),
                patch("enoch.telegram.bot._save_telegram_offset"),
                patch.object(bot, "_queue_session_sync"),
            ):
                bot.handle_event(event)

        self.assertEqual(chat.acks, [("room-1", "message-1")])
        self.assertEqual(chat.sent, [("room-1", "Hello from a plugin runtime.")])
        self.assertEqual(runtime.reset_count, 1)
        self.assertEqual(runtime.messages[0][2]["session_key"], "test-chat:room-1")

    def test_custom_chat_provider_persists_opaque_cursor(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            runtime = _Runtime()
            chat.events = [
                ChatEvent(
                    cursor="next-page-token",
                    conversation_id="room-1",
                    message_id="message-1",
                    text="/status",
                )
            ]
            app = EnochApplication(load_identity(), root, chat, runtime=runtime)

            with (
                patch("enoch.application.log_conversation_turn"),
                patch("enoch.application.ensure_long_term_memory"),
            ):
                app.run_once()
                restarted = EnochApplication(load_identity(), root, chat, runtime=runtime)
                restarted.run_once()

            state = (root / ".enoch" / "channels" / "test-chat" / "cursor.json").read_text(
                encoding="utf-8"
            )

        self.assertEqual(chat.cursors, [None, "next-page-token"])
        self.assertIn('"cursor": "next-page-token"', state)
        self.assertIn("Test Chat conversation id: room-1", chat.sent[0][1])
        self.assertNotIn("Telegram", chat.sent[0][1])

    def test_custom_chat_provider_supplies_image_attachment(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            chat = _Chat()
            runtime = _Runtime()
            app = EnochApplication(load_identity(), root, chat, runtime=runtime)
            event = ChatEvent(
                cursor="image-2",
                conversation_id="room-1",
                message_id="message-1",
                text="What is this?",
                attachments=(
                    Attachment(
                        kind="image",
                        file_id="plugin-file-1",
                        mime_type="image/jpeg",
                        size=128,
                    ),
                ),
            )

            with (
                patch("enoch.application.log_conversation_turn"),
                patch("enoch.application.ensure_long_term_memory"),
            ):
                app.handle_event(event)

            image_path = runtime.messages[0][2]["image_paths"][0]

        self.assertEqual(chat.downloaded[0][0], "plugin-file-1")
        self.assertFalse(image_path.exists())
        self.assertIn("Test Chat conversation", runtime.messages[0][1])
        self.assertNotIn("Telegram image boundary", runtime.messages[0][1])

    def test_custom_chat_provider_selects_generic_daemon_launcher(self) -> None:
        register_provider("chat", "test-chat", lambda _root=None: _Chat(), replace=True)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / ".enoch" / "config.yaml"
            config.parent.mkdir()
            config.write_text("providers:\n  chat: test-chat\n", encoding="utf-8")
            paths = daemon_paths(root, home=root / "home")

            payload = plist_bytes(paths).decode("utf-8")

        self.assertIn("enoch-agent", payload)
        self.assertNotIn("enoch-telegram", payload)

    def test_string_chat_and_message_ids_survive_task_persistence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job = enqueue_task("room-1", "plugin task", root)
            record_task_status_message(job.id, "message-9", root)

            pending = task_queue_status(root).pending[0]

        self.assertEqual(pending.chat_id, "room-1")
        self.assertEqual(pending.status_message_id, "message-9")

    def test_builtin_providers_are_discoverable(self) -> None:
        self.assertIn("telegram", available_providers("chat"))
        self.assertIn("codex", available_providers("runtime"))
        self.assertEqual(load_provider("vcs", name="git").name, "git")

    def test_installed_entry_point_loads_without_core_changes(self) -> None:
        with patch(
            "enoch.providers.registry.metadata.entry_points",
            return_value=_EntryPoints([_EntryPoint()]),
        ):
            provider = load_provider("chat", name="entry-chat")

        self.assertEqual(provider.name, "test-chat")
        self.assertEqual(provider.provider_kind, "chat")


if __name__ == "__main__":
    unittest.main()
