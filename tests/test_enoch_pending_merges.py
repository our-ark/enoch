"""Hands-off ``/pr merge``: land a requested review now, or when it is ready.

Covers the provider-agnostic mechanism: the merge request, the pending-merge
state, the throttled watcher, notifications, ``/pr cancel``, ``/pr`` listing,
and the task board section. It also covers forges with and without the
optional ``forge.check-status`` / ``forge.update-branch`` capabilities. GitHub
specifics (gh calls) are tested in the github workflow and library tests.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libraries" / "github" / "src"))

from enoch.app.core import EnochApplication
from enoch.app.pending_merges import (
    PendingMergeController,
    load_pending_merges,
    pending_merges_path,
)
from enoch.app.task_board import render_task_board
from enoch.commands import pr_usage
from enoch.identity import load_identity
from enoch.providers.forge import FunctionForgeProvider
from enoch.tasks.queue import TaskQueueStatus
from our_ark_github.workflow import PublishError, _require_mergeable_pull_request
from our_ark_provider_kit import (
    PullRequestBranchUpdateResult,
    PullRequestMergeResult,
    PullRequestMergeStatus,
    ReviewBaseFreshness,
    ReviewCheck,
    ReviewCheckFailure,
    ReviewCheckReport,
    ReviewCommit,
    ReviewRevisionComparison,
)
from tests.test_enoch_application import _Chat, _Runtime


URL = "https://github.com/our-ark/enoch/pull/91"
START = datetime(2026, 11, 24, 18, 0, tzinfo=timezone.utc)


class _Forge:
    """An in-memory forge whose merge refuses exactly like the GitHub forge."""

    def __init__(
        self,
        *,
        merge_state: str = "CLEAN",
        checks: tuple[ReviewCheck, ...] = (),
        check_status: bool = True,
        update_branch: bool = True,
        verify_update: bool | None = None,
        verified_update_heads: bool = True,
        ci_repair: bool = False,
        base_freshness: bool = False,
    ) -> None:
        self.status = _status(merge_state_status=merge_state)
        self.checks = checks
        self.updates: list[str] = []
        # Every update call, and errors the next calls raise (in order).
        self.update_attempts: list[str] = []
        self.update_errors: list[Exception] = []
        self.merges: list[str] = []
        self.verified_update_heads = verified_update_heads
        # forge.ci-repair fakes: failure evidence, re-runs, and a commit graph.
        self.failures: tuple[ReviewCheckFailure, ...] | None = None
        self.reruns: list[str] = []
        self.parents: dict[str, str] = {}
        self.subjects: dict[str, str] = {}
        self.contains_base = True
        # forge.base-freshness fake: heads that lack the current base tip.
        self.stale_heads: set[str] = set()
        self.freshness_error: Exception | None = None
        self.inspect = MagicMock(side_effect=lambda reference, root=None: self.status)
        optional = {}
        if check_status:
            optional["checks_fn"] = MagicMock(side_effect=self._checks)
        if update_branch:
            optional["update_branch_fn"] = MagicMock(side_effect=self._update)
        if verify_update is not None:
            optional["verify_update_fn"] = MagicMock(return_value=verify_update)
        if ci_repair:
            optional["check_failures_fn"] = MagicMock(side_effect=self._failures)
            optional["rerun_checks_fn"] = MagicMock(side_effect=self._rerun)
            optional["compare_fn"] = MagicMock(side_effect=self._compare)
        if base_freshness:
            optional["base_freshness_fn"] = MagicMock(side_effect=self._freshness)
        self.provider = FunctionForgeProvider(
            close_fn=MagicMock(),
            create_fn=MagicMock(),
            inspect_fn=MagicMock(),
            inspect_merge_fn=self.inspect,
            list_fn=MagicMock(return_value=()),
            merge_fn=MagicMock(side_effect=self._merge),
            **optional,
        )

    def set(self, *, checks: tuple[ReviewCheck, ...] | None = None, **status) -> None:
        if status:
            self.status = replace(self.status, **status)
        if checks is not None:
            self.checks = checks

    def _checks(self, reference, root=None) -> ReviewCheckReport:
        return ReviewCheckReport(revision=self.status.head_sha, checks=self.checks)

    def push(self, head: str, *, subject: str = "", parent: str = "") -> None:
        """Simulate a push on top of the current head; CI restarts on it."""

        self.parents[head] = parent or self.status.head_sha
        self.subjects[head] = subject
        self.status = replace(self.status, head_sha=head)
        self.checks = (ReviewCheck("CI / test", "pending"),)

    def _failures(self, reference, *, head_sha, root=None) -> tuple[ReviewCheckFailure, ...]:
        if head_sha != self.status.head_sha:
            raise AssertionError("failure evidence must be read for the inspected head")
        if self.failures is not None:
            return self.failures
        return tuple(
            ReviewCheckFailure(
                check.name,
                check.detail or "failure",
                run_id="4544",
                job_id="1386",
                log="FAILED tests/test_x.py::test_y - AssertionError",
            )
            for check in self.checks
            if check.state == "failure"
        )

    def _rerun(self, reference, *, head_sha, root=None) -> tuple[str, ...]:
        if head_sha != self.status.head_sha:
            raise AssertionError("re-runs must be pinned to the inspected head")
        self.reruns.append(head_sha)
        self.checks = tuple(
            ReviewCheck(check.name, "pending") if check.state == "failure" else check
            for check in self.checks
        )
        return ("4544",)

    def _compare(self, reference, *, base, head, root=None) -> ReviewRevisionComparison:
        if base == "main":
            return ReviewRevisionComparison(base, head, self.contains_base)
        commits = []
        cursor = head
        while cursor != base and cursor in self.parents:
            commits.append(ReviewCommit(cursor, self.subjects.get(cursor, "")))
            cursor = self.parents[cursor]
        descends = cursor == base
        return ReviewRevisionComparison(
            base, head, descends, tuple(reversed(commits)) if descends else ()
        )

    def _freshness(self, reference, *, head_sha="", root=None) -> ReviewBaseFreshness:
        if self.freshness_error is not None:
            raise self.freshness_error
        head = head_sha or self.status.head_sha
        return ReviewBaseFreshness("main", "main-tip", head, 1 if head in self.stale_heads else 0)

    def _update(self, reference, *, expected_head_sha, root=None) -> PullRequestBranchUpdateResult:
        if expected_head_sha != self.status.head_sha:
            raise AssertionError("update must be pinned to the inspected head")
        self.update_attempts.append(expected_head_sha)
        if self.update_errors:
            raise self.update_errors.pop(0)
        previous = self.status.head_sha
        new_head = f"update-{len(self.updates) + 1}"
        self.updates.append(previous)
        # GitHub restarts CI on the updated head.
        self.status = replace(self.status, head_sha=new_head, merge_state_status="BLOCKED")
        self.checks = (ReviewCheck("CI / test", "pending"),)
        return PullRequestBranchUpdateResult(
            number=91,
            url=URL,
            base_branch="main",
            previous_head_sha=previous,
            head_sha=new_head if self.verified_update_heads else "",
        )

    def _merge(self, reference, root=None) -> PullRequestMergeResult:
        _require_mergeable_pull_request(self.status)
        self.merges.append(self.status.head_sha)
        self.status = replace(
            self.status,
            state="MERGED",
            merged_at="2026-11-24T19:00:00Z",
            merge_commit="merged-91",
        )
        return PullRequestMergeResult(
            number=91, url=URL, method="merge", merge_commit="merged-91", message="Merged."
        )


class PendingMergeTests(unittest.TestCase):
    def setUp(self) -> None:
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = START

    def _app(self, forge: _Forge, chat: _Chat | None = None) -> EnochApplication:
        app = EnochApplication(
            load_identity(),
            self.root,
            chat or _Chat(),
            runtime=_Runtime(),
            review=_review(forge),
        )
        app.pending_merges._now = lambda: self.now
        app.pending_merges.interval_seconds = 0
        return app

    def _entries(self):
        return load_pending_merges(self.root)

    # Merge requests ---------------------------------------------------

    def test_ready_review_merges_immediately(self) -> None:
        forge = _Forge(checks=(ReviewCheck("CI / test", "success"),))
        app = self._app(forge)

        reply = app._pr("room-1", "merge 91")

        self.assertTrue(reply.startswith("Review "), reply)
        self.assertIn(": landed.", reply)
        self.assertEqual(forge.merges, ["head-91"])
        self.assertEqual(self._entries(), ())

    def test_unstable_review_with_finished_green_checks_merges_immediately(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("lint", "success"),))

        reply = self._app(forge)._pr("room-1", "merge 91")

        self.assertIn(": landed.", reply)
        self.assertEqual(forge.merges, ["head-91"])

    def test_red_ci_is_refused_immediately_even_when_not_required(self) -> None:
        forge = _Forge(
            merge_state="UNSTABLE",
            checks=(ReviewCheck("lint", "failure"), ReviewCheck("test", "success")),
        )

        reply = self._app(forge)._pr("room-1", "merge 91")

        self.assertEqual(reply, "❌ Not merging #91: CI failed (lint).")
        self.assertEqual(forge.merges, [])
        self.assertEqual(self._entries(), ())

    def test_ci_running_records_pending_then_merges_on_a_later_cycle(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)

        reply = app._pr("room-1", "merge 91")

        self.assertEqual(
            reply,
            "PR #91 isn't ready yet (CI running). I'll merge it automatically when it is.",
        )
        self.assertEqual(forge.merges, [])
        (entry,) = self._entries()
        self.assertEqual(entry.number, 91)
        self.assertEqual(entry.url, URL)
        self.assertEqual(entry.repository, "our-ark/enoch")
        self.assertEqual(entry.authorized_head_sha, "head-91")
        self.assertEqual(entry.allowed_head_shas, ("head-91",))
        self.assertEqual(entry.chat_id, "room-1")
        self.assertEqual(entry.requested_at, "2026-11-24T18:00:00Z")
        stored = json.loads(pending_merges_path(self.root).read_text())
        self.assertEqual(stored["merges"][0]["authorized_head_sha"], "head-91")

        app.pending_merges.tick()
        self.assertEqual(forge.merges, [])
        self.assertEqual(chat.sent, [])

        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()

        self.assertEqual(forge.merges, ["head-91"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])
        self.assertEqual(self._entries(), ())

    def test_behind_review_is_updated_then_merged(self) -> None:
        forge = _Forge(merge_state="BEHIND", checks=(ReviewCheck("CI / test", "success"),))
        chat = _Chat()
        app = self._app(forge, chat)

        reply = app._pr("room-1", "merge 91")

        self.assertEqual(
            reply,
            "PR #91 isn't ready yet (behind main / CI running). "
            "I'll merge it automatically when it is.",
        )
        self.assertEqual(forge.updates, ["head-91"])
        (entry,) = self._entries()
        self.assertEqual(entry.authorized_head_sha, "head-91")
        self.assertEqual(entry.allowed_head_shas, ("head-91", "update-1"))
        self.assertEqual(entry.waiting_for, "updating from main")

        app.pending_merges.tick()
        self.assertEqual(forge.merges, [])
        self.assertEqual(self._entries()[0].waiting_for, "waiting for CI")

        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()

        self.assertEqual(forge.merges, ["update-1"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_second_behind_after_main_moves_again_updates_again(self) -> None:
        forge = _Forge(merge_state="BEHIND")
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        # Another PR landed on main while CI ran on update-1.
        forge.set(merge_state_status="BEHIND", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()

        self.assertEqual(forge.updates, ["head-91", "update-1"])
        self.assertEqual(
            self._entries()[0].allowed_head_shas,
            ("head-91", "update-1", "update-2"),
        )
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()

        self.assertEqual(forge.merges, ["update-2"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_failed_update_is_retried_each_cycle_then_stops_with_the_reason(self) -> None:
        forge = _Forge(merge_state="BEHIND", checks=(ReviewCheck("CI / test", "success"),))
        reason = "GitHub returned HTTP 422 Unprocessable Entity with an empty response"
        forge.update_errors = [
            PublishError(f"Could not update PR #91 from main: {reason}") for _ in range(3)
        ]
        chat = _Chat()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as record:
            reply = app._pr("room-1", "merge 91")
            self.assertIn("isn't ready yet (behind main", reply)
            (entry,) = self._entries()
            self.assertEqual(entry.update_failures, 1)
            self.assertEqual(entry.waiting_for, "updating from main")

            app.pending_merges.tick()
            self.assertEqual(self._entries()[0].update_failures, 2)
            self.assertEqual(chat.sent, [])

            app.pending_merges.tick()

        self.assertEqual(forge.update_attempts, ["head-91"] * 3)
        self.assertEqual(forge.updates, [])
        self.assertEqual(forge.merges, [])
        self.assertEqual(self._entries(), ())
        self.assertEqual(
            chat.sent,
            [("room-1", f"⏹ Not merging #91: could not update the branch from main ({reason}).")],
        )
        failed = [
            call.kwargs["details"]
            for call in record.call_args_list
            if call.args[0] == "pending_merge_update_failed"
        ]
        self.assertEqual([details["failures"] for details in failed], [1, 2, 3])
        (stopped,) = [
            call.kwargs["details"]
            for call in record.call_args_list
            if call.args[0] == "pending_merge_stopped"
        ]
        self.assertEqual(stopped["reason"], "update-failed")
        self.assertIn(reason, stopped["error"])

    def test_a_successful_update_resets_the_failure_count(self) -> None:
        forge = _Forge(merge_state="BEHIND")
        forge.update_errors = [PublishError("Could not update PR #91 from main: HTTP 502")] * 2
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")
        app.pending_merges.tick()
        self.assertEqual(self._entries()[0].update_failures, 2)

        app.pending_merges.tick()  # third attempt succeeds
        (entry,) = self._entries()
        self.assertEqual((entry.update_failures, entry.update_error), (0, ""))
        self.assertEqual(entry.allowed_head_shas, ("head-91", "update-1"))

        # main moves again; two more failures do not stop the merge.
        forge.update_errors = [PublishError("Could not update PR #91 from main: HTTP 502")] * 2
        forge.set(merge_state_status="BEHIND", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()
        app.pending_merges.tick()
        self.assertEqual(self._entries()[0].update_failures, 2)
        self.assertEqual(chat.sent, [])

        app.pending_merges.tick()
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()
        self.assertEqual(forge.merges, ["update-2"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_update_failures_survive_a_daemon_restart(self) -> None:
        forge = _Forge(merge_state="BEHIND")
        forge.update_errors = [PublishError("Could not update PR #91 from main: HTTP 502")] * 3
        self._app(forge)._pr("room-1", "merge 91")
        self._app(forge).pending_merges.tick()

        stored = json.loads(pending_merges_path(self.root).read_text())["merges"][0]
        self.assertEqual(stored["update_failures"], 2)
        self.assertEqual(stored["update_error"], "Could not update PR #91 from main: HTTP 502")

        chat = _Chat()
        self._app(forge, chat).pending_merges.tick()
        self.assertEqual(
            chat.sent,
            [("room-1", "⏹ Not merging #91: could not update the branch from main (HTTP 502).")],
        )

    def test_unverified_update_head_is_accepted_only_when_the_forge_verifies_it(self) -> None:
        for verified, merged in ((True, ["update-1"]), (False, [])):
            with self.subTest(verified=verified):
                pending_merges_path(self.root).unlink(missing_ok=True)
                forge = _Forge(
                    merge_state="BEHIND",
                    verify_update=verified,
                    verified_update_heads=False,
                )
                chat = _Chat()
                app = self._app(forge, chat)
                app._pr("room-1", "merge 91")
                self.assertEqual(self._entries()[0].unverified_update_from, "head-91")

                forge.set(
                    merge_state_status="CLEAN",
                    checks=(ReviewCheck("CI / test", "success"),),
                )
                app.pending_merges.tick()

                self.assertEqual(forge.merges, merged)
                if not verified:
                    self.assertIn("new commits were pushed", chat.sent[-1][1])

    def test_foreign_push_cancels(self) -> None:
        forge = _Forge(merge_state="BLOCKED", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        forge.set(
            head_sha="pushed-by-someone",
            merge_state_status="CLEAN",
            checks=(ReviewCheck("CI / test", "success"),),
        )
        app.pending_merges.tick()

        self.assertEqual(forge.merges, [])
        self.assertEqual(
            chat.sent,
            [
                (
                    "room-1",
                    "⏹ Not merging #91: new commits were pushed after your merge request. "
                    "Ask again to merge the new version.",
                )
            ],
        )
        self.assertEqual(self._entries(), ())

    def test_failed_check_cancels_with_check_names(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        forge.set(
            checks=(
                ReviewCheck("CI / test", "failure", "failure"),
                ReviewCheck("lint", "failure", "cancelled"),
                ReviewCheck("docs", "failure", "timed_out"),
                ReviewCheck("build", "success"),
            )
        )
        app.pending_merges.tick()

        self.assertEqual(forge.merges, [])
        self.assertEqual(
            chat.sent,
            [("room-1", "❌ Not merging #91: CI failed (CI / test, lint, docs).")],
        )
        self.assertEqual(self._entries(), ())

    def test_conflicts_are_refused_and_never_resolved(self) -> None:
        forge = _Forge(merge_state="DIRTY")
        forge.set(mergeable="CONFLICTING")
        app = self._app(forge)

        reply = app._pr("room-1", "merge 91")

        self.assertIn("could not land that review: PR #91 has merge conflicts.", reply)
        self.assertEqual(forge.updates, [])
        self.assertEqual(self._entries(), ())

        forge.set(mergeable="MERGEABLE", merge_state_status="BLOCKED",
                  checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")
        forge.set(mergeable="CONFLICTING", merge_state_status="DIRTY")
        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "❌ Not merging #91: it has merge conflicts.")])
        self.assertEqual(forge.updates, [])
        self.assertEqual(forge.merges, [])
        self.assertEqual(self._entries(), ())

    def test_draft_and_closed_reviews_are_refused_immediately(self) -> None:
        for overrides, expected in (
            ({"is_draft": True}, "is a draft"),
            ({"state": "CLOSED"}, "is closed"),
        ):
            with self.subTest(expected=expected):
                forge = _Forge(merge_state="BEHIND")
                forge.set(**overrides)

                reply = self._app(forge)._pr("room-1", "merge 91")

                self.assertIn(expected, reply)
                self.assertEqual(forge.updates, [])
                self.assertEqual(self._entries(), ())

    def test_review_merged_elsewhere_is_not_an_error(self) -> None:
        forge = _Forge(merge_state="BLOCKED", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        forge.set(state="MERGED", merged_at="2026-11-24T18:30:00Z", merge_commit="elsewhere")
        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "✅ #91 was already merged.")])
        self.assertEqual(forge.merges, [])
        self.assertEqual(self._entries(), ())
        self.assertIn("Already merged", app._pr("room-1", "merge 91"))

    def test_closed_while_waiting_stops(self) -> None:
        forge = _Forge(merge_state="BLOCKED", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        forge.set(state="CLOSED")
        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "⏹ Not merging #91: it was closed.")])
        self.assertEqual(self._entries(), ())

    def test_gives_up_after_two_hours(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        self.now = START + timedelta(hours=1, minutes=59)
        app.pending_merges.tick()
        self.assertEqual(chat.sent, [])
        self.now = START + timedelta(hours=2, minutes=1)
        app.pending_merges.tick()

        self.assertEqual(
            chat.sent,
            [
                (
                    "room-1",
                    "⏹ Not merging #91: still not ready after 2 hours (waiting for CI). "
                    "Ask again to merge it when it is ready.",
                )
            ],
        )
        self.assertEqual(forge.merges, [])
        self.assertEqual(self._entries(), ())

    def test_blocked_with_green_checks_notifies_once_and_keeps_waiting(self) -> None:
        forge = _Forge(merge_state="BLOCKED", checks=(ReviewCheck("CI / test", "success"),))
        chat = _Chat()
        app = self._app(forge, chat)

        reply = app._pr("room-1", "merge 91")
        app.pending_merges.tick()
        app.pending_merges.tick()

        self.assertEqual(
            reply,
            "PR #91 isn't ready yet (blocked, not by CI). I'll merge it automatically when it is.",
        )
        self.assertEqual(len(chat.sent), 1)
        self.assertTrue(chat.sent[0][1].startswith("⏸ #91 is blocked for a non-CI reason"))
        self.assertTrue(self._entries()[0].blocked_notified)

        forge.set(merge_state_status="CLEAN")
        app.pending_merges.tick()
        self.assertEqual(chat.sent[-1], ("room-1", "✅ Merged #91."))

    def test_pending_merges_survive_a_daemon_restart(self) -> None:
        forge = _Forge(merge_state="BEHIND")
        self._app(forge)._pr("room-1", "merge 91")

        chat = _Chat()
        restarted = self._app(forge, chat)
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        restarted.pending_merges.tick()

        self.assertEqual(forge.merges, ["update-1"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_repeated_request_refreshes_without_duplicating(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self._app(forge)
        app._pr("room-1", "merge 91")
        first = self._entries()[0]

        self.now = START + timedelta(minutes=30)
        forge.set(head_sha="head-91b")
        app._pr("room-1", f"merge {URL}")

        (entry,) = self._entries()
        self.assertEqual(entry.requested_at, "2026-11-24T18:30:00Z")
        self.assertEqual(entry.authorized_head_sha, "head-91b")
        self.assertEqual(entry.allowed_head_shas, ("head-91b",))
        self.assertNotEqual(entry.requested_at, first.requested_at)

    def test_pr_cancel_stops_a_pending_merge(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        self.assertIn("locked", app._pr("room-2", "cancel 91"))
        self.assertEqual(len(self._entries()), 1)
        reply = app._pr("room-1", "cancel 91")
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.pending_merges.tick()

        self.assertEqual(
            reply,
            "Cancelled the pending merge for #91. It will not be merged automatically.",
        )
        self.assertEqual(self._entries(), ())
        self.assertEqual(forge.merges, [])
        self.assertEqual(chat.sent, [])
        self.assertEqual(app._pr("room-1", "cancel 91"), "No pending merge for 91.")

    def test_pr_list_shows_pending_merges(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self._app(forge)
        self.assertEqual(app._pr("room-1", ""), "Open reviews: none.")

        app._pr("room-1", "merge 91")
        listing = app._pr("room-1", "")

        self.assertIn("Open reviews: none.", listing)
        self.assertIn("Pending merges (1):", listing)
        self.assertIn(f"- #91 (waiting for CI) requested at", listing)
        self.assertIn(URL, listing)
        self.assertEqual(app._pr("room-1", "list"), listing)

    def test_other_conversation_cannot_request_a_merge(self) -> None:
        forge = _Forge(merge_state="BEHIND")

        reply = self._app(forge)._pr("room-2", "merge 91")

        self.assertIn("locked", reply)
        self.assertEqual(forge.updates, [])
        self.assertEqual(self._entries(), ())

    # Watcher ------------------------------------------------------------

    def test_watcher_is_throttled(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self._app(forge)
        app._pr("room-1", "merge 91")
        clock = [1000.0]
        app.pending_merges._monotonic = lambda: clock[0]
        app.pending_merges.interval_seconds = 60
        inspections = forge.inspect.call_count

        app.pending_merges.tick()
        self.assertEqual(forge.inspect.call_count, inspections + 1)
        clock[0] += 59
        app.pending_merges.tick()
        self.assertEqual(forge.inspect.call_count, inspections + 1)
        clock[0] += 2
        app.pending_merges.tick()
        self.assertEqual(forge.inspect.call_count, inspections + 2)

    def test_run_once_drives_the_watcher_and_never_raises(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        forge.inspect.side_effect = RuntimeError("forge is down")
        with patch("enoch.app.core._record_system_event") as record:
            app.run_once()
        failures = [call for call in record.call_args_list if call.args[0] == "pending_merge_check_failed"]
        self.assertEqual(len(failures), 1)
        self.assertIn("forge is down", failures[0].kwargs["details"]["error"])
        self.assertEqual(len(self._entries()), 1)

        forge.inspect.side_effect = lambda reference, root=None: forge.status
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        app.run_once()

        self.assertEqual(forge.merges, ["head-91"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_watcher_records_system_events(self) -> None:
        forge = _Forge(merge_state="BEHIND")
        with patch("enoch.app.core._record_system_event") as record:
            app = self._app(forge)
            app._pr("room-1", "merge 91")
            forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
            app.pending_merges.tick()

        events = [call.args[0] for call in record.call_args_list]
        self.assertIn("pending_merge_requested", events)
        self.assertIn("pending_merge_branch_updated", events)
        self.assertIn("pending_merge_landed", events)

    # Board, help, and forges without the capabilities ------------------------

    def test_board_shows_merging_section_only_when_non_empty(self) -> None:
        empty = TaskQueueStatus(
            pending_count=0, paused_count=0, running=None, pending=(), paused=(), history=()
        )
        without = render_task_board(empty, updated_at=START)
        with_merge = render_task_board(
            empty, updated_at=START, merging=("#91 (waiting for CI)",)
        )

        self.assertNotIn("Merging", without.text)
        self.assertIn("Merging:\n- #91 (waiting for CI)", with_merge.text)
        self.assertNotEqual(without.digest, with_merge.digest)

        forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        app = self._app(forge)
        self.assertNotIn("Merging", app._render_task_board().text)
        app._pr("room-1", "merge 91")
        self.assertIn("Merging:\n- #91 (waiting for CI)", app._render_task_board().text)

    def test_forge_without_check_status_keeps_refusing_with_reason(self) -> None:
        forge = _Forge(merge_state="BEHIND", check_status=False, update_branch=False)
        app = self._app(forge)

        reply = app._pr("room-1", "merge 91")

        self.assertFalse(app.pending_merges.supported())
        self.assertIn(
            "could not land that review: PR #91 is behind main and must be updated "
            "before it can merge.",
            reply,
        )
        self.assertEqual(self._entries(), ())

        forge.set(merge_state_status="CLEAN")
        self.assertIn(": landed.", app._pr("room-1", "merge 91"))
        self.assertEqual(forge.merges, ["head-91"])

    def test_forge_with_checks_but_no_branch_update_refuses_behind(self) -> None:
        forge = _Forge(merge_state="BEHIND", update_branch=False)

        reply = self._app(forge)._pr("room-1", "merge 91")

        self.assertEqual(
            reply,
            "PR #91 is behind main and must be updated before it can merge. "
            "This forge cannot update review branches automatically.",
        )
        self.assertEqual(self._entries(), ())

    def test_pr_help_describes_hands_off_merging(self) -> None:
        usage = pr_usage()

        self.assertIn("/pr cancel <review id or URL>", usage)
        self.assertIn("pending merges", usage)
        self.assertIn("lands it automatically when it is ready", usage)
        self.assertIn("2 hours", usage)
        self.assertIn("conflicted, draft and closed reviews are refused right away", usage)
        self.assertIn("end to end", usage)
        self.assertIn("covered by your merge request and listed in the final merged message", usage)
        self.assertIn("commits from anyone else cancel the merge", usage)
        self.assertIn("re-run once", usage)
        self.assertIn("up to 2 attempts", usage)
        self.assertIn("6 hours overall", usage)
        self.assertIn("never merged without a request", usage)

    def test_corrupt_entries_are_skipped_on_load(self) -> None:
        path = pending_merges_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"merges": [{"number": 1}, "junk"]}))

        self.assertEqual(load_pending_merges(self.root), ())
        self.assertIsInstance(
            PendingMergeController(
                self.root,
                review=lambda: None,
                authorize=lambda *args: None,
                run_authorized=lambda *args, **kwargs: None,
                notify=lambda *args: None,
                record_event=lambda *args, **kwargs: None,
            ).list_lines(),
            tuple,
        )


def _review(forge: _Forge):
    from our_ark_provider_kit import as_review_provider

    return as_review_provider(forge.provider)


def _status(**overrides) -> PullRequestMergeStatus:
    values = {
        "reference": "91",
        "url": URL,
        "state": "OPEN",
        "base_branch": "main",
        "merge_commit": "",
        "merged_at": "",
        "number": 91,
        "repository": "our-ark/enoch",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "head_sha": "head-91",
    }
    values.update(overrides)
    return PullRequestMergeStatus(**values)


if __name__ == "__main__":
    unittest.main()
