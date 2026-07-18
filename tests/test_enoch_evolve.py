from pathlib import Path
from datetime import datetime, timezone
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.automatic_learning import record_learning_artifact
from enoch.brainstorming import generate_brainstorm_ideas
from enoch.cron import add_cron_job
from enoch.evolve import (
    MODE_DISABLED,
    cancel_evolve_candidate_for_task,
    claim_due_evolve_schedule,
    collect_evolve_candidates,
    disable_evolve_schedule,
    evolve_report,
    complete_evolve_candidate_for_task,
    fail_evolve_candidate_for_task,
    load_evolve_candidates,
    load_evolve_state,
    propose_evolve,
    rank_evolve_candidates,
    remove_evolve_candidate,
    run_evolve_candidate,
    set_evolve_cron_schedule,
    set_evolve_daily_schedule,
    set_evolve_schedule,
    set_evolve_mode,
    set_evolve_theme,
    sync_evolve_candidates,
)
from enoch.experience import record_task_experience
from enoch.identity import load_identity
from enoch.learn import LearnRequest, record_peer_learning_observation
from enoch.lineage.core import LineageCandidate
from enoch.logs import log_conversation_turn
from enoch.task_queue import TaskJob, begin_next_task, enqueue_task, fail_task


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

    def test_collects_operational_and_learning_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship flaky workflow", root)
            running = begin_next_task(root)
            assert running is not None
            fail_task(queued.id, root, result="Tests failed in Telegram workflow.")
            add_cron_job(42, "summarize health", 24 * 60 * 60, root)
            record_learning_artifact(
                load_identity(),
                request="add a notes skill",
                result="\n".join(["Files:", "- src/enoch/skills/notes/SKILL.md"]),
                root=root,
                task_id=7,
                command="/task",
            )

            candidates = collect_evolve_candidates(root)
            report = evolve_report(root)

        sources = {candidate.source for candidate in candidates}
        self.assertEqual(sources, {"experience"})
        self.assertEqual(report.counts_by_source["experience"], 3)

    def test_repeated_successful_experiences_become_one_reuse_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            for task_id in (1, 2):
                record_task_experience(
                    _experience_task(task_id, "summarize repository health"),
                    root,
                    command="/task",
                )

            candidates = collect_evolve_candidates(root)

        repeated = [candidate for candidate in candidates if candidate.id.startswith("experience-repeat-")]
        self.assertEqual(len(repeated), 1)
        self.assertIn("completing successfully 2 times", repeated[0].rationale)

    def test_unstarted_cancellation_is_journaled_but_not_an_evolve_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record_task_experience(
                TaskJob(
                    id=3,
                    chat_id=42,
                    text="cancel this before it starts",
                    created_at="2026-07-18T00:00:00+00:00",
                    completed_at="2026-07-18T00:01:00+00:00",
                    status="cancelled",
                    result="Cancelled before running.",
                ),
                root,
                command="/task cancel",
            )

            candidates = collect_evolve_candidates(root)

        self.assertNotIn("task-3", {candidate.id for candidate in candidates})

    def test_collects_exactly_the_six_declared_evolution_sources(self) -> None:
        brainstorm_response = json.dumps(
            [
                {
                    "title": "Make provenance visible",
                    "rationale": "The theme emphasizes accountability.",
                    "proposed_change": "Show source details in candidate reports.",
                    "expected_benefit": "Improves review quality.",
                    "risk": "Adds output.",
                    "test_plan": "Add report tests.",
                }
            ]
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            log_conversation_turn(
                chat_id=42,
                message="No, keep candidate provenance in the report.",
                reply="Understood.",
                root=root,
            )
            _write_lineage_candidate(root, _lineage_candidate())
            queued = enqueue_task(42, "ship flaky workflow", root)
            begin_next_task(root)
            fail_task(queued.id, root, result="Tests failed.")
            record_peer_learning_observation(LearnRequest(skill="research", agent="enosh"), root)
            generate_brainstorm_ideas(
                "accountable evolution",
                root,
                mission="Evolve safely",
                generator=lambda _prompt: brainstorm_response,
            )

            candidates = collect_evolve_candidates(root, theme="accountable evolution")

        self.assertEqual(
            {candidate.source for candidate in candidates},
            {"backlog", "feedback", "experience", "inheritance", "learning", "brainstorming"},
        )

    def test_brainstorm_candidates_are_scoped_to_current_theme(self) -> None:
        response = json.dumps(
            [
                {
                    "title": "Improve audit trail",
                    "rationale": "Useful for theme A.",
                    "proposed_change": "Show provenance.",
                    "expected_benefit": "Better review.",
                    "risk": "More output.",
                    "test_plan": "Add formatting tests.",
                }
            ]
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            generate_brainstorm_ideas(
                "theme A",
                root,
                mission="Evolve safely",
                generator=lambda _prompt: response,
            )
            first = sync_evolve_candidates(root, theme="theme A")
            second = sync_evolve_candidates(root, theme="theme B")

        self.assertEqual({candidate.source for candidate in first}, {"brainstorming"})
        self.assertEqual(second, ())

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

    def test_removed_candidate_status_is_persisted_across_reports(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "low value cleanup", root, priority="p2")

            removed = remove_evolve_candidate("backlog-1", root)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        self.assertEqual(removed.status, "removed")
        self.assertNotIn("backlog-1", {candidate.id for candidate in visible})
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses["backlog-1"], "removed")

    def test_legacy_selected_and_rejected_statuses_are_migrated(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".enoch" / "evolve_candidates.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "candidates": [
                            {"id": "legacy-selected", "title": "Selected", "status": "selected"},
                            {"id": "legacy-rejected", "title": "Rejected", "status": "rejected"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            candidates = load_evolve_candidates(root, include_inactive=True)

        statuses = {candidate.id: candidate.status for candidate in candidates}
        self.assertEqual(statuses["legacy-selected"], "candidate")
        self.assertEqual(statuses["legacy-rejected"], "removed")

    def test_run_candidate_marks_it_running(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "ship evolve run", root, priority="p1")

            running = run_evolve_candidate("backlog-1", root)
            visible = load_evolve_candidates(root)

        self.assertEqual(running.status, "running")
        self.assertEqual(visible[0].id, "backlog-1")
        self.assertEqual(visible[0].status, "running")

    def test_proposal_skips_running_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "lower priority candidate", root, priority="p2")
            add_backlog_item(2, "highest priority candidate", root, priority="p0")
            run_evolve_candidate("backlog-2", root)

            proposal = propose_evolve(root)

        self.assertEqual([candidate.id for candidate in proposal.candidates], ["backlog-1"])
        assert proposal.top_candidate is not None
        self.assertEqual(proposal.top_candidate.id, "backlog-1")
        self.assertEqual(proposal.top_candidate.status, "candidate")

    def test_completed_evolve_task_marks_candidate_done(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "ship evolve completion", root, priority="p1")
            run_evolve_candidate("backlog-1", root)
            job = enqueue_task(
                42,
                "Evolve approved candidate backlog-1",
                root,
                context="\n".join(["Evolve candidate context:", "ID: backlog-1"]),
                context_source="evolve-approve",
            )

            completed = complete_evolve_candidate_for_task(job, root)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        assert completed is not None
        self.assertEqual(completed.status, "done")
        self.assertNotIn("backlog-1", {candidate.id for candidate in visible})
        self.assertEqual(all_candidates[0].id, "backlog-1")
        self.assertEqual(all_candidates[0].status, "done")

    def test_failed_and_cancelled_evolve_tasks_mark_candidates_inactive(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "ship failing evolve", root, priority="p1")
            add_backlog_item(2, "ship cancelled evolve", root, priority="p1")
            run_evolve_candidate("backlog-1", root)
            run_evolve_candidate("backlog-2", root)
            failed_job = enqueue_task(
                42,
                "Evolve approved candidate backlog-1",
                root,
                context="\n".join(["Evolve candidate context:", "ID: backlog-1"]),
                context_source="evolve-approve",
            )
            cancelled_job = enqueue_task(
                42,
                "Evolve approved candidate backlog-2",
                root,
                context="\n".join(["Evolve candidate context:", "ID: backlog-2"]),
                context_source="evolve-approve",
            )

            failed = fail_evolve_candidate_for_task(failed_job, root)
            cancelled = cancel_evolve_candidate_for_task(cancelled_job, root)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        assert failed is not None
        assert cancelled is not None
        self.assertEqual(failed.status, "failed")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(visible, ())
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses["backlog-1"], "failed")
        self.assertEqual(statuses["backlog-2"], "cancelled")

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


def _experience_task(task_id: int, text: str) -> TaskJob:
    return TaskJob(
        id=task_id,
        chat_id=42,
        text=text,
        created_at=f"2026-07-18T00:0{task_id}:00+00:00",
        started_at=f"2026-07-18T00:0{task_id}:10+00:00",
        completed_at=f"2026-07-18T00:0{task_id}:20+00:00",
        status="completed",
        result="Completed successfully.",
    )


if __name__ == "__main__":
    unittest.main()
