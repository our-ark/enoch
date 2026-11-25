"""GitHub update-branch: real HTTP status and reasons, and the local-merge fallback.

GitHub's ``PUT .../update-branch`` normally answers ``202 Accepted`` with a
JSON message. When it answers an error status with an empty JSON body, ``gh
api`` hides the status and prints only ``unexpected end of JSON input``; that
is what left a pending merge stuck at "updating from main". These tests mock
``gh`` for those answers. The fallback tests run real git against a local bare
"remote" and fake only ``gh``.
"""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libraries" / "provider-kit" / "src"))
sys.path.insert(0, str(ROOT / "libraries" / "github" / "src"))
sys.path.insert(0, str(ROOT / "src"))

from our_ark_github import workflow
from our_ark_github.workflow import (
    PublishError,
    PullRequestMergeStatus,
    _PublishAuthor,
    _remote_for_repository,
    update_pull_request_branch,
)


GH = "/usr/local/bin/gh"
PUT = ["--include", "--method", "PUT", "repos/our-ark/enoch/pulls/12/update-branch"]
EMPTY_BODY_STDERR = "unexpected end of JSON input"
_REAL_RUN = subprocess.run


class BranchUpdateResponseTests(unittest.TestCase):
    """How update-branch answers are read, with ``gh`` mocked."""

    def setUp(self) -> None:
        for target, value in (
            ("our_ark_github.workflow.time.sleep", None),
            ("our_ark_github.workflow.shutil.which", GH),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.inspect = self._patch(
            "our_ark_github.workflow.inspect_pull_request_merge",
            return_value=_status(),
        )
        self.local_merge = self._patch(
            "our_ark_github.workflow._merge_base_into_pull_request_locally",
            return_value="local789",
        )

    def _patch(self, target: str, **kwargs) -> MagicMock:
        patcher = patch(target, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    @patch("our_ark_github.workflow.subprocess.run")
    def test_202_with_an_empty_body_is_accepted_and_the_new_head_confirmed(
        self, run: MagicMock
    ) -> None:
        run.side_effect = [
            _http(202, "Accepted", ""),
            _ok({"head": {"sha": "merge789"}}),
            _ok({"sha": "merge789", "parents": [{"sha": "abc123"}, {"sha": "main456"}]}),
        ]

        result = update_pull_request_branch("12", expected_head_sha="abc123")

        self.assertEqual(result.head_sha, "merge789")
        self.assertEqual(result.message, "Updated PR #12 from main.")
        self.assertEqual(run.call_args_list[0].args[0][1:6], ["api", *PUT])
        self.local_merge.assert_not_called()

    @patch("our_ark_github.workflow.subprocess.run")
    def test_other_success_answers_are_accepted(self, run: MagicMock) -> None:
        for answer in (
            _http(200, "OK", '{"message":"Updating pull request branch."}'),
            _http(204, "No Content", ""),
            _process(stdout=""),  # an older gh without headers, exit 0
        ):
            with self.subTest(answer=answer.stdout[:16]):
                run.reset_mock()
                run.side_effect = [
                    answer,
                    _ok({"head": {"sha": "merge789"}}),
                    _ok({"sha": "merge789", "parents": [{"sha": "abc123"}]}),
                ]
                result = update_pull_request_branch("12", expected_head_sha="abc123")
                self.assertEqual(result.head_sha, "merge789")

    @patch("our_ark_github.workflow.subprocess.run")
    def test_a_real_422_reason_is_reported_without_retry_or_fallback(self, run: MagicMock) -> None:
        run.return_value = _http(
            422,
            "Unprocessable Entity",
            json.dumps(
                {
                    "message": "merge conflict between base and head",
                    "documentation_url": "https://docs.github.com/rest/pulls/pulls",
                    "status": "422",
                }
            ),
            stderr="gh: merge conflict between base and head (HTTP 422)",
        )

        with self.assertRaises(PublishError) as raised:
            update_pull_request_branch("12", expected_head_sha="abc123")

        self.assertEqual(
            str(raised.exception),
            "Could not update PR #12 from main: merge conflict between base and head "
            "(HTTP 422 Unprocessable Entity)",
        )
        run.assert_called_once()
        self.local_merge.assert_not_called()

    @patch("our_ark_github.workflow.subprocess.run")
    def test_error_detail_lists_are_included(self, run: MagicMock) -> None:
        run.return_value = _http(
            422,
            "Unprocessable Entity",
            '{"message":"Validation Failed","errors":[{"message":"head ref is locked"}]}',
        )

        with self.assertRaisesRegex(
            PublishError, r"Validation Failed: head ref is locked \(HTTP 422"
        ):
            update_pull_request_branch("12", expected_head_sha="abc123")

    @patch("our_ark_github.workflow.subprocess.run")
    def test_empty_body_error_shows_the_status_retries_then_merges_locally(
        self, run: MagicMock
    ) -> None:
        empty = _http(422, "Unprocessable Entity", "", stderr=EMPTY_BODY_STDERR)
        unchanged = _ok({"head": {"sha": "abc123"}})
        run.side_effect = [
            empty,
            unchanged,  # GitHub did not do the update after all
            empty,
            unchanged,
            # after the local merge: the new head is confirmed as usual
            _ok({"head": {"sha": "local789"}}),
            _ok({"sha": "local789", "parents": [{"sha": "abc123"}, {"sha": "main456"}]}),
        ]

        result = update_pull_request_branch("12", expected_head_sha="abc123")

        puts = [call for call in run.call_args_list if "--method" in call.args[0]]
        self.assertEqual(len(puts), 2)
        self.local_merge.assert_called_once()
        self.assertEqual(self.local_merge.call_args.args[2], "abc123")
        self.assertEqual(result.head_sha, "local789")
        self.assertEqual(
            result.message,
            "Updated PR #12 from main with a local merge (GitHub's update-branch "
            "failed: GitHub returned HTTP 422 Unprocessable Entity with an empty response).",
        )
        self.assertNotIn(EMPTY_BODY_STDERR, result.message)

    @patch("our_ark_github.workflow.subprocess.run")
    def test_empty_body_error_is_explained_when_the_fallback_is_unsafe(
        self, run: MagicMock
    ) -> None:
        empty = _http(422, "Unprocessable Entity", "", stderr=EMPTY_BODY_STDERR)
        unchanged = _ok({"head": {"sha": "abc123"}})
        run.side_effect = [empty, unchanged, empty, unchanged]
        # Re-inspected after the failures: someone pushed meanwhile.
        self.inspect.side_effect = [_status(), _status(head_sha="pushed999")]

        with self.assertRaises(PublishError) as raised:
            update_pull_request_branch("12", expected_head_sha="abc123")

        message = str(raised.exception)
        self.assertTrue(
            message.startswith(
                "Could not update PR #12 from main: GitHub returned HTTP 422 "
                "Unprocessable Entity with an empty response; PR #12 head changed to pushed999"
            ),
            message,
        )
        self.local_merge.assert_not_called()

    @patch("our_ark_github.workflow.subprocess.run")
    def test_a_failed_local_merge_is_reported_with_both_reasons(self, run: MagicMock) -> None:
        empty = _http(502, "Bad Gateway", "", stderr=EMPTY_BODY_STDERR)
        unchanged = _ok({"head": {"sha": "abc123"}})
        run.side_effect = [empty, unchanged, empty, unchanged]
        self.local_merge.side_effect = PublishError("merging main conflicts; nothing was pushed.")

        with self.assertRaises(PublishError) as raised:
            update_pull_request_branch("12", expected_head_sha="abc123")

        self.assertEqual(
            str(raised.exception),
            "Could not update PR #12 from main: GitHub returned HTTP 502 Bad Gateway with an "
            "empty response; the local merge fallback failed: merging main conflicts; "
            "nothing was pushed.",
        )

    @patch("our_ark_github.workflow.subprocess.run")
    def test_an_error_answer_after_github_did_update_is_accepted(self, run: MagicMock) -> None:
        run.side_effect = [
            _http(500, "Internal Server Error", "", stderr=EMPTY_BODY_STDERR),
            # The update went through anyway: the head is a direct update.
            _ok({"head": {"sha": "merge789"}}),
            _ok({"sha": "merge789", "parents": [{"sha": "abc123"}, {"sha": "main456"}]}),
            _ok({"head": {"sha": "merge789"}}),
            _ok({"sha": "merge789", "parents": [{"sha": "abc123"}, {"sha": "main456"}]}),
        ]

        result = update_pull_request_branch("12", expected_head_sha="abc123")

        self.assertEqual(result.head_sha, "merge789")
        self.assertEqual(result.message, "Updated PR #12 from main.")
        self.local_merge.assert_not_called()


class LocalMergeFallbackTests(unittest.TestCase):
    """The fallback runs real git against a local bare remote; only gh is fake."""

    def setUp(self) -> None:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.remote = self.tmp / "remote.git"
        self.clone = self.tmp / "clone"
        _git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        _git(self.tmp, "clone", "--quiet", str(self.remote), str(self.clone))
        _git(self.clone, "config", "user.name", "Repo Default")
        _git(self.clone, "config", "user.email", "repo@example.invalid")
        _git(self.clone, "checkout", "--quiet", "-b", "main")
        (self.clone / "a.txt").write_text("one\n")
        (self.clone / "b.txt").write_text("one\n")
        _git(self.clone, "add", ".")
        _git(self.clone, "commit", "--quiet", "-m", "base")
        _git(self.clone, "push", "--quiet", "origin", "main")
        _git(self.clone, "checkout", "--quiet", "-b", "feature")
        (self.clone / "a.txt").write_text("feature\n")
        _git(self.clone, "commit", "--quiet", "-am", "feature work")
        _git(self.clone, "push", "--quiet", "origin", "feature")
        self.feature = _git(self.clone, "rev-parse", "HEAD")
        _git(self.clone, "checkout", "--quiet", "main")
        for target, kwargs in (
            ("our_ark_github.workflow.time.sleep", {}),
            ("our_ark_github.workflow.shutil.which", {"return_value": GH}),
            ("our_ark_github.workflow._remote_for_repository", {"return_value": "origin"}),
            (
                "our_ark_github.workflow._publish_author",
                {"return_value": _PublishAuthor("Enoch", "enoch@example.invalid")},
            ),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.git_calls: list[list[str]] = []
        self.put_status = (422, "Unprocessable Entity")
        run = patch("our_ark_github.workflow.subprocess.run", side_effect=self._run)
        run.start()
        self.addCleanup(run.stop)
        inspect = patch(
            "our_ark_github.workflow.inspect_pull_request_merge",
            side_effect=lambda reference, root=None: _status(head_sha=self.feature),
        )
        inspect.start()
        self.addCleanup(inspect.stop)

    def _advance_main(self, path: str, text: str) -> str:
        _git(self.clone, "checkout", "--quiet", "main")
        (self.clone / path).write_text(text)
        _git(self.clone, "commit", "--quiet", "-am", f"main changes {path}")
        _git(self.clone, "push", "--quiet", "origin", "main")
        return _git(self.clone, "rev-parse", "HEAD")

    def _remote_head(self) -> str:
        return _git(self.remote, "rev-parse", "refs/heads/feature")

    def _run(self, command, **kwargs):
        if command[0] != GH:
            self.git_calls.append(list(command))
            return _REAL_RUN(command, **kwargs)
        args = command[1:]
        if "--method" in args:
            return _http(*self.put_status, "", stderr=EMPTY_BODY_STDERR)
        path = args[-1]
        if path == "repos/our-ark/enoch/pulls/12":
            return _ok(
                {
                    "head": {
                        "sha": self._remote_head(),
                        "ref": "feature",
                        "repo": {"full_name": "our-ark/enoch"},
                    }
                }
            )
        if path.startswith("repos/our-ark/enoch/commits/"):
            sha = path.rsplit("/", 1)[1]
            parents = _git(self.remote, "rev-list", "--parents", "-n", "1", sha).split()[1:]
            return _ok({"sha": sha, "parents": [{"sha": parent} for parent in parents]})
        raise AssertionError(f"unexpected gh call: {command}")

    def _worktrees(self) -> list[str]:
        listed = _git(self.clone, "worktree", "list", "--porcelain")
        return [line for line in listed.splitlines() if line.startswith("worktree ")]

    def test_merges_the_base_locally_and_pushes_without_force(self) -> None:
        main = self._advance_main("b.txt", "main\n")

        result = update_pull_request_branch(
            "12", expected_head_sha=self.feature, root=self.clone
        )

        pushed = self._remote_head()
        self.assertEqual(result.head_sha, pushed)
        self.assertEqual(result.previous_head_sha, self.feature)
        self.assertIn("with a local merge", result.message)
        self.assertIn("HTTP 422 Unprocessable Entity with an empty response", result.message)
        parents = _git(self.remote, "rev-list", "--parents", "-n", "1", pushed).split()[1:]
        self.assertEqual(parents, [self.feature, main])
        self.assertEqual(
            _git(self.remote, "log", "-1", "--format=%s|%an|%ae", pushed),
            "Merge branch 'main' into feature|Enoch|enoch@example.invalid",
        )
        pushes = [call for call in self.git_calls if "push" in call]
        self.assertEqual(len(pushes), 1)
        self.assertFalse(
            any(arg.startswith(("-f", "--force", "+")) for arg in pushes[0][2:]), pushes[0]
        )
        self.assertEqual(len(self._worktrees()), 1, "the temporary worktree was removed")
        self.assertEqual(_git(self.clone, "status", "--porcelain"), "")

    def test_stops_on_conflict_and_pushes_nothing(self) -> None:
        self._advance_main("a.txt", "main\n")

        with self.assertRaisesRegex(
            PublishError,
            r"the local merge fallback failed: merging main conflicts; nothing was pushed\.",
        ):
            update_pull_request_branch("12", expected_head_sha=self.feature, root=self.clone)

        self.assertEqual(self._remote_head(), self.feature)
        self.assertFalse([call for call in self.git_calls if "push" in call])
        self.assertEqual(len(self._worktrees()), 1, "the temporary worktree was removed")

    def test_a_branch_that_moved_is_never_overwritten(self) -> None:
        self._advance_main("b.txt", "main\n")
        moved = self.feature
        # The PR still looks unchanged to GitHub's API, but the branch moves
        # right before the push: the plain push must be rejected.
        original = self._run

        def run(command, **kwargs):
            if command[0] == "git" and "push" in command:
                _git(self.clone, "checkout", "--quiet", "feature")
                (self.clone / "c.txt").write_text("racing push\n")
                _git(self.clone, "add", "c.txt")
                _git(self.clone, "commit", "--quiet", "-m", "racing push")
                _git(self.clone, "push", "--quiet", "origin", "feature")
                nonlocal moved
                moved = _git(self.clone, "rev-parse", "HEAD")
                _git(self.clone, "checkout", "--quiet", "main")
            return original(command, **kwargs)

        with patch("our_ark_github.workflow.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(PublishError, "could not push feature|rejected"):
                update_pull_request_branch(
                    "12", expected_head_sha=self.feature, root=self.clone
                )

        self.assertEqual(self._remote_head(), moved)
        self.assertNotEqual(moved, self.feature)

    def test_remote_is_found_by_its_github_repository(self) -> None:
        with patch("our_ark_github.workflow._remote_for_repository", _remote_for_repository):
            _git(self.clone, "remote", "set-url", "origin", "https://github.com/someone/else.git")
            _git(self.clone, "remote", "add", "upstream", "git@github.com:our-ark/enoch.git")
            self.assertEqual(_remote_for_repository("Our-Ark/Enoch", self.clone), "upstream")
            with self.assertRaisesRegex(PublishError, "no git remote points at our-ark/other"):
                _remote_for_repository("our-ark/other", self.clone)


def _git(cwd: Path, *args: str) -> str:
    result = _REAL_RUN(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _http(
    status: int, phrase: str, body: str = "", *, stderr: str = ""
) -> MagicMock:
    """What ``gh api --include`` prints: status line, headers, blank line, body."""

    headers = (
        f"HTTP/2.0 {status} {phrase}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "\r\n"
    )
    return _process(returncode=0 if status < 400 else 1, stdout=headers + body, stderr=stderr)


def _ok(payload: dict[str, object]) -> MagicMock:
    return _process(stdout=json.dumps(payload))


def _process(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def _status(**overrides) -> PullRequestMergeStatus:
    status = PullRequestMergeStatus(
        reference="12",
        url="https://github.com/our-ark/enoch/pull/12",
        state="OPEN",
        base_branch="main",
        merge_commit="",
        merged_at="",
        number=12,
        repository="our-ark/enoch",
        mergeable="MERGEABLE",
        merge_state_status="BEHIND",
        head_sha="abc123",
    )
    return replace(status, **overrides)


if __name__ == "__main__":
    unittest.main()
