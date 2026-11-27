"""The library re-pin PR Enoch opens after a merge that changed libraries/.

Real temporary Git repositories stand in for the authoritative remote (a bare
``origin``) and the resident checkout; the review provider's publish, list and
close operations are faked.
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from enoch.app.auto_update import LandedReview
from enoch.app.core import EnochApplication, TaskContextSnapshot
from enoch.app.library_repin import (
    CURRENT,
    NOT_APPLICABLE,
    REPIN,
    clear_library_repin_pending,
    load_library_repin_state,
    mark_library_repin_pending,
    plan_library_repin,
    remote_branch_exists,
    repin_workspace,
)
from enoch.app.pending_merges import load_pending_merges
from enoch.identity import load_identity
from enoch.library_pins import read_pins
from enoch.tasks.config import save_pr_auto_merge
from our_ark_provider_kit import (
    RepositoryRevision,
    ReviewCheck,
    ReviewIdentity,
    ReviewRecord,
    ReviewVersion,
)
from tests.test_enoch_application import _Chat, _Runtime
from tests.test_enoch_library_pins import (
    commit_all,
    git,
    pinned_repository,
    write_library,
    write_pin_files,
)
from tests.test_enoch_pending_merges import START, _Forge, _review


REPOSITORY = "acme/agent"
PIN_URL = f"https://github.com/{REPOSITORY}.git"
REPIN_URL = f"https://github.com/{REPOSITORY}/pull/120"
OLD_REPIN_URL = f"https://github.com/{REPOSITORY}/pull/110"
LANDED_URL = f"https://github.com/{REPOSITORY}/pull/91"


class _Repositories:
    """A bare origin, an author clone that pushes to main, and the resident clone."""

    def __init__(self, base: Path, *, pin_url: str = PIN_URL) -> None:
        self.origin = base / "origin.git"
        self.author = base / "author"
        self.resident = base / "resident"
        self.author.mkdir()
        pinned_repository(self.author, pin_url)
        git(self.author, "branch", "-M", "main")
        git(base, "init", "-q", "--bare", str(self.origin))
        git(self.author, "remote", "add", "origin", str(self.origin))
        git(self.author, "push", "-q", "origin", "main")
        git(base, "clone", "-q", str(self.origin), str(self.resident))
        git(self.resident, "config", "user.name", "Resident")
        git(self.resident, "config", "user.email", "resident@example.invalid")

    def merge(self, change) -> str:
        """Land a change on origin/main, as a merged PR would."""

        change(self.author)
        revision = commit_all(self.author, "Merged change")
        git(self.author, "push", "-q", "origin", "main")
        return revision

    def plan(self, repository: str = REPOSITORY):
        return plan_library_repin(
            self.resident, remote="origin", base_branch="main", repository=repository
        )


def _change_library(root: Path) -> None:
    write_library(root, "chat", "VERSION = 'chat-2'\n")


def _change_body(root: Path) -> None:
    (root / "README.md").write_text("body only\n", encoding="utf-8")


class RepinPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repos = _Repositories(Path(temporary.name))

    def test_no_repin_when_main_changed_no_library(self) -> None:
        self.repos.merge(_change_body)

        plan = self.repos.plan()

        self.assertEqual(plan.action, CURRENT)

    def test_library_change_on_main_needs_one_repin_to_that_commit(self) -> None:
        merged = self.repos.merge(_change_library)

        plan = self.repos.plan()

        self.assertEqual((plan.action, plan.revision, plan.libraries), (REPIN, merged, ("chat",)))
        self.assertEqual(plan.branch, f"enoch/repin-libraries-{merged[:12]}")
        self.assertEqual(plan.title, f"Re-pin libraries to {merged[:12]}")

    def test_workspace_holds_one_pin_only_commit_and_is_removed(self) -> None:
        merged = self.repos.merge(_change_library)
        plan = self.repos.plan()

        with repin_workspace(self.repos.resident, plan) as workspace:
            self.assertEqual(git(workspace, "rev-parse", "HEAD^"), merged)
            self.assertEqual(git(workspace, "branch", "--show-current"), plan.branch)
            self.assertEqual(
                git(workspace, "diff", "--name-only", "HEAD^", "HEAD").splitlines(),
                ["genesis.toml", "pyproject.toml"],
            )
            self.assertEqual({pin.revision for pin in read_pins(workspace, "HEAD")}, {merged})
            self.assertEqual(git(workspace, "log", "-1", "--format=%s"), plan.title)

        self.assertFalse(workspace.exists())
        self.assertEqual(git(self.repos.resident, "branch", "--list", plan.branch), "")

    def test_landing_the_repin_does_not_ask_for_another(self) -> None:
        merged = self.repos.merge(_change_library)
        self.assertEqual(self.repos.plan().action, REPIN)

        # The re-pin PR lands: only pin lines change.
        self.repos.merge(lambda root: write_pin_files(root, merged, PIN_URL))

        self.assertEqual(self.repos.plan().action, CURRENT)

    def test_descendants_without_libraries_never_repin(self) -> None:
        def drop_libraries(root: Path) -> None:
            git(root, "rm", "-q", "-r", "libraries")

        self.repos.merge(drop_libraries)

        plan = self.repos.plan()

        self.assertEqual(plan.action, NOT_APPLICABLE)
        self.assertIn("not hosted", plan.reason)

    def test_pins_naming_another_repository_never_repin(self) -> None:
        self.repos.merge(_change_library)

        self.assertEqual(self.repos.plan("someone/fork").action, NOT_APPLICABLE)
        self.assertEqual(self.repos.plan("").action, NOT_APPLICABLE)

    def test_remote_branch_detection(self) -> None:
        self.assertTrue(remote_branch_exists(self.repos.resident, "origin", "main"))
        self.assertFalse(remote_branch_exists(self.repos.resident, "origin", "enoch/none"))


class RepinStateTests(unittest.TestCase):
    def test_merges_share_one_pending_check(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            mark_library_repin_pending("#91", "2026-11-26T10:00:00+00:00", root)
            mark_library_repin_pending("#92", "2026-11-26T10:05:00+00:00", root)
            mark_library_repin_pending("#91", "2026-11-26T10:06:00+00:00", root)

            state = load_library_repin_state(root)
            self.assertEqual(state.pending, ("#91", "#92"))
            self.assertEqual(state.requested_at, "2026-11-26T10:00:00+00:00")

            clear_library_repin_pending(("#91",), root)
            self.assertEqual(load_library_repin_state(root).pending, ("#92",))
            clear_library_repin_pending(None, root)
            self.assertEqual(load_library_repin_state(root), load_library_repin_state(root))
            self.assertEqual(load_library_repin_state(root).pending, ())


class ApplicationRepinTests(unittest.TestCase):
    def setUp(self) -> None:
        worker = patch.object(EnochApplication, "_maybe_start_task_worker")
        worker.start()
        self.addCleanup(worker.stop)
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repos = _Repositories(Path(temporary.name))
        self.root = self.repos.resident
        self.chat = _Chat()
        self.forge = _Forge(merge_state="UNSTABLE", checks=(ReviewCheck("CI / test", "pending"),))
        self.forge.set(url=REPIN_URL, reference="120", number=120, head_sha="repin-head")
        self.published: list[dict] = []
        self.closed: list[str] = []
        self.open_reviews: list[ReviewRecord] = []

    def app(self) -> EnochApplication:
        app = EnochApplication(
            load_identity(),
            self.root,
            self.chat,
            runtime=_Runtime(),
            review=_review(self.forge),
        )
        app.pending_merges._now = lambda: START
        app.pending_merges.interval_seconds = 0
        app._authoritative_repository = lambda: REPOSITORY
        app._authoritative_branch_name = lambda: "main"
        app._authoritative_remote_name = lambda: "origin"
        app._resolve_task_context_snapshot = MagicMock(
            return_value=TaskContextSnapshot(context="", source="chat-snapshot")
        )
        app._compact_task_messages = lambda: True
        app.review.publish_review = self._publish
        app.review.list_open_reviews = lambda root=None, limit=20: tuple(self.open_reviews)
        app.review.close_review = self._close
        return app

    def _publish(self, request, root=None) -> ReviewRecord:
        workspace = Path(root)
        branch = git(workspace, "branch", "--show-current")
        git(workspace, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        self.published.append(
            {
                "title": request.title,
                "body": request.body,
                "branch": branch,
                "revision": request.revision.id,
                "files": git(workspace, "diff", "--name-only", "HEAD^", "HEAD").splitlines(),
                "pins": {pin.revision for pin in read_pins(workspace, "HEAD")},
                "base": request.metadata.get("base_name"),
            }
        )
        return _record(REPIN_URL, request.title)

    def _close(self, request, root=None) -> ReviewRecord:
        self.closed.append(request.review.url)
        return _record(request.review.url, "", state="closed")

    def texts(self) -> list[str]:
        return [text for _chat, text in self.chat.sent]

    def land(self, app: EnochApplication) -> None:
        app._after_requested_merge(LandedReview("#91", LANDED_URL, REPOSITORY, "main"))

    def test_landed_library_merge_opens_one_repin_pr(self) -> None:
        merged = self.repos.merge(_change_library)
        app = self.app()

        self.land(app)
        self.assertEqual(load_library_repin_state(self.root).pending, ("#91",))
        app._maybe_run_library_repin()

        (published,) = self.published
        self.assertEqual(published["title"], f"Re-pin libraries to {merged[:12]}")
        self.assertEqual(published["files"], ["genesis.toml", "pyproject.toml"])
        self.assertEqual(published["pins"], {merged})
        self.assertEqual(published["base"], "main")
        self.assertIn("chat", published["body"])
        self.assertTrue(remote_branch_exists(self.root, "origin", published["branch"]))
        self.assertEqual(load_library_repin_state(self.root).pending, ())
        (text,) = self.texts()
        self.assertIn("opened PR #120 to re-pin every library", text)
        self.assertIn("/pr merge 120", text)
        # Auto-merge is off: nothing is registered for merging.
        self.assertEqual(load_pending_merges(self.root), ())

    def test_auto_merge_registers_the_repin_pr(self) -> None:
        self.repos.merge(_change_library)
        save_pr_auto_merge(True, self.root)
        app = self.app()

        self.land(app)
        app._maybe_run_library_repin()

        (entry,) = load_pending_merges(self.root)
        self.assertEqual((entry.number, entry.task_id, entry.chat_id), (120, 0, "room-1"))
        self.assertEqual(entry.authorized_head_sha, "repin-head")
        self.assertIn("I'll merge it automatically", self.texts()[0])
        # A self-opened review never holds the task queue.
        self.assertIsNone(app._land_hold())

    def test_merge_without_library_changes_opens_nothing(self) -> None:
        self.repos.merge(_change_body)
        app = self.app()

        self.land(app)
        app._maybe_run_library_repin()

        self.assertEqual(self.published, [])
        self.assertEqual(self.texts(), [])
        self.assertEqual(load_library_repin_state(self.root).pending, ())

    def test_landing_the_repin_pr_opens_no_other(self) -> None:
        merged = self.repos.merge(_change_library)
        app = self.app()
        self.land(app)
        app._maybe_run_library_repin()
        self.assertEqual(len(self.published), 1)

        self.repos.merge(lambda root: write_pin_files(root, merged, PIN_URL))
        app._after_requested_merge(LandedReview("#120", REPIN_URL, REPOSITORY, "main"))
        app._maybe_run_library_repin()

        self.assertEqual(len(self.published), 1)
        self.assertEqual(load_library_repin_state(self.root).pending, ())

    def test_existing_repin_branch_is_not_published_twice(self) -> None:
        self.repos.merge(_change_library)
        app = self.app()
        self.land(app)
        app._maybe_run_library_repin()
        self.land(app)
        app._maybe_run_library_repin()

        self.assertEqual(len(self.published), 1)

    def test_newer_repin_closes_older_open_ones(self) -> None:
        self.repos.merge(_change_library)
        self.open_reviews = [
            _record(OLD_REPIN_URL, "Re-pin libraries to 0123456789ab"),
            _record(f"https://github.com/{REPOSITORY}/pull/111", "Unrelated work"),
            _record(REPIN_URL, "Re-pin libraries to new"),
        ]
        app = self.app()

        self.land(app)
        app._maybe_run_library_repin()

        self.assertEqual(self.closed, [OLD_REPIN_URL])

    def test_merges_elsewhere_do_not_mark_a_repin(self) -> None:
        app = self.app()

        app._after_requested_merge(LandedReview("#91", LANDED_URL, REPOSITORY, "release/v1"))
        app._after_requested_merge(LandedReview("#7", "https://github.com/x/y/pull/7", "x/y", "main"))
        app._after_requested_merge()

        self.assertEqual(load_library_repin_state(self.root).pending, ())

    def test_publish_failure_alerts_once_and_clears(self) -> None:
        self.repos.merge(_change_library)
        app = self.app()

        def refuse(request, root=None):
            raise RuntimeError("forge offline")

        app.review.publish_review = refuse
        self.land(app)
        app._maybe_run_library_repin()
        app._maybe_run_library_repin()

        (text,) = self.texts()
        self.assertIn("could not open the library re-pin PR: forge offline", text)
        self.assertEqual(load_library_repin_state(self.root).pending, ())
        # The temporary worktree and its local branch are gone.
        self.assertEqual(len(git(self.root, "worktree", "list").splitlines()), 1)

    def test_tick_runs_the_repin_before_the_automatic_update(self) -> None:
        app = self.app()
        order: list[str] = []
        app._maybe_run_library_repin = MagicMock(side_effect=lambda: order.append("repin"))
        app._maybe_run_auto_update = MagicMock(side_effect=lambda: order.append("update"))

        app.run_once()

        self.assertEqual(order, ["repin", "update"])


def _record(url: str, title: str, *, state: str = "open") -> ReviewRecord:
    revision = RepositoryRevision(id="repin-head")
    return ReviewRecord(
        identity=ReviewIdentity(id=url, url=url),
        title=title,
        body="",
        state=state,
        versions=(ReviewVersion(id=revision.id, revision=revision),),
    )


if __name__ == "__main__":
    unittest.main()
