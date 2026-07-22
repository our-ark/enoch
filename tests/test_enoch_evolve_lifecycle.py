from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.backlog import add_backlog_item
from enoch.evolution.core import (
    complete_evolve_candidate,
    get_evolve_candidate,
    run_evolve_candidate,
)
from enoch.evolution.events import load_evolve_events
from enoch.evolution.lifecycle import (
    EvolveLifecycleError,
    finalize_promoted_evolve_adoptions,
    pending_adoption_path,
    reconcile_evolve_candidate,
    stage_promoted_evolve_adoptions,
)
from our_ark_github.workflow import PullRequestMergeStatus
from enoch.tasks.queue import (
    begin_next_task,
    complete_task,
    enqueue_task,
    record_task_result,
)


PR_URL = "https://github.com/our-ark/enoch/pull/12"
MERGE_COMMIT = "7207317aabbccddeeff001122334455667788990"
VERSION = "9999999aabbccddeeff001122334455667788990"


class EnochEvolveLifecycleTests(unittest.TestCase):
    @patch(
        "enoch.evolution.lifecycle.revision_merged_into_origin_main",
        return_value=True,
    )
    @patch("enoch.evolution.lifecycle.fetch_origin_main")
    @patch("enoch.evolution.lifecycle.inspect_pull_request_merge")
    def test_reconcile_records_verified_human_promotion_once(
        self,
        inspect_pull_request_merge,
        _fetch_origin_main,
        revision_on_main,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_pr(root)
            inspect_pull_request_merge.return_value = _merged_pr()

            first = reconcile_evolve_candidate(candidate_id, root)
            second = reconcile_evolve_candidate(candidate_id, root)
            events = load_evolve_events(root, candidate_id=candidate_id)

        self.assertFalse(first.already_recorded)
        self.assertTrue(second.already_recorded)
        self.assertEqual(
            [event.event for event in events],
            ["promoted"],
        )
        event = first.event
        self.assertEqual(event.event_actor, "human")
        self.assertEqual(event.pr_url, PR_URL)
        self.assertEqual(event.merge_commit, MERGE_COMMIT)
        self.assertEqual(event.authoritative_branch, "main")
        self.assertEqual(event.promoted_at, "2026-07-18T18:30:00Z")
        self.assertTrue(event.verified_at)
        self.assertEqual(event.recording_mode, "realtime")
        revision_on_main.assert_called_with(MERGE_COMMIT, root)

    @patch(
        "enoch.evolution.lifecycle.revision_merged_into_origin_main",
        return_value=False,
    )
    @patch("enoch.evolution.lifecycle.fetch_origin_main")
    @patch("enoch.evolution.lifecycle.inspect_pull_request_merge")
    def test_reconcile_refuses_merge_commit_outside_trusted_main(
        self,
        inspect_pull_request_merge,
        _fetch_origin_main,
        _revision_on_main,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_pr(root)
            inspect_pull_request_merge.return_value = _merged_pr()

            with self.assertRaisesRegex(
                EvolveLifecycleError,
                "not on trusted origin/main",
            ):
                reconcile_evolve_candidate(candidate_id, root)

            self.assertEqual(
                [
                    event.event
                    for event in load_evolve_events(root, candidate_id=candidate_id)
                ],
                [],
            )

    @patch("enoch.evolution.lifecycle.inspect_pull_request_merge")
    def test_reconcile_refuses_open_pull_request(
        self,
        inspect_pull_request_merge,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_pr(root)
            inspect_pull_request_merge.return_value = PullRequestMergeStatus(
                reference=PR_URL,
                url=PR_URL,
                state="OPEN",
                base_branch="main",
                merge_commit="",
                merged_at="",
            )

            with self.assertRaisesRegex(EvolveLifecycleError, "is not merged"):
                reconcile_evolve_candidate(candidate_id, root)

    @patch(
        "enoch.evolution.lifecycle.revision_merged_into_origin_main",
        return_value=True,
    )
    @patch("enoch.evolution.lifecycle.fetch_origin_main")
    @patch("enoch.evolution.lifecycle.inspect_pull_request_merge")
    def test_backfill_and_restart_adoption_preserve_recording_mode(
        self,
        inspect_pull_request_merge,
        _fetch_origin_main,
        _revision_on_main,
    ) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            candidate_id = _completed_candidate_with_pr(root)
            inspect_pull_request_merge.return_value = _merged_pr()
            promoted = reconcile_evolve_candidate(
                candidate_id,
                root,
                recording_mode="backfill",
            )

            with patch(
                "enoch.evolution.lifecycle._is_ancestor",
                return_value=True,
            ):
                staged = stage_promoted_evolve_adoptions(
                    root,
                    VERSION,
                    health_check="passed",
                )
            adopted = finalize_promoted_evolve_adoptions(
                root,
                running_version=VERSION,
            )
            duplicate = finalize_promoted_evolve_adoptions(
                root,
                running_version=VERSION,
            )

            events = load_evolve_events(root, candidate_id=candidate_id)
            raw_events = [
                json.loads(line)
                for line in (root / ".enoch" / "evolve_events.jsonl")
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
        self.assertEqual(adoption.merge_commit, MERGE_COMMIT)
        self.assertEqual(adoption.health_check, "passed")
        self.assertEqual(adoption.recording_mode, "backfill")
        self.assertEqual(raw_events[0]["recording_mode"], "backfill")
        self.assertEqual(raw_events[1]["recording_mode"], "backfill")
        self.assertFalse(pending_adoption_path(root).exists())


def _completed_candidate_with_pr(root: Path) -> str:
    item = add_backlog_item(42, "Improve governed evolution evidence", root, priority="p0")
    candidate_id = f"backlog-{item.id}"
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
    record_task_result(job.id, f"Opened pull request: {PR_URL}", root)
    complete_task(job.id, root, result=f"Opened pull request: {PR_URL}")
    return candidate_id


def _merged_pr() -> PullRequestMergeStatus:
    return PullRequestMergeStatus(
        reference=PR_URL,
        url=PR_URL,
        state="MERGED",
        base_branch="main",
        merge_commit=MERGE_COMMIT,
        merged_at="2026-07-18T18:30:00Z",
    )


if __name__ == "__main__":
    unittest.main()
