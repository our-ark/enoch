from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolution.core import (
    complete_evolve_candidate,
    get_evolve_candidate,
    run_evolve_candidate,
    synthesize_evolve_candidates_from_evidence,
)
from enoch.evolution.evidence import scan_evidence
from enoch.evolution.events import load_evolve_events
from enoch.evolution.lifecycle import (
    EvolveLifecycleError,
    finalize_promoted_evolve_adoptions,
    pending_adoption_path,
    reconcile_evolve_candidate,
    stage_promoted_evolve_adoptions,
)
from enoch.logs import log_conversation_turn
from our_ark_provider_kit import (
    BranchlessRepositoryFixture,
    IndependentReviewFixture,
    RepositoryRevision,
    ReviewLandRequest,
    ReviewSubmission,
)
from enoch.tasks.queue import (
    TaskPublicationState,
    begin_next_task,
    claim_running_task,
    complete_task,
    enqueue_task,
    record_task_publication,
)


REVIEW_URL = "https://reviews.invalid/review-1"
MERGE_COMMIT = "7207317aabbccddeeff001122334455667788990"
VERSION = "9999999aabbccddeeff001122334455667788990"


class EnochEvolveLifecycleTests(unittest.TestCase):
    def test_reconcile_uses_branchless_repository_and_independent_review(self) -> None:
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_review(
                root,
                repository,
                review,
            )

            result = reconcile_evolve_candidate(
                candidate_id,
                root,
                repository=repository,
                review=review,
            )

        self.assertEqual(result.review_id, "review-1")
        self.assertEqual(result.review_urls, (REVIEW_URL,))
        self.assertEqual(result.revision_id, MERGE_COMMIT)
        self.assertEqual(result.authoritative_revision_id, MERGE_COMMIT)
        self.assertEqual(result.authoritative_name, "authoritative")
        self.assertFalse(repository.repository_features.named_branches)
        self.assertFalse(repository.repository_features.staging_index)

    def test_reconcile_records_verified_human_promotion_once(self) -> None:
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_review(
                root,
                repository,
                review,
            )

            first = reconcile_evolve_candidate(
                candidate_id,
                root,
                repository=repository,
                review=review,
            )
            second = reconcile_evolve_candidate(
                candidate_id,
                root,
                repository=repository,
                review=review,
            )
            events = load_evolve_events(root, candidate_id=candidate_id)

        self.assertFalse(first.already_recorded)
        self.assertTrue(second.already_recorded)
        self.assertEqual(
            [event.event for event in events],
            ["promoted"],
        )
        event = first.event
        self.assertEqual(event.event_actor, "human")
        self.assertEqual(event.review_id, "review-1")
        self.assertEqual(event.review_urls, (REVIEW_URL,))
        self.assertEqual(event.revision_id, MERGE_COMMIT)
        self.assertEqual(event.authoritative_revision_id, MERGE_COMMIT)
        self.assertEqual(event.authoritative_name, "authoritative")
        self.assertTrue(event.promoted_at)
        self.assertTrue(event.verified_at)
        self.assertEqual(event.recording_mode, "realtime")

    def test_reconcile_refuses_landed_revision_outside_authoritative_history(self) -> None:
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_review(
                root,
                repository,
                review,
            )
            repository.authoritative = repository.revisions["r0"]

            with self.assertRaisesRegex(
                EvolveLifecycleError,
                "not on trusted authoritative revision r0",
            ):
                reconcile_evolve_candidate(
                    candidate_id,
                    root,
                    repository=repository,
                    review=review,
                )

            self.assertEqual(
                [
                    event.event
                    for event in load_evolve_events(root, candidate_id=candidate_id)
                ],
                [],
            )

    def test_reconcile_refuses_open_review(self) -> None:
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_review(
                root,
                repository,
                review,
                landed=False,
            )

            with self.assertRaisesRegex(EvolveLifecycleError, "is not landed"):
                reconcile_evolve_candidate(
                    candidate_id,
                    root,
                    repository=repository,
                    review=review,
                )

    def test_backfill_and_restart_adoption_preserve_recording_mode(self) -> None:
        repository = BranchlessRepositoryFixture()
        review = IndependentReviewFixture()
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_review(
                root,
                repository,
                review,
            )
            promoted = reconcile_evolve_candidate(
                candidate_id,
                root,
                recording_mode="backfill",
                repository=repository,
                review=review,
            )
            version = RepositoryRevision(VERSION)
            repository.revisions[VERSION] = version
            repository.parents[VERSION] = MERGE_COMMIT
            repository.current = version
            repository.authoritative = version
            staged = stage_promoted_evolve_adoptions(
                root,
                VERSION,
                health_check="passed",
                repository=repository,
            )
            adopted = finalize_promoted_evolve_adoptions(
                root,
                running_version=VERSION,
                repository=repository,
            )
            duplicate = finalize_promoted_evolve_adoptions(
                root,
                running_version=VERSION,
                repository=repository,
            )

            events = load_evolve_events(root, candidate_id=candidate_id)
            raw_events = [
                json.loads(line)
                for line in (root / ".enoch" / "artifacts" / "evolve_events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertEqual(promoted.recording_mode, "backfill")
        self.assertEqual(len(staged), 1)
        self.assertEqual(len(adopted), 1)
        self.assertEqual(duplicate, ())
        self.assertEqual([event.event for event in events], ["promoted", "adopted"])
        adoption = adopted[0]
        self.assertEqual(adoption.event_actor, "system")
        self.assertEqual(adoption.trigger, "daemon-startup")
        self.assertEqual(adoption.version, VERSION)
        self.assertEqual(adoption.revision_id, MERGE_COMMIT)
        self.assertEqual(adoption.health_check, "passed")
        self.assertEqual(adoption.recording_mode, "backfill")
        self.assertEqual(raw_events[0]["recording_mode"], "backfill")
        self.assertEqual(raw_events[1]["recording_mode"], "backfill")
        self.assertFalse(pending_adoption_path(root).exists())


def _completed_candidate_with_review(
    root: Path,
    repository: BranchlessRepositoryFixture,
    review: IndependentReviewFixture,
    *,
    landed: bool = True,
) -> str:
    message = "I want improved governed evolution evidence."
    log_conversation_turn(chat_id=42, message=message, reply="Understood.", root=root)
    scan = scan_evidence(
        "feedback",
        root,
        force=True,
        generator=lambda prompt: _feedback_evidence_response(prompt, message),
    )
    created = synthesize_evolve_candidates_from_evidence(
        root,
        mission="Evolve safely.",
        generator=lambda _prompt: json.dumps(
            [
                {
                    "evidence_ids": [scan.evidence[0].id],
                    "title": "Improve governed evolution evidence",
                    "rationale": "The explicit feedback supports a bounded improvement.",
                    "proposed_change": "Add one focused evidence guardrail.",
                    "expected_benefit": "Evolution evidence remains reliable.",
                    "risk": "The guardrail may need fixture maintenance.",
                    "test_plan": "Run lifecycle tests and doctor.",
                }
            ]
        ),
    )
    if len(created) != 1:
        raise AssertionError("Expected one evidence-backed evolve candidate.")
    candidate_id = created[0].id
    run_evolve_candidate(candidate_id, root)
    complete_evolve_candidate(candidate_id, root)
    candidate = get_evolve_candidate(candidate_id, root)
    job = enqueue_task(
        42,
        "Implement governed evolution evidence",
        root,
        source=candidate.source,
        initiated_by="human",
        trigger="/evolve approve",
        candidate_id=candidate.id,
        evidence_source=candidate.evidence_source,
        signal_actor=candidate.signal_actor,
        candidate_actor=candidate.candidate_actor,
        approval_actor="human",
    )
    running = begin_next_task(root)
    if running is None:
        raise AssertionError("Expected queued evolve task.")
    revision = RepositoryRevision(MERGE_COMMIT)
    repository.revisions[revision.id] = revision
    repository.parents[revision.id] = repository.current.id
    repository.current = revision
    repository.authoritative = revision
    published = review.publish_review(
        ReviewSubmission(
            title="Improve governed evolution evidence",
            body="",
            revision=revision,
        )
    )
    if landed:
        review.land_review(ReviewLandRequest(published.identity))
    claim_running_task(job.id, "worker-one", 1, root)
    record_task_publication(
        job.id,
        "worker-one",
        TaskPublicationState(
            stage="review_published",
            revision_id=revision.id,
            workspace_id="workspace-independent",
            review_id=published.identity.id,
            review_url=published.identity.url,
            review_published=True,
        ),
        root,
    )
    complete_task(
        job.id,
        root,
        result=f"Published review: {published.identity.url}",
        worker_id="worker-one",
    )
    return candidate_id


def _feedback_evidence_response(prompt: str, message: str) -> str:
    records = json.loads(prompt.split("Evidence input: ", 1)[1])
    record = next(item for item in records if item["user_message"] == message)
    return json.dumps(
        [
            {
                "observation": message,
                "evidence_type": "explicit feedback",
                "affected_area": "evolution governance",
                "desired_outcome": "Governed evolution evidence is more reliable.",
                "confidence": 1.0,
                "explicit": True,
                "evidence_refs": [record["ref"]],
            }
        ]
    )


if __name__ == "__main__":
    unittest.main()
