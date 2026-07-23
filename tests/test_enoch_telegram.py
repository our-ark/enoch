from pathlib import Path
from datetime import datetime, timezone
import json
import os
import sys
import threading
import time
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.identity import load_identity
from enoch import brain
from enoch.brain import CodexAccessUnavailable
from enoch.automatic_learning import learning_index_path
from enoch.backlog import add_backlog_item, backlog_status
from enoch.config import read_section
from enoch.command_surface import checktree as _checktree
from enoch.cron import add_cron_job, cron_status
from enoch.evolution.core import (
    MODE_AUTO_EVOLVE,
    evolve_report,
    get_evolve_candidate,
    load_evolve_candidates,
    set_evolve_mode,
    set_evolve_schedule,
    set_evolve_theme,
)
from enoch.evolution.sources.experience import load_experience_records, record_task_experience
from enoch.evolution.events import load_evolve_events
from enoch.git_tools import GitError
from our_ark_github.workflow import (
    LocalPublishResult,
    PublishError,
    PullRequestMergeCandidate,
    PullRequestCloseResult,
    PullRequestMergeResult,
    PullRequestResult,
    PullRequestTarget,
    RemotePublishResult,
)
from enoch.immune import DoctorDiagnosis
from enoch.lineage.core import (
    AncestorLink,
    LineageCandidate,
    LineageResolution,
    load_inbox_candidates,
)
from enoch.logs import log_conversation_turn, log_system_event
from enoch.prompt_append import (
    EDIT_REQUEST_END,
    EDIT_REQUEST_START,
    MEMORY_REQUEST_END,
    MEMORY_REQUEST_START,
    TASK_REGRESSION_END,
    TASK_REGRESSION_START,
)
from enoch.providers.runtime import FunctionAgentRuntime
from enoch.providers import AgentRuntimeTimedOut
from enoch.tasks.queue import (
    TaskJob,
    begin_next_task,
    complete_task,
    enqueue_task,
    fail_task,
    pause_task,
    record_task_result,
    task_queue_path,
    task_queue_status,
)
from enoch.tasks.events import load_task_events
from enoch.tasks.worktree import TaskWorktree
from enoch.app.core import (
    EnochApplication,
    ShutdownRequested,
    TaskContextSnapshot,
    WorkStatusMessage,
    _CURRENT_WORK_STATUS,
    _action_sandbox,
    _begin_lifecycle_run,
    _format_task_final_message,
    _parse_task_context_snapshot,
    _record_lifecycle_shutdown,
    _shutdown_message,
    _signal_reason,
    _task_context_snapshot_prompt,
)
from enoch.app import core as telegram
from enoch.channel import (
    MAX_IMAGE_BYTES as MAX_TELEGRAM_IMAGE_BYTES,
    load_channel_lifecycle,
    previous_shutdown_warning as _previous_shutdown_warning,
    save_channel_cursor,
)
from our_ark_telegram import (
    READ_ACK_EMOJI,
    TelegramClient,
    TelegramConfig,
    TelegramError,
    load_config,
    telegram_event,
)
from enoch.operations.update_tools import (
    ensure_local_main_current,
    main_pull_summary as _main_pull_summary,
)


_REAL_TASK_CONTEXT_RESOLVER = EnochApplication._resolve_task_context_snapshot


class _ImmediateTimer:
    def __init__(self, _seconds, callback) -> None:
        self.callback = callback
        self.daemon = False

    def start(self) -> None:
        self.callback()

    def cancel(self) -> None:
        return None


class EnochTelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self._task_context_snapshot_patch = patch(
            "enoch.app.core.EnochApplication._resolve_task_context_snapshot",
            return_value=TaskContextSnapshot(),
        )
        self.resolve_task_context_snapshot = self._task_context_snapshot_patch.start()
        self.addCleanup(self._task_context_snapshot_patch.stop)
        self._sync_session_activity_patch = patch("enoch.app.core._sync_session_activity")
        self.sync_session_activity = self._sync_session_activity_patch.start()
        self.addCleanup(self._sync_session_activity_patch.stop)
        self._save_telegram_offset_patch = patch(
            "enoch.app.core.save_channel_cursor",
            side_effect=_save_test_offset,
        )
        self.save_telegram_offset = self._save_telegram_offset_patch.start()
        self.addCleanup(self._save_telegram_offset_patch.stop)
        self._log_conversation_turn_patch = patch("enoch.app.core.log_conversation_turn")
        self.log_conversation_turn = self._log_conversation_turn_patch.start()
        self.addCleanup(self._log_conversation_turn_patch.stop)
        self._log_system_event_patch = patch("enoch.app.core.log_system_event")
        self.log_system_event = self._log_system_event_patch.start()
        self.addCleanup(self._log_system_event_patch.stop)
        self._update_memory_patch = patch("enoch.app.core.ensure_long_term_memory")
        self.update_memory = self._update_memory_patch.start()
        self.addCleanup(self._update_memory_patch.stop)

    def _capture_direct_work_worker(self, bot: EnochApplication):
        started = []

        def start(job, *, session_key):
            started.append((job, session_key))

        return started, patch.object(bot, "_start_direct_work_worker", side_effect=start)

    @patch.dict("os.environ", {"ENOCH_TELEGRAM_BOT_TOKEN": "token", "ENOCH_TELEGRAM_ALLOWED_CHAT_ID": "42"})
    def test_loads_config_from_environment(self) -> None:
        config = load_config()

        self.assertEqual(config.token, "token")
        self.assertEqual(config.allowed_chat_id, 42)

    @patch.dict("os.environ", {}, clear=True)
    def test_loads_config_from_local_file(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".enoch").mkdir()
            (root / ".enoch" / "config.yaml").write_text(
                "\n".join(
                    [
                        "telegram:",
                        '  bot_token: "file-token"',
                        "  allowed_chat_id: 42",
                        "  poll_timeout: 10",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(root=root)

        self.assertEqual(config.token, "file-token")
        self.assertEqual(config.allowed_chat_id, 42)
        self.assertEqual(config.poll_timeout, 10)

    @patch("our_ark_telegram.core.request.urlopen")
    def test_downloads_telegram_file_with_size_limit(self, urlopen: MagicMock) -> None:
        response = MagicMock()
        response.headers = {"Content-Length": "16"}
        response.read.return_value = b"\xff\xd8\xfftelegram-photo"
        urlopen.return_value.__enter__.return_value = response
        client = TelegramClient(TelegramConfig(token="secret-token", poll_timeout=5))

        with TemporaryDirectory() as temp:
            destination = Path(temp) / "image.jpg"
            with patch.object(
                client,
                "_call",
                return_value={"result": {"file_path": "photos/image.jpg"}},
            ):
                client.download_file("file-id", destination, max_bytes=1024)

            self.assertEqual(destination.read_bytes(), b"\xff\xd8\xfftelegram-photo")

        request_object = urlopen.call_args.args[0]
        self.assertTrue(request_object.full_url.endswith("/photos/image.jpg"))
        response.read.assert_called_once_with(1025)

    @patch("enoch.app.core.respond")
    def test_photo_is_downloaded_attached_and_deleted(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            def inspect_image(_identity, prompt, **kwargs):
                image_path = kwargs["image_paths"][0]
                self.assertTrue(image_path.exists())
                self.assertEqual(image_path.read_bytes(), b"\xff\xd8\xfftelegram-photo")
                self.assertIn("这是什么花？", prompt)
                self.assertEqual(kwargs["session_key"], "telegram:42")
                return "这是一朵向日葵。"

            respond.side_effect = inspect_image
            _handle_update(bot, _photo_update(chat_id=42, caption="这是什么花？"))

            image_dir = root / ".enoch" / "channels" / "telegram" / "images"
            self.assertEqual(list(image_dir.iterdir()), [])

        self.assertEqual(client.downloads, [("large-photo", MAX_TELEGRAM_IMAGE_BYTES)])
        self.assertEqual(client.sent, [(42, "这是一朵向日葵。")])

    @patch("enoch.app.core.respond")
    def test_photo_without_caption_gets_natural_image_prompt(
        self,
        respond: MagicMock,
    ) -> None:
        respond.return_value = "I can see a dog."
        with TemporaryDirectory() as temp:
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), Path(temp), client)

            _handle_update(bot, _photo_update(chat_id=42))

        prompt = respond.call_args.args[1]
        self.assertIn("without a caption", prompt)
        self.assertEqual(client.sent, [(42, "I can see a dog.")])

    @patch("enoch.app.core.respond")
    def test_photo_from_unlocked_chat_is_not_downloaded(
        self,
        respond: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            client = FakeTelegramClient(allowed_chat_id=7)
            bot = EnochApplication(load_identity(), Path(temp), client)

            _handle_update(bot, _photo_update(chat_id=42))

        respond.assert_not_called()
        self.assertEqual(client.downloads, [])
        self.assertEqual(client.sent, [])

    @patch.dict(
        "os.environ",
        {
            "ENOCH_TELEGRAM_BOT_TOKEN": "env-token",
            "ENOCH_TELEGRAM_ALLOWED_CHAT_ID": "7",
            "ENOCH_TELEGRAM_POLL_TIMEOUT": "5",
        },
        clear=True,
    )
    def test_environment_overrides_local_config(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".enoch").mkdir()
            (root / ".enoch" / "config.yaml").write_text(
                "\n".join(
                    [
                        "telegram:",
                        '  bot_token: "file-token"',
                        "  allowed_chat_id: 42",
                        "  poll_timeout: 10",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(root=root)

        self.assertEqual(config.token, "env-token")
        self.assertEqual(config.allowed_chat_id, 7)
        self.assertEqual(config.poll_timeout, 5)

    @patch.dict("os.environ", {}, clear=True)
    def test_config_requires_token(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(TelegramError, "telegram.bot_token"):
                load_config(root=Path(temp))

    @patch.dict(
        "os.environ",
        {"ENOCH_TELEGRAM_BOT_TOKEN": "token", "ENOCH_TELEGRAM_ALLOWED_CHAT_ID": "not-a-number"},
        clear=True,
    )
    def test_invalid_allowed_chat_id_is_config_error(self) -> None:
        with self.assertRaisesRegex(TelegramError, "allowed chat id must be a whole number"):
            load_config()

    @patch.dict(
        "os.environ",
        {"ENOCH_TELEGRAM_BOT_TOKEN": "token", "ENOCH_TELEGRAM_POLL_TIMEOUT": "not-a-number"},
        clear=True,
    )
    def test_invalid_poll_timeout_is_config_error(self) -> None:
        with self.assertRaisesRegex(TelegramError, "poll timeout must be a whole number"):
            load_config()

    @patch.dict(
        "os.environ",
        {"ENOCH_TELEGRAM_BOT_TOKEN": "token", "ENOCH_TELEGRAM_POLL_TIMEOUT": "0"},
        clear=True,
    )
    def test_poll_timeout_must_be_positive(self) -> None:
        with self.assertRaisesRegex(TelegramError, "poll timeout must be at least 1"):
            load_config()

    @patch("builtins.print")
    @patch.dict("os.environ", {}, clear=True)
    def test_main_exits_cleanly_without_token(self, print_: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            previous = Path.cwd()
            try:
                os.chdir(temp)
                with self.assertRaises(SystemExit):
                    telegram.main("telegram")
            finally:
                os.chdir(previous)

        printed = print_.call_args.args[0]
        self.assertIn("telegram.bot_token", printed)
        self.assertIn("ENOCH_TELEGRAM_BOT_TOKEN", printed)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.respond", return_value="Hello from Enoch")
    def test_replies_to_allowed_chat(
        self,
        respond: MagicMock,
        log_conversation_turn: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="hello"))

        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("Enoch wrapper instructions:", respond.call_args.args[1])
        self.assertIn("/do", respond.call_args.args[1])
        self.assertIn("/task", respond.call_args.args[1])
        self.assertEqual(client.acks, [(42, 1001, READ_ACK_EMOJI)])
        self.assertEqual(client.sent, [(42, "Hello from Enoch")])
        log_conversation_turn.assert_called_once()
        self.assertEqual(log_conversation_turn.call_args.kwargs["chat_id"], 42)
        self.assertEqual(log_conversation_turn.call_args.kwargs["message"], "hello")
        self.assertIn("Hello from Enoch", log_conversation_turn.call_args.kwargs["reply"])
        update_memory.assert_called_once_with(ROOT)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_replies_do_not_include_accumulated_input_tokens(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        def answer(*_args, **_kwargs):
            brain.update_token_usage(input_tokens=321, cached_input_tokens=300, output_tokens=12)
            return "Hello from Enoch"

        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        with patch("enoch.app.core.respond", side_effect=answer):
            _handle_update(bot, _message_update(chat_id=42, text="hello"))

        self.assertEqual(client.sent, [(42, "Hello from Enoch")])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.respond", return_value="[PLAIN_TEXT_MARKER]\nIntent:\nAdd reminders")
    def test_plain_marker_text_is_not_special_for_normal_chat(
        self,
        respond: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="add reminders"))

        respond.assert_called_once()
        self.assertIn("[PLAIN_TEXT_MARKER]", client.sent[0][1])
        self.assertNotIn("local = edit on a feature branch", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.remember_memory", return_value={"id": "mem_1", "text": "User likes apples."})
    @patch(
        "enoch.app.core.respond",
        return_value=(
            "I will remember that."
            f"\n\n{MEMORY_REQUEST_START}\nUser likes apples.\n{MEMORY_REQUEST_END}"
        ),
    )
    def test_memory_request_marker_is_saved_by_wrapper(
        self,
        respond: MagicMock,
        remember_memory: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="I like apples"))

        respond.assert_called_once()
        remember_memory.assert_called_once_with("User likes apples.", root=root)
        sent = client.sent[0][1]
        self.assertIn("I will remember that.", sent)
        self.assertIn("Saved to Enoch long-term memory.", sent)
        self.assertNotIn(MEMORY_REQUEST_START, sent)
        self.assertNotIn(MEMORY_REQUEST_END, sent)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.remember_memory", side_effect=OSError("read-only"))
    @patch(
        "enoch.app.core.respond",
        return_value=(
            "I should remember that."
            f"\n\n{MEMORY_REQUEST_START}\nUser likes apples.\n{MEMORY_REQUEST_END}"
        ),
    )
    def test_memory_request_reports_save_failure(
        self,
        _respond: MagicMock,
        remember_memory: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="I like apples"))

        remember_memory.assert_called_once_with("User likes apples.", root=root)
        sent = client.sent[0][1]
        self.assertIn("Enoch could not save that long-term memory.", sent)
        self.assertNotIn(MEMORY_REQUEST_START, sent)

    @patch("enoch.app.core.respond")
    def test_ignores_disallowed_chat(self, respond: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=7, text="hello"))

        respond.assert_not_called()
        self.assertEqual(client.acks, [])
        self.assertEqual(client.sent, [])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.respond", return_value="natural reply")
    def test_unknown_slash_command_routes_to_natural_conversation(
        self,
        respond: MagicMock,
        log_conversation_turn: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/unknown"))

        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        log_conversation_turn.assert_called_once()
        update_memory.assert_called_once_with(ROOT)
        self.assertEqual(client.sent[0][1], "natural reply")

    @patch("enoch.app.core.model_summary", return_value="AI model: gpt-5-codex")
    def test_telegram_command_parser_accepts_bot_mentions(self, _model_summary: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/status@EnochBot"))

        self.assertIn("Enoch status:", client.sent[0][1])

    def test_startup_notification_goes_to_locked_chat(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            bot.notify_startup()

        self.assertEqual(client.sent[0][0], 42)
        self.assertIn("Enoch restarted and is listening on Telegram.", client.sent[0][1])
        self.assertNotIn("Action mode:", client.sent[0][1])
        self.assertIn("Last git main pull observed:", client.sent[0][1])
        self.assertIn("/help", client.sent[0][1])
        self.sync_session_activity.assert_called_once()
        self.assertIn("Enoch startup context:", self.sync_session_activity.call_args.args[3])

    def test_startup_notification_reports_previous_shutdown_warning(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(
            load_identity(),
            ROOT,
            client,
            previous_shutdown_warning="Previous shutdown: unexpected; Enoch could not send the normal shutdown message.",
        )

        bot.notify_startup()

        self.assertIn("Previous shutdown: unexpected", client.sent[0][1])

    def test_startup_notification_requires_locked_chat(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochApplication(load_identity(), ROOT, client)

        bot.notify_startup()

        self.assertEqual(client.sent, [])

    def test_shutdown_notification_goes_to_locked_chat(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            bot.notify_shutdown("SIGTERM")

        self.assertEqual(client.sent[0][0], 42)
        self.assertIn("Enoch is shutting down.", client.sent[0][1])
        self.assertIn("Reason: SIGTERM.", client.sent[0][1])
        self.assertNotIn("Action mode:", client.sent[0][1])

    def test_shutdown_notification_requires_locked_chat(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochApplication(load_identity(), ROOT, client)

        bot.notify_shutdown("SIGTERM")

        self.assertEqual(client.sent, [])

    def test_shutdown_message_includes_reason(self) -> None:
        message = _shutdown_message(load_identity(), ROOT, "keyboard interrupt")

        self.assertIn("Enoch is shutting down.", message)
        self.assertIn("Reason: keyboard interrupt.", message)
        self.assertNotIn("Action mode:", message)

    def test_signal_reason_uses_signal_name(self) -> None:
        self.assertEqual(_signal_reason(15), "SIGTERM")

    def test_lifecycle_run_warns_after_unexpected_prior_running_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".enoch").mkdir()
            lifecycle = root / ".enoch" / "channels" / "telegram" / "lifecycle.json"
            lifecycle.parent.mkdir(parents=True)
            lifecycle.write_text(
                json.dumps({"status": "running"}),
                encoding="utf-8",
            )

            warning = _begin_lifecycle_run(root, provider="telegram")
            state = _load_lifecycle_state(root)

        self.assertIn("Previous shutdown: unexpected", warning)
        self.assertEqual(state["status"], "running")

    def test_lifecycle_run_records_started_head(self) -> None:
        current_repository_revision = MagicMock(return_value="1111111111111111111111111111111111111111")
        with TemporaryDirectory() as temp:
            root = Path(temp)

            with patch("enoch.channel.current_repository_revision", current_repository_revision):
                _begin_lifecycle_run(root, provider="telegram")
            state = _load_lifecycle_state(root)

        current_repository_revision.assert_called_once_with(root)
        self.assertEqual(state["started_head"], "1111111111111111111111111111111111111111")

    def test_lifecycle_run_warns_when_shutdown_notice_was_not_sent(self) -> None:
        warning = _previous_shutdown_warning(
            {"status": "stopped", "shutdown_notification_sent": False}
        )

        self.assertIn("could not send the normal shutdown message", warning)

    def test_lifecycle_run_is_quiet_after_clean_shutdown_notice(self) -> None:
        warning = _previous_shutdown_warning(
            {"status": "stopped", "shutdown_notification_sent": True}
        )

        self.assertEqual(warning, "")

    def test_records_lifecycle_shutdown_notice_status(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            _record_lifecycle_shutdown(
                root,
                "SIGTERM",
                shutdown_notification_sent=True,
                provider="telegram",
            )
            state = _load_lifecycle_state(root)

        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["reason"], "SIGTERM")
        self.assertTrue(state["shutdown_notification_sent"])

    @patch("enoch.providers.vcs.GitVersionControlProvider.run")
    def test_main_pull_summary_reports_fetch_head_timestamp_and_main_sha(self, run_git: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fetch_head = root / ".git" / "FETCH_HEAD"
            fetch_head.parent.mkdir()
            fetch_head.write_text("abc123\t\tbranch 'main' of https://github.com/our-ark/enoch\n", encoding="utf-8")
            os.utime(fetch_head, (1_700_000_000, 1_700_000_000))
            run_git.side_effect = [
                MagicMock(returncode=0, stdout="abc1234"),
                MagicMock(returncode=0, stdout=".git/FETCH_HEAD"),
            ]

            summary = _main_pull_summary(root)

        self.assertIn("Last git main pull observed:", summary)
        self.assertIn("2023-", summary)
        self.assertIn("origin/main abc1234", summary)

    @patch("enoch.providers.vcs.GitVersionControlProvider.run")
    def test_main_pull_summary_is_unavailable_without_main_fetch(self, run_git: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fetch_head = root / ".git" / "FETCH_HEAD"
            fetch_head.parent.mkdir()
            fetch_head.write_text("abc123\t\tbranch 'feature' of https://github.com/our-ark/enoch\n", encoding="utf-8")
            run_git.side_effect = [
                MagicMock(returncode=0, stdout="abc1234"),
                MagicMock(returncode=0, stdout=".git/FETCH_HEAD"),
            ]

            summary = _main_pull_summary(root)

        self.assertEqual(summary, "Last git main pull observed: unavailable (origin/main abc1234)")

    def test_start_points_to_help(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/start"))

        self.assertEqual(client.sent[0][1], "Use /help to see available commands.")

    def test_help_lists_safe_commands(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help"))

        reply = client.sent[0][1]
        self.assertLess(reply.index("/help - show this command list"), reply.index("Common:"))
        self.assertLess(reply.index("Common:"), reply.index("Work:"))
        self.assertLess(reply.index("Work:"), reply.index("/do <request>"))
        self.assertLess(reply.index("Work:"), reply.index("Inherit:"))
        self.assertLess(reply.index("Inherit:"), reply.index("/ancestors"))
        self.assertLess(reply.index("Inherit:"), reply.index("Learn:"))
        self.assertLess(reply.index("Learn:"), reply.index("/skills"))
        self.assertLess(reply.index("Learn:"), reply.index("Evolve:"))
        self.assertLess(reply.index("Evolve:"), reply.index("/evolve"))
        self.assertLess(reply.index("Evolve:"), reply.index("System:"))
        self.assertIn("Common:", client.sent[0][1])
        self.assertNotIn("Memory:", client.sent[0][1])
        self.assertIn("Inherit:", client.sent[0][1])
        self.assertIn("Learn:", client.sent[0][1])
        self.assertNotIn("Vision:", client.sent[0][1])
        self.assertNotIn("Send a photo", client.sent[0][1])
        self.assertIn("Evolve:", client.sent[0][1])
        self.assertIn("Work:", client.sent[0][1])
        self.assertIn("System:", client.sent[0][1])
        self.assertNotIn("Mode:", client.sent[0][1])
        self.assertIn("/doctor", client.sent[0][1])
        self.assertNotIn("/debug", client.sent[0][1])
        self.assertNotIn("/mode [chat|work]", client.sent[0][1])
        self.assertLess(client.sent[0][1].index("/mission [text]"), client.sent[0][1].index("/status"))
        self.assertIn("/self - show Enoch's identity, role, ancestor, and mission", client.sent[0][1])
        self.assertIn("/status", client.sent[0][1])
        self.assertIn("/mission [text] - show or update Enoch's mission", client.sent[0][1])
        self.assertNotIn("/thinking", client.sent[0][1])
        self.assertNotIn("/lineage", client.sent[0][1])
        self.assertIn("/ancestors - show ancestor chain and ancestor skills", client.sent[0][1])
        self.assertIn("/inherit - show inheritable direct-parent changes", client.sent[0][1])
        self.assertNotIn("/memory", client.sent[0][1])
        self.assertIn("/skills [agent-or-path] - show declared skills", client.sent[0][1])
        self.assertNotIn("/teach", client.sent[0][1])
        self.assertIn("/learn <skill> from <agent> - adapt a published skill from another agent", client.sent[0][1])
        self.assertIn("/do <request> - run work now instead of queueing it", client.sent[0][1])
        self.assertIn("/task <request> - queue background work for Enoch", client.sent[0][1])
        self.assertNotIn("/task cancel <id> - cancel a queued background task", client.sent[0][1])
        self.assertIn("/queue - show running, queued, and recent task history", client.sent[0][1])
        self.assertNotIn("/tasks", client.sent[0][1])
        self.assertIn("/stop - stop the currently running task", client.sent[0][1])
        self.assertIn("/backlog [p0|p1|p2] <request> - save deferred work for idle time", client.sent[0][1])
        self.assertNotIn("/backlog remove <id> - remove a pending backlog item", client.sent[0][1])
        self.assertNotIn("/backlog priority <id> p0|p1|p2 - reprioritize backlog work", client.sent[0][1])
        self.assertNotIn("/cron every <interval> <request> - schedule recurring work", client.sent[0][1])
        self.assertNotIn("/cron cancel <id> - cancel a scheduled job", client.sent[0][1])
        self.assertIn("/cron - show scheduled jobs", client.sent[0][1])
        self.assertIn("/evolve - show self-evolution mode, theme, and top candidate", client.sent[0][1])
        self.assertIn("/feedback - show feedback signals available to self-evolution", client.sent[0][1])
        self.assertIn("/experience - show task provenance statistics and evolution candidates", client.sent[0][1])
        self.assertIn("/propose - rank all evolve sources and propose the strongest candidate", client.sent[0][1])
        self.assertNotIn("/evolve mode <mode> - set self-evolution behavior", client.sent[0][1])
        self.assertNotIn("/evolve mode disabled|co-evolve|auto-evolve", client.sent[0][1])
        self.assertNotIn("/evolve theme <text> - set the current self-evolution theme", client.sent[0][1])
        self.assertNotIn("/evolve list - show current self-evolution candidates", client.sent[0][1])
        self.assertNotIn("/evolve select <id> - select a self-evolution candidate", client.sent[0][1])
        self.assertNotIn("/evolve run <id> - queue a self-evolution candidate as a task", client.sent[0][1])
        self.assertNotIn("/evolve reject <id> - reject a self-evolution candidate", client.sent[0][1])
        self.assertNotIn("/evolve schedule <text> - let Enoch interpret common schedule text", client.sent[0][1])
        self.assertNotIn("/evolve schedule off - stop scheduled evolve checks", client.sent[0][1])
        self.assertNotIn("/evolve schedule once a day - run evolve once per day", client.sent[0][1])
        self.assertNotIn("/evolve schedule every <interval> - run periodic evolve checks", client.sent[0][1])
        self.assertNotIn("/evolve schedule daily HH:MM - run evolve once per day at local time", client.sent[0][1])
        self.assertNotIn("/evolve schedule cron '30 9 * * *' - run evolve with a cron-style daily schedule", client.sent[0][1])
        self.assertIn("/update", client.sent[0][1])
        self.assertIn("/config - show or update local system settings", client.sent[0][1])
        self.assertNotIn("/resume", client.sent[0][1])
        self.assertIn("/restart - restart Enoch's chat daemon from the locked conversation", client.sent[0][1])
        self.assertNotIn("/shutdown", client.sent[0][1])
        self.assertIn("say the request naturally", client.sent[0][1])

    def test_help_config_shows_only_config_commands(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help config"))

        reply = client.sent[0][1]
        self.assertIn("Config commands:", reply)
        self.assertIn("/config profiles", reply)
        self.assertIn("/config profile <name|default>", reply)
        self.assertIn("/config model <name>", reply)
        self.assertIn("/config model default", reply)
        self.assertIn("/config reasoning-effort low|medium|high", reply)
        self.assertIn("/config reasoning-effort default", reply)
        self.assertIn("/config task-timeout <duration>", reply)
        self.assertIn("/config task-timeout default", reply)
        self.assertIn("/config runtime <provider>", reply)
        self.assertIn("/config runtime <provider> <setting> [value]", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    @patch("enoch.app.core.merge_pull_request")
    def test_pr_merge_requires_explicit_target(self, merge: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/pr merge"))

        self.assertIn("/pr merge <PR number or PR URL>", client.sent[0][1])
        merge.assert_not_called()

    @patch("enoch.app.core.merge_pull_request")
    def test_pr_merge_is_ignored_from_unauthorized_chat(self, merge: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=99, text="/pr merge 12"))

        self.assertEqual(client.sent, [])
        merge.assert_not_called()

    @patch("enoch.app.core.merge_pull_request")
    def test_pr_merge_requires_configured_chat_lock(self, merge: MagicMock) -> None:
        client = FakeTelegramClient()
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/pr merge 12"))

        self.assertIn("locked Telegram conversation", client.sent[0][1])
        merge.assert_not_called()

    @patch("enoch.app.core.merge_pull_request")
    def test_pr_merge_reports_success_for_exact_target(self, merge: MagicMock) -> None:
        merge.return_value = PullRequestMergeResult(
            number=12,
            url="https://github.com/our-ark/enoch/pull/12",
            method="merge",
            merge_commit="def456",
            message="Pull Request successfully merged",
        )
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot,
            _message_update(
                chat_id=42,
                text="/pr merge https://github.com/our-ark/enoch/pull/12",
            )
        )

        merge.assert_called_once_with("https://github.com/our-ark/enoch/pull/12", ROOT)
        self.assertIn("Merged PR #12.", client.sent[0][1])
        self.assertIn("https://github.com/our-ark/enoch/pull/12", client.sent[0][1])
        self.assertIn("Merge commit: def456", client.sent[0][1])

    @patch("enoch.app.core.merge_pull_request", side_effect=PublishError("PR #12 is a draft."))
    def test_pr_merge_reports_github_workflow_error(self, merge: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/pr merge 12"))

        merge.assert_called_once_with("12", ROOT)
        self.assertEqual(
            client.sent[0][1],
            "Enoch could not merge that pull request: PR #12 is a draft.",
        )

    def test_help_resume_reports_removed_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help resume"))

        self.assertEqual(
            client.sent[0][1],
            "No help found for /resume.\nUse /help to see available commands.",
        )

    def test_help_lists_pr_and_explains_its_subcommands(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help"))
        _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/help pr"))

        self.assertIn(
            "/pr - list and manage pull requests",
            client.sent[0][1],
        )
        self.assertNotIn("/pr merge", client.sent[0][1])
        self.assertIn("/pr - list open pull requests", client.sent[1][1])
        self.assertIn("/pr show <PR number or PR URL>", client.sent[1][1])
        self.assertIn("/pr merge <PR number or PR URL>", client.sent[1][1])
        self.assertIn("will not infer one", client.sent[1][1])

    @patch("enoch.app.core.list_open_pull_requests")
    def test_pr_lists_open_pull_requests(
        self,
        list_open_pull_requests: MagicMock,
    ) -> None:
        list_open_pull_requests.return_value = (
            _pull_request_info(
                number=13,
                title="Add PR commands",
                head_branch="feature/pr-commands",
            ),
            _pull_request_info(
                number=12,
                title="Document config precedence",
                is_draft=True,
                mergeable="UNKNOWN",
                merge_state_status="BLOCKED",
            ),
        )
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/pr"))

        list_open_pull_requests.assert_called_once_with(ROOT)
        reply = client.sent[0][1]
        self.assertIn("Open pull requests (2):", reply)
        self.assertIn("#13 [ready] Add PR commands", reply)
        self.assertIn("main <- feature/pr-commands", reply)
        self.assertIn("#12 [draft] Document config precedence", reply)

    @patch("enoch.app.core.list_open_pull_requests", return_value=())
    def test_pr_reports_when_no_pull_requests_are_open(
        self,
        list_open_pull_requests: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/pr"))

        list_open_pull_requests.assert_called_once_with(ROOT)
        self.assertEqual(client.sent[0][1], "Open pull requests: none.")

    @patch("enoch.app.core.inspect_pull_request")
    def test_pr_show_reports_read_only_detail(
        self,
        inspect_pull_request: MagicMock,
    ) -> None:
        inspect_pull_request.return_value = _pull_request_info(
            number=13,
            title="Add PR commands",
            head_branch="feature/pr-commands",
            author="enoch",
        )
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/pr show 13"))

        inspect_pull_request.assert_called_once_with("13", ROOT)
        reply = client.sent[0][1]
        self.assertIn("Pull request #13", reply)
        self.assertIn("Status: ready", reply)
        self.assertIn("Branch: main <- feature/pr-commands", reply)
        self.assertIn("Author: enoch", reply)

    def test_config_shows_sets_and_resets_task_timeout(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/config"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/config task-timeout 30m"))
            configured = read_section("task", root)
            _handle_update(bot, _message_update(update_id=3, chat_id=42, text="/config task-timeout default"))
            reset = read_section("task", root)
            _handle_update(bot, _message_update(update_id=4, chat_id=42, text="/config task-timeout off"))

        self.assertIn("Task timeout: 10m (default)", client.sent[0][1])
        self.assertIn("Task timeout set to 30m.", client.sent[1][1])
        self.assertEqual(configured["timeout_seconds"], "1800")
        self.assertIn("Task timeout set to 10m (default).", client.sent[2][1])
        self.assertNotIn("timeout_seconds", reset)
        self.assertIn("must look like 10m, 30m, or 2h", client.sent[3][1])

    def test_config_shows_sets_and_resets_codex_model_and_reasoning(self) -> None:
        with TemporaryDirectory() as temp, TemporaryDirectory() as codex_home:
            root = Path(temp)
            (Path(codex_home) / "config.toml").write_text(
                "\n".join(
                    [
                        'model = "gpt-global"',
                        'model_reasoning_effort = "medium"',
                    ]
                ),
                encoding="utf-8",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.dict("os.environ", {"CODEX_HOME": codex_home}, clear=True):
                _handle_update(bot, _message_update(chat_id=42, text="/config"))
                _handle_update(bot,
                    _message_update(
                        update_id=2,
                        chat_id=42,
                        text="/config model gpt-enoch-local",
                    )
                )
                _handle_update(bot,
                    _message_update(
                        update_id=3,
                        chat_id=42,
                        text="/config reasoning-effort high",
                    )
                )
                configured = read_section("codex", root)
                _handle_update(bot,
                    _message_update(
                        update_id=4,
                        chat_id=42,
                        text="/config model default",
                    )
                )
                _handle_update(bot,
                    _message_update(
                        update_id=5,
                        chat_id=42,
                        text="/config reasoning_effort default",
                    )
                )
                reset = read_section("codex", root)

        self.assertIn("AI model: gpt-global", client.sent[0][1])
        self.assertIn("Reasoning effort: medium", client.sent[0][1])
        self.assertIn("AI model: gpt-enoch-local", client.sent[1][1])
        self.assertIn("Model source: Enoch config codex.model", client.sent[1][1])
        self.assertIn("Reasoning effort: high", client.sent[2][1])
        self.assertEqual(configured["model"], "gpt-enoch-local")
        self.assertEqual(configured["reasoning_effort"], "high")
        self.assertIn("AI model: gpt-global", client.sent[3][1])
        self.assertIn("Reasoning effort: medium", client.sent[4][1])
        self.assertNotIn("model", reset)
        self.assertNotIn("reasoning_effort", reset)

    def test_config_model_lists_installed_models_and_marks_current(self) -> None:
        options = (
            brain.CodexModelOption(
                slug="gpt-5.6-sol",
                display_name="GPT-5.6-Sol",
                description="Latest frontier agentic coding model.",
            ),
            brain.CodexModelOption(
                slug="gpt-5.6-terra",
                display_name="GPT-5.6-Terra",
                description="Balanced agentic coding model for everyday work.",
            ),
            brain.CodexModelOption(
                slug="gpt-5.5",
                display_name="GPT-5.5",
                description="Previous frontier model.",
            ),
        )
        runtime = FunctionAgentRuntime(
            respond_fn=lambda *_args, **_kwargs: "",
            act_in_session_fn=lambda *_args, **_kwargs: "",
            model_summary_fn=lambda _root=None: "AI model: gpt-5.6-terra",
            model_options_fn=lambda: options[:2],
            reset_usage_fn=lambda: None,
        )
        runtime.model_catalog_label = "Available GPT-5.6 models:"
        runtime.model_example = "gpt-5.6-sol"
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client, runtime=runtime)

            _handle_update(bot, _message_update(chat_id=42, text="/config model"))

        reply = client.sent[0][1]
        self.assertIn("Available GPT-5.6 models:", reply)
        self.assertIn("- gpt-5.6-sol - Latest frontier", reply)
        self.assertIn("- gpt-5.6-terra [current]", reply)
        self.assertNotIn("gpt-5.5", reply)
        self.assertIn("Example: /config model gpt-5.6-sol", reply)
        self.assertIn("private or future Codex rollouts", reply)

    def test_help_inherit_shows_inherit_usage(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help inherit"))

        reply = client.sent[0][1]
        self.assertIn("Inherit commands:", reply)
        self.assertIn("/inherit - show inheritable direct-parent changes", reply)
        self.assertIn("/inherit show - show inheritable direct-parent changes", reply)
        self.assertIn("/inherit <change_id> - inherit one direct-parent change", reply)
        self.assertIn("/inherit all - inherit all direct-parent changes", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    def test_help_topic_shows_single_command_usage(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help task cancel"))

        reply = client.sent[0][1]
        self.assertIn("Task commands:", reply)
        self.assertIn("/task <request> - queue background work for Enoch", reply)
        self.assertIn("/task cancel <id> - cancel a queued background task", reply)
        self.assertIn("/task resume <id|all> - continue paused tasks with the same ids", reply)
        self.assertIn("/task retry <id> - retry a failed task as a new linked task", reply)
        self.assertNotIn("/task regress", reply)
        self.assertNotIn("/task resolve", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    def test_help_topic_supports_work_command_aliases(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help /crons"))

        reply = client.sent[0][1]
        self.assertIn("Cron commands:", reply)
        self.assertIn("/cron every <interval> <request>", reply)
        self.assertIn("/cron cancel <id>", reply)

    def test_help_queue_shows_canonical_queue_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help queue"))

        self.assertEqual(
            client.sent[0][1],
            "/queue - show running, queued, and recent task history",
        )

    def test_help_backlog_shows_backlog_subcommands(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help backlog"))

        reply = client.sent[0][1]
        self.assertIn("Backlog commands:", reply)
        self.assertIn("/backlog remove <id>", reply)
        self.assertIn("/backlog priority <id> p0|p1|p2", reply)
        self.assertIn("/backlog promote <id>", reply)

    def test_help_evolve_shows_evolve_usage(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help evolve"))

        reply = client.sent[0][1]
        self.assertIn("Evolve commands:", reply)
        self.assertNotIn("/feedback", reply)
        self.assertNotIn("/experience", reply)
        self.assertNotIn("/propose", reply)
        self.assertIn("/evolve - show self-evolution mode, theme, and top candidate", reply)
        self.assertIn("/evolve mode <mode>", reply)
        self.assertIn("Modes: disabled, co-evolve, auto-evolve.", reply)
        self.assertNotIn("/evolve mode disabled|co-evolve|auto-evolve", reply)
        self.assertNotIn("/evolve co-evolve - propose candidates", reply)
        self.assertNotIn("/evolve disabled - stop collecting", reply)
        self.assertNotIn("/evolve auto-evolve - select bounded", reply)
        self.assertIn("/evolve theme [text]", reply)
        self.assertNotIn("/evolve explore", reply)
        self.assertIn("/evolve list", reply)
        self.assertNotIn("/evolve candidates", reply)
        self.assertNotIn("/evolve select <id>", reply)
        self.assertNotIn("/evolve run <id>", reply)
        self.assertNotIn("/evolve reject <id>", reply)
        self.assertIn("/evolve approve <id>", reply)
        self.assertIn("/evolve retry <id>", reply)
        self.assertIn("/evolve remove <id>", reply)
        self.assertIn("/evolve schedule <text>", reply)
        self.assertNotIn("/evolve schedule off - stop scheduled evolve checks", reply)
        self.assertNotIn("/evolve schedule once a day - run evolve once per day", reply)
        self.assertNotIn("/evolve schedule every <interval> - run periodic evolve checks", reply)
        self.assertNotIn("/evolve schedule daily HH:MM - run evolve once per day at local time", reply)
        self.assertNotIn("/evolve schedule cron '30 9 * * *' - run evolve with a cron-style daily schedule", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    def test_evolve_related_top_level_commands_have_separate_help_topics(self) -> None:
        topics = {
            "feedback": "/feedback - show feedback signals available to self-evolution",
            "experience": "/experience - show task provenance statistics and evolution candidates",
            "propose": "/propose - rank all evolve sources and propose the strongest candidate",
        }
        for index, (topic, expected) in enumerate(topics.items(), start=1):
            with self.subTest(topic=topic):
                client = FakeTelegramClient(allowed_chat_id=42)
                bot = EnochApplication(load_identity(), ROOT, client)

                _handle_update(bot, _message_update(update_id=index, chat_id=42, text=f"/help {topic}"))

                self.assertEqual(client.sent[0][1], expected)

    def test_help_debug_reports_unknown_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help /debug"))

        self.assertEqual(client.sent[0][1], "No help found for /debug.\nUse /help to see available commands.")

    def test_help_shutdown_reports_removed_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help shutdown"))

        self.assertEqual(client.sent[0][1], "No help found for /shutdown.\nUse /help to see available commands.")

    def test_help_topic_reports_unknown_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/help nope"))

        self.assertEqual(client.sent[0][1], "No help found for /nope.\nUse /help to see available commands.")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_command_enqueues_persistent_fifo_job(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))

            data = json.loads(task_queue_path(root).read_text(encoding="utf-8"))
            events = load_task_events(root, task_id=1)

        self.assertEqual(len(client.sent), 1)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Status: queued", client.sent[0][1])
        self.assertIn("Latest update: Queued at position 1.", client.sent[0][1])
        self.assertIn("PRs created:\n- none", client.sent[0][1])
        self.assertIn("Request:\nadd queued work", client.sent[0][1])
        self.assertEqual(data["pending"][0]["id"], 1)
        self.assertEqual(data["pending"][0]["chat_id"], 42)
        self.assertEqual(data["pending"][0]["text"], "add queued work")
        self.assertEqual(data["pending"][0]["source"], "task")
        self.assertEqual(data["pending"][0]["initiated_by"], "human")
        self.assertEqual([event.event for event in events], ["created", "queued"])
        self.assertTrue(all(event.event_actor == "human" for event in events))
        self.assertIsNone(data["running"])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_command_includes_replied_message_context(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot,
                _message_update(
                    chat_id=42,
                    text="/task handle this",
                    reply_text="Original bug report details.",
                )
            )

            data = json.loads(task_queue_path(root).read_text(encoding="utf-8"))

        request = data["pending"][0]["text"]
        self.assertIn("handle this", request)
        self.assertIn("Context from replied Telegram message:", request)
        self.assertIn("Original bug report details.", request)
        self.assertIn("Original bug report details.", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_command_stores_conversation_context_snapshot(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            context="Build the reminders feature discussed earlier.",
            source="chat-snapshot",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task do it"))

            data = json.loads(task_queue_path(root).read_text(encoding="utf-8"))

        self.resolve_task_context_snapshot.assert_called_once_with(42, "do it")
        self.assertEqual(data["pending"][0]["text"], "do it")
        self.assertEqual(data["pending"][0]["context"], "Build the reminders feature discussed earlier.")
        self.assertEqual(data["pending"][0]["context_source"], "chat-snapshot")
        self.assertIn("Conversation context snapshot:", client.sent[0][1])
        self.assertIn("Build the reminders feature discussed earlier.", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_command_asks_for_clarification_before_queueing(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            clarification="Which feature should Enoch implement?"
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task do it"))
            status = task_queue_status(root)

        self.assertEqual(status.pending, ())
        self.assertIn("Which feature should Enoch implement?", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_command_requires_request(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/task"))

        self.assertEqual(client.sent[0][1], "Use /task <request> to queue background work.")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_cancel_removes_pending_task_and_edits_status(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/task cancel 1"))
            status = task_queue_status(root)

        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertIn("Cancelled task #1.", client.sent[-1][1])
        self.assertIn("Status: cancelled", client.edited[-1][2])
        self.assertIn("Latest update: Cancelled before running.", client.edited[-1][2])

    def test_task_retry_queues_new_linked_task_and_preserves_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(
                42,
                "retry transient work",
                root,
                context="Keep the original context.",
                context_source="chat-snapshot",
            )
            begin_next_task(root)
            fail_task(original.id, root, result="temporary failure")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot,
                _message_update(chat_id=42, text="/task retry 1")
            )
            status = task_queue_status(root)
            events = load_task_events(root, task_id=2)
            _handle_update(bot,
                _message_update(update_id=2, chat_id=42, text="/queue")
            )
            tasks_report = client.sent[-1][1]

        self.assertEqual([(job.id, job.status) for job in status.history], [(1, "failed")])
        self.assertEqual(len(status.pending), 1)
        retry = status.pending[0]
        self.assertEqual(retry.id, 2)
        self.assertEqual(retry.parent_task_id, 1)
        self.assertEqual(retry.text, original.text)
        self.assertEqual(retry.context, original.context)
        self.assertEqual(retry.trigger, "/task retry")
        self.assertIn("Task #2", client.sent[0][1])
        self.assertIn("Retry of failed task #1", client.sent[0][1])
        self.assertEqual([event.event for event in events], ["created", "queued"])
        self.assertIn("#2 [pending] retry transient work (retry of #1)", tasks_report)

    def test_task_retry_reconciles_existing_pr_before_execution(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "publish the PR command", root)
            begin_next_task(root)
            fail_task(
                original.id,
                root,
                result="Worker state was lost before completion.",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            existing_result = (
                "Implemented and published ready for review: "
                "https://github.com/our-ark/enoch/pull/13"
            )

            with patch(
                "enoch.app.core._latest_direct_action_result_for_task",
                return_value=existing_result,
            ):
                with patch(
                    "enoch.app.core.inspect_pull_request",
                    return_value=_pull_request_info(
                        number=13,
                        title="Add PR command",
                    ),
                ) as inspect:
                    with patch.object(bot, "_maybe_start_task_worker"):
                        _handle_update(bot,
                            _message_update(chat_id=42, text="/task retry 1")
                        )
            retry = task_queue_status(root).pending[0]
            running = begin_next_task(root)
            with patch.object(bot, "_run_direct_work") as run_direct_work:
                bot._run_task_job(running)
            status = task_queue_status(root)

        inspect.assert_called_once_with(
            "https://github.com/our-ark/enoch/pull/13",
            root,
        )
        self.assertEqual(retry.parent_task_id, original.id)
        self.assertEqual(
            retry.pr_urls,
            ("https://github.com/our-ark/enoch/pull/13",),
        )
        self.assertEqual(retry.result, existing_result)
        run_direct_work.assert_not_called()
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].pr_urls, retry.pr_urls)

    def test_task_retry_reconciles_pr_from_preserved_branch(self) -> None:
        job = TaskJob(
            id=7,
            chat_id=42,
            text="publish PR command",
            created_at="2026-07-18T21:10:00+00:00",
            started_at="2026-07-18T21:10:01+00:00",
            status="failed",
            branch_name="feature/pr-command",
        )
        pull_request = _pull_request_info(
            number=14,
            title="Add PR command",
            head_branch="feature/pr-command",
        )

        with patch(
            "enoch.app.core._latest_direct_action_result_for_task",
            return_value="",
        ):
            with patch(
                "enoch.app.core.list_open_pull_requests",
                return_value=(pull_request,),
            ) as list_pull_requests:
                result = telegram._reconciled_retry_result(job, ROOT)

        list_pull_requests.assert_called_once_with(ROOT)
        self.assertIn("Reconciled existing PR #14", result)
        self.assertIn(pull_request.url, result)

    def test_task_retry_restores_linked_evolve_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "retry evolve work", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            bot._evolve_approve("backlog-1")
            original = begin_next_task(root)
            with patch.object(
                bot,
                "_run_direct_work",
                return_value="Enoch could not complete evolve work: invalid request",
            ):
                bot._run_task_job(original)
            self.assertEqual(
                get_evolve_candidate("backlog-1", root).status,
                "failed",
            )
            client.sent.clear()
            client.edited.clear()

            _handle_update(bot,
                _message_update(chat_id=42, text="/task retry 1")
            )
            status = task_queue_status(root)
            candidate = get_evolve_candidate("backlog-1", root)
            evolve_events = load_evolve_events(root, candidate_id="backlog-1")

        self.assertEqual(candidate.status, "running")
        self.assertEqual(status.pending[0].candidate_id, "backlog-1")
        self.assertEqual(status.pending[0].parent_task_id, 1)
        self.assertEqual(status.pending[0].approval_actor, "human")
        self.assertEqual(evolve_events[-1].event, "queued")
        self.assertEqual(evolve_events[-1].trigger, "/task retry")
        self.assertEqual(evolve_events[-1].retry_of_task_id, 1)

    @patch(
        "enoch.app.core.respond",
        return_value=(
            "I found the regression and rolled it back.\n"
            f"{TASK_REGRESSION_START}\n"
            '{"task_id": 1, "reason": "Recovery broke after deployment.", '
            '"resolution": "reverted"}\n'
            f"{TASK_REGRESSION_END}"
        ),
    )
    def test_agent_records_reverted_regression_from_natural_turn(
        self,
        respond: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "ship risky work", root)
            complete_task(begin_next_task(root).id, root, result="Shipped.")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="the last deploy broke recovery"))
            status = task_queue_status(root)
            events = load_task_events(root, task_id=original.id)
            record = next(
                item
                for item in load_experience_records(root)
                if item.task_id == original.id
            )

        self.assertEqual(status.history[0].status, "reverted")
        self.assertEqual(
            [event.event for event in events[-3:]],
            ["completed", "regressed", "reverted"],
        )
        self.assertTrue(record.regressed)
        self.assertEqual(record.regression_resolution, "reverted")
        self.assertEqual(client.sent[0][1], "I found the regression and rolled it back.")
        self.assertNotIn(TASK_REGRESSION_START, client.sent[0][1])
        self.assertEqual(events[-1].event_actor, "agent")
        self.assertEqual(events[-1].trigger, "agent-regression-signal")
        self.assertIn("Enoch owns regression bookkeeping", respond.call_args.args[1])

    def test_completed_fix_task_automatically_resolves_original_regression(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "ship risky work", root)
            complete_task(begin_next_task(root).id, root, result="Shipped.")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            fix = enqueue_task(42, "repair recovery", root, parent_task_id=original.id)
            running_fix = begin_next_task(root)
            assert running_fix is not None

            result = (
                "Recovery is fixed and verified.\n"
                f"{TASK_REGRESSION_START}\n"
                '{"task_id": 1, "reason": "Original recovery path failed.", '
                '"resolution": "forward-fixed"}\n'
                f"{TASK_REGRESSION_END}"
            )
            with patch.object(bot, "_run_direct_work", return_value=result):
                bot._run_task_job(running_fix)
            _handle_update(bot,
                _message_update(update_id=2, chat_id=42, text="/experience")
            )
            status = task_queue_status(root)
            record = next(
                item
                for item in load_experience_records(root)
                if item.task_id == original.id
            )
            events = load_task_events(root, task_id=original.id)

        self.assertEqual(status.history[0].status, "forward-fixed")
        self.assertEqual(record.regression_related_task_id, fix.id)
        self.assertNotIn(TASK_REGRESSION_START, client.sent[0][1])
        self.assertIn("Regressions: 1/2 completed tasks (50.0%)", client.sent[-1][1])
        self.assertIn(
            "Regression resolution: forward-fixed 1, reverted 0, unresolved 0",
            client.sent[-1][1],
        )
        self.assertIn("Regression sources:", client.sent[-1][1])
        self.assertIn("task 1", client.sent[-1][1])
        self.assertIn("Regression initiated by: agent 0, human 1", client.sent[-1][1])
        self.assertIn("regression forward-fixed by task-2", client.sent[-1][1])
        self.assertEqual(events[-1].event_actor, "agent")
        self.assertEqual(events[-1].trigger, "agent-regression-signal")

    def test_failed_fix_task_leaves_original_regression_unresolved(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(42, "ship risky work", root)
            complete_task(begin_next_task(root).id, root)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            enqueue_task(42, "repair recovery", root, parent_task_id=original.id)
            running_fix = begin_next_task(root)
            assert running_fix is not None

            result = (
                "Enoch could not complete the requested work yet: tests failed.\n"
                f"{TASK_REGRESSION_START}\n"
                '{"task_id": 1, "reason": "Original recovery path failed.", '
                '"resolution": "forward-fixed"}\n'
                f"{TASK_REGRESSION_END}"
            )
            with patch.object(bot, "_run_direct_work", return_value=result):
                bot._run_task_job(running_fix)
            status = task_queue_status(root)

        self.assertEqual(status.history[0].status, "regressed")
        self.assertEqual(status.history[1].status, "failed")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_stop_cancels_running_task_and_edits_status(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _started, start_worker = self._capture_direct_work_worker(bot)
            with start_worker:
                _handle_update(bot, _message_update(chat_id=42, text="/do long edit"))
            status = task_queue_status(root)
            assert status.running is not None
            event = threading.Event()
            bot._task_cancellations[status.running.id] = event

            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/stop"))
            status = task_queue_status(root)

        self.assertTrue(event.is_set())
        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertIn("Stopped task #1.", client.sent[-1][1])
        self.assertIn("Status: cancelled", client.edited[-1][2])
        self.assertIn("Latest update: Stopped by /stop.", client.edited[-1][2])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_stop_reports_when_no_task_is_running(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/stop"))

        self.assertEqual(client.sent[0][1], "No running task to stop.")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_queue_command_shows_queue_and_history(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task first queued work"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/task second queued work"))
            _handle_update(bot, _message_update(update_id=3, chat_id=42, text="/task cancel 2"))
            _handle_update(bot, _message_update(update_id=4, chat_id=42, text="/queue"))

        reply = client.sent[-1][1]
        self.assertIn("Running: none", reply)
        self.assertIn("#1 [pending] first queued work", reply)
        self.assertIn("#2 [cancelled] second queued work", reply)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_queue_command_shows_history_pr_url(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None
            complete_task(job.id, root, result="Opened pull request: https://github.com/our-ark/enoch/pull/3")
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/queue"))

        reply = client.sent[-1][1]
        self.assertIn("#1 [completed] add queued work", reply)
        self.assertIn("PR: https://github.com/our-ark/enoch/pull/3", reply)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_tasks_command_remains_a_hidden_queue_alias(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/tasks"))

        self.assertIn("Running: none", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_worker_edits_single_status_message(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            context="Use the agreed task snapshot.",
            source="chat-snapshot",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None

            def run_task(*_args, **_kwargs):
                bot._send_step_update(42, "Editing files.")
                return "Done with the task."

            with patch.object(bot, "_run_direct_work", side_effect=run_task) as run_direct_work:
                bot._run_task_job(job)
            experiences = load_experience_records(root)

        self.assertEqual(len(client.sent), 2)
        self.assertGreaterEqual(len(client.edited), 3)
        self.assertEqual({message_id for _chat_id, message_id, _text in client.edited}, {2001})
        self.assertIn("Status: running", client.edited[0][2])
        self.assertIn("Latest update: Editing files.", "\n".join(text for _chat_id, _message_id, text in client.edited))
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("Latest update: Completed. Final summary sent below.", client.edited[-1][2])
        self.assertNotIn("Done with the task.", client.edited[-1][2])
        self.assertIn("Task #1 final update", client.sent[-1][1])
        self.assertIn("Final status: completed", client.sent[-1][1])
        self.assertIn("PR URL:\n- none", client.sent[-1][1])
        self.assertIn("Result summary:\nDone with the task.", client.sent[-1][1])
        self.assertEqual(run_direct_work.call_args.kwargs["context"], "Use the agreed task snapshot.")
        self.assertEqual(len(experiences), 1)
        self.assertEqual(experiences[0].outcome, "completed")
        self.assertEqual(experiences[0].request, "add queued work")
        self.assertEqual(experiences[0].command, "/task")
        log_conversation_turn.assert_called()

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_evolve_task_worker_creates_one_status_message_for_all_progress(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship evolve status updates", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot,
                _message_update(chat_id=42, text="/evolve approve backlog-1")
            )
            job = begin_next_task(root)
            assert job is not None

            def run_task(*_args, **_kwargs):
                bot._send_step_update(42, "Preparing a fresh branch.")
                bot._send_step_update(42, "Working.")
                bot._send_progress(42, 60, "workspace-write")
                return "Done with evolve work."

            with patch.object(bot, "_run_direct_work", side_effect=run_task):
                bot._run_task_job(job)
            completed = task_queue_status(root).history[-1]

        self.assertEqual(len(client.sent), 3)
        self.assertIn("Approved evolve candidate backlog-1", client.sent[0][1])
        self.assertIn("Task #1", client.sent[1][1])
        self.assertIn("Task #1 final update", client.sent[2][1])
        self.assertFalse(
            any("Enoch update:" in text for _chat_id, text in client.sent)
        )
        self.assertFalse(
            any("Enoch is still working" in text for _chat_id, text in client.sent)
        )
        self.assertGreaterEqual(len(client.edited), 4)
        self.assertEqual(
            {message_id for _chat_id, message_id, _text in client.edited},
            {2002},
        )
        edited_text = "\n".join(text for _chat_id, _message_id, text in client.edited)
        self.assertIn("Latest update: Preparing a fresh branch.", edited_text)
        self.assertIn("Latest update: Working.", edited_text)
        self.assertIn("Still working after 1 minute", edited_text)
        self.assertEqual(completed.status_message_id, 2002)

    def test_task_worker_pauses_on_codex_access_error_and_resumes_same_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None

            with patch.object(
                bot,
                "_run_direct_work",
                side_effect=CodexAccessUnavailable("Codex authentication is unavailable."),
            ):
                bot._run_task_job(job)
            paused_status = task_queue_status(root)
            paused_events = load_task_events(root, task_id=job.id)

            with patch.object(bot, "_maybe_start_task_worker") as start_worker:
                _handle_update(
                    bot,
                    _message_update(
                        update_id=2,
                        chat_id=42,
                        text=f"/task resume {job.id}",
                    ),
                )
            resumed_status = task_queue_status(root)
            resumed_events = load_task_events(root, task_id=job.id)

        self.assertIsNone(paused_status.running)
        self.assertEqual(paused_status.history, ())
        self.assertEqual(paused_status.paused[0].id, job.id)
        self.assertEqual(paused_status.paused[0].status, "paused")
        self.assertEqual(paused_events[-1].event, "paused")
        self.assertEqual(paused_events[-1].trigger, "codex-unavailable")
        self.assertIn("Status: paused", client.edited[-2][2])
        self.assertIn(f"use /task resume {job.id}", client.sent[-2][1].lower())
        self.assertEqual(resumed_status.paused, ())
        self.assertEqual(resumed_status.pending[0].id, job.id)
        self.assertEqual(resumed_events[-1].event, "resumed")
        self.assertEqual(resumed_events[-1].trigger, "/task resume")
        self.assertIn("Resumed 1 task: #1.", client.sent[-1][1])
        self.assertIn("Status: queued", client.edited[-1][2])
        start_worker.assert_called_once()

    def test_task_resume_selects_one_paused_task_and_all_alias_resumes_rest(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = enqueue_task(42, "first paused task", root)
            second = enqueue_task(42, "second paused task", root)
            pause_task(begin_next_task(root).id, root, result="No Codex access.")
            pause_task(begin_next_task(root).id, root, result="No Codex access.")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker") as start_worker:
                _handle_update(bot,
                    _message_update(chat_id=42, text=f"/task resume {second.id}")
                )
                selected_status = task_queue_status(root)
                _handle_update(bot,
                    _message_update(
                        update_id=2,
                        chat_id=42,
                        text="/task resume all",
                    )
                )
            final_status = task_queue_status(root)
            first_events = load_task_events(root, task_id=first.id)
            second_events = load_task_events(root, task_id=second.id)

        self.assertEqual([job.id for job in selected_status.pending], [second.id])
        self.assertEqual([job.id for job in selected_status.paused], [first.id])
        self.assertEqual(final_status.paused, ())
        self.assertEqual(
            [job.id for job in final_status.pending],
            [first.id, second.id],
        )
        self.assertEqual(first_events[-1].trigger, "/task resume")
        self.assertEqual(second_events[-1].trigger, "/task resume")
        self.assertIn(f"Resumed 1 task: #{second.id}.", client.sent[0][1])
        self.assertIn(f"Resumed 1 task: #{first.id}.", client.sent[1][1])
        self.assertEqual(start_worker.call_count, 2)

    def test_task_worker_stops_queue_after_codex_access_error(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            enqueue_task(42, "first task", root)
            second = enqueue_task(42, "second task", root)

            with patch.object(
                bot,
                "_run_direct_work",
                side_effect=CodexAccessUnavailable("Codex usage quota is currently unavailable."),
            ) as run_direct_work:
                bot._run_task_worker()
            status = task_queue_status(root)

        self.assertEqual(run_direct_work.call_count, 1)
        self.assertEqual(status.paused_count, 1)
        self.assertEqual(status.pending, (second,))
        self.assertEqual(status.history, ())

    def test_task_is_saved_as_paused_when_context_snapshot_has_no_codex_access(self) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            codex_unavailable_reason="Codex authentication is unavailable."
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/task preserve this work"))
            status = task_queue_status(root)

        self.assertEqual(status.pending, ())
        self.assertEqual(status.paused_count, 1)
        self.assertEqual(status.paused[0].text, "preserve this work")
        self.assertIn("Status: paused", client.sent[0][1])
        self.assertIn("Use /task resume 1", client.sent[0][1])

    def test_task_worker_records_learning_only_for_skill_changes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task add skill"))
            job = begin_next_task(root)
            assert job is not None

            with patch.object(
                bot,
                "_run_direct_work",
                return_value="\n".join(["Done.", "Files:", "- src/enoch/skills/cron/SKILL.md"]),
            ):
                bot._run_task_job(job)
            learning_lines = learning_index_path(root).read_text(encoding="utf-8").splitlines()

        payload = json.loads(learning_lines[0])
        self.assertEqual(payload["artifact_type"], "skill")
        self.assertEqual(payload["skill_names"], ["cron"])
        self.assertEqual(payload["request"], "add skill")

    @patch("enoch.app.core.respond", return_value="Build the reminders feature discussed earlier.")
    def test_task_context_snapshot_resolver_uses_chat_session(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            snapshot = _REAL_TASK_CONTEXT_RESOLVER(bot, 42, "do it")

        self.assertEqual(snapshot.context, "Build the reminders feature discussed earlier.")
        self.assertEqual(snapshot.source, "chat-snapshot")
        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("Task context snapshot request:", respond.call_args.args[1])
        self.assertIn("do it", respond.call_args.args[1])

    def test_task_context_snapshot_parser_handles_sentinel_responses(self) -> None:
        self.assertEqual(_parse_task_context_snapshot("No extra context needed.").context, "")
        clarification = _parse_task_context_snapshot("NEEDS_CLARIFICATION: Which file should Enoch update?")

        self.assertEqual(clarification.clarification, "Which file should Enoch update?")
        self.assertIn("NEEDS_CLARIFICATION:", _task_context_snapshot_prompt("do it"))

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_command_stores_priority_and_context_snapshot(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            context="Use the backlog context snapshot.",
            source="chat-snapshot",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/backlog p0 do it later"))
            status = backlog_status(root)

        self.resolve_task_context_snapshot.assert_called_once_with(42, "do it later")
        self.assertEqual(status.pending_count, 1)
        self.assertEqual(status.pending[0].priority, "p0")
        self.assertEqual(status.pending[0].context, "Use the backlog context snapshot.")
        self.assertIn("Backlog #1 [p0] saved.", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_command_defaults_to_p1(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/backlog do it eventually"))
            status = backlog_status(root)

        self.assertEqual(status.pending[0].priority, "p1")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_command_asks_for_clarification_before_adding(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            clarification="Which deferred item should Enoch save?"
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/backlog do it"))
            status = backlog_status(root)

        self.assertEqual(status.pending, ())
        self.assertIn("Which deferred item should Enoch save?", client.sent[0][1])

    def test_backlog_idle_promotion_moves_item_to_task_queue(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            backlog_item = add_backlog_item(
                42,
                "background cleanup",
                root,
                priority="p2",
                context="Use saved context.",
                context_source="chat-snapshot",
            )

            job = bot._promote_next_backlog_if_idle()
            queue = task_queue_status(root)
            backlog = backlog_status(root)
            events = load_task_events(root, task_id=queue.pending[0].id)

        self.assertEqual(job.text, "background cleanup")
        self.assertEqual(job.context, "Use saved context.")
        self.assertEqual(queue.pending[0].id, job.id)
        self.assertEqual(backlog.pending, ())
        self.assertEqual(backlog.history[-1].id, backlog_item.id)
        self.assertEqual(backlog.history[-1].promoted_task_id, job.id)
        self.assertEqual(events[0].event_actor, "system")
        self.assertEqual(events[0].trigger, "backlog-idle")
        self.assertIn("Promoted from backlog #1 (p2).", client.sent[0][1])

    def test_backlog_idle_promotion_waits_for_empty_task_queue(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            add_backlog_item(42, "background cleanup", root, priority="p0")
            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/task active work"))

            job = bot._promote_next_backlog_if_idle()
            queue = task_queue_status(root)
            backlog = backlog_status(root)

        self.assertIsNone(job)
        self.assertEqual(queue.pending_count, 1)
        self.assertEqual(backlog.pending_count, 1)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_command_can_remove_and_reprioritize(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/backlog p2 first"))
                _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/backlog priority 1 p0"))
                _handle_update(bot, _message_update(update_id=3, chat_id=42, text="/backlog remove 1"))
            status = backlog_status(root)

        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].status, "removed")
        self.assertIn("priority is now p0", client.sent[1][1])
        self.assertIn("Removed backlog #1.", client.sent[2][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_cancel_points_to_remove_without_adding_item(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/backlog cancel 1"))
            status = backlog_status(root)

        self.assertEqual(status.pending_count, 0)
        self.assertIn("Use /backlog remove <id>", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_command_can_manually_promote(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/backlog p1 first"))
                _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/backlog promote 1"))
            queue = task_queue_status(root)
            backlog = backlog_status(root)
            events = load_task_events(root, task_id=queue.pending[0].id)

        self.assertEqual(queue.pending_count, 1)
        self.assertEqual(backlog.history[-1].promoted_task_id, queue.pending[0].id)
        self.assertEqual(queue.pending[0].source, "backlog")
        self.assertEqual(queue.pending[0].initiated_by, "human")
        self.assertEqual(events[0].event_actor, "human")
        self.assertEqual(events[0].trigger, "/backlog promote")
        self.assertIn("Promoted backlog #1 to task #1.", client.sent[2][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_backlog_report_lists_pending_and_history(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(bot, _message_update(chat_id=42, text="/backlog p0 first"))
                _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/backlog"))

        self.assertIn("Backlog:", client.sent[1][1])
        self.assertIn("#1 [p0 pending] first", client.sent[1][1])

    def test_evolve_reports_top_candidate_from_backlog(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve"))

        reply = client.sent[0][1]
        self.assertIn("Evolve:", reply)
        self.assertIn("Mode: co-evolve", reply)
        self.assertIn("- backlog: 1", reply)
        self.assertIn("backlog-1 [candidate backlog] improve Telegram work UX", reply)
        self.assertIn("wait for human approval", reply)

    @patch(
        "enoch.app.core.format_reconcile_result",
        return_value="Recorded governed promotion.",
    )
    @patch("enoch.app.core.reconcile_evolve_candidate")
    def test_evolve_reconcile_supports_explicit_backfill(
        self,
        reconcile_evolve_candidate: MagicMock,
        _format_reconcile_result: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot,
                _message_update(
                    chat_id=42,
                    text="/evolve reconcile feedback-c3ed71fd1d2d backfill",
                )
            )

        reconcile_evolve_candidate.assert_called_once_with(
            "feedback-c3ed71fd1d2d",
            root,
            recording_mode="backfill",
        )
        self.assertEqual(client.sent[0][1], "Recorded governed promotion.")

    def test_feedback_lists_evolve_feedback_signals(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            log_conversation_turn(
                chat_id=42,
                message="The evolve proposal is broken.",
                reply="I will inspect it.",
                root=root,
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/feedback"))

        reply = client.sent[0][1]
        self.assertIn("Feedback:", reply)
        self.assertIn("[complaint x1] The evolve proposal is broken.", reply)

    def test_experience_lists_candidates_from_work_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_cron_job(42, "review recurring recovery", 3600, root)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/experience"))

        reply = client.sent[0][1]
        self.assertIn("Experience:", reply)
        self.assertIn("cron-1 [candidate experience] Review recurring workflow #1", reply)

    def test_experience_lists_recent_journal_records(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record_task_experience(
                TaskJob(
                    id=7,
                    chat_id=42,
                    text="improve Telegram recovery",
                    created_at="2026-07-18T00:00:00+00:00",
                    started_at="2026-07-18T00:01:00+00:00",
                    completed_at="2026-07-18T00:02:00+00:00",
                    status="completed",
                    result="Recovery tests passed.",
                    context_source="chat-snapshot",
                ),
                root,
                command="/task",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/experience"))

        reply = client.sent[0][1]
        self.assertIn("Task statistics:", reply)
        self.assertIn("backlog 0", reply)
        self.assertIn("brainstorming 0", reply)
        self.assertIn("chat-task 0", reply)
        self.assertIn("task 1", reply)
        self.assertIn("Initiated by: agent 0, human 1", reply)
        self.assertIn("Recent tasks:", reply)
        self.assertIn("task-7 [completed] improve Telegram recovery", reply)
        self.assertIn("source task; initiated by human; trigger /task; context chat-snapshot", reply)
        self.assertIn("Result: Recovery tests passed.", reply)

    def test_propose_ranks_all_sources_without_selecting_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/propose"))
            candidates = load_evolve_candidates(root)
            events = load_evolve_events(root)
            queued = task_queue_status(root)

        reply = client.sent[0][1]
        self.assertIn("Enoch proposes:", reply)
        self.assertIn("Ranked 1 actionable candidate(s) from the six evolve sources.", reply)
        self.assertIn("Deterministic fallback recommendation", reply)
        self.assertIn("backlog-1 [candidate backlog] improve Telegram work UX", reply)
        self.assertIn("Approve with /evolve approve backlog-1.", reply)
        self.assertIn("Remove with /evolve remove backlog-1.", reply)
        self.assertEqual(candidates[0].status, "candidate")
        self.assertEqual([event.event for event in events], ["checked", "proposed"])
        self.assertEqual([event.event_actor for event in events], ["human", "human"])
        self.assertEqual(events[-1].trigger, "/propose")
        self.assertEqual(events[-1].candidate_id, "backlog-1")
        self.assertTrue(events[-1].proposal_id.startswith("proposal-"))
        self.assertTrue(events[-1].curation_id.startswith("curation-"))
        self.assertEqual(events[-1].recommendation_kind, "deterministic-fallback")

    def test_propose_displays_llm_recommendation_and_remove_suggestions_without_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "context-only background note", root, priority="p0")
            add_backlog_item(42, "add bounded curation coverage", root, priority="p1")
            response = json.dumps(
                {
                    "recommended_candidate_id": "backlog-2",
                    "recommendation_reason": "This is the clearest bounded code change.",
                    "scope_guidance": "Limit the work to curation tests.",
                    "risk_guidance": "Avoid broad refactors.",
                    "test_plan_guidance": "Run focused tests and doctor.",
                    "remove_suggestions": [
                        {
                            "candidate_id": "backlog-1",
                            "classification": "context-only",
                            "reason": "The note does not request an implementation change.",
                        }
                    ],
                    "new_candidates": [],
                }
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_respond_read_only_turn", return_value=response):
                _handle_update(bot, _message_update(chat_id=42, text="/propose"))
            candidates = load_evolve_candidates(root)
            events = load_evolve_events(root)
            queued = task_queue_status(root)

        reply = client.sent[0][1]
        self.assertIn("LLM recommended candidate:", reply)
        self.assertIn("backlog-2 [candidate backlog]", reply)
        self.assertIn("LLM remove suggestions (no status changed):", reply)
        self.assertIn("backlog-1 [context-only]", reply)
        self.assertEqual({candidate.status for candidate in candidates}, {"candidate"})
        self.assertEqual(queued.pending_count, 0)
        self.assertEqual(events[-1].recommendation_kind, "llm")
        self.assertTrue(events[-1].curation_id.startswith("curation-"))

    def test_repeated_proposal_marks_previous_one_no_action(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/propose"))
            _handle_update(bot,
                _message_update(update_id=2, chat_id=42, text="/propose")
            )
            _handle_update(bot,
                _message_update(update_id=3, chat_id=42, text="/experience")
            )
            events = load_evolve_events(root)

        proposed = [event for event in events if event.event == "proposed"]
        no_action = [event for event in events if event.event == "no-action"]
        self.assertEqual(len(proposed), 2)
        self.assertNotEqual(proposed[0].proposal_id, proposed[1].proposal_id)
        self.assertEqual(no_action[0].proposal_id, proposed[0].proposal_id)
        self.assertEqual(no_action[0].reason, "superseded-by-new-proposal")
        self.assertIn(
            "Proposal disposition: no-action 1, pending 1, removed 0, selected 0, untracked 0",
            client.sent[2][1],
        )
        self.assertIn("Proposal acceptance: 0/2 (0.0%)", client.sent[2][1])

    def test_removed_candidate_closes_latest_proposal(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "remove proposed cleanup", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/propose"))
            _handle_update(bot,
                _message_update(
                    update_id=2,
                    chat_id=42,
                    text="/evolve remove backlog-1",
                )
            )
            events = load_evolve_events(root, candidate_id="backlog-1")

        self.assertEqual([event.event for event in events], ["proposed", "removed"])
        self.assertEqual(events[0].proposal_id, events[1].proposal_id)

    def test_propose_curator_suggests_bounded_candidate_when_pool_is_empty(self) -> None:
        response = json.dumps(
            {
                "recommended_candidate_id": None,
                "recommendation_reason": "",
                "scope_guidance": "",
                "risk_guidance": "",
                "test_plan_guidance": "",
                "remove_suggestions": [],
                "new_candidates": [
                {
                    "title": "Improve curation observability",
                    "rationale": "No stronger candidate exists.",
                    "proposed_change": "Expose semantic curation outcomes.",
                    "expected_benefit": "Makes proposals easier to audit.",
                    "risk": "Adds output.",
                    "test_plan": "Add proposal tests.",
                }
                ],
            }
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            set_evolve_theme("proposal observability", root)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_respond_read_only_turn", return_value=response) as respond:
                _handle_update(bot, _message_update(chat_id=42, text="/propose"))
            candidates = load_evolve_candidates(root)

        self.assertIn("New bounded candidates suggested by LLM", client.sent[0][1])
        self.assertEqual(candidates[0].source, "brainstorming")
        self.assertEqual(candidates[0].signal_actor, "agent")
        self.assertIn("[candidate brainstorming] Improve curation observability", client.sent[0][1])
        self.assertEqual(candidates[0].initiated_by, "agent")
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42:propose-fallback:curation")

    def test_evolve_can_list_and_remove_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "low value cleanup", root, priority="p2")
            add_backlog_item(42, "important Telegram recovery", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve list"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/evolve remove backlog-1"))
            _handle_update(bot, _message_update(update_id=3, chat_id=42, text="/evolve list"))
            _handle_update(bot, _message_update(update_id=4, chat_id=42, text="/evolve list all"))
            events = load_evolve_events(root)

        self.assertIn("Evolve candidates:", client.sent[0][1])
        self.assertIn("backlog-1 [candidate backlog] low value cleanup", client.sent[0][1])
        self.assertIn("Removed evolve candidate.", client.sent[1][1])
        self.assertIn("backlog-1 [removed backlog] low value cleanup", client.sent[1][1])
        self.assertNotIn("backlog-1", client.sent[2][1])
        self.assertIn("backlog-1 [removed backlog] low value cleanup", client.sent[3][1])
        self.assertEqual(events[-1].event, "removed")
        self.assertEqual(events[-1].event_actor, "human")
        self.assertEqual(events[-1].trigger, "/evolve remove")

    def test_removed_evolve_commands_no_longer_change_candidates(self) -> None:
        for index, command in enumerate(("select", "run", "reject"), start=1):
            with self.subTest(command=command), TemporaryDirectory() as temp:
                root = Path(temp)
                add_backlog_item(42, "keep this candidate", root, priority="p1")
                client = FakeTelegramClient(allowed_chat_id=42)
                bot = EnochApplication(load_identity(), root, client)

                _handle_update(bot,
                    _message_update(update_id=index, chat_id=42, text=f"/evolve {command} backlog-1")
                )
                candidates = evolve_report(root).candidates
                queued = task_queue_status(root)

                self.assertIn("Use /evolve approve", client.sent[0][1])
                self.assertEqual(candidates[0].status, "candidate")
                self.assertEqual(queued.pending_count, 0)

    def test_evolve_approve_queues_candidate_as_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship evolve approval", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            queued = task_queue_status(root)
            events = load_evolve_events(root)

        self.assertEqual(queued.pending_count, 1)
        self.assertIn("Approved evolve candidate backlog-1 and queued task #1.", client.sent[0][1])
        self.assertIn("backlog-1 [running backlog] ship evolve approval", client.sent[0][1])
        self.assertIn("Evolve candidate backlog-1", queued.pending[0].text)
        self.assertIn("open a ready-for-review PR", queued.pending[0].text)
        self.assertNotIn("Open a PR for human review", queued.pending[0].text)
        self.assertEqual(queued.pending[0].context_source, "evolve-approve")
        self.assertEqual(queued.pending[0].source, "backlog")
        self.assertEqual(queued.pending[0].initiated_by, "human")
        self.assertEqual(queued.pending[0].candidate_id, "backlog-1")
        self.assertEqual(queued.pending[0].evidence_source, "backlog")
        self.assertEqual(queued.pending[0].signal_actor, "human")
        self.assertEqual(queued.pending[0].candidate_actor, "agent")
        self.assertEqual(queued.pending[0].approval_actor, "human")
        self.assertIn("Evolve candidate context:", queued.pending[0].context)
        worker_context = telegram._task_worker_context(queued.pending[0])
        self.assertIn("## Evolution provenance", worker_context)
        self.assertIn("- Candidate: `backlog-1`", worker_context)
        self.assertIn("- Evidence source: backlog", worker_context)
        self.assertIn("- Signal actor: human", worker_context)
        self.assertIn("- Candidate actor: agent", worker_context)
        self.assertIn("- Approval actor: human", worker_context)
        self.assertIn("- Task: #1", worker_context)
        self.assertNotIn("Retry of task", worker_context)
        self.assertEqual([event.event for event in events], ["selected", "queued"])
        self.assertEqual([event.event_actor for event in events], ["human", "human"])
        self.assertEqual(events[-1].task_id, queued.pending[0].id)
        self.assertEqual(events[-1].signal_actor, "human")
        self.assertEqual(events[-1].candidate_actor, "agent")
        self.assertEqual(events[-1].approval_actor, "human")

    @patch("enoch.app.core.create_pull_request")
    def test_evolve_pr_helper_passes_current_task_provenance(
        self,
        create_pull_request: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(
                42,
                "original evolve attempt",
                root,
                source="feedback",
                candidate_id="feedback-c3ed71fd1d2d",
            )
            retry = enqueue_task(
                42,
                "retry evolve attempt",
                root,
                source="feedback",
                candidate_id="feedback-c3ed71fd1d2d",
                parent_task_id=original.id,
            )
            token = telegram._CURRENT_TASK_ID.set(retry.id)
            try:
                telegram._create_pull_request_for_current_task(root)
            finally:
                telegram._CURRENT_TASK_ID.reset(token)

        provenance = create_pull_request.call_args.kwargs["evolution_provenance"]
        self.assertEqual(provenance.candidate_id, "feedback-c3ed71fd1d2d")
        self.assertEqual(provenance.evidence_source, "feedback")
        self.assertEqual(provenance.signal_actor, "human")
        self.assertEqual(provenance.candidate_actor, "agent")
        self.assertEqual(provenance.approval_actor, "human")
        self.assertEqual(provenance.task_id, retry.id)
        self.assertEqual(provenance.retry_of_task_id, original.id)

    def test_experience_candidate_pr_context_preserves_feedback_causal_chain(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            original = enqueue_task(
                42,
                "apply human feedback",
                root,
                source="feedback",
                initiated_by="human",
                candidate_id="feedback-c3ed71fd1d2d",
                evidence_source="feedback",
                signal_actor="human",
                candidate_actor="agent",
                approval_actor="human",
            )
            begin_next_task(root)
            fail_task(original.id, root, result="Worktree branch failed.")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve task-1"))
            queued = task_queue_status(root).pending[0]
            worker_context = telegram._task_worker_context(queued)

        self.assertEqual(queued.candidate_id, "task-1")
        self.assertEqual(queued.evidence_source, "experience")
        self.assertEqual(queued.signal_actor, "system")
        self.assertEqual(queued.candidate_actor, "agent")
        self.assertEqual(queued.approval_actor, "human")
        self.assertEqual(queued.parent_candidate_id, "feedback-c3ed71fd1d2d")
        self.assertEqual(queued.source_task_id, original.id)
        self.assertIn("- Parent candidate: `feedback-c3ed71fd1d2d`", worker_context)
        self.assertIn("- Source task: #1", worker_context)

    def test_evolve_cannot_approve_removed_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "remove this candidate", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve remove backlog-1"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/evolve approve backlog-1"))
            queued = task_queue_status(root)

        self.assertIn("cannot be approved from status removed", client.sent[1][1])
        self.assertEqual(queued.pending_count, 0)

    def test_queued_evolve_task_cancellation_is_journaled(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "cancel queued evolution", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/task cancel 1"))
            events = load_evolve_events(root, candidate_id="backlog-1")
            candidate = load_evolve_candidates(root, include_inactive=True)[0]

        self.assertEqual([event.event for event in events], ["selected", "queued", "cancelled"])
        self.assertEqual(events[-1].event_actor, "human")
        self.assertEqual(events[-1].trigger, "/task cancel")
        self.assertEqual(candidate.status, "cancelled")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_evolve_approved_candidate_is_marked_done_after_task_completion(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship evolve completion", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            job = begin_next_task(root)
            assert job is not None
            with patch.object(bot, "_run_direct_work", return_value="Done with evolve work."):
                bot._run_task_job(job)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        self.assertNotIn("backlog-1", {candidate.id for candidate in visible})
        self.assertEqual(all_candidates[0].id, "backlog-1")
        self.assertEqual(all_candidates[0].status, "done")
        self.assertIn("Final status: completed", client.sent[-1][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_selected_proposal_tracks_task_completion_and_funnel(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship tracked proposal", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/propose"))
            _handle_update(bot,
                _message_update(
                    update_id=2,
                    chat_id=42,
                    text="/evolve approve backlog-1",
                )
            )
            job = begin_next_task(root)
            assert job is not None
            with patch.object(bot, "_run_direct_work", return_value="Done with evolve work."):
                bot._run_task_job(job)
            _handle_update(bot,
                _message_update(update_id=3, chat_id=42, text="/experience")
            )
            events = load_evolve_events(root, candidate_id="backlog-1")

        proposal_id = next(
            event.proposal_id for event in events if event.event == "proposed"
        )
        tracked = [
            event.event
            for event in events
            if event.proposal_id == proposal_id
        ]
        self.assertEqual(
            tracked,
            ["proposed", "selected", "queued", "completed"],
        )
        self.assertIn("Proposal acceptance: 1/1 (100.0%)", client.sent[-1][1])
        self.assertIn("Selected proposal outcomes:", client.sent[-1][1])
        self.assertIn("completed 1", client.sent[-1][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_evolve_approved_candidate_is_marked_failed_after_task_failure(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship failing evolve", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            job = begin_next_task(root)
            assert job is not None
            with patch.object(bot, "_run_direct_work", return_value="Enoch could not publish this edit as a pull request: GH007"):
                bot._run_task_job(job)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)
            report = evolve_report(root)

        self.assertIn("backlog-1", {candidate.id for candidate in visible})
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses["backlog-1"], "failed")
        self.assertIn("experience", report.counts_by_source)
        self.assertIn("Final status: failed", client.sent[-1][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_propose_suggests_retry_and_retry_links_new_task_to_failure(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship retryable evolve work", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            failed_job = begin_next_task(root)
            assert failed_job is not None
            with patch.object(
                bot,
                "_run_direct_work",
                return_value="Enoch could not complete the requested work yet: transient failure",
            ):
                bot._run_task_job(failed_job)

            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/propose"))
            proposal_reply = client.sent[-1][1]
            _handle_update(bot,
                _message_update(
                    update_id=3,
                    chat_id=42,
                    text="/evolve retry backlog-1",
                )
            )
            queued = task_queue_status(root)
            candidates = load_evolve_candidates(root)
            task_events = load_task_events(root, task_id=2)
            evolve_events = load_evolve_events(root, candidate_id="backlog-1")

        self.assertIn("backlog-1 [failed backlog]", proposal_reply)
        self.assertIn("Retry with /evolve retry backlog-1.", proposal_reply)
        self.assertNotIn("Approve with /evolve approve backlog-1.", proposal_reply)
        self.assertEqual(queued.pending_count, 1)
        self.assertEqual(queued.pending[0].id, 2)
        self.assertEqual(queued.pending[0].candidate_id, "backlog-1")
        self.assertEqual(queued.pending[0].parent_task_id, failed_job.id)
        self.assertEqual(queued.pending[0].context_source, "evolve-retry")
        worker_context = telegram._task_worker_context(queued.pending[0])
        self.assertIn("- Candidate: `backlog-1`", worker_context)
        self.assertIn("- Evidence source: backlog", worker_context)
        self.assertIn("- Signal actor: human", worker_context)
        self.assertIn("- Candidate actor: agent", worker_context)
        self.assertIn("- Approval actor: human", worker_context)
        self.assertIn("- Task: #2", worker_context)
        self.assertIn("- Retry of task: #1", worker_context)
        self.assertEqual(candidates[0].status, "running")
        self.assertEqual([event.parent_task_id for event in task_events], [1, 1])
        self.assertEqual([event.trigger for event in task_events], ["/evolve retry", "/evolve retry"])
        self.assertEqual(
            [event.event for event in evolve_events[-3:]],
            ["proposed", "selected", "queued"],
        )
        self.assertEqual(evolve_events[-1].task_id, 2)
        self.assertEqual(evolve_events[-1].retry_of_task_id, 1)
        self.assertEqual(evolve_events[-1].approval_actor, "human")
        self.assertEqual(evolve_events[-1].reason, "retry-of-task-1")
        self.assertIn(
            "Retrying evolve candidate backlog-1 as task #2, linked to failed task #1.",
            client.sent[-1][1],
        )

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_evolve_task_regression_and_resolution_are_journaled(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship regressing evolve work", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot,
                _message_update(chat_id=42, text="/evolve approve backlog-1")
            )
            job = begin_next_task(root)
            assert job is not None
            with patch.object(bot, "_run_direct_work", return_value="Done with evolve work."):
                bot._run_task_job(job)
            regression_reply = (
                "The evolve change regressed behavior, so I rolled it back.\n"
                f"{TASK_REGRESSION_START}\n"
                '{"task_id": 1, "reason": "Introduced a regression.", '
                '"resolution": "reverted"}\n'
                f"{TASK_REGRESSION_END}"
            )
            with patch("enoch.app.core.respond", return_value=regression_reply):
                _handle_update(bot,
                    _message_update(
                        update_id=2,
                        chat_id=42,
                        text="that evolve change regressed behavior; revert it",
                    )
                )
            events = load_evolve_events(root, candidate_id="backlog-1")
            candidate = next(
                item
                for item in load_evolve_candidates(root, include_inactive=True)
                if item.id == "backlog-1"
            )

        self.assertEqual(
            [event.event for event in events[-3:]],
            ["completed", "regressed", "reverted"],
        )
        self.assertEqual(candidate.status, "reverted")

    def test_evolve_can_set_theme_and_mode(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve theme improve recovery"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/evolve mode disabled"))

        self.assertIn("Theme: improve recovery", client.sent[0][1])
        self.assertIn("Mode: disabled", client.sent[1][1])
        self.assertIn("Candidate counts:\n- none", client.sent[1][1])

    def test_evolve_theme_shows_current_theme(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve theme"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/evolve theme improve recovery"))
            _handle_update(bot, _message_update(update_id=3, chat_id=42, text="/evolve theme"))

        self.assertIn("Evolve theme:\nnot set", client.sent[0][1])
        self.assertIn("Evolve theme:\nimprove recovery", client.sent[2][1])
        self.assertIn("Set with /evolve theme <text>.", client.sent[2][1])

    @patch(
        "enoch.app.core.respond",
        return_value=json.dumps(
            [
                {
                    "title": "Expose candidate provenance",
                    "rationale": "The theme calls for auditability.",
                    "proposed_change": "Add provenance to evolve reports.",
                    "expected_benefit": "Improves review.",
                    "risk": "Adds output.",
                    "test_plan": "Add report tests.",
                }
            ]
        ),
    )
    def test_evolve_brainstorm_generates_candidate_under_theme(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve theme auditable evolution"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/evolve brainstorm"))

        self.assertIn("Added 1 theme-guided brainstorming candidate", client.sent[1][1])
        self.assertIn("brainstorming: 1", client.sent[1][1])
        self.assertIn("Current evolution theme: auditable evolution", respond.call_args.args[1])

    @patch("enoch.app.core.respond")
    def test_evolve_brainstorm_requires_theme(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve brainstorm"))

        self.assertIn("Set a theme", client.sent[0][1])
        respond.assert_not_called()

    def test_evolve_explore_is_not_a_command(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve explore enosh"))

        self.assertNotIn("peer-learning candidate", client.sent[0][1])
        self.assertNotIn("/evolve explore", client.sent[0][1])
        self.assertIn("Use /evolve list", client.sent[0][1])

    def test_evolve_candidates_is_not_a_command(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve candidates"))

        self.assertIn("Use /evolve list", client.sent[0][1])
        self.assertNotIn("Evolve candidates:", client.sent[0][1])

    def test_evolve_keeps_direct_mode_aliases(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve auto-evovle"))

        self.assertIn("Mode: auto-evolve", client.sent[0][1])

    def test_evolve_can_set_and_disable_schedule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule every 1d"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/evolve schedule off"))

        self.assertIn("Schedule: every 1d; next", client.sent[0][1])
        self.assertIn("Schedule: off", client.sent[1][1])

    def test_evolve_once_a_day_alias_sets_daily_interval(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule once a day"))

        self.assertIn("Schedule: every 1d; next", client.sent[0][1])

    def test_evolve_once_a_day_alias_accepts_time(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule once a day at 09:30"))

        self.assertIn("Schedule: daily 09:30; next", client.sent[0][1])

    def test_evolve_schedule_interprets_quoted_text(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text='/evolve schedule "once a day"'))

        self.assertIn("Schedule: every 1d; next", client.sent[0][1])

    def test_evolve_schedule_interprets_raw_cron_text(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule 30 9 * * *"))

        self.assertIn("Schedule: cron 30 9 * * *; next", client.sent[0][1])

    def test_evolve_schedule_interprets_natural_daily_time(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule every day at 09:30"))

        self.assertIn("Schedule: daily 09:30; next", client.sent[0][1])

    def test_evolve_can_set_daily_schedule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule daily 09:30"))

        self.assertIn("Schedule: daily 09:30; next", client.sent[0][1])

    def test_evolve_can_set_cron_schedule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/evolve schedule cron 30 9 * * *"))

        self.assertIn("Schedule: cron 30 9 * * *; next", client.sent[0][1])

    def test_due_evolve_schedule_reports_in_co_evolve_mode(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            set_evolve_schedule(
                60,
                root,
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            job = bot._run_due_evolve_schedule()
            events = load_evolve_events(root)

        self.assertIsNone(job)
        self.assertIn("Scheduled evolve check", client.sent[0][1])
        self.assertIn("Enoch proposes:", client.sent[0][1])
        self.assertIn("Ranked 1 actionable candidate(s) from the six evolve sources.", client.sent[0][1])
        self.assertIn("backlog-1 [candidate backlog] improve Telegram work UX", client.sent[0][1])
        self.assertEqual([event.event for event in events], ["checked", "proposed", "skipped"])
        self.assertEqual(events[-1].reason, "awaiting-human-approval")
        self.assertEqual(events[-1].proposal_id, events[-2].proposal_id)

    def test_due_evolve_schedule_auto_mode_still_requires_human_approval(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            set_evolve_mode(MODE_AUTO_EVOLVE, root)
            set_evolve_schedule(
                60,
                root,
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            job = bot._run_due_evolve_schedule()
            queued = task_queue_status(root)
            report = evolve_report(root)
            events = load_evolve_events(root)

        self.assertIsNone(job)
        self.assertEqual(queued.pending_count, 0)
        self.assertEqual(report.top_candidate.status, "candidate")
        self.assertIn("Scheduled evolve check", client.sent[0][1])
        self.assertIn("without human action", client.sent[0][1].lower())
        self.assertEqual(
            [event.event for event in events],
            ["checked", "proposed", "skipped"],
        )
        self.assertTrue(all(event.event_actor == "system" for event in events))
        self.assertEqual(events[-1].trigger, "evolve-scheduler")
        self.assertEqual(events[-1].reason, "awaiting-human-approval")
        self.assertEqual(events[-1].task_id, None)
        proposal_ids = {
            event.proposal_id
            for event in events
            if event.event in {"proposed", "skipped"}
        }
        self.assertEqual(len(proposal_ids), 1)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_due_auto_evolve_proposes_failed_candidate_without_automatic_retry(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "retry only with human approval", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            failed_job = begin_next_task(root)
            assert failed_job is not None
            with patch.object(
                bot,
                "_run_direct_work",
                return_value="Enoch could not complete the requested work yet: transient failure",
            ):
                bot._run_task_job(failed_job)
            set_evolve_mode(MODE_AUTO_EVOLVE, root)
            set_evolve_schedule(
                60,
                root,
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )
            client.sent.clear()

            retry_job = bot._run_due_evolve_schedule()
            queued = task_queue_status(root)
            events = load_evolve_events(root, candidate_id="backlog-1")

        self.assertIsNone(retry_job)
        self.assertEqual(queued.pending_count, 0)
        self.assertIn("Retry with /evolve retry backlog-1.", client.sent[0][1])
        self.assertEqual(events[-1].event, "skipped")
        self.assertEqual(events[-1].reason, "retry-requires-human")

    def test_due_auto_evolve_schedule_does_not_requeue_running_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            set_evolve_mode(MODE_AUTO_EVOLVE, root)
            set_evolve_schedule(60, root, now=datetime(2020, 1, 1, tzinfo=timezone.utc))
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            first = bot._run_due_evolve_schedule()
            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            set_evolve_schedule(60, root, now=datetime(2020, 1, 1, tzinfo=timezone.utc))
            second = bot._run_due_evolve_schedule()
            queued = task_queue_status(root)

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(queued.pending_count, 1)

    def test_due_auto_evolve_schedule_suggests_new_candidate_without_queueing(self) -> None:
        response = json.dumps(
            {
                "recommended_candidate_id": None,
                "recommendation_reason": "",
                "scope_guidance": "",
                "risk_guidance": "",
                "test_plan_guidance": "",
                "remove_suggestions": [],
                "new_candidates": [
                {
                    "title": "Improve scheduled curation",
                    "rationale": "The scheduled proposal had no candidate.",
                    "proposed_change": "Add bounded scheduled curation coverage.",
                    "expected_benefit": "Keeps scheduled evolution useful.",
                    "risk": "The idea is speculative.",
                    "test_plan": "Add scheduler tests.",
                }
                ],
            }
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            set_evolve_theme("scheduled reliability", root)
            set_evolve_mode(MODE_AUTO_EVOLVE, root)
            set_evolve_schedule(60, root, now=datetime(2020, 1, 1, tzinfo=timezone.utc))
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_respond_read_only_turn", return_value=response) as respond:
                job = bot._run_due_evolve_schedule()
            queued = task_queue_status(root)
            candidate = load_evolve_candidates(root)[0]
            evolve_events = load_evolve_events(root)

        self.assertIsNone(job)
        self.assertEqual(queued.pending_count, 0)
        self.assertEqual(candidate.source, "brainstorming")
        self.assertEqual(candidate.initiated_by, "agent")
        self.assertEqual(candidate.signal_actor, "agent")
        self.assertEqual([event.event for event in evolve_events], ["checked", "skipped"])
        self.assertIn("New bounded candidates suggested by LLM", client.sent[0][1])
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42:evolve-scheduler:curation")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_cron_command_schedules_recurring_work_with_context(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            context="Use the scheduled context snapshot.",
            source="chat-snapshot",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/cron every 10m run scheduled cleanup"))
            status = cron_status(root)

        self.resolve_task_context_snapshot.assert_called_once_with(42, "run scheduled cleanup")
        self.assertEqual(status.active_count, 1)
        self.assertEqual(status.active[0].text, "run scheduled cleanup")
        self.assertEqual(status.active[0].interval_seconds, 600)
        self.assertEqual(status.active[0].context, "Use the scheduled context snapshot.")
        self.assertIn("Cron #1 scheduled every 10m.", client.sent[0][1])
        self.assertIn("Next run:", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_cron_command_can_cancel_and_report(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/cron every 1h scheduled cleanup"))
            _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/cron"))
            _handle_update(bot, _message_update(update_id=3, chat_id=42, text="/cron cancel 1"))
            status = cron_status(root)

        self.assertIn("Cron:", client.sent[1][1])
        self.assertIn("#1 [active] every 1h", client.sent[1][1])
        self.assertIn("Cancelled cron #1.", client.sent[2][1])
        self.assertEqual(status.active, ())
        self.assertEqual(status.history[-1].status, "cancelled")

    def test_due_cron_job_enters_task_queue_with_status_message(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            cron = add_cron_job(
                42,
                "scheduled cleanup",
                60,
                root,
                context="Use saved cron context.",
                context_source="chat-snapshot",
                now=datetime(2020, 1, 1, tzinfo=timezone.utc),
            )

            jobs = bot._enqueue_due_cron_jobs()
            queued = task_queue_status(root)
            cron_after = cron_status(root)
            jobs_again = bot._enqueue_due_cron_jobs()
            events = load_task_events(root, task_id=queued.pending[0].id)

        self.assertEqual([job.id for job in jobs], [1])
        self.assertEqual(jobs_again, ())
        self.assertEqual(queued.pending[0].text, "scheduled cleanup")
        self.assertEqual(queued.pending[0].context, "Use saved cron context.")
        self.assertEqual(queued.pending[0].context_source, "cron:chat-snapshot")
        self.assertEqual(queued.pending[0].source, "task")
        self.assertEqual(queued.pending[0].initiated_by, "human")
        self.assertEqual(events[0].event_actor, "system")
        self.assertEqual(events[0].trigger, "cron:1")
        self.assertEqual(cron_after.active[0].id, cron.id)
        self.assertEqual(cron_after.active[0].last_task_id, queued.pending[0].id)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Latest update: Scheduled by cron #1.", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_worker_marks_publish_failure_failed(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None

            with patch.object(
                bot,
                "_run_direct_work",
                return_value="Enoch could not publish this edit as a pull request: GH007",
            ):
                bot._run_task_job(job)
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].status, "failed")
        self.assertIn("GH007", status.history[-1].result)
        self.assertIn("Status: failed", client.edited[-1][2])
        self.assertIn("Latest update: Failed. Final summary sent below.", client.edited[-1][2])
        self.assertNotIn("GH007", client.edited[-1][2])
        self.assertIn("Task #1 final update", client.sent[-1][1])
        self.assertIn("Final status: failed", client.sent[-1][1])
        self.assertIn("PR URL:\n- none", client.sent[-1][1])
        self.assertIn("GH007", client.sent[-1][1])
        log_conversation_turn.assert_called()

    def test_dirty_worktree_failure_is_not_automatically_retried(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            queued = enqueue_task(42, "edit from a dirty worktree", root)
            job = begin_next_task(root)

            with patch.object(
                bot,
                "_run_direct_work",
                return_value=(
                    "Enoch could not complete the requested work yet: "
                    "Worktree is not clean. Commit, stash, or discard changes before evolving."
                ),
            ):
                bot._run_task_job(job)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=queued.id)

        failed = status.history[-1]
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.attempt, 1)
        self.assertEqual(failed.failure_code, "dirty_worktree")
        self.assertEqual(failed.failure_class, "permanent")
        self.assertFalse(failed.retryable)
        self.assertNotIn("retrying", [event.event for event in events])
        self.assertIn("Failure: dirty_worktree", client.sent[-1][1])
        self.assertIn("Attempts: 1/3", client.sent[-1][1])

    def test_transient_task_failure_retries_then_completes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            queued = enqueue_task(42, "survive a network interruption", root)
            first = begin_next_task(root)

            with patch(
                "enoch.app.core.automatic_retry_delay_seconds",
                return_value=0,
            ):
                with patch.object(
                    bot,
                    "_run_direct_work",
                    side_effect=[
                        "Enoch could not continue: connection reset by peer.",
                        "Completed after reconnecting.",
                    ],
                ):
                    bot._run_task_job(first)
                    retry_status = task_queue_status(root)
                    second = begin_next_task(root)
                    bot._run_task_job(second)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=queued.id)

        self.assertEqual(retry_status.pending[0].attempt, 1)
        self.assertEqual(retry_status.pending[0].failure_code, "network_error")
        self.assertEqual(second.attempt, 2)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].attempt, 2)
        self.assertEqual(
            [event.event for event in events],
            [
                "created",
                "queued",
                "started",
                "retrying",
                "queued",
                "started",
                "completed",
            ],
        )
        self.assertTrue(
            any("Status: retrying" in message for _, _, message in client.edited)
        )
        self.assertEqual(
            sum("Final status:" in message for _, message in client.sent),
            1,
        )

    def test_task_worker_timeout_is_logged_as_system_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task long queued work"))
            job = begin_next_task(root)
            assert job is not None

            with patch("enoch.app.core.threading.Timer", _ImmediateTimer):
                with patch.object(bot, "_run_direct_work", return_value="Done."):
                    bot._run_task_job(job)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=job.id)

        self.assertEqual(status.history[-1].status, "failed")
        self.assertIn("configured 10m timeout", status.history[-1].result)
        self.assertEqual(events[-1].event, "failed")
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].trigger, "task-timeout")

    def test_runtime_timeout_is_logged_as_system_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task timed runtime"))
            job = begin_next_task(root)
            assert job is not None

            with patch.object(
                bot,
                "_run_direct_work",
                side_effect=AgentRuntimeTimedOut("Provider deadline expired."),
            ):
                bot._run_task_job(job)
            status = task_queue_status(root)
            events = load_task_events(root, task_id=job.id)

        self.assertEqual(status.history[-1].status, "failed")
        self.assertIn("configured 10m timeout", status.history[-1].result)
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].trigger, "task-timeout")

    def test_evolve_task_timeout_is_journaled_as_system_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "time out evolution safely", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            job = begin_next_task(root)
            assert job is not None

            with patch("enoch.app.core.threading.Timer", _ImmediateTimer):
                with patch.object(bot, "_run_direct_work", return_value="Done."):
                    bot._run_task_job(job)
            events = load_evolve_events(root, task_id=job.id)

        self.assertEqual([event.event for event in events], ["queued", "failed"])
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].trigger, "task-timeout")
        self.assertIn("configured 10m timeout", events[-1].reason)

    def test_evolve_task_pause_and_resume_keep_candidate_running(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "pause evolution safely", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/evolve approve backlog-1"))
            job = begin_next_task(root)
            assert job is not None

            with patch.object(
                bot,
                "_run_direct_work",
                side_effect=CodexAccessUnavailable("Codex authentication is unavailable."),
            ):
                bot._run_task_job(job)
            with patch.object(bot, "_maybe_start_task_worker"):
                _handle_update(
                    bot,
                    _message_update(
                        update_id=2,
                        chat_id=42,
                        text=f"/task resume {job.id}",
                    ),
                )
            events = load_evolve_events(root, task_id=job.id)
            candidate = next(
                item
                for item in load_evolve_candidates(root, include_inactive=True)
                if item.id == "backlog-1"
            )

        self.assertEqual(
            [event.event for event in events],
            ["queued", "paused", "resumed"],
        )
        self.assertEqual(events[-2].event_actor, "system")
        self.assertEqual(events[-1].event_actor, "human")
        self.assertEqual(candidate.status, "running")

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_worker_does_not_rerun_task_with_recorded_pr(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None
            result = "Opened pull request: https://github.com/our-ark/enoch/pull/3"
            record_task_result(job.id, result, root)
            job = task_queue_status(root).running
            assert job is not None

            with patch.object(bot, "_run_direct_work") as run_direct_work:
                bot._run_task_job(job)
            status = task_queue_status(root)

        run_direct_work.assert_not_called()
        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertIn("https://github.com/our-ark/enoch/pull/3", status.history[-1].result)
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("https://github.com/our-ark/enoch/pull/3", client.edited[-1][2])
        self.assertIn("Latest update: Completed. Final summary sent below.", client.edited[-1][2])
        self.assertIn("Task #1 final update", client.sent[-1][1])
        self.assertIn("Final status: completed", client.sent[-1][1])
        self.assertIn("PR URL:\n- https://github.com/our-ark/enoch/pull/3", client.sent[-1][1])
        self.assertIn("Result summary:\nOpened pull request: https://github.com/our-ark/enoch/pull/3", client.sent[-1][1])
        log_conversation_turn.assert_called()

    def test_task_final_message_preserves_multiline_publish_summary(self) -> None:
        job = TaskJob(
            id=4,
            chat_id=42,
            text="add test4",
            created_at="now",
            status="completed",
            result="\n\n".join(
                [
                    "\n".join(
                        [
                            "Enoch committed this change.",
                            "Branch: enoch/add-test4",
                            "Files:",
                            "- README.md",
                            "Publish step: local commit created.",
                        ]
                    ),
                    "\n".join(
                        [
                            "Enoch opened a pull request.",
                            "PR URL: https://github.com/our-ark/enoch/pull/21",
                            "Branch: enoch/add-test4",
                        ]
                    ),
                ]
            ),
            pr_urls=("https://github.com/our-ark/enoch/pull/21",),
        )

        message = _format_task_final_message(job, "completed", "")

        self.assertIn("PR URL:\n- https://github.com/our-ark/enoch/pull/21", message)
        self.assertIn("Enoch committed this change.\nBranch: enoch/add-test4", message)
        self.assertIn("Files:\n- README.md", message)
        self.assertIn("Enoch opened a pull request.\nPR URL: https://github.com/our-ark/enoch/pull/21", message)
        self.assertNotIn("Enoch committed this change. Branch:", message)

    @patch("enoch.app.core.ensure_long_term_memory")
    def test_startup_completes_running_task_from_matching_direct_action_log(
        self,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "resolve conflicts in PR 10", root)
            running = begin_next_task(root)
            assert running is not None
            result = "PR 10 is clean now. Validation passed."
            log_system_event(
                "direct_action",
                root=root,
                details={"request": queued.text, "result": result},
            )

            EnochApplication(load_identity(), root, FakeTelegramClient(allowed_chat_id=42))
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].result, result)

    @patch("enoch.app.core.ensure_long_term_memory")
    def test_startup_fails_running_task_from_matching_failure_log(
        self,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship it", root)
            running = begin_next_task(root)
            assert running is not None
            result = "Enoch could not complete the requested work yet: usage limit"
            log_system_event(
                "direct_action",
                root=root,
                details={"request": queued.text, "result": result},
            )

            EnochApplication(load_identity(), root, FakeTelegramClient(allowed_chat_id=42))
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "failed")
        self.assertEqual(status.history[-1].result, result)

    @patch("enoch.app.core.create_pull_request")
    @patch("enoch.app.core.push_current_branch")
    @patch("enoch.app.core.switch_branch")
    @patch("enoch.app.core.ensure_clean_worktree")
    @patch("enoch.app.core.current_branch", return_value="agent/enoch-gary")
    @patch("enoch.app.core.act_in_session")
    @patch("enoch.app.core.respond")
    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_task_publish_existing_branch_runs_as_job_action(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
        respond: MagicMock,
        act_in_session: MagicMock,
        _current_branch: MagicMock,
        _ensure_clean_worktree: MagicMock,
        _switch_branch: MagicMock,
        push_current_branch: MagicMock,
        create_pull_request: MagicMock,
    ) -> None:
        push_current_branch.return_value = RemotePublishResult(
            branch="enoch/existing",
            remote="origin",
            pushed=True,
            ahead_count=1,
            compare_url="https://github.com/our-ark/enoch/compare/main...enoch/existing?expand=1",
        )
        create_pull_request.return_value = PullRequestResult(
            branch="enoch/existing",
            title="Existing work",
            body="body",
            created=True,
            url="https://github.com/our-ark/enoch/pull/3",
            fallback_url="https://github.com/our-ark/enoch/compare/main...enoch/existing?expand=1",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            metadata = root / ".agent" / "instance.yaml"
            metadata.parent.mkdir()
            metadata.write_text(
                'worktree:\n  branch: "agent/enoch-gary"\n',
                encoding="utf-8",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot,
                _message_update(
                    chat_id=42,
                    text="/task publish existing local branch `enoch/existing` as a PR against `main`",
                )
            )
            job = begin_next_task(root)
            assert job is not None

            with patch(
                "enoch.app.core.prepare_existing_branch_worktree",
                return_value=TaskWorktree(job.id, root, "enoch/existing", True),
            ):
                with patch(
                    "enoch.app.core.remove_task_worktree",
                    return_value="Removed task worktree.",
                ):
                    bot._run_task_job(job)

        respond.assert_not_called()
        act_in_session.assert_not_called()
        push_current_branch.assert_called_once_with(root=root)
        create_pull_request.assert_called_once_with(root=root)
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("https://github.com/our-ark/enoch/pull/3", client.edited[-1][2])
        log_conversation_turn.assert_called()

    @patch("enoch.app.core.skills_command", return_value="Lucy skills:")
    def test_skills_command_shows_declared_skills(self, skills_command: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/skills lucy"))

        skills_command.assert_called_once_with("/skills lucy", ROOT)
        self.assertIn("Lucy skills:", client.sent[0][1])
        self.assertNotIn("Input tokens:", client.sent[0][1])

    def test_mission_command_shows_and_updates_identity_mission(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            identity_file = root / "src" / "enoch" / "identity.yaml"
            identity_file.parent.mkdir(parents=True)
            identity_file.write_text((ROOT / "src" / "enoch" / "identity.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/mission"))
            _handle_update(bot, _message_update(chat_id=42, text="/mission Build calm agent networks"))
            _handle_update(bot, _message_update(chat_id=42, text="/self"))
            identity_text = identity_file.read_text(encoding="utf-8")

            self.assertIn("Enoch mission:", client.sent[0][1])
            self.assertIn("Update with /mission <new mission>.", client.sent[0][1])
            self.assertIn("Enoch mission updated.", client.sent[1][1])
            self.assertIn("Mission: Build calm agent networks", client.sent[1][1])
            self.assertIn("Mission: Build calm agent networks", client.sent[2][1])
            self.assertIn("Build calm agent networks", identity_text)

    @patch("enoch.app.core.respond", return_value="Memory is managed internally now.")
    def test_memory_command_is_not_user_facing(self, respond: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/memory"))

        respond.assert_called_once()
        self.assertIn("Memory is managed internally now.", client.sent[0][1])

    @patch("enoch.app.core.respond", return_value="Teaching is automatic now.")
    def test_teach_command_is_not_user_facing(self, respond: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/teach natural agency"))

        respond.assert_called_once()
        self.assertIn("Teaching is automatic now.", client.sent[0][1])
        self.assertNotIn("Input tokens:", client.sent[0][1])

    @patch("enoch.app.core.record_peer_learning_observation")
    @patch("enoch.app.core.learn_skill_prompt", return_value="learn prompt")
    @patch("enoch.app.core.respond", return_value="This skill does not fit Enoch yet.")
    def test_learn_skill_uses_read_only_session(
        self,
        respond: MagicMock,
        learn_skill_prompt: MagicMock,
        record_peer_learning_observation: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/learn teach from lucy"))

        learn_skill_prompt.assert_called_once_with("/learn teach from lucy", root=ROOT)
        record_peer_learning_observation.assert_called_once()
        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("learn prompt", respond.call_args.args[1])
        self.assertIn("Enoch wrapper instructions:", respond.call_args.args[1])
        self.assertIn("This skill does not fit Enoch yet.", client.sent[0][1])

    def test_human_triggered_learning_work_is_tracked_with_learning_source(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_run_direct_work", return_value="Adapted the skill."):
                reply = bot._run_tracked_inline_work(
                    42,
                    "adapt the research skill",
                    source="learning",
                    initiated_by="human",
                    trigger="/learn",
                    session_key="telegram:42",
                )
            history = task_queue_status(root).history
            events = load_task_events(root, task_id=1)

        self.assertEqual(reply, "Adapted the skill.")
        self.assertEqual(history[-1].source, "learning")
        self.assertEqual(history[-1].initiated_by, "human")
        self.assertEqual([event.event for event in events], ["created", "started", "completed"])
        self.assertEqual(events[0].trigger, "/learn")

    def test_tracked_inline_timeout_is_logged_as_system_failure(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch("enoch.app.core.threading.Timer", _ImmediateTimer):
                with patch.object(bot, "_run_direct_work", return_value="Done."):
                    reply = bot._run_tracked_inline_work(
                        42,
                        "adapt the research skill",
                        source="learning",
                        initiated_by="human",
                        trigger="/learn",
                        session_key="telegram:42",
                    )
            history = task_queue_status(root).history
            events = load_task_events(root, task_id=1)

        self.assertIn("configured 10m timeout", reply)
        self.assertEqual(history[-1].status, "failed")
        self.assertEqual(events[-1].event_actor, "system")
        self.assertEqual(events[-1].trigger, "task-timeout")

    @patch("enoch.app.core.model_summary", return_value="AI model: gpt-5-codex")
    def test_self_reports_identity_without_runtime_status(self, model_summary: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/self"))

        model_summary.assert_not_called()
        self.assertIn("I am Enoch.", client.sent[0][1])
        self.assertIn("Role: descendant_agent", client.sent[0][1])
        self.assertIn("Generation: 3", client.sent[0][1])
        self.assertIn("Ancestor: Seth", client.sent[0][1])
        self.assertIn("Mission:", client.sent[0][1])
        self.assertNotIn("Enoch status:", client.sent[0][1])
        self.assertNotIn("Local state:", client.sent[0][1])
        self.assertNotIn("AI model:", client.sent[0][1])

    @patch("enoch.app.core.model_summary", return_value="AI model: gpt-5-codex")
    def test_status_reports_runtime_state_and_chat_lock(self, model_summary: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/status"))

        model_summary.assert_called_once_with(ROOT)
        self.assertIn("Enoch status:", client.sent[0][1])
        self.assertIn("AI model: gpt-5-codex", client.sent[0][1])
        self.assertIn("Local state:", client.sent[0][1])
        self.assertIn("Telegram conversation lock: 42", client.sent[0][1])
        self.assertNotIn("action mode", client.sent[0][1])
        self.assertNotIn("I am Enoch.", client.sent[0][1])
        self.assertNotIn("Ancestor:", client.sent[0][1])
        self.assertNotIn("Mission:", client.sent[0][1])
        self.assertNotIn("current evolution", client.sent[0][1])

    @patch("enoch.app.core.model_summary", return_value="AI model: gpt-5-codex")
    def test_self_uses_lineage_parent_before_identity_ancestor(self, model_summary: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            lineage = root / ".agent" / "lineage.yaml"
            lineage.parent.mkdir()
            lineage.write_text(
                "\n".join(["parent:", "  name: Adam", "  repo: our-ark/adam"]),
                encoding="utf-8",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/self"))

        self.assertIn("Ancestor: Adam", client.sent[0][1])
        self.assertNotIn("Ancestor: Lucy", client.sent[0][1])
        model_summary.assert_not_called()

    def test_status_includes_setup_hint_without_chat_lock(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/status"))

        self.assertIn("Telegram conversation lock: not set", client.sent[0][1])
        self.assertIn("Configure the Telegram provider", client.sent[0][1])
        self.assertIn("restart Enoch", client.sent[0][1])

    @patch("enoch.app.core.respond", return_value="Thinking config is managed locally.")
    def test_thinking_is_no_longer_a_telegram_command(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/thinking high"))

        respond.assert_called_once()
        self.assertNotIn("reasoning_effort", read_section("codex", root))
        self.assertIn("Thinking config is managed locally.", client.sent[0][1])

    def test_ancestors_reports_missing_parent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/ancestors"))

        self.assertIn("no direct parent configured", client.sent[0][1])
        self.assertIn(".agent/lineage.yaml", client.sent[0][1])
        self.assertIn("Ancestor commands:", client.sent[0][1])
        self.assertIn("/inherit", client.sent[0][1])

    @patch("enoch.app.core.resolve_lineage")
    def test_ancestors_reports_resolution_warnings(self, resolve_lineage: MagicMock) -> None:
        resolve_lineage.return_value = LineageResolution(
            ancestors=(AncestorLink(name="Enoch", repo="our-ark/enoch", branch="main", depth=1),),
            warnings=("Could not read parent lineage from our-ark/enoch@main: private repo",),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/ancestors"))

        resolve_lineage.assert_called_once_with(root)
        self.assertIn("Warnings:", client.sent[0][1])
        self.assertIn("private repo", client.sent[0][1])
        self.assertIn("Ancestor commands:", client.sent[0][1])

    @patch("enoch.app.core.inherit_command", return_value="Direct parent inheritance checked.\nour-ark/enoch#32")
    def test_inherit_uses_shared_command(self, inherit_command: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/inherit"))

        inherit_command.assert_called_once()
        self.assertEqual(inherit_command.call_args.args[:2], ("/inherit", root))
        self.assertEqual(inherit_command.call_args.kwargs["command_name"], "inherit")
        self.assertIn("Direct parent inheritance checked", client.sent[0][1])
        self.assertIn("our-ark/enoch#32", client.sent[0][1])

    def test_inherit_inspect_syncs_candidate_context_to_codex_session(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            lineage = root / ".agent" / "lineage.yaml"
            lineage.parent.mkdir()
            lineage.write_text("parent:\n  name: Enoch\n  repo: our-ark/enoch\n", encoding="utf-8")
            inbox = root / ".agent" / "lineage_inbox.json"
            inbox.write_text(
                json.dumps({"schema_version": 1, "candidates": [_lineage_candidate().__dict__]}),
                encoding="utf-8",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/inherit inspect our-ark/enoch#32"))

        self.assertIn("Status: pending", client.sent[0][1])
        self.assertIn("src/enoch/telegram/bot.py", client.sent[0][1])
        self.sync_session_activity.assert_called_once()
        self.assertIn("Ancestor change context", self.sync_session_activity.call_args.args[3])

    def test_inherit_candidate_id_uses_lineage_adoption_flow(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        with patch.object(bot, "_adopt_lineage_candidate", return_value="adopted change") as adopt:
            _handle_update(bot, _message_update(chat_id=42, text="/inherit our-ark/enoch#32"))

        adopt.assert_called_once_with(42, "our-ark/enoch#32")
        self.assertEqual(client.sent[0][1], "adopted change")

    def test_inherit_rejects_non_inheritable_parent_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            lineage = root / ".agent" / "lineage.yaml"
            lineage.parent.mkdir()
            lineage.write_text("parent:\n  name: Enoch\n  repo: our-ark/enoch\n", encoding="utf-8")
            candidate = _lineage_candidate()
            candidate = candidate.__class__(**{**candidate.__dict__, "relevance": "low"})
            inbox = root / ".agent" / "lineage_inbox.json"
            inbox.write_text(json.dumps({"schema_version": 1, "candidates": [candidate.__dict__]}), encoding="utf-8")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/inherit our-ark/enoch#32"))

        self.assertIn("could not find direct-parent change", client.sent[0][1])

    def test_unknown_ancestors_subcommand_returns_usage(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="/ancestors unknown our-ark/enoch#32"))

        self.assertIn("Ancestor commands:", client.sent[0][1])

    def test_default_action_sandbox_is_danger_full_access(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(_action_sandbox(Path(temp)), "danger-full-access")

    def test_legacy_conversation_mode_state_is_ignored(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / ".enoch" / "action_mode.json"
            state.parent.mkdir()
            state.write_text('{"mode":"conversation-only"}', encoding="utf-8")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            self.assertTrue(bot._action_allowed())
            self.assertEqual(_action_sandbox(root), "danger-full-access")

    def test_progress_update_uses_minutes_at_default_interval(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        bot._send_progress(42, 60, "workspace-write")

        self.assertEqual(client.sent[0][1], "Enoch is still working after 1 minute: editing her code body.")

    def test_elapsed_time_omits_seconds(self) -> None:
        self.assertEqual(telegram._format_elapsed(0), "<1 minute")
        self.assertEqual(telegram._format_elapsed(59), "<1 minute")
        self.assertEqual(telegram._format_elapsed(60), "1 minute")
        self.assertEqual(telegram._format_elapsed(122), "2 minutes")
        self.assertEqual(telegram._format_elapsed(4082), "1 hour 8 minutes")

    @patch("enoch.command_surface.load_provider")
    def test_checktree_reports_clean_worktree(self, load_provider: MagicMock) -> None:
        load_provider.return_value.is_clean.return_value = True

        self.assertEqual(_checktree(ROOT), "Worktree status: clean")
        load_provider.assert_called_once_with("vcs", ROOT)

    @patch("enoch.command_surface.load_provider")
    def test_checktree_reports_dirty_worktree(self, load_provider: MagicMock) -> None:
        load_provider.return_value.is_clean.return_value = False
        load_provider.return_value.changed_files.return_value = ["README.md", "scratch.txt"]

        result = _checktree(ROOT)

        self.assertIn("Worktree status: dirty", result)
        self.assertIn("README.md", result)
        self.assertIn("scratch.txt", result)

    @patch("enoch.command_surface.load_provider")
    def test_checktree_reports_unknown_when_vcs_fails(self, load_provider: MagicMock) -> None:
        load_provider.return_value.is_clean.side_effect = GitError("not a repository")

        result = _checktree(ROOT)

        self.assertIn("Worktree status: unknown", result)
        self.assertIn("not a repository", result)

    @patch("enoch.app.core.run_immune_system")
    def test_doctor_runs_health_checks(self, run_immune_system: MagicMock) -> None:
        run_immune_system.return_value = MagicMock(
            passed=True,
            command="python3 -m unittest discover -s tests",
            diagnosis=MagicMock(summary="All tests passed.", suggested_action="Keep going.", failing_tests=[]),
        )
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/doctor"))

        run_immune_system.assert_called_once_with(ROOT)
        self.assertIn("Doctor passed.", client.sent[0][1])
        self.assertIn("All tests passed.", client.sent[0][1])

    @patch("enoch.app.core._schedule_daemon_restart")
    @patch("enoch.app.core.update_from_authoritative")
    def test_update_uses_shared_updater_and_restarts_when_safe(
        self,
        update_from_authoritative: MagicMock,
        schedule_restart: MagicMock,
    ) -> None:
        update_from_authoritative.return_value.message = "Enoch pulled latest main and doctor passed.\n\nRestarting now."
        update_from_authoritative.return_value.direct_action_result = "Updating 1111111..2222222"
        update_from_authoritative.return_value.restart_required = True
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/update"))

        update_from_authoritative.assert_called_once_with(ROOT)
        schedule_restart.assert_called_once_with(ROOT)
        self.assertIn("Enoch pulled latest main and doctor passed.", client.sent[0][1])
        self.assertIn("Restarting now.", client.sent[0][1])

    @patch("enoch.app.core._schedule_daemon_restart")
    @patch("enoch.app.core.update_from_authoritative")
    def test_update_does_not_restart_when_shared_result_does_not_request_it(
        self,
        update_from_authoritative: MagicMock,
        schedule_restart: MagicMock,
    ) -> None:
        update_from_authoritative.return_value.message = "Enoch is already up to date.\nAlready up to date."
        update_from_authoritative.return_value.direct_action_result = "Already up to date."
        update_from_authoritative.return_value.restart_required = False
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/update"))

        update_from_authoritative.assert_called_once_with(ROOT)
        schedule_restart.assert_not_called()
        self.assertIn("Enoch is already up to date.", client.sent[0][1])

    @patch("enoch.app.core.update_from_authoritative")
    def test_update_requires_locked_chat(self, update_from_authoritative: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/update"))

        update_from_authoritative.assert_not_called()
        self.assertIn("locked to one conversation", client.sent[0][1])

    @patch("enoch.app.core._schedule_daemon_restart")
    def test_restart_schedules_daemon_restart_after_reply(self, schedule_restart: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/restart"))

        schedule_restart.assert_called_once_with(ROOT)
        self.assertIn("Enoch is restarting.", client.sent[0][1])

    @patch("enoch.app.core._schedule_daemon_restart")
    def test_restart_requires_locked_chat(self, schedule_restart: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(chat_id=42, text="/restart"))

        schedule_restart.assert_not_called()
        self.assertIn("locked to one conversation", client.sent[0][1])

    @patch("enoch.app.core.respond", return_value="Let's think through reminders first.")
    def test_natural_feature_request_uses_read_only_wrapper(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="我想让 Enoch 支持 reminders"))

        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("Enoch wrapper instructions:", respond.call_args.args[1])
        self.assertIn("/do", respond.call_args.args[1])
        self.assertIn("/task", respond.call_args.args[1])
        self.sync_session_activity.assert_not_called()
        self.assertIn("Let's think through reminders first.", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.respond")
    @patch("enoch.app.core.act_in_session")
    def test_natural_edit_request_does_not_auto_run_work(
        self,
        act_in_session: MagicMock,
        respond: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        respond.return_value = (
            f"I can make that change.\n\n{EDIT_REQUEST_START}\nUpdate README for the new workflow.\n{EDIT_REQUEST_END}"
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

        _handle_update(bot, _message_update(chat_id=42, text="make the README clearer"))

        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("/do", respond.call_args.args[1])
        self.assertIn("/task", respond.call_args.args[1])
        self.assertEqual(client.edited, [])
        self.assertIn("I can make that change.", client.sent[0][1])
        self.assertNotIn(EDIT_REQUEST_START, client.sent[0][1])
        act_in_session.assert_not_called()
        self.sync_session_activity.assert_not_called()

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.respond", return_value="I can talk that through first.")
    @patch("enoch.app.core.prepare_local_publish")
    def test_natural_message_without_marker_does_not_publish_pr(
        self,
        prepare_local_publish: MagicMock,
        respond: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(chat_id=42, text="commit then open a PR for it"))

        respond.assert_called_once()
        prepare_local_publish.assert_not_called()
        self.assertIn("I can talk that through first.", client.sent[0][1])

    @patch("enoch.app.core.create_pull_request")
    @patch("enoch.app.core.push_current_branch")
    @patch("enoch.app.core.switch_branch")
    @patch("enoch.app.core.ensure_clean_worktree")
    @patch("enoch.app.core.current_branch", return_value="agent/enoch-gary")
    @patch("enoch.app.core.act_in_session")
    @patch("enoch.app.core.respond")
    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_publish_existing_branch_runs_as_job_action(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
        respond: MagicMock,
        act_in_session: MagicMock,
        current_branch: MagicMock,
        ensure_clean_worktree: MagicMock,
        switch_branch: MagicMock,
        push_current_branch: MagicMock,
        create_pull_request: MagicMock,
    ) -> None:
        push_current_branch.return_value = RemotePublishResult(
            branch="enoch/existing",
            remote="origin",
            pushed=True,
            ahead_count=1,
            compare_url="https://github.com/our-ark/enoch/compare/main...enoch/existing?expand=1",
        )
        create_pull_request.return_value = PullRequestResult(
            branch="enoch/existing",
            title="Existing work",
            body="body",
            created=True,
            url="https://github.com/our-ark/enoch/pull/3",
            fallback_url="https://github.com/our-ark/enoch/compare/main...enoch/existing?expand=1",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            metadata = root / ".agent" / "instance.yaml"
            metadata.parent.mkdir()
            metadata.write_text(
                'worktree:\n  branch: "agent/enoch-gary"\n',
                encoding="utf-8",
            )
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch(
                "enoch.app.core.prepare_existing_branch_worktree",
                return_value=TaskWorktree(1, root, "enoch/existing", True),
            ):
                with patch(
                    "enoch.app.core.remove_task_worktree",
                    return_value="Removed task worktree.",
                ):
                    with start_worker:
                        _handle_update(bot,
                            _message_update(
                                chat_id=42,
                                text="/do publish existing local branch `enoch/existing` as a PR against `main`",
                            )
                        )
                    self.assertEqual(len(started), 1)
                    bot._run_direct_task_job(started[0][0], session_key=started[0][1])

        respond.assert_not_called()
        act_in_session.assert_not_called()
        ensure_clean_worktree.assert_called_once_with(root)
        current_branch.assert_not_called()
        switch_branch.assert_not_called()
        push_current_branch.assert_called_once_with(root=root)
        create_pull_request.assert_called_once_with(root=root)
        self.assertEqual(len(client.sent), 2)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("https://github.com/our-ark/enoch/pull/3", client.edited[-1][2])

    @patch("enoch.app.core.create_pull_request")
    @patch("enoch.app.core.push_current_branch")
    @patch("enoch.app.core.prepare_local_publish")
    @patch("enoch.app.core.run_immune_system")
    @patch("enoch.app.core.delete_branch")
    @patch("enoch.app.core.switch_branch")
    @patch("enoch.app.core._task_branch_base", return_value="origin/main")
    @patch("enoch.app.core.ensure_clean_worktree")
    @patch("enoch.app.core.current_branch", side_effect=["main", "enoch/readme", "enoch/readme"])
    @patch("enoch.app.core.changed_files", return_value=["README.md"])
    @patch("enoch.app.core._worktree_snapshot", side_effect=["clean", "changed"])
    @patch("enoch.app.core.act_in_session", return_value="Updated README.")
    @patch("enoch.app.core.respond")
    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core.time.time", return_value=789)
    def test_do_runs_direct_work_as_job(
        self,
        _time: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
        respond: MagicMock,
        act_in_session: MagicMock,
        _snapshot: MagicMock,
        _changed_files: MagicMock,
        _current_branch: MagicMock,
        _ensure_clean_worktree: MagicMock,
        task_branch_base: MagicMock,
        _switch_branch: MagicMock,
        _delete_branch: MagicMock,
        run_immune_system: MagicMock,
        prepare_local_publish: MagicMock,
        push_current_branch: MagicMock,
        create_pull_request: MagicMock,
    ) -> None:
        run_immune_system.return_value = _doctor_result()
        prepare_local_publish.return_value = LocalPublishResult(
            branch="enoch/readme",
            commit_message="Update README directly.",
            changed_files=["README.md"],
            diff="README.md | 1 +",
            doctor=_doctor_result(),
            commit_sha="abc123",
        )
        push_current_branch.return_value = RemotePublishResult(
            branch="enoch/readme",
            remote="origin",
            pushed=True,
            ahead_count=1,
            compare_url="https://github.com/our-ark/enoch/compare/main...enoch/readme?expand=1",
        )
        create_pull_request.return_value = PullRequestResult(
            branch="enoch/readme",
            title="Update README",
            body="body",
            created=True,
            url="https://github.com/our-ark/enoch/pull/2",
            fallback_url="https://github.com/our-ark/enoch/compare/main...enoch/readme?expand=1",
        )
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            context="Use the earlier README scope decision.",
            source="chat-snapshot",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch(
                "enoch.app.core.prepare_task_worktree",
                return_value=TaskWorktree(1, root, "enoch/readme", True),
            ) as prepare_task_worktree:
                with patch(
                    "enoch.app.core.remove_task_worktree",
                    return_value="Removed task #1 worktree.",
                ):
                    with start_worker:
                        _handle_update(bot, _message_update(chat_id=42, text="/do Update README directly."))
                    self.assertEqual(len(started), 1)
                    bot._run_direct_task_job(started[0][0], session_key=started[0][1])
            status = task_queue_status(root)
            events = load_task_events(root, task_id=1)

        respond.assert_not_called()
        act_in_session.assert_called_once()
        self.assertEqual(act_in_session.call_args.kwargs["session_key"], "telegram:42:do:1")
        self.assertIn("Work request:", act_in_session.call_args.args[1])
        self.assertIn("Conversation context snapshot:", act_in_session.call_args.args[1])
        self.assertIn("Use the earlier README scope decision.", act_in_session.call_args.args[1])
        self.assertNotIn("Edit phase:", act_in_session.call_args.args[1])
        prepare_local_publish.assert_called_once()
        push_current_branch.assert_called_once_with(root=root)
        create_pull_request.assert_called_once_with(root=root)
        task_branch_base.assert_called_once_with(root)
        prepare_task_worktree.assert_called_once()
        self.assertEqual(len(client.sent), 2)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Use the earlier README scope decision.", client.sent[0][1])
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].source, "chat-task")
        self.assertEqual(status.history[-1].initiated_by, "human")
        self.assertEqual([event.event for event in events], ["created", "started", "completed"])
        self.assertEqual([event.event_actor for event in events], ["human", "system", "agent"])
        self.assertEqual(status.history[-1].context, "Use the earlier README scope decision.")
        self.assertIn("https://github.com/our-ark/enoch/pull/2", status.history[-1].result)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_starts_worker_and_keeps_telegram_responsive(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work") as run_direct_work:
                with start_worker:
                    _handle_update(bot, _message_update(chat_id=42, text="/do long edit"))
                    run_direct_work.assert_not_called()
                    self.assertEqual(len(started), 1)
                    status = task_queue_status(root)
                    self.assertIsNotNone(status.running)

                    _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/status"))

        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Tasks:", client.sent[-1][1])
        self.assertIn("- running: #1 long edit", client.sent[-1][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_asks_for_clarification_before_running(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            clarification="Which change should Enoch make?"
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            with patch.object(bot, "_run_direct_work") as run_direct_work:
                _handle_update(bot, _message_update(chat_id=42, text="/do do it"))
            status = task_queue_status(root)

        run_direct_work.assert_not_called()
        self.assertIsNone(status.running)
        self.assertEqual(status.history, ())
        self.assertIn("Which change should Enoch make?", client.sent[0][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_marks_work_failure_status_failed(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(
                bot,
                "_run_direct_work",
                return_value="Enoch could not complete the requested work yet: usage limit",
            ):
                with start_worker:
                    _handle_update(bot, _message_update(chat_id=42, text="/do Update README directly."))
                self.assertEqual(len(started), 1)
                bot._run_direct_task_job(started[0][0], session_key=started[0][1])
            status = task_queue_status(root)

        self.assertEqual(len(client.sent), 2)
        self.assertIn("Status: failed", client.edited[-1][2])
        self.assertIn("Latest update: Failed. Final summary sent below.", client.edited[-1][2])
        self.assertNotIn("usage limit", client.edited[-1][2])
        self.assertIn("usage limit", client.sent[-1][1])
        self.assertEqual(status.history[-1].status, "failed")
        self.assertIn("usage limit", status.history[-1].result)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_command_includes_replied_message_context(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work", return_value="Done.") as run_direct_work:
                with start_worker:
                    _handle_update(bot,
                        _message_update(
                            chat_id=42,
                            text="/do handle this",
                            reply_text="Original bug report details.",
                        )
                    )
                self.assertEqual(len(started), 1)
                bot._run_direct_task_job(started[0][0], session_key=started[0][1])

        request = run_direct_work.call_args.args[1]
        self.assertEqual(run_direct_work.call_args.kwargs["session_key"], "telegram:42:do:1")
        self.assertIn("handle this", request)
        self.assertIn("Context from replied Telegram message:", request)
        self.assertIn("Original bug report details.", request)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_uses_new_action_session_for_each_direct_task(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work", return_value="Done.") as run_direct_work:
                with start_worker:
                    _handle_update(bot, _message_update(chat_id=42, text="/do first"))
                    self.assertEqual(len(started), 1)
                    bot._run_direct_task_job(started[0][0], session_key=started[0][1])

                    _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/do second"))
                    self.assertEqual(len(started), 2)
                    bot._run_direct_task_job(started[1][0], session_key=started[1][1])

        self.assertEqual(
            [call.kwargs["session_key"] for call in run_direct_work.call_args_list],
            ["telegram:42:do:1", "telegram:42:do:2"],
        )

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_queues_next_when_task_is_running(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        self.resolve_task_context_snapshot.return_value = TaskContextSnapshot(
            context="Use the latest README decision.",
            source="chat-snapshot",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            _handle_update(bot, _message_update(chat_id=42, text="/task existing work"))
            job = begin_next_task(root)
            assert job is not None
            already_queued = enqueue_task(42, "already queued work", root)

            with patch.object(bot, "_run_direct_work") as run_direct_work:
                _handle_update(bot, _message_update(update_id=2, chat_id=42, text="/do new work"))
            status = task_queue_status(root)

        run_direct_work.assert_not_called()
        self.assertEqual([pending.id for pending in status.pending], [3, already_queued.id])
        self.assertEqual(status.pending[0].context, "Use the latest README decision.")
        self.assertEqual(status.pending[0].context_source, "chat-snapshot")
        self.assertIn("Task #3", client.sent[-1][1])
        self.assertIn("Status: queued", client.sent[-1][1])
        self.assertIn("Queued next after running task #1.", client.sent[-1][1])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_finished_do_checks_for_queued_work(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work", return_value="Done."):
                with start_worker:
                    _handle_update(bot, _message_update(chat_id=42, text="/do first"))
                self.assertEqual(len(started), 1)
                enqueue_task(42, "queued next", root)
                with patch.object(bot, "_maybe_start_task_worker") as maybe_start:
                    bot._run_direct_task_job(started[0][0], session_key=started[0][1])

        maybe_start.assert_called_once()

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_stale_worker_does_not_announce_completed_after_authoritative_failure(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            started, start_worker = self._capture_direct_work_worker(bot)

            with start_worker:
                _handle_update(bot, _message_update(chat_id=42, text="/do isolated work"))
            job = started[0][0]

            def authoritative_failure(*_args, **_kwargs) -> str:
                fail_task(job.id, root, result="Recovery marked the task failed.")
                return "Original worker completed later."

            with patch.object(bot, "_run_direct_work", side_effect=authoritative_failure):
                bot._run_direct_task_job(job, session_key=started[0][1])

            history = task_queue_status(root).history

        self.assertEqual(history[-1].status, "failed")
        self.assertFalse(
            any("Final status: completed" in message for _, message in client.sent)
        )

    def test_stop_workers_cancels_active_work_and_waits_for_workers(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            bot = EnochApplication(
                load_identity(),
                root,
                FakeTelegramClient(allowed_chat_id=42),
            )
            cancellation = threading.Event()
            direct_worker = MagicMock()
            direct_worker.is_alive.return_value = True
            queued_worker = MagicMock()
            queued_worker.is_alive.return_value = True
            bot._task_cancellations[1] = cancellation
            bot._direct_workers[1] = direct_worker
            bot._task_worker = queued_worker

            bot.stop_workers(timeout_seconds=1)

        self.assertTrue(bot._stopping)
        self.assertTrue(cancellation.is_set())
        direct_worker.join.assert_called_once()
        queued_worker.join.assert_called_once()

    @patch("enoch.app.core.create_pull_request")
    @patch("enoch.app.core.push_current_branch")
    @patch("enoch.app.core.prepare_local_publish")
    @patch("enoch.app.core.delete_branch")
    @patch("enoch.app.core.switch_branch")
    @patch("enoch.app.core.ensure_clean_worktree")
    @patch("enoch.app.core.current_branch", return_value="enoch/readme")
    def test_task_publish_records_pr_url_before_worker_completion(
        self,
        _current_branch: MagicMock,
        _ensure_clean_worktree: MagicMock,
        _switch_branch: MagicMock,
        _delete_branch: MagicMock,
        prepare_local_publish: MagicMock,
        push_current_branch: MagicMock,
        create_pull_request: MagicMock,
    ) -> None:
        prepare_local_publish.return_value = LocalPublishResult(
            branch="enoch/readme",
            commit_message="Update README directly.",
            changed_files=["README.md"],
            diff="README.md | 1 +",
            doctor=_doctor_result(),
            commit_sha="abc123",
        )
        push_current_branch.return_value = RemotePublishResult(
            branch="enoch/readme",
            remote="origin",
            pushed=True,
            ahead_count=1,
            compare_url="https://github.com/our-ark/enoch/compare/main...enoch/readme?expand=1",
        )
        create_pull_request.return_value = PullRequestResult(
            branch="enoch/readme",
            title="Update README",
            body="body",
            created=True,
            url="https://github.com/our-ark/enoch/pull/2",
            fallback_url="https://github.com/our-ark/enoch/compare/main...enoch/readme?expand=1",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)
            bot._resident_branch = "agent/enoch-gary"
            _handle_update(bot, _message_update(chat_id=42, text="/task Update README directly."))
            job = begin_next_task(root)
            assert job is not None
            status_message = WorkStatusMessage(
                chat_id=42,
                message_id=2001,
                request=job.text,
                started_at=time.monotonic(),
                task_id=job.id,
                status="running",
            )
            token = _CURRENT_WORK_STATUS.set(status_message)
            try:
                bot._publish_feature_pr(42, job.text, ("README.md",))
            finally:
                _CURRENT_WORK_STATUS.reset(token)
            status = task_queue_status(root)

        self.assertIsNotNone(status.running)
        _switch_branch.assert_called_once_with("agent/enoch-gary", root)
        self.assertIn("https://github.com/our-ark/enoch/pull/2", status.running.result)
        self.assertIn("Enoch opened a pull request.", status.running.result)

    @patch("enoch.app.core.close_pull_request")
    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_do_closes_duplicate_pull_requests(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
        close_pull_request: MagicMock,
    ) -> None:
        close_pull_request.side_effect = [
            PullRequestCloseResult(number=2, closed=True, url="https://github.com/our-ark/enoch/pull/2"),
            PullRequestCloseResult(number=3, closed=True, url="https://github.com/our-ark/enoch/pull/3"),
        ]
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with start_worker:
                _handle_update(bot, _message_update(chat_id=42, text="/do 保留 #1，关闭重复的 #2 和 #3"))
            self.assertEqual(len(started), 1)
            bot._run_direct_task_job(started[0][0], session_key=started[0][1])

        self.assertEqual(close_pull_request.call_count, 2)
        self.assertEqual(close_pull_request.call_args_list[0].args[0], 2)
        self.assertIn("duplicate of #1", close_pull_request.call_args_list[0].kwargs["comment"])
        self.assertEqual(close_pull_request.call_args_list[1].args[0], 3)
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("Latest update: Completed. Final summary sent below.", client.edited[-1][2])
        self.assertIn("Kept PR: #1", client.sent[-1][1])
        self.assertIn("#2: closed", client.sent[-1][1])
        self.assertIn("#3: closed", client.sent[-1][1])

    def test_client_splits_long_messages(self) -> None:
        config = TelegramConfig(token="token", poll_timeout=1)
        client = TelegramClient(config)
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True}

        client._call = fake_call
        client.send_message(42, "x" * 4100)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertEqual(calls[0][1]["chat_id"], 42)

    def test_client_sets_read_ack_as_message_reaction(self) -> None:
        config = TelegramConfig(token="token", poll_timeout=1)
        client = TelegramClient(config)
        calls = []

        def fake_call(method, payload):
            calls.append((method, payload))
            return {"ok": True}

        client._call = fake_call
        client.send_read_ack(42, 1001)

        self.assertEqual(calls[0][0], "setMessageReaction")
        self.assertEqual(calls[0][1]["chat_id"], 42)
        self.assertEqual(calls[0][1]["message_id"], 1001)
        self.assertEqual(json.loads(calls[0][1]["reaction"]), [{"type": "emoji", "emoji": READ_ACK_EMOJI}])

    def test_run_once_advances_offset(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient()
            client.updates = [_message_update(update_id=10, chat_id=42, text="/status")]
            bot = EnochApplication(load_identity(), root, client)

            bot.run_once()

        self.assertEqual(bot.offset, 11)
        self.assertIn("Telegram conversation id: 42", client.sent[0][1])

    def test_run_once_persists_offset_for_restart(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient()
            client.updates = [_message_update(update_id=10, chat_id=42, text="/status")]
            bot = EnochApplication(load_identity(), root, client)

            bot.run_once()
            restarted_bot = EnochApplication(load_identity(), root, client)
            restarted_bot.run_once()

            self.assertEqual(bot.offset, 11)
            self.assertEqual(restarted_bot.offset, 11)
            self.assertEqual(client.offsets, [None, 11])
            self.assertEqual(len(client.sent), 1)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_final_send_failure_still_records_and_advances_offset(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FailingTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(update_id=10, chat_id=42, text="/status"))

        self.assertEqual(bot.offset, 11)
        log_conversation_turn.assert_called_once()
        self.assertIn("Telegram send failed", log_conversation_turn.call_args.kwargs["reply"])

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    def test_read_ack_failure_is_logged_without_blocking_reply(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        client = FailingAckTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        _handle_update(bot, _message_update(update_id=10, chat_id=42, text="/status"))

        self.log_system_event.assert_any_call(
            "chat_read_ack_failed",
            root=ROOT,
            status="failed",
            details={
                "provider": "telegram",
                "chat_id": 42,
                "message_id": 1010,
                "error": "reaction failed",
            },
        )
        self.assertIn("Enoch status:", client.sent[0][1])

    def test_progress_send_failure_does_not_abort_action(self) -> None:
        client = FailingTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)

        bot._send_progress(42, 60, "danger-full-access")

        self.assertEqual(client.sent, [])

    @patch("builtins.print")
    @patch("enoch.app.core.time.sleep")
    def test_run_forever_continues_after_polling_error(
        self,
        sleep: MagicMock,
        _print: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochApplication(load_identity(), ROOT, client)
        bot.run_once = MagicMock(side_effect=[OSError("network down"), KeyboardInterrupt])

        with self.assertRaises(KeyboardInterrupt):
            bot.run_forever()

        sleep.assert_called_once_with(5)

    @patch("enoch.operations.update_tools.update_repository")
    @patch("enoch.operations.update_tools.current_branch", return_value="main")
    @patch(
        "enoch.operations.update_tools.authoritative_repository_revision",
        return_value="bbb222",
    )
    @patch("enoch.operations.update_tools.resolve_revision", return_value="aaa111")
    @patch("enoch.operations.update_tools.authoritative_branch_name", return_value="main")
    @patch("enoch.operations.update_tools.refresh_repository")
    def test_ensure_local_main_current_pulls_stale_main(
        self,
        refresh_repository: MagicMock,
        _authoritative_branch: MagicMock,
        _resolve_revision: MagicMock,
        _authoritative_revision: MagicMock,
        _current_branch: MagicMock,
        update_repository: MagicMock,
    ) -> None:

        ensure_local_main_current(ROOT)

        refresh_repository.assert_called_once_with(ROOT)
        update_repository.assert_called_once_with(ROOT)

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_system_event", side_effect=OSError("disk full"))
    def test_direct_action_system_log_failure_does_not_abort_workflow(
        self,
        _log_system_event: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        telegram._record_direct_action("push branch", "Pushed branch.", ROOT)

        update_memory.assert_not_called()

    @patch("enoch.app.core.ensure_long_term_memory")
    @patch("enoch.app.core.log_conversation_turn")
    @patch("enoch.app.core._schedule_daemon_restart")
    @patch("enoch.app.core.update_from_authoritative")
    def test_restart_triggering_update_saves_offset_before_restart(
        self,
        update_from_authoritative: MagicMock,
        schedule_restart: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        update_from_authoritative.return_value.message = "Enoch pulled latest main and doctor passed."
        update_from_authoritative.return_value.direct_action_result = "Updating 1111111..2222222"
        update_from_authoritative.return_value.restart_required = True
        with TemporaryDirectory() as temp:
            root = Path(temp)

            def assert_offset_saved(restart_root: Path) -> None:
                self.assertEqual(restart_root, root)
                offset_file = root / ".enoch" / "channels" / "telegram" / "cursor.json"
                self.assertEqual(json.loads(offset_file.read_text(encoding="utf-8"))["cursor"], 11)

            schedule_restart.side_effect = assert_offset_saved
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochApplication(load_identity(), root, client)

            _handle_update(bot, _message_update(update_id=10, chat_id=42, text="/update"))

        schedule_restart.assert_called_once_with(root)
        self.assertEqual(bot.offset, 11)


class FakeTelegramClient:
    name = "telegram"
    provider_kind = "chat"

    def __init__(self, allowed_chat_id=None) -> None:
        self.config = TelegramConfig(token="token", allowed_chat_id=allowed_chat_id)
        self.acks = []
        self.sent = []
        self.edited = []
        self.updates = []
        self.offsets = []
        self.downloads = []

    def get_updates(self, offset=None):
        self.offsets.append(offset)
        if offset is None:
            return self.updates
        return [update for update in self.updates if int(update["update_id"]) >= offset]

    def receive(self, cursor=None):
        return [
            event
            for update in self.get_updates(cursor)
            if (event := telegram_event(update)) is not None
        ]

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return 2000 + len(self.sent)

    def edit_message(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))

    def send_read_ack(self, chat_id, message_id):
        self.acks.append((chat_id, message_id, READ_ACK_EMOJI))

    def download_file(self, file_id, destination, *, max_bytes):
        self.downloads.append((file_id, max_bytes))
        destination.write_bytes(b"\xff\xd8\xfftelegram-photo")


class FailingTelegramClient(FakeTelegramClient):
    def send_message(self, chat_id, text):
        raise TelegramError("send failed")


class FailingAckTelegramClient(FakeTelegramClient):
    def send_read_ack(self, chat_id, message_id):
        raise TelegramError("reaction failed")


def _pull_request_info(
    *,
    number: int,
    title: str,
    head_branch: str = "feature/example",
    is_draft: bool = False,
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    author: str = "",
) -> PullRequestMergeCandidate:
    url = f"https://github.com/our-ark/enoch/pull/{number}"
    return PullRequestMergeCandidate(
        target=PullRequestTarget(
            reference=url,
            number=number,
            repository="our-ark/enoch",
        ),
        number=number,
        repository="our-ark/enoch",
        url=url,
        state="OPEN",
        is_draft=is_draft,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        head_oid=f"head-{number}",
        base_branch="main",
        title=title,
        head_branch=head_branch,
        author=author,
    )


def _message_update(update_id=1, chat_id=42, text="hello", reply_text=None):
    message = {
        "message_id": 1000 + update_id,
        "chat": {"id": chat_id},
        "text": text,
    }
    if reply_text is not None:
        message["reply_to_message"] = {
            "message_id": 9000 + update_id,
            "chat": {"id": chat_id},
            "text": reply_text,
        }
    return {
        "update_id": update_id,
        "message": message,
    }


def _photo_update(update_id=1, chat_id=42, caption=""):
    message = {
        "message_id": 1000 + update_id,
        "chat": {"id": chat_id},
        "photo": [
            {"file_id": "small-photo", "width": 90, "height": 90, "file_size": 100},
            {"file_id": "large-photo", "width": 1280, "height": 720, "file_size": 500},
        ],
    }
    if caption:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


def _handle_update(bot: EnochApplication, update: dict) -> None:
    event = telegram_event(update)
    if event is not None:
        bot.handle_event(event)


def _save_test_offset(name: str, offset: int, root: Path) -> None:
    if root == ROOT:
        return
    save_channel_cursor(name, offset, root)


def _load_lifecycle_state(root: Path) -> dict:
    return load_channel_lifecycle("telegram", root)


def _doctor_result():
    return MagicMock(
        passed=True,
        command="python3 -m unittest discover -s tests",
        output="OK",
        diagnosis=DoctorDiagnosis(
            summary="All configured health checks passed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="No repair needed.",
        ),
    )


def _lineage_candidate():
    return LineageCandidate(
        id="our-ark/enoch#32",
        repo="our-ark/enoch",
        pr_number=32,
        title="Add Telegram thinking level command",
        url="https://github.com/our-ark/enoch/pull/32",
        merged_at="2026-06-17T01:31:12Z",
        merge_commit="abc123",
        ancestor_name="Enoch",
        depth=1,
        labels=("inherit:recommended",),
        files=("src/enoch/telegram/bot.py",),
        relevance="high",
        confidence="high",
        reason="PR has an inheritance label.",
        body_excerpt="Adds /thinking.",
    )


if __name__ == "__main__":
    unittest.main()
