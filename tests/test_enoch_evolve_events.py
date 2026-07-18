from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolve import EvolveCandidate
from enoch.evolve_events import (
    evolve_event_path,
    load_evolve_events,
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
            )

            events = load_evolve_events(root)
            candidate_events = load_evolve_events(root, candidate_id=candidate.id)
            task_events = load_evolve_events(root, task_id=7)

        self.assertEqual([event.event for event in events], ["checked", "queued"])
        self.assertEqual(candidate_events, task_events)
        self.assertEqual(candidate_events[0].candidate_id, candidate.id)
        self.assertEqual(candidate_events[0].source, "brainstorming")
        self.assertEqual(candidate_events[0].candidate_initiated_by, "agent")
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
        score=42,
    )


if __name__ == "__main__":
    unittest.main()
