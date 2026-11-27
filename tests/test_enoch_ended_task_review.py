"""A task that ends unfinished after opening its PR records that PR.

When a task times out, fails or is stopped, Enoch looks for an open review
opened from the task's own branch (its head is the pushed tip of that branch).
A found review is recorded on the task, mentioned in the final message and
watched for CI, but never merged automatically: the task did not finish, so
the review may be incomplete. A retry continues on that review instead of
opening a duplicate, and the task's worktree is kept while the review is open.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.core import EnochApplication, TaskContextSnapshot, _task_worker_context
from enoch.app.execution_context import CURRENT_TASK_WORKER_ID
from enoch.app.models import WorkOutcome
from enoch.app.pending_merges import load_ci_watches, load_pending_merges
from enoch.app.reporting import _format_task_list_item
from enoch.identity import load_identity
from enoch.providers.contracts import AgentRuntimeCancelled, AgentRuntimeTimedOut
from enoch.providers.forge import ForgeProviderError
from enoch.tasks.config import save_pr_auto_merge
from enoch.tasks.queue import (
    begin_next_task,
    complete_task,
    enqueue_task,
    fail_task,
    record_ended_task_reviews,
    task_has_unfinished_review,
)
from enoch.tasks.worktree import TaskWorktreeState
from enoch.workflows import EndedTaskReviewWorkflow, LocalWorkflowEngine
from our_ark_provider_kit import ReviewCheck
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_pending_merges import URL, _Forge, _review


TASK_BRANCH = "enoch/example-task-branch"
OTHER_BRANCH_TIP = "head-of-another-branch"


def _pushes_then(app: EnochApplication, task_id: int, outcome):
    """A stand-in for ``_run_direct_work``: works on the task branch, then ends.

    ``outcome`` is an exception to raise or a value to return.
    """

    def run(*_args, **_kwargs):
        recorded = app.workflow.record_workspace(
            task_id, CURRENT_TASK_WORKER_ID.get(), app.root / "task-worktree", TASK_BRANCH
        )
        assert recorded is not None
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return outcome()
        return outcome

    return run


class EndedTaskReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _forge(self, *, open_pr: bool = True) -> _Forge:
        forge = _Forge(merge_state="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        # The forge lists PR #91 as open; its head is "head-91".
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
        outcome,
        *,
        tip: str = "head-91",
        merge_policy: str = "",
    ):
        """Run one queued task on its own branch, whose pushed tip is ``tip``."""

        enqueue_task("room-1", "build a feature", self.root, merge_policy=merge_policy)
        job = begin_next_task(self.root)
        with patch.object(
            app, "_run_direct_work", side_effect=_pushes_then(app, job.id, outcome)
        ), patch.object(app, "_remote_branch_revision", return_value=tip) as remote, patch.object(
            app, "_record_turn"
        ) as turn, patch("enoch.app.core.remove_task_worktree") as remove:
            app._run_task_job(job)
        self.turn = turn
        self.remote = remote
        # A task that ended unfinished keeps its worktree and branch.
        remove.assert_not_called()
        return app.workflow.find(job.id)

    def _assert_no_merge(self, app: EnochApplication, forge: _Forge) -> None:
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(forge.merges, [])
        self.assertIsNone(app._land_hold())

    # Timeout ---------------------------------------------------------------

    def test_timeout_after_opening_a_pr_records_watches_and_reports_it(self) -> None:
        # Both auto-merge routes are on; neither may merge an unfinished task's PR.
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(app, AgentRuntimeTimedOut("deadline"), merge_policy="auto")

        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.failure_code, "task_timeout")
        self.assertEqual(finished.review_urls, (URL,))
        self.assertTrue(task_has_unfinished_review(finished))
        self.remote.assert_called_with(TASK_BRANCH)
        # Shown in /queue.
        self.assertIn(f"Review: {URL}", _format_task_list_item(finished))
        # CI is watched under the task's id.
        (watch,) = load_ci_watches(self.root)
        self.assertEqual((watch.url, watch.task_id), (URL, finished.id))
        # Never merged, never holding the queue, and a human merge is not linked to it.
        self._assert_no_merge(app, forge)
        self.assertEqual(app._task_for_review(URL), 0)
        final = chat.sent[-1][1]
        self.assertIn("failed: task_timeout", final)
        self.assertIn(f"Timed out after opening PR #91 ({URL}); CI is being watched.", final)
        self.assertIn("not merged automatically", final)
        self.assertIn("Say 'merge #91' to land it", final)
        self.assertIn("/task retry 1 to continue on it", final)
        self.assertNotIn("Auto-merge:", final)
        # The conversation remembers the PR too.
        self.assertIn("Timed out after opening PR #91", self.turn.call_args.args[2])
        recorded = [call.args[0] for call in events.call_args_list]
        self.assertIn("task_ended_with_review", recorded)
        self.assertNotIn("pr_auto_merge_requested", recorded)

    def test_watched_pr_of_a_timed_out_task_is_not_merged_when_ci_passes(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)
        self._run(app, AgentRuntimeTimedOut("deadline"), merge_policy="auto")

        app.pending_merges.tick()

        self.assertIn("✅ PR #91 CI passed. Ready to merge.", [text for _, text in chat.sent])
        self._assert_no_merge(app, forge)

    def test_full_final_message_lists_the_pr(self) -> None:
        chat = _Chat()
        app = self._app(self._forge(), chat)
        app._compact_task_messages = lambda: False

        self._run(app, AgentRuntimeTimedOut("deadline"))

        final = chat.sent[-1][1]
        self.assertIn(f"Review URL:\n- {URL}", final)
        self.assertIn("Timed out after opening PR #91", final)

    def test_timeout_without_a_pr_is_unchanged(self) -> None:
        for case in ("branch not pushed", "no open PR from the branch", "PR from another branch"):
            with self.subTest(case=case):
                chat = _Chat()
                forge = self._forge(open_pr=case != "no open PR from the branch")
                app = self._app(forge, chat)
                tip = {
                    "branch not pushed": "",
                    "no open PR from the branch": "head-91",
                    "PR from another branch": OTHER_BRANCH_TIP,
                }[case]

                finished = self._run(app, AgentRuntimeTimedOut("deadline"), tip=tip)

                self.assertEqual(finished.status, "failed")
                self.assertEqual(finished.failure_code, "task_timeout")
                self.assertEqual(finished.review_urls, ())
                self.assertFalse(task_has_unfinished_review(finished))
                self.assertEqual(load_ci_watches(self.root), ())
                final = chat.sent[-1][1]
                self.assertEqual(
                    final.splitlines()[0],
                    f"❌ #{finished.id} failed: task_timeout · retry with /task retry {finished.id}",
                )
                self.assertNotIn("after opening", final)
                self.assertNotIn("after opening", self.turn.call_args.args[2])
                # Only a pushed branch costs a forge lookup.
                self.assertEqual(
                    forge.provider.list_fn.call_count, 0 if case == "branch not pushed" else 1
                )

    def test_forge_lookup_error_keeps_the_plain_failure_message(self) -> None:
        chat = _Chat()
        forge = self._forge()
        forge.provider.list_fn.side_effect = ForgeProviderError("GitHub is down")
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(app, AgentRuntimeTimedOut("deadline"))

        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.failure_code, "task_timeout")
        self.assertEqual(finished.review_urls, ())
        self.assertEqual(load_ci_watches(self.root), ())
        final = chat.sent[-1][1]
        self.assertIn("failed: task_timeout", final)
        self.assertNotIn("after opening", final)
        failed = [
            call for call in events.call_args_list if call.args[0] == "task_review_lookup_failed"
        ]
        self.assertEqual(len(failed), 1)
        self.assertIn("GitHub is down", failed[0].kwargs["details"]["error"])

    def test_lookup_never_raises(self) -> None:
        app = self._app(self._forge())
        fail = enqueue_task("room-1", "build a feature", self.root)
        begin_next_task(self.root)
        failed = fail_task(fail.id, self.root, "Task exceeded the configured 1h timeout.",
                           failure_code="task_timeout")
        with patch.object(app, "_task_branch_tip", side_effect=RuntimeError("git exploded")):
            self.assertEqual(app._ended_task_review(failed), (failed, ""))
        # Not for tasks that did not end unfinished.
        completed = enqueue_task("room-1", "other work", self.root)
        begin_next_task(self.root)
        done = complete_task(completed.id, self.root, "done")
        with patch.object(app, "_task_branch_tip") as tip:
            self.assertEqual(app._ended_task_review(done), (done, ""))
        tip.assert_not_called()

    def test_pr_named_in_the_request_is_not_the_tasks_own(self) -> None:
        app = self._app(self._forge())
        job = enqueue_task("room-1", f"review {URL}", self.root)
        begin_next_task(self.root)
        failed = fail_task(job.id, self.root, "boom", failure_code="unknown_failure")
        with patch.object(app, "_task_branch_tip", return_value="head-91"):
            self.assertEqual(app._ended_task_review(failed), (failed, ""))
        self.assertEqual(app.workflow.find(job.id).review_urls, ())

    # Failed and stopped ----------------------------------------------------

    def test_failed_task_with_a_pr_is_recorded_and_not_merged(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)

        finished = self._run(
            app,
            WorkOutcome.failure("Doctor failed after publishing.", code="validation_failed"),
        )

        self.assertEqual(finished.status, "failed")
        self.assertEqual(finished.review_urls, (URL,))
        (watch,) = load_ci_watches(self.root)
        self.assertEqual(watch.task_id, finished.id)
        self._assert_no_merge(app, forge)
        final = chat.sent[-1][1]
        self.assertIn(f"Failed after opening PR #91 ({URL}); CI is being watched.", final)
        self.assertIn("Say 'merge #91' to land it", final)

    def test_stopped_task_with_a_pr_is_recorded_and_not_merged(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)

        def stop():
            self.assertEqual(app._stop_running_job(), "Stopped task #1.")
            raise AgentRuntimeCancelled("cancelled")

        finished = self._run(app, stop)

        self.assertEqual(finished.status, "cancelled")
        self.assertEqual(finished.review_urls, (URL,))
        self.assertTrue(task_has_unfinished_review(finished))
        (watch,) = load_ci_watches(self.root)
        self.assertEqual(watch.task_id, finished.id)
        self._assert_no_merge(app, forge)
        final = chat.sent[-1][1]
        self.assertIn("cancelled", final)
        self.assertIn(f"Stopped after opening PR #91 ({URL}); CI is being watched.", final)

    def test_paused_task_is_not_looked_up(self) -> None:
        from enoch.providers.contracts import AgentRuntimeAccessUnavailable

        forge = self._forge()
        app = self._app(forge)

        with patch.object(app, "_record_runtime_access_pause"):
            finished = self._run(app, AgentRuntimeAccessUnavailable("usage limit reached"))

        self.assertEqual(finished.status, "paused")
        self.assertEqual(finished.review_urls, ())
        self.remote.assert_not_called()
        self.assertEqual(forge.provider.list_fn.call_count, 0)

    def test_engine_without_the_optional_method_still_reports_and_watches(self) -> None:
        class OlderWorkflow(LocalWorkflowEngine):
            record_ended_task_reviews = None  # type: ignore[assignment]

        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)
        app.workflow = OlderWorkflow(self.root)

        finished = self._run(app, AgentRuntimeTimedOut("deadline"))

        self.assertEqual(finished.review_urls, ())  # not stored by this engine
        (watch,) = load_ci_watches(self.root)
        self.assertEqual(watch.url, URL)
        self.assertIn("Timed out after opening PR #91", chat.sent[-1][1])

    # Retry -----------------------------------------------------------------

    def test_retry_mentions_the_pr_and_continues_on_it(self) -> None:
        chat = _Chat()
        forge = self._forge()
        app = self._app(forge, chat)
        original = self._run(app, AgentRuntimeTimedOut("deadline"))

        reply = app._retry_task(original.id)

        self.assertEqual(
            reply,
            f"Queued #2 (retry of #1). It continues on PR #91 ({URL}) on branch "
            f"{TASK_BRANCH} instead of opening a new PR.",
        )
        retry = app.workflow.find(2)
        self.assertEqual(retry.parent_task_id, original.id)
        self.assertEqual(retry.workspace_id, TASK_BRANCH)
        self.assertEqual(retry.review_urls, (URL,))
        # The retry's worker is told to continue on the PR and its branch.
        context = _task_worker_context(retry, workflow=app.workflow)
        self.assertIn("Existing pull request from the previous attempt", context)
        self.assertIn("do not open a new pull request", context)
        self.assertIn(URL, context)
        self.assertIn(TASK_BRANCH, context)

        # The retry runs the worker; the existing PR does not make it "done".
        begin_next_task(self.root)
        work = MagicMock(return_value=f"Finished the work and pushed it to {URL}")
        with patch.object(app, "_run_direct_work", work), patch.object(
            app, "_remote_branch_revision", return_value="head-91"
        ):
            app._run_task_job(retry)
        work.assert_called_once()
        self.assertIn("Existing pull request", work.call_args.kwargs["context"])
        self.assertEqual(app.workflow.find(retry.id).status, "completed")

    def test_full_retry_status_mentions_the_pr(self) -> None:
        chat = _Chat()
        app = self._app(self._forge(), chat)
        original = self._run(app, AgentRuntimeTimedOut("deadline"))
        app._compact_task_messages = lambda: False

        app._retry_task(original.id)

        status = chat.sent[-1][1]
        self.assertIn(f"Retry of failed task #{original.id} queued at position 1.", status)
        self.assertIn(f"It continues on PR #91 ({URL}) on branch {TASK_BRANCH}", status)

    def test_retry_of_a_task_without_a_pr_is_unchanged(self) -> None:
        app = self._app(self._forge(open_pr=False))
        original = self._run(app, AgentRuntimeTimedOut("deadline"))

        self.assertEqual(app._retry_task(original.id), "Queued #2 (retry of #1).")
        retry = app.workflow.find(2)
        self.assertNotIn(
            "Existing pull request", _task_worker_context(retry, workflow=app.workflow)
        )

    # Worktree --------------------------------------------------------------

    def test_worktree_is_kept_while_the_pr_is_open(self) -> None:
        app = self._app(self._forge())
        original = self._run(app, AgentRuntimeTimedOut("deadline"))
        state = TaskWorktreeState(
            task_id=original.id, path=self.root / "task-worktree", branch=TASK_BRANCH
        )

        with patch("enoch.app.core.task_worktree_state", return_value=state), patch(
            "enoch.app.core.remove_managed_task_worktree"
        ) as remove:
            for action in (f"cleanup {original.id}", f"discard {original.id} force"):
                with self.subTest(action=action):
                    reply = app._worktree("room-1", action)
                    self.assertIn("will not remove task #1 worktree while PR #91", reply)
            remove.assert_not_called()

            # Once the PR is closed, removal works as before.
            app.review = _review(self._closed_forge())
            app._worktree("room-1", f"cleanup {original.id}")
            remove.assert_called_once()

    def _closed_forge(self) -> _Forge:
        forge = self._forge()
        forge.set(state="CLOSED")
        return forge


class QueueTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def _ended(self, status: str):
        job = enqueue_task("room-1", "work", self.root)
        begin_next_task(self.root)
        if status == "completed":
            return complete_task(job.id, self.root, "done")
        return fail_task(job.id, self.root, "boom")

    def test_record_ended_task_reviews_only_for_failed_or_cancelled(self) -> None:
        failed = self._ended("failed")
        updated = record_ended_task_reviews(failed.id, (URL, "not a url"), self.root)
        self.assertEqual(updated.review_urls, (URL,))
        # Idempotent.
        self.assertEqual(record_ended_task_reviews(failed.id, (URL,), self.root).review_urls, (URL,))
        completed = self._ended("completed")
        self.assertIsNone(record_ended_task_reviews(completed.id, (URL,), self.root))
        self.assertIsNone(record_ended_task_reviews(999, (URL,), self.root))
        self.assertEqual(LocalWorkflowEngine(self.root).find(completed.id).review_urls, ())

    def test_local_engine_implements_the_optional_protocol(self) -> None:
        engine = LocalWorkflowEngine(self.root)
        self.assertIsInstance(engine, EndedTaskReviewWorkflow)
        failed = self._ended("failed")
        self.assertEqual(engine.record_ended_task_reviews(failed.id, (URL,)).review_urls, (URL,))

    def test_unfinished_review_marker(self) -> None:
        from dataclasses import replace

        failed = record_ended_task_reviews(self._ended("failed").id, (URL,), self.root)
        self.assertTrue(task_has_unfinished_review(failed))
        self.assertTrue(task_has_unfinished_review(replace(failed, status="cancelled")))
        # Not when Enoch's own publish step recorded the review: retries resume it.
        self.assertFalse(task_has_unfinished_review(replace(failed, review_published=True)))
        self.assertFalse(task_has_unfinished_review(replace(failed, publish_stage="pr_opened")))
        self.assertFalse(task_has_unfinished_review(replace(failed, status="completed")))
        self.assertFalse(task_has_unfinished_review(replace(failed, review_urls=())))


if __name__ == "__main__":
    unittest.main()
