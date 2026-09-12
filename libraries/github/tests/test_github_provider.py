from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
for source in (
    ROOT / "src",
    ROOT / "libraries" / "provider-kit" / "src",
    ROOT / "libraries" / "github" / "src",
):
    sys.path.insert(0, str(source))

from our_ark_github import OUR_ARK_PROVIDERS, GithubForgeProvider
from our_ark_provider_kit import (
    ForgeProvider,
    ProviderContractConformanceMixin,
    PullRequestMergeResult,
    PullRequestMergeStatus,
    PullRequestResult,
    RepositoryRevision,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewProvider,
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


if __name__ == "__main__":
    unittest.main()
