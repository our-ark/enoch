from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch.app.core import EnochApplication
from enoch.app.pending_merges import (
    BoardReview,
    PendingMerge,
    WatchedReview,
    save_ci_watches,
    save_pending_merges,
)
from enoch.app.task_board import (
    TaskBoardState,
    load_task_board_state,
    render_task_board,
    save_task_board_state,
    task_board_path,
)
from enoch.backlog import (
    BacklogItem,
    add_backlog_item,
    backlog_path,
    promote_next_backlog_item,
    remove_backlog_item,
)
from enoch.commands import core_command, help_message
from enoch.identity import load_identity
from enoch.providers import ChatEvent, ChatProviderError, MessagePinProvider
from enoch.tasks.queue import TaskJob, TaskQueueStatus


UPDATED_AT = datetime(2026, 11, 24, 14, 5)


def _job(task_id: int, text: str, **changes) -> TaskJob:
    return TaskJob(
        id=task_id,
        chat_id=42,
        text=text,
        created_at="2026-11-24T13:00:00+00:00",
        **changes,
    )


def _backlog_item(item_id: int, text: str, *, priority: str = "p1") -> BacklogItem:
    return BacklogItem(
        id=item_id,
        chat_id=42,
        text=text,
        priority=priority,
        created_at="2026-11-24T13:00:00+00:00",
    )


def _reviews(*pairs: tuple[int, str]) -> tuple[BoardReview, ...]:
    return tuple(BoardReview(number, status) for number, status in pairs)


def _watch(number: int, status: str, *, authoritative: bool | None = True) -> WatchedReview:
    url = f"https://github.com/our-ark/enoch/pull/{number}"
    return WatchedReview(
        number=number,
        url=url,
        repository="our-ark/enoch",
        review_id=url,
        chat_id=42,
        task_id=number,
        started_at="2026-11-24T13:00:00Z",
        settled=True,
        board_status=status,
        authoritative=authoritative,
    )


def _pending_merge(number: int) -> PendingMerge:
    url = f"https://github.com/our-ark/enoch/pull/{number}"
    return PendingMerge(
        number=number,
        url=url,
        repository="our-ark/enoch",
        review_id=url,
        authorized_head_sha=f"head-{number}",
        allowed_head_shas=(f"head-{number}",),
        requested_at="2026-11-24T13:00:00Z",
        chat_id=42,
        waiting_for="waiting for CI",
    )


def _status(
    *,
    running: TaskJob | None = None,
    pending: tuple[TaskJob, ...] = (),
    paused: tuple[TaskJob, ...] = (),
    history: tuple[TaskJob, ...] = (),
) -> TaskQueueStatus:
    return TaskQueueStatus(
        pending_count=len(pending),
        paused_count=len(paused),
        running=running,
        pending=pending,
        paused=paused,
        history=history,
    )


