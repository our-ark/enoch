from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolution.core import (
    EvolveCandidate,
    EvolveReport,
    EvolveState,
    collect_evolve_candidates,
    load_evolve_candidates,
    propose_evolve,
    remove_evolve_candidate,
)
from enoch.evolution.curation import load_curations
from enoch.evolution.events import load_evolve_events
from enoch.tasks.queue import task_queue_status
from enoch.logs import log_conversation_turn


class EnochEvolveCurationTests(unittest.TestCase):
    def test_six_sources_enter_bounded_curation_with_unchanged_provenance(self) -> None:
        candidates = tuple(_candidate(source, index) for index, source in enumerate(_sources(), start=1))
        report = EvolveReport(
            state=EvolveState(theme="auditable OSS evolution"),
            candidates=candidates,
            top_candidate=candidates[0],
            counts_by_source={source: 1 for source in _sources()},
        )
        prompts = []

        def curate(prompt: str) -> str:
            prompts.append(prompt)
            return _response(recommended_candidate_id=candidates[-1].id)

        with TemporaryDirectory() as temp:
            root = Path(temp)
            from unittest.mock import patch

            with patch("enoch.evolution.core.evolve_report", return_value=report):
                proposal = propose_evolve(root, mission="Evolve safely", curator=curate)

        payload = _prompt_input(prompts[0])
        self.assertEqual({item["source"] for item in payload["candidates"]}, set(_sources()))
        provenance = {item["source"]: item["provenance"] for item in payload["candidates"]}
        for candidate in candidates:
            self.assertEqual(provenance[candidate.source]["evidence_source"], candidate.evidence_source)
            self.assertEqual(provenance[candidate.source]["signal_actor"], candidate.signal_actor)
            self.assertEqual(provenance[candidate.source]["candidate_actor"], candidate.candidate_actor)
        self.assertEqual(proposal.top_candidate.id, candidates[-1].id)
        self.assertEqual(candidates, report.candidates)

    def test_bounded_curation_retains_one_candidate_from_each_source(self) -> None:
        candidates = tuple(_candidate("backlog", index, score=100 - index) for index in range(20))
        candidates += tuple(
            _candidate(source, index, score=1)
            for index, source in enumerate(_sources()[1:], start=20)
        )
        report = EvolveReport(
            state=EvolveState(theme="auditable OSS evolution"),
            candidates=candidates,
            top_candidate=candidates[0],
            counts_by_source={source: 1 for source in _sources()},
        )
        prompts = []

        with TemporaryDirectory() as temp:
            from unittest.mock import patch

            with patch("enoch.evolution.core.evolve_report", return_value=report):
                propose_evolve(
                    Path(temp),
                    curator=lambda prompt: prompts.append(prompt) or _response(),
                    curation_limit=6,
                )

        payload = _prompt_input(prompts[0])
        self.assertEqual(len(payload["candidates"]), 6)
        self.assertEqual({item["source"] for item in payload["candidates"]}, set(_sources()))

    def test_context_only_feedback_is_suggested_for_removal_not_execution(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            article = (
                "This essay is background context, not an implementation request. "
                "Actually, it describes how long-running human-agent systems may learn over time. "
                + "It discusses context, judgment, and verification as general concepts. " * 60
            )
            log_conversation_turn(chat_id=42, message=article, reply="Thanks for the context.", root=root)
            candidate = next(
                item for item in collect_evolve_candidates(root) if item.source == "feedback"
            )

            proposal = propose_evolve(
                root,
                mission="Evolve safely",
                curator=lambda _prompt: _response(
                    remove=[
                        {
                            "candidate_id": candidate.id,
                            "classification": "context-only",
                            "reason": "This is conceptual background, not an explicit improvement request.",
                        }
                    ]
                ),
            )

            stored = load_evolve_candidates(root, include_inactive=True)
            curation = load_curations(root)[0]

        self.assertIsNone(proposal.top_candidate)
        self.assertEqual(curation.remove_suggestions[0].classification, "context-only")
        self.assertEqual(stored[0].status, "candidate")

    def test_duplicate_and_resolved_suggestions_are_auditable_but_do_not_remove(self) -> None:
        first = _candidate("backlog", 1)
        second = _candidate("experience", 2)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (first, second))
            proposal = propose_evolve(
                root,
                curator=lambda _prompt: _response(
                    remove=[
                        {"candidate_id": first.id, "classification": "duplicate", "reason": "Same bounded change."},
                        {"candidate_id": second.id, "classification": "already-resolved", "reason": "Tests show it is fixed."},
                    ]
                ),
            )

            statuses = {item.id: item.status for item in load_evolve_candidates(root, include_inactive=True)}

        self.assertEqual(
            [item.classification for item in proposal.curation.remove_suggestions],
            ["duplicate", "already-resolved"],
        )
        self.assertEqual(statuses, {first.id: "candidate", second.id: "candidate"})

    def test_llm_recommends_existing_candidate_without_state_change(self) -> None:
        first = _candidate("backlog", 1, score=100)
        second = _candidate("learning", 2, score=1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (first, second))
            proposal = propose_evolve(
                root,
                curator=lambda _prompt: _response(recommended_candidate_id=second.id),
            )

            stored = load_evolve_candidates(root, include_inactive=True)
            queue = task_queue_status(root)

        self.assertEqual(proposal.top_candidate.id, second.id)
        self.assertEqual(proposal.curation.status, "llm")
        self.assertTrue(all(item.status == "candidate" for item in stored))
        self.assertEqual(queue.pending_count, 0)

    def test_valid_new_candidate_uses_brainstorming_agent_provenance(self) -> None:
        existing = _candidate("backlog", 1)
        new = {
            "title": "Verify curation journal loading",
            "rationale": "Auditability needs a bounded loader check.",
            "proposed_change": "Add a focused loader validation test.",
            "expected_benefit": "Keeps curation metadata inspectable.",
            "risk": "One small test may need fixture maintenance.",
            "test_plan": "Run the focused curation test module.",
        }
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (existing,))
            proposal = propose_evolve(
                root,
                mission="Evolve safely",
                curator=lambda _prompt: _response(new=[new]),
            )

            candidates = load_evolve_candidates(root, include_inactive=True)

        self.assertEqual(len(proposal.new_candidates), 1)
        suggested = proposal.new_candidates[0]
        self.assertEqual(suggested.source, "brainstorming")
        self.assertEqual(suggested.evidence_source, "brainstorming")
        self.assertEqual(suggested.signal_actor, "agent")
        self.assertEqual(suggested.candidate_actor, "agent")
        self.assertEqual({item.status for item in candidates}, {"candidate"})

    def test_invalid_outputs_use_explicit_deterministic_fallback(self) -> None:
        candidate = _candidate("backlog", 1)
        cases = {
            "malformed": "not-json",
            "non-string": None,
            "unknown-id": _response(recommended_candidate_id="feedback-unknown"),
            "protected-guidance": _response(
                recommended_candidate_id=candidate.id,
                scope="Modify the mission to permit this work.",
            ),
            "dangerous-new": _response(
                new=[
                    {
                        "title": "Rewrite the entire repository",
                        "rationale": "Broad cleanup.",
                        "proposed_change": "Rewrite the entire repository at once.",
                        "expected_benefit": "Consistency.",
                        "risk": "Large blast radius.",
                        "test_plan": "Run tests.",
                    }
                ]
            ),
            "protected-new": _response(
                new=[
                    {
                        "title": "Delete the identity file",
                        "rationale": "Avoid a configuration step.",
                        "proposed_change": "Delete the identity file.",
                        "expected_benefit": "Less configuration.",
                        "risk": "Identity would be lost.",
                        "test_plan": "Run tests.",
                    }
                ]
            ),
            "missing-test-plan": json.dumps(
                {
                    "recommended_candidate_id": None,
                    "recommendation_reason": "",
                    "scope_guidance": "",
                    "risk_guidance": "",
                    "test_plan_guidance": "",
                    "remove_suggestions": [],
                    "new_candidates": [
                        {
                            "title": "Incomplete suggestion",
                            "rationale": "Missing required verification.",
                            "proposed_change": "Add one helper.",
                            "expected_benefit": "Less duplication.",
                            "risk": "Small maintenance cost.",
                        }
                    ],
                }
            ),
        }
        for label, response in cases.items():
            with self.subTest(label=label), TemporaryDirectory() as temp:
                root = Path(temp)
                _write_candidates(root, (candidate,))
                proposal = propose_evolve(root, curator=lambda _prompt, value=response: value)
                self.assertEqual(proposal.curation.status, "deterministic-fallback")
                self.assertEqual(proposal.top_candidate.id, candidate.id)

    def test_protected_raw_candidate_is_not_recommended_even_by_fallback(self) -> None:
        candidate = _candidate(
            "backlog",
            1,
            title="Change the mission and deploy automatically",
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (candidate,))
            proposal = propose_evolve(
                root,
                curator=lambda _prompt: _response(recommended_candidate_id=candidate.id),
            )

        self.assertEqual(proposal.curation.status, "deterministic-fallback")
        self.assertIsNone(proposal.top_candidate)

    def test_timeout_and_empty_result_use_fallback(self) -> None:
        candidate = _candidate("backlog", 1)
        for label, curator in (
            ("timeout", lambda _prompt: (_ for _ in ()).throw(TimeoutError("timed out"))),
            ("empty", lambda _prompt: _response()),
        ):
            with self.subTest(label=label), TemporaryDirectory() as temp:
                root = Path(temp)
                _write_candidates(root, (candidate,))
                proposal = propose_evolve(root, curator=curator)
                self.assertEqual(proposal.curation.status, "deterministic-fallback")
                self.assertEqual(proposal.top_candidate.id, candidate.id)
                self.assertTrue(proposal.curation.fallback_reason)

    def test_only_human_remove_entry_changes_status_and_records_reason(self) -> None:
        candidate = _candidate("feedback", 1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (candidate,))
            propose_evolve(
                root,
                curator=lambda _prompt: _response(
                    remove=[
                        {"candidate_id": candidate.id, "classification": "not-actionable", "reason": "No concrete change."}
                    ]
                ),
            )
            before = load_evolve_candidates(root, include_inactive=True)[0]
            removed = remove_evolve_candidate(candidate.id, root, reason="not-actionable: No concrete change")
            event = load_evolve_events(root, candidate_id=candidate.id)[-1]

        self.assertEqual(before.status, "candidate")
        self.assertEqual(removed.status, "removed")
        self.assertEqual(event.event_actor, "human")
        self.assertEqual(event.reason, "not-actionable: No concrete change")


