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
from enoch.automatic_learning import learning_index_path
from enoch.backlog import add_backlog_item, backlog_status
from enoch.config import read_section
from enoch.command_surface import checktree as _checktree
from enoch.cron import add_cron_job, cron_status
from enoch.evolve import MODE_AUTO_EVOLVE, evolve_report, load_evolve_candidates, set_evolve_mode, set_evolve_schedule
from enoch.git_tools import GitError
from enoch.github.workflow import (
    LocalPublishResult,
    PullRequestCloseResult,
    PullRequestResult,
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
from enoch.prompt_append import EDIT_REQUEST_END, EDIT_REQUEST_START, MEMORY_REQUEST_END, MEMORY_REQUEST_START
from enoch.task_queue import (
    TaskJob,
    begin_next_task,
    complete_task,
    enqueue_task,
    record_task_result,
    task_queue_path,
    task_queue_status,
)
from enoch.telegram.bot import (
    EnochTelegramBot,
    ShutdownRequested,
    TaskContextSnapshot,
    WorkStatusMessage,
    _CURRENT_WORK_STATUS,
    _action_sandbox,
    _action_mode,
    _begin_lifecycle_run,
    _format_task_final_message,
    _parse_task_context_snapshot,
    _record_lifecycle_shutdown,
    _shutdown_message,
    _signal_reason,
    _task_context_snapshot_prompt,
)
from enoch.telegram import bot as telegram
from enoch.telegram.client import READ_ACK_EMOJI, TelegramClient, TelegramConfig, TelegramError, load_config
from enoch.telegram.lifecycle import (
    load_lifecycle_state as _load_lifecycle_state,
    previous_shutdown_warning as _previous_shutdown_warning,
    save_telegram_offset as _real_save_telegram_offset,
)
from enoch.update_tools import main_pull_summary as _main_pull_summary


_REAL_TASK_CONTEXT_RESOLVER = EnochTelegramBot._resolve_task_context_snapshot


class EnochTelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        self._task_context_snapshot_patch = patch(
            "enoch.telegram.bot.EnochTelegramBot._resolve_task_context_snapshot",
            return_value=TaskContextSnapshot(),
        )
        self.resolve_task_context_snapshot = self._task_context_snapshot_patch.start()
        self.addCleanup(self._task_context_snapshot_patch.stop)
        self._sync_session_activity_patch = patch("enoch.telegram.bot._sync_session_activity")
        self.sync_session_activity = self._sync_session_activity_patch.start()
        self.addCleanup(self._sync_session_activity_patch.stop)
        self._save_telegram_offset_patch = patch(
            "enoch.telegram.bot._save_telegram_offset",
            side_effect=_save_test_offset,
        )
        self.save_telegram_offset = self._save_telegram_offset_patch.start()
        self.addCleanup(self._save_telegram_offset_patch.stop)
        self._log_conversation_turn_patch = patch("enoch.telegram.bot.log_conversation_turn")
        self.log_conversation_turn = self._log_conversation_turn_patch.start()
        self.addCleanup(self._log_conversation_turn_patch.stop)
        self._log_system_event_patch = patch("enoch.telegram.bot.log_system_event")
        self.log_system_event = self._log_system_event_patch.start()
        self.addCleanup(self._log_system_event_patch.stop)
        self._update_memory_patch = patch("enoch.telegram.bot.ensure_long_term_memory")
        self.update_memory = self._update_memory_patch.start()
        self.addCleanup(self._update_memory_patch.stop)

    def _capture_direct_work_worker(self, bot: EnochTelegramBot):
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
            with self.assertRaisesRegex(TelegramError, "setup-token"):
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
                    telegram.main()
            finally:
                os.chdir(previous)

        printed = print_.call_args.args[0]
        self.assertIn("setup-token", printed)
        self.assertIn("ENOCH_TELEGRAM_BOT_TOKEN", printed)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.respond", return_value="Hello from Enoch")
    def test_replies_to_allowed_chat(
        self,
        respond: MagicMock,
        log_conversation_turn: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="hello"))

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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_replies_do_not_include_accumulated_input_tokens(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        def answer(*_args, **_kwargs):
            brain.update_token_usage(input_tokens=321, cached_input_tokens=300, output_tokens=12)
            return "Hello from Enoch"

        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        with patch("enoch.telegram.bot.respond", side_effect=answer):
            bot.handle_update(_message_update(chat_id=42, text="hello"))

        self.assertEqual(client.sent, [(42, "Hello from Enoch")])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.respond", return_value="[PLAIN_TEXT_MARKER]\nIntent:\nAdd reminders")
    def test_plain_marker_text_is_not_special_for_normal_chat(
        self,
        respond: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="add reminders"))

        respond.assert_called_once()
        self.assertIn("[PLAIN_TEXT_MARKER]", client.sent[0][1])
        self.assertNotIn("local = edit on a feature branch", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.remember_memory", return_value={"id": "mem_1", "text": "User likes apples."})
    @patch(
        "enoch.telegram.bot.respond",
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="I like apples"))

        respond.assert_called_once()
        remember_memory.assert_called_once_with("User likes apples.", root=root)
        sent = client.sent[0][1]
        self.assertIn("I will remember that.", sent)
        self.assertIn("Saved to Enoch long-term memory.", sent)
        self.assertNotIn(MEMORY_REQUEST_START, sent)
        self.assertNotIn(MEMORY_REQUEST_END, sent)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.remember_memory", side_effect=OSError("read-only"))
    @patch(
        "enoch.telegram.bot.respond",
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="I like apples"))

        remember_memory.assert_called_once_with("User likes apples.", root=root)
        sent = client.sent[0][1]
        self.assertIn("Enoch could not save that long-term memory.", sent)
        self.assertNotIn(MEMORY_REQUEST_START, sent)

    @patch("enoch.telegram.bot.respond")
    def test_ignores_disallowed_chat(self, respond: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=7, text="hello"))

        respond.assert_not_called()
        self.assertEqual(client.acks, [])
        self.assertEqual(client.sent, [])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.respond", return_value="natural reply")
    def test_unknown_slash_command_routes_to_natural_conversation(
        self,
        respond: MagicMock,
        log_conversation_turn: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/unknown"))

        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        log_conversation_turn.assert_called_once()
        update_memory.assert_called_once_with(ROOT)
        self.assertEqual(client.sent[0][1], "natural reply")

    @patch("enoch.telegram.bot.model_summary", return_value="AI model: gpt-5-codex")
    def test_telegram_command_parser_accepts_bot_mentions(self, _model_summary: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/status@EnochBot"))

        self.assertIn("Enoch status:", client.sent[0][1])

    def test_startup_notification_goes_to_locked_chat(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.notify_startup()

        self.assertEqual(client.sent[0][0], 42)
        self.assertIn("Enoch restarted and is listening on Telegram.", client.sent[0][1])
        self.assertIn("Action mode: full-access.", client.sent[0][1])
        self.assertIn("Last git main pull observed:", client.sent[0][1])
        self.assertIn("/help", client.sent[0][1])
        self.sync_session_activity.assert_called_once()
        self.assertIn("Enoch startup context:", self.sync_session_activity.call_args.args[3])

    def test_startup_notification_reports_previous_shutdown_warning(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(
            load_identity(),
            ROOT,
            client,
            previous_shutdown_warning="Previous shutdown: unexpected; Enoch could not send the normal shutdown message.",
        )

        bot.notify_startup()

        self.assertIn("Previous shutdown: unexpected", client.sent[0][1])

    def test_startup_notification_requires_locked_chat(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.notify_startup()

        self.assertEqual(client.sent, [])

    def test_shutdown_notification_goes_to_locked_chat(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.notify_shutdown("SIGTERM")

        self.assertEqual(client.sent[0][0], 42)
        self.assertIn("Enoch is shutting down.", client.sent[0][1])
        self.assertIn("Reason: SIGTERM.", client.sent[0][1])
        self.assertIn("Action mode: full-access.", client.sent[0][1])

    def test_shutdown_notification_requires_locked_chat(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.notify_shutdown("SIGTERM")

        self.assertEqual(client.sent, [])

    def test_shutdown_message_includes_reason_and_mode(self) -> None:
        message = _shutdown_message(load_identity(), ROOT, "keyboard interrupt")

        self.assertIn("Enoch is shutting down.", message)
        self.assertIn("Reason: keyboard interrupt.", message)
        self.assertIn("Action mode:", message)

    def test_signal_reason_uses_signal_name(self) -> None:
        self.assertEqual(_signal_reason(15), "SIGTERM")

    def test_lifecycle_run_warns_after_unexpected_prior_running_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".enoch").mkdir()
            (root / ".enoch" / "telegram_lifecycle.json").write_text(
                json.dumps({"status": "running"}),
                encoding="utf-8",
            )

            warning = _begin_lifecycle_run(root)
            state = _load_lifecycle_state(root)

        self.assertIn("Previous shutdown: unexpected", warning)
        self.assertEqual(state["status"], "running")

    def test_lifecycle_run_records_started_head(self) -> None:
        current_head = MagicMock(return_value="1111111111111111111111111111111111111111")
        with TemporaryDirectory() as temp:
            root = Path(temp)

            with patch.dict(_begin_lifecycle_run.__globals__, {"current_head": current_head}):
                _begin_lifecycle_run(root)
            state = _load_lifecycle_state(root)

        current_head.assert_called_once_with(root)
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

            _record_lifecycle_shutdown(root, "SIGTERM", shutdown_notification_sent=True)
            state = _load_lifecycle_state(root)

        self.assertEqual(state["status"], "stopped")
        self.assertEqual(state["reason"], "SIGTERM")
        self.assertTrue(state["shutdown_notification_sent"])

    @patch("enoch.update_tools.run_git")
    def test_main_pull_summary_reports_fetch_head_timestamp_and_main_sha(self, run_git: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fetch_head = root / ".git" / "FETCH_HEAD"
            fetch_head.parent.mkdir()
            fetch_head.write_text("abc123\t\tbranch 'main' of https://github.com/our-ark/enoch\n", encoding="utf-8")
            os.utime(fetch_head, (1_700_000_000, 1_700_000_000))
            run_git.side_effect = [
                MagicMock(returncode=0, stdout=".git/FETCH_HEAD"),
                MagicMock(returncode=0, stdout="abc1234"),
            ]

            summary = _main_pull_summary(root)

        self.assertIn("Last git main pull observed:", summary)
        self.assertIn("2023-", summary)
        self.assertIn("origin/main abc1234", summary)

    @patch("enoch.update_tools.run_git")
    def test_main_pull_summary_is_unavailable_without_main_fetch(self, run_git: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            fetch_head = root / ".git" / "FETCH_HEAD"
            fetch_head.parent.mkdir()
            fetch_head.write_text("abc123\t\tbranch 'feature' of https://github.com/our-ark/enoch\n", encoding="utf-8")
            run_git.side_effect = [
                MagicMock(returncode=0, stdout=".git/FETCH_HEAD"),
                MagicMock(returncode=0, stdout="abc1234"),
            ]

            summary = _main_pull_summary(root)

        self.assertEqual(summary, "Last git main pull observed: unavailable (origin/main abc1234)")

    def test_start_points_to_help(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/start"))

        self.assertEqual(client.sent[0][1], "Use /help to see available commands.")

    def test_help_lists_safe_commands(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help"))

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
        self.assertLess(reply.index("Evolve:"), reply.index("Operations:"))
        self.assertIn("Common:", client.sent[0][1])
        self.assertNotIn("Memory:", client.sent[0][1])
        self.assertIn("Inherit:", client.sent[0][1])
        self.assertIn("Learn:", client.sent[0][1])
        self.assertIn("Evolve:", client.sent[0][1])
        self.assertIn("Work:", client.sent[0][1])
        self.assertIn("Operations:", client.sent[0][1])
        self.assertNotIn("Mode:", client.sent[0][1])
        self.assertIn("/doctor", client.sent[0][1])
        self.assertNotIn("/debug", client.sent[0][1])
        self.assertIn("/mode [chat|work]", client.sent[0][1])
        self.assertLess(client.sent[0][1].index("Common:"), client.sent[0][1].index("/mode [chat|work]"))
        self.assertLess(client.sent[0][1].index("/mission [text]"), client.sent[0][1].index("/status"))
        self.assertLess(client.sent[0][1].index("/mode [chat|work]"), client.sent[0][1].index("Work:"))
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
        self.assertIn("/tasks - show running, queued, and recent task history", client.sent[0][1])
        self.assertIn("/stop - stop the currently running task", client.sent[0][1])
        self.assertIn("/backlog [p0|p1|p2] <request> - save deferred work for idle time", client.sent[0][1])
        self.assertNotIn("/backlog remove <id> - remove a pending backlog item", client.sent[0][1])
        self.assertNotIn("/backlog priority <id> p0|p1|p2 - reprioritize backlog work", client.sent[0][1])
        self.assertNotIn("/cron every <interval> <request> - schedule recurring work", client.sent[0][1])
        self.assertNotIn("/cron cancel <id> - cancel a scheduled job", client.sent[0][1])
        self.assertIn("/cron - show scheduled jobs", client.sent[0][1])
        self.assertIn("/evolve - show self-evolution mode, theme, and top candidate", client.sent[0][1])
        self.assertIn("/feedback - show feedback signals available to self-evolution", client.sent[0][1])
        self.assertIn("/experience - show experience candidates from Enoch's work history", client.sent[0][1])
        self.assertIn("/propose - rank all evolve sources and propose the strongest candidate", client.sent[0][1])
        self.assertNotIn("/evolve mode <mode> - set self-evolution behavior", client.sent[0][1])
        self.assertNotIn("/evolve mode disabled|co-evolve|auto-evolve", client.sent[0][1])
        self.assertNotIn("/evolve theme <text> - set the current self-evolution theme", client.sent[0][1])
        self.assertNotIn("/evolve candidates - show current self-evolution candidates", client.sent[0][1])
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
        self.assertIn("/restart - restart Enoch's Telegram daemon from the locked chat", client.sent[0][1])
        self.assertLess(client.sent[0][1].index("Operations:"), client.sent[0][1].index("/shutdown"))
        self.assertIn("say the request naturally", client.sent[0][1])

    def test_help_inherit_shows_inherit_usage(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help inherit"))

        reply = client.sent[0][1]
        self.assertIn("Inherit commands:", reply)
        self.assertIn("/inherit - show inheritable direct-parent changes", reply)
        self.assertIn("/inherit show - show inheritable direct-parent changes", reply)
        self.assertIn("/inherit <change_id> - inherit one direct-parent change", reply)
        self.assertIn("/inherit all - inherit all direct-parent changes", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    def test_help_topic_shows_single_command_usage(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help task cancel"))

        reply = client.sent[0][1]
        self.assertIn("Task commands:", reply)
        self.assertIn("/task <request> - queue background work for Enoch", reply)
        self.assertIn("/task cancel <id> - cancel a queued background task", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    def test_help_topic_supports_work_command_aliases(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help /crons"))

        reply = client.sent[0][1]
        self.assertIn("Cron commands:", reply)
        self.assertIn("/cron every <interval> <request>", reply)
        self.assertIn("/cron cancel <id>", reply)

    def test_help_backlog_shows_backlog_subcommands(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help backlog"))

        reply = client.sent[0][1]
        self.assertIn("Backlog commands:", reply)
        self.assertIn("/backlog remove <id>", reply)
        self.assertIn("/backlog priority <id> p0|p1|p2", reply)
        self.assertIn("/backlog promote <id>", reply)

    def test_help_evolve_shows_evolve_usage(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help evolve"))

        reply = client.sent[0][1]
        self.assertIn("Evolve commands:", reply)
        self.assertIn("/feedback", reply)
        self.assertIn("/experience", reply)
        self.assertIn("/propose", reply)
        self.assertIn("/evolve mode <mode>", reply)
        self.assertIn("Modes: disabled, co-evolve, auto-evolve.", reply)
        self.assertNotIn("/evolve mode disabled|co-evolve|auto-evolve", reply)
        self.assertNotIn("/evolve co-evolve - propose candidates", reply)
        self.assertNotIn("/evolve disabled - stop collecting", reply)
        self.assertNotIn("/evolve auto-evolve - select bounded", reply)
        self.assertIn("/evolve theme <text>", reply)
        self.assertIn("/evolve candidates", reply)
        self.assertIn("/evolve select <id>", reply)
        self.assertIn("/evolve run <id>", reply)
        self.assertIn("/evolve reject <id>", reply)
        self.assertIn("/evolve schedule <text>", reply)
        self.assertNotIn("/evolve schedule off - stop scheduled evolve checks", reply)
        self.assertNotIn("/evolve schedule once a day - run evolve once per day", reply)
        self.assertNotIn("/evolve schedule every <interval> - run periodic evolve checks", reply)
        self.assertNotIn("/evolve schedule daily HH:MM - run evolve once per day at local time", reply)
        self.assertNotIn("/evolve schedule cron '30 9 * * *' - run evolve with a cron-style daily schedule", reply)
        self.assertNotIn("Enoch Telegram commands:", reply)

    def test_help_debug_reports_unknown_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help /debug"))

        self.assertEqual(client.sent[0][1], "No help found for /debug.\nUse /help to see available commands.")

    def test_help_topic_reports_unknown_command(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/help nope"))

        self.assertEqual(client.sent[0][1], "No help found for /nope.\nUse /help to see available commands.")

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_task_command_enqueues_persistent_fifo_job(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/task add queued work"))

            data = json.loads(task_queue_path(root).read_text(encoding="utf-8"))

        self.assertEqual(len(client.sent), 1)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Status: queued", client.sent[0][1])
        self.assertIn("Latest update: Queued at position 1.", client.sent[0][1])
        self.assertIn("PRs created:\n- none", client.sent[0][1])
        self.assertIn("Request:\nadd queued work", client.sent[0][1])
        self.assertEqual(data["pending"][0]["id"], 1)
        self.assertEqual(data["pending"][0]["chat_id"], 42)
        self.assertEqual(data["pending"][0]["text"], "add queued work")
        self.assertIsNone(data["running"])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_task_command_includes_replied_message_context(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(
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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/task do it"))

            data = json.loads(task_queue_path(root).read_text(encoding="utf-8"))

        self.resolve_task_context_snapshot.assert_called_once_with(42, "do it")
        self.assertEqual(data["pending"][0]["text"], "do it")
        self.assertEqual(data["pending"][0]["context"], "Build the reminders feature discussed earlier.")
        self.assertEqual(data["pending"][0]["context_source"], "chat-snapshot")
        self.assertIn("Conversation context snapshot:", client.sent[0][1])
        self.assertIn("Build the reminders feature discussed earlier.", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/task do it"))
            status = task_queue_status(root)

        self.assertEqual(status.pending, ())
        self.assertIn("Which feature should Enoch implement?", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_task_command_requires_request(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/task"))

        self.assertEqual(client.sent[0][1], "Use /task <request> to queue background work.")

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_task_cancel_removes_pending_task_and_edits_status(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/task add queued work"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/task cancel 1"))
            status = task_queue_status(root)

        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertIn("Cancelled task #1.", client.sent[-1][1])
        self.assertIn("Status: cancelled", client.edited[-1][2])
        self.assertIn("Latest update: Cancelled before running.", client.edited[-1][2])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_stop_cancels_running_task_and_edits_status(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            _started, start_worker = self._capture_direct_work_worker(bot)
            with start_worker:
                bot.handle_update(_message_update(chat_id=42, text="/do long edit"))
            status = task_queue_status(root)
            assert status.running is not None
            event = threading.Event()
            bot._task_cancellations[status.running.id] = event

            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/stop"))
            status = task_queue_status(root)

        self.assertTrue(event.is_set())
        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].status, "cancelled")
        self.assertIn("Stopped task #1.", client.sent[-1][1])
        self.assertIn("Status: cancelled", client.edited[-1][2])
        self.assertIn("Latest update: Stopped by /stop.", client.edited[-1][2])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_stop_reports_when_no_task_is_running(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/stop"))

        self.assertEqual(client.sent[0][1], "No running task to stop.")

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_tasks_command_shows_queue_and_history(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/task first queued work"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/task second queued work"))
            bot.handle_update(_message_update(update_id=3, chat_id=42, text="/task cancel 2"))
            bot.handle_update(_message_update(update_id=4, chat_id=42, text="/tasks"))

        reply = client.sent[-1][1]
        self.assertIn("Running: none", reply)
        self.assertIn("#1 [pending] first queued work", reply)
        self.assertIn("#2 [cancelled] second queued work", reply)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_tasks_command_shows_history_pr_url(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None
            complete_task(job.id, root, result="Opened pull request: https://github.com/our-ark/enoch/pull/3")
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/tasks"))

        reply = client.sent[-1][1]
        self.assertIn("#1 [completed] add queued work", reply)
        self.assertIn("PR: https://github.com/our-ark/enoch/pull/3", reply)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(_message_update(chat_id=42, text="/task add queued work"))
            job = begin_next_task(root)
            assert job is not None

            def run_task(*_args, **_kwargs):
                bot._send_step_update(42, "Editing files.")
                return "Done with the task."

            with patch.object(bot, "_run_direct_work", side_effect=run_task) as run_direct_work:
                bot._run_task_job(job)

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
        log_conversation_turn.assert_called()

    def test_task_worker_records_learning_only_for_skill_changes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(_message_update(chat_id=42, text="/task add skill"))
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

    @patch("enoch.telegram.bot.respond", return_value="Build the reminders feature discussed earlier.")
    def test_task_context_snapshot_resolver_uses_chat_session(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/backlog p0 do it later"))
            status = backlog_status(root)

        self.resolve_task_context_snapshot.assert_called_once_with(42, "do it later")
        self.assertEqual(status.pending_count, 1)
        self.assertEqual(status.pending[0].priority, "p0")
        self.assertEqual(status.pending[0].context, "Use the backlog context snapshot.")
        self.assertIn("Backlog #1 [p0] saved.", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_backlog_command_defaults_to_p1(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/backlog do it eventually"))
            status = backlog_status(root)

        self.assertEqual(status.pending[0].priority, "p1")

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/backlog do it"))
            status = backlog_status(root)

        self.assertEqual(status.pending, ())
        self.assertIn("Which deferred item should Enoch save?", client.sent[0][1])

    def test_backlog_idle_promotion_moves_item_to_task_queue(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)
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

        self.assertEqual(job.text, "background cleanup")
        self.assertEqual(job.context, "Use saved context.")
        self.assertEqual(queue.pending[0].id, job.id)
        self.assertEqual(backlog.pending, ())
        self.assertEqual(backlog.history[-1].id, backlog_item.id)
        self.assertEqual(backlog.history[-1].promoted_task_id, job.id)
        self.assertIn("Promoted from backlog #1 (p2).", client.sent[0][1])

    def test_backlog_idle_promotion_waits_for_empty_task_queue(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)
            add_backlog_item(42, "background cleanup", root, priority="p0")
            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/task active work"))

            job = bot._promote_next_backlog_if_idle()
            queue = task_queue_status(root)
            backlog = backlog_status(root)

        self.assertIsNone(job)
        self.assertEqual(queue.pending_count, 1)
        self.assertEqual(backlog.pending_count, 1)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_backlog_command_can_remove_and_reprioritize(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/backlog p2 first"))
                bot.handle_update(_message_update(update_id=2, chat_id=42, text="/backlog priority 1 p0"))
                bot.handle_update(_message_update(update_id=3, chat_id=42, text="/backlog remove 1"))
            status = backlog_status(root)

        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].status, "removed")
        self.assertIn("priority is now p0", client.sent[1][1])
        self.assertIn("Removed backlog #1.", client.sent[2][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_backlog_cancel_points_to_remove_without_adding_item(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/backlog cancel 1"))
            status = backlog_status(root)

        self.assertEqual(status.pending_count, 0)
        self.assertIn("Use /backlog remove <id>", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_backlog_command_can_manually_promote(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/backlog p1 first"))
                bot.handle_update(_message_update(update_id=2, chat_id=42, text="/backlog promote 1"))
            queue = task_queue_status(root)
            backlog = backlog_status(root)

        self.assertEqual(queue.pending_count, 1)
        self.assertEqual(backlog.history[-1].promoted_task_id, queue.pending[0].id)
        self.assertIn("Promoted backlog #1 to task #1.", client.sent[2][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_backlog_report_lists_pending_and_history(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_maybe_start_task_worker"):
                bot.handle_update(_message_update(chat_id=42, text="/backlog p0 first"))
                bot.handle_update(_message_update(update_id=2, chat_id=42, text="/backlog"))

        self.assertIn("Backlog:", client.sent[1][1])
        self.assertIn("#1 [p0 pending] first", client.sent[1][1])

    def test_evolve_reports_top_candidate_from_backlog(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve"))

        reply = client.sent[0][1]
        self.assertIn("Evolve:", reply)
        self.assertIn("Mode: co-evolve", reply)
        self.assertIn("- backlog: 1", reply)
        self.assertIn("backlog-1 [candidate backlog] improve Telegram work UX", reply)
        self.assertIn("wait for human approval", reply)

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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/feedback"))

        reply = client.sent[0][1]
        self.assertIn("Feedback:", reply)
        self.assertIn("[complaint x1] The evolve proposal is broken.", reply)

    def test_experience_lists_candidates_from_work_history(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_cron_job(42, "review recurring recovery", 3600, root)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/experience"))

        reply = client.sent[0][1]
        self.assertIn("Experience:", reply)
        self.assertIn("cron-1 [candidate experience] Review recurring workflow #1", reply)

    def test_propose_ranks_all_sources_without_selecting_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/propose"))
            candidates = load_evolve_candidates(root)

        reply = client.sent[0][1]
        self.assertIn("Enoch proposes:", reply)
        self.assertIn("Ranked 1 candidate(s) from the six evolve sources.", reply)
        self.assertIn("backlog-1 [candidate backlog] improve Telegram work UX", reply)
        self.assertIn("Approve with /evolve run backlog-1.", reply)
        self.assertEqual(candidates[0].status, "candidate")

    def test_evolve_can_list_select_and_reject_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "low value cleanup", root, priority="p2")
            add_backlog_item(42, "important Telegram recovery", root, priority="p0")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve candidates"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/evolve select backlog-1"))
            bot.handle_update(_message_update(update_id=3, chat_id=42, text="/evolve"))
            bot.handle_update(_message_update(update_id=4, chat_id=42, text="/evolve reject 1"))
            bot.handle_update(_message_update(update_id=5, chat_id=42, text="/evolve candidates"))
            bot.handle_update(_message_update(update_id=6, chat_id=42, text="/evolve candidates all"))

        self.assertIn("Evolve candidates:", client.sent[0][1])
        self.assertIn("backlog-1 [candidate backlog] low value cleanup", client.sent[0][1])
        self.assertIn("Selected evolve candidate.", client.sent[1][1])
        self.assertIn("backlog-1 [selected backlog] low value cleanup", client.sent[1][1])
        self.assertIn("backlog-1 [selected backlog] low value cleanup", client.sent[2][1])
        self.assertIn("Rejected evolve candidate.", client.sent[3][1])
        self.assertNotIn("backlog-1", client.sent[4][1])
        self.assertIn("backlog-1 [rejected backlog] low value cleanup", client.sent[5][1])

    def test_evolve_run_queues_candidate_as_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship evolve run", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve run backlog-1"))
            queued = task_queue_status(root)

        self.assertEqual(queued.pending_count, 1)
        self.assertIn("Queued evolve candidate backlog-1 as task #1.", client.sent[0][1])
        self.assertIn("backlog-1 [running backlog] ship evolve run", client.sent[0][1])
        self.assertIn("Evolve selected candidate backlog-1", queued.pending[0].text)
        self.assertEqual(queued.pending[0].context_source, "evolve-run")
        self.assertIn("Scheduled evolve candidate context:", queued.pending[0].context)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_evolve_run_candidate_is_marked_done_after_task_completion(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship evolve completion", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve run backlog-1"))
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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_evolve_run_candidate_is_marked_failed_after_task_failure(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "ship failing evolve", root, priority="p1")
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve run backlog-1"))
            job = begin_next_task(root)
            assert job is not None
            with patch.object(bot, "_run_direct_work", return_value="Enoch could not publish this edit as a pull request: GH007"):
                bot._run_task_job(job)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)
            report = evolve_report(root)

        self.assertNotIn("backlog-1", {candidate.id for candidate in visible})
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses["backlog-1"], "failed")
        self.assertIn("experience", report.counts_by_source)
        self.assertIn("Final status: failed", client.sent[-1][1])

    def test_evolve_can_set_theme_and_mode(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve theme improve recovery"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/evolve mode disabled"))

        self.assertIn("Theme: improve recovery", client.sent[0][1])
        self.assertIn("Mode: disabled", client.sent[1][1])
        self.assertIn("Candidate counts:\n- none", client.sent[1][1])

    @patch(
        "enoch.telegram.bot.respond",
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve theme auditable evolution"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/evolve brainstorm"))

        self.assertIn("Added 1 theme-guided brainstorming candidate", client.sent[1][1])
        self.assertIn("brainstorming: 1", client.sent[1][1])
        self.assertIn("Current evolution theme: auditable evolution", respond.call_args.args[1])

    @patch("enoch.telegram.bot.respond")
    def test_evolve_brainstorm_requires_theme(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve brainstorm"))

        self.assertIn("Set a theme", client.sent[0][1])
        respond.assert_not_called()

    @patch("enoch.telegram.bot.explore_peer_skills", return_value=(MagicMock(), MagicMock()))
    def test_evolve_explore_discovers_peer_skills(self, explore_peer_skills: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve explore enosh"))

        explore_peer_skills.assert_called_once_with("enosh", root)
        self.assertIn("Added 2 peer-learning candidate(s) from enosh", client.sent[0][1])

    def test_evolve_keeps_direct_mode_aliases(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve auto-evovle"))

        self.assertIn("Mode: auto-evolve", client.sent[0][1])

    def test_evolve_can_set_and_disable_schedule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule every 1d"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/evolve schedule off"))

        self.assertIn("Schedule: every 1d; next", client.sent[0][1])
        self.assertIn("Schedule: off", client.sent[1][1])

    def test_evolve_once_a_day_alias_sets_daily_interval(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule once a day"))

        self.assertIn("Schedule: every 1d; next", client.sent[0][1])

    def test_evolve_once_a_day_alias_accepts_time(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule once a day at 09:30"))

        self.assertIn("Schedule: daily 09:30; next", client.sent[0][1])

    def test_evolve_schedule_interprets_quoted_text(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text='/evolve schedule "once a day"'))

        self.assertIn("Schedule: every 1d; next", client.sent[0][1])

    def test_evolve_schedule_interprets_raw_cron_text(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule 30 9 * * *"))

        self.assertIn("Schedule: cron 30 9 * * *; next", client.sent[0][1])

    def test_evolve_schedule_interprets_natural_daily_time(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule every day at 09:30"))

        self.assertIn("Schedule: daily 09:30; next", client.sent[0][1])

    def test_evolve_can_set_daily_schedule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule daily 09:30"))

        self.assertIn("Schedule: daily 09:30; next", client.sent[0][1])

    def test_evolve_can_set_cron_schedule(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/evolve schedule cron 30 9 * * *"))

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
            bot = EnochTelegramBot(load_identity(), root, client)

            job = bot._run_due_evolve_schedule()

        self.assertIsNone(job)
        self.assertIn("Scheduled evolve check", client.sent[0][1])
        self.assertIn("backlog-1 [candidate backlog] improve Telegram work UX", client.sent[0][1])

    def test_due_evolve_schedule_auto_mode_queues_top_candidate(self) -> None:
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
            bot = EnochTelegramBot(load_identity(), root, client)

            job = bot._run_due_evolve_schedule()
            queued = task_queue_status(root)
            report = evolve_report(root)

        self.assertIsNotNone(job)
        self.assertEqual(queued.pending_count, 1)
        self.assertIn("Evolve selected candidate backlog-1", queued.pending[0].text)
        self.assertEqual(queued.pending[0].context_source, "evolve-scheduler")
        self.assertEqual(report.top_candidate.status, "running")
        self.assertIn("Latest update: Scheduled by evolve auto-evolve.", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/cron every 10m run scheduled cleanup"))
            status = cron_status(root)

        self.resolve_task_context_snapshot.assert_called_once_with(42, "run scheduled cleanup")
        self.assertEqual(status.active_count, 1)
        self.assertEqual(status.active[0].text, "run scheduled cleanup")
        self.assertEqual(status.active[0].interval_seconds, 600)
        self.assertEqual(status.active[0].context, "Use the scheduled context snapshot.")
        self.assertIn("Cron #1 scheduled every 10m.", client.sent[0][1])
        self.assertIn("Next run:", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_cron_command_can_cancel_and_report(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/cron every 1h scheduled cleanup"))
            bot.handle_update(_message_update(update_id=2, chat_id=42, text="/cron"))
            bot.handle_update(_message_update(update_id=3, chat_id=42, text="/cron cancel 1"))
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
            bot = EnochTelegramBot(load_identity(), root, client)
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

        self.assertEqual([job.id for job in jobs], [1])
        self.assertEqual(jobs_again, ())
        self.assertEqual(queued.pending[0].text, "scheduled cleanup")
        self.assertEqual(queued.pending[0].context, "Use saved cron context.")
        self.assertEqual(cron_after.active[0].id, cron.id)
        self.assertEqual(cron_after.active[0].last_task_id, queued.pending[0].id)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Latest update: Scheduled by cron #1.", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_task_worker_marks_publish_failure_failed(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(_message_update(chat_id=42, text="/task add queued work"))
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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_task_worker_does_not_rerun_task_with_recorded_pr(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(_message_update(chat_id=42, text="/task add queued work"))
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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
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

            EnochTelegramBot(load_identity(), root, FakeTelegramClient(allowed_chat_id=42))
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.pending, ())
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].result, result)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
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

            EnochTelegramBot(load_identity(), root, FakeTelegramClient(allowed_chat_id=42))
            status = task_queue_status(root)

        self.assertIsNone(status.running)
        self.assertEqual(status.history[-1].id, queued.id)
        self.assertEqual(status.history[-1].status, "failed")
        self.assertEqual(status.history[-1].result, result)

    @patch("enoch.telegram.bot.create_pull_request")
    @patch("enoch.telegram.bot.push_current_branch")
    @patch("enoch.telegram.bot.switch_branch")
    @patch("enoch.telegram.bot.ensure_clean_worktree")
    @patch("enoch.telegram.bot.current_branch", return_value="main")
    @patch("enoch.telegram.bot.act_in_session")
    @patch("enoch.telegram.bot.respond")
    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(
                _message_update(
                    chat_id=42,
                    text="/task publish existing local branch `enoch/existing` as a PR against `main`",
                )
            )
            job = begin_next_task(root)
            assert job is not None

            bot._run_task_job(job)

        respond.assert_not_called()
        act_in_session.assert_not_called()
        push_current_branch.assert_called_once_with(root=root)
        create_pull_request.assert_called_once_with(root=root)
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("https://github.com/our-ark/enoch/pull/3", client.edited[-1][2])
        log_conversation_turn.assert_called()

    @patch("enoch.telegram.bot.skills_command", return_value="Lucy skills:")
    def test_skills_command_shows_declared_skills(self, skills_command: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/skills lucy"))

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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mission"))
            bot.handle_update(_message_update(chat_id=42, text="/mission Build calm agent networks"))
            bot.handle_update(_message_update(chat_id=42, text="/self"))
            identity_text = identity_file.read_text(encoding="utf-8")

            self.assertIn("Enoch mission:", client.sent[0][1])
            self.assertIn("Update with /mission <new mission>.", client.sent[0][1])
            self.assertIn("Enoch mission updated.", client.sent[1][1])
            self.assertIn("Mission: Build calm agent networks", client.sent[1][1])
            self.assertIn("Mission: Build calm agent networks", client.sent[2][1])
            self.assertIn("Build calm agent networks", identity_text)

    @patch("enoch.telegram.bot.respond", return_value="Memory is managed internally now.")
    def test_memory_command_is_not_user_facing(self, respond: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/memory"))

        respond.assert_called_once()
        self.assertIn("Memory is managed internally now.", client.sent[0][1])

    @patch("enoch.telegram.bot.respond", return_value="Teaching is automatic now.")
    def test_teach_command_is_not_user_facing(self, respond: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/teach natural agency"))

        respond.assert_called_once()
        self.assertIn("Teaching is automatic now.", client.sent[0][1])
        self.assertNotIn("Input tokens:", client.sent[0][1])

    @patch("enoch.telegram.bot.record_peer_learning_observation")
    @patch("enoch.telegram.bot.learn_skill_prompt", return_value="learn prompt")
    @patch("enoch.telegram.bot.respond", return_value="This skill does not fit Enoch yet.")
    def test_learn_skill_uses_read_only_session(
        self,
        respond: MagicMock,
        learn_skill_prompt: MagicMock,
        record_peer_learning_observation: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/learn teach from lucy"))

        learn_skill_prompt.assert_called_once_with("/learn teach from lucy", root=ROOT)
        record_peer_learning_observation.assert_called_once()
        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("learn prompt", respond.call_args.args[1])
        self.assertIn("Enoch wrapper instructions:", respond.call_args.args[1])
        self.assertIn("This skill does not fit Enoch yet.", client.sent[0][1])

    @patch(
        "enoch.telegram.bot.respond",
        return_value=f"I can adapt it.\n\n{EDIT_REQUEST_START}\nadapt skill\n{EDIT_REQUEST_END}",
    )
    @patch("enoch.telegram.bot.learn_skill_prompt", return_value="learn prompt")
    @patch("enoch.telegram.bot.act_in_session")
    def test_learn_skill_requires_action_mode_for_edit(
        self,
        act_in_session: MagicMock,
        _learn_skill_prompt: MagicMock,
        _respond: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mode chat"))
            bot.handle_update(_message_update(chat_id=42, text="/learn teach from lucy"))

        self.assertIn("Enoch mode: chat.", client.sent[0][1])
        self.assertIn("will not change code", client.sent[1][1])
        act_in_session.assert_not_called()

    @patch("enoch.telegram.bot.model_summary", return_value="AI model: gpt-5-codex")
    def test_self_reports_identity_without_runtime_status(self, model_summary: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/self"))

        model_summary.assert_not_called()
        self.assertIn("I am Enoch.", client.sent[0][1])
        self.assertIn("Role: descendant_agent", client.sent[0][1])
        self.assertIn("Generation: 3", client.sent[0][1])
        self.assertIn("Ancestor: Seth", client.sent[0][1])
        self.assertIn("Mission:", client.sent[0][1])
        self.assertNotIn("Enoch status:", client.sent[0][1])
        self.assertNotIn("Local state:", client.sent[0][1])
        self.assertNotIn("AI model:", client.sent[0][1])

    @patch("enoch.telegram.bot.model_summary", return_value="AI model: gpt-5-codex")
    def test_status_reports_runtime_state_and_chat_lock(self, model_summary: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/status"))

        model_summary.assert_called_once_with(ROOT)
        self.assertIn("Enoch status:", client.sent[0][1])
        self.assertIn("AI model: gpt-5-codex", client.sent[0][1])
        self.assertIn("Local state:", client.sent[0][1])
        self.assertIn("Telegram chat lock: 42", client.sent[0][1])
        self.assertIn("action mode: full-access", client.sent[0][1])
        self.assertNotIn("I am Enoch.", client.sent[0][1])
        self.assertNotIn("Ancestor:", client.sent[0][1])
        self.assertNotIn("Mission:", client.sent[0][1])
        self.assertNotIn("current evolution", client.sent[0][1])

    @patch("enoch.telegram.bot.model_summary", return_value="AI model: gpt-5-codex")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/self"))

        self.assertIn("Ancestor: Adam", client.sent[0][1])
        self.assertNotIn("Ancestor: Lucy", client.sent[0][1])
        model_summary.assert_not_called()

    def test_status_includes_setup_hint_without_chat_lock(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/status"))

        self.assertIn("Telegram chat lock: not set", client.sent[0][1])
        self.assertIn("bin/enoch setup-chat 42", client.sent[0][1])
        self.assertIn("bin/enoch-daemon restart", client.sent[0][1])

    @patch("enoch.telegram.bot.respond", return_value="Thinking config is managed locally.")
    def test_thinking_is_no_longer_a_telegram_command(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/thinking high"))

        respond.assert_called_once()
        self.assertNotIn("reasoning_effort", read_section("codex", root))
        self.assertIn("Thinking config is managed locally.", client.sent[0][1])

    def test_ancestors_reports_missing_parent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/ancestors"))

        self.assertIn("no direct parent configured", client.sent[0][1])
        self.assertIn(".agent/lineage.yaml", client.sent[0][1])
        self.assertIn("Ancestor commands:", client.sent[0][1])
        self.assertIn("/inherit", client.sent[0][1])

    @patch("enoch.telegram.bot.resolve_lineage")
    def test_ancestors_reports_resolution_warnings(self, resolve_lineage: MagicMock) -> None:
        resolve_lineage.return_value = LineageResolution(
            ancestors=(AncestorLink(name="Enoch", repo="our-ark/enoch", branch="main", depth=1),),
            warnings=("Could not read parent lineage from our-ark/enoch@main: private repo",),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/ancestors"))

        resolve_lineage.assert_called_once_with(root)
        self.assertIn("Warnings:", client.sent[0][1])
        self.assertIn("private repo", client.sent[0][1])
        self.assertIn("Ancestor commands:", client.sent[0][1])

    @patch("enoch.telegram.bot.inherit_command", return_value="Direct parent inheritance checked.\nour-ark/enoch#32")
    def test_inherit_uses_shared_command(self, inherit_command: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/inherit"))

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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/inherit inspect our-ark/enoch#32"))

        self.assertIn("Status: pending", client.sent[0][1])
        self.assertIn("src/enoch/telegram/bot.py", client.sent[0][1])
        self.sync_session_activity.assert_called_once()
        self.assertIn("Ancestor change context", self.sync_session_activity.call_args.args[3])

    def test_inherit_candidate_id_uses_lineage_adoption_flow(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        with patch.object(bot, "_adopt_lineage_candidate", return_value="adopted change") as adopt:
            bot.handle_update(_message_update(chat_id=42, text="/inherit our-ark/enoch#32"))

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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/inherit our-ark/enoch#32"))

        self.assertIn("could not find direct-parent change", client.sent[0][1])

    def test_unknown_ancestors_subcommand_returns_usage(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/ancestors unknown our-ark/enoch#32"))

        self.assertIn("Ancestor commands:", client.sent[0][1])

    def test_default_action_sandbox_is_danger_full_access(self) -> None:
        with TemporaryDirectory() as temp:
            self.assertEqual(_action_sandbox(Path(temp)), "danger-full-access")

    def test_action_sandbox_follows_mode(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mode chat"))
            locked_sandbox = _action_sandbox(root)
            bot.handle_update(_message_update(chat_id=42, text="/mode work"))
            unlocked_sandbox = _action_sandbox(root)

            self.assertEqual(locked_sandbox, "read-only")
            self.assertEqual(unlocked_sandbox, "danger-full-access")

    def test_mode_sets_action_mode(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mode chat"))
            bot.handle_update(_message_update(chat_id=42, text="/mode work"))

            self.assertIn("Enoch mode: chat.", client.sent[0][1])
            self.assertIn("Enoch mode: work.", client.sent[1][1])
            self.assertEqual(_action_mode(root), "full-access")

    def test_mode_requires_locked_chat(self) -> None:
        with TemporaryDirectory() as temp:
            client = FakeTelegramClient(allowed_chat_id=None)
            bot = EnochTelegramBot(load_identity(), Path(temp), client)

            bot.handle_update(_message_update(chat_id=42, text="/mode chat"))

            self.assertIn("locked to one chat", client.sent[0][1])

    def test_mode_without_argument_shows_status(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mode"))

            self.assertIn("Enoch mode: work.", client.sent[0][1])
            self.assertIn("Use /mode chat or /mode work.", client.sent[0][1])

    @patch(
        "enoch.telegram.bot.respond",
        return_value=(
            "I can make that change."
            f"\n\n{EDIT_REQUEST_START}\nadd reminders\n{EDIT_REQUEST_END}"
        ),
    )
    @patch("enoch.telegram.bot.act_in_session")
    def test_conversation_only_mode_blocks_requested_edit(
        self,
        act_in_session: MagicMock,
        respond: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mode chat"))
            bot.handle_update(_message_update(chat_id=42, text="add reminders"))

            self.assertIn("Enoch mode: chat.", client.sent[0][1])
            self.assertIn("I can make that change.", client.sent[1][1])
            self.assertNotIn("will not change code", client.sent[1][1])
            respond.assert_called_once()
            act_in_session.assert_not_called()

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.act_in_session", return_value="Implemented requested edit.")
    @patch("enoch.telegram.bot._worktree_snapshot", side_effect=["clean", "clean"])
    @patch("enoch.telegram.bot.create_branch")
    @patch("enoch.telegram.bot._ensure_local_main_current")
    @patch("enoch.telegram.bot.ensure_clean_worktree")
    @patch("enoch.telegram.bot.current_branch", return_value="main")
    @patch("enoch.telegram.bot.time.time", return_value=789)
    def test_locked_mode_uses_read_only_sandbox_if_action_runner_is_reached(
        self,
        _time: MagicMock,
        current_branch: MagicMock,
        ensure_clean_worktree: MagicMock,
        ensure_local_main_current: MagicMock,
        create_branch: MagicMock,
        _snapshot: MagicMock,
        act_in_session: MagicMock,
        log_conversation_turn: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="/mode chat"))
            result = bot._run_direct_work(42, "add reminders", session_key="telegram:42")

        self.assertEqual(current_branch.call_count, 2)
        current_branch.assert_any_call(root)
        ensure_clean_worktree.assert_called_once_with(root)
        ensure_local_main_current.assert_called_once_with(root)
        create_branch.assert_called_once_with("enoch/789-add-reminders", root)
        act_in_session.assert_called_once()
        self.assertEqual(act_in_session.call_args.kwargs["sandbox"], "read-only")
        self.assertEqual(act_in_session.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("Work request:", act_in_session.call_args.args[1])
        self.assertNotIn("Edit phase:", act_in_session.call_args.args[1])
        log_conversation_turn.assert_called()
        update_memory.assert_any_call(root)
        self.assertIn("Implemented requested edit.", result)

    def test_progress_update_uses_minutes_at_default_interval(self) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot._send_progress(42, 60, "workspace-write")

        self.assertEqual(client.sent[0][1], "Enoch is still working after 1 minute(s): editing her code body.")

    @patch("enoch.command_surface.run_git")
    def test_checktree_reports_clean_worktree(self, run_git: MagicMock) -> None:
        run_git.return_value = MagicMock(returncode=0, stdout="", stderr="")

        self.assertEqual(_checktree(ROOT), "Worktree status: clean")
        run_git.assert_called_once_with(["status", "--porcelain"], ROOT)

    @patch("enoch.command_surface.run_git")
    def test_checktree_reports_dirty_worktree(self, run_git: MagicMock) -> None:
        run_git.return_value = MagicMock(returncode=0, stdout=" M README.md\n?? scratch.txt", stderr="")

        result = _checktree(ROOT)

        self.assertIn("Worktree status: dirty", result)
        self.assertIn("M README.md", result)
        self.assertIn("?? scratch.txt", result)

    @patch("enoch.command_surface.run_git")
    def test_checktree_reports_unknown_when_git_fails(self, run_git: MagicMock) -> None:
        run_git.return_value = MagicMock(returncode=1, stdout="", stderr="not a git repository")

        result = _checktree(ROOT)

        self.assertIn("Worktree status: unknown", result)
        self.assertIn("not a git repository", result)

    @patch("enoch.telegram.bot.run_immune_system")
    def test_doctor_runs_health_checks(self, run_immune_system: MagicMock) -> None:
        run_immune_system.return_value = MagicMock(
            passed=True,
            command="python3 -m unittest discover -s tests",
            diagnosis=MagicMock(summary="All tests passed.", suggested_action="Keep going.", failing_tests=[]),
        )
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/doctor"))

        run_immune_system.assert_called_once_with(ROOT)
        self.assertIn("Doctor passed.", client.sent[0][1])
        self.assertIn("All tests passed.", client.sent[0][1])

    @patch("enoch.telegram.bot._schedule_daemon_restart")
    @patch("enoch.telegram.bot.update_from_main")
    def test_update_uses_shared_updater_and_restarts_when_safe(
        self,
        update_from_main: MagicMock,
        schedule_restart: MagicMock,
    ) -> None:
        update_from_main.return_value.message = "Enoch pulled latest main and doctor passed.\n\nRestarting now."
        update_from_main.return_value.direct_action_result = "Updating 1111111..2222222"
        update_from_main.return_value.restart_required = True
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/update"))

        update_from_main.assert_called_once_with(ROOT)
        schedule_restart.assert_called_once_with(ROOT)
        self.assertIn("Enoch pulled latest main and doctor passed.", client.sent[0][1])
        self.assertIn("Restarting now.", client.sent[0][1])

    @patch("enoch.telegram.bot._schedule_daemon_restart")
    @patch("enoch.telegram.bot.update_from_main")
    def test_update_does_not_restart_when_shared_result_does_not_request_it(
        self,
        update_from_main: MagicMock,
        schedule_restart: MagicMock,
    ) -> None:
        update_from_main.return_value.message = "Enoch is already up to date.\nAlready up to date."
        update_from_main.return_value.direct_action_result = "Already up to date."
        update_from_main.return_value.restart_required = False
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/update"))

        update_from_main.assert_called_once_with(ROOT)
        schedule_restart.assert_not_called()
        self.assertIn("Enoch is already up to date.", client.sent[0][1])

    @patch("enoch.telegram.bot.update_from_main")
    def test_update_requires_locked_chat(self, update_from_main: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/update"))

        update_from_main.assert_not_called()
        self.assertIn("locked to one chat", client.sent[0][1])

    @patch("enoch.telegram.bot._schedule_daemon_stop")
    def test_shutdown_schedules_daemon_stop_after_reply(self, schedule_stop: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        with self.assertRaisesRegex(ShutdownRequested, "Telegram /shutdown"):
            bot.handle_update(_message_update(chat_id=42, text="/shutdown"))

        schedule_stop.assert_called_once_with(ROOT)
        self.assertIn("Enoch is closing.", client.sent[0][1])

    @patch("enoch.telegram.bot._schedule_daemon_stop")
    def test_shutdown_requires_locked_chat(self, schedule_stop: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/shutdown"))

        schedule_stop.assert_not_called()
        self.assertIn("locked to one chat", client.sent[0][1])

    @patch("enoch.telegram.bot._schedule_daemon_restart")
    def test_restart_schedules_daemon_restart_after_reply(self, schedule_restart: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/restart"))

        schedule_restart.assert_called_once_with(ROOT)
        self.assertIn("Enoch is restarting.", client.sent[0][1])

    @patch("enoch.telegram.bot._schedule_daemon_restart")
    def test_restart_requires_locked_chat(self, schedule_restart: MagicMock) -> None:
        client = FakeTelegramClient(allowed_chat_id=None)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(chat_id=42, text="/restart"))

        schedule_restart.assert_not_called()
        self.assertIn("locked to one chat", client.sent[0][1])

    @patch("enoch.telegram.bot.respond", return_value="Let's think through reminders first.")
    def test_natural_feature_request_uses_read_only_wrapper(self, respond: MagicMock) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="我想让 Enoch 支持 reminders"))

        respond.assert_called_once()
        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("Enoch wrapper instructions:", respond.call_args.args[1])
        self.assertIn("/do", respond.call_args.args[1])
        self.assertIn("/task", respond.call_args.args[1])
        self.sync_session_activity.assert_not_called()
        self.assertIn("Let's think through reminders first.", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.respond")
    @patch("enoch.telegram.bot.act_in_session")
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
            bot = EnochTelegramBot(load_identity(), root, client)

        bot.handle_update(_message_update(chat_id=42, text="make the README clearer"))

        self.assertEqual(respond.call_args.kwargs["session_key"], "telegram:42")
        self.assertIn("/do", respond.call_args.args[1])
        self.assertIn("/task", respond.call_args.args[1])
        self.assertEqual(client.edited, [])
        self.assertIn("I can make that change.", client.sent[0][1])
        self.assertNotIn(EDIT_REQUEST_START, client.sent[0][1])
        act_in_session.assert_not_called()
        self.sync_session_activity.assert_not_called()

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.respond", return_value="I can talk that through first.")
    @patch("enoch.telegram.bot.prepare_local_publish")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(chat_id=42, text="commit then open a PR for it"))

        respond.assert_called_once()
        prepare_local_publish.assert_not_called()
        self.assertIn("I can talk that through first.", client.sent[0][1])

    @patch("enoch.telegram.bot.create_pull_request")
    @patch("enoch.telegram.bot.push_current_branch")
    @patch("enoch.telegram.bot.switch_branch")
    @patch("enoch.telegram.bot.ensure_clean_worktree")
    @patch("enoch.telegram.bot.current_branch", return_value="main")
    @patch("enoch.telegram.bot.act_in_session")
    @patch("enoch.telegram.bot.respond")
    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with start_worker:
                bot.handle_update(
                    _message_update(
                        chat_id=42,
                        text="/do publish existing local branch `enoch/existing` as a PR against `main`",
                    )
                )
            self.assertEqual(len(started), 1)
            bot._run_direct_task_job(started[0][0], session_key=started[0][1])

        respond.assert_not_called()
        act_in_session.assert_not_called()
        current_branch.assert_called_once_with(root)
        ensure_clean_worktree.assert_called_once_with(root)
        switch_branch.assert_any_call("enoch/existing", root)
        switch_branch.assert_any_call("main", root)
        push_current_branch.assert_called_once_with(root=root)
        create_pull_request.assert_called_once_with(root=root)
        self.assertEqual(len(client.sent), 2)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertIn("https://github.com/our-ark/enoch/pull/3", client.edited[-1][2])

    @patch("enoch.telegram.bot.create_pull_request")
    @patch("enoch.telegram.bot.push_current_branch")
    @patch("enoch.telegram.bot.prepare_local_publish")
    @patch("enoch.telegram.bot.run_immune_system")
    @patch("enoch.telegram.bot.delete_branch")
    @patch("enoch.telegram.bot.switch_branch")
    @patch("enoch.telegram.bot.create_branch")
    @patch("enoch.telegram.bot._ensure_local_main_current")
    @patch("enoch.telegram.bot.ensure_clean_worktree")
    @patch("enoch.telegram.bot.current_branch", side_effect=["main", "enoch/readme", "enoch/readme"])
    @patch("enoch.telegram.bot.changed_files", return_value=["README.md"])
    @patch("enoch.telegram.bot._worktree_snapshot", side_effect=["clean", "changed"])
    @patch("enoch.telegram.bot.act_in_session", return_value="Updated README.")
    @patch("enoch.telegram.bot.respond")
    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot.time.time", return_value=789)
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
        _ensure_local_main_current: MagicMock,
        _create_branch: MagicMock,
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
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with start_worker:
                bot.handle_update(_message_update(chat_id=42, text="/do Update README directly."))
            self.assertEqual(len(started), 1)
            bot._run_direct_task_job(started[0][0], session_key=started[0][1])
            status = task_queue_status(root)

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
        self.assertEqual(len(client.sent), 2)
        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Use the earlier README scope decision.", client.sent[0][1])
        self.assertIn("Status: completed", client.edited[-1][2])
        self.assertEqual(status.history[-1].status, "completed")
        self.assertEqual(status.history[-1].context, "Use the earlier README scope decision.")
        self.assertIn("https://github.com/our-ark/enoch/pull/2", status.history[-1].result)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_do_starts_worker_and_keeps_telegram_responsive(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work") as run_direct_work:
                with start_worker:
                    bot.handle_update(_message_update(chat_id=42, text="/do long edit"))
                    run_direct_work.assert_not_called()
                    self.assertEqual(len(started), 1)
                    status = task_queue_status(root)
                    self.assertIsNotNone(status.running)

                    bot.handle_update(_message_update(update_id=2, chat_id=42, text="/status"))

        self.assertIn("Task #1", client.sent[0][1])
        self.assertIn("Tasks:", client.sent[-1][1])
        self.assertIn("- running: #1 long edit", client.sent[-1][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            with patch.object(bot, "_run_direct_work") as run_direct_work:
                bot.handle_update(_message_update(chat_id=42, text="/do do it"))
            status = task_queue_status(root)

        run_direct_work.assert_not_called()
        self.assertIsNone(status.running)
        self.assertEqual(status.history, ())
        self.assertIn("Which change should Enoch make?", client.sent[0][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_do_marks_work_failure_status_failed(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(
                bot,
                "_run_direct_work",
                return_value="Enoch could not complete the requested work yet: usage limit",
            ):
                with start_worker:
                    bot.handle_update(_message_update(chat_id=42, text="/do Update README directly."))
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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_do_command_includes_replied_message_context(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work", return_value="Done.") as run_direct_work:
                with start_worker:
                    bot.handle_update(
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

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_do_uses_new_action_session_for_each_direct_task(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work", return_value="Done.") as run_direct_work:
                with start_worker:
                    bot.handle_update(_message_update(chat_id=42, text="/do first"))
                    self.assertEqual(len(started), 1)
                    bot._run_direct_task_job(started[0][0], session_key=started[0][1])

                    bot.handle_update(_message_update(update_id=2, chat_id=42, text="/do second"))
                    self.assertEqual(len(started), 2)
                    bot._run_direct_task_job(started[1][0], session_key=started[1][1])

        self.assertEqual(
            [call.kwargs["session_key"] for call in run_direct_work.call_args_list],
            ["telegram:42:do:1", "telegram:42:do:2"],
        )

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(_message_update(chat_id=42, text="/task existing work"))
            job = begin_next_task(root)
            assert job is not None
            already_queued = enqueue_task(42, "already queued work", root)

            with patch.object(bot, "_run_direct_work") as run_direct_work:
                bot.handle_update(_message_update(update_id=2, chat_id=42, text="/do new work"))
            status = task_queue_status(root)

        run_direct_work.assert_not_called()
        self.assertEqual([pending.id for pending in status.pending], [3, already_queued.id])
        self.assertEqual(status.pending[0].context, "Use the latest README decision.")
        self.assertEqual(status.pending[0].context_source, "chat-snapshot")
        self.assertIn("Task #3", client.sent[-1][1])
        self.assertIn("Status: queued", client.sent[-1][1])
        self.assertIn("Queued next after running task #1.", client.sent[-1][1])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_finished_do_checks_for_queued_work(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with patch.object(bot, "_run_direct_work", return_value="Done."):
                with start_worker:
                    bot.handle_update(_message_update(chat_id=42, text="/do first"))
                self.assertEqual(len(started), 1)
                enqueue_task(42, "queued next", root)
                with patch.object(bot, "_maybe_start_task_worker") as maybe_start:
                    bot._run_direct_task_job(started[0][0], session_key=started[0][1])

        maybe_start.assert_called_once()

    @patch("enoch.telegram.bot.create_pull_request")
    @patch("enoch.telegram.bot.push_current_branch")
    @patch("enoch.telegram.bot.prepare_local_publish")
    @patch("enoch.telegram.bot.delete_branch")
    @patch("enoch.telegram.bot.switch_branch")
    @patch("enoch.telegram.bot.ensure_clean_worktree")
    @patch("enoch.telegram.bot.current_branch", return_value="enoch/readme")
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
            bot = EnochTelegramBot(load_identity(), root, client)
            bot.handle_update(_message_update(chat_id=42, text="/task Update README directly."))
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
        self.assertIn("https://github.com/our-ark/enoch/pull/2", status.running.result)
        self.assertIn("Enoch opened a pull request.", status.running.result)

    @patch("enoch.telegram.bot.close_pull_request")
    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
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
            bot = EnochTelegramBot(load_identity(), root, client)

            started, start_worker = self._capture_direct_work_worker(bot)
            with start_worker:
                bot.handle_update(_message_update(chat_id=42, text="/do 保留 #1，关闭重复的 #2 和 #3"))
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
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.run_once()

        self.assertEqual(bot.offset, 11)
        self.assertIn("Telegram chat id: 42", client.sent[0][1])

    def test_run_once_persists_offset_for_restart(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FakeTelegramClient()
            client.updates = [_message_update(update_id=10, chat_id=42, text="/status")]
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.run_once()
            restarted_bot = EnochTelegramBot(load_identity(), root, client)
            restarted_bot.run_once()

            self.assertEqual(bot.offset, 11)
            self.assertEqual(restarted_bot.offset, 11)
            self.assertEqual(client.offsets, [None, 11])
            self.assertEqual(len(client.sent), 1)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_final_send_failure_still_records_and_advances_offset(
        self,
        log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            client = FailingTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(update_id=10, chat_id=42, text="/status"))

        self.assertEqual(bot.offset, 11)
        log_conversation_turn.assert_called_once()
        self.assertIn("Telegram send failed", log_conversation_turn.call_args.kwargs["reply"])

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    def test_read_ack_failure_is_logged_without_blocking_reply(
        self,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        client = FailingAckTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot.handle_update(_message_update(update_id=10, chat_id=42, text="/status"))

        self.log_system_event.assert_any_call(
            "telegram_read_ack_failed",
            root=ROOT,
            status="failed",
            details={
                "chat_id": 42,
                "message_id": 1010,
                "error": "reaction failed",
            },
        )
        self.assertIn("Enoch status:", client.sent[0][1])

    def test_progress_send_failure_does_not_abort_action(self) -> None:
        client = FailingTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)

        bot._send_progress(42, 60, "danger-full-access")

        self.assertEqual(client.sent, [])

    @patch("builtins.print")
    @patch("enoch.telegram.bot.time.sleep")
    def test_run_forever_continues_after_polling_error(
        self,
        sleep: MagicMock,
        _print: MagicMock,
    ) -> None:
        client = FakeTelegramClient(allowed_chat_id=42)
        bot = EnochTelegramBot(load_identity(), ROOT, client)
        bot.run_once = MagicMock(side_effect=[OSError("network down"), KeyboardInterrupt])

        with self.assertRaises(KeyboardInterrupt):
            bot.run_forever()

        sleep.assert_called_once_with(5)

    @patch("enoch.update_tools.run_git")
    def test_ensure_local_main_current_pulls_stale_main(self, run_git: MagicMock) -> None:
        run_git.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="aaa111", stderr=""),
            MagicMock(returncode=0, stdout="bbb222", stderr=""),
            MagicMock(returncode=0, stdout="main", stderr=""),
            MagicMock(returncode=0, stdout="Updating aaa111..bbb222", stderr=""),
        ]

        telegram._ensure_local_main_current(ROOT)

        run_git.assert_any_call(["pull", "--ff-only", "origin", "main"], ROOT)

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_system_event", side_effect=OSError("disk full"))
    def test_direct_action_system_log_failure_does_not_abort_workflow(
        self,
        _log_system_event: MagicMock,
        update_memory: MagicMock,
    ) -> None:
        telegram._record_direct_action("push branch", "Pushed branch.", ROOT)

        update_memory.assert_not_called()

    @patch("enoch.telegram.bot.ensure_long_term_memory")
    @patch("enoch.telegram.bot.log_conversation_turn")
    @patch("enoch.telegram.bot._schedule_daemon_restart")
    @patch("enoch.telegram.bot.update_from_main")
    def test_restart_triggering_update_saves_offset_before_restart(
        self,
        update_from_main: MagicMock,
        schedule_restart: MagicMock,
        _log_conversation_turn: MagicMock,
        _update_memory: MagicMock,
    ) -> None:
        update_from_main.return_value.message = "Enoch pulled latest main and doctor passed."
        update_from_main.return_value.direct_action_result = "Updating 1111111..2222222"
        update_from_main.return_value.restart_required = True
        with TemporaryDirectory() as temp:
            root = Path(temp)

            def assert_offset_saved(restart_root: Path) -> None:
                self.assertEqual(restart_root, root)
                offset_file = root / ".enoch" / "telegram_offset.json"
                self.assertEqual(json.loads(offset_file.read_text(encoding="utf-8"))["offset"], 11)

            schedule_restart.side_effect = assert_offset_saved
            client = FakeTelegramClient(allowed_chat_id=42)
            bot = EnochTelegramBot(load_identity(), root, client)

            bot.handle_update(_message_update(update_id=10, chat_id=42, text="/update"))

        schedule_restart.assert_called_once_with(root)
        self.assertEqual(bot.offset, 11)


class FakeTelegramClient:
    def __init__(self, allowed_chat_id=None) -> None:
        self.config = TelegramConfig(token="token", allowed_chat_id=allowed_chat_id)
        self.acks = []
        self.sent = []
        self.edited = []
        self.updates = []
        self.offsets = []

    def get_updates(self, offset=None):
        self.offsets.append(offset)
        if offset is None:
            return self.updates
        return [update for update in self.updates if int(update["update_id"]) >= offset]

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return 2000 + len(self.sent)

    def edit_message(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))

    def send_read_ack(self, chat_id, message_id):
        self.acks.append((chat_id, message_id, READ_ACK_EMOJI))


class FailingTelegramClient(FakeTelegramClient):
    def send_message(self, chat_id, text):
        raise TelegramError("send failed")


class FailingAckTelegramClient(FakeTelegramClient):
    def send_read_ack(self, chat_id, message_id):
        raise TelegramError("reaction failed")


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


def _save_test_offset(offset: int, root: Path) -> None:
    if root == ROOT:
        return
    _real_save_telegram_offset(offset, root)


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
