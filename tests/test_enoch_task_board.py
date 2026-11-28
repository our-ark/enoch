from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch


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
    EDIT_FAILED,
    EDIT_GONE,
    EDIT_TRANSIENT,
    EDIT_UNCHANGED,
    BoardEditFailure,
    TaskBoardState,
    classify_board_edit_error,
    count_task_board_message,
    load_task_board_state,
    render_quota_line,
    render_task_board,
    save_task_board_state,
    task_board_path,
    update_task_board_state,
)
from enoch.backlog import (
    BacklogItem,
    add_backlog_item,
    backlog_path,
    promote_next_backlog_item,
    remove_backlog_item,
)
from enoch.app.notifications import (
    NotificationDeliveryService,
    notification_record,
    notifications_path,
)
from enoch.commands import config_status, core_command, help_message
from enoch.identity import load_identity
from enoch.providers import (
    ChatEvent,
    ChatProviderError,
    MessageDeleteProvider,
    MessagePinProvider,
    MessageUnpinProvider,
    SilentMessageProvider,
)
from enoch.tasks.config import board_repost_settings
from enoch.tasks.queue import TaskJob, TaskQueueStatus


UPDATED_AT = datetime(2026, 11, 24, 14, 5)
QUOTA_NOW = datetime(2026, 11, 24, 23, 35, tzinfo=timezone.utc)
FIVE_HOUR_RESET = QUOTA_NOW + timedelta(hours=4, minutes=55)
SEVEN_DAY_RESET = QUOTA_NOW + timedelta(days=1, hours=4, minutes=25)


def _clock(moment: datetime) -> str:
    return moment.astimezone().strftime("%H:%M")


def _quota(
    five_hour_used: float | None = 13,
    seven_day_used: float | None = 36,
    *,
    five_hour_reset: datetime | None = FIVE_HOUR_RESET,
    seven_day_reset: datetime | None = SEVEN_DAY_RESET,
    extra: tuple[dict, ...] = (),
) -> dict:
    def window(label: str, used: float | None, reset: datetime | None) -> dict:
        return {
            "label": label,
            "used_percent": used,
            "resets_at": reset.isoformat() if reset is not None else None,
        }

    return {
        "plan": "pro",
        "source": "Claude Code get_usage (experimental)",
        "windows": [
            window("5h", five_hour_used, five_hour_reset),
            window("7d", seven_day_used, seven_day_reset),
            *extra,
        ],
    }


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
                "messages_since_board": 0,
                "last_reposted_at": "",
                "stopped_by_human": False,
                "stale_since": "",
                "edit_failures": 0,
                "last_recovery_at": "",
            },
        )
        self.assertEqual(loaded, TaskBoardState(True, 42, 9, "abc"))
        self.assertTrue(loaded.active)


class QuotaLineTests(unittest.TestCase):
    def line(self, snapshot, provider: str = "claude") -> str:
        return render_quota_line(provider, snapshot, now=QUOTA_NOW)

    def test_line_shows_both_windows_and_the_5h_reset(self) -> None:
        self.assertEqual(
            self.line(_quota()),
            f"Quota (Claude): 5h 90% · 7d 60% left · 5h resets {_clock(FIVE_HOUR_RESET)}",
        )

    def test_remaining_is_rounded_to_the_nearest_ten_percent(self) -> None:
        cases = {13: 90, 17: 80, 15: 90, 25: 80, 0: 100, 76: 20, 100: 0}
        for used, shown in cases.items():
            with self.subTest(used=used):
                expected = shown if shown >= 20 else 100 - used
                self.assertIn(f"5h {expected}%", self.line(_quota(five_hour_used=used)))

    def test_exact_percent_is_shown_below_twenty(self) -> None:
        self.assertIn("5h 19%", self.line(_quota(five_hour_used=81)))
        self.assertIn("5h 7%", self.line(_quota(five_hour_used=93)))
        # Fractions round down so the board never overstates what is left.
        self.assertIn("5h 11%", self.line(_quota(five_hour_used=88.5)))
        self.assertIn("5h 0%", self.line(_quota(five_hour_used=100)))
        self.assertIn("5h 20%", self.line(_quota(five_hour_used=80)))

    def test_reset_time_is_local_hh_mm_for_the_window_closest_to_running_out(self) -> None:
        # The 7d window has less left but resets more than a day away, so
        # only its percentage is shown.
        self.assertTrue(self.line(_quota()).endswith(f"5h resets {_clock(FIVE_HOUR_RESET)}"))

        soon = QUOTA_NOW + timedelta(hours=20)
        line = self.line(_quota(seven_day_reset=soon))
        self.assertTrue(line.endswith(f"· 7d resets {_clock(soon)}"))
        self.assertEqual(line.count("resets"), 1)

        epoch = self.line(
            {"windows": [{"label": "5h", "used_percent": 50, "resets_at": FIVE_HOUR_RESET.timestamp()}]}
        )
        self.assertEqual(epoch, f"Quota (Claude): 5h 50% left · 5h resets {_clock(FIVE_HOUR_RESET)}")

    def test_line_has_no_reset_when_none_is_reported_soon(self) -> None:
        self.assertEqual(
            self.line(_quota(five_hour_reset=None)),
            "Quota (Claude): 5h 90% · 7d 60% left",
        )

    def test_expired_and_unknown_windows_are_skipped(self) -> None:
        expired = QUOTA_NOW - timedelta(minutes=1)
        self.assertEqual(
            self.line(_quota(five_hour_reset=expired)),
            "Quota (Claude): 7d 60% left",
        )
        self.assertEqual(
            self.line(_quota(five_hour_used=None)),
            "Quota (Claude): 7d 60% left",
        )

    def test_extra_windows_appear_only_when_nearly_exhausted(self) -> None:
        opus = {"label": "7d / Opus", "used_percent": 50, "resets_at": SEVEN_DAY_RESET.isoformat()}
        self.assertNotIn("Opus", self.line(_quota(extra=(opus,))))
        low = {**opus, "used_percent": 96}
        self.assertIn("· 7d / Opus 4% left", self.line(_quota(extra=(low,))))

    def test_shared_codex_bucket_prefix_is_dropped(self) -> None:
        snapshot = {
            "windows": [
                {"label": "codex / 5h", "used_percent": 40, "resets_at": None},
                {"label": "codex / 7d", "used_percent": 10, "resets_at": None},
            ]
        }
        self.assertEqual(self.line(snapshot, "codex"), "Quota (GPT / Codex): 5h 60% · 7d 90% left")

    def test_unavailable_or_failed_quota_yields_no_line(self) -> None:
        for snapshot in (
            None,
            {"error": "quota query failed. Check CLI login and version on this host."},
            {**_quota(), "error": "Plan quota is unavailable for this authentication mode."},
            {"windows": []},
            {"windows": "broken"},
            {"windows": [None, {"label": "5h"}, {"used_percent": "x"}]},
            {"windows": [{"label": "", "used_percent": 10}]},
            "not a mapping",
        ):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.line(snapshot), "")


