from pathlib import Path
from datetime import datetime, timezone
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.evolve import (
    MODE_DISABLED,
    claim_due_evolve_schedule,
    collect_evolve_candidates,
    disable_evolve_schedule,
    evolve_report,
    load_evolve_candidates,
    load_evolve_state,
    rank_evolve_candidates,
    reject_evolve_candidate,
    select_evolve_candidate,
    set_evolve_cron_schedule,
    set_evolve_daily_schedule,
    set_evolve_schedule,
    set_evolve_mode,
    set_evolve_theme,
)
from enoch.lineage.core import LineageCandidate


class EnochEvolveTests(unittest.TestCase):
    def test_default_state_is_co_evolve(self) -> None:
        with TemporaryDirectory() as temp:
            state = load_evolve_state(Path(temp))

        self.assertEqual(state.mode, "co-evolve")
        self.assertEqual(state.theme, "")

    def test_collects_backlog_and_parent_inheritance_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            _write_lineage_candidate(root, _lineage_candidate())

            candidates = collect_evolve_candidates(root)
            ranked = rank_evolve_candidates(candidates, theme="Telegram UX")

        self.assertEqual({candidate.source for candidate in candidates}, {"backlog", "inheritance"})
        self.assertEqual(ranked[0].source, "backlog")
        self.assertIn("Telegram", ranked[0].title)

    def test_disabled_mode_does_not_collect_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "do this later", root, priority="p0")
            set_evolve_mode(MODE_DISABLED, root)

            report = evolve_report(root)

        self.assertEqual(report.state.mode, MODE_DISABLED)
        self.assertEqual(report.candidates, ())
        self.assertIsNone(report.top_candidate)

    def test_theme_is_persisted(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            state = set_evolve_theme(" improve Telegram work UX ", root)
            loaded = load_evolve_state(root)

        self.assertEqual(state.theme, "improve Telegram work UX")
        self.assertEqual(loaded.theme, "improve Telegram work UX")

    def test_candidate_status_is_persisted_across_reports(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "low value cleanup", root, priority="p2")
            add_backlog_item(2, "important Telegram recovery", root, priority="p0")

            selected = select_evolve_candidate("backlog-1", root)
            report = evolve_report(root)
            rejected = reject_evolve_candidate("backlog-1", root)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        self.assertEqual(selected.status, "selected")
        self.assertEqual(report.top_candidate.id, "backlog-1")
        self.assertEqual(report.top_candidate.status, "selected")
        self.assertEqual(rejected.status, "rejected")
        self.assertNotIn("backlog-1", {candidate.id for candidate in visible})
        self.assertIn("backlog-1", {candidate.id for candidate in all_candidates})

    def test_schedule_can_be_set_claimed_and_disabled(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            start = datetime(2020, 1, 1, tzinfo=timezone.utc)
            due = datetime(2020, 1, 2, tzinfo=timezone.utc)

            scheduled = set_evolve_schedule(86400, root, now=start)
            before_due = claim_due_evolve_schedule(root, now=datetime(2020, 1, 1, 23, tzinfo=timezone.utc))
            claimed = claim_due_evolve_schedule(root, now=due)
            claimed_again = claim_due_evolve_schedule(root, now=due)
            disabled = disable_evolve_schedule(root)

        self.assertTrue(scheduled.schedule_enabled)
        self.assertEqual(scheduled.schedule_interval_seconds, 86400)
        self.assertEqual(scheduled.schedule_next_run_at, "2020-01-02T00:00:00+00:00")
        self.assertIsNone(before_due)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.schedule_next_run_at, "2020-01-02T00:00:00+00:00")
        self.assertIsNone(claimed_again)
        self.assertFalse(disabled.schedule_enabled)

    def test_daily_schedule_uses_next_local_time(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            start = datetime(2020, 1, 1, 8, 30, tzinfo=timezone.utc)
            due = datetime(2020, 1, 1, 9, 0, tzinfo=timezone.utc)

            scheduled = set_evolve_daily_schedule("9:00", root, now=start)
            claimed = claim_due_evolve_schedule(root, now=due)
            state_after_claim = load_evolve_state(root)

        self.assertEqual(scheduled.schedule_daily_time, "09:00")
        self.assertEqual(scheduled.schedule_interval_seconds, 86400)
        self.assertEqual(scheduled.schedule_next_run_at, "2020-01-01T09:00:00+00:00")
        self.assertIsNotNone(claimed)
        self.assertEqual(state_after_claim.schedule_next_run_at, "2020-01-02T09:00:00+00:00")

    def test_daily_schedule_rejects_invalid_time(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "HH:MM"):
                set_evolve_daily_schedule("tomorrow morning", Path(temp))

    def test_cron_schedule_uses_daily_cron_expression(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            start = datetime(2020, 1, 1, 8, 30, tzinfo=timezone.utc)
            due = datetime(2020, 1, 1, 9, 30, tzinfo=timezone.utc)

            scheduled = set_evolve_cron_schedule("30 9 * * *", root, now=start)
            claimed = claim_due_evolve_schedule(root, now=due)
            state_after_claim = load_evolve_state(root)

        self.assertEqual(scheduled.schedule_cron_expression, "30 9 * * *")
        self.assertEqual(scheduled.schedule_next_run_at, "2020-01-01T09:30:00+00:00")
        self.assertIsNotNone(claimed)
        self.assertEqual(state_after_claim.schedule_next_run_at, "2020-01-02T09:30:00+00:00")

    def test_cron_schedule_rejects_non_daily_expression(self) -> None:
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "daily expressions"):
                set_evolve_cron_schedule("30 9 * * 1", Path(temp))


def _write_lineage_candidate(root: Path, candidate: LineageCandidate) -> None:
    lineage = root / ".agent" / "lineage.yaml"
    lineage.parent.mkdir(parents=True)
    lineage.write_text("parent:\n  name: Seth\n  repo: our-ark/enoch\n", encoding="utf-8")
    inbox = root / ".agent" / "lineage_inbox.json"
    inbox.write_text(json.dumps({"schema_version": 1, "candidates": [candidate.__dict__]}), encoding="utf-8")


def _lineage_candidate() -> LineageCandidate:
    return LineageCandidate(
        id="our-ark/enoch#32",
        repo="our-ark/enoch",
        pr_number=32,
        title="Add Telegram recovery command",
        url="https://github.com/our-ark/enoch/pull/32",
        merged_at="2026-06-17T01:31:12Z",
        merge_commit="abc123",
        ancestor_name="Seth",
        depth=1,
        labels=("inherit:recommended",),
        files=("src/enoch/telegram/bot.py",),
        relevance="high",
        confidence="high",
        reason="PR has an inheritance label.",
        body_excerpt="Adds a recovery command.",
    )


if __name__ == "__main__":
    unittest.main()
