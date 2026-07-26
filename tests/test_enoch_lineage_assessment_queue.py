from __future__ import annotations

from pathlib import Path
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.lineage.assessment_queue import (
    COMPLETED,
    FAILED,
    PENDING,
    RUNNING,
    claim_lineage_assessment,
    complete_lineage_assessment,
    enqueue_lineage_assessment,
    fail_lineage_assessment,
    load_lineage_assessment_queue,
)


class EnochLineageAssessmentQueueTests(unittest.TestCase):
    def test_job_lifecycle_is_durable(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job, created = enqueue_lineage_assessment(
                42,
                ("our-ark/enoch#32", "our-ark/enoch#32", " parent@abc "),
                root,
                new_count=2,
            )

            pending = load_lineage_assessment_queue(root)
            claimed = claim_lineage_assessment("epoch-a", root)
            duplicate_claim = claim_lineage_assessment("epoch-a", root)
            finished = complete_lineage_assessment(
                job.id,
                root,
                owner_epoch="epoch-a",
                assessed_count=1,
                failed_count=1,
            )
            saved = load_lineage_assessment_queue(root)

        self.assertTrue(created)
        self.assertEqual(job.status, PENDING)
        self.assertEqual(job.candidate_ids, ("our-ark/enoch#32", "parent@abc"))
        self.assertEqual(pending.current, job)
        assert claimed is not None
        self.assertEqual(claimed.status, RUNNING)
        self.assertEqual(claimed.owner_epoch, "epoch-a")
        self.assertEqual(claimed.attempts, 1)
        self.assertIsNone(duplicate_claim)
        assert finished is not None
        self.assertEqual(finished.status, COMPLETED)
        self.assertEqual(finished.assessed_count, 1)
        self.assertEqual(finished.failed_count, 1)
        self.assertIsNone(saved.current)
        self.assertEqual(saved.last, finished)

    def test_new_daemon_reclaims_an_interrupted_running_job(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job, _ = enqueue_lineage_assessment(
                "telegram:42",
                ("our-ark/enoch#32",),
                root,
            )
            first = claim_lineage_assessment("old-epoch", root)
            recovered = claim_lineage_assessment("new-epoch", root)

        assert first is not None
        assert recovered is not None
        self.assertEqual(first.id, job.id)
        self.assertEqual(recovered.id, job.id)
        self.assertEqual(recovered.status, RUNNING)
        self.assertEqual(recovered.owner_epoch, "new-epoch")
        self.assertEqual(recovered.attempts, 2)

    def test_only_one_assessment_job_can_be_queued(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first, first_created = enqueue_lineage_assessment(
                42,
                ("our-ark/enoch#32",),
                root,
            )
            second, second_created = enqueue_lineage_assessment(
                42,
                ("our-ark/enoch#33",),
                root,
            )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second, first)

    def test_failed_job_is_preserved_as_the_last_result(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job, _ = enqueue_lineage_assessment(
                42,
                ("our-ark/enoch#32",),
                root,
            )
            claim_lineage_assessment("epoch-a", root)

            failed = fail_lineage_assessment(
                job.id,
                " runtime  unavailable ",
                root,
                owner_epoch="epoch-a",
            )
            saved = load_lineage_assessment_queue(root)

        assert failed is not None
        self.assertEqual(failed.status, FAILED)
        self.assertEqual(failed.error, "runtime unavailable")
        self.assertIsNone(saved.current)
        self.assertEqual(saved.last, failed)

    def test_stale_daemon_cannot_finish_a_reclaimed_job(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            job, _ = enqueue_lineage_assessment(
                42,
                ("our-ark/enoch#32",),
                root,
            )
            claim_lineage_assessment("old-epoch", root)
            recovered = claim_lineage_assessment("new-epoch", root)

            stale_result = complete_lineage_assessment(
                job.id,
                root,
                owner_epoch="old-epoch",
                assessed_count=1,
                failed_count=0,
            )
            current = load_lineage_assessment_queue(root).current

        self.assertIsNone(stale_result)
        self.assertEqual(current, recovered)


if __name__ == "__main__":
    unittest.main()
