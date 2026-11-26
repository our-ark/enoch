"""Hands-off ``/pr merge`` right after the base branch moves.

Right after another review lands, GitHub recomputes mergeability lazily: for a
short time a review that is now behind still reports UNSTABLE (or UNKNOWN)
together with the failed checks of its old head, which was built on the broken
base. Those checks must not refuse the merge. The controller asks the forge
whether the head contains the base tip (``forge.base-freshness``) and, when it
does not, updates the branch and waits for CI on the new head, exactly as for
BEHIND. Red CI is still refused on a head that already contains the base tip.

The forge is the in-memory fake from ``test_enoch_pending_merges``; the GitHub
side (compare API, update-branch on a stale merge state) is tested with mocked
gh in ``test_enoch_github_workflow``.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from enoch.app.core import EnochApplication
from enoch.app.pending_merges import load_pending_merges
from enoch.app.review_freshness import (
    BASE_FRESHNESS_CAPABILITY,
    CI_REPAIR_CAPABILITY,
    base_tip_capability,
)
from enoch.identity import load_identity
from enoch.providers.contracts import ForgeProviderError, ReviewIdentity
from our_ark_provider_kit import ReviewCheck
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_pending_merges import START, URL, _Forge, _review


DESCENT = "Full-body depth-2 descent / Python 3.12"
# The old head's checks: the non-required descent check failed on the broken base.
STALE_RED = (ReviewCheck(DESCENT, "failure", "failure"), ReviewCheck("Tests / test", "success"))
GREEN = (ReviewCheck(DESCENT, "success"), ReviewCheck("Tests / test", "success"))
PENDING = (ReviewCheck("Tests / test", "pending"),)


class _Harness(unittest.TestCase):
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

    def _entry(self):
        (entry,) = load_pending_merges(self.root)
        return entry

    def _freshness_calls(self, forge: _Forge) -> list[str]:
        return [call.kwargs["head_sha"] for call in forge.provider.base_freshness_fn.call_args_list]


class StaleMergeStateRaceTests(_Harness):
    def test_request_updates_a_behind_head_instead_of_refusing_its_stale_red_ci(self) -> None:
        # #94 just fixed main; #93's head predates it and its descent check failed.
        forge = _Forge(merge_state="UNSTABLE", checks=STALE_RED, base_freshness=True)
        forge.stale_heads = {"head-91"}
        chat = _Chat()
        app = self._app(forge, chat)

        reply = app._pr("room-1", "merge 91")

        self.assertEqual(
            reply,
            "PR #91 isn't ready yet (behind main / CI running). "
            "I'll merge it automatically when it is.",
        )
        self.assertEqual(self._freshness_calls(forge), ["head-91"])
        self.assertEqual(forge.updates, ["head-91"])  # pinned to the inspected head
        entry = self._entry()
        self.assertEqual(entry.authorized_head_sha, "head-91")
        self.assertEqual(entry.allowed_head_shas, ("head-91", "update-1"))
        self.assertEqual(entry.waiting_for, "updating from main")

        app.pending_merges.tick()
        self.assertEqual(self._entry().waiting_for, "waiting for CI")
        self.assertEqual(forge.merges, [])

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.merges, ["update-1"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])
        self.assertEqual(load_pending_merges(self.root), ())

    def test_watcher_updates_instead_of_refusing_when_the_base_moves_under_it(self) -> None:
        forge = _Forge(merge_state="BLOCKED", checks=PENDING, base_freshness=True)
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        # Another review lands; this head's CI fails and the merge state is stale.
        forge.stale_heads = {"head-91"}
        forge.set(merge_state_status="UNSTABLE", checks=STALE_RED)
        app.pending_merges.tick()

        self.assertEqual(chat.sent, [])
        self.assertEqual(forge.updates, ["head-91"])
        entry = self._entry()
        self.assertEqual(entry.allowed_head_shas, ("head-91", "update-1"))
        self.assertEqual(entry.waiting_for, "updating from main")

        app.pending_merges.tick()
        self.assertEqual(self._entry().waiting_for, "waiting for CI")

        forge.set(merge_state_status="CLEAN", checks=GREEN)
        app.pending_merges.tick()

        self.assertEqual(forge.merges, ["update-1"])
        self.assertEqual(chat.sent, [("room-1", "✅ Merged #91.")])

    def test_update_failure_keeps_waiting_and_retries(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=STALE_RED, base_freshness=True)
        forge.stale_heads = {"head-91"}
        forge.provider.update_branch_fn.side_effect = ForgeProviderError("HTTP 422")
        chat = _Chat()
        app = self._app(forge, chat)

        reply = app._pr("room-1", "merge 91")

        self.assertIn("isn't ready yet (behind main", reply)
        self.assertEqual(self._entry().allowed_head_shas, ("head-91",))

        forge.provider.update_branch_fn.side_effect = forge._update
        app.pending_merges.tick()

        self.assertEqual(forge.updates, ["head-91"])
        self.assertEqual(self._entry().allowed_head_shas, ("head-91", "update-1"))
        self.assertEqual(chat.sent, [])

    def test_foreign_push_after_the_update_still_cancels(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=STALE_RED, base_freshness=True)
        forge.stale_heads = {"head-91"}
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")

        forge.push("someone-else")
        app.pending_merges.tick()

        self.assertEqual(forge.merges, [])
        self.assertEqual(
            chat.sent,
            [
                (
                    "room-1",
                    "⏹ Not merging #91: new commits were pushed after your merge "
                    "request. Ask again to merge the new version.",
                )
            ],
        )

    def test_red_ci_on_a_head_that_contains_the_base_tip_is_still_refused(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=STALE_RED, base_freshness=True)

        reply = self._app(forge)._pr("room-1", "merge 91")

        self.assertEqual(reply, f"❌ Not merging #91: CI failed ({DESCENT}).")
        self.assertEqual(self._freshness_calls(forge), ["head-91"])
        self.assertEqual(forge.updates, [])
        self.assertEqual(load_pending_merges(self.root), ())

        # The same rule in the watcher, after an update made the head current.
        forge = _Forge(merge_state="BEHIND", checks=PENDING, base_freshness=True)
        chat = _Chat()
        app = self._app(forge, chat)
        app._pr("room-1", "merge 91")
        forge.set(merge_state_status="UNSTABLE", checks=STALE_RED)
        app.pending_merges.tick()

        self.assertEqual(self._freshness_calls(forge), ["update-1"])
        self.assertEqual(chat.sent, [("room-1", f"❌ Not merging #91: CI failed ({DESCENT}).")])
        self.assertEqual(forge.merges, [])
        self.assertEqual(load_pending_merges(self.root), ())

    def test_unknown_or_missing_merge_state_waits_without_refusing_or_landing(self) -> None:
        for state in ("UNKNOWN", ""):
            with self.subTest(state=state):
                for path in self.root.rglob("pending_merges.json"):
                    path.unlink()
                forge = _Forge(merge_state=state, checks=STALE_RED, base_freshness=True)
                chat = _Chat()
                app = self._app(forge, chat)

                reply = app._pr("room-1", "merge 91")

                self.assertEqual(
                    reply,
                    "PR #91 isn't ready yet (merge status not ready). "
                    "I'll merge it automatically when it is.",
                )
                app.pending_merges.tick()
                self.assertEqual(self._entry().waiting_for, "waiting for merge status")

                # Green checks do not land while the merge state is unknown.
                forge.set(checks=GREEN)
                app.pending_merges.tick()
                self.assertEqual(self._entry().waiting_for, "waiting for merge status")

                self.assertEqual(forge.merges, [])
                self.assertEqual(forge.updates, [])
                self.assertEqual(chat.sent, [])
                self.assertEqual(self._freshness_calls(forge), [])

                # Once GitHub settles on BEHIND, the usual update path runs.
                forge.set(merge_state_status="BEHIND", checks=STALE_RED)
                app.pending_merges.tick()
                self.assertEqual(forge.updates, ["head-91"])

    def test_base_check_error_keeps_waiting_instead_of_refusing(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=STALE_RED, base_freshness=True)
        forge.freshness_error = ForgeProviderError("GitHub API is unavailable")
        chat = _Chat()
        app = self._app(forge, chat)

        with patch("enoch.app.core._record_system_event") as record:
            reply = app._pr("room-1", "merge 91")
            app.pending_merges.tick()

        self.assertEqual(
            reply,
            "PR #91 isn't ready yet (merge status not ready). "
            "I'll merge it automatically when it is.",
        )
        self.assertEqual(self._entry().waiting_for, "waiting for merge status")
        self.assertIn(
            "pending_merge_base_check_failed",
            [call.args[0] for call in record.call_args_list],
        )
        self.assertEqual(chat.sent, [])

        # It still stops after the usual timeout.
        self.now = START + app.pending_merges.timeout
        app.pending_merges.tick()
        self.assertEqual(len(chat.sent), 1)
        self.assertIn("still not ready after 2 hours (waiting for merge status)", chat.sent[0][1])

    def test_forge_without_a_base_check_keeps_refusing_red_ci(self) -> None:
        forge = _Forge(merge_state="UNSTABLE", checks=STALE_RED)

        reply = self._app(forge)._pr("room-1", "merge 91")

        self.assertEqual(reply, f"❌ Not merging #91: CI failed ({DESCENT}).")


class BaseTipRuleTests(_Harness):
    def test_base_freshness_is_preferred_over_ci_repair_compare(self) -> None:
        forge = _Forge(ci_repair=True, base_freshness=True)
        forge.stale_heads = {"head-91"}
        forge.contains_base = True  # compare would say otherwise
        app = self._app(forge)

        self.assertEqual(base_tip_capability(app.review), BASE_FRESHNESS_CAPABILITY)
        self.assertFalse(app.pending_merges.head_contains_base(_identity(), "head-91", "main"))
        self.assertTrue(app.pending_merges.head_contains_base(_identity(), "update-1", "main"))
        forge.provider.compare_fn.assert_not_called()

    def test_ci_repair_compare_answers_without_base_freshness(self) -> None:
        forge = _Forge(ci_repair=True)
        forge.contains_base = False
        app = self._app(forge)

        self.assertEqual(base_tip_capability(app.review), CI_REPAIR_CAPABILITY)
        self.assertFalse(app.pending_merges.head_contains_base(_identity(), "head-91", "main"))
        # The compare fallback needs the base branch name.
        self.assertIsNone(app.pending_merges.head_contains_base(_identity(), "head-91", ""))

    def test_forge_without_either_cannot_tell(self) -> None:
        app = self._app(_Forge())

        self.assertIsNone(base_tip_capability(app.review))
        self.assertIsNone(app.pending_merges.head_contains_base(_identity(), "head-91", "main"))


def _identity() -> ReviewIdentity:
    return ReviewIdentity(id=URL, url=URL)


if __name__ == "__main__":
    unittest.main()
