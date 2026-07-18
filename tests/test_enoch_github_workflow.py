from pathlib import Path
import json
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.github.workflow import (
    EvolutionProvenance,
    PublishError,
    PullRequestMergeStatus,
    close_pull_request,
    create_pull_request,
    inspect_pull_request_merge,
    merge_pull_request,
    parse_pull_request_target,
    prepare_local_publish,
    push_current_branch,
)
from enoch.immune import DoctorDiagnosis


class EnochGithubWorkflowTests(unittest.TestCase):
    @patch("enoch.github.workflow.current_branch", return_value="main")
    def test_refuses_protected_branch_by_default(self, _current_branch: MagicMock) -> None:
        with self.assertRaisesRegex(PublishError, "Refusing to publish from main"):
            prepare_local_publish("Test commit", root=ROOT)

    @patch("enoch.github.workflow.changed_files", return_value=[])
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_refuses_empty_diff(
        self, _current_branch: MagicMock, _changed_files: MagicMock
    ) -> None:
        with self.assertRaisesRegex(PublishError, "No local changes"):
            prepare_local_publish("Test commit", root=ROOT)

    @patch("enoch.github.workflow.run_immune_system")
    @patch("enoch.github.workflow.diff_summary", return_value="README.md | 1 +")
    @patch("enoch.github.workflow.changed_files", return_value=["README.md"])
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_refuses_when_doctor_fails(
        self,
        _current_branch: MagicMock,
        _changed_files: MagicMock,
        _diff_summary: MagicMock,
        run_immune_system: MagicMock,
    ) -> None:
        run_immune_system.return_value = _doctor_result(passed=False, summary="1 test(s) failed.")

        with self.assertRaisesRegex(PublishError, "Doctor failed"):
            prepare_local_publish("Test commit", root=ROOT)

    @patch("enoch.github.workflow.read_section")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.run_immune_system")
    @patch("enoch.github.workflow.diff_summary", return_value="README.md | 1 +")
    @patch("enoch.github.workflow.changed_files", return_value=["README.md"])
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_stages_and_commits_after_doctor_passes(
        self,
        _current_branch: MagicMock,
        _changed_files: MagicMock,
        _diff_summary: MagicMock,
        run_immune_system: MagicMock,
        run_git: MagicMock,
        read_section: MagicMock,
    ) -> None:
        doctor = _doctor_result(passed=True, summary="All configured health checks passed.")
        run_immune_system.return_value = doctor
        read_section.return_value = {
            "author_name": "Gary Zhao",
            "author_email": "3261777+peking2@users.noreply.github.com",
        }
        run_git.side_effect = [
            _git_result(returncode=0),
            _git_result(returncode=0),
            _git_result(returncode=0, stdout="abc123"),
        ]

        result = prepare_local_publish("Test commit", root=ROOT)

        self.assertEqual(result.branch, "feature/enoch")
        self.assertEqual(result.changed_files, ["README.md"])
        self.assertEqual(result.doctor, doctor)
        self.assertEqual(result.commit_sha, "abc123")
        run_git.assert_any_call(["add", "--", "README.md"], ROOT)
        run_git.assert_any_call(
            [
                "-c",
                "user.email=3261777+peking2@users.noreply.github.com",
                "-c",
                "user.name=Gary Zhao",
                "commit",
                "-m",
                "Test commit",
            ],
            ROOT,
        )

    @patch("enoch.github.workflow.read_section", return_value={})
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.run_immune_system")
    @patch("enoch.github.workflow.diff_summary", return_value="README.md | 1 +")
    @patch("enoch.github.workflow.changed_files", return_value=["README.md"])
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_commit_author_falls_back_to_latest_noreply_email(
        self,
        _current_branch: MagicMock,
        _changed_files: MagicMock,
        _diff_summary: MagicMock,
        run_immune_system: MagicMock,
        run_git: MagicMock,
        _read_section: MagicMock,
    ) -> None:
        run_immune_system.return_value = _doctor_result(passed=True, summary="All configured health checks passed.")
        run_git.side_effect = [
            _git_result(returncode=0),
            _git_result(returncode=0, stdout="Gary Zhao"),
            _git_result(returncode=0, stdout="3261777+peking2@users.noreply.github.com"),
            _git_result(returncode=0),
            _git_result(returncode=0, stdout="abc123"),
        ]

        prepare_local_publish("Test commit", root=ROOT)

        run_git.assert_any_call(
            [
                "-c",
                "user.email=3261777+peking2@users.noreply.github.com",
                "-c",
                "user.name=Gary Zhao",
                "commit",
                "-m",
                "Test commit",
            ],
            ROOT,
        )

    @patch("enoch.github.workflow.run_immune_system")
    @patch("enoch.github.workflow.diff_summary", return_value="README.md | 1 +\nscratch.txt | 1 +")
    @patch("enoch.github.workflow.changed_files", return_value=["README.md", "scratch.txt"])
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_publish_refuses_unexpected_files(
        self,
        _current_branch: MagicMock,
        _changed_files: MagicMock,
        _diff_summary: MagicMock,
        run_immune_system: MagicMock,
    ) -> None:
        run_immune_system.return_value = _doctor_result(passed=True, summary="All configured health checks passed.")

        with self.assertRaisesRegex(PublishError, "unexpected files"):
            prepare_local_publish("Test commit", root=ROOT, allowed_files=("README.md",))

    @patch("enoch.github.workflow.current_branch", return_value="main")
    def test_push_refuses_protected_branch_by_default(self, _current_branch: MagicMock) -> None:
        with self.assertRaisesRegex(PublishError, "Refusing to push main"):
            push_current_branch(root=ROOT)

    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_push_refuses_when_branch_has_no_ahead_commits(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
    ) -> None:
        run_git.return_value = _git_result(returncode=0, stdout="0")

        with self.assertRaisesRegex(PublishError, "no local commits ahead"):
            push_current_branch(root=ROOT)

    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_pushes_current_branch_and_returns_compare_url(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        ensure_clean_worktree: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=0, stdout="2"),
            _git_result(returncode=0, stdout=""),
            _git_result(returncode=0, stdout="https://github.com/our-ark/genesis.git"),
        ]

        result = push_current_branch(root=ROOT)

        ensure_clean_worktree.assert_called_once_with(ROOT)
        self.assertTrue(result.pushed)
        self.assertEqual(result.branch, "feature/enoch")
        self.assertEqual(result.ahead_count, 2)
        self.assertEqual(
            result.compare_url,
            "https://github.com/our-ark/genesis/compare/main...feature/enoch?expand=1",
        )
        run_git.assert_any_call(["push", "-u", "origin", "feature/enoch"], ROOT)

    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_push_count_for_new_branch_uses_base_branch(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=1, stderr="unknown revision"),
            _git_result(returncode=0, stdout="2"),
            _git_result(returncode=0, stdout=""),
            _git_result(returncode=0, stdout="https://github.com/our-ark/genesis.git"),
        ]

        result = push_current_branch(root=ROOT)

        self.assertEqual(result.ahead_count, 2)
        run_git.assert_any_call(["rev-list", "--count", "origin/main..HEAD"], ROOT)

    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_push_compare_url_allows_dots_in_repo_name(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=0, stdout="1"),
            _git_result(returncode=0, stdout=""),
            _git_result(returncode=0, stdout="https://github.com/our-ark/enoch.agent.git"),
        ]

        result = push_current_branch(root=ROOT)

        self.assertEqual(
            result.compare_url,
            "https://github.com/our-ark/enoch.agent/compare/main...feature/enoch?expand=1",
        )

    @patch("enoch.github.workflow.current_branch", return_value="main")
    def test_pr_refuses_protected_branch_by_default(self, _current_branch: MagicMock) -> None:
        with self.assertRaisesRegex(PublishError, "Refusing to open a PR from main"):
            create_pull_request(root=ROOT)

    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_pr_requires_pushed_branch(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
    ) -> None:
        run_git.return_value = _git_result(returncode=1, stderr="missing")

        with self.assertRaisesRegex(PublishError, "Push this branch first"):
            create_pull_request(root=ROOT)

    @patch("enoch.github.workflow.shutil.which", return_value=None)
    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_pr_returns_fallback_url_when_gh_is_missing(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
        _which: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=0, stdout="abc123"),
            _git_result(returncode=0, stdout="Add Enoch feature"),
            _git_result(returncode=0, stdout="Add Enoch feature"),
            _git_result(returncode=0, stdout="https://github.com/our-ark/genesis.git"),
        ]

        result = create_pull_request(root=ROOT)

        self.assertFalse(result.created)
        self.assertEqual(result.title, "Add Enoch feature")
        self.assertEqual(result.note, "GitHub CLI is not available.")
        self.assertEqual(
            result.fallback_url,
            "https://github.com/our-ark/genesis/compare/main...feature/enoch?expand=1",
        )

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_pr_creates_with_gh(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=0, stdout="abc123"),
            _git_result(returncode=0, stdout="Add Enoch feature"),
            _git_result(returncode=0, stdout="Add Enoch feature"),
            _git_result(returncode=0, stdout="https://github.com/our-ark/genesis.git"),
        ]
        run.return_value.returncode = 0
        run.return_value.stdout = "https://github.com/our-ark/genesis/pull/12\n"
        run.return_value.stderr = ""

        result = create_pull_request(root=ROOT)

        self.assertTrue(result.created)
        self.assertEqual(result.url, "https://github.com/our-ark/genesis/pull/12")
        args = run.call_args.args[0]
        self.assertEqual(args[:3], ["/usr/local/bin/gh", "pr", "create"])
        self.assertIn("--head", args)
        self.assertIn("feature/enoch", args)
        self.assertNotIn("--draft", args)
        self.assertFalse(result.draft)

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_pr_appends_evolution_provenance(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=0, stdout="abc123"),
            _git_result(returncode=0, stdout="Trace evolve candidate"),
            _git_result(returncode=0, stdout="https://github.com/our-ark/enoch.git"),
        ]
        run.return_value.returncode = 0
        run.return_value.stdout = "https://github.com/our-ark/enoch/pull/12\n"
        run.return_value.stderr = ""

        result = create_pull_request(
            body="## Summary\n- Preserve traceability.",
            root=ROOT,
            evolution_provenance=EvolutionProvenance(
                candidate_id="feedback-c3ed71fd1d2d",
                evidence_source="feedback",
                signal_actor="human",
                candidate_actor="agent",
                approval_actor="human",
                task_id=3,
                parent_candidate_id="feedback-parent",
                source_task_id=1,
                retry_of_task_id=1,
            ),
        )

        self.assertIn("## Evolution provenance", result.body)
        self.assertIn("- Candidate: `feedback-c3ed71fd1d2d`", result.body)
        self.assertIn("- Evidence source: feedback", result.body)
        self.assertIn("- Signal actor: human", result.body)
        self.assertIn("- Candidate actor: agent", result.body)
        self.assertIn("- Approval actor: human", result.body)
        self.assertIn("- Task: #3", result.body)
        self.assertIn("- Parent candidate: `feedback-parent`", result.body)
        self.assertIn("- Source task: #1", result.body)
        self.assertIn("- Retry of task: #1", result.body)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("--body") + 1], result.body)

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    @patch("enoch.github.workflow.ensure_clean_worktree")
    @patch("enoch.github.workflow.run_git")
    @patch("enoch.github.workflow.current_branch", return_value="feature/enoch")
    def test_pr_uses_draft_only_when_explicitly_requested(
        self,
        _current_branch: MagicMock,
        run_git: MagicMock,
        _ensure_clean_worktree: MagicMock,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run_git.side_effect = [
            _git_result(returncode=0, stdout="abc123"),
            _git_result(returncode=0, stdout="Add unfinished experiment"),
            _git_result(returncode=0, stdout="Add unfinished experiment"),
            _git_result(returncode=0, stdout="https://github.com/our-ark/genesis.git"),
        ]
        run.return_value.returncode = 0
        run.return_value.stdout = "https://github.com/our-ark/genesis/pull/13\n"
        run.return_value.stderr = ""

        result = create_pull_request(root=ROOT, draft=True)

        self.assertTrue(result.created)
        self.assertTrue(result.draft)
        self.assertIn("--draft", run.call_args.args[0])

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    @patch("enoch.github.workflow.run_git")
    def test_closes_pull_request_with_comment(
        self,
        run_git: MagicMock,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run_git.return_value = _git_result(returncode=0, stdout="https://github.com/our-ark/enoch.git")
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""

        result = close_pull_request(2, root=ROOT, comment="duplicate")

        self.assertTrue(result.closed)
        self.assertEqual(result.url, "https://github.com/our-ark/enoch/pull/2")
        self.assertEqual(
            run.call_args.args[0],
            ["/usr/local/bin/gh", "pr", "close", "2", "--comment", "duplicate"],
        )

    @patch("enoch.github.workflow.shutil.which", return_value=None)
    @patch("enoch.github.workflow.run_git")
    def test_close_pull_request_reports_missing_gh(
        self,
        run_git: MagicMock,
        _which: MagicMock,
    ) -> None:
        run_git.return_value = _git_result(returncode=0, stdout="https://github.com/our-ark/enoch.git")

        result = close_pull_request(2, root=ROOT)

        self.assertFalse(result.closed)
        self.assertEqual(result.note, "GitHub CLI is not available.")
        self.assertEqual(result.url, "https://github.com/our-ark/enoch/pull/2")

    @patch("enoch.github.workflow.subprocess.run")
    @patch(
        "enoch.github.workflow.shutil.which",
        return_value="/usr/local/bin/gh",
    )
    def test_inspects_pull_request_merge_evidence(
        self,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = (
            '{"state":"MERGED","mergedAt":"2026-07-18T18:30:00Z",'
            '"mergeCommit":{"oid":"7207317abc"},"baseRefName":"main",'
            '"url":"https://github.com/our-ark/enoch/pull/12"}'
        )
        run.return_value.stderr = ""

        result = inspect_pull_request_merge(
            "https://github.com/our-ark/enoch/pull/12",
            ROOT,
        )

        self.assertEqual(result.state, "MERGED")
        self.assertEqual(result.merge_commit, "7207317abc")
        self.assertEqual(result.base_branch, "main")
        self.assertEqual(result.merged_at, "2026-07-18T18:30:00Z")
        self.assertEqual(
            run.call_args.args[0],
            [
                "/usr/local/bin/gh",
                "pr",
                "view",
                "https://github.com/our-ark/enoch/pull/12",
                "--json",
                (
                    "number,state,isDraft,mergeable,mergeStateStatus,mergedAt,"
                    "mergeCommit,baseRefName,headRefOid,url"
                ),
            ],
        )

    def test_parses_numeric_pull_request_target_for_current_repository(self) -> None:
        target = parse_pull_request_target("12")

        self.assertEqual(target.reference, "12")
        self.assertEqual(target.number, 12)
        self.assertEqual(target.repository, "")

    def test_parses_and_normalizes_full_github_pull_request_url(self) -> None:
        target = parse_pull_request_target(
            "https://github.com/our-ark/enoch/pull/12/?tab=checks"
        )

        self.assertEqual(target.reference, "https://github.com/our-ark/enoch/pull/12")
        self.assertEqual(target.number, 12)
        self.assertEqual(target.repository, "our-ark/enoch")

    def test_rejects_non_pull_request_target(self) -> None:
        for target in ("", "0", "feature", "https://github.com/our-ark/enoch/issues/12"):
            with self.subTest(target=target), self.assertRaisesRegex(
                PublishError,
                "positive PR number",
            ):
                parse_pull_request_target(target)

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    def test_numeric_target_is_inspected_in_current_repository(
        self,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run.return_value = _process_result(
            stdout=(
                '{"number":12,"state":"OPEN","isDraft":false,"mergeable":"MERGEABLE",'
                '"mergeStateStatus":"CLEAN","mergedAt":null,"mergeCommit":null,'
                '"baseRefName":"main","headRefOid":"abc123",'
                '"url":"https://github.com/our-ark/enoch/pull/12"}'
            )
        )

        result = inspect_pull_request_merge("12", ROOT)

        self.assertEqual(result.repository, "our-ark/enoch")
        self.assertEqual(result.number, 12)
        self.assertEqual(run.call_args.args[0][3], "12")

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    def test_inaccessible_pull_request_url_reports_github_failure(
        self,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        run.return_value = _process_result(
            returncode=1,
            stderr="GraphQL: Could not resolve to a PullRequest",
        )

        with self.assertRaisesRegex(PublishError, "Could not resolve"):
            inspect_pull_request_merge("https://github.com/private/repo/pull/12", ROOT)

    def test_merge_refuses_draft_conflicting_blocked_closed_and_merged_prs(self) -> None:
        cases = (
            (_merge_status(is_draft=True), "is a draft"),
            (
                _merge_status(mergeable="CONFLICTING", merge_state_status="DIRTY"),
                "merge conflicts",
            ),
            (_merge_status(merge_state_status="BLOCKED"), "merge state: blocked"),
            (_merge_status(state="CLOSED"), "is closed"),
            (_merge_status(state="MERGED", merged_at="2026-07-18T18:30:00Z"), "already merged"),
        )
        for status, expected in cases:
            with self.subTest(expected=expected), patch(
                "enoch.github.workflow.inspect_pull_request_merge",
                return_value=status,
            ):
                with self.assertRaisesRegex(PublishError, expected):
                    merge_pull_request("12", ROOT)

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    @patch("enoch.github.workflow.inspect_pull_request_merge", return_value=None)
    def test_merges_inspected_head_with_supported_default_method(
        self,
        inspect: MagicMock,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        inspect.return_value = _merge_status()
        run.side_effect = [
            _process_result(
                stdout=(
                    '{"mergeCommitAllowed":true,"squashMergeAllowed":true,'
                    '"rebaseMergeAllowed":true}'
                )
            ),
            _process_result(
                stdout=(
                    '{"merged":true,"message":"Pull Request successfully merged",'
                    '"sha":"def456"}'
                )
            ),
        ]

        result = merge_pull_request("12", ROOT)

        self.assertEqual(result.number, 12)
        self.assertEqual(result.method, "merge")
        self.assertEqual(result.merge_commit, "def456")
        self.assertEqual(
            run.call_args_list[1].args[0],
            [
                "/usr/local/bin/gh",
                "api",
                "--method",
                "PUT",
                "repos/our-ark/enoch/pulls/12/merge",
                "--input",
                "-",
            ],
        )
        self.assertEqual(
            json.loads(run.call_args_list[1].kwargs["input"]),
            {"sha": "abc123", "merge_method": "merge"},
        )

    @patch("enoch.github.workflow.subprocess.run")
    @patch("enoch.github.workflow.shutil.which", return_value="/usr/local/bin/gh")
    @patch("enoch.github.workflow.inspect_pull_request_merge", return_value=None)
    def test_merge_reports_github_api_failure(
        self,
        inspect: MagicMock,
        _which: MagicMock,
        run: MagicMock,
    ) -> None:
        inspect.return_value = _merge_status()
        run.side_effect = [
            _process_result(stdout='{"mergeCommitAllowed":true}'),
            _process_result(returncode=1, stderr="HTTP 405: Pull Request is not mergeable"),
        ]

        with self.assertRaisesRegex(PublishError, "HTTP 405"):
            merge_pull_request("12", ROOT)


def _doctor_result(passed: bool, summary: str) -> MagicMock:
    result = MagicMock()
    result.passed = passed
    result.command = "python3 -m unittest discover -s tests"
    result.output = "OK" if passed else "FAILED"
    result.diagnosis = DoctorDiagnosis(
        summary=summary,
        failing_tests=[],
        likely_files=[],
        suggested_action="No repair needed." if passed else "Inspect failing tests.",
    )
    return result


def _git_result(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _process_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _merge_status(**overrides) -> PullRequestMergeStatus:
    values = {
        "reference": "12",
        "url": "https://github.com/our-ark/enoch/pull/12",
        "state": "OPEN",
        "base_branch": "main",
        "merge_commit": "",
        "merged_at": "",
        "number": 12,
        "repository": "our-ark/enoch",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "merge_state_status": "CLEAN",
        "head_sha": "abc123",
    }
    values.update(overrides)
    return PullRequestMergeStatus(**values)


if __name__ == "__main__":
    unittest.main()
