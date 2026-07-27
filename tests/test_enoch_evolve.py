from pathlib import Path
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.evolution.sources.brainstorming import generate_brainstorm_ideas
from enoch.evolution.core import (
    MODE_DISABLED,
    cancel_evolve_candidate_for_task,
    claim_due_evolve_schedule,
    acknowledge_evolve_schedule,
    collect_evolve_candidates,
    disable_evolve_schedule,
    evolve_report,
    complete_evolve_candidate_for_task,
    create_learning_candidate,
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
    synthesize_evolve_candidates_from_evidence,
)
from enoch.evolution.evidence import scan_evidence
from enoch.evolution.events import load_evolve_events
from enoch.learn import LearningCandidateDraft, PublishedSkill
from enoch.lineage.core import LineageCandidate
from enoch.tasks.events import record_task_event
from enoch.tasks.queue import TaskJob, begin_next_task, enqueue_task, fail_task


class EnochEvolveTests(unittest.TestCase):
    def test_default_state_is_co_evolve(self) -> None:
        with TemporaryDirectory() as temp:
            state = load_evolve_state(Path(temp))

        self.assertEqual(state.mode, "co-evolve")
        self.assertEqual(state.theme, "")

    def test_backlog_and_inheritance_are_not_evolution_evidence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            add_backlog_item(42, "improve Telegram work UX", root, priority="p0")
            _write_lineage_candidate(root, _lineage_candidate())

            candidates = collect_evolve_candidates(root)
        self.assertEqual(candidates, ())

    def test_task_failures_are_pending_evidence_not_hardcoded_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship flaky workflow", root)
            running = begin_next_task(root)
            assert running is not None
            fail_task(queued.id, root, result="Tests failed in Telegram workflow.")

            candidates = collect_evolve_candidates(root)
            report = evolve_report(root)

        self.assertEqual(candidates, ())
        self.assertNotIn("experience", report.counts_by_source)
        self.assertEqual(report.pending_evidence["experience"], 1)

    def test_repeated_successes_do_not_become_hardcoded_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_task_events(root, task_id=1, status="completed")
            _write_task_events(root, task_id=2, status="completed")

            candidates = collect_evolve_candidates(root)

        self.assertEqual(candidates, ())

    def test_unstarted_cancellation_is_not_a_hardcoded_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_task_events(root, task_id=3, status="cancelled")

            candidates = collect_evolve_candidates(root)

        self.assertNotIn("task-3", {candidate.id for candidate in candidates})

    def test_direct_pathways_remain_separate_candidate_sources(self) -> None:
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
            _write_lineage_candidate(root, _lineage_candidate())
            learning, created = create_learning_candidate(
                _learning_skill(),
                LearningCandidateDraft(
                    title="Adapt peer research summaries",
                    rationale="Enosh has a portable research synthesis capability.",
                    proposed_change="Add a bounded research summary adapter.",
                    expected_benefit="Improves research handoffs.",
                    risk="Source assumptions may not fit Enoch.",
                    test_plan="Add focused adapter tests.",
                ),
                root,
            )
            generate_brainstorm_ideas(
                "accountable evolution",
                root,
                mission="Evolve safely",
                generator=lambda _prompt: brainstorm_response,
            )

            candidates = evolve_report(root).candidates

        self.assertTrue(created)
        self.assertEqual(learning.source_revision, "a" * 40)
        self.assertEqual(
            {candidate.source for candidate in candidates},
            {"learning", "brainstorming"},
        )
        initiators = {candidate.source: candidate.initiated_by for candidate in candidates}
        self.assertEqual(set(initiators.values()), {"agent"})
        signals = {candidate.source: candidate.signal_actor for candidate in candidates}
        self.assertEqual(signals["learning"], "human")
        self.assertEqual(signals["brainstorming"], "agent")
        self.assertTrue(all(candidate.candidate_actor == "agent" for candidate in candidates))
        self.assertTrue(all(candidate.evidence_source == candidate.source for candidate in candidates))

    def test_learning_candidate_deduplicates_exact_skill_snapshot(self) -> None:
        draft = LearningCandidateDraft(
            title="Adapt peer research summaries",
            rationale="The capability is missing.",
            proposed_change="Add a bounded research summary adapter.",
            expected_benefit="Improves research handoffs.",
            risk="Source assumptions may differ.",
            test_plan="Add focused adapter tests.",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)

            first, first_created = create_learning_candidate(
                _learning_skill(),
                draft,
                root,
            )
            second, second_created = create_learning_candidate(
                _learning_skill(),
                draft,
                root,
            )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.source_repository, "our-ark/enosh")
        self.assertEqual(first.source_path, "src/enosh/skills/research")

    def test_semantic_experience_candidate_keeps_task_causal_chain(self) -> None:
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

            scan = scan_evidence(
                "experience",
                root,
                force=True,
                generator=lambda prompt: _experience_evidence_response(
                    prompt,
                    queued.id,
                ),
            )
            created = synthesize_evolve_candidates_from_evidence(
                root,
                mission="Improve Enoch safely.",
                generator=lambda _prompt: json.dumps(
                    [
                        {
                            "evidence_ids": [scan.evidence[0].id],
                            "title": "Harden task worktree setup",
                            "rationale": "The failed task records a durable setup failure.",
                            "proposed_change": "Add a focused worktree setup guardrail.",
                            "expected_benefit": "Fewer setup failures.",
                            "risk": "May reject an unusual valid state.",
                            "test_plan": "Add a focused task setup regression test.",
                        }
                    ]
                ),
            )
            candidate = created[0]

        self.assertEqual(candidate.evidence_source, "experience")
        self.assertEqual(candidate.signal_actor, "system")
        self.assertEqual(candidate.candidate_actor, "agent")
        self.assertEqual(candidate.parent_candidate_id, "feedback-c3ed71fd1d2d")
        self.assertEqual(candidate.source_task_id, queued.id)
        self.assertEqual(candidate.evidence_ids, (scan.evidence[0].id,))

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
            candidate = _feedback_candidate(root, "I want less low value cleanup.")

            removed = remove_evolve_candidate(candidate.id, root)
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        self.assertEqual(removed.status, "removed")
        self.assertNotIn(candidate.id, {item.id for item in visible})
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses[candidate.id], "removed")

    def test_stored_actionable_backlog_candidate_is_retired(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_stored_candidate(root, candidate_id="backlog-7", source="backlog")

            visible = sync_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)
            events = load_evolve_events(root, candidate_id="backlog-7")

        self.assertNotIn("backlog-7", {candidate.id for candidate in visible})
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses["backlog-7"], "removed")
        self.assertEqual([event.event for event in events], ["removed"])
        self.assertEqual(events[0].event_actor, "system")
        self.assertEqual(events[0].trigger, "candidate-source-retirement")
        self.assertEqual(events[0].reason, "backlog-is-not-evolution-evidence")

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
            candidate = _feedback_candidate(root, "I want a clearer evolve run.")

            running = run_evolve_candidate(candidate.id, root)
            visible = load_evolve_candidates(root)

        self.assertEqual(running.status, "running")
        self.assertEqual(visible[0].id, candidate.id)
        self.assertEqual(visible[0].status, "running")

    def test_proposal_skips_running_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = _feedback_candidate(root, "I want a clearer lower priority candidate.")
            second = _feedback_candidate(root, "I want a clearer highest priority candidate.")
            run_evolve_candidate(second.id, root)

            proposal = propose_evolve(root)

        self.assertEqual([candidate.id for candidate in proposal.candidates], [first.id])
        assert proposal.top_candidate is not None
        self.assertEqual(proposal.top_candidate.id, first.id)
        self.assertEqual(proposal.top_candidate.status, "candidate")

    def test_proposal_does_not_brainstorm_when_candidate_exists(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _feedback_candidate(root, "I want clearer existing work.")
            calls = []

            proposal = propose_evolve(root, brainstormer=lambda theme: calls.append(theme) or ())

        self.assertEqual(calls, [])
        self.assertFalse(proposal.brainstorm_attempted)
        self.assertEqual(proposal.top_candidate.source, "feedback")

    def test_proposal_does_not_brainstorm_while_candidate_is_running(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = _feedback_candidate(root, "I want clearer running evolve work.")
            set_evolve_theme("reliable evolution", root)
            run_evolve_candidate(candidate.id, root, theme="reliable evolution")
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
            candidate = _feedback_candidate(root, "I want clearer evolve completion.")
            run_evolve_candidate(candidate.id, root)
            job = enqueue_task(
                42,
                f"Evolve approved candidate {candidate.id}",
                root,
                context="\n".join(["Evolve candidate context:", f"ID: {candidate.id}"]),
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
        self.assertNotIn(candidate.id, {item.id for item in visible})
        self.assertEqual(all_candidates[0].id, candidate.id)
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
            failed_candidate = _feedback_candidate(root, "I want clearer failing evolve work.")
            cancelled_candidate = _feedback_candidate(root, "I want clearer cancelled evolve work.")
            run_evolve_candidate(failed_candidate.id, root)
            run_evolve_candidate(cancelled_candidate.id, root)
            failed_job = enqueue_task(
                42,
                f"Evolve approved candidate {failed_candidate.id}",
                root,
                context="\n".join(["Evolve candidate context:", f"ID: {failed_candidate.id}"]),
                context_source="evolve-approve",
            )
            cancelled_job = enqueue_task(
                42,
                f"Evolve approved candidate {cancelled_candidate.id}",
                root,
                context="\n".join(["Evolve candidate context:", f"ID: {cancelled_candidate.id}"]),
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
            [(failed_candidate.id, "failed")],
        )
        statuses = {candidate.id: candidate.status for candidate in all_candidates}
        self.assertEqual(statuses[failed_candidate.id], "failed")
        self.assertEqual(statuses[cancelled_candidate.id], "cancelled")
        self.assertEqual(failed_events[0].event, "failed")
        self.assertEqual(cancelled_events[0].event, "cancelled")

    def test_failed_candidate_remains_proposable_and_can_retry(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = _feedback_candidate(root, "I want clearer retryable evolve work.")
            run_evolve_candidate(candidate.id, root)
            queued = enqueue_task(
                42,
                f"Evolve approved candidate {candidate.id}",
                root,
                context="\n".join(["Evolve candidate context:", f"ID: {candidate.id}"]),
                context_source="evolve-approve",
                source="feedback",
                candidate_id=candidate.id,
            )
            running = begin_next_task(root)
            assert running is not None
            fail_task(running.id, root, result="Transient branch setup failure.")
            failed = fail_evolve_candidate_for_task(running, root, reason=running.result)

            proposal = propose_evolve(
                root,
                brainstormer=lambda _theme: self.fail("failed candidate should prevent fallback brainstorming"),
            )
            failed_task = latest_failed_evolve_task(candidate.id, root)
            retried = retry_evolve_candidate(candidate.id, root)

        assert failed is not None
        assert proposal.top_candidate is not None
        assert failed_task is not None
        self.assertEqual(proposal.top_candidate.id, candidate.id)
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
            assert claimed is not None
            acknowledged = acknowledge_evolve_schedule(
                claimed.schedule_claim_id,
                root,
                now=due,
            )
            disabled = disable_evolve_schedule(root)

        self.assertTrue(scheduled.schedule_enabled)
        self.assertEqual(scheduled.schedule_interval_seconds, 86400)
        self.assertEqual(scheduled.schedule_next_run_at, "2020-01-02T00:00:00+00:00")
        self.assertIsNone(before_due)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.schedule_next_run_at, "2020-01-02T00:00:00+00:00")
        self.assertEqual(claimed_again.schedule_claim_id, claimed.schedule_claim_id)
        self.assertEqual(
            acknowledged.schedule_next_run_at,
            "2020-01-03T00:00:00+00:00",
        )
        self.assertFalse(disabled.schedule_enabled)

    def test_daily_schedule_uses_next_local_time(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            start = datetime(2020, 1, 1, 8, 30, tzinfo=timezone.utc)
            due = datetime(2020, 1, 1, 9, 0, tzinfo=timezone.utc)

            scheduled = set_evolve_daily_schedule("9:00", root, now=start)
            claimed = claim_due_evolve_schedule(root, now=due)
            assert claimed is not None
            state_after_claim = acknowledge_evolve_schedule(
                claimed.schedule_claim_id,
                root,
                now=due,
            )

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
            assert claimed is not None
            state_after_claim = acknowledge_evolve_schedule(
                claimed.schedule_claim_id,
                root,
                now=due,
            )

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


def _feedback_candidate(root: Path, message: str):
    digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
    evidence_id = f"evidence-{digest[:16]}"
    candidate_id = f"feedback-evidence-{digest[:12]}"
    evidence_path = root / ".enoch" / "artifacts" / "evidence.jsonl"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    with evidence_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": evidence_id,
                    "source": "feedback",
                    "observation": message,
                    "evidence_type": "explicit feedback",
                    "affected_area": "Enoch workflow",
                    "desired_outcome": message,
                    "confidence": 1.0,
                    "explicit": True,
                    "evidence_refs": [f"conversation:fixture-{digest[:16]}"],
                    "created_at": "2026-07-18T00:00:00+00:00",
                    "updated_at": "2026-07-18T00:00:00+00:00",
                    "status": "linked",
                    "candidate_ids": [candidate_id],
                },
                sort_keys=True,
            )
            + "\n"
        )
    path = root / ".enoch" / "evolve_candidates.json"
    existing = (
        json.loads(path.read_text(encoding="utf-8")).get("candidates", [])
        if path.exists()
        else []
    )
    existing.append(
        {
            "id": candidate_id,
            "source": "feedback",
            "title": message,
            "rationale": "Explicit semantic evidence supports this candidate.",
            "proposed_change": message,
            "expected_benefit": "Addresses the recorded feedback.",
            "risk": "The change may be too broad.",
            "test_plan": "Add focused tests.",
            "evidence_source": "feedback",
            "signal_actor": "human",
            "candidate_actor": "agent",
            "evidence_ids": [evidence_id],
            "evidence_refs": [f"conversation:fixture-{digest[:16]}"],
            "status": "candidate",
            "score": 30,
            "base_score": 30,
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 5, "candidates": existing}),
        encoding="utf-8",
    )
    return next(
        candidate
        for candidate in load_evolve_candidates(root)
        if candidate.id == candidate_id
    )


