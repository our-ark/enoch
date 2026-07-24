from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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
from enoch.evolution.curation import curation_index_path, load_curations
from enoch.evolution.events import load_evolve_events, record_evolve_event
from enoch.tasks.events import record_task_event
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
            _record_completed_work(
                root,
                task_id=17,
                candidate=second,
                request="Implement focused experience validation.",
                result="Implemented focused experience validation.\nFiles:\n- src/enoch/evolution/core.py",
                pr_url="https://github.com/our-ark/enoch/pull/19",
                promoted=True,
            )
            proposal = propose_evolve(
                root,
                curator=lambda _prompt: _response(
                    remove=[
                        {"candidate_id": first.id, "classification": "duplicate", "reason": "Same bounded change."},
                        {
                            "candidate_id": second.id,
                            "classification": "already-resolved",
                            "reason": "Task 17 was promoted through PR 19.",
                            "evidence_refs": ["task:17", "merge:e536d32"],
                        },
                    ]
                ),
            )

            statuses = {item.id: item.status for item in load_evolve_candidates(root, include_inactive=True)}

        self.assertEqual(
            [item.classification for item in proposal.curation.remove_suggestions],
            ["duplicate", "already-resolved"],
        )
        self.assertEqual(statuses, {first.id: "candidate", second.id: "candidate"})

    def test_prompt_receives_bounded_redacted_recent_completed_work(self) -> None:
        candidate = _candidate(
            "feedback",
            1,
            title="Review /workspace/private/candidate.py token=candidate-secret",
        )
        prompts: list[str] = []
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (candidate,))
            for task_id in range(1, 16):
                _record_completed_work(
                    root,
                    task_id=task_id,
                    request=(
                        f"Review task {task_id} at /Users/gary/private/repo "
                        "chat_id=987 mem_20260723_private api_key=top-secret"
                    ),
                    result=(
                        "Completed review in /tmp/private.log with password=hunter2. "
                        + "bounded " * 300
                    ),
                )
            propose_evolve(
                root,
                mission="Evolve /workspace/private/body.py with secret=mission-secret",
                curator=lambda prompt: prompts.append(prompt)
                or _response(recommended_candidate_id=candidate.id),
            )

        prompt_payload = _prompt_input(prompts[0])
        evidence = prompt_payload["recent_completed_work"]
        serialized = json.dumps(prompt_payload)
        self.assertEqual(len(evidence), 12)
        self.assertNotIn("/Users/", serialized)
        self.assertNotIn("/tmp/", serialized)
        self.assertNotIn("chat_id=987", serialized)
        self.assertNotIn("mem_20260723_private", serialized)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("hunter2", serialized)
        self.assertNotIn("candidate-secret", serialized)
        self.assertNotIn("mission-secret", serialized)
        self.assertLessEqual(
            max(len(item["result_summary"]) for item in evidence),
            800,
        )
        self.assertEqual(
            {item["completion_kind"] for item in evidence},
            {"completed-no-body-change"},
        )
        self.assertEqual(
            {item["resolution_authority"] for item in evidence},
            {"task-completion"},
        )

    def test_direct_merged_candidate_link_supports_already_resolved_evidence(self) -> None:
        candidate = EvolveCandidate(
            **{**_candidate("feedback", 1).__dict__, "source_task_id": 17}
        )
        prompts: list[str] = []
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (candidate,))
            _record_completed_work(
                root,
                task_id=17,
                candidate=candidate,
                request="Add semantic candidate curation.",
                result="Added semantic candidate curation.\nFiles:\n- src/enoch/evolution/curation.py",
                pr_url="https://github.com/our-ark/enoch/pull/19",
                promoted=True,
                version="v0.2.1",
            )
            proposal = propose_evolve(
                root,
                curator=lambda prompt: prompts.append(prompt)
                or _response(
                    remove=[
                        {
                            "candidate_id": candidate.id,
                            "classification": "already-resolved",
                            "reason": (
                                "The linked task is merged into the authoritative body; "
                                "/Users/gary/private/result token=private-token."
                            ),
                            "evidence_refs": ["task:17", "pr:https://github.com/our-ark/enoch/pull/19", "merge:e536d32"],
                        }
                    ]
                ),
            )
            stored = load_evolve_candidates(root, include_inactive=True)[0]
            journal = load_curations(root)[0]
            raw_journal = curation_index_path(root).read_text(encoding="utf-8")

        completion = _prompt_input(prompts[0])["recent_completed_work"][0]
        self.assertIn(candidate.id, completion["direct_candidate_ids"])
        self.assertEqual(completion["resolution_authority"], "authoritative-body")
        self.assertEqual(completion["authoritative_version"], "v0.2.1")
        self.assertIn("version:v0.2.1", completion["evidence_refs"])
        self.assertEqual(proposal.curation.status, "llm")
        self.assertEqual(stored.status, "candidate")
        self.assertEqual(
            journal.remove_suggestions[0].evidence_refs,
            (
                "task:17",
                "pr:https://github.com/our-ark/enoch/pull/19",
                "merge:e536d32",
            ),
        )
        self.assertNotIn("Added semantic candidate curation", raw_journal)
        self.assertNotIn("/Users/gary", raw_journal)
        self.assertNotIn("private-token", raw_journal)

    def test_semantic_resolution_without_direct_link_and_supersession_are_allowed(self) -> None:
        resolved = _candidate("feedback", 1, title="Use LLM semantic candidate curation")
        superseded = _candidate("backlog", 2, title="Add fixed-score candidate pruning")
        completed_candidate = _candidate("brainstorming", 99)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (resolved, superseded))
            _record_completed_work(
                root,
                task_id=22,
                candidate=completed_candidate,
                request="Replace fixed pruning with LLM semantic candidate curation.",
                result=(
                    "Implemented LLM semantic curation and replaced fixed pruning."
                    "\nFiles:\n- src/enoch/evolution/curation.py"
                ),
                pr_url="https://github.com/our-ark/enoch/pull/22",
                promoted=True,
                merge_commit="abcdef123456",
            )
            proposal = propose_evolve(
                root,
                curator=lambda _prompt: _response(
                    remove=[
                        {
                            "candidate_id": resolved.id,
                            "classification": "already-resolved",
                            "reason": "Task 22 semantically implements this request.",
                            "evidence_refs": ["task:22", "merge:abcdef123456"],
                        },
                        {
                            "candidate_id": superseded.id,
                            "classification": "superseded",
                            "reason": "The later semantic implementation replaces fixed pruning.",
                            "evidence_refs": ["task:22", "merge:abcdef123456"],
                        },
                    ]
                ),
            )

        self.assertEqual(
            [item.classification for item in proposal.curation.remove_suggestions],
            ["already-resolved", "superseded"],
        )

    def test_open_pr_partial_failed_and_cancelled_work_cannot_resolve_body_candidate(self) -> None:
        candidate = _candidate("feedback", 1)
        for label, event, runtime_reason, promoted in (
            ("open-pr", "completed", "completed", False),
            ("partial", "completed", "output-limit", True),
            ("failed", "failed", "failed", False),
            ("cancelled", "cancelled", "cancelled", False),
        ):
            with self.subTest(label=label), TemporaryDirectory() as temp:
                root = Path(temp)
                _write_candidates(root, (candidate,))
                _record_completed_work(
                    root,
                    task_id=31,
                    candidate=candidate,
                    result="Changed curation.\nFiles:\n- src/enoch/evolution/curation.py",
                    pr_url="https://github.com/our-ark/enoch/pull/31",
                    event=event,
                    runtime_reason=runtime_reason,
                    promoted=promoted,
                )
                proposal = propose_evolve(
                    root,
                    curator=lambda _prompt: _response(
                        remove=[
                            {
                                "candidate_id": candidate.id,
                                "classification": "already-resolved",
                                "reason": "Claimed complete.",
                                "evidence_refs": ["task:31"],
                            }
                        ]
                    ),
                )
                stored = load_evolve_candidates(root, include_inactive=True)[0]

            self.assertEqual(proposal.curation.status, "deterministic-fallback")
            self.assertEqual(stored.status, "candidate")

    def test_unknown_and_malformed_evidence_refs_are_rejected_without_mutation(self) -> None:
        candidate = _candidate("feedback", 1)
        for ref in ("task:999", "local:/Users/gary/private"):
            with self.subTest(ref=ref), TemporaryDirectory() as temp:
                root = Path(temp)
                _write_candidates(root, (candidate,))
                _record_completed_work(root, task_id=17)
                proposal = propose_evolve(
                    root,
                    curator=lambda _prompt, value=ref: _response(
                        remove=[
                            {
                                "candidate_id": candidate.id,
                                "classification": "already-resolved",
                                "reason": "Invalid evidence must not be trusted.",
                                "evidence_refs": [value],
                            }
                        ]
                    ),
                )
                status = load_evolve_candidates(root, include_inactive=True)[0].status
            self.assertEqual(proposal.curation.status, "deterministic-fallback")
            self.assertEqual(status, "candidate")

    def test_completion_evidence_loading_failure_uses_explicit_fallback(self) -> None:
        candidate = _candidate("feedback", 1)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_candidates(root, (candidate,))
            from unittest.mock import patch

            with patch(
                "enoch.evolution.core.recent_completion_evidence",
                side_effect=OSError("journal unavailable"),
            ):
                proposal = propose_evolve(
                    root,
                    curator=lambda _prompt: self.fail("curator must not run"),
                )

        self.assertEqual(proposal.curation.status, "deterministic-fallback")
        self.assertIn("completion-evidence-unavailable", proposal.curation.fallback_reason)

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
            _record_completed_work(
                root,
                task_id=17,
                candidate=candidate,
                result="Resolved candidate.\nFiles:\n- src/enoch/evolution/curation.py",
                pr_url="https://github.com/our-ark/enoch/pull/19",
                promoted=True,
            )
            proposal = propose_evolve(
                root,
                curator=lambda _prompt: _response(
                    remove=[
                        {
                            "candidate_id": candidate.id,
                            "classification": "already-resolved",
                            "reason": "Merged by task 17.",
                            "evidence_refs": ["task:17", "merge:e536d32"],
                        }
                    ]
                ),
            )
            before = load_evolve_candidates(root, include_inactive=True)[0]
            suggestion = proposal.curation.remove_suggestions[0]
            with self.assertRaisesRegex(ValueError, "Only a human"):
                remove_evolve_candidate(
                    candidate.id,
                    root,
                    event_actor="agent",
                    reason=suggestion.reason,
                    classification=suggestion.classification,
                    curation_id=proposal.curation.id,
                    evidence_refs=suggestion.evidence_refs,
                )
            removed = remove_evolve_candidate(
                candidate.id,
                root,
                reason=suggestion.reason,
                classification=suggestion.classification,
                curation_id=proposal.curation.id,
                evidence_refs=suggestion.evidence_refs,
            )
            event = load_evolve_events(root, candidate_id=candidate.id)[-1]

        self.assertEqual(before.status, "candidate")
        self.assertEqual(removed.status, "removed")
        self.assertEqual(event.event_actor, "human")
        self.assertEqual(event.reason, "Merged by task 17.")
        self.assertEqual(event.removal_classification, "already-resolved")
        self.assertEqual(event.curation_id, proposal.curation.id)
        self.assertEqual(event.evidence_refs, ("task:17", "merge:e536d32"))

    def test_legacy_curation_without_evidence_fields_still_loads(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = curation_index_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "id": "curation-legacy",
                        "created_at": "2026-07-19T00:00:00Z",
                        "status": "llm",
                        "input_candidate_ids": ["feedback-1"],
                        "remove_suggestions": [
                            {
                                "candidate_id": "feedback-1",
                                "classification": "duplicate",
                                "reason": "Legacy duplicate.",
                            }
                        ],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            loaded = load_curations(root)[0]

        self.assertEqual(loaded.input_evidence_refs, ())
        self.assertEqual(loaded.remove_suggestions[0].evidence_refs, ())


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
    remove_suggestions = [
        {**item, "evidence_refs": item.get("evidence_refs", [])}
        for item in (remove or [])
    ]
    return json.dumps(
        {
            "recommended_candidate_id": recommended_candidate_id,
            "recommendation_reason": "Best mission-aligned bounded change." if recommended_candidate_id else "",
            "scope_guidance": scope if recommended_candidate_id else "",
            "risk_guidance": "Keep the change reversible and reviewable." if recommended_candidate_id else "",
            "test_plan_guidance": "Run focused tests and doctor." if recommended_candidate_id else "",
            "remove_suggestions": remove_suggestions,
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


def _record_completed_work(
    root: Path,
    *,
    task_id: int,
    candidate: EvolveCandidate | None = None,
    request: str = "Complete a bounded non-code review.",
    result: str = "Completed the bounded review.",
    pr_url: str = "",
    event: str = "completed",
    runtime_reason: str = "completed",
    promoted: bool = False,
    merge_commit: str = "e536d32",
    version: str = "",
) -> None:
    completed_at = f"2026-07-23T12:{task_id % 60:02d}:00Z"
    job = SimpleNamespace(
        id=task_id,
        text=request,
        created_at=completed_at,
        started_at=completed_at,
        completed_at=completed_at,
        result=result,
        pr_urls=(pr_url,) if pr_url else (),
        publish_stage="pr_opened" if pr_url else "",
        commit_sha="d58edcc" if pr_url else "",
        context_source="",
        source=candidate.source if candidate is not None else "task",
        initiated_by="human",
        trigger="/task",
        candidate_id=candidate.id if candidate is not None else "",
        parent_task_id=None,
        evidence_source=candidate.evidence_source if candidate is not None else "",
        signal_actor=candidate.signal_actor if candidate is not None else "",
        candidate_actor=candidate.candidate_actor if candidate is not None else "",
        approval_actor="human" if candidate is not None else "",
        parent_candidate_id="",
        source_task_id=None,
        attempt=1,
        max_attempts=3,
        next_attempt_at="",
        failure_code="",
        failure_class="",
        retryable=False,
        runtime_provider="codex",
        runtime_session_id="private-session-not-persisted-in-curation",
        runtime_completion_reason=runtime_reason,
        runtime_usage={},
        runtime_event_types=(),
        runtime_output_refs=(),
        runtime_side_effects=(),
    )
    record_task_event(
        job,
        event,
        root,
        event_actor="agent" if event != "cancelled" else "human",
        trigger="task-runner",
    )
    if promoted:
        promoted_candidate = candidate or _candidate("brainstorming", task_id)
        record_evolve_event(
            "promoted",
            root,
            event_actor="human",
            trigger="/evolve reconcile",
            candidate=promoted_candidate,
            task_id=task_id,
            pr_url=pr_url or f"https://github.com/our-ark/enoch/pull/{task_id}",
            merge_commit=merge_commit,
            authoritative_branch="main",
            promoted_at=completed_at,
        )
        if version:
            record_evolve_event(
                "adopted",
                root,
                event_actor="system",
                trigger="daemon-startup",
                candidate=promoted_candidate,
                task_id=task_id,
                pr_url=pr_url or f"https://github.com/our-ark/enoch/pull/{task_id}",
                merge_commit=merge_commit,
                authoritative_branch="main",
                promoted_at=completed_at,
                version=version,
                health_check="passed",
            )


if __name__ == "__main__":
    unittest.main()
