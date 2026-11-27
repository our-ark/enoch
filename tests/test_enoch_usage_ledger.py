"""Private runtime token-usage ledger and where it is shown.

The ledger records one line per runtime call (counts only, never prompt or
reply text), stays bounded by folding old days into a summary, and feeds the
task final message, the task board ``Today:`` line, and ``/usage``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))

from enoch import usage_ledger
from enoch.app import core
from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app.execution_context import CURRENT_TASK_ID, CURRENT_USAGE_CATEGORY
from enoch.app.task_board import TaskBoardState, render_task_board, save_task_board_state
from enoch.commands import help_message
from enoch.identity import load_identity
from enoch.providers import ChatEvent, RuntimeResult, RuntimeUsage
from enoch.tasks.queue import TaskJob, TaskQueueStatus, begin_next_task
from enoch.usage_ledger import (
    CATEGORIES,
    UsageTotals,
    board_usage_line,
    day_file,
    format_task_tokens_line,
    format_tokens,
    format_usage_report,
    load_usage_report,
    record_runtime_usage,
    rotate_usage_ledger,
    task_usage,
    usage_directory,
)
from tests.test_enoch_application import _Runtime


NOW = datetime(2026, 11, 26, 15, 30).astimezone()
SECRET = "PRIVATE-PROMPT-TEXT-7f3a"


def _usage(input_tokens=0, cached=0, output=0, reasoning=0) -> RuntimeUsage:
    return RuntimeUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output,
        reasoning_tokens=reasoning,
    )


def _records(root: Path, day: date | None = None) -> list[dict]:
    path = day_file(day or datetime.now().astimezone().date(), root)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


class _TempRootCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)


class LedgerWriteTests(_TempRootCase):
    def test_each_category_writes_one_record_with_counts_only(self) -> None:
        for index, category in enumerate(CATEGORIES, start=1):
            self.assertTrue(
                record_runtime_usage(
                    self.root,
                    category=category,
                    usage=_usage(1000 * index, 900 * index, 10 * index, index),
                    provider="claude",
                    model="claude-opus-5-5",
                    task_id=index if category in {"task", "ci-fix"} else None,
                    now=NOW,
                )
            )

        records = _records(self.root, NOW.date())
        self.assertEqual([record["category"] for record in records], list(CATEGORIES))
        self.assertEqual(
            set(records[0]),
            {
                "ts", "category", "task_id", "provider", "model",
                "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
            },
        )
        self.assertEqual(records[0]["task_id"], 1)
        self.assertEqual(records[1]["category"], "ci-fix")
        self.assertIsNone(records[2]["task_id"])
        self.assertEqual(records[2]["input_tokens"], 3000)
        self.assertEqual(records[2]["cached_input_tokens"], 2700)
        self.assertEqual(records[2]["output_tokens"], 30)
        self.assertEqual(records[2]["reasoning_tokens"], 3)
        self.assertEqual(records[0]["provider"], "claude")
        self.assertEqual(records[0]["model"], "claude-opus-5-5")
        self.assertTrue(records[0]["ts"].startswith("2026-11-26T15:30"))

    def test_ledger_lives_in_private_state(self) -> None:
        record_runtime_usage(self.root, category="task", usage=_usage(1), now=NOW)

        self.assertEqual(usage_directory(self.root), (self.root / ".enoch" / "usage").resolve())
        self.assertTrue((self.root / ".enoch" / "usage" / "2026-11-26.jsonl").is_file())
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".enoch/", gitignore)

    def test_unknown_category_is_recorded_as_other(self) -> None:
        record_runtime_usage(self.root, category="mystery", usage=_usage(5), now=NOW)

        self.assertEqual(_records(self.root, NOW.date())[0]["category"], "other")

    def test_missing_or_malformed_usage_records_zeros(self) -> None:
        for usage in (None, object(), {}, {"input_tokens": "lots", "output_tokens": -4}):
            self.assertTrue(
                record_runtime_usage(self.root, category="conversation", usage=usage, now=NOW)
            )

        records = _records(self.root, NOW.date())
        self.assertEqual(len(records), 4)
        for record in records:
            for name in usage_ledger.TOKEN_FIELDS:
                self.assertEqual(record[name], 0)

    def test_mapping_usage_is_accepted(self) -> None:
        record_runtime_usage(
            self.root,
            category="task",
            usage={"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
            now=NOW,
        )

        record = _records(self.root, NOW.date())[0]
        self.assertEqual((record["input_tokens"], record["cached_input_tokens"]), (10, 4))
        self.assertEqual(record["reasoning_tokens"], 0)

    def test_write_failure_returns_false_without_raising(self) -> None:
        (self.root / ".enoch").mkdir()
        # A file where the usage directory should be makes every write fail.
        (self.root / ".enoch" / "usage").write_text("not a directory", encoding="utf-8")

        self.assertFalse(record_runtime_usage(self.root, category="task", usage=_usage(1), now=NOW))
        self.assertEqual(load_usage_report(self.root, now=NOW).total.calls, 0)
        self.assertTrue(task_usage(self.root, 1).empty)

    def test_labels_are_single_line_and_bounded(self) -> None:
        record_runtime_usage(
            self.root, category="task", usage=_usage(1), provider="a\nb", model="m" * 500, now=NOW,
        )

        record = _records(self.root, NOW.date())[0]
        self.assertEqual(record["provider"], "a b")
        self.assertEqual(len(record["model"]), usage_ledger.LABEL_LIMIT)


class RotationTests(_TempRootCase):
    def _write_day(self, day: date, category: str, input_tokens: int, calls: int = 1) -> None:
        moment = datetime(day.year, day.month, day.day, 12).astimezone()
        for _ in range(calls):
            record_runtime_usage(
                self.root, category=category, usage=_usage(input_tokens, output=1), now=moment,
            )

    def test_old_days_are_summarized_and_removed(self) -> None:
        today = NOW.date()
        old = today - timedelta(days=usage_ledger.RAW_RETENTION_DAYS + 3)
        kept = today - timedelta(days=2)
        self._write_day(old, "task", 100, calls=2)
        self._write_day(old, "conversation", 7)
        self._write_day(kept, "task", 5)

        self.assertTrue(rotate_usage_ledger(self.root, today=today))

        directory = usage_directory(self.root)
        self.assertFalse(day_file(old, self.root).exists())
        self.assertTrue(day_file(kept, self.root).exists())
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(
            summary["days"][old.isoformat()]["task"],
            {
                "input_tokens": 200,
                "cached_input_tokens": 0,
                "output_tokens": 2,
                "reasoning_tokens": 0,
                "calls": 2,
            },
        )
        self.assertEqual(summary["days"][old.isoformat()]["conversation"]["calls"], 1)

    def test_summary_drops_days_past_its_own_retention(self) -> None:
        today = NOW.date()
        ancient = today - timedelta(days=usage_ledger.SUMMARY_RETENTION_DAYS + 5)
        directory = usage_directory(self.root)
        directory.mkdir(parents=True)
        (directory / "summary.json").write_text(
            json.dumps({"schema_version": 1, "days": {ancient.isoformat(): {"task": {"calls": 1}}}}),
            encoding="utf-8",
        )
        self._write_day(ancient, "task", 9)

        rotate_usage_ledger(self.root, today=today)

        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["days"], {})
        self.assertFalse(day_file(ancient, self.root).exists())

    def test_first_record_of_a_day_rotates(self) -> None:
        old = date.today() - timedelta(days=usage_ledger.RAW_RETENTION_DAYS + 1)
        self._write_day(old, "task", 1)
        usage_ledger._ROTATED_DAYS.pop(str(usage_directory(self.root)), None)

        record_runtime_usage(self.root, category="task", usage=_usage(1))

        self.assertFalse(day_file(old, self.root).exists())
        self.assertTrue((usage_directory(self.root) / "summary.json").exists())

    def test_oversized_day_file_stops_growing(self) -> None:
        with patch.object(usage_ledger, "MAX_DAY_FILE_BYTES", 1):
            self.assertTrue(record_runtime_usage(self.root, category="task", usage=_usage(1), now=NOW))
            self.assertFalse(record_runtime_usage(self.root, category="task", usage=_usage(1), now=NOW))

        self.assertEqual(len(_records(self.root, NOW.date())), 1)


class AggregationTests(_TempRootCase):
    def test_today_and_seven_day_windows(self) -> None:
        record_runtime_usage(self.root, category="task", usage=_usage(1000, 900, 50), task_id=7, now=NOW)
        record_runtime_usage(
            self.root, category="conversation", usage=_usage(200, 100, 10), now=NOW,
        )
        record_runtime_usage(
            self.root, category="task", usage=_usage(5000, 0, 5), task_id=8,
            now=NOW - timedelta(days=3),
        )
        record_runtime_usage(
            self.root, category="task", usage=_usage(99999), task_id=9,
            now=NOW - timedelta(days=8),
        )
        with day_file(NOW.date(), self.root).open("a", encoding="utf-8") as handle:
            handle.write("{not json\n[1, 2]\n\n")

        today = load_usage_report(self.root, days=1, now=NOW)
        week = load_usage_report(self.root, days=7, now=NOW)

        self.assertEqual(today.total.calls, 2)
        self.assertEqual(today.total.input_tokens, 1200)
        self.assertEqual(today.by_category["conversation"].cached_input_tokens, 100)
        self.assertEqual(set(today.by_task), {7})
        self.assertEqual(week.total.input_tokens, 6200)
        self.assertEqual(set(week.by_task), {7, 8})
        self.assertEqual(week.start_day, (NOW.date() - timedelta(days=6)).isoformat())

    def test_report_cache_sees_new_records(self) -> None:
        record_runtime_usage(self.root, category="task", usage=_usage(10), now=NOW)
        first = load_usage_report(self.root, now=NOW)
        self.assertIs(load_usage_report(self.root, now=NOW), first)

        record_runtime_usage(self.root, category="task", usage=_usage(10), now=NOW)

        self.assertEqual(load_usage_report(self.root, now=NOW).total.input_tokens, 20)

    def test_task_usage_sums_every_call_for_the_task(self) -> None:
        record_runtime_usage(self.root, category="task", usage=_usage(100, 90, 1), task_id=5, now=NOW)
        record_runtime_usage(
            self.root, category="task", usage=_usage(50, 40, 2), task_id=5,
            now=NOW - timedelta(days=1),
        )
        record_runtime_usage(self.root, category="task", usage=_usage(999), task_id=6, now=NOW)

        totals = task_usage(self.root, 5)
        self.assertEqual((totals.input_tokens, totals.cached_input_tokens), (150, 130))
        self.assertEqual(totals.calls, 2)
        recent = task_usage(self.root, 5, since=NOW.isoformat())
        # One day of slack keeps yesterday's record.
        self.assertEqual(recent.input_tokens, 150)
        later = task_usage(self.root, 5, since=(NOW + timedelta(days=3)).isoformat())
        self.assertTrue(later.empty)
        self.assertEqual(task_usage(self.root, 5, since="garbage").input_tokens, 150)


class FormattingTests(unittest.TestCase):
    def test_compact_token_counts(self) -> None:
        self.assertEqual(format_tokens(0), "0")
        self.assertEqual(format_tokens(950), "950")
        self.assertEqual(format_tokens(62_400), "62k")
        self.assertEqual(format_tokens(999_400), "999k")
        self.assertEqual(format_tokens(999_600), "1.0M")
        self.assertEqual(format_tokens(8_100_000), "8.1M")
        self.assertEqual(format_tokens(150_400_000), "150M")

    def test_task_tokens_line(self) -> None:
        totals = UsageTotals(input_tokens=8_100_000, cached_input_tokens=7_950_000, output_tokens=62_000)

        self.assertEqual(format_task_tokens_line(totals), "Tokens: 8.1M in (98% cached) · 62k out")
        self.assertEqual(format_task_tokens_line(UsageTotals()), "")
        self.assertEqual(format_task_tokens_line(None), "")

    def test_board_line_rounds_to_buckets(self) -> None:
        report = _report(task=32_370_000, conversation=6_225_000, snapshot=2_905_000, output=430_000)

        self.assertEqual(
            board_usage_line(report),
            "Today: 41M in · 0.4M out (tasks 78%, chat 15%, other 7%)",
        )

    def test_board_line_small_and_empty_days(self) -> None:
        self.assertEqual(board_usage_line(_report()), "")
        self.assertEqual(board_usage_line(None), "")
        self.assertEqual(
            board_usage_line(_report(conversation=300_000, output=2_000)),
            "Today: <1M in · <0.1M out (chat 100%)",
        )

    def test_board_line_is_stable_inside_a_bucket(self) -> None:
        base = _report(task=32_370_000, conversation=6_225_000, snapshot=2_905_000, output=430_000)
        chatted = _report(task=32_370_000, conversation=6_325_000, snapshot=2_905_000, output=432_000)
        crossed = _report(task=32_370_000, conversation=7_225_000, snapshot=2_905_000, output=430_000)

        self.assertEqual(board_usage_line(base), board_usage_line(chatted))
        self.assertNotEqual(board_usage_line(base), board_usage_line(crossed))

        status = TaskQueueStatus(pending_count=0)
        updated = datetime(2026, 11, 26, 18, 0)
        first = render_task_board(status, updated_at=updated, usage_line=board_usage_line(base))
        same = render_task_board(status, updated_at=updated, usage_line=board_usage_line(chatted))
        moved = render_task_board(status, updated_at=updated, usage_line=board_usage_line(crossed))
        self.assertEqual(first.digest, same.digest)
        self.assertNotEqual(first.digest, moved.digest)

    def test_board_places_usage_just_above_quota(self) -> None:
        status = TaskQueueStatus(pending_count=0)
        text = render_task_board(
            status,
            updated_at=datetime(2026, 11, 26, 18, 0),
            usage_line="Today: 41M in · 0.4M out (tasks 100%)",
            quota_line="Quota (Claude): 5h 80% · 7d 60% left",
        ).text
        lines = text.splitlines()
        usage_index = lines.index("Today: 41M in · 0.4M out (tasks 100%)")
        self.assertEqual(lines[usage_index + 1], "Quota (Claude): 5h 80% · 7d 60% left")

    def test_board_drops_usage_line_before_quota_when_tight(self) -> None:
        status = TaskQueueStatus(pending_count=0)
        usage = "Today: 41M in · 0.4M out (tasks 100%)"
        quota = "Quota (Claude): 5h 80% left"
        roomy = render_task_board(status, updated_at=NOW, usage_line=usage, quota_line=quota).text
        tight = render_task_board(
            status, updated_at=NOW, usage_line=usage, quota_line=quota,
            max_chars=len(roomy) - 5,
        ).text

        self.assertNotIn(usage, tight)
        self.assertIn(quota, tight)

    def test_usage_report_text(self) -> None:
        report = _report(task=8_000_000, conversation=1_000_000, snapshot=1_000_000, output=100_000)
        report.by_task[77] = UsageTotals(input_tokens=6_000_000, output_tokens=50_000, calls=1)
        report.by_task[12] = UsageTotals(input_tokens=2_000_000, output_tokens=1_000, calls=1)
        report.by_category["conversation"].calls = 4

        text = format_usage_report(report, label="today", task_titles={77: "Token usage"})

        self.assertIn("Token usage, today (2026-11-26)", text)
        self.assertIn("Total: 10.0M in", text)
        self.assertIn("- task: 8.0M in", text)
        self.assertIn("80% of input", text)
        self.assertNotIn("- evolve:", text)
        self.assertLess(text.index("#77 Token usage"), text.index("#12"))
        self.assertIn("Conversation turns: 4 · average 250k in", text)

    def test_empty_usage_report(self) -> None:
        text = format_usage_report(_report(), label="last 7 days")

        self.assertIn("No runtime calls recorded.", text)


def _report(*, output: int = 0, **inputs: int) -> usage_ledger.UsageReport:
    report = usage_ledger.UsageReport(start_day="2026-11-26", end_day="2026-11-26")
    first = True
    for category, value in inputs.items():
        totals = UsageTotals(
            input_tokens=value,
            cached_input_tokens=value * 9 // 10,
            output_tokens=output if first else 0,
            calls=1,
        )
        first = False
        report.by_category[category] = totals
        report.total.add(totals)
    return report


class _Chat:
    name = "test"
    provider_kind = "chat"

    def __init__(self) -> None:
        self.allowed_conversation_id = 42
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    def receive(self, cursor=None):
        return []

    def send_message(self, conversation_id, text):
        self.sent.append((conversation_id, text))
        return len(self.sent)

    def edit_message(self, conversation_id, message_id, text):
        self.edits.append((conversation_id, message_id, text))

    def send_read_ack(self, conversation_id, message_id):
        return None


class _UsageRuntime(_Runtime):
    name = "claude"
    config_section = "claude"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def respond(self, identity, message, **kwargs):
        self.prompts.append(message)
        return RuntimeResult(
            final_text=f"Reply about {SECRET}",
            usage=_usage(12_000, 11_000, 300, 20),
        )


class ApplicationUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        for target in (
            "enoch.app.core._sync_session_activity",
            "enoch.app.core.log_conversation_turn",
            "enoch.app.core.log_system_event",
            "enoch.app.core.ensure_long_term_memory",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.chat = _Chat()
        self.runtime = _UsageRuntime()
        self.app = EnochApplication(load_identity(), self.root, self.chat, runtime=self.runtime)
        self._update_id = 0

    def send(self, text: str) -> None:
        self._update_id += 1
        self.app.handle_event(
            ChatEvent(cursor=self._update_id, conversation_id=42, message_id=1000 + self._update_id, text=text)
        )

    def test_conversation_turn_is_recorded_without_prompt_text(self) -> None:
        self.send(f"Tell me about {SECRET}")

        records = _records(self.root)
        self.assertTrue(records)
        self.assertEqual({record["category"] for record in records}, {"conversation"})
        self.assertEqual(records[0]["input_tokens"], 12_000)
        self.assertEqual(records[0]["provider"], "claude")
        self.assertTrue(any(SECRET in prompt for prompt in self.runtime.prompts))
        for path in usage_directory(self.root).iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(SECRET, text)
            self.assertNotIn("Reply about", text)

    def test_snapshot_and_evidence_calls_have_their_own_categories(self) -> None:
        self.runtime.respond = lambda identity, message, **kwargs: RuntimeResult(
            final_text="CONTEXT_SNAPSHOT: none", usage=_usage(500, 400, 5),
        )
        self.app._resolve_task_context_snapshot(42, "do the thing")
        self.app._respond_isolated_evidence_turn(42, "evidence prompt", phase="scan")

        categories = [record["category"] for record in _records(self.root)]
        self.assertEqual(categories, ["snapshot", "evolve"])

    def test_runtime_access_probe_is_recorded_as_other(self) -> None:
        self.app._probe_runtime_access()

        self.assertEqual([record["category"] for record in _records(self.root)], ["other"])

    def test_plain_string_reply_records_zero_usage(self) -> None:
        self.runtime.respond = lambda identity, message, **kwargs: "plain reply"

        self.app._respond_isolated_evidence_turn(42, "prompt", phase="scan")

        record = _records(self.root)[0]
        self.assertEqual(record["category"], "evolve")
        self.assertEqual(record["input_tokens"], 0)

    def test_ledger_errors_never_break_a_turn(self) -> None:
        with patch("enoch.app.core.record_runtime_usage", side_effect=RuntimeError("disk")):
            reply = self.app._respond_isolated_evidence_turn(42, "prompt", phase="scan")

        self.assertIn("Reply about", reply)
        with patch("enoch.app.core.load_usage_report", side_effect=RuntimeError("disk")):
            self.assertEqual(self.app._task_board_usage_line(), "")
            self.assertIn("could not read the usage ledger", self.app._usage_command(""))

    def test_task_and_ci_fix_categories(self) -> None:
        self.assertEqual(core._task_usage_category(_job(trigger="")), "task")
        self.assertEqual(core._task_usage_category(_job(trigger="ci-repair")), "ci-fix")

    def test_task_job_records_worker_usage_under_its_category(self) -> None:
        for trigger, expected in (("", "task"), ("ci-repair", "ci-fix")):
            with self.subTest(trigger=trigger):
                seen: list[tuple[str, int | None]] = []

                def run_work(*_args, **_kwargs):
                    seen.append((CURRENT_USAGE_CATEGORY.get(), CURRENT_TASK_ID.get()))
                    self.app._record_runtime_usage(
                        RuntimeResult(final_text="ok", usage=_usage(1000, 990, 10)),
                        CURRENT_USAGE_CATEGORY.get(),
                    )
                    return "Done."

                job = self.app.workflow.enqueue(42, f"work {trigger}", trigger=trigger)
                started = begin_next_task(self.root)
                self.assertEqual(started.id, job.id)
                with patch.object(self.app, "_run_direct_work", side_effect=run_work):
                    self.app._run_task_job(started)

                self.assertEqual(seen, [(expected, job.id)])
                record = [r for r in _records(self.root) if r["task_id"] == job.id][-1]
                self.assertEqual(record["category"], expected)
                self.assertEqual(CURRENT_USAGE_CATEGORY.get(), "")

    def test_work_turn_records_runtime_result(self) -> None:
        workflow = self.app._task_workflow
        result = RuntimeResult(final_text="worker reply", usage=_usage(7000, 6000, 70))
        token = CURRENT_USAGE_CATEGORY.set("ci-fix")
        task_token = CURRENT_TASK_ID.set(31)
        try:
            with patch("enoch.app.task_workflow.invoke_runtime_action", return_value=result), \
                 patch("enoch.app.task_workflow.record_current_task_runtime_result"), \
                 patch.object(
                     workflow, "dependencies",
                     replace(workflow.dependencies, discover_test_environment=lambda *a, **k: None),
                 ), \
                 patch.object(self.app, "_raise_if_current_task_cancelled"):
                workflow._run_work_turn(
                    42, f"request {SECRET}", context="", work_root=self.root,
                    sandbox="workspace-write", execution=core.RuntimeExecutionControl(request_id="t"),
                )
        finally:
            CURRENT_TASK_ID.reset(task_token)
            CURRENT_USAGE_CATEGORY.reset(token)

        record = _records(self.root)[-1]
        self.assertEqual((record["category"], record["task_id"]), ("ci-fix", 31))
        self.assertEqual(record["input_tokens"], 7000)
        self.assertNotIn(SECRET, day_file(date.today(), self.root).read_text(encoding="utf-8"))

    def _run_task_with_usage(self) -> str:
        def run_work(*_args, **_kwargs):
            self.app._record_runtime_usage(
                RuntimeResult(final_text="ok", usage=_usage(8_000_000, 7_900_000, 60_000)),
                "task",
            )
            self.app._record_runtime_usage(
                RuntimeResult(final_text="ok", usage=_usage(100_000, 50_000, 2_000)),
                "task",
            )
            return "Done with the task."

        with patch.object(
            EnochApplication,
            "_resolve_task_context_snapshot",
            return_value=TaskContextSnapshot(context="brief", source="chat-snapshot"),
        ):
            self.send("/task add usage")
        job = begin_next_task(self.root)
        with patch.object(self.app, "_run_direct_work", side_effect=run_work):
            self.app._run_task_job(job)
        return self.chat.sent[-1][1]

    def test_final_message_has_tokens_line(self) -> None:
        final = self._run_task_with_usage()

        self.assertTrue(final.startswith("Task #1 final update"), final)
        self.assertEqual(final.splitlines()[-1], "Tokens: 8.1M in (98% cached) · 62k out")

    def test_compact_final_message_has_tokens_line(self) -> None:
        save_task_board_state(
            TaskBoardState(enabled=True, chat_id=42, message_id=9999, last_digest="x"), self.root,
        )

        final = self._run_task_with_usage()

        self.assertTrue(final.startswith("✅ #1 completed"), final)
        self.assertEqual(final.splitlines()[-1], "Tokens: 8.1M in (98% cached) · 62k out")

    def test_final_message_falls_back_to_job_usage_and_omits_unknown(self) -> None:
        job = _job(
            runtime_usage={"input_tokens": 2_000_000, "cached_input_tokens": 1_000_000, "output_tokens": 5},
        )
        self.assertEqual(self.app._task_tokens_line(job), "Tokens: 2.0M in (50% cached) · 5 out")
        self.assertEqual(self.app._task_tokens_line(_job()), "")
        self.assertNotIn("Tokens:", self.app._format_task_final(_job(), "completed", "Done."))

    def test_board_shows_today_line(self) -> None:
        record_runtime_usage(self.root, category="task", usage=_usage(3_400_000, output=150_000))
        record_runtime_usage(self.root, category="conversation", usage=_usage(600_000))

        self.assertEqual(
            self.app._task_board_usage_line(),
            "Today: 4M in · 0.1M out (tasks 85%, chat 15%)",
        )
        self.assertIn("Today: 4M in", self.app._render_task_board().text)

    def test_usage_command_today_and_seven_days(self) -> None:
        job = self.app.workflow.enqueue(42, "Make token usage visible")
        now = datetime.now().astimezone()
        record_runtime_usage(self.root, category="task", usage=_usage(5_000_000, 4_900_000, 40_000), task_id=job.id)
        record_runtime_usage(self.root, category="conversation", usage=_usage(40_000, 30_000, 500))
        record_runtime_usage(
            self.root, category="ci-fix", usage=_usage(900_000), task_id=99, now=now - timedelta(days=3),
        )

        self.send("/usage")
        today = self.chat.sent[-1][1]
        self.send("/usage 7d")
        week = self.chat.sent[-1][1]
        self.send("/usage forever")
        invalid = self.chat.sent[-1][1]

        self.assertIn("Token usage, today", today)
        self.assertIn("- task: 5.0M in (98% cached)", today)
        self.assertIn(f"#{job.id} Make token usage visible: 5.0M in", today)
        self.assertIn("Conversation turns: 1 · average 40k in", today)
        self.assertNotIn("ci-fix", today)
        self.assertIn("Token usage, last 7 days", week)
        self.assertIn("- ci-fix: 900k in", week)
        self.assertIn("#99: 900k in", week)
        self.assertIn("/usage [today|7d]", invalid)

    def test_usage_is_in_help(self) -> None:
        self.assertIn("/usage [today|7d]", help_message())
        self.assertIn("private usage ledger", help_message("usage"))


def _job(**changes) -> TaskJob:
    values = {"id": 41, "chat_id": 42, "text": "Simplify", "created_at": "2026-11-24T13:00:00+00:00"}
    values.update(changes)
    return TaskJob(**values)


if __name__ == "__main__":
    unittest.main()
