from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.task_worktree import (
    prepare_existing_branch_worktree,
    prepare_task_worktree,
    remove_task_worktree,
)


class TaskWorktreeTests(unittest.TestCase):
    def test_task_worktree_isolated_from_dirty_resident_checkout(self) -> None:
        with TemporaryDirectory() as temp:
            source, resident = _create_agent_worktree(Path(temp))
            (resident / "README.md").write_text("resident draft\n", encoding="utf-8")

            task = prepare_task_worktree(
                resident,
                7,
                "update task behavior",
                start_point="main",
                resident_branch="agent/enoch-gary",
                created_at="2026-07-18T20:14:54+00:00",
            )

            self.assertTrue(task.path.is_dir())
            self.assertNotEqual(task.path, resident)
            self.assertEqual(_git(task.path, "branch", "--show-current"), task.branch)
            self.assertEqual((task.path / "README.md").read_text(encoding="utf-8"), "initial\n")
            self.assertIn("README.md", _git(resident, "status", "--porcelain"))
            self.assertEqual(_git(task.path, "status", "--porcelain"), "")

            reused = prepare_task_worktree(
                resident,
                7,
                "update task behavior",
                start_point="main",
                resident_branch="agent/enoch-gary",
                created_at="2026-07-18T20:14:54+00:00",
                existing_path=str(task.path),
                existing_branch=task.branch,
            )

            self.assertFalse(reused.created)
            self.assertEqual(reused.path, task.path)
            cleanup = remove_task_worktree(resident, reused)
            self.assertIn("Removed task #7 worktree.", cleanup)
            self.assertFalse(task.path.exists())
            self.assertEqual(_git(source, "branch", "--list", task.branch), "")

    def test_existing_branch_publish_worktree_does_not_switch_resident(self) -> None:
        with TemporaryDirectory() as temp:
            source, resident = _create_agent_worktree(Path(temp))
            _git(source, "branch", "enoch/existing", "main")

            task = prepare_existing_branch_worktree(
                resident,
                8,
                "enoch/existing",
            )

            self.assertEqual(_git(task.path, "branch", "--show-current"), "enoch/existing")
            self.assertEqual(
                _git(resident, "branch", "--show-current"),
                "agent/enoch-gary",
            )

            remove_task_worktree(
                resident,
                task,
                delete_local_branch=False,
            )

            self.assertFalse(task.path.exists())
            self.assertEqual(
                _git(source, "branch", "--list", "enoch/existing"),
                "enoch/existing",
            )


def _create_agent_worktree(base: Path) -> tuple[Path, Path]:
    source = base / "source"
    resident = base / "instance"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "Enoch Test")
    _git(source, "config", "user.email", "enoch@example.com")
    (source / "README.md").write_text("initial\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    _git(source, "worktree", "add", "-b", "agent/enoch-gary", str(resident), "main")
    return source, resident


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