class TaskBoardQuotaRenderTests(unittest.TestCase):
    def test_quota_line_sits_last_above_the_footer(self) -> None:
        quota = render_quota_line("claude", _quota(), now=QUOTA_NOW)
        text = render_task_board(
            _status(),
            updated_at=UPDATED_AT,
            merging=("PR #5",),
            backlog=(_backlog_item(3, "Only item"),),
            quota_line=quota,
        ).text

        self.assertTrue(
            text.endswith(
                "Merging:\n- PR #5\n\nUp next: 1 in backlog (#3 [p1] Only item)\n"
                f"{quota}\n\nUpdated 14:05"
            )
        )
        self.assertEqual(len([line for line in text.splitlines() if "Quota" in line]), 1)
        self.assertEqual(
            [line for line in text.splitlines() if line.endswith(":")],
            ["Running:", "Queued:", "Paused:", "Merging:"],
        )

    def test_open_prs_then_backlog_then_quota_share_the_trailing_block(self) -> None:
        quota = render_quota_line("claude", _quota(), now=QUOTA_NOW)
        status = _status(running=_job(7, "Implement the board", status="running"))
        backlog = (_backlog_item(3, "Only item"),)
        reviews = _reviews((96, "ready"), (97, "failed"))
        text = render_task_board(
            status,
            updated_at=UPDATED_AT,
            open_reviews=reviews,
            backlog=backlog,
            quota_line=quota,
        ).text

        self.assertTrue(
            text.endswith(
                "\n\nOpen PRs: #97 ❌ CI failed · #96 ✅ ready\n"
                "Up next: 1 in backlog (#3 [p1] Only item)\n"
                f"{quota}\n\nUpdated 14:05"
            )
        )
        # Tight boards shed the quota line before the open-review line, and
        # both before any task item.
        bare = render_task_board(status, updated_at=UPDATED_AT, backlog=backlog).text
        tight = render_task_board(
            status,
            updated_at=UPDATED_AT,
            open_reviews=reviews,
            backlog=backlog,
            quota_line=quota,
            max_chars=len(bare) + len("\nOpen PRs: 2") + 1,
        ).text
        self.assertNotIn("Quota", tight)
        self.assertIn("Open PRs: ", tight)
        self.assertIn("- #7 ", tight)

    def test_quota_line_without_backlog_follows_paused(self) -> None:
        quota = render_quota_line("claude", _quota(), now=QUOTA_NOW)
        text = render_task_board(_status(), updated_at=UPDATED_AT, quota_line=quota).text

        self.assertIn(f"Paused:\n- none\n\n{quota}\n\nUpdated 14:05", text)

    def test_no_quota_line_leaves_the_board_unchanged(self) -> None:
        empty = render_task_board(_status(), updated_at=UPDATED_AT, quota_line="")
        without = render_task_board(_status(), updated_at=UPDATED_AT)

        self.assertEqual(empty, without)
        self.assertNotIn("Quota", empty.text)

    def test_digest_changes_only_when_a_bucket_or_reset_changes(self) -> None:
        status = _status(pending=(_job(1, "Work"),))

        def digest(snapshot) -> str:
            line = render_quota_line("claude", snapshot, now=QUOTA_NOW)
            return render_task_board(status, updated_at=UPDATED_AT, quota_line=line).digest

        baseline = digest(_quota(13, 36))  # 87% and 64% left: 90% / 60%.
        for same in (_quota(11, 36), _quota(14, 39), _quota(6, 44)):
            self.assertEqual(digest(same), baseline)
        self.assertNotEqual(digest(_quota(17, 36)), baseline)  # 83% -> 80%.
        self.assertNotEqual(digest(_quota(13, 46)), baseline)  # 7d 54% -> 50%.
        later_reset = FIVE_HOUR_RESET + timedelta(minutes=30)
        self.assertNotEqual(digest(_quota(five_hour_reset=later_reset)), baseline)
        # Below 20% every percent counts.
        self.assertNotEqual(digest(_quota(82)), digest(_quota(83)))

    def test_truncation_drops_quota_before_hiding_tasks(self) -> None:
        status = _status(
            running=_job(1, "Running work"),
            pending=(_job(2, "Queued work"), _job(3, "More queued work")),
            paused=(_job(4, "Paused work", status="paused"),),
        )
        quota = render_quota_line("claude", _quota(), now=QUOTA_NOW)
        bare = render_task_board(status, updated_at=UPDATED_AT).text

        rendered = render_task_board(
            status, updated_at=UPDATED_AT, quota_line=quota, max_chars=len(bare) + 5,
        )

        self.assertEqual(rendered.text, bare)
        for item in ("#1 Running work", "#2 Queued work", "#3 More queued work", "#4 Paused work"):
            self.assertIn(item, rendered.text)
        self.assertNotIn("more (see", rendered.text)

        roomy = render_task_board(
            status, updated_at=UPDATED_AT, quota_line=quota, max_chars=len(bare) + len(quota) + 2,
        )
        self.assertIn(f"\n\n{quota}\n\nUpdated 14:05", roomy.text)

    def test_truncation_with_quota_keeps_every_task_section(self) -> None:
        pending = tuple(_job(index, "queued request " * 10) for index in range(1, 80))
        paused = (_job(90, "Paused work", status="paused"),)
        backlog = tuple(_backlog_item(index, "backlog request " * 5) for index in range(1, 9))

        rendered = render_task_board(
            _status(running=_job(100, "Running work"), pending=pending, paused=paused),
            updated_at=UPDATED_AT,
            max_chars=1200,
            backlog=backlog,
            quota_line=render_quota_line("claude", _quota(), now=QUOTA_NOW),
        )

        self.assertLessEqual(len(rendered.text), 1200)
        self.assertIn("Running:\n- #100 Running work", rendered.text)
        self.assertIn("Queued:\n- #1 ", rendered.text)
        self.assertIn("more (see /queue)", rendered.text)
        self.assertIn("Paused:\n- #90 Paused work", rendered.text)
        self.assertNotIn("Quota", rendered.text)
        self.assertTrue(rendered.text.endswith("Updated 14:05"))


