"""Land before next: the PR auto-merge policy, per-task overrides, the queue hold.

A task that publishes exactly one PR on the authoritative repository has it
registered with the pending-merge watcher (as if the human ran ``/pr merge``)
when ``/pr auto-merge`` is on or the task says ``--merge=auto``. While such a
merge is pending, only CI fix tasks start; the hold releases when the PR lands
or its pending merge stops, is cancelled or times out.

Only reviews a task published count: ones its publish stage recorded, or ones
whose head is the tip of the task's own pushed branch. A review URL its result
merely mentions is a reference and never starts any of this.
"""

from __future__ import annotations

from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.core import (
    CI_REPAIR_TRIGGER,
    EnochApplication,
    TaskContextSnapshot,
    _repository_from_remote_url,
)
from enoch.app.auto_update import LandedReview
from enoch.app.execution_context import CURRENT_TASK_WORKER_ID
from enoch.app.merge_policy import (
    RequestOptionError,
    merge_policy_label,
    split_request_options,
    wants_auto_merge,
)
from enoch.app.pending_merges import load_ci_watches, load_pending_merges
from enoch.app.presentation import format_compact_task_start, format_task_details
from enoch.app.reporting import _format_task_list_item
from enoch.app.task_board import render_task_board
from enoch.commands import config_status, pr_usage
from enoch.identity import load_identity
from enoch.prompt_append import conversation_turn_prompt, work_request_prompt
from enoch.tasks.config import pr_auto_merge_enabled, save_auto_update, save_pr_auto_merge
from enoch.tasks.queue import (
    _job_to_dict,
    _parse_job,
    begin_next_task,
    complete_task,
    enqueue_task,
    enqueue_task_front,
    task_queue_status,
)
from our_ark_provider_kit import ReviewCheck
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_pending_merges import START, URL, _Forge, _review, _status


OTHER_URL = "https://github.com/our-ark/enoch/pull/92"
FOREIGN_URL = "https://github.com/someone-else/project/pull/7"
TASK_BRANCH = "enoch/example-task-branch"
# The real worker starter, before setUp replaces it with a mock.
_START_WORKER = EnochApplication._maybe_start_task_worker


def _runs_on_branch(app: EnochApplication, task_id: int, reply: str, branch: str = TASK_BRANCH):
    """A stand-in for ``_run_direct_work``: works on ``branch``, returns ``reply``."""

    def run(*_args, **_kwargs):
        recorded = app.workflow.record_workspace(
            task_id, CURRENT_TASK_WORKER_ID.get(), app.root / "task-worktree", branch
        )
        assert recorded is not None
        return reply

    return run


class RequestOptionTests(unittest.TestCase):
    def test_no_options_leave_the_request_unchanged(self) -> None:
        options = split_request_options("fix the --merge=auto docs")
        self.assertEqual((options.request, options.merge), ("fix the --merge=auto docs", ""))

    def test_merge_options_are_parsed_and_stripped(self) -> None:
        self.assertEqual(split_request_options("--merge=auto fix it").merge, "auto")
        options = split_request_options("--merge=NEVER\n\nfix it")
        self.assertEqual((options.request, options.merge), ("fix it", "never"))

    def test_unknown_keys_values_and_malformed_options_are_rejected(self) -> None:
        for argument in (
            "--force=yes fix it",
            "--merge=now fix it",
            "--merge fix it",
            "--merge=auto --merge=never fix it",
            "--merge=auto --auto-close=1 fix it",
            "-- fix it",
        ):
            with self.subTest(argument=argument), self.assertRaises(RequestOptionError):
                split_request_options(argument)

    def test_policy_decision(self) -> None:
        self.assertFalse(wants_auto_merge("", enabled=False))
        self.assertTrue(wants_auto_merge("", enabled=True))
        self.assertTrue(wants_auto_merge("auto", enabled=False))
        self.assertFalse(wants_auto_merge("never", enabled=True))
        self.assertFalse(wants_auto_merge("bogus", enabled=False))
        self.assertEqual(merge_policy_label("auto"), "merge when ready")
        self.assertEqual(merge_policy_label("never"), "no merge")
        self.assertEqual(merge_policy_label(""), "")


class SettingAndSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_setting_defaults_off_and_persists(self) -> None:
        self.assertFalse(pr_auto_merge_enabled(self.root))
        self.assertTrue(save_pr_auto_merge(True, self.root))
        self.assertTrue(pr_auto_merge_enabled(self.root))
        self.assertFalse(save_pr_auto_merge(False, self.root))
        self.assertFalse(pr_auto_merge_enabled(self.root))

    def test_config_and_help_show_the_setting(self) -> None:
        status = config_status(self.root, runtime=_Runtime())
        self.assertIn("- PR auto-merge: off (default)", status)
        self.assertIn("/pr auto-merge on|off", status)
        save_pr_auto_merge(False, self.root)
        self.assertIn("- PR auto-merge: off\n", config_status(self.root, runtime=_Runtime()))
        usage = pr_usage("/")
        self.assertIn("/pr auto-merge on|off", usage)
        self.assertIn("--merge=never", usage)

    def test_queue_schema_stays_compatible(self) -> None:
        job = enqueue_task("room-1", "plain work", self.root)
        self.assertEqual(job.merge_policy, "")
        self.assertNotIn("merge_policy", _job_to_dict(job))
        # A job written before the field existed still loads, with the default.
        legacy = _job_to_dict(job)
        self.assertEqual(_parse_job(legacy).merge_policy, "")
        flagged = enqueue_task("room-1", "flagged work", self.root, merge_policy="never")
        stored = json.loads((self.root / ".enoch" / "task_queue.json").read_text())
        self.assertEqual(stored["pending"][-1]["merge_policy"], "never")
        self.assertEqual(task_queue_status(self.root).pending[-1].merge_policy, "never")
        self.assertEqual(_parse_job({**_job_to_dict(flagged), "merge_policy": "bogus"}).merge_policy, "")

    def test_start_line_and_task_show_carry_the_override(self) -> None:
        self.assertEqual(
            format_compact_task_start(4, "fix it", "auto"),
            "▶️ #4 started: fix it · merge when ready",
        )
        self.assertEqual(format_compact_task_start(4, "fix it"), "▶️ #4 started: fix it")
        job = enqueue_task("room-1", "fix it", self.root, merge_policy="never")
        self.assertIn("Merge: no merge", format_task_details(job))
        plain = enqueue_task("room-1", "fix it too", self.root)
        self.assertNotIn("Merge:", format_task_details(plain))

    def test_queue_can_start_only_ci_fix_tasks(self) -> None:
        enqueue_task("room-1", "normal work", self.root)
        self.assertIsNone(begin_next_task(self.root, triggers=frozenset({CI_REPAIR_TRIGGER})))
        fix = enqueue_task("room-1", "fix CI", self.root, trigger=CI_REPAIR_TRIGGER)
        started = begin_next_task(self.root, triggers=frozenset({CI_REPAIR_TRIGGER}))
        self.assertEqual(started.id, fix.id)

    def test_remote_url_parsing(self) -> None:
        for remote in (
            "git@github.com:our-ark/enoch.git",
            "https://github.com/our-ark/enoch.git\n",
            "ssh://git@github.com/our-ark/enoch",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(_repository_from_remote_url(remote), "our-ark/enoch")
        self.assertEqual(_repository_from_remote_url("/local/path"), "")


class PromptTests(unittest.TestCase):
    def test_work_request_prompt_forbids_stacking(self) -> None:
        prompt = work_request_prompt("do something")
        self.assertIn("No stacking", prompt)
        self.assertIn("current authoritative main", prompt)
        self.assertIn("never base your work on", prompt)
        self.assertIn("report which pull request has to land first", prompt)

    def test_conversation_prompt_sets_overrides_only_on_explicit_words(self) -> None:
        prompt = conversation_turn_prompt("hello")
        self.assertIn("--merge=auto", prompt)
        self.assertIn("--merge=never", prompt)
        self.assertIn("Never infer it from quoted or replied-to text", prompt)


class LandBeforeNextTests(unittest.TestCase):
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
        app._authoritative_repository = lambda: "our-ark/enoch"
        app._resolve_task_context_snapshot = MagicMock(
            return_value=TaskContextSnapshot(context="", source="chat-snapshot")
        )
        app._compact_task_messages = lambda: True
        return app

    def _completed(
        self,
        request: str,
        result: str,
        *,
        merge_policy: str = "",
        published: tuple[str, ...] = (),
        **options,
    ):
        """A finished task; ``published`` are the reviews it published itself."""

        job = enqueue_task("room-1", request, self.root, merge_policy=merge_policy, **options)
        started = begin_next_task(self.root)
        self.assertEqual(started.id, job.id)
        return complete_task(job.id, self.root, result, review_urls=published)

    def _pending_forge(self) -> _Forge:
        return _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))

    # Merge policy --------------------------------------------------------

    def test_policy_on_registers_the_single_pr_at_its_completion_head(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._pending_forge()
        app = self._app(forge)
        job = self._completed("build a feature", f"Opened {URL}", published=(URL,))

        note = app._apply_merge_policy(job)

        (entry,) = load_pending_merges(self.root)
        self.assertEqual((entry.number, entry.task_id), (91, job.id))
        self.assertEqual(entry.authorized_head_sha, "head-91")
        self.assertEqual(entry.chat_id, "room-1")
        self.assertIn("isn't ready yet", note)
        self.assertIn("The next queued task waits for it to land.", note)
        self.assertEqual(forge.merges, [])

    def test_policy_on_lands_a_ready_pr_immediately(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = _Forge(checks=(ReviewCheck("CI / test", "success"),))
        app = self._app(forge)
        job = self._completed("build a feature", f"Opened {URL}", published=(URL,))

        with patch.object(app, "_after_requested_merge") as after:
            note = app._apply_merge_policy(job)

        self.assertIn(": landed.", note)
        self.assertEqual(forge.merges, ["head-91"])
        after.assert_called_once_with(
            LandedReview("#91", URL, "our-ark/enoch", "main")
        )
        self.assertIsNone(app._land_hold())

    def test_policy_off_does_not_register(self) -> None:
        forge = self._pending_forge()
        app = self._app(forge)
        job = self._completed("build a feature", f"Opened {URL}", published=(URL,))

        self.assertEqual(app._apply_merge_policy(job), "")
        self.assertEqual(load_pending_merges(self.root), ())

    def test_merge_when_ready_override_merges_even_when_off(self) -> None:
        forge = self._pending_forge()
        app = self._app(forge)
        job = self._completed(
            "build a feature", f"Opened {URL}", published=(URL,), merge_policy="auto"
        )

        app._apply_merge_policy(job)

        (entry,) = load_pending_merges(self.root)
        self.assertEqual(entry.task_id, job.id)

    def test_dont_merge_override_skips_even_when_on(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._pending_forge()
        app = self._app(forge)
        job = self._completed(
            "build a feature", f"Opened {URL}", published=(URL,), merge_policy="never"
        )

        self.assertEqual(app._apply_merge_policy(job), "")
        self.assertEqual(load_pending_merges(self.root), ())

    def test_zero_or_several_prs_only_notify(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._pending_forge()
        app = self._app(forge)
        none = self._completed("answer a question", "No changes needed.")
        several = self._completed(
            "split work", f"Opened {URL} and {OTHER_URL}", published=(URL, OTHER_URL)
        )
        foreign = self._completed(
            "upstream fix", f"Opened {FOREIGN_URL}", published=(FOREIGN_URL,)
        )
        # Even an attributed review is skipped when the request named it.
        mentioned = self._completed(f"review {URL}", f"Looked at {URL}", published=(URL,))

        self.assertIn("published no PR", app._apply_merge_policy(none))
        note = app._apply_merge_policy(several)
        self.assertIn("published 2 PRs (PR #91, PR #92)", note)
        self.assertIn("/pr merge <number>", note)
        self.assertIn("published no PR", app._apply_merge_policy(foreign))
        self.assertIn("published no PR", app._apply_merge_policy(mentioned))
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(forge.inspect.call_count, 0)

    def test_ci_fix_tasks_are_never_auto_merged(self) -> None:
        save_pr_auto_merge(True, self.root)
        app = self._app(self._pending_forge())
        job = self._completed(
            "fix CI",
            f"Pushed to {URL}",
            published=(URL,),
            trigger=CI_REPAIR_TRIGGER,
            initiated_by="agent",
        )

        self.assertEqual(app._apply_merge_policy(job), "")
        self.assertEqual(load_pending_merges(self.root), ())

    def test_completion_appends_the_note_to_the_final_message(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        app = self._app(self._pending_forge(), chat)
        enqueue_task("room-1", "build a feature", self.root)
        job = begin_next_task(self.root)

        # The task opens PR #91 from its own branch, whose pushed tip is #91's head.
        with patch.object(
            app, "_run_direct_work", side_effect=_runs_on_branch(app, job.id, f"Opened {URL}")
        ), patch.object(app, "_remote_branch_revision", return_value="head-91"):
            app._run_task_job(job)

        (entry,) = load_pending_merges(self.root)
        self.assertEqual(entry.task_id, job.id)
        final = chat.sent[-1][1]
        self.assertIn("Auto-merge: PR #91 isn't ready yet", final)

    # Attribution: only reviews a task published are its reviews -----------

    def _run(self, app: EnochApplication, reply: str, *, tip: str, merge_policy: str = ""):
        """Run one queued task on its own branch, whose pushed tip is ``tip``."""

        enqueue_task("room-1", "backlog step 2", self.root, merge_policy=merge_policy)
        job = begin_next_task(self.root)
        with patch.object(
            app, "_run_direct_work", side_effect=_runs_on_branch(app, job.id, reply)
        ), patch.object(app, "_remote_branch_revision", return_value=tip) as remote:
            app._run_task_job(job)
        if "/pull/" in reply:
            remote.assert_called_with(TASK_BRANCH)
        return app.workflow.find(job.id)

    def _assert_not_attributed(self, app: EnochApplication, finished, forge: _Forge) -> None:
        self.assertEqual(finished.status, "completed")
        self.assertEqual(finished.review_urls, ())
        self.assertEqual(load_pending_merges(self.root), ())
        self.assertEqual(load_ci_watches(self.root), ())
        self.assertIsNone(app._land_hold())
        self.assertEqual(forge.merges, [])
        self.assertNotIn("Review", _format_task_list_item(finished))
        # A later human merge of that PR is not linked to this task either.
        self.assertEqual(app._task_for_review(URL), 0)

    def test_result_that_mentions_another_tasks_pr_is_not_attributed(self) -> None:
        save_pr_auto_merge(True, self.root)
        chat = _Chat()
        forge = self._pending_forge()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as events:
            finished = self._run(
                app,
                f"Precondition not met; stopping without changes. See {URL}.",
                tip="head-of-this-task",
            )

        self._assert_not_attributed(app, finished, forge)
        final = chat.sent[-1][1]
        self.assertIn(URL, final)  # still shown, as a plain reference
        self.assertIn("Not merging automatically: task #1 published no PR", final)
        self.assertNotIn("Auto-merge:", final)
        recorded = [call.args[0] for call in events.call_args_list]
        self.assertNotIn("pr_auto_merge_requested", recorded)
        self.assertIn("task_review_references", recorded)

    def test_mentioned_pr_is_not_attributed_when_ownership_cannot_be_checked(self) -> None:
        save_pr_auto_merge(True, self.root)
        for case in ("unknown remote tip", "no task branch"):
            with self.subTest(case=case):
                forge = self._pending_forge()
                app = self._app(forge)
                if case == "unknown remote tip":
                    finished = self._run(app, f"See {URL}", tip="")
                else:
                    enqueue_task("room-1", "answer a question", self.root)
                    job = begin_next_task(self.root)
                    with patch.object(app, "_run_direct_work", return_value=f"See {URL}"):
                        app._run_task_job(job)
                    finished = app.workflow.find(job.id)

                self._assert_not_attributed(app, finished, forge)
                self.assertEqual(forge.inspect.call_count, 0)

    def test_task_that_published_its_own_pr_is_still_auto_merged(self) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._pending_forge()
        app = self._app(forge)

        finished = self._run(app, f"Opened {URL}", tip="head-91")

        self.assertEqual(finished.review_urls, (URL,))
        (entry,) = load_pending_merges(self.root)
        self.assertEqual((entry.number, entry.task_id), (91, finished.id))
        self.assertEqual(entry.authorized_head_sha, "head-91")
        self.assertEqual(app._land_hold().task_id, finished.id)
        (watch,) = load_ci_watches(self.root)
        self.assertEqual((watch.url, watch.task_id), (URL, finished.id))
        self.assertIn(f"Review: {URL}", _format_task_list_item(finished))

    def test_result_that_publishes_one_pr_and_mentions_another_counts_only_its_own(
        self,
    ) -> None:
        save_pr_auto_merge(True, self.root)
        forge = self._pending_forge()
        other = _status(url=OTHER_URL, reference="92", number=92, head_sha="head-92")
        forge.inspect.side_effect = lambda reference, root=None: (
            other if reference == OTHER_URL else forge.status
        )
        app = self._app(forge)

        finished = self._run(
            app, f"Opened {URL}. It follows the approach of {OTHER_URL}.", tip="head-91"
        )

        self.assertEqual(finished.review_urls, (URL,))
        (entry,) = load_pending_merges(self.root)
        self.assertEqual((entry.number, entry.task_id), (91, finished.id))
        self.assertEqual([watch.url for watch in load_ci_watches(self.root)], [URL])
        self.assertEqual(app._task_for_review(OTHER_URL), 0)

    def test_merge_auto_task_that_only_mentions_a_pr_does_not_merge(self) -> None:
        # Policy off; only the task's own --merge=auto could merge, and it published nothing.
        chat = _Chat()
        forge = _Forge(checks=(ReviewCheck("CI / test", "success"),))
        app = self._app(forge, chat)

        finished = self._run(
            app, f"Nothing to do; {URL} already covers it.", tip="head-of-this-task",
            merge_policy="auto",
        )

        self._assert_not_attributed(app, finished, forge)
        self.assertIn("published no PR", chat.sent[-1][1])

    def test_remote_branch_revision_reads_only_the_exact_branch(self) -> None:
        app = self._app(self._pending_forge())
        listing = (
            "1111111\trefs/heads/enoch/example-task-branch-2\n"
            "2222222\trefs/heads/enoch/example-task-branch"
        )
        with patch("enoch.app.core.run_git") as run_git:
            run_git.return_value = MagicMock(returncode=0, stdout=listing)
            self.assertEqual(app._remote_branch_revision(TASK_BRANCH), "2222222")
            command = run_git.call_args.args[0]
            self.assertEqual(command[0], "ls-remote")
            self.assertEqual(command[-1], f"refs/heads/{TASK_BRANCH}")
            run_git.return_value = MagicMock(returncode=0, stdout="")
            self.assertEqual(app._remote_branch_revision(TASK_BRANCH), "")
            run_git.return_value = MagicMock(returncode=128, stdout="", stderr="no remote")
            self.assertEqual(app._remote_branch_revision(TASK_BRANCH), "")

    # Overrides from typed commands ----------------------------------------

    def test_typed_task_and_do_carry_the_override(self) -> None:
        app = self._app(self._pending_forge())

        app._task("room-1", "/task --merge=never build it")
        app._task("room-1", "/task build more")
        pending = task_queue_status(self.root).pending
        self.assertEqual([(job.text, job.merge_policy) for job in pending], [
            ("build it", "never"), ("build more", ""),
        ])

        with patch.object(app, "_start_direct_work_worker") as start:
            app._do("room-1", "/do --merge=auto ship it")
        direct = start.call_args.args[0]
        self.assertEqual((direct.text, direct.merge_policy), ("ship it", "auto"))
        self.assertIn("merge when ready", app.client.sent[-1][1])

    def test_agent_task_action_carries_the_override_through_the_same_parser(self) -> None:
        from enoch.app.conversation import ConversationAction
        from enoch.providers.contracts import ChatEvent

        app = self._app(self._pending_forge())
        event = ChatEvent(cursor="1", conversation_id="room-1", message_id="1", text="hi")

        app._execute_conversation_action(
            event, ConversationAction("task", "--merge=never build it"), 0
        )
        rejected = app._execute_conversation_action(
            event, ConversationAction("task", "--merge=yes build it"), 1
        )

        (job,) = task_queue_status(self.root).pending
        self.assertEqual((job.text, job.merge_policy), ("build it", "never"))
        self.assertIn("--merge must be one of: auto, never.", rejected.text)

    def test_typed_commands_reject_unknown_options(self) -> None:
        app = self._app(self._pending_forge())

        reply = app._task("room-1", "/task --automerge=yes build it")
        self.assertIn("Unknown request option --automerge", reply)
        self.assertIn("Nothing was queued.", reply)
        self.assertIn("Nothing was started.", app._do("room-1", "/do --merge=sometimes x"))
        self.assertEqual(task_queue_status(self.root).pending, ())

    def test_pr_auto_merge_command_is_locked_and_persists(self) -> None:
        app = self._app(self._pending_forge())

        self.assertIn("off (default)", app._pr("room-1", "auto-merge"))
        self.assertIn("locked", app._pr("room-2", "auto-merge on"))
        self.assertFalse(pr_auto_merge_enabled(self.root))
        self.assertIn("PR auto-merge is on", app._pr("room-1", "auto-merge on"))
        self.assertTrue(pr_auto_merge_enabled(self.root))
        self.assertIn("PR auto-merge is off", app._pr("room-1", "auto-merge off"))
        self.assertIn("Use /pr auto-merge on|off", app._pr("room-1", "auto-merge maybe"))
        self.assertIn("PR auto-merge: off.", app._help("pr"))

    # The hold -------------------------------------------------------------

    def _held(self, chat: _Chat | None = None, forge: _Forge | None = None):
        forge = forge or self._pending_forge()
        app = self._app(forge, chat)
        job = self._completed(
            "build a feature", f"Opened {URL}", published=(URL,), merge_policy="auto"
        )
        app._apply_merge_policy(job)
        self.assertIsNotNone(app._land_hold())
        return app, forge

    def _worker_starts(self, app: EnochApplication) -> bool:
        app._run_task_worker = MagicMock()
        app._task_worker = None
        _START_WORKER(app)
        if app._task_worker is None:
            return False
        app._task_worker.join(5)
        return app._run_task_worker.called

    def test_hold_blocks_the_next_normal_task_but_not_ci_fix_tasks(self) -> None:
        app, _forge = self._held()
        enqueue_task("room-1", "next feature", self.root)

        self.assertFalse(self._worker_starts(app))
        self.assertIn(
            "Queued (waiting for #91 to land)", app._render_task_board().text
        )

        fix = enqueue_task_front(
            "room-1", "fix CI", self.root, trigger=CI_REPAIR_TRIGGER, initiated_by="agent"
        )
        self.assertTrue(self._worker_starts(app))

        ran = []

        def run(job):
            ran.append(job.id)
            complete_task(job.id, self.root, "fixed")

        app._run_task_job = run
        del app._run_task_worker
        app._run_task_worker()
        self.assertEqual(ran, [fix.id])
        self.assertEqual([job.text for job in task_queue_status(self.root).pending], ["next feature"])

    def test_hold_also_queues_do_instead_of_running_it(self) -> None:
        app, _forge = self._held()

        with patch.object(app, "_start_direct_work_worker") as start:
            reply = app._do("room-1", "/do quick fix")

        start.assert_not_called()
        self.assertIn("Waiting for #91 to land.", reply)
        self.assertEqual([job.text for job in task_queue_status(self.root).pending], ["quick fix"])

    def test_no_hold_without_a_pending_merge(self) -> None:
        app = self._app(self._pending_forge())
        job = self._completed("build a feature", f"Opened {URL}", published=(URL,))
        app._watch_published_reviews(job)
        app._apply_merge_policy(job)  # policy off: no pending merge
        enqueue_task("room-1", "next feature", self.root)

        self.assertIsNone(app._land_hold())
        self.assertTrue(self._worker_starts(app))
        self.assertIn("\nQueued:\n", app._render_task_board().text)

    def test_human_pr_merge_of_a_task_pr_also_holds(self) -> None:
        app = self._app(self._pending_forge())
        job = self._completed("build a feature", f"Opened {URL}", published=(URL,))

        app._pr("room-1", "merge 91")

        self.assertEqual(app._land_hold().task_id, job.id)

    def _release(self, app: EnochApplication, chat: _Chat) -> list[str]:
        enqueue_task("room-1", "next feature", self.root)
        self.assertFalse(self._worker_starts(app))
        app.pending_merges.tick()
        app.pending_merges.tick()
        self.assertIsNone(app._land_hold())
        self.assertTrue(self._worker_starts(app))
        return [text for _chat, text in chat.sent]

    def test_hold_releases_when_the_pr_lands(self) -> None:
        # With auto-update on, the pending update holds the queue next; that
        # is covered in test_enoch_auto_update.
        save_auto_update(False, self.root)
        chat = _Chat()
        app, forge = self._held(chat)
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))

        # The watcher calls the requested-merge hook point after landing.
        with patch.object(app, "_reconcile_lineage_adoptions") as after:
            sent = self._release(app, chat)

        self.assertEqual(sent, ["✅ Merged #91."])
        after.assert_called_once_with()

    def test_hold_releases_with_one_notice_when_the_merge_stops(self) -> None:
        chat = _Chat()
        app, forge = self._held(chat)
        forge.set(mergeable="CONFLICTING", merge_state_status="DIRTY")

        sent = self._release(app, chat)

        self.assertEqual(
            sent,
            ["❌ Not merging #91: it has merge conflicts. The task queue continues."],
        )

    def test_hold_releases_with_one_notice_when_cancelled(self) -> None:
        chat = _Chat()
        app, _forge = self._held(chat)
        enqueue_task("room-1", "next feature", self.root)
        self.assertFalse(self._worker_starts(app))

        reply = app._pr("room-1", "cancel 91")

        self.assertEqual(
            reply,
            "Cancelled the pending merge for #91. It will not be merged "
            "automatically. The task queue continues.",
        )
        self.assertIsNone(app._land_hold())
        self.assertTrue(self._worker_starts(app))
        app.pending_merges.tick()
        self.assertEqual(chat.sent, [])

    def test_hold_releases_with_one_notice_on_timeout(self) -> None:
        chat = _Chat()
        app, _forge = self._held(chat)
        self.now = START + app.pending_merges.max_wait + timedelta(minutes=1)

        sent = self._release(app, chat)

        self.assertEqual(len(sent), 1)
        self.assertIn("⏹ Not merging #91: still not ready after", sent[0])
        self.assertTrue(sent[0].endswith("The task queue continues."))

    def test_hold_releases_on_timeout_even_when_the_forge_keeps_failing(self) -> None:
        chat = _Chat()
        app, forge = self._held(chat)
        forge.inspect.side_effect = RuntimeError("forge unavailable")
        enqueue_task("room-1", "next feature", self.root)

        with patch("enoch.app.core._record_system_event"):
            app.pending_merges.tick()
            self.assertIsNotNone(app._land_hold())
            self.now = START + app.pending_merges.max_wait + timedelta(minutes=1)
            app.pending_merges.tick()
            app.pending_merges.tick()

        self.assertIsNone(app._land_hold())
        self.assertEqual(len(chat.sent), 1)
        self.assertIn("could not check it", chat.sent[0][1])
        self.assertTrue(chat.sent[0][1].endswith("The task queue continues."))
        self.assertTrue(self._worker_starts(app))

    def test_tick_releases_before_starting_the_next_task(self) -> None:
        app, forge = self._held()
        forge.set(merge_state_status="CLEAN", checks=(ReviewCheck("CI / test", "success"),))
        order = []
        app.pending_merges.tick = MagicMock(side_effect=lambda: order.append("merges"))
        app._maybe_start_task_worker = MagicMock(side_effect=lambda: order.append("tasks"))

        with patch.object(app, "_refresh_task_board"), patch.object(app.client, "receive", return_value=()):
            app.run_once()

        self.assertLess(order.index("merges"), order.index("tasks"))


class BoardTests(unittest.TestCase):
    def test_board_shows_the_hold_only_with_queued_tasks(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            enqueue_task("room-1", "next feature", root)
            status = task_queue_status(root)
            empty = task_queue_status(Path(temp) / "missing")
        from datetime import datetime

        held = render_task_board(status, updated_at=datetime(2026, 11, 26), held_by="#91")
        self.assertIn("Queued (waiting for #91 to land)", held.text)
        idle = render_task_board(empty, updated_at=datetime(2026, 11, 26), held_by="#91")
        self.assertNotIn("waiting for #91", idle.text)


if __name__ == "__main__":
    unittest.main()
