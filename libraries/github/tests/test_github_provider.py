from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[3]
for source in (
    ROOT / "src",
    ROOT / "libraries" / "provider-kit" / "src",
    ROOT / "libraries" / "github" / "src",
):
    sys.path.insert(0, str(source))

from our_ark_github import OUR_ARK_PROVIDERS, GithubForgeProvider, workflow
from our_ark_provider_kit import (
    ForgeProvider,
    ProviderContractConformanceMixin,
    PullRequestBranchUpdateResult,
    PullRequestMergeResult,
    PullRequestMergeStatus,
    PullRequestResult,
    RepositoryRevision,
    ReviewCheck,
    ReviewCheckFailure,
    ReviewCheckReport,
    ReviewCommit,
    ReviewRevisionComparison,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewProvider,
    ReviewProviderError,
    ReviewSubmission,
    UnsupportedProviderFeature,
    as_review_provider,
)


class GithubProviderTests(ProviderContractConformanceMixin, unittest.TestCase):
    provider_kind = "forge"
    provider_protocol = ForgeProvider

    def create_provider(self, root: Path) -> GithubForgeProvider:
        return GithubForgeProvider(gh="/usr/local/bin/gh", root=root)

    def test_descriptor_registers_forge_factory(self) -> None:
        descriptor = OUR_ARK_PROVIDERS[0]

        self.assertEqual(descriptor["kind"], "forge")
        self.assertEqual(descriptor["name"], "github")
        self.assertTrue(descriptor["default"])

    def test_github_provider_adapts_to_semantic_review_contract(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")
        result = PullRequestResult(
            branch="change/config",
            title="Review config",
            body="Evidence.",
            created=True,
            url="https://github.com/our-ark/enoch/pull/42",
            fallback_url=None,
        )
        review_provider = as_review_provider(provider)

        with patch.object(provider, "publish_revision_for_review") as publish:
            with patch.object(provider, "create_pull_request", return_value=result) as create:
                review = review_provider.publish_review(
                    ReviewSubmission(
                        title="Review config",
                        body="Evidence.",
                        revision=RepositoryRevision("revision-42"),
                    )
                )

        self.assertIsInstance(review_provider, ReviewProvider)
        self.assertEqual(review.identity.id, result.url)
        self.assertEqual(review.versions[-1].revision.id, "revision-42")
        publish.assert_called_once_with(root=None)
        create.assert_called_once()

    def test_github_adapter_rejects_stack_before_provider_side_effect(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")
        review_provider = as_review_provider(provider)
        dependency = review_provider.publish_review
        request = ReviewSubmission(
            title="Dependent review",
            body="",
            revision=RepositoryRevision("revision-2"),
            dependencies=(
                ReviewIdentity(
                    id="review-independent",
                    url="https://reviews.invalid/review-independent",
                ),
            ),
        )

        with patch.object(provider, "publish_revision_for_review") as publish:
            with patch.object(provider, "create_pull_request") as create:
                with self.assertRaises(UnsupportedProviderFeature):
                    dependency(request)

        publish.assert_not_called()
        create.assert_not_called()

    def test_github_adapter_returns_verified_review_landing_evidence(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")
        review_provider = as_review_provider(provider)
        identity = ReviewIdentity(
            id="https://github.com/our-ark/enoch/pull/42",
            url="https://github.com/our-ark/enoch/pull/42",
        )
        merged_at = "2026-07-18T18:30:00Z"
        status = PullRequestMergeStatus(
            reference=identity.id,
            url=identity.url,
            state="MERGED",
            base_branch="main",
            merge_commit="revision-42",
            merged_at=merged_at,
            number=42,
            head_sha="head-42",
        )
        merged = PullRequestMergeResult(
            number=42,
            url=identity.url,
            method="merge",
            merge_commit="revision-42",
            message="Merged.",
        )

        with patch.object(provider, "merge_pull_request", return_value=merged):
            with patch.object(
                provider,
                "inspect_pull_request_merge",
                return_value=status,
            ):
                result = review_provider.land_review(ReviewLandRequest(identity))
                record = review_provider.inspect_review(identity)

        self.assertEqual(result.status, "landed")
        self.assertEqual(result.revision, RepositoryRevision("revision-42"))
        self.assertEqual(result.landed_at, merged_at)
        self.assertEqual(record.state, "landed")
        self.assertEqual(record.landed_revision, result.revision)
        self.assertEqual(record.landed_at, merged_at)

    def test_github_declares_optional_branch_update_and_check_status(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")

        self.assertTrue(provider.capabilities.supports("forge.update-branch"))
        self.assertTrue(provider.capabilities.supports("forge.check-status"))
        self.assertTrue(provider.capabilities.supports("forge.ci-repair"))
        self.assertFalse(provider.capabilities.supports("forge.auto-merge"))
        review_provider = as_review_provider(provider)
        self.assertTrue(review_provider.capabilities.supports("forge.update-branch"))
        self.assertTrue(review_provider.capabilities.supports("forge.check-status"))

    def test_land_review_never_updates_or_waits_on_its_own(self) -> None:
        provider, review_provider, identity = _behind_fixture()
        update = MagicMock()

        with patch.object(provider, "update_review_branch", update), patch(
            "our_ark_github.workflow.inspect_pull_request_merge",
            return_value=_status(),
        ):
            with self.assertRaisesRegex(
                workflow.PublishError,
                "PR #42 is behind main and must be updated before it can merge",
            ):
                review_provider.land_review(ReviewLandRequest(identity))

        update.assert_not_called()

    def test_land_review_pins_the_expected_head(self) -> None:
        provider, review_provider, identity = _behind_fixture()
        merged = PullRequestMergeResult(
            number=42, url=identity.url, method="merge", merge_commit="revision-42", message="Merged."
        )
        landed = _status(state="MERGED", merge_commit="revision-42", merged_at="2026-07-18T18:30:00Z")

        with patch.object(provider, "inspect_pull_request_merge", return_value=landed), \
                patch.object(provider, "merge_pull_request", return_value=merged) as merge:
            result = review_provider.land_review(
                ReviewLandRequest(identity, expected_revision="head-42")
            )

        self.assertEqual(result.status, "landed")
        merge.assert_called_once_with(identity.id, None, expected_head_sha="head-42")

    def test_conflicted_blocked_and_draft_reviews_are_still_refused_without_updating(self) -> None:
        cases = (
            (_status(mergeable="CONFLICTING", merge_state_status="DIRTY"), "merge conflicts"),
            (_status(mergeable="CONFLICTING", merge_state_status="BEHIND"), "merge conflicts"),
            (_status(merge_state_status="BLOCKED"), "merge state: blocked"),
            (_status(is_draft=True), "is a draft"),
        )
        for status, expected in cases:
            with self.subTest(expected=expected):
                provider, review_provider, identity = _behind_fixture()
                update = MagicMock()
                with patch.object(provider, "update_review_branch", update), patch(
                    "our_ark_github.workflow.inspect_pull_request_merge",
                    return_value=status,
                ):
                    with self.assertRaisesRegex(workflow.PublishError, expected):
                        review_provider.land_review(ReviewLandRequest(identity))
                update.assert_not_called()

    def test_clean_and_unstable_reviews_merge_as_before(self) -> None:
        for merge_state in ("CLEAN", "UNSTABLE"):
            with self.subTest(merge_state=merge_state):
                provider, review_provider, identity = _behind_fixture()
                merged = PullRequestMergeResult(
                    number=42,
                    url=identity.url,
                    method="merge",
                    merge_commit="revision-42",
                    message="Merged.",
                )
                landed = _status(
                    state="MERGED",
                    merge_commit="revision-42",
                    merged_at="2026-07-18T18:30:00Z",
                )
                update = MagicMock()
                with patch.object(
                    provider,
                    "inspect_pull_request_merge",
                    side_effect=[landed],
                ), patch.object(provider, "update_review_branch", update), \
                        patch.object(provider, "merge_pull_request", return_value=merged) as merge:
                    result = review_provider.land_review(ReviewLandRequest(identity))

                self.assertEqual(result.status, "landed")
                merge.assert_called_once_with(identity.id, None)
                update.assert_not_called()

    def test_adapter_updates_branch_with_head_guard(self) -> None:
        provider, review_provider, identity = _behind_fixture()
        update = MagicMock(return_value=_branch_update("merge-43"))

        with patch.object(provider, "update_review_branch", update):
            result = review_provider.update_review_branch(identity, expected_revision="head-42")

        self.assertEqual(result.head_sha, "merge-43")
        update.assert_called_once_with(identity.id, expected_head_sha="head-42", root=None)

    def test_adapter_reports_checks_and_verifies_updates(self) -> None:
        provider, review_provider, identity = _behind_fixture()
        report = ReviewCheckReport(
            revision="head-42",
            checks=(ReviewCheck("test", "failure", "timed_out"),),
        )
        verify = MagicMock(return_value=True)

        with patch.object(provider, "inspect_review_checks", return_value=report) as checks, \
                patch.object(provider, "verify_review_branch_update", verify):
            self.assertEqual(review_provider.inspect_review_checks(identity), report)
            self.assertTrue(
                review_provider.verify_review_branch_update(
                    identity, previous_revision="head-42", revision="merge-43"
                )
            )

        checks.assert_called_once_with(identity.id, None)
        verify.assert_called_once_with(
            identity.id, previous_head_sha="head-42", head_sha="merge-43", root=None
        )
        self.assertFalse(report.green)
        self.assertEqual([check.name for check in report.failed], ["test"])

    def test_adapter_exposes_ci_repair_operations(self) -> None:
        provider, review_provider, identity = _behind_fixture()
        self.assertTrue(review_provider.capabilities.supports("forge.ci-repair"))
        failures = (ReviewCheckFailure("test", "failure", run_id="9", job_id="10", log="x"),)
        comparison = ReviewRevisionComparison(
            "head-42", "fix-43", True, (ReviewCommit("fix-43", "Fix it"),)
        )

        with patch.object(provider, "inspect_review_check_failures", return_value=failures) as inspect, \
                patch.object(provider, "rerun_failed_review_checks", return_value=("9",)) as rerun, \
                patch.object(provider, "compare_review_revisions", return_value=comparison) as compare:
            self.assertEqual(
                review_provider.inspect_review_check_failures(identity, revision="head-42"),
                failures,
            )
            self.assertEqual(
                review_provider.rerun_failed_review_checks(identity, revision="head-42"),
                ("9",),
            )
            self.assertEqual(
                review_provider.compare_review_revisions(
                    identity, base="head-42", revision="fix-43"
                ),
                comparison,
            )

        inspect.assert_called_once_with(identity.id, head_sha="head-42", root=None)
        rerun.assert_called_once_with(identity.id, head_sha="head-42", root=None)
        compare.assert_called_once_with(identity.id, base="head-42", head="fix-43", root=None)

        with patch.object(provider, "compare_review_revisions", return_value="bogus"):
            with self.assertRaises(ReviewProviderError):
                review_provider.compare_review_revisions(identity, base="a", revision="b")

    def test_review_record_carries_repository_and_base(self) -> None:
        provider, review_provider, identity = _behind_fixture()

        with patch.object(provider, "inspect_pull_request_merge", return_value=_status()):
            record = review_provider.inspect_review(identity)

        self.assertEqual(record.identity.metadata["repository"], "our-ark/enoch")
        self.assertEqual(record.identity.metadata["base_branch"], "main")
        self.assertEqual(record.versions[-1].revision.id, "head-42")

    def test_lineage_commit_listing_honors_limits_across_pages(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")
        first = [{"sha": f"sha-{index}"} for index in range(100)]
        second = [{"sha": f"sha-{index}"} for index in range(100, 150)]

        with patch.object(provider, "_json", side_effect=[first, second]) as request:
            commits = provider.commits("our-ark/seth", "main", limit=150)

        self.assertEqual(len(commits), 150)
        self.assertEqual(request.call_count, 2)
        self.assertIn("per_page=100&page=1", request.call_args_list[0].args[0][1])
        self.assertIn("per_page=50&page=2", request.call_args_list[1].args[0][1])

    def test_lineage_pr_commits_exposes_every_constituent_revision(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")
        with patch.object(
            provider,
            "_json",
            return_value={"commits": [{"oid": "one"}, {"oid": "two"}]},
        ):
            commits = provider.pr_commits("our-ark/seth", 42)

        self.assertEqual(commits, ("one", "two"))

    def test_declared_skills_prefers_published_body_file(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh", root=ROOT)
        with patch.object(
            provider,
            "_content_text",
            return_value="name: Lucy\nskills:\n  - name: teach\n",
        ) as read:
            skills = provider.declared_skills("our-ark/lucy", "main")

        self.assertEqual(skills, ("teach",))
        read.assert_called_once_with(
            "our-ark/lucy",
            "src/lucy/body.yaml",
            "main",
        )

    def test_declared_skills_falls_back_to_legacy_identity_file(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh", root=ROOT)
        with patch.object(
            provider,
            "_content_text",
            side_effect=[None, "name: Lucy\nskills:\n  - name: teach\n"],
        ) as read:
            skills = provider.declared_skills("our-ark/lucy", "main")

        self.assertEqual(skills, ("teach",))
        self.assertEqual(
            [call.args[1] for call in read.call_args_list],
            ["src/lucy/body.yaml", "src/lucy/identity.yaml"],
        )

    def test_lineage_commit_diff_includes_file_stats_and_patch(self) -> None:
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")
        with patch.object(
            provider,
            "_json",
            return_value={
                "files": [
                    {
                        "filename": "src/seth/core.py",
                        "status": "modified",
                        "additions": 2,
                        "deletions": 1,
                        "patch": "@@ -1 +1 @@\n-old\n+new",
                    }
                ]
            },
        ):
            diff = provider.commit_diff("our-ark/seth", "abc")

        self.assertIn("File: src/seth/core.py", diff)
        self.assertIn("additions: 2; deletions: 1", diff)
        self.assertIn("+new", diff)

    @patch("our_ark_github.subprocess.run")
    def test_reads_published_text_through_forge_contract(self, run) -> None:
        encoded = base64.b64encode(b"name: Lucy\n").decode("ascii")
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({"content": encoded})
        run.return_value.stderr = ""
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")

        text = provider.read_text("our-ark/lucy", "src/lucy/identity.yaml", "main")

        self.assertEqual(text, "name: Lucy\n")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/local/bin/gh",
                "api",
                "repos/our-ark/lucy/contents/src/lucy/identity.yaml?ref=main",
            ],
        )

    @patch("our_ark_github.subprocess.run")
    def test_health_reports_authenticated_cli(self, run) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "Logged in to github.com"
        run.return_value.stderr = ""
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")

        health = provider.health(ROOT)

        self.assertTrue(health.passed)
        self.assertEqual(health.summary, "authenticated")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/local/bin/gh", "auth", "status"],
        )

    @patch("our_ark_github.subprocess.run")
    def test_health_reports_invalid_authentication(self, run) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "The token is invalid."
        provider = GithubForgeProvider(gh="/usr/local/bin/gh")

        health = provider.health(ROOT)

        self.assertFalse(health.passed)
        self.assertEqual(health.summary, "not authenticated")
        self.assertIn("token is invalid", health.output)

    @patch("our_ark_github.shutil.which", return_value=None)
    def test_health_reports_missing_cli(self, _which) -> None:
        health = GithubForgeProvider().health(ROOT)

        self.assertEqual(health.summary, "gh not found")


def _behind_fixture():
    provider = GithubForgeProvider(gh="/usr/local/bin/gh")
    identity = ReviewIdentity(
        id="https://github.com/our-ark/enoch/pull/42",
        url="https://github.com/our-ark/enoch/pull/42",
    )
    return provider, as_review_provider(provider), identity


def _status(**overrides) -> PullRequestMergeStatus:
    values = {
        "reference": "https://github.com/our-ark/enoch/pull/42",
        "url": "https://github.com/our-ark/enoch/pull/42",
        "state": "OPEN",
        "base_branch": "main",
        "merge_commit": "",
        "merged_at": "",
        "number": 42,
        "repository": "our-ark/enoch",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "merge_state_status": "BEHIND",
        "head_sha": "head-42",
    }
    values.update(overrides)
    return PullRequestMergeStatus(**values)


def _branch_update(head_sha: str) -> PullRequestBranchUpdateResult:
    return PullRequestBranchUpdateResult(
        number=42,
        url="https://github.com/our-ark/enoch/pull/42",
        base_branch="main",
        previous_head_sha="head-42",
        head_sha=head_sha,
    )


if __name__ == "__main__":
    unittest.main()