class TaskBoardRenderTests(unittest.TestCase):
    def test_empty_board_lists_every_section_as_none(self) -> None:
        rendered = render_task_board(_status(), updated_at=UPDATED_AT)

        self.assertEqual(
            rendered.text,
            "\n".join(
                [
                    "Task board",
                    "",
                    "Running:",
                    "- none",
                    "",
                    "Queued:",
                    "- none",
                    "",
                    "Paused:",
                    "- none",
                    "",
                    "Updated 14:05",
                ]
            ),
        )

    def test_renders_running_queued_and_paused_sections(self) -> None:
        started = datetime(2026, 11, 24, 13, 2, tzinfo=timezone.utc)
        status = _status(
            running=_job(7, "Implement the board", status="running", started_at=started.isoformat()),
            pending=(_job(8, "Write docs"), _job(9, "Retry it", parent_task_id=3)),
            paused=(_job(5, "Paused work", status="paused"),),
        )

        text = render_task_board(status, updated_at=UPDATED_AT).text

        started_local = started.astimezone().strftime("%H:%M")
        self.assertIn(
            f"Running:\n- #7 Implement the board (started {started_local})",
            text,
        )
        self.assertIn("Queued:\n- #8 Write docs\n- #9 Retry it (retry of #3)", text)
        self.assertIn("Paused:\n- #5 Paused work", text)
        self.assertNotIn("[pending]", text)
        self.assertNotIn("elapsed", text.lower())
        self.assertNotIn(" min", text)
        self.assertTrue(text.endswith("\n\nUpdated 14:05"))

    def test_board_omits_recent_reviews_and_failures_from_history(self) -> None:
        history = (
            _job(
                1,
                "Reviewed change",
                status="completed",
                review_urls=("https://example.test/pull/1",),
            ),
            _job(2, "Broken attempt", status="failed", result="Reason here"),
            _job(3, "Timed out", status="failed", failure_code="timeout"),
        )
        status = _status(pending=(_job(4, "Next up"),), history=history)

        text = render_task_board(status, updated_at=UPDATED_AT).text

        self.assertNotIn("Recent reviews", text)
        self.assertNotIn("Recent failures", text)
        self.assertNotIn("https://example.test/pull/1", text)
        self.assertNotIn("Reviewed change", text)
        self.assertNotIn("Broken attempt", text)
        self.assertNotIn("Reason", text)
        self.assertIn("Queued:\n- #4 Next up", text)
        self.assertEqual(
            [line for line in text.splitlines() if line.endswith(":")],
            ["Running:", "Queued:", "Paused:"],
        )

    def test_long_board_is_truncated_within_the_limit(self) -> None:
        pending = tuple(_job(index, "queued request " * 10) for index in range(1, 80))
        history = tuple(
            _job(
                100 + index,
                "reviewed " * 10,
                status="completed",
                review_urls=(f"https://example.test/pull/{index}",),
            )
            for index in range(5)
        )

        rendered = render_task_board(
            _status(pending=pending, history=history),
            updated_at=UPDATED_AT,
            queue_command="!queue",
            max_chars=1200,
        )

        self.assertLessEqual(len(rendered.text), 1200)
        self.assertIn("more (see !queue)", rendered.text)
        self.assertIn("Running:", rendered.text)
        self.assertIn("Paused:", rendered.text)
        self.assertNotIn("Recent reviews", rendered.text)
        self.assertTrue(rendered.text.endswith("Updated 14:05"))

    def test_digest_ignores_the_updated_footer(self) -> None:
        status = _status(pending=(_job(1, "Work"),))

        first = render_task_board(status, updated_at=UPDATED_AT)
        later = render_task_board(status, updated_at=datetime(2026, 11, 24, 18, 30))

        self.assertNotEqual(first.text, later.text)
        self.assertEqual(first.digest, later.digest)

    def test_open_prs_line_orders_problems_then_ready_then_ci(self) -> None:
        reviews = _reviews(
            (101, "ci"),
            (98, "ready"),
            (97, "failed"),
            (99, "blocked"),
            (96, "failed"),
        )

        text = render_task_board(
            _status(),
            updated_at=UPDATED_AT,
            merging=("#91 (waiting for CI)",),
            open_reviews=reviews,
            backlog=(_backlog_item(8, "Later work"),),
        ).text

        line = "Open PRs: #96 ❌ CI failed · #97 ❌ CI failed · #99 ⚠️ blocked · #98 ✅ ready · #101 ⏳ CI"
        self.assertIn(
            f"Merging:\n- #91 (waiting for CI)\n\n{line}\nUp next: 1 in backlog",
            text,
        )
        self.assertEqual([row for row in text.splitlines() if row.startswith("Open PRs")], [line])
        self.assertTrue(text.endswith("\n\nUpdated 14:05"))

    def test_open_prs_line_shows_four_and_counts_the_rest(self) -> None:
        reviews = _reviews(*((number, "ready") for number in range(90, 97)))

        text = render_task_board(_status(), updated_at=UPDATED_AT, open_reviews=reviews).text

        self.assertIn(
            "Open PRs: #90 ✅ ready · #91 ✅ ready · #92 ✅ ready · #93 ✅ ready · +3 more",
            text,
        )
        self.assertNotIn("#94", text)

        five = render_task_board(
            _status(), updated_at=UPDATED_AT, open_reviews=reviews[:5]
        ).text
        self.assertIn("#94 ✅ ready", five)
        self.assertNotIn("more", five)

    def test_board_has_no_open_prs_line_when_there_are_none(self) -> None:
        empty = render_task_board(_status(), updated_at=UPDATED_AT, open_reviews=())
        without = render_task_board(_status(), updated_at=UPDATED_AT)

        self.assertNotIn("Open PRs", empty.text)
        self.assertEqual(empty.text, without.text)
        self.assertEqual(empty.digest, without.digest)

    def test_open_prs_digest_changes_only_with_the_set_or_a_marker(self) -> None:
        reviews = _reviews((96, "ci"), (97, "ready"))

        first = render_task_board(_status(), updated_at=UPDATED_AT, open_reviews=reviews)
        same = render_task_board(
            _status(),
            updated_at=datetime(2026, 11, 24, 18, 30),
            open_reviews=tuple(reversed(reviews)),
        )
        status_changed = render_task_board(
            _status(), updated_at=UPDATED_AT, open_reviews=_reviews((96, "ready"), (97, "ready"))
        )
        set_changed = render_task_board(
            _status(), updated_at=UPDATED_AT, open_reviews=reviews[:1]
        )

        self.assertEqual(first.digest, same.digest)
        self.assertEqual(len({first.digest, status_changed.digest, set_changed.digest}), 3)
        line = next(row for row in first.text.splitlines() if row.startswith("Open PRs"))
        self.assertNotRegex(line, r"\d{1,2}:\d{2}|\bmin\b|ago|elapsed")

    def test_open_prs_line_gives_way_before_any_task_item(self) -> None:
        status = _status(
            running=_job(7, "Implement the board " * 3, status="running"),
            pending=tuple(_job(index, f"Queued request {index} " * 3) for index in range(8, 12)),
            paused=(_job(5, "Paused work " * 3, status="paused"),),
        )
        reviews = _reviews(*((number, "failed") for number in range(90, 99)))
        bare = render_task_board(status, updated_at=UPDATED_AT).text

        for slack in (0, 5, 20, 40):
            with self.subTest(slack=slack):
                text = render_task_board(
                    status,
                    updated_at=UPDATED_AT,
                    open_reviews=reviews,
                    max_chars=len(bare) + slack,
                ).text

                self.assertLessEqual(len(text), len(bare) + slack)
                self.assertNotIn("more (see", text)
                for task_id in (7, 8, 9, 10, 11, 5):
                    self.assertIn(f"- #{task_id} ", text)
                self.assertTrue(text.endswith("Updated 14:05"))
        roomy = render_task_board(
            status, updated_at=UPDATED_AT, open_reviews=reviews, max_chars=len(bare) + 20
        ).text
        self.assertIn("Open PRs: 9", roomy)

    def test_board_has_no_backlog_line_when_backlog_is_empty(self) -> None:
        with_empty = render_task_board(_status(), updated_at=UPDATED_AT, backlog=())
        without = render_task_board(_status(), updated_at=UPDATED_AT)

        self.assertNotIn("Up next", with_empty.text)
        self.assertNotIn("backlog", with_empty.text.lower())
        self.assertEqual(with_empty.text, without.text)
        self.assertEqual(with_empty.digest, without.digest)

    def test_backlog_line_counts_items_in_promotion_order(self) -> None:
        backlog = (
            _backlog_item(8, "Later p1 work", priority="p1"),
            _backlog_item(9, "Low priority", priority="p2"),
            _backlog_item(10, "Urgent first", priority="p0"),
            _backlog_item(11, "Urgent second", priority="p0"),
        )

        text = render_task_board(_status(), updated_at=UPDATED_AT, backlog=backlog).text

        self.assertIn(
            "\n\nUp next: 4 in backlog (#10 [p0] Urgent first, #11 [p0] Urgent second, +2 more)"
            "\n\nUpdated 14:05",
            text,
        )

    def test_backlog_line_follows_paused_and_merging(self) -> None:
        backlog = (_backlog_item(3, "Only item"),)

        plain = render_task_board(_status(), updated_at=UPDATED_AT, backlog=backlog).text
        merging = render_task_board(
            _status(),
            updated_at=UPDATED_AT,
            merging=("PR #5",),
            backlog=backlog,
        ).text

        self.assertIn("Paused:\n- none\n\nUp next: 1 in backlog (#3 [p1] Only item)\n\n", plain)
        self.assertIn("Merging:\n- PR #5\n\nUp next: 1 in backlog (#3 [p1] Only item)\n\n", merging)
        self.assertEqual(len([line for line in plain.splitlines() if "Up next" in line]), 1)

    def test_backlog_labels_use_the_first_sentence_and_are_clipped(self) -> None:
        backlog = (
            _backlog_item(1, "Fix two small bugs. Then explain the details at length."),
            _backlog_item(
                2,
                "Give task workers a persistent test environment\nso they can run e2e checks",
            ),
            _backlog_item(3, "Third"),
        )

        text = render_task_board(_status(), updated_at=UPDATED_AT, backlog=backlog).text
        line = next(line for line in text.splitlines() if line.startswith("Up next:"))

        self.assertEqual(
            line,
            "Up next: 3 in backlog (#1 [p1] Fix two small bugs, "
            "#2 [p1] Give task workers a persistent test…, +1 more)",
        )
        self.assertNotIn("Then explain", text)
        self.assertNotIn("Third", text)

    def test_backlog_line_has_no_more_suffix_when_everything_is_shown(self) -> None:
        backlog = (_backlog_item(1, "One"), _backlog_item(2, "Two"))

        text = render_task_board(_status(), updated_at=UPDATED_AT, backlog=backlog).text

        self.assertIn("Up next: 2 in backlog (#1 [p1] One, #2 [p1] Two)\n", text)
        self.assertNotIn("more", text)

    def test_digest_changes_when_the_backlog_changes(self) -> None:
        status = _status(pending=(_job(1, "Work"),))
        one = (_backlog_item(1, "First"),)
        two = (*one, _backlog_item(2, "Second"))
        reprioritized = (_backlog_item(1, "First", priority="p0"),)

        digests = {
            render_task_board(status, updated_at=UPDATED_AT, backlog=backlog).digest
            for backlog in ((), one, two, reprioritized)
        }
        repeat = render_task_board(
            status,
            updated_at=datetime(2026, 11, 24, 18, 30),
            backlog=two,
        ).digest

        self.assertEqual(len(digests), 4)
        self.assertIn(repeat, digests)

    def test_truncation_shortens_backlog_before_hiding_tasks(self) -> None:
        status = _status(
            running=_job(1, "Running work"),
            pending=(_job(2, "Queued work"), _job(3, "More queued work")),
            paused=(_job(4, "Paused work", status="paused"),),
        )
        backlog = tuple(
            _backlog_item(index, "A rather long backlog request description " * 3)
            for index in range(10, 16)
        )
        full = render_task_board(status, updated_at=UPDATED_AT, backlog=backlog).text
        bare = render_task_board(status, updated_at=UPDATED_AT).text

        rendered = render_task_board(
            status,
            updated_at=UPDATED_AT,
            backlog=backlog,
            max_chars=len(bare) + len("\n\nUp next: 6 in backlog"),
        )

        self.assertGreater(len(full), len(rendered.text))
        self.assertLessEqual(len(rendered.text), len(bare) + len("\n\nUp next: 6 in backlog"))
        for item in ("#1 Running work", "#2 Queued work", "#3 More queued work", "#4 Paused work"):
            self.assertIn(item, rendered.text)
        self.assertNotIn("more (see", rendered.text)
        self.assertIn("\n\nUp next: 6 in backlog\n\nUpdated 14:05", rendered.text)

    def test_truncation_with_backlog_keeps_every_task_section(self) -> None:
        pending = tuple(_job(index, "queued request " * 10) for index in range(1, 80))
        paused = (_job(90, "Paused work", status="paused"),)
        backlog = tuple(_backlog_item(index, "backlog request " * 5) for index in range(1, 9))

        rendered = render_task_board(
            _status(running=_job(100, "Running work"), pending=pending, paused=paused),
            updated_at=UPDATED_AT,
            max_chars=1200,
            backlog=backlog,
        )

        self.assertLessEqual(len(rendered.text), 1200)
        self.assertIn("Running:\n- #100 Running work", rendered.text)
        self.assertIn("Queued:\n- #1 ", rendered.text)
        self.assertIn("more (see /queue)", rendered.text)
        self.assertIn("Paused:\n- #90 Paused work", rendered.text)
        self.assertIn("Up next: 8 in backlog", rendered.text)
        self.assertTrue(rendered.text.endswith("Updated 14:05"))

    def test_state_round_trips_in_private_state(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(load_task_board_state(root), TaskBoardState())
            save_task_board_state(
                TaskBoardState(enabled=True, chat_id=42, message_id=9, last_digest="abc"),
                root,
            )
            raw = json.loads(task_board_path(root).read_text(encoding="utf-8"))
            loaded = load_task_board_state(root)

        self.assertEqual(
            raw,
            {
                "schema_version": 1,
                "enabled": True,
                "chat_id": 42,
                "message_id": 9,
                "last_digest": "abc",
            },
        )
        self.assertEqual(loaded, TaskBoardState(True, 42, 9, "abc"))
        self.assertTrue(loaded.active)


class TaskBoardCommandTests(unittest.TestCase):
    def test_board_is_registered_with_help(self) -> None:
        command = core_command("board")

        self.assertIsNotNone(command)
        assert command is not None
        usage = command.usage_message("/")
        self.assertIn("/board off", usage)
        self.assertIn("/board - post a task board", usage)
        self.assertIn("/board [off]", help_message())

    def test_board_is_off_by_default(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                app._refresh_task_board()

            self.assertFalse(task_board_path(root).exists())

        self.assertEqual(chat.sent, [])
        self.assertEqual(chat.edits, [])

    def test_board_command_posts_through_inbox_and_falls_back_without_pinning(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                app.handle_event(
                    ChatEvent(cursor=5, conversation_id=42, message_id=3, text="/board")
                )
            state = load_task_board_state(root)

        self.assertNotIsInstance(chat, MessagePinProvider)
        self.assertEqual(len(chat.sent), 2)
        board, reply = chat.sent
        self.assertTrue(board[1].startswith("Enoch task board\n"))
        self.assertIn("cannot pin messages", reply[1])
        self.assertIn("/board off", reply[1])
        self.assertEqual(state, TaskBoardState(True, 42, 1, state.last_digest))
        self.assertTrue(state.last_digest)

    def test_board_command_pins_when_supported(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _PinningChat()
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                reply = app._task_board_command("")

        self.assertEqual(chat.pins, [(42, 1)])
        self.assertIn("posted and pinned", reply)

    def test_pin_failure_still_tracks_the_board(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _PinningChat(pin_error="not enough rights")
            app = EnochApplication(load_identity(), root, chat)

            with patch.object(app.workflow, "inspect", return_value=_status()):
                reply = app._task_board_command("")
            state = load_task_board_state(root)

        self.assertIn("could not be pinned", reply)
        self.assertIn("not enough rights", reply)
        self.assertTrue(state.active)

    def test_board_requires_a_locked_conversation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _LegacyChat()
            chat.allowed_conversation_id = None
            app = EnochApplication(load_identity(), root, chat)

            reply = app._task_board_command("")

            self.assertFalse(task_board_path(root).exists())

        self.assertIn("locked to one conversation", reply)
        self.assertEqual(chat.sent, [])


class TaskBoardRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _LegacyChat()
        self.app = EnochApplication(load_identity(), self.root, self.chat)
        self.status = _status()
        inspect = patch.object(self.app.workflow, "inspect", side_effect=lambda: self.status)
        inspect.start()
        self.addCleanup(inspect.stop)
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)

    def test_backlog_changes_edit_the_board(self) -> None:
        self.app._task_board_command("")
        self.assertNotIn("Up next", self.chat.sent[0][1])

        first = add_backlog_item(42, "Set up a test environment", self.root)
        self.app._refresh_task_board()
        add_backlog_item(42, "Fix the parser", self.root, priority="p0")
        self.app._refresh_task_board()
        promote_next_backlog_item(self.root, promoted_task_id=50)
        self.app._refresh_task_board()
        remove_backlog_item(first.id, self.root)
        self.app._refresh_task_board()

        texts = [edit[2] for edit in self.chat.edits]
        self.assertEqual(len(texts), 4)
        self.assertIn("Up next: 1 in backlog (#1 [p1] Set up a test environment)", texts[0])
        self.assertIn(
            "Up next: 2 in backlog (#2 [p0] Fix the parser, #1 [p1] Set up a test environment)",
            texts[1],
        )
        self.assertIn("Up next: 1 in backlog (#1 [p1] Set up a test environment)", texts[2])
        self.assertNotIn("Up next", texts[3])

    def test_unchanged_backlog_is_not_edited(self) -> None:
        add_backlog_item(42, "Waiting work", self.root)
        self.app._task_board_command("")
        self.assertIn("Up next: 1 in backlog", self.chat.sent[0][1])

        for _ in range(3):
            self.app._refresh_task_board()

        self.assertEqual(self.chat.edits, [])

    def test_unreadable_backlog_only_hides_the_backlog_line(self) -> None:
        self.app._task_board_command("")
        backlog_path(self.root).write_text("{not json", encoding="utf-8")
        self.status = _status(pending=(_job(4, "New work"),))

        with patch("enoch.app.core._record_system_event") as record:
            self.app._refresh_task_board()

        record.assert_not_called()
        self.assertEqual(len(self.chat.edits), 1)
        self.assertIn("Queued:\n- #4 New work", self.chat.edits[0][2])
        self.assertNotIn("Up next", self.chat.edits[0][2])

    def test_unchanged_content_is_not_edited(self) -> None:
        self.app._task_board_command("")

        for _ in range(3):
            self.app._refresh_task_board()

        self.assertEqual(self.chat.edits, [])
        self.assertEqual(len(self.chat.sent), 1)

    def test_changed_content_is_edited_exactly_once(self) -> None:
        self.app._task_board_command("")
        self.status = _status(pending=(_job(4, "New work"),))

        for _ in range(3):
            self.app._refresh_task_board()

        self.assertEqual(len(self.chat.edits), 1)
        chat_id, message_id, text = self.chat.edits[0]
        self.assertEqual((chat_id, message_id), (42, 1))
        self.assertIn("Queued:\n- #4 New work", text)
        self.assertEqual(len(self.chat.sent), 1)

    def test_board_returning_to_earlier_content_is_edited_again(self) -> None:
        self.app._task_board_command("")
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()
        self.status = _status()
        self.app._refresh_task_board()

        self.assertEqual(len(self.chat.edits), 2)
        self.assertIn("Queued:\n- none", self.chat.edits[1][2])
        self.assertTrue(load_task_board_state(self.root).active)

    def test_failed_edit_marks_board_stale_without_reposting(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = "Bad Request: message to edit not found"
        self.status = _status(pending=(_job(4, "New work"),))

        self.app._refresh_task_board()
        self.status = _status(pending=(_job(4, "New work"), _job(5, "More")))
        self.app._refresh_task_board()
        self.app._refresh_task_board()
        state = load_task_board_state(self.root)

        self.assertEqual(self.chat.edit_attempts, 1)
        self.assertEqual(len(self.chat.sent), 1)
        self.assertFalse(state.enabled)
        self.assertFalse(state.active)
        self.assertEqual(state.message_id, 1)

    def test_board_off_stops_updates(self) -> None:
        self.app._task_board_command("")

        reply = self.app._task_board_command("off")
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()

        self.assertIn("updates stopped", reply)
        self.assertEqual(self.chat.edits, [])
        self.assertFalse(load_task_board_state(self.root).enabled)
        self.assertIn("already off", self.app._task_board_command("off"))

    def test_running_board_again_replaces_the_tracked_message(self) -> None:
        self.app._task_board_command("")
        reply = self.app._task_board_command("")
        self.status = _status(pending=(_job(4, "New work"),))

        self.app._refresh_task_board()

        self.assertIn("previous board is no longer updated", reply)
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual([edit[1] for edit in self.chat.edits], [2])
        self.assertEqual(load_task_board_state(self.root).message_id, 2)

    def test_board_failure_never_breaks_the_cycle(self) -> None:
        save_task_board_state(
            TaskBoardState(enabled=True, chat_id=42, message_id=1, last_digest="old"),
            self.root,
        )
        task_board_path(self.root).write_text("{not json", encoding="utf-8")

        with patch("enoch.app.core._record_system_event") as record:
            self.app._refresh_task_board()
            self.app._refresh_task_board()

        self.assertEqual(
            [call.args[0] for call in record.call_args_list],
            ["task_board_refresh_failed"],
        )
        self.assertEqual(self.chat.edits, [])

    def test_open_prs_line_reads_watched_reviews_and_skips_merging_ones(self) -> None:
        save_ci_watches(
            (
                _watch(96, "ready"),
                _watch(97, "failed"),
                _watch(98, "ci"),
                _watch(99, "ready", authoritative=False),
                _watch(100, "ready", authoritative=None),
            ),
            self.root,
        )
        save_pending_merges((_pending_merge(98),), self.root)

        text = self.app._render_task_board().text

        self.assertIn("Merging:\n- #98 (waiting for CI)", text)
        self.assertIn("Open PRs: #97 ❌ CI failed · #96 ✅ ready\n", text + "\n")
        self.assertEqual(text.count("#98"), 1)
        self.assertNotIn("#99", text)
        self.assertNotIn("#100", text)

    def test_open_prs_line_edits_the_board_only_when_it_changes(self) -> None:
        save_ci_watches((_watch(96, "ci"),), self.root)
        self.app._task_board_command("")
        self.assertIn("Open PRs: #96 ⏳ CI", self.chat.sent[0][1])

        for _ in range(3):
            self.app._refresh_task_board()
        self.assertEqual(self.chat.edits, [])

        save_ci_watches((_watch(96, "ready"),), self.root)
        self.app._refresh_task_board()
        self.app._refresh_task_board()
        save_ci_watches((), self.root)
        self.app._refresh_task_board()

        texts = [edit[2] for edit in self.chat.edits]
        self.assertEqual(len(texts), 2)
        self.assertIn("Open PRs: #96 ✅ ready", texts[0])
        self.assertNotIn("Open PRs", texts[1])

    def test_board_refresh_never_calls_the_forge(self) -> None:
        save_ci_watches((_watch(96, "ready"), _watch(97, "failed")), self.root)
        save_pending_merges((_pending_merge(91),), self.root)
        self.app._task_board_command("")
        forge = MagicMock()
        self.app.review = forge
        save_ci_watches((_watch(96, "ci"), _watch(97, "failed")), self.root)

        self.app._refresh_task_board()

        self.assertEqual(forge.mock_calls, [])
        self.assertEqual(len(self.chat.edits), 1)
        self.assertIn("Open PRs: #97 ❌ CI failed · #96 ⏳ CI", self.chat.edits[0][2])

    def test_run_once_refreshes_the_board(self) -> None:
        with patch.object(self.app, "_refresh_task_board") as refresh:
            self.app.run_once()

        refresh.assert_called_once_with()

    def test_unknown_argument_shows_usage(self) -> None:
        reply = self.app._task_board_command("sideways")

        self.assertIn("Task board commands:", reply)
        self.assertEqual(self.chat.sent, [])


class _LegacyChat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.edit_attempts = 0
        self.edit_error = ""

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edit_attempts += 1
        if self.edit_error:
            raise ChatProviderError(self.edit_error)
        self.edits.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _PinningChat(_LegacyChat):
    def __init__(self, pin_error: str = "") -> None:
        super().__init__()
        self.pins: list[tuple[int, int]] = []
        self.pin_error = pin_error

    def pin_message(self, conversation_id, message_id):
        if self.pin_error:
            raise ChatProviderError(self.pin_error)
        self.pins.append((conversation_id, message_id))


if __name__ == "__main__":
    unittest.main()
