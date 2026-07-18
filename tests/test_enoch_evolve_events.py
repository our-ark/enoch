from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolve import EvolveCandidate
from enoch.evolve_events import (
    close_open_proposals,
    evolve_event_path,
    latest_open_proposal_id,
    load_evolve_events,
    load_open_proposals,
    record_evolve_event,
)


class EnochEvolveEventTests(unittest.TestCase):
    def test_records_and_filters_evolution_funnel_events(self) -> None:
        candidate = _candidate()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record_evolve_event(
                "checked",
                root,
                event_actor="system",
                trigger="evolve-scheduler",
                mode="auto-evolve",
                theme="reliable evolution",
                reason="ranked-1-candidate",
            )
            record_evolve_event(
                "queued",
                root,
                event_actor="system",
                trigger="evolve-scheduler",
                mode="auto-evolve",
                theme="reliable evolution",
                candidate=candidate,
                task_id=7,
                approval_actor="agent",
                retry_of_task_id=3,
            )

            events = load_evolve_events(root)
            candidate_events = load_evolve_events(root, candidate_id=candidate.id)
            task_events = load_evolve_events(root, task_id=7)

        self.assertEqual([event.event for event in events], ["checked", "queued"])
        self.assertEqual(candidate_events, task_events)
        self.assertEqual(candidate_events[0].candidate_id, candidate.id)
        self.assertEqual(candidate_events[0].source, "brainstorming")
        self.assertEqual(candidate_events[0].candidate_initiated_by, "agent")
        self.assertEqual(candidate_events[0].evidence_source, "brainstorming")
        self.assertEqual(candidate_events[0].signal_actor, "agent")
        self.assertEqual(candidate_events[0].candidate_actor, "agent")
        self.assertEqual(candidate_events[0].approval_actor, "agent")
        self.assertEqual(candidate_events[0].retry_of_task_id, 3)
        self.assertEqual(candidate_events[0].event_actor, "system")
        self.assertEqual(candidate_events[0].trigger, "evolve-scheduler")
        self.assertEqual(candidate_events[0].task_id, 7)

    def test_loader_skips_malformed_lines(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            record_evolve_event(
                "checked",
                root,
                event_actor="human",
                trigger="/propose",
                mode="co-evolve",
            )
            with evolve_event_path(root).open("a", encoding="utf-8") as handle:
                handle.write("not json\n")
                handle.write('{"event":"queued","event_actor":"system"}\n')

            events = load_evolve_events(root)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event, "checked")

    def test_legacy_feedback_event_infers_split_provenance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = evolve_event_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                """{"event":"queued","event_actor":"human","trigger":"/evolve approve","candidate_id":"feedback-legacy","task_id":3,"source":"feedback","candidate_initiated_by":"human"}\n""",
                encoding="utf-8",
            )

            event = load_evolve_events(root)[0]

        self.assertEqual(event.evidence_source, "feedback")
        self.assertEqual(event.signal_actor, "human")
        self.assertEqual(event.candidate_actor, "agent")
        self.assertEqual(event.approval_actor, "human")

    def test_candidate_and_task_requirements_are_validated(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaises(ValueError):
                record_evolve_event(
                    "selected",
                    root,
                    event_actor="human",
                    trigger="/evolve approve",
                )
            with self.assertRaises(ValueError):
                record_evolve_event(
                    "queued",
                    root,
                    event_actor="system",
                    trigger="evolve-scheduler",
                    candidate=_candidate(),
                )
            with self.assertRaises(ValueError):
                record_evolve_event(
                    "no-action",
                    root,
                    event_actor="system",
                    trigger="/propose",
                    candidate=_candidate(),
                )

    def test_tracks_each_proposal_and_closes_open_disposition(self) -> None:
        candidate = _candidate()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            proposed = record_evolve_event(
                "proposed",
                root,
                event_actor="human",
                trigger="/propose",
                mode="co-evolve",
                candidate=candidate,
            )
            open_before = load_open_proposals(root)
            closed = close_open_proposals(
                root,
                event_actor="human",
                trigger="/propose",
                reason="superseded-by-new-proposal",
            )
            open_after = latest_open_proposal_id(candidate.id, root)
            proposal_events = load_evolve_events(
                root,
                proposal_id=proposed.proposal_id,
            )

        self.assertTrue(proposed.proposal_id.startswith("proposal-"))
        self.assertEqual(open_before, (proposed,))
        self.assertEqual(open_after, "")
        self.assertEqual(closed[0].proposal_id, proposed.proposal_id)
        self.assertEqual(
            [event.event for event in proposal_events],
            ["proposed", "no-action"],
        )
        self.assertEqual(proposal_events[-1].reason, "superseded-by-new-proposal")

    def test_governed_lifecycle_events_require_verified_evidence(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "require a human actor"):
                record_evolve_event(
                    "promoted",
                    root,
                    event_actor="system",
                    trigger="/evolve reconcile",
                    candidate=_candidate(),
                    pr_url="https://github.com/our-ark/enoch/pull/12",
                    merge_commit="7207317",
                    authoritative_branch="main",
                    promoted_at="2026-07-18T18:30:00Z",
                )
            with self.assertRaisesRegex(ValueError, "passed health check"):
                record_evolve_event(
                    "adopted",
                    root,
                    event_actor="system",
                    trigger="daemon-startup",
                    candidate=_candidate(),
                    version="7207317",
                    health_check="failed",
                )


def _candidate() -> EvolveCandidate:
    return EvolveCandidate(
        id="brainstorm-1",
        source="brainstorming",
        title="Improve evolution telemetry",
        rationale="Agent-origin evolution needs an audit trail.",
        proposed_change="Record an append-only evolution event journal.",
        expected_benefit="Makes autonomous evolution measurable.",
        risk="Adds local state.",
        test_plan="Verify journal lifecycle events.",
        initiated_by="agent",
        evidence_source="brainstorming",
        signal_actor="agent",
        candidate_actor="agent",
        score=42,
    )


if __name__ == "__main__":
    unittest.main()
