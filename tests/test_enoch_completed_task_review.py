"""A completed task keeps its own PR even when its final message has no link.

A task's worker can open its PR itself and finish with "opened PR #125" and no
URL. When neither the publish stage nor a verified URL in the result gives the
task a review, Enoch looks for the review whose head is the tip of the task's
own pushed branch. It first checks a "PR #N" reference it can verify, then
lists the open reviews once. A review found this way is the task's own,
exactly like one from the publish stage: it is recorded, watched for CI, merged
under the normal policy for completed tasks, and holds the queue until it
lands. References that cannot be verified never count, and a PR that only
belongs to another task is never attributed.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.auto_update import LandedReview
from enoch.app.core import (
    CI_REPAIR_TRIGGER,
    EnochApplication,
    TaskContextSnapshot,
    _review_numbers_in_text,
)
from enoch.app.execution_context import CURRENT_TASK_ID, CURRENT_TASK_WORKER_ID
from enoch.app.pending_merges import load_ci_watches, load_pending_merges
from enoch.app.reporting import _format_task_list_item
from enoch.identity import load_identity
from enoch.providers.contracts import AgentRuntimeTimedOut
from enoch.providers.forge import ForgeProviderError
from enoch.tasks.config import save_pr_auto_merge
from enoch.tasks.queue import begin_next_task, enqueue_task, task_queue_status
from our_ark_provider_kit import ReviewCheck
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_pending_merges import START, URL, _Forge, _review, _status


TASK_BRANCH = "enoch/example-task-branch"
OTHER_URL = "https://github.com/our-ark/enoch/pull/77"
FOREIGN_URL = "https://github.com/someone-else/project/pull/91"
# The real worker starter, before setUp replaces it with a mock.
_START_WORKER = EnochApplication._maybe_start_task_worker


def _runs_on_branch(app: EnochApplication, reply):
    """A stand-in for ``_run_direct_work``: works on the task branch, returns ``reply``.

    ``reply`` may be an exception to raise instead.
    """

    def run(*_args, **_kwargs):
        recorded = app.workflow.record_workspace(
            CURRENT_TASK_ID.get(),
            CURRENT_TASK_WORKER_ID.get(),
            app.root / "task-worktree",
            TASK_BRANCH,
        )
        assert recorded is not None
        if isinstance(reply, BaseException):
            raise reply
        return reply

    return run


class CompletedTaskReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _forge(self, *, ready: bool = False, open_pr: bool = True) -> _Forge:
        """PR #91 (head "head-91"), open and listed unless ``open_pr`` is False."""

        if ready:
            forge = _Forge(checks=(ReviewCheck("CI / test", "success"),))
        else:
            forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        forge.provider.list_fn.return_value = (forge.status,) if open_pr else ()
        return forge

    def _app(self, forge: _Forge, chat: _Chat | None = None) -> EnochApplication:
        app = EnochApplication(
            load_identity(),
            self.root,
            chat or _Chat(),
            runtime=_Runtime(),
            review=_review(forge),
        )
        app.pending_merges._now = lambda: START
        app.pending_merges.interval_seconds = 0
        app._authoritative_repository = lambda: "our-ark/enoch"
        app._resolve_task_context_snapshot = MagicMock(
            return_value=TaskContextSnapshot(context="", source="chat-snapshot")
        )
        app._compact_task_messages = lambda: True
        return app

    def _run(
        self,
        app: EnochApplication,
        reply,
        *,
        tip: str = "head-91",
        request: str = "build a feature",
        **options,
    ):
        """Run one queued task on its own branch, whose pushed tip is ``tip``."""

        enqueue_task("room-1", request, self.root, **options)
        job = begin_next_task(self.root)
        with patch.object(
            app, "_run_direct_work", side_effect=_runs_on_branch(app, reply)
        ), patch.object(app, "_remote_branch_revision", return_value=tip) as remote:
            app._run_task_job(job)
        self.remote = remote
        return app.workflow.find(job.id)

    def _worker_starts(self, app: EnochApplication) -> bool:
        app._run_task_worker = MagicMock()
        app._task_worker = None
        _START_WORKER(app)
        if app._task_worker is None:
            return False
        app._task_worker.join(5)
        return app._run_task_worker.called

    def _assert_recorded_and_watched(self, finished) -> None:
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.review_urls, (URL,))
        self.assertIn(f"Review: {URL}", _format_task_list_item(finished))
        (watch,) = load_ci_watches(self.root)
        self.assertEqual((watch.url, watch.task_id), (URL, finished.id))

    def _assert_not_attributed(self, app: EnochApplication, finished, forge: _Forge) -> None:
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.review_urls, ())
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(load_ci_watches(self.root), ())
        self.assertIsNone(app._land_hold())
        self.assertEqual(forge.merges, [])

    @staticmethod
    def _events(events, name: str) -> list:
        return [call for call in events.call_args_list if call.args[0] == name]

    # The task's own PR, found on its branch -------------------------------

    def test_pr_without_a_link_is_recorded_watched_merged_and_holds_the_queue(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(app, "Opened the PR from the task branch; CI is green.")

        self._assert_recorded_and_watched(finished)
        # One ls-remote of the task's own branch, then one listing.
        self.remote.assert_called_once_with(TASK_BRANCH)
        self.assertEqual(forge.provider.list_fn.call_count, 1)
        # The normal merge policy registered it at its head, held by this task.
        (entry,) = load_pending_merges(self.root)
        self.assertEqual((entry.number, entry.task_id), (91, finished.id))
        self.assertEqual(entry.authorized_head_sha, "head-91")
        self.assertEqual(app._land_hold().task_id, finished.id)
        final = chat.sent[-1][1]
        self.assertIn("Auto-merge:", final)
        self.assertIn("The next queued task waits for it to land.", final)
        self.assertEqual(self._events(events, "pr_auto_merge_skipped"), [])
        (found,) = self._events(events, "task_review_found_on_branch")
        self.assertEqual(
            found.kwargs["details"],
            {
                "task_id": finished.id,
                "workspace_id": TASK_BRANCH,
                "review": URL,
                "source": "task-branch",
            },
        )
        # A human merge of it later is linked to this task too.
        self.assertEqual(app._task_for_review(URL), finished.id)
        # Land before next: the next ordinary task waits for the PR to land.
        enqueue_task("room-1", "next feature", self.root)
        self.assertFalse(self._worker_starts(app))

    def test_ready_pr_without_a_link_is_merged_immediately(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge(ready=True)
        app = self._app(forge)

        with patch.object(app, "_after_requested_merge") as after:
            finished = self._run(app, "Done. Opened the pull request.")

        self.assertEqual(finished.review_urls, (URL,))
        self.assertEqual(forge.merges, ["head-91"])
        after.assert_called_once_with(LandedReview("#91", URL, "our-ark/enoch", "main"))
        self.assertIsNone(app._land_hold())

    def test_with_auto_merge_off_the_pr_is_recorded_and_watched_only(self) -> None:
        forge = self._forge(ready=True)
        app = self._app(forge)

        finished = self._run(app, "Opened the PR.")

        self._assert_recorded_and_watched(finished)
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(forge.merges, [])
        self.assertIsNone(app._land_hold())

    def test_merge_never_override_skips_the_merge(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge(ready=True)
        app = self._app(forge)

        finished = self._run(app, "Opened the PR.", merge_policy="never")

        self._assert_recorded_and_watched(finished)
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(forge.merges, [])

    def test_merge_auto_override_merges_with_the_setting_off(self) -> None:
        forge = self._forge()
        app = self._app(forge)

        finished = self._run(app, "Opened the PR.", merge_policy="auto")

        self._assert_recorded_and_watched(finished)
        (entry,) = load_pending_merges(self.root)
        self.assertEqual(entry.task_id, finished.id)

    def test_branch_never_pushed_costs_one_git_call_and_no_forge_call(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge()
        app = self._app(forge)

        # Even with a URL and a "PR #N" to verify, an unpushed branch owns nothing.
        finished = self._run(app, f"Opened PR #91; compare {OTHER_URL}.", tip="")

        self._assert_not_attributed(app, finished, forge)
        self.remote.assert_called_once_with(TASK_BRANCH)
        self.assertEqual(forge.provider.list_fn.call_count, 0)
        self.assertEqual(forge.inspect.call_count, 0)

    # "PR #N" references ------------------------------------------------------

    def test_verified_pr_number_in_the_result_is_the_tasks_pr(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge()
        app = self._app(forge)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(app, "Task done: opened PR #91; CI including descent is green.")

        self._assert_recorded_and_watched(finished)
        (entry,) = load_pending_merges(self.root)
        self.assertEqual(entry.task_id, finished.id)
        # Verified by inspecting PR 91 itself; no listing was needed.
        forge.inspect.assert_any_call("91", self.root)
        self.assertEqual(forge.provider.list_fn.call_count, 0)
        (found,) = self._events(events, "task_review_found_on_branch")
        self.assertEqual(found.kwargs["details"]["source"], "result-reference")

    def test_pr_number_whose_head_is_another_branch_never_counts(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge(open_pr=False)
        other = _status(url=OTHER_URL, reference="77", number=77, head_sha="head-77")
        forge.inspect.side_effect = lambda reference, root=None: (
            other if reference == "77" else forge.status
        )
        chat = _Chat()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(app, "Opened #77 for this.")

        self._assert_not_attributed(app, finished, forge)
        self.assertEqual(app._task_for_review(OTHER_URL), 0)
        self.assertIn("Not merging automatically: task #1 published no PR", chat.sent[-1][1])
        self.assertEqual(len(self._events(events, "pr_auto_merge_skipped")), 1)
        self.assertEqual(self._events(events, "task_review_found_on_branch"), [])

    def test_unverified_reference_does_not_displace_the_tasks_own_pr(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge()
        other = _status(url=OTHER_URL, reference="77", number=77, head_sha="head-77")
        forge.inspect.side_effect = lambda reference, root=None: (
            other if reference == "77" else forge.status
        )
        app = self._app(forge)

        finished = self._run(app, "Followed the approach of PR #77 from task #12.")

        # Only the PR on the task's own branch counts, found by the listing.
        self.assertEqual(finished.review_urls, (URL,))
        (entry,) = load_pending_merges(self.root)
        self.assertEqual((entry.number, entry.task_id), (91, finished.id))
        self.assertEqual([watch.url for watch in load_ci_watches(self.root)], [URL])
        self.assertEqual(app._task_for_review(OTHER_URL), 0)

    def test_reference_on_another_repository_never_counts(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge(open_pr=False)
        # Same head as the task branch, but not on the authoritative repository.
        forge.set(url=FOREIGN_URL, repository="someone-else/project")
        app = self._app(forge)

        finished = self._run(app, "Opened PR #91 upstream.")

        self._assert_not_attributed(app, finished, forge)

    def test_mention_of_another_tasks_pr_is_never_attributed(self) -> None:
        # The #120 rule: PR #91 belongs to another task's branch.
        save_pr_auto_merge(True, self.root)
        forge = self._forge()
        chat = _Chat()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(
                app,
                f"Nothing to do; {URL} (PR #91) already covers it.",
                tip="head-of-this-task",
            )

        self._assert_not_attributed(app, finished, forge)
        self.assertEqual(app._task_for_review(URL), 0)
        self.assertIn(URL, chat.sent[-1][1])  # still shown, as a plain reference
        # The URL was checked once; its bare "#91" is not inspected again.
        self.assertEqual(forge.inspect.call_count, 1)
        self.assertEqual(len(self._events(events, "task_review_references")), 1)
        self.assertEqual(self._events(events, "pr_auto_merge_requested"), [])

    def test_pr_the_request_names_is_not_the_tasks_own(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._forge()
        app = self._app(forge)

        finished = self._run(app, "Addressed the review.", request=f"address the review on {URL}")

        self._assert_not_attributed(app, finished, forge)

    # Errors ------------------------------------------------------------------

    def test_forge_error_completes_the_task_as_before(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._forge()
        forge.inspect.side_effect = ForgeProviderError("GitHub is down")
        forge.provider.list_fn.side_effect = ForgeProviderError("GitHub is down")
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(app, "Opened PR #91.")

        self._assert_not_attributed(app, finished, forge)
        self.assertEqual(finished.result, "Opened PR #91.")
        self.assertIn("completed", chat.sent[-1][1])
        (failed,) = self._events(events, "task_review_attribution_failed")
        self.assertIn("GitHub is down", failed.kwargs["details"]["error"])

    def test_lookup_never_raises(self) -> None:
        app = self._app(self._forge())
        enqueue_task("room-1", "build a feature", self.root)
        running = begin_next_task(self.root)
        with patch.object(
            app, "_completed_task_needs_review_lookup", return_value=True
        ), patch.object(
            app, "_task_branch_tip", side_effect=RuntimeError("git exploded")
        ), patch("enoch.app.core._record_system_event") as events:
            self.assertEqual(app._reviews_published_by(running, "done", completed=True), ())
        self.assertEqual(len(self._events(events, "task_review_attribution_failed")), 1)

    # Who looks ---------------------------------------------------------------

    def test_ci_fix_task_does_not_look_for_a_pr(self) -> None:
        forge = self._forge()
        app = self._app(forge)

        finished = self._run(
            app, "Pushed the fix.", trigger=CI_REPAIR_TRIGGER, initiated_by="agent"
        )

        self.assertEqual(finished.review_urls, ())
        self.remote.assert_not_called()
        self.assertEqual(forge.provider.list_fn.call_count, 0)

    def test_failed_task_keeps_the_ended_task_rules(self) -> None:
        # The #122 rules: recorded and watched, never merged, no queue hold.
        save_pr_auto_merge(True, self.root)
        forge = self._forge(ready=True)
        app = self._app(forge)

        finished = self._run(app, AgentRuntimeTimedOut("deadline"), merge_policy="auto")

        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.review_urls, (URL,))
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(forge.merges, [])
        self.assertIsNone(app._land_hold())

    def test_retry_that_carries_its_parents_pr_does_not_look_again(self) -> None:
        forge = self._forge()
        app = self._app(forge)
        original = self._run(app, AgentRuntimeTimedOut("deadline"))
        self.assertEqual(original.review_urls, (URL,))
        app._retry_task(original.id)
        retry = begin_next_task(self.root)
        listings = forge.provider.list_fn.call_count

        with patch.object(
            app, "_run_direct_work", side_effect=_runs_on_branch(app, "Finished the work.")
        ), patch.object(app, "_remote_branch_revision", return_value="head-91") as remote, patch(
            "enoch.app.core._record_system_event"
        ) as events:
            app._run_task_job(retry)

        finished = app.workflow.find(retry.id)
        self.assertEqual(finished.status, "completed")
        # It keeps exactly the PR it carried; nothing new is looked up.
        self.assertEqual(finished.review_urls, (URL,))
        remote.assert_not_called()
        self.assertEqual(forge.provider.list_fn.call_count, listings)
        self.assertEqual(self._events(events, "task_review_found_on_branch"), [])

    def test_inline_tracked_work_records_the_pr_too(self) -> None:
        forge = self._forge()
        app = self._app(forge)

        with patch.object(
            app, "_run_direct_work", side_effect=_runs_on_branch(app, "Opened the PR.")
        ), patch.object(app, "_remote_branch_revision", return_value="head-91"):
            app._run_tracked_inline_work(
                "room-1",
                "build a feature",
                source="chat-task",
                initiated_by="human",
                trigger="/do",
                session_key="room-1",
            )

        (finished,) = task_queue_status(self.root).history
        self._assert_recorded_and_watched(finished)


class ReviewNumberReferenceTests(unittest.TestCase):
    def test_explicit_pr_references_come_first_and_other_numbers_are_skipped(self) -> None:
        self.assertEqual(
            _review_numbers_in_text("Task #83 is done after #120: opened PR #125 (backlog #25)."),
            (125, 120),
        )

    def test_forms(self) -> None:
        self.assertEqual(
            _review_numbers_in_text(
                "PR#7, pull request #8, issue #9, https://x.test/a#10, &#11; and #7 again"
            ),
            (7, 8),
        )
        self.assertEqual(
            _review_numbers_in_text(f"Opened [#122]({URL.replace('91', '122')})"),
            (122,),
        )
        self.assertEqual(_review_numbers_in_text("No references here."), ())


if __name__ == "__main__":
    unittest.main()