def _write_task_events(root: Path, *, task_id: int, status: str) -> None:
    job = _experience_task(task_id, "summarize repository health")
    record_task_event(job, "created", root, event_actor="human", trigger="/task")
    record_task_event(
        replace(job, status=status),
        status,
        root,
        event_actor="agent" if status == "completed" else "human",
        trigger="task-runner" if status == "completed" else "/task cancel",
    )


def _experience_evidence_response(prompt: str, task_id: int) -> str:
    records = json.loads(prompt.split("Evidence input: ", 1)[1])
    assert records[0]["task_id"] == task_id
    return json.dumps(
        [
            {
                "observation": "A task failed while preparing its worktree branch.",
                "evidence_type": "task failure",
                "affected_area": "task worktree setup",
                "desired_outcome": "Task worktrees initialize reliably.",
                "confidence": 0.98,
                "explicit": False,
                "evidence_refs": [records[0]["task_ref"]],
            }
        ]
    )


def _write_stored_candidate(root: Path, *, candidate_id: str, source: str) -> None:
    path = root / ".enoch" / "evolve_candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "candidates": [
                    {
                        "id": candidate_id,
                        "source": source,
                        "title": "Legacy candidate",
                        "status": "candidate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _learning_skill() -> PublishedSkill:
    revision = "a" * 40
    return PublishedSkill(
        name="research",
        version="0.1.0",
        agent="enosh",
        agent_name="Enosh",
        repository="our-ark/enosh",
        branch="main",
        revision=revision,
        path="src/enosh/skills/research",
        url=(
            "https://github.com/our-ark/enosh/tree/"
            f"{revision}/src/enosh/skills/research"
        ),
        description="Synthesize research.",
        metadata="name: research\nversion: 0.1.0\n",
        instructions="# Research\n\nSynthesize research.\n",
        content_hash="b" * 64,
    )


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
