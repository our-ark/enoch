"""Landing a review that is only behind its base branch.

Covers the provider-agnostic side: forges with and without the optional
``forge.update-branch`` / ``forge.auto-merge`` capabilities, and the reply a
human sees from ``/pr merge``. GitHub specifics are tested in the github
library and workflow tests.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.core import EnochApplication
from enoch.commands import pr_usage
from enoch.identity import load_identity
from enoch.providers.forge import FunctionForgeProvider, LocalForgeProvider
from our_ark_provider_kit import (
    ProviderCapabilities,
    PullRequestAutoMergeResult,
    PullRequestBranchUpdateResult,
    PullRequestMergeStatus,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewProviderError,
    as_review_provider,
)
from tests.test_enoch_application import _Chat, _Runtime


URL = "https://github.com/our-ark/enoch/pull/91"


class ReviewBranchUpdateTests(unittest.TestCase):
    def test_forge_without_capability_refuses_behind_review(self) -> None:
        forge = _forge()
        review = as_review_provider(forge)

        self.assertFalse(forge.capabilities.supports("forge.update-branch"))
        with self.assertRaisesRegex(
            ReviewProviderError,
            "PR #91 is behind main and must be updated before it can merge",
        ):
            review.land_review(ReviewLandRequest(ReviewIdentity(id=URL, url=URL)))
        forge.merge_fn.assert_not_called()

    def test_update_method_without_declared_capability_is_not_used(self) -> None:
        class UndeclaredForge:
            name = "undeclared"
            provider_kind = "forge"
            capabilities = ProviderCapabilities(
                provider_kind="forge",
                capabilities=frozenset({"forge.read", "forge.merge"}),
            )
            inspect_pull_request_merge = MagicMock(return_value=_status())
            update_review_branch = MagicMock()
            merge_pull_request = MagicMock()

        forge = UndeclaredForge()
        with self.assertRaisesRegex(ReviewProviderError, "must be updated before it can merge"):
            as_review_provider(forge).land_review(
                ReviewLandRequest(ReviewIdentity(id=URL, url=URL))
            )
        forge.update_review_branch.assert_not_called()
        forge.merge_pull_request.assert_not_called()

    def test_forge_that_cannot_auto_merge_only_updates_the_branch(self) -> None:
        update = MagicMock(return_value=_branch_update("merge-92"))
        forge = _forge(update_branch_fn=update)

        result = as_review_provider(forge).land_review(
            ReviewLandRequest(ReviewIdentity(id=URL, url=URL))
        )

        self.assertTrue(forge.capabilities.supports("forge.update-branch"))
        self.assertFalse(forge.capabilities.supports("forge.auto-merge"))
        self.assertEqual(result.status, "branch-updated")
        self.assertIn("This forge cannot enable auto-merge.", result.message)
        update.assert_called_once_with(URL, expected_head_sha="head-91", root=None)
        forge.merge_fn.assert_not_called()

    def test_local_forge_keeps_refusing_merges(self) -> None:
        with self.assertRaisesRegex(Exception, "requires a configured forge provider"):
            as_review_provider(LocalForgeProvider()).land_review(
                ReviewLandRequest(ReviewIdentity(id=URL, url=URL))
            )

    def test_pr_help_describes_behind_handling(self) -> None:
        usage = pr_usage()

        self.assertIn("only behind its base branch", usage)
        self.assertIn("auto-merge", usage)
        self.assertIn("Conflicted or blocked reviews are still refused", usage)


class PrMergeCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)

    def _app(self, temp: str, forge: FunctionForgeProvider) -> EnochApplication:
        return EnochApplication(
            load_identity(),
            Path(temp),
            _Chat(),
            runtime=_Runtime(),
            review=as_review_provider(forge),
        )

    def test_pr_merge_on_behind_review_enables_auto_merge(self) -> None:
        forge = _forge(
            update_branch_fn=MagicMock(return_value=_branch_update("merge-92")),
            enable_auto_merge_fn=MagicMock(
                return_value=PullRequestAutoMergeResult(
                    number=91, url=URL, enabled=True, method="merge"
                )
            ),
        )
        with TemporaryDirectory() as temp:
            reply = self._app(temp, forge)._pr("room-1", "merge 91")

        self.assertTrue(
            reply.startswith(
                "PR #91 was behind main. I updated its branch and enabled auto-merge; "
                "it will land when CI passes. If CI fails it will not merge."
            ),
            reply,
        )
        self.assertIn("Updated revision: merge-92", reply)
        self.assertIn(f"URL: {URL}", reply)
        forge.update_branch_fn.assert_called_once()
        self.assertEqual(
            forge.update_branch_fn.call_args.kwargs["expected_head_sha"], "head-91"
        )
        forge.enable_auto_merge_fn.assert_called_once()
        self.assertEqual(forge.enable_auto_merge_fn.call_args.args, ("91",))
        self.assertEqual(
            forge.enable_auto_merge_fn.call_args.kwargs["expected_head_sha"], "merge-92"
        )
        forge.merge_fn.assert_not_called()

    def test_pr_merge_on_behind_review_without_auto_merge_asks_again_later(self) -> None:
        forge = _forge(
            update_branch_fn=MagicMock(return_value=_branch_update("merge-92")),
            enable_auto_merge_fn=MagicMock(
                return_value=PullRequestAutoMergeResult(
                    number=91,
                    url=URL,
                    enabled=False,
                    message="This repository does not allow auto-merge.",
                )
            ),
        )
        with TemporaryDirectory() as temp:
            reply = self._app(temp, forge)._pr("room-1", "merge 91")

        self.assertIn("PR #91 was behind main. I updated its branch and CI is running.", reply)
        self.assertIn("ask me to merge it again once CI is green", reply)
        self.assertNotIn("landed", reply)
        forge.merge_fn.assert_not_called()

    def test_pr_merge_on_behind_review_without_capability_is_refused(self) -> None:
        forge = _forge()
        with TemporaryDirectory() as temp:
            reply = self._app(temp, forge)._pr("room-1", "merge 91")

        self.assertIn("could not land that review", reply)
        self.assertIn("PR #91 is behind main and must be updated before it can merge", reply)
        forge.merge_fn.assert_not_called()

    def test_pr_merge_from_another_conversation_does_not_update(self) -> None:
        update = MagicMock()
        forge = _forge(update_branch_fn=update)
        with TemporaryDirectory() as temp:
            reply = self._app(temp, forge)._pr("room-2", "merge 91")

        self.assertIn("locked", reply)
        update.assert_not_called()
        forge.merge_fn.assert_not_called()


def _forge(**optional) -> FunctionForgeProvider:
    return FunctionForgeProvider(
        close_fn=MagicMock(),
        create_fn=MagicMock(),
        inspect_fn=MagicMock(),
        inspect_merge_fn=MagicMock(return_value=_status()),
        list_fn=MagicMock(return_value=()),
        merge_fn=MagicMock(),
        **optional,
    )


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
        "merge_state_status": "BEHIND",
        "head_sha": "head-91",
    }
    values.update(overrides)
    return PullRequestMergeStatus(**values)


def _branch_update(head_sha: str) -> PullRequestBranchUpdateResult:
    return PullRequestBranchUpdateResult(
        number=91,
        url=URL,
        base_branch="main",
        previous_head_sha="head-91",
        head_sha=head_sha,
    )


if __name__ == "__main__":
    unittest.main()