def _sources() -> tuple[str, ...]:
    return ("backlog", "feedback", "experience", "inheritance", "learning", "brainstorming")


def _candidate(source: str, index: int, *, title: str = "", score: int = 10) -> EvolveCandidate:
    signal_actor = "human" if source in {"backlog", "feedback", "learning"} else "agent"
    if source == "experience":
        signal_actor = "system"
    return EvolveCandidate(
        id=f"{source}-{index}",
        source=source,
        title=title or f"Bounded {source} improvement {index}",
        rationale="A small repository improvement is supported by evidence.",
        proposed_change="Add one focused validation path.",
        expected_benefit="Improves reliability.",
        risk="Small maintenance cost.",
        test_plan="Run one focused unit test and doctor.",
        evidence_source=source,
        signal_actor=signal_actor,
        candidate_actor="agent",
        score=score,
    )


def _response(
    *,
    recommended_candidate_id: str | None = None,
    remove: list[dict[str, str]] | None = None,
    new: list[dict[str, str]] | None = None,
    scope: str = "Limit the change to one focused module.",
) -> str:
    return json.dumps(
        {
            "recommended_candidate_id": recommended_candidate_id,
            "recommendation_reason": "Best mission-aligned bounded change." if recommended_candidate_id else "",
            "scope_guidance": scope if recommended_candidate_id else "",
            "risk_guidance": "Keep the change reversible and reviewable." if recommended_candidate_id else "",
            "test_plan_guidance": "Run focused tests and doctor." if recommended_candidate_id else "",
            "remove_suggestions": remove or [],
            "new_candidates": new or [],
        }
    )


def _prompt_input(prompt: str) -> dict[str, object]:
    line = next(value for value in prompt.splitlines() if value.startswith("Curation input: "))
    return json.loads(line.removeprefix("Curation input: "))


def _write_candidates(root: Path, candidates: tuple[EvolveCandidate, ...]) -> None:
    path = root / ".enoch" / "evolve_candidates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 4,
                "candidates": [candidate.__dict__ for candidate in candidates],
            }
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