class TaskBoardCommandTests(unittest.TestCase):
    def test_board_is_registered_with_help(self) -> None:
        command = core_command("board")

        self.assertIsNotNone(command)
        assert command is not None
        usage = command.usage_message("/")
        self.assertIn("/board off", usage)
        self.assertIn("/board - post a task board", usage)
        self.assertIn("/board [off|repost]", help_message())

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
        self.assertEqual(
            (state.enabled, state.chat_id, state.message_id), (True, 42, 1),
        )
        self.assertTrue(state.last_digest)
        self.assertTrue(state.last_reposted_at)
        # The /board reply is the one chat message below the new board.
        self.assertEqual(state.messages_since_board, 1)

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
        self._quota_shift = datetime.now(timezone.utc) - QUOTA_NOW
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

    def test_gone_board_is_replaced_and_edits_continue_on_the_new_one(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = "Bad Request: message to edit not found"
        self.status = _status(pending=(_job(4, "New work"),))

        self.app._refresh_task_board()
        state = load_task_board_state(self.root)

        # A provider without pin, unpin, delete or silent send still heals.
        self.assertEqual(self.chat.edit_attempts, 1)
        self.assertEqual(len(self.chat.sent), 2)
        self.assertIn("Queued:\n- #4 New work", self.chat.sent[1][1])
        self.assertTrue(state.active)
        self.assertEqual((state.message_id, state.stale_since), (2, ""))

        self.chat.edit_error = ""
        self.status = _status(pending=(_job(4, "New work"), _job(5, "More")))
        self.app._refresh_task_board()
        self.assertEqual([edit[1] for edit in self.chat.edits], [2])

    def test_board_off_stops_updates(self) -> None:
        self.app._task_board_command("")

        reply = self.app._task_board_command("off")
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()

        self.assertIn("updates stopped", reply)
        self.assertEqual(self.chat.edits, [])
        state = load_task_board_state(self.root)
        self.assertFalse(state.enabled)
        self.assertTrue(state.stopped_by_human)
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

    def _live_quota(
        self, five_hour_used: float | None = 13, seven_day_used: float | None = 36,
    ) -> dict:
        """Return ``_quota`` with its reset times moved next to the real clock.

        The application renders the board with the wall clock, so the fixed
        ``QUOTA_NOW`` resets would expire (hiding their windows) once real time
        passes them. The shift is fixed in setUp, so every snapshot in a test
        keeps the same reset times and the board digest stays comparable.
        """

        return _quota(
            five_hour_used,
            seven_day_used,
            five_hour_reset=FIVE_HOUR_RESET + self._quota_shift,
            seven_day_reset=SEVEN_DAY_RESET + self._quota_shift,
        )

    def _use_quota_runtime(self, snapshot) -> Mock:
        reader = Mock(return_value=snapshot)
        self.app.runtime = SimpleNamespace(name="claude", quota=reader)
        return reader

    def _poll_quota(self, snapshot) -> None:
        self.app.quota_warnings.collect = lambda: iter([("claude", snapshot)])
        self.app.quota_warnings.check_once()

    def test_board_shows_the_cached_quota_without_calling_the_provider(self) -> None:
        reader = self._use_quota_runtime(self._live_quota())
        self._poll_quota(self._live_quota())

        with patch("enoch.quota.quota_snapshots") as snapshots, \
                patch("enoch.quota.load_provider") as load:
            self.app._task_board_command("")
            for _ in range(3):
                self.app._refresh_task_board()

        reader.assert_not_called()
        snapshots.assert_not_called()
        load.assert_not_called()
        board = self.chat.sent[0][1]
        self.assertIn("\n\nQuota (Claude): 5h ", board)
        self.assertIn(" left · 5h resets ", board)
        self.assertTrue(board.splitlines()[-3].startswith("Quota (Claude): "))
        self.assertEqual(self.chat.edits, [])

    def test_quota_changes_inside_a_bucket_do_not_edit_the_board(self) -> None:
        self._use_quota_runtime(self._live_quota())
        self._poll_quota(self._live_quota(13, 36))
        self.app._task_board_command("")

        self._poll_quota(self._live_quota(11, 39))
        self.app._refresh_task_board()
        self.assertEqual(self.chat.edits, [])

        self._poll_quota(self._live_quota(17, 39))
        self.app._refresh_task_board()
        self.app._refresh_task_board()
        self.assertEqual(len(self.chat.edits), 1)
        self.assertIn("5h 80% · 7d 60% left", self.chat.edits[0][2])

    def test_board_shows_only_the_active_runtime_quota(self) -> None:
        self._use_quota_runtime(self._live_quota())
        self.app.quota_warnings.collect = lambda: iter([("codex", self._live_quota())])
        self.app.quota_warnings.check_once()

        self.app._task_board_command("")

        self.assertNotIn("Quota", self.chat.sent[0][1])

    def test_unavailable_quota_omits_the_line_and_keeps_refreshing(self) -> None:
        self._use_quota_runtime(None)
        self._poll_quota({"error": "quota query failed. Check CLI login and version on this host."})
        self.app._task_board_command("")
        self.assertNotIn("Quota", self.chat.sent[0][1])

        self.status = _status(pending=(_job(4, "New work"),))
        with patch.object(
            self.app.quota_warnings, "latest_snapshot", side_effect=RuntimeError("boom"),
        ), patch("enoch.app.core._record_system_event") as record:
            self.app._refresh_task_board()

        record.assert_not_called()
        self.assertEqual(len(self.chat.edits), 1)
        self.assertIn("Queued:\n- #4 New work", self.chat.edits[0][2])
        self.assertNotIn("Quota", self.chat.edits[0][2])

    def test_stale_quota_snapshot_is_not_shown(self) -> None:
        self._use_quota_runtime(self._live_quota())
        self.app.quota_warnings.collect = lambda: iter([("claude", self._live_quota())])
        self.app.quota_warnings.check_once(now=datetime.now(timezone.utc) - timedelta(hours=1))

        self.app._task_board_command("")

        self.assertNotIn("Quota", self.chat.sent[0][1])

    def test_unknown_argument_shows_usage(self) -> None:
        reply = self.app._task_board_command("sideways")

        self.assertIn("Task board commands:", reply)
        self.assertEqual(self.chat.sent, [])


class TaskBoardRepostTests(unittest.TestCase):
    """The board is posted again once enough messages pushed it up the chat."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.status = _status()
        self.chat = _DeletingChat()
        self.app = self._app(self.chat)
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)

    def _app(self, chat) -> EnochApplication:
        app = EnochApplication(load_identity(), self.root, chat)
        inspect = patch.object(app.workflow, "inspect", side_effect=lambda: self.status)
        inspect.start()
        self.addCleanup(inspect.stop)
        return app

    def _post_board(self) -> TaskBoardState:
        self.app._task_board_command("")
        return load_task_board_state(self.root)

    def _set_count(self, count: int) -> None:
        update_task_board_state(self.root, messages_since_board=count)

    def _later(self, **delta) -> datetime:
        # /board stamps the real clock, so checks run relative to it.
        return datetime.now(timezone.utc) + timedelta(**delta)

    def test_counts_inbound_and_outbound_messages_but_not_edits(self) -> None:
        self._post_board()
        self.assertEqual(load_task_board_state(self.root).messages_since_board, 0)

        event = ChatEvent(cursor=7, conversation_id=42, message_id=30, text="/queue")
        self.app.handle_event(event)  # Gary's message and Enoch's reply
        self.app.handle_event(event)  # a replayed event is not counted again
        self.app._safe_send_message(42, "Started task #5.")  # task start line
        self.app._safe_send_message(42, "Task #5 finished.")  # task final line
        self.app._safe_send_message(99, "Elsewhere")  # another conversation
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()  # board edit
        self.app._safe_edit_message(42, 3, "Edited start line")  # other edit

        self.assertEqual(len(self.chat.edits), 2)
        self.assertEqual(load_task_board_state(self.root).messages_since_board, 4)

    def test_counter_ignores_messages_while_board_is_inactive(self) -> None:
        self.app._safe_send_message(42, "No board yet")
        self.assertFalse(task_board_path(self.root).exists())

        self._post_board()
        self.app._safe_send_message(42, "Board is on")
        self.app._task_board_command("off")
        self.app._safe_send_message(42, "Board is off")

        self.assertEqual(load_task_board_state(self.root).messages_since_board, 1)

    def test_no_repost_at_or_below_threshold(self) -> None:
        self._post_board()
        self._set_count(10)

        self.app._maybe_repost_task_board(self._later(hours=2))

        self.assertEqual(len(self.chat.sent), 1)
        self.assertEqual(self.chat.deleted, [])
        self.assertEqual(load_task_board_state(self.root).message_id, 1)

    def test_repost_above_threshold_is_silent_pins_and_deletes_old(self) -> None:
        before = self._post_board()
        self._set_count(11)
        now = self._later(hours=1, minutes=1)

        with patch("enoch.app.core._record_system_event") as record:
            self.app._maybe_repost_task_board(now)
        state = load_task_board_state(self.root)

        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual(self.chat.silent, [2])
        self.assertTrue(self.chat.sent[1][1].startswith("Enoch task board\n"))
        self.assertEqual(self.chat.pins, [(42, 1), (42, 2)])
        self.assertEqual(self.chat.deleted, [(42, 1)])
        self.assertEqual(
            state,
            TaskBoardState(
                enabled=True,
                chat_id=42,
                message_id=2,
                last_digest=before.last_digest,
                messages_since_board=0,
                last_reposted_at=now.isoformat(),
            ),
        )
        events = [call.args[0] for call in record.call_args_list]
        self.assertEqual(events, ["task_board_reposted"])
        self.assertTrue(record.call_args.kwargs["details"]["old_board_deleted"])

        # Edits continue, change-only, on the new message.
        self.app._refresh_task_board()
        self.status = _status(pending=(_job(4, "New work"),))
        self.app._refresh_task_board()
        self.assertEqual([edit[1] for edit in self.chat.edits], [2])

    def test_repost_goes_through_the_notification_journal_and_effect_fence(self) -> None:
        self._post_board()
        self._set_count(11)

        with patch.object(
            self.app.effect_fence, "run_authorized", wraps=self.app.effect_fence.run_authorized,
        ) as fenced:
            self.app._maybe_repost_task_board(self._later(hours=2))

        self.assertEqual(
            [call.args[0] for call in fenced.call_args_list], ["chat.pin", "chat.delete"],
        )
        records = json.loads(_notifications_file(self.root).read_text())["notifications"]
        repost = [key for key in records if key.startswith("task-board-repost:1:")]
        self.assertEqual(len(repost), 1)
        self.assertTrue(records[repost[0]]["silent"])
        self.assertEqual(records[repost[0]]["status"], "delivered")

    def test_hourly_check_is_throttled(self) -> None:
        self._post_board()
        first = self._later(hours=1)
        self._set_count(5)
        self.app._maybe_repost_task_board(first)  # due, but too few messages

        self._set_count(11)
        self.app._maybe_repost_task_board(first + timedelta(minutes=30))
        self.app._maybe_repost_task_board(first + timedelta(minutes=59))
        self.assertEqual(len(self.chat.sent), 1)

        self.app._maybe_repost_task_board(first + timedelta(hours=1))
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual(load_task_board_state(self.root).message_id, 2)

        # The new board starts a new count; the next check finds too few.
        self._set_count(3)
        self.app._maybe_repost_task_board(first + timedelta(hours=2))
        self.assertEqual(len(self.chat.sent), 2)

    def test_first_check_waits_an_interval_after_the_board_was_posted(self) -> None:
        self._post_board()
        self._set_count(50)

        self.app._maybe_repost_task_board(self._later(minutes=30))
        self.assertEqual(len(self.chat.sent), 1)

        self.app._maybe_repost_task_board(self._later(minutes=61))
        self.assertEqual(len(self.chat.sent), 2)

    def test_without_delete_silent_or_pin_support_the_board_still_moves(self) -> None:
        self.chat = _LegacyChat()
        self.app = self._app(self.chat)
        self.assertNotIsInstance(self.chat, MessageDeleteProvider)
        self.assertNotIsInstance(self.chat, SilentMessageProvider)
        self._post_board()
        self._set_count(11)

        with patch("enoch.app.core._record_system_event") as record:
            self.app._maybe_repost_task_board(self._later(hours=2))
        state = load_task_board_state(self.root)

        self.assertEqual(len(self.chat.sent), 2)  # the old and the new board
        self.assertTrue(self.chat.sent[1][1].startswith("Enoch task board\n"))
        self.assertEqual((state.message_id, state.messages_since_board), (2, 0))
        self.assertTrue(state.active)
        self.assertEqual([call.args[0] for call in record.call_args_list], ["task_board_reposted"])
        self.assertFalse(record.call_args.kwargs["details"]["old_board_deleted"])

    def test_failed_delete_leaves_old_board_and_logs_once(self) -> None:
        self.chat.delete_error = "Bad Request: message can't be deleted"
        self.chat.pin_error = "not enough rights"
        self._post_board()
        self._set_count(11)

        with patch("enoch.app.core._record_system_event") as record:
            self.app._maybe_repost_task_board(self._later(hours=2))
            self.app._maybe_repost_task_board(self._later(hours=4))
        state = load_task_board_state(self.root)

        events = [call.args[0] for call in record.call_args_list]
        self.assertEqual(
            events, ["task_board_pin_failed", "task_board_delete_failed", "task_board_reposted"],
        )
        self.assertEqual(self.chat.delete_attempts, 1)
        self.assertEqual((state.message_id, state.messages_since_board), (2, 0))
        self.assertTrue(state.active)

    def test_failed_send_keeps_the_old_board_and_logs_once(self) -> None:
        self._post_board()
        self._set_count(11)
        self.chat.send_error = "Too Many Requests"

        with patch("enoch.app.core._record_system_event") as record:
            self.app._maybe_repost_task_board(self._later(hours=2))
            self.app._maybe_repost_task_board(self._later(hours=4))
        state = load_task_board_state(self.root)

        self.assertEqual(
            [call.args[0] for call in record.call_args_list], ["task_board_repost_failed"],
        )
        self.assertEqual((state.message_id, state.messages_since_board), (1, 11))
        self.assertTrue(state.active)
        self.assertEqual(self.chat.deleted, [])

    def test_counter_survives_restart(self) -> None:
        self._post_board()
        for index in range(12):
            self.app._safe_send_message(42, f"Update {index}")

        restarted = self._app(_DeletingChat())
        self.assertEqual(load_task_board_state(self.root).messages_since_board, 12)
        restarted._maybe_repost_task_board(self._later(hours=2))

        self.assertEqual(restarted.client.silent, [1])
        self.assertEqual(restarted.client.deleted, [(42, 1)])
        self.assertEqual(load_task_board_state(self.root).messages_since_board, 0)

    def test_old_state_file_loads_with_defaults(self) -> None:
        path = task_board_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "enabled": True,
                    "chat_id": 42,
                    "message_id": 609,
                    "last_digest": "abc",
                }
            ),
            encoding="utf-8",
        )

        state = load_task_board_state(self.root)
        self.assertEqual(state, TaskBoardState(True, 42, 609, "abc", 0, ""))

        self.assertTrue(count_task_board_message(42, self.root))
        self.assertFalse(count_task_board_message(7, self.root))
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["messages_since_board"], 1)
        self.assertEqual(raw["message_id"], 609)

        # With no recorded post time, the first check runs right away.
        self._set_count(11)
        self.app._maybe_repost_task_board(self._later(seconds=1))
        self.assertEqual(self.chat.deleted, [(42, 609)])
        self.assertEqual(load_task_board_state(self.root).message_id, 1)

    def test_board_command_resets_the_counter(self) -> None:
        self._post_board()
        self._set_count(7)

        reply = self.app._task_board_command("")
        state = load_task_board_state(self.root)

        self.assertEqual((state.message_id, state.messages_since_board), (2, 0))
        self.assertTrue(state.last_reposted_at)
        self.assertIn(
            "posted again near the bottom of the chat when more than 10 messages appear "
            "below it (checked every 1h; /board repost off turns this off)",
            reply,
        )

        # Through the chat, the reply to /board lands below the board.
        self.app.handle_event(ChatEvent(cursor=9, conversation_id=42, message_id=40, text="/board"))
        self.assertEqual(load_task_board_state(self.root).messages_since_board, 1)

    def test_settings_default_off_and_custom(self) -> None:
        self.assertEqual(board_repost_settings(self.root).interval_seconds, 3600)
        self.assertEqual(board_repost_settings(self.root).threshold, 10)
        self.assertIn(
            "every 1h after more than 10 new messages (default)",
            self.app._task_board_command("repost"),
        )
        self.assertIn(
            "- Task board re-post: every 1h after more than 10 new messages (default)",
            config_status(self.root, runtime=self.app.runtime),
        )
        self._post_board()

        reply = self.app._task_board_command("repost off")
        self.assertIn("re-posting is off", reply)
        self.assertIn("- Task board re-post: off", config_status(self.root, runtime=self.app.runtime))
        self._set_count(100)
        self.app._maybe_repost_task_board(self._later(hours=3))
        self.assertEqual(len(self.chat.sent), 1)  # the board only

        reply = self.app._task_board_command("repost 30m 3")
        self.assertIn("every 30m after more than 3 new messages", reply)
        settings = board_repost_settings(self.root)
        self.assertEqual((settings.enabled, settings.interval_seconds, settings.threshold), (True, 1800, 3))
        self._set_count(3)
        self.app._maybe_repost_task_board(self._later(minutes=31))
        self.assertEqual(len(self.chat.sent), 1)
        self._set_count(4)
        self.app._maybe_repost_task_board(self._later(minutes=62))
        self.assertEqual(len(self.chat.sent), 2)

        self.app._task_board_command("repost off")
        reply = self.app._task_board_command("repost on")
        self.assertIn("every 30m after more than 3 new messages.", reply)
        reply = self.app._task_board_command("repost default")
        self.assertIn("every 1h after more than 10 new messages (default)", reply)

    def test_invalid_settings_are_rejected(self) -> None:
        for argument in ("repost 1m 10", "repost 2d 10", "repost 1h 0", "repost 1h lots", "repost 1h"):
            with self.subTest(argument=argument):
                reply = self.app._task_board_command(argument)
                self.assertIn("/board repost", reply)
                self.assertTrue(board_repost_settings(self.root).uses_defaults)

    def test_help_mentions_repost(self) -> None:
        usage = core_command("board").usage_message("/")

        self.assertIn("/board repost <interval> <count>", usage)
        self.assertIn("/board repost off", usage)

    def test_run_once_checks_the_repost_and_never_raises(self) -> None:
        with patch.object(self.app, "_maybe_repost_task_board") as check:
            self.app.run_once()
        check.assert_called_once_with()

        self._post_board()
        with patch(
            "enoch.app.core.board_repost_settings", side_effect=RuntimeError("boom"),
        ), patch("enoch.app.core._record_system_event") as record:
            self.app.run_once()
            self.app.run_once()

        self.assertEqual(
            [call.args[0] for call in record.call_args_list if call.args[0].startswith("task_board")],
            ["task_board_repost_failed"],
        )


NOT_MODIFIED = (
    "Telegram API call editMessageText failed (400): Bad Request: message is not "
    "modified: specified new message content and reply markup are exactly the same "
    "as a current content and reply markup of the message"
)
NOT_FOUND = "Telegram API call editMessageText failed (400): Bad Request: message to edit not found"
TIMEOUT = "Telegram API call editMessageText failed: network error (TimeoutError: timed out)."
BAD_GATEWAY = "Telegram API call editMessageText failed (502): HTTP 502 Bad Gateway"
FLOOD = "Telegram API call editMessageText failed (429): Too Many Requests: retry after 30"
# What the Telegram provider reported before it kept the Bot API detail.
BARE_FAILURE = "Telegram API call editMessageText failed."


class TaskBoardEditClassificationTests(unittest.TestCase):
    def test_edit_errors_are_classified(self) -> None:
        cases = {
            NOT_MODIFIED: (EDIT_UNCHANGED, None),
            NOT_FOUND: (EDIT_GONE, None),
            "Telegram API call editMessageText failed (400): Bad Request: message can't be edited": (
                EDIT_GONE, None,
            ),
            "Bad Request: MESSAGE_ID_INVALID": (EDIT_GONE, None),
            FLOOD: (EDIT_TRANSIENT, 30),
            "Telegram API call editMessageText failed (429): Flood control (retry after 7)": (
                EDIT_TRANSIENT, 7,
            ),
            BAD_GATEWAY: (EDIT_TRANSIENT, None),
            "Telegram API call editMessageText failed (500): Internal Server Error": (
                EDIT_TRANSIENT, None,
            ),
            TIMEOUT: (EDIT_TRANSIENT, None),
            "Telegram API call editMessageText failed: network error "
            "(ConnectionResetError: [Errno 54] Connection reset by peer).": (EDIT_TRANSIENT, None),
            BARE_FAILURE: (EDIT_FAILED, None),
            "Telegram API call editMessageText failed (403): Forbidden: bot was kicked": (
                EDIT_FAILED, None,
            ),
            "Authorization denied for notification.edit: denied.": (EDIT_FAILED, None),
            "": (EDIT_FAILED, None),
        }
        for error, (kind, retry_after) in cases.items():
            with self.subTest(error=error):
                self.assertEqual(
                    classify_board_edit_error(error), BoardEditFailure(kind, retry_after),
                )


class TaskBoardSelfHealingTests(unittest.TestCase):
    """A failed edit never freezes the board; replaced boards leave the pins."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.status = _status()
        self.changes = 0
        self.chat = _UnpinningChat()
        self.app = self._app(self.chat)
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        self.now = datetime.now(timezone.utc)

    def _app(self, chat) -> EnochApplication:
        app = EnochApplication(load_identity(), self.root, chat)
        inspect = patch.object(app.workflow, "inspect", side_effect=lambda: self.status)
        inspect.start()
        self.addCleanup(inspect.stop)
        return app

    def _change(self) -> None:
        """Change the board content so the next refresh edits it."""

        self.changes += 1
        self.status = _status(pending=(_job(100 + self.changes, f"Work {self.changes}"),))

    def _refresh(self, **delta) -> None:
        self.app._refresh_task_board(self.now + timedelta(**delta))

    def _events(self, record: MagicMock) -> list[str]:
        return [call.args[0] for call in record.call_args_list]

    def test_not_modified_counts_as_success(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = NOT_MODIFIED
        self._change()

        with patch("enoch.app.core._record_system_event") as record:
            self._refresh()
            self._refresh(seconds=30)
        state = load_task_board_state(self.root)

        record.assert_not_called()
        self.assertEqual(self.chat.edit_attempts, 1)  # not retried
        self.assertEqual(state.last_digest, self.app._render_task_board().digest)
        self.assertEqual((state.message_id, state.edit_failures, state.stale_since), (1, 0, ""))
        self.assertEqual(len(self.chat.sent), 1)

    def test_transient_errors_keep_the_board_and_retry_on_later_cycles(self) -> None:
        self.app._task_board_command("")
        self._change()

        with patch("enoch.app.core._record_system_event") as record:
            self.chat.edit_error = TIMEOUT
            for second in range(4):
                self._refresh(seconds=second)
            self.chat.edit_error = BAD_GATEWAY
            for second in range(4, 8):
                self._refresh(seconds=second)
            state = load_task_board_state(self.root)
            self.chat.edit_error = ""
            self._refresh(seconds=8)

        self.assertEqual(self.chat.edit_attempts, 9)
        self.assertTrue(state.active)
        self.assertEqual((state.message_id, state.edit_failures, state.stale_since), (1, 0, ""))
        self.assertEqual(len(self.chat.sent), 1)  # no fresh board
        # Each distinct error is logged once.
        self.assertEqual(self._events(record), ["task_board_edit_failed"] * 2)
        self.assertEqual(
            [call.kwargs["details"]["kind"] for call in record.call_args_list],
            ["transient", "transient"],
        )
        self.assertEqual([edit[1] for edit in self.chat.edits], [1])
        self.assertIn("Work 1", self.chat.edits[0][2])
        self.assertEqual(
            load_task_board_state(self.root).last_digest, self.app._render_task_board().digest,
        )

    def test_rate_limit_waits_for_retry_after(self) -> None:
        self.app._task_board_command("")
        self._change()
        self.chat.edit_error = FLOOD

        with patch("enoch.app.core._record_system_event") as record:
            self._refresh()
            self._refresh(seconds=10)
            self._refresh(seconds=29)
            self.assertEqual(self.chat.edit_attempts, 1)
            self.chat.edit_error = FLOOD.replace("30", "12")
            self._refresh(seconds=31)
            self.assertEqual(self.chat.edit_attempts, 2)
            self._refresh(seconds=40)
            self.assertEqual(self.chat.edit_attempts, 2)
            self.chat.edit_error = ""
            self._refresh(seconds=44)

        self.assertEqual(self.chat.edit_attempts, 3)
        self.assertEqual(len(self.chat.edits), 1)
        # A different countdown is the same error.
        self.assertEqual(self._events(record), ["task_board_edit_failed"])
        self.assertEqual(record.call_args.kwargs["details"]["retry_after"], 30)
        self.assertTrue(load_task_board_state(self.root).active)

    def test_message_not_found_posts_one_rate_limited_fresh_board(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = NOT_FOUND
        self._change()

        with patch("enoch.app.core._record_system_event") as record:
            self._refresh()
        state = load_task_board_state(self.root)

        self.assertEqual(self._events(record), ["task_board_stale", "task_board_reposted"])
        self.assertEqual(record.call_args_list[0].kwargs["details"]["reason"], "gone")
        details = record.call_args.kwargs["details"]
        self.assertEqual(details["reason"], "stale")
        self.assertTrue(details["old_board_unpinned"] and details["old_board_deleted"])
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual(self.chat.silent, [2])
        self.assertIn("Work 1", self.chat.sent[1][1])
        self.assertEqual(self.chat.pins, [(42, 1), (42, 2)])
        self.assertEqual(self.chat.calls, [("unpin", 42, 1), ("delete", 42, 1)])
        self.assertTrue(state.active)
        self.assertEqual(
            (state.message_id, state.stale_since, state.edit_failures, state.messages_since_board),
            (2, "", 0, 0),
        )
        self.assertEqual(state.last_recovery_at, self.now.isoformat())

        # The fresh board disappears too: no second board inside the interval.
        self._change()
        with patch("enoch.app.core._record_system_event") as record:
            self._refresh(minutes=1)
            self._change()
            self._refresh(minutes=5)
            self._refresh(minutes=9)
        state = load_task_board_state(self.root)

        self.assertEqual(self._events(record), ["task_board_stale"])
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual(self.chat.edit_attempts, 2)  # a stale board is not edited
        self.assertTrue(state.active)
        self.assertEqual(state.message_id, 2)
        self.assertTrue(state.stale_since)

        self._refresh(minutes=11)
        state = load_task_board_state(self.root)
        self.assertEqual(len(self.chat.sent), 3)
        self.assertIn("Work 3", self.chat.sent[2][1])
        self.assertEqual((state.message_id, state.stale_since), (3, ""))
        self.assertEqual(self.chat.calls[-2:], [("unpin", 42, 2), ("delete", 42, 2)])

    def test_repeated_unclassified_failures_replace_the_board_past_the_bound(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = BARE_FAILURE
        self._change()

        with patch("enoch.app.core._record_system_event") as record:
            self._refresh()
            self._refresh(seconds=30)
            state = load_task_board_state(self.root)
            self.assertEqual((state.message_id, state.edit_failures, state.stale_since), (1, 2, ""))
            self.assertEqual(len(self.chat.sent), 1)
            self._refresh(seconds=60)
        state = load_task_board_state(self.root)

        self.assertEqual(
            self._events(record),
            ["task_board_edit_failed", "task_board_stale", "task_board_reposted"],
        )
        self.assertEqual(record.call_args_list[1].kwargs["details"]["edit_failures"], 3)
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual((state.message_id, state.edit_failures, state.stale_since), (2, 0, ""))

    def test_failed_recovery_keeps_the_stale_board_and_retries_after_the_interval(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = NOT_FOUND
        self.chat.send_error = "Telegram API call sendMessage failed: network error (TimeoutError: timed out)."
        self._change()

        with patch("enoch.app.core._record_system_event") as record:
            self._refresh()
            self._refresh(minutes=5)
            self._refresh(minutes=11)
        state = load_task_board_state(self.root)

        self.assertEqual(
            self._events(record), ["task_board_stale", "task_board_recovery_failed"],
        )
        self.assertEqual(self.chat.send_attempts, 3)  # /board, then two recoveries
        self.assertTrue(state.active and state.stale_since)
        self.assertEqual(state.message_id, 1)

        self.chat.send_error = ""
        self._refresh(minutes=15)
        self.assertEqual(len(self.chat.sent), 1)
        self._refresh(minutes=22)
        state = load_task_board_state(self.root)
        self.assertEqual(len(self.chat.sent), 2)
        self.assertEqual((state.message_id, state.stale_since), (2, ""))

    def test_board_off_is_never_revived(self) -> None:
        self.app._task_board_command("")
        self.app._task_board_command("off")
        self.chat.edit_error = NOT_FOUND
        update_task_board_state(self.root, messages_since_board=50)

        for hours in range(4):
            self._change()
            self._refresh(hours=hours)
            self.app._maybe_repost_task_board(self.now + timedelta(hours=hours, minutes=1))
        state = load_task_board_state(self.root)

        self.assertEqual(self.chat.edit_attempts, 0)
        self.assertEqual(len(self.chat.sent), 1)
        self.assertEqual((state.enabled, state.stopped_by_human, state.message_id), (False, True, 1))

        # Only the human brings it back.
        self.chat.edit_error = ""
        self.app._task_board_command("")
        state = load_task_board_state(self.root)
        self.assertTrue(state.active)
        self.assertFalse(state.stopped_by_human)
        self.assertEqual(state.message_id, 2)

    def test_stale_board_stopped_by_the_human_is_not_replaced(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = NOT_FOUND
        self.chat.send_error = "Telegram API call sendMessage failed (500): Internal Server Error"
        self._change()
        self._refresh()
        self.assertTrue(load_task_board_state(self.root).stale_since)

        self.app._task_board_command("off")
        self.chat.send_error = ""
        self._refresh(minutes=20)
        self._refresh(hours=3)
        state = load_task_board_state(self.root)

        self.assertEqual(len(self.chat.sent), 1)
        self.assertFalse(state.active)
        self.assertTrue(state.stopped_by_human)

    def test_board_stopped_while_a_fresh_board_is_sent_stays_stopped(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = NOT_FOUND
        self.chat.on_silent_send = lambda: self.app._task_board_command("off")
        self._change()

        with patch("enoch.app.core._record_system_event") as record:
            self._refresh()
        state = load_task_board_state(self.root)

        self.assertEqual(
            self._events(record), ["task_board_stale", "task_board_repost_superseded"],
        )
        self.assertEqual((state.enabled, state.stopped_by_human, state.message_id), (False, True, 1))
        # The unused fresh board is removed and never pinned.
        self.assertEqual(self.chat.pins, [(42, 1)])
        self.assertEqual(self.chat.calls, [("delete", 42, 2)])

    def test_legacy_disabled_board_is_not_revived(self) -> None:
        path = task_board_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"schema_version": 1, "enabled": False, "chat_id": 42, "message_id": 7,
                 "last_digest": "abc"}
            ),
            encoding="utf-8",
        )

        self._refresh(hours=1)
        self.app._maybe_repost_task_board(self.now + timedelta(hours=2))

        self.assertEqual((self.chat.sent, self.chat.edit_attempts), ([], 0))
        self.assertFalse(load_task_board_state(self.root).active)

    def test_board_command_unpins_then_deletes_the_previous_board(self) -> None:
        self.app._task_board_command("")

        with patch.object(
            self.app.effect_fence, "run_authorized", wraps=self.app.effect_fence.run_authorized,
        ) as fenced:
            reply = self.app._task_board_command("")

        self.assertIn("The previous board was removed.", reply)
        self.assertEqual(self.chat.pins, [(42, 1), (42, 2)])
        self.assertEqual(self.chat.calls, [("unpin", 42, 1), ("delete", 42, 1)])
        self.assertEqual(
            [(call.args[0], call.args[1]) for call in fenced.call_args_list],
            [("chat.pin", ("chat.pin",)), ("chat.unpin", ("chat.pin",)), ("chat.delete", ("chat.delete",))],
        )

    def test_repost_unpins_then_deletes_the_old_board(self) -> None:
        self.app._task_board_command("")
        update_task_board_state(self.root, messages_since_board=11)

        with patch("enoch.app.core._record_system_event") as record:
            self.app._maybe_repost_task_board(self.now + timedelta(hours=2))

        self.assertEqual(self.chat.calls, [("unpin", 42, 1), ("delete", 42, 1)])
        details = record.call_args.kwargs["details"]
        self.assertEqual(details["reason"], "drift")
        self.assertTrue(details["old_board_unpinned"] and details["old_board_deleted"])

    def test_delete_failure_still_unpins_the_old_board(self) -> None:
        self.app._task_board_command("")
        update_task_board_state(self.root, messages_since_board=11)
        self.chat.delete_error = "Bad Request: message can't be deleted for everyone"

        with patch("enoch.app.core._record_system_event") as record:
            self.app._maybe_repost_task_board(self.now + timedelta(hours=2))
            reply = self.app._task_board_command("")

        self.assertEqual(
            self._events(record),
            ["task_board_delete_failed", "task_board_reposted", "task_board_delete_failed"],
        )
        details = record.call_args_list[1].kwargs["details"]
        self.assertTrue(details["old_board_unpinned"])
        self.assertFalse(details["old_board_deleted"])
        self.assertEqual(self.chat.unpinned, [(42, 1), (42, 2)])
        self.assertEqual(self.chat.deleted, [])
        self.assertIn("previous board was unpinned; the chat did not allow deleting it", reply)
        self.assertTrue(load_task_board_state(self.root).active)

    def test_unpin_failure_is_logged_once_and_never_raised(self) -> None:
        for error in (ChatProviderError("Bad Request: message to unpin not found"), RuntimeError("boom")):
            with self.subTest(error=type(error).__name__):
                self.chat.unpin_failure = error
                self.app._task_board_command("")
                old = load_task_board_state(self.root).message_id
                update_task_board_state(self.root, messages_since_board=11)
                self.app._task_board_repost_checked_at = None

                with patch("enoch.app.core._record_system_event") as record:
                    self.app._maybe_repost_task_board(self.now + timedelta(hours=2))

                self.assertEqual(
                    self._events(record), ["task_board_unpin_failed", "task_board_reposted"],
                )
                self.assertEqual(self.chat.calls[-1], ("delete", 42, old))
                self.assertNotEqual(load_task_board_state(self.root).message_id, old)

    def test_unpin_contract_falls_back_for_an_older_provider_kit(self) -> None:
        # Descendants may install a provider-kit without the unpin contract.
        # Load a separate copy of the contracts module against a provider-kit
        # that lacks it; the real module and its classes stay untouched.
        import our_ark_provider_kit
        from enoch.providers import _message_unpin_compat, contracts

        older_kit = ModuleType(our_ark_provider_kit.__name__)
        older_kit.__dict__.update(
            (name, value)
            for name, value in vars(our_ark_provider_kit).items()
            if name != "MessageUnpinProvider"
        )
        spec = importlib.util.spec_from_file_location(
            "_contracts_with_an_older_provider_kit", contracts.__file__,
        )
        loaded = importlib.util.module_from_spec(spec)
        with patch.dict(
            sys.modules, {our_ark_provider_kit.__name__: older_kit, spec.name: loaded},
        ):
            spec.loader.exec_module(loaded)

        fallback = loaded.MessageUnpinProvider
        self.assertIs(fallback, _message_unpin_compat.MessageUnpinProvider)
        self.assertIsNot(fallback, our_ark_provider_kit.MessageUnpinProvider)
        self.assertIsInstance(_UnpinningChat(), fallback)
        self.assertNotIsInstance(_DeletingChat(), fallback)

    def test_provider_without_unpin_falls_back_to_delete(self) -> None:
        chat = _DeletingChat()
        self.app = self._app(chat)
        self.assertNotIsInstance(chat, MessageUnpinProvider)
        self.app._task_board_command("")

        with patch("enoch.app.core._record_system_event") as record, patch.object(
            self.app.effect_fence, "run_authorized", wraps=self.app.effect_fence.run_authorized,
        ) as fenced:
            reply = self.app._task_board_command("")

        record.assert_not_called()
        self.assertIn("The previous board was removed.", reply)
        self.assertEqual(chat.deleted, [(42, 1)])
        self.assertEqual([call.args[0] for call in fenced.call_args_list], ["chat.pin", "chat.delete"])

    def test_failed_board_edits_are_not_replayed_after_a_restart(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = TIMEOUT
        self._change()
        self._refresh()
        self.chat.edit_error = ""
        self._change()
        self._refresh(seconds=30)
        self.assertEqual(len(self.chat.edits), 1)

        restarted = self._app(self.chat)

        self.assertEqual(len(self.chat.edits), 1)
        self.assertIn("Work 2", self.chat.edits[0][2])
        restarted._refresh_task_board(self.now + timedelta(minutes=1))
        self.assertEqual(len(self.chat.edits), 1)

    def test_failed_fresh_boards_are_not_replayed_after_a_restart(self) -> None:
        self.app._task_board_command("")
        self.chat.edit_error = NOT_FOUND
        self.chat.send_error = "Telegram API call sendMessage failed (500): Internal Server Error"
        self._change()
        self._refresh()
        self._refresh(minutes=11)
        self.assertEqual((self.chat.send_attempts, len(self.chat.sent)), (3, 1))

        self.chat.send_error = ""
        self._app(self.chat)

        # A replayed send would leave untracked, unpinned boards in the chat.
        self.assertEqual((self.chat.send_attempts, len(self.chat.sent)), (3, 1))
        state = load_task_board_state(self.root)
        self.assertTrue(state.active and state.stale_since)
        self.assertEqual(state.message_id, 1)


class SilentNotificationTests(unittest.TestCase):
    def test_silent_send_is_journaled_and_uses_the_silent_capability(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            chat = _DeletingChat()
            app = EnochApplication(load_identity(), root, chat)
            delivered = []
            service = NotificationDeliveryService(
                chat, "test", root, app.daemon_epoch, delivery_listener=delivered.append,
            )

            first = service.send(42, "Quiet", idempotency_key="quiet", silent=True)
            replay = service.send(42, "Quiet", idempotency_key="quiet", silent=True)
            loud = service.send(42, "Loud", idempotency_key="loud")
            mismatch = service.send(42, "Loud", idempotency_key="loud", silent=True)
            service.edit(42, 1, "Edited", idempotency_key="edit")
            record = notification_record("test", "quiet", root)

        self.assertTrue(first.delivered and replay.delivered and loud.delivered)
        self.assertFalse(mismatch.delivered)
        self.assertEqual(chat.silent, [1])
        self.assertEqual(len(chat.sent), 2)
        self.assertTrue(record.silent)
        self.assertEqual([item.idempotency_key for item in delivered], ["quiet", "loud"])


def _notifications_file(root: Path) -> Path:
    return notifications_path("test", root)


class _LegacyChat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.edit_attempts = 0
        self.edit_error = ""
        self.send_attempts = 0

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.send_attempts += 1
        if getattr(self, "send_error", ""):
            raise ChatProviderError(self.send_error)
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


class _DeletingChat(_PinningChat):
    """Pins, deletes, and sends silently, like the Telegram provider."""

    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[tuple[int, int]] = []
        self.delete_attempts = 0
        self.delete_error = ""
        self.silent: list[int] = []

    def send_silent_message(self, conversation_id, text):
        message_id = self.send_message(conversation_id, text)
        self.silent.append(message_id)
        return message_id

    def delete_message(self, conversation_id, message_id):
        self.delete_attempts += 1
        if self.delete_error:
            raise ChatProviderError(self.delete_error)
        self.deleted.append((conversation_id, message_id))


class _UnpinningChat(_DeletingChat):
    """Also unpins, like the Telegram provider; records unpin/delete order."""

    def __init__(self) -> None:
        super().__init__()
        self.unpinned: list[tuple[int, int]] = []
        self.unpin_failure: BaseException | None = None
        self.calls: list[tuple[str, int, int]] = []
        self.on_silent_send = None

    def send_silent_message(self, conversation_id, text):
        message_id = super().send_silent_message(conversation_id, text)
        if self.on_silent_send is not None:
            self.on_silent_send()
        return message_id

    def unpin_message(self, conversation_id, message_id):
        self.calls.append(("unpin", conversation_id, message_id))
        if self.unpin_failure is not None:
            raise self.unpin_failure
        self.unpinned.append((conversation_id, message_id))

    def delete_message(self, conversation_id, message_id):
        self.calls.append(("delete", conversation_id, message_id))
        super().delete_message(conversation_id, message_id)


if __name__ == "__main__":
    unittest.main()
