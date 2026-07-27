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
from enoch.evolution.sources.brainstorming import BrainstormCandidateDraft
from enoch.evolution.core import (
    MODE_DISABLED,
    approve_evolve_candidate,
    claim_due_evolve_schedule,
    claim_scheduled_brainstorm,
    acknowledge_evolve_schedule,
    create_brainstorm_candidates,
    disable_evolve_schedule,
    evolve_report,
    create_learning_candidate,
    load_evolve_candidates,
    load_evolve_state,
    propose_evolve,
    rank_evolve_candidates,
    remove_evolve_candidate,
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

            candidates = sync_evolve_candidates(root)
        self.assertEqual(candidates, ())

    def test_task_failures_are_pending_evidence_not_hardcoded_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            queued = enqueue_task(42, "ship flaky workflow", root)
            running = begin_next_task(root)
            assert running is not None
            fail_task(queued.id, root, result="Tests failed in Telegram workflow.")

            candidates = sync_evolve_candidates(root)
            report = evolve_report(root)

        self.assertEqual(candidates, ())
        self.assertNotIn("experience", report.counts_by_source)
        self.assertEqual(report.pending_evidence["experience"], 1)

    def test_repeated_successes_do_not_become_hardcoded_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_task_events(root, task_id=1, status="completed")
            _write_task_events(root, task_id=2, status="completed")

            candidates = sync_evolve_candidates(root)

        self.assertEqual(candidates, ())

    def test_unstarted_cancellation_is_not_a_hardcoded_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_task_events(root, task_id=3, status="cancelled")

            candidates = sync_evolve_candidates(root)

        self.assertNotIn("task-3", {candidate.id for candidate in candidates})

    def test_direct_pathways_remain_separate_candidate_sources(self) -> None:
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
            brainstorming = create_brainstorm_candidates(
                (
                    BrainstormCandidateDraft(
                        title="Make provenance visible",
                        rationale="The theme emphasizes accountability.",
                        proposed_change="Show source details in candidate reports.",
                        expected_benefit="Improves review quality.",
                        risk="Adds output.",
                        test_plan="Add report tests.",
                    ),
                ),
                root,
                theme="accountable evolution",
                context_hash="c" * 64,
            )

            candidates = evolve_report(root).candidates

        self.assertTrue(created)
        self.assertEqual(len(brainstorming.created), 1)
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

    def test_brainstorm_candidates_remain_visible_across_theme_changes(self) -> None:
        draft = BrainstormCandidateDraft(
            title="Improve audit trail",
            rationale="Useful for theme A.",
            proposed_change="Show provenance.",
            expected_benefit="Better review.",
            risk="More output.",
            test_plan="Add formatting tests.",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            creation = create_brainstorm_candidates(
                (draft,),
                root,
                theme="auditability",
                context_hash="d" * 64,
            )
            first = sync_evolve_candidates(root, theme="auditability")
            second = sync_evolve_candidates(root, theme="runtime latency")

        self.assertEqual(len(creation.created), 1)
        self.assertEqual({candidate.source for candidate in first}, {"brainstorming"})
        self.assertEqual({candidate.id for candidate in second}, {first[0].id})
        self.assertGreater(first[0].score, second[0].score)
        self.assertEqual(first[0].source_theme, "auditability")
        self.assertEqual(first[0].source_context_hash, "d" * 64)

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

    def test_approve_candidate_archives_it(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = _feedback_candidate(root, "I want a clearer evolve handoff.")

            approved = approve_evolve_candidate(candidate.id, root)
            visible = load_evolve_candidates(root)
            archived = load_evolve_candidates(root, include_inactive=True)

        self.assertEqual(approved.status, "approved")
        self.assertEqual(visible, ())
        self.assertEqual(archived[0].id, candidate.id)
        self.assertEqual(archived[0].status, "approved")

    def test_proposal_skips_approved_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = _feedback_candidate(root, "I want a clearer lower priority candidate.")
            second = _feedback_candidate(root, "I want a clearer highest priority candidate.")
            approve_evolve_candidate(second.id, root)

            proposal = propose_evolve(root)

        self.assertEqual([candidate.id for candidate in proposal.candidates], [first.id])
        assert proposal.top_candidate is not None
        self.assertEqual(proposal.top_candidate.id, first.id)
        self.assertEqual(proposal.top_candidate.status, "candidate")

    def test_empty_proposal_does_not_curate_or_invent_candidates(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            proposal = propose_evolve(
                root,
                curator=lambda _prompt: self.fail(
                    "curation should not run without candidates"
                ),
            )

        self.assertEqual(proposal.candidates, ())
        self.assertIsNone(proposal.top_candidate)
        self.assertIsNone(proposal.curation)

    def test_brainstorm_candidate_deduplicates_change_across_themes(self) -> None:
        draft = BrainstormCandidateDraft(
            title="Improve telemetry",
            rationale="Telemetry is hard to inspect.",
            proposed_change="Add a bounded telemetry summary.",
            expected_benefit="Improves debugging.",
            risk="Adds output.",
            test_plan="Add focused formatting tests.",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = create_brainstorm_candidates(
                (draft,),
                root,
                theme="reliable task telemetry",
                context_hash="e" * 64,
            )
            second = create_brainstorm_candidates(
                (replace(draft, title="Clarify telemetry"),),
                root,
                theme="operational visibility",
                context_hash="f" * 64,
            )

        self.assertEqual(len(first.created), 1)
        self.assertEqual(second.created, ())
        self.assertEqual(
            [candidate.id for candidate in second.existing],
            [first.created[0].id],
        )

    def test_scheduled_brainstorm_claim_is_theme_scoped_and_cooled_down(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            start = datetime(2026, 7, 18, 9, 0, tzinfo=timezone.utc)

            first = claim_scheduled_brainstorm(
                "reliable task telemetry",
                root,
                now=start,
            )
            second = claim_scheduled_brainstorm(
                "reliable task telemetry",
                root,
                now=start + timedelta(hours=1),
            )
            other_theme = claim_scheduled_brainstorm(
                "clear reports",
                root,
                now=start + timedelta(hours=1),
            )
            after_cooldown = claim_scheduled_brainstorm(
                "reliable task telemetry",
                root,
                now=start + timedelta(hours=25),
            )

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertTrue(other_theme)
        self.assertTrue(after_cooldown)

    def test_scheduled_brainstorm_preserves_legacy_cooldown_attempts(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / ".enoch" / "evolve_brainstorm_fallback.json"
            state.parent.mkdir(parents=True)
            state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "attempts": {
                            "reliable task telemetry": (
                                "2026-07-18T09:00:00+00:00"
                            ),
                        },
                    }
                ),
                encoding="utf-8",
            )

            blocked = claim_scheduled_brainstorm(
                "reliable task telemetry",
                root,
                now=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )
            claimed = claim_scheduled_brainstorm(
                "reliable task telemetry",
                root,
                now=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
            )
            migrated = json.loads(
                (
                    root
                    / ".enoch"
                    / "evolve_brainstorm_schedule.json"
                ).read_text(encoding="utf-8")
            )

        self.assertFalse(blocked)
        self.assertTrue(claimed)
        self.assertIn(
            "reliable task telemetry",
            migrated["attempts"],
        )

    def test_task_outcomes_do_not_mutate_approved_candidate(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate = _feedback_candidate(root, "I want clearer evolve completion.")
            approved = approve_evolve_candidate(candidate.id, root)
            job = enqueue_task(
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
            failed = fail_task(job.id, root, result="Validation failed.")
            visible = load_evolve_candidates(root)
            all_candidates = load_evolve_candidates(root, include_inactive=True)

        assert failed is not None
        self.assertEqual(approved.status, "approved")
        self.assertEqual(failed.status, "failed")
        self.assertNotIn(candidate.id, {item.id for item in visible})
        self.assertEqual(all_candidates[0].id, candidate.id)
        self.assertEqual(all_candidates[0].status, "approved")

    def test_legacy_execution_statuses_migrate_to_approved_archive(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / ".enoch" / "evolve_candidates.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 7,
                        "candidates": [
                            {
                                "id": f"legacy-{status}",
                                "source": "feedback",
                                "title": f"Legacy {status}",
                                "status": status,
                            }
                            for status in (
                                "running",
                                "done",
                                "failed",
                                "cancelled",
                                "regressed",
                                "reverted",
                                "forward-fixed",
                            )
                        ],
                    }
                ),
                encoding="utf-8",
            )

            visible = sync_evolve_candidates(root)
            archived = load_evolve_candidates(root, include_inactive=True)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(visible, ())
        self.assertEqual({item.status for item in archived}, {"approved"})
        self.assertEqual(migrated["schema_version"], 8)
        self.assertEqual(
            {item["status"] for item in migrated["candidates"]},
            {"approved"},
        )

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
