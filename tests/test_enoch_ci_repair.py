"""End-to-end ``/pr merge``: red CI is repaired before the merge lands.

Covers infrastructure re-runs, fix tasks queued at the front of the queue, the
authorization of fix-task pushes (option A: covered by the original merge
request and listed in the final message), the attempt limit, the paused
timeout and its overall cap, failures that also happen on the base branch,
conflicts, restarts, and the watch-only path for reviews nobody asked to merge,
including the cached status it keeps for the task board's open-review line.
The forge is an in-memory fake; GitHub specifics are tested with mocked gh in
the github workflow tests.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from enoch.app.ci_repair import (
    CI_FIX_RESULT_TAG,
    CiFixResult,
    ci_fix_task_brief,
    classify_check_failures,
    extract_ci_fix_results,
    load_ci_fix_result,
    record_ci_fix_result,
)
from enoch.app.core import EnochApplication
from enoch.app.execution_context import CURRENT_TASK_ID
from enoch.app.pending_merges import BoardReview, load_ci_watches, load_pending_merges
from enoch.identity import load_identity
from our_ark_provider_kit import ReviewCheck, ReviewCheckFailure
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_pending_merges import START, URL, _Forge, _review


RED = (ReviewCheck("CI / test", "failure", "failure"), ReviewCheck("lint", "success"))
GREEN = (ReviewCheck("CI / test", "success"), ReviewCheck("lint", "success"))
PENDING = (ReviewCheck("CI / test", "pending"),)


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = START
        self.task_states: dict[int, str] = {}

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
        real_status = app.pending_merges._task_status
        app.pending_merges._task_status = lambda task_id: self.task_states.get(
            task_id, real_status(task_id)
        )
        return app

    def _entry(self):
        (entry,) = load_pending_merges(self.root)
        return entry

    def _fix_tasks(self, app: EnochApplication):
        return tuple(job for job in app.workflow.inspect().pending if job.trigger == "ci-repair")

    def _push_fix(self, forge: _Forge, task_id: int, head: str, subject: str) -> None:
        previous = forge.status.head_sha
        forge.push(head, subject=subject)
        record_ci_fix_result(
            task_id,
            CiFixResult("pushed", head_sha=head, previous_head_sha=previous),
            self.root,
        )

    def _red_merge(self, chat: _Chat | None = None) -> tuple[_Forge, EnochApplication]:
        forge = _Forge(merge_state="BLOCKED", checks=PENDING, ci_repair=True)
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")
        forge.set(merge_state_status="UNSTABLE", checks=RED)
        return forge, app


class PendingMergeRepairTests(_Harness):
    def test_infrastructure_failure_is_rerun_once_and_merge_keeps_waiting(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        forge.failures = (
            ReviewCheckFailure(
                "CI / test", "failure", run_id="4544", job_id="1386",
                log="Error: The runner has received a shutdown signal.",
            ),
        )

        app.pending_merges.tick()

        self.assertEqual(forge.reruns, ["head-91"])
        self.assertEqual(self._entry().waiting_for, "re-running failed CI jobs")
        self.assertEqual(self._fix_tasks(app), ())
        self.assertEqual(chat.sent, [])

        # The re-run fails the same way: no second re-run, a fix task instead.
        forge.set(checks=RED)
        app.pending_merges.tick()

        self.assertEqual(forge.reruns, ["head-91"])
        self.assertEqual(len(self._fix_tasks(app)), 1)

    def test_green_rerun_lands_with_only_the_merged_notice(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        forge.failures = (ReviewCheckFailure("CI / test", "cancelled"),)

        app.pending_merges.tick()
        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.reruns, ["head-91"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_cancelled_and_timed_out_jobs_count_as_infrastructure(self) -> None:
        for detail in ("cancelled", "timed_out"):
            with self.subTest(detail=detail):
                result = classify_check_failures(
                    (ReviewCheckFailure("CI / test", detail, log=""),)
                )
                self.assertTrue(result.infrastructure, result)
        self.assertTrue(
            classify_check_failures(
                (ReviewCheckFailure("CI / test", "failure", log="curl: (6) Could not resolve host"),)
            ).infrastructure
        )
        for log in (
            "FAILED tests/test_x.py::test_y - AssertionError",
            "FAIL: test_merges (tests.test_x.Tests)\nFAILED (failures=1)",
            "Could not resolve host\n=== 1 failed, 20 passed in 3s ===",
            "",
        ):
            with self.subTest(log=log):
                self.assertFalse(
                    classify_check_failures(
                        (ReviewCheckFailure("CI / test", "failure", log=log),)
                    ).infrastructure
                )
        mixed = (
            ReviewCheckFailure("a", "cancelled"),
            ReviewCheckFailure("b", "failure", log="FAILED (errors=2)"),
        )
        self.assertFalse(classify_check_failures(mixed).infrastructure)

    def test_pr_caused_failure_queues_exactly_one_front_fix_task(self) -> None:
        chat = _Chat()
        forge = _Forge(merge_state="BLOCKED", checks=PENDING, ci_repair=True)
        app = self._app(forge, chat)
        app.workflow.enqueue("room-1", "Some other queued work.")
        app._pr("room-1", "merge 91")
        forge.set(merge_state_status="UNSTABLE", checks=RED)

        app.pending_merges.tick()
        app.pending_merges.tick()
        app.pending_merges.tick()

        (fix,) = self._fix_tasks(app)
        self.assertEqual(app.workflow.inspect().pending[0].id, fix.id)
        self.assertEqual(fix.initiated_by, "agent")
        self.assertIn("Fix the failing CI on PR #91", fix.text)
        self.assertIn("head head-91", fix.text)
        self.assertIn("gh run view", fix.text)
        self.assertIn("run 4544", fix.text)
        self.assertIn(CI_FIX_RESULT_TAG, fix.text)
        self.assertEqual(
            chat.sent,
            [
                (
                    "room-1",
                    f"🔧 #91 CI failed (CI / test); started task #{fix.id} to fix it, "
                    "will merge when green.",
                )
            ],
        )
        entry = self._entry()
        self.assertEqual(entry.waiting_for, f"fixing CI (task #{fix.id})")
        self.assertEqual(entry.repair.task_ids, (fix.id,))
        self.assertEqual(forge.merges, [])
        self.assertIn(f"#91 (fixing CI (task #{fix.id}))", app._render_task_board().text)
        self.assertIn(f"- #91 (fixing CI (task #{fix.id}))", app._pr("room-1", ""))

    def test_request_on_red_review_starts_the_fix_right_away(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=RED, ci_repair=True)
        app = self._app(forge)

        reply = app._pr("room-1", "merge 91")

        (fix,) = self._fix_tasks(app)
        self.assertEqual(
            reply,
            f"🔧 #91 CI failed (CI / test); started task #{fix.id} to fix it, "
            "will merge when green.",
        )
        self.assertEqual(self._entry().repair.task_ids, (fix.id,))

    def test_fix_push_is_accepted_and_listed_in_the_final_message(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        # The fix task pushes, then reports the pushed head before it finishes.
        self._push_fix(forge, fix.id, "fix1234567", "Fix the flaky merge test")
        app.pending_merges.tick()

        entry = self._entry()
        self.assertIn("fix1234567", entry.allowed_head_shas)
        self.assertEqual(entry.repair.attempts[0].state, "pushed")
        self.assertEqual(entry.waiting_for, "waiting for CI")

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.merges, ["fix1234567"])
        self.assertEqual(
            chat.sent[-1],
            (
                "room-1",
                f"✅ Merged #91 (+1 fix commit by task #{fix.id}: fix1234 Fix the flaky merge test).",
            ),
        )
        self.assertEqual(load_pending_merges(self.root), ())

    def test_foreign_push_cancels_even_while_a_fix_task_runs(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        forge.push("someone-else", subject="Unrelated change")
        app.pending_merges.tick()
        # Undecided while the fix task may still report this head.
        self.assertEqual(len(load_pending_merges(self.root)), 1)
        self.assertEqual(len(chat.sent), 1)

        self.task_states[fix.id] = "completed"
        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.merges, [])
        self.assertEqual(
            chat.sent[-1],
            (
                "room-1",
                "⏹ Not merging #91: new commits were pushed after your merge request. "
                "Ask again to merge the new version.",
            ),
        )
        self.assertEqual(load_pending_merges(self.root), ())

    def test_reported_push_that_does_not_descend_from_the_allowed_head_cancels(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        # Reports a head that was force-pushed on a different parent.
        forge.push("rewritten", subject="Rewrite", parent="elsewhere")
        record_ci_fix_result(
            fix.id,
            CiFixResult("pushed", head_sha="rewritten", previous_head_sha="head-91"),
            self.root,
        )
        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.merges, [])
        self.assertIn("new commits were pushed", chat.sent[-1][1])

    def test_push_reported_by_a_different_task_is_not_accepted(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        self._push_fix(forge, fix.id + 100, "other-task-head", "Not the fix task")
        self.task_states[fix.id] = "completed"
        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.merges, [])
        self.assertIn("new commits were pushed", chat.sent[-1][1])

    def test_at_most_two_fix_attempts_then_stop_with_task_ids(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (first,) = self._fix_tasks(app)
        self._push_fix(forge, first.id, "fix-a", "First try")
        self.task_states[first.id] = "completed"
        app.pending_merges.tick()
        forge.set(checks=RED)
        app.pending_merges.tick()

        second = [job for job in self._fix_tasks(app) if job.id != first.id]
        self.assertEqual(len(second), 1)
        self._push_fix(forge, second[0].id, "fix-b", "Second try")
        self.task_states[second[0].id] = "completed"
        app.pending_merges.tick()
        forge.set(checks=RED)
        app.pending_merges.tick()

        self.assertEqual(len(self._fix_tasks(app)), 2)
        self.assertEqual(forge.merges, [])
        self.assertEqual(
            chat.sent[-1],
            (
                "room-1",
                f"❌ Not merging #91: CI still fails (CI / test) after 2 automatic fix "
                f"attempts (tasks #{first.id}, #{second[0].id}).",
            ),
        )
        self.assertEqual(load_pending_merges(self.root), ())

    def test_fix_task_without_a_push_uses_an_attempt(self) -> None:
        forge, app = self._red_merge()
        app.pending_merges.tick()
        (first,) = self._fix_tasks(app)
        self.task_states[first.id] = "failed"

        app.pending_merges.tick()

        self.assertEqual(self._entry().repair.attempts[0].state, "no-push")
        self.assertEqual(len(self._entry().repair.attempts), 2)

    def test_timeout_pauses_while_fixing_and_the_six_hour_cap_holds(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        self.now = START + timedelta(minutes=30)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        # 3 hours of fixing do not count toward the 2-hour timeout.
        self.now = START + timedelta(hours=3, minutes=30)
        app.pending_merges.tick()
        self.assertEqual(len(load_pending_merges(self.root)), 1)
        self._push_fix(forge, fix.id, "fix-a", "Fix")
        app.pending_merges.tick()
        entry = self._entry()
        self.assertEqual(entry.repair.paused_seconds, 3 * 3600)
        self.assertEqual(entry.repair.pause_started_at, "")

        # Active waiting: 30 min before + 1h29 after the fix is still < 2h.
        self.now = START + timedelta(hours=4, minutes=59)
        app.pending_merges.tick()
        self.assertEqual(len(load_pending_merges(self.root)), 1)
        self.now = START + timedelta(hours=5, minutes=1)
        app.pending_merges.tick()
        self.assertIn("still not ready after 2 hours (waiting for CI)", chat.sent[-1][1])
        self.assertIn(f"Fix task: #{fix.id}.", chat.sent[-1][1])

    def test_six_hour_cap_stops_even_while_a_fix_task_is_running(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        self.now = START + timedelta(hours=5, minutes=59)
        app.pending_merges.tick()
        self.assertEqual(len(load_pending_merges(self.root)), 1)
        self.now = START + timedelta(hours=6)
        app.pending_merges.tick()

        self.assertEqual(
            chat.sent[-1],
            (
                "room-1",
                f"⏹ Not merging #91: still not ready after 6 hours (fixing CI (task #{fix.id})). "
                f"Ask again to merge it when it is ready. Fix task: #{fix.id}.",
            ),
        )
        self.assertEqual(load_pending_merges(self.root), ())

    def test_failure_that_also_happens_on_main_stops_and_notifies(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        record_ci_fix_result(
            fix.id,
            CiFixResult("main-failure", review_url="https://github.com/our-ark/enoch/pull/120"),
            self.root,
        )
        app.pending_merges.tick()

        self.assertEqual(
            chat.sent[-1],
            (
                "room-1",
                "⏹ Not merging #91: its CI failure also happens on main. Task "
                f"#{fix.id} opened https://github.com/our-ark/enoch/pull/120 to fix main; "
                "that fix needs its own merge. Ask again to merge #91 after it lands.",
            ),
        )
        self.assertEqual(forge.merges, [])
        self.assertEqual(load_pending_merges(self.root), ())

    def test_head_without_the_base_tip_is_updated_instead_of_fixed(self) -> None:
        # Without forge.base-freshness, ci-repair's compare answers the question.
        forge, app = self._red_merge()
        forge.contains_base = False

        app.pending_merges.tick()

        self.assertEqual(self._fix_tasks(app), ())
        self.assertEqual(forge.reruns, [])
        self.assertEqual(forge.updates, ["head-91"])
        entry = self._entry()
        self.assertEqual(entry.waiting_for, "updating from main")
        self.assertEqual(entry.allowed_head_shas, ("head-91", "update-1"))

    def test_conflicts_are_unchanged_and_never_fixed(self) -> None:
        chat = _Chat()
        forge, app = self._red_merge(chat)
        forge.set(mergeable="CONFLICTING", merge_state_status="DIRTY")

        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "❌ Not merging #91: it has merge conflicts.")])
        self.assertEqual(self._fix_tasks(app), ())
        self.assertEqual(forge.updates, [])
        self.assertIn("has merge conflicts", self._app(forge)._pr("room-1", "merge 91"))

    def test_forge_without_ci_repair_keeps_stopping_on_red_ci(self) -> None:
        chat = _Chat()
        forge = _Forge(merge_state="BLOCKED", checks=PENDING)
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")
        forge.set(merge_state_status="UNSTABLE", checks=RED)

        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "❌ Not merging #91: CI failed (CI / test).")])
        self.assertEqual(self._fix_tasks(app), ())

    def test_repair_state_survives_a_restart(self) -> None:
        forge, app = self._red_merge()
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)
        self._push_fix(forge, fix.id, "fix-a", "Fix after restart")

        chat = _Chat()
        restarted = self._app(forge, chat)
        restarted.pending_merges.tick()
        forge.set(merge_state_status="CLEAN", checks=GREEN)
        restarted.pending_merges.tick()

        self.assertEqual(forge.merges, ["fix-a"])
        self.assertEqual(
            chat.sent,
            [("room-1", f"✅ Merged #91 (+1 fix commit by task #{fix.id}: fix-a Fix after restart).")],
        )
        self.assertEqual(len(self._fix_tasks(restarted)), 1)

    def test_repeated_request_keeps_the_running_fix_task(self) -> None:
        forge, app = self._red_merge()
        app.pending_merges.tick()
        (fix,) = self._fix_tasks(app)

        app._pr("room-1", "merge 91")

        self.assertEqual(self._entry().repair.task_ids, (fix.id,))
        self.assertEqual(len(self._fix_tasks(app)), 1)

    def test_fix_task_result_is_captured_from_the_reply_and_hidden(self) -> None:
        forge = _Forge(ci_repair=True)
        app = self._app(forge)
        reply = (
            "Fixed the failing test.\n\n"
            f"[{CI_FIX_RESULT_TAG}]\n"
            '{"outcome": "pushed", "previous_head_sha": "head-91", "head_sha": "abc1234"}\n'
            f"[/{CI_FIX_RESULT_TAG}]"
        )
        token = CURRENT_TASK_ID.set(61)
        try:
            visible = app._capture_task_regression_signals(reply)
        finally:
            CURRENT_TASK_ID.reset(token)

        self.assertEqual(visible, "Fixed the failing test.")
        self.assertEqual(
            load_ci_fix_result(61, self.root),
            CiFixResult("pushed", head_sha="abc1234", previous_head_sha="head-91"),
        )
        self.assertIsNone(load_ci_fix_result(62, self.root))
        self.assertEqual(app._capture_task_regression_signals("plain"), "plain")

    def test_result_blocks_are_validated(self) -> None:
        for raw in (
            '{"outcome": "pushed", "head_sha": "abc"}',
            '{"outcome": "main-failure"}',
            '{"outcome": "merged"}',
            "not json",
        ):
            with self.subTest(raw=raw):
                extraction = extract_ci_fix_results(
                    f"x [{CI_FIX_RESULT_TAG}]{raw}[/{CI_FIX_RESULT_TAG}]"
                )
                self.assertEqual(extraction.results, ())
                self.assertEqual(extraction.visible_reply, "x")

    def test_fix_brief_keeps_the_guardrails(self) -> None:
        brief = ci_fix_task_brief(
            number=95,
            url="https://github.com/our-ark/enoch/pull/95",
            head="5d8d9be",
            base_branch="main",
            failures=(ReviewCheckFailure("test (3.12)", "failure", run_id="1", job_id="2"),),
            check_names=("test (3.12)",),
            goal="Merge pending.",
        )

        for text in (
            "PR #95's own branch",
            "git reset --hard 5d8d9be",
            "Never force-push",
            "separate PR based on main",
            "Never skip tests, mark them xfail, loosen assertions, or merge any PR",
            "Never edit the library pin lines",
            "Enoch re-pins every library after the merge",
            "EnochApplication._maybe_start_task_worker",
            "run 1, job 2",
            '"outcome": "main-failure"',
        ):
            self.assertIn(text, brief)


class WatchOnlyTests(_Harness):
    def _watched(self, chat: _Chat | None = None) -> tuple[_Forge, EnochApplication]:
        forge = _Forge(merge_state="BLOCKED", checks=PENDING, ci_repair=True)
        app = self._app(forge, chat)
        job = SimpleNamespace(
            id=7, chat_id="room-1", text="Add a feature.", context="", review_urls=(URL,)
        )
        app._watch_published_reviews(job)
        return forge, app

    def test_published_review_is_watched_and_green_is_reported_once(self) -> None:
        chat = _Chat()
        forge, app = self._watched(chat)
        (watch,) = load_ci_watches(self.root)
        self.assertEqual((watch.number, watch.task_id), (91, 7))

        app.pending_merges.tick()
        self.assertEqual(chat.sent, [])
        self.assertIn("Watching CI (1):", app._pr("room-1", ""))

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()
        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "✅ PR #91 CI passed. Ready to merge.")])
        self.assertEqual(forge.merges, [])
        self.assertEqual(load_ci_watches(self.root), ())

    def test_reviews_the_task_was_only_asked_about_are_not_watched(self) -> None:
        forge = _Forge(ci_repair=True)
        app = self._app(forge)
        job = SimpleNamespace(
            id=8, chat_id="room-1", text=f"Investigate {URL}", context="", review_urls=(URL,)
        )

        app._watch_published_reviews(job)
        app._watch_published_reviews(
            SimpleNamespace(id=9, chat_id="room-1", text="x", context="", review_urls=(URL,))
        )
        app._watch_published_reviews(
            SimpleNamespace(id=10, chat_id="room-1", text="y", context="", review_urls=(URL,))
        )

        (watch,) = load_ci_watches(self.root)
        self.assertEqual(watch.task_id, 9)

    def test_red_ci_gets_one_fix_task_and_is_never_merged(self) -> None:
        chat = _Chat()
        forge, app = self._watched(chat)
        forge.set(merge_state_status="UNSTABLE", checks=RED)

        app.pending_merges.tick()
        app.pending_merges.tick()

        (fix,) = self._fix_tasks(app)
        self.assertIn("Nobody has asked to merge it yet", fix.text)
        self.assertEqual(
            chat.sent,
            [("room-1", f"🔧 PR #91 CI failed (CI / test); started task #{fix.id} to fix it.")],
        )

        self._push_fix(forge, fix.id, "fix-w", "Fix")
        app.pending_merges.tick()
        forge.set(checks=RED)
        app.pending_merges.tick()

        self.assertEqual(len(self._fix_tasks(app)), 1)
        self.assertEqual(
            chat.sent[-1],
            ("room-1", f"❌ PR #91 CI still fails (CI / test) after an automatic fix attempt (task #{fix.id})."),
        )
        self.assertEqual(forge.merges, [])
        self.assertEqual(load_ci_watches(self.root), ())

    def test_infrastructure_failure_is_rerun_once_without_notice(self) -> None:
        chat = _Chat()
        forge, app = self._watched(chat)
        forge.set(checks=RED)
        forge.failures = (ReviewCheckFailure("CI / test", "timed_out"),)

        app.pending_merges.tick()
        self.assertEqual(forge.reruns, ["head-91"])
        self.assertEqual(chat.sent, [])

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()
        self.assertEqual(chat.sent, [("room-1", "✅ PR #91 CI passed. Ready to merge.")])
        self.assertEqual(forge.merges, [])

    def test_no_duplicate_notice_when_a_merge_is_pending(self) -> None:
        chat = _Chat()
        forge, app = self._watched(chat)
        app._pr("room-1", "merge 91")

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()
        app.pending_merges.tick()

        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])
        self.assertEqual(load_ci_watches(self.root), ())

    def test_watch_stops_when_closed_or_after_six_hours(self) -> None:
        chat = _Chat()
        forge, app = self._watched(chat)
        forge.set(state="CLOSED")
        app.pending_merges.tick()
        self.assertEqual(load_ci_watches(self.root), ())

        forge, app = self._watched(chat)
        self.now = START + timedelta(hours=6)
        app.pending_merges.tick()
        self.assertEqual(load_ci_watches(self.root), ())
        self.assertEqual(chat.sent, [])

    def test_watches_survive_a_restart_without_duplicate_notices(self) -> None:
        forge, app = self._watched()
        forge.set(checks=RED)
        first_chat = _Chat()
        app = self._app(forge, first_chat)
        app.pending_merges.tick()
        self.assertEqual(len(first_chat.sent), 1)

        chat = _Chat()
        restarted = self._app(forge, chat)
        restarted.pending_merges.tick()

        self.assertEqual(chat.sent, [])
        self.assertEqual(len(self._fix_tasks(restarted)), 1)

    def test_run_once_drives_watches_and_never_raises(self) -> None:
        chat = _Chat()
        forge, app = self._watched(chat)
        forge.inspect.side_effect = RuntimeError("forge is down")

        app.run_once()

        self.assertEqual(len(load_ci_watches(self.root)), 1)
        forge.inspect.side_effect = lambda reference, root=None: forge.status
        forge.set(checks=GREEN)
        app.run_once()
        self.assertEqual(chat.sent, [("room-1", "✅ PR #91 CI passed. Ready to merge.")])


class BoardStatusTests(_Harness):
    """The watcher's cached per-review status that the task board reads."""

    def _tracked(self, chat: _Chat | None = None) -> tuple[_Forge, EnochApplication]:
        forge = _Forge(merge_state="BLOCKED", checks=PENDING, ci_repair=True)
        # The review is open on Enoch's own (authoritative) repository.
        forge.provider.list_fn.side_effect = lambda root=None, limit=20: (forge.status,)
        app = self._app(forge, chat)
        app.pending_merges.tracked_interval_seconds = 0
        app._watch_published_reviews(
            SimpleNamespace(id=7, chat_id="room-1", text="Add a feature.", context="", review_urls=(URL,))
        )
        return forge, app

    def _board(self, app: EnochApplication) -> tuple[BoardReview, ...]:
        return app.pending_merges.board_reviews()

    def test_green_review_stays_on_the_board_until_it_is_closed(self) -> None:
        chat = _Chat()
        forge, app = self._tracked(chat)
        self.assertEqual(self._board(app), ())  # not yet known to be authoritative

        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "ci"),))

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()
        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "ready"),))
        self.assertEqual(chat.sent, [("room-1", "✅ PR #91 CI passed. Ready to merge.")])
        self.assertNotIn("Watching CI", app._pr("room-1", ""))

        forge.set(merge_state_status="BEHIND")
        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "ci"),))

        forge.set(state="MERGED")
        app.pending_merges.tick()
        self.assertEqual(self._board(app), ())
        self.assertEqual(load_ci_watches(self.root), ())
        self.assertEqual(len(chat.sent), 1)

    def test_red_ci_on_a_stale_base_is_ci_not_failed(self) -> None:
        forge, app = self._tracked()
        forge.contains_base = False
        forge.set(merge_state_status="UNSTABLE", checks=RED)

        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "ci"),))

        forge.contains_base = True
        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "failed"),))

    def test_stale_base_uses_the_shared_base_freshness_rule(self) -> None:
        forge = _Forge(merge_state="BLOCKED", checks=PENDING, ci_repair=True, base_freshness=True)
        forge.provider.list_fn.side_effect = lambda root=None, limit=20: (forge.status,)
        app = self._app(forge)
        app.pending_merges.tracked_interval_seconds = 0
        app._watch_published_reviews(
            SimpleNamespace(id=7, chat_id="room-1", text="Add a feature.", context="", review_urls=(URL,))
        )
        app.pending_merges.tick()
        # forge.base-freshness answers, not the ci-repair comparison.
        forge.contains_base = True
        forge.stale_heads.add(forge.status.head_sha)
        forge.set(merge_state_status="UNSTABLE", checks=RED)

        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "ci"),))
        self.assertTrue(forge.provider.base_freshness_fn.called)

        forge.stale_heads.clear()
        app.pending_merges.tick()
        self.assertEqual(self._board(app), (BoardReview(91, "failed"),))

    def test_conflicts_are_blocked(self) -> None:
        forge, app = self._tracked()
        forge.set(merge_state_status="DIRTY", mergeable="CONFLICTING", checks=GREEN)

        app.pending_merges.tick()
        app.pending_merges.tick()

        self.assertEqual(self._board(app), (BoardReview(91, "blocked"),))

    def test_review_with_a_pending_merge_is_left_to_the_merging_line(self) -> None:
        forge, app = self._tracked()
        app.pending_merges.tick()
        app._pr("room-1", "merge 91")

        self.assertEqual(len(load_pending_merges(self.root)), 1)
        self.assertEqual(self._board(app), ())

    def test_reviews_on_other_repositories_are_not_tracked_after_ci(self) -> None:
        forge, app = self._tracked()
        forge.provider.list_fn.side_effect = lambda root=None, limit=20: ()
        forge.set(merge_state_status="CLEAN", checks=GREEN)

        app.pending_merges.tick()

        self.assertEqual(self._board(app), ())
        self.assertEqual(load_ci_watches(self.root), ())


if __name__ == "__main__":
    unittest.main()
