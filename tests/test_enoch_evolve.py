from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.automatic_learning import record_learning_artifact
from enoch.evolution.sources.brainstorming import generate_brainstorm_ideas
from enoch.cron import add_cron_job
from enoch.evolution.core import (
    MODE_DISABLED,
    cancel_evolve_candidate_for_task,
    claim_due_evolve_schedule,
    collect_evolve_candidates,
    collect_experience_candidates,
    disable_evolve_schedule,
    evolve_report,
    complete_evolve_candidate_for_task,
    fail_evolve_candidate_for_task,
    latest_failed_evolve_task,
    load_evolve_candidates,
    load_evolve_state,
    propose_evolve,
    rank_evolve_candidates,
    remove_evolve_candidate,
    retry_evolve_candidate,
    run_evolve_candidate,
    set_evolve_cron_schedule,
    set_evolve_daily_schedule,
    set_evolve_schedule,
    set_evolve_mode,
    set_evolve_theme,
    sync_evolve_candidates,
)
from enoch.evolution.events import load_evolve_events
from enoch.evolution.sources.experience import record_task_experience
from enoch.identity import load_identity
from enoch.learn import LearnRequest, record_peer_learning_observation
from enoch.lineage.core import LineageCandidate
from enoch.logs import log_conversation_turn
from enoch.tasks.queue import TaskJob, begin_next_task, enqueue_task, fail_task


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
        initiators = {candidate.source: candidate.initiated_by for candidate in candidates}
        self.assertEqual(set(initiators.values()), {"agent"})
        signals = {candidate.source: candidate.signal_actor for candidate in candidates}
        self.assertEqual(signals["backlog"], "human")
        self.assertEqual(signals["feedback"], "human")
        self.assertEqual(signals["experience"], "system")
        self.assertEqual(signals["inheritance"], "agent")
        self.assertEqual(signals["learning"], "human")
        self.assertEqual(signals["brainstorming"], "agent")
        self.assertTrue(all(candidate.candidate_actor == "agent" for candidate in candidates))
        self.assertTrue(all(candidate.evidence_source == candidate.source for candidate in candidates))

    def test_experience_candidate_links_to_failed_evolve_candidate_and_task(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(
                42,
                "apply feedback candidate",
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
            fail_task(queued.id, root, result="Worktree branch failed.")

            candidate = next(
                item
                for item in collect_experience_candidates(root)
                if item.id == f"task-{queued.id}"
            )

        self.assertEqual(candidate.evidence_source, "experience")
        self.assertEqual(candidate.signal_actor, "system")
        self.assertEqual(candidate.candidate_actor, "agent")
        self.assertEqual(candidate.parent_candidate_id, "feedback-c3ed71fd1d2d")
        self.assertEqual(candidate.source_task_id, queued.id)

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

    def test_legacy_feedback_candidate_infers_split_provenance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".enoch" / "evolve_candidates.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "candidates": [
                            {
                                "id": "feedback-legacy",
                                "source": "feedback",
                                "title": "Legacy feedback",
                                "initiated_by": "human",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            candidate = load_evolve_candidates(root, include_inactive=True)[0]

        self.assertEqual(candidate.evidence_source, "feedback")
        self.assertEqual(candidate.signal_actor, "human")
        self.assertEqual(candidate.candidate_actor, "agent")
        self.assertEqual(candidate.initiated_by, "agent")

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

    def test_proposal_does_not_brainstorm_when_candidate_exists(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "existing work", root, priority="p1")
            calls = []

            proposal = propose_evolve(root, brainstormer=lambda theme: calls.append(theme) or ())

        self.assertEqual(calls, [])
        self.assertFalse(proposal.brainstorm_attempted)
        self.assertEqual(proposal.top_candidate.source, "backlog")

    def test_proposal_does_not_brainstorm_while_candidate_is_running(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "running evolve work", root, priority="p1")
            set_evolve_theme("reliable evolution", root)
            run_evolve_candidate("backlog-1", root, theme="reliable evolution")
            calls = []

            proposal = propose_evolve(root, brainstormer=lambda theme: calls.append(theme) or ())

        self.assertEqual(calls, [])
        self.assertFalse(proposal.brainstorm_attempted)
        self.assertEqual(proposal.brainstorm_skip_reason, "candidate-running")

    def test_empty_proposal_requires_theme_before_fallback_brainstorm(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []

            proposal = propose_evolve(root, brainstormer=lambda theme: calls.append(theme) or ())

        self.assertEqual(calls, [])
        self.assertFalse(proposal.brainstorm_attempted)
        self.assertEqual(proposal.brainstorm_skip_reason, "theme-not-set")

    def test_empty_proposal_brainstorms_once_per_theme_per_day(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            set_evolve_theme("reliable task telemetry", root)
            calls = []

            def brainstorm(theme: str):
                calls.append(theme)
                title = f"Improve telemetry fallback {len(calls)}"
                response = json.dumps(
                    [
                        {
                            "title": title,
                            "rationale": "No stronger candidate exists.",
                            "proposed_change": "Add a bounded telemetry improvement.",
                            "expected_benefit": "Keeps evolution moving.",
                            "risk": "The idea may be speculative.",
                            "test_plan": "Add focused tests.",
                        }
                    ]
                )
                return generate_brainstorm_ideas(
                    theme,
                    root,
                    mission="Evolve safely",
                    generator=lambda _prompt: response,
                )

            start = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)
            first = propose_evolve(root, brainstormer=brainstorm, now=start)
            assert first.top_candidate is not None
            remove_evolve_candidate(first.top_candidate.id, root, theme="reliable task telemetry")
            second = propose_evolve(root, brainstormer=brainstorm, now=start + timedelta(hours=1))
            third = propose_evolve(root, brainstormer=brainstorm, now=start + timedelta(hours=25))

        self.assertTrue(first.brainstorm_attempted)
        self.assertEqual(first.brainstorm_added, 1)
        self.assertEqual(first.top_candidate.source, "brainstorming")
        self.assertEqual(first.top_candidate.initiated_by, "agent")
        self.assertFalse(second.brainstorm_attempted)
        self.assertEqual(second.brainstorm_skip_reason, "cooldown")
        self.assertTrue(third.brainstorm_attempted)
        self.assertEqual(len(calls), 2)

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
            job = replace(
                job,
                runtime_provider="codex",
                runtime_session_id="session-7",
                runtime_completion_reason="completed",
                runtime_usage={"input_tokens": 100, "output_tokens": 25},
                runtime_event_types=("turn.completed",),
            )

            completed = complete_evolve_candidate_for_task(job, root)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)
            events = load_evolve_events(root, task_id=job.id)

        assert completed is not None
        self.assertEqual(completed.status, "done")
        self.assertNotIn("backlog-1", {candidate.id for candidate in visible})
        self.assertEqual(all_candidates[0].id, "backlog-1")
        self.assertEqual(all_candidates[0].status, "done")
        self.assertEqual([event.event for event in events], ["completed"])
        self.assertEqual(events[0].event_actor, "agent")
        self.assertEqual(events[0].trigger, "task-runner")
        self.assertEqual(events[0].runtime_provider, "codex")
        self.assertEqual(events[0].runtime_session_id, "session-7")
        self.assertEqual(events[0].runtime_usage["output_tokens"], 25)

    def test_failed_candidate_stays_retryable_while_cancelled_candidate_is_inactive(self) -> None:
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
            failed_events = load_evolve_events(root, task_id=failed_job.id)
            cancelled_events = load_evolve_events(root, task_id=cancelled_job.id)

        assert failed is not None
        assert cancelled is not None
        self.assertEqual(failed.status, "failed")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(
            [(candidate.id, candidate.status) for candidate in visible],
            [("backlog-1", "failed")],
        )
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses["backlog-1"], "failed")
        self.assertEqual(statuses["backlog-2"], "cancelled")
        self.assertEqual(failed_events[0].event, "failed")
        self.assertEqual(cancelled_events[0].event, "cancelled")

    def test_failed_candidate_remains_proposable_and_can_retry(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(1, "ship retryable evolve work", root, priority="p1")
            run_evolve_candidate("backlog-1", root)
            queued = enqueue_task(
                42,
                "Evolve approved candidate backlog-1",
                root,
                context="\n".join(["Evolve candidate context:", "ID: backlog-1"]),
                context_source="evolve-approve",
                source="backlog",
                candidate_id="backlog-1",
            )
            running = begin_next_task(root)
            assert running is not None
            fail_task(running.id, root, result="Transient branch setup failure.")
            failed = fail_evolve_candidate_for_task(running, root, reason=running.result)

            proposal = propose_evolve(
                root,
                brainstormer=lambda _theme: self.fail("failed candidate should prevent fallback brainstorming"),
            )
            failed_task = latest_failed_evolve_task("backlog-1", root)
            retried = retry_evolve_candidate("backlog-1", root)

        assert failed is not None
        assert proposal.top_candidate is not None
        assert failed_task is not None
        self.assertEqual(proposal.top_candidate.id, "backlog-1")
        self.assertEqual(proposal.top_candidate.status, "failed")
        self.assertFalse(proposal.brainstorm_attempted)
        self.assertEqual(failed_task.id, queued.id)
        self.assertEqual(retried.status, "running")

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
