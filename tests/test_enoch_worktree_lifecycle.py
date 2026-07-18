from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.identity import load_identity
from enoch.instance import instance_branch
from enoch.telegram.bot import EnochTelegramBot


class _Client:
    class _Config:
        allowed_chat_id = None

    config = _Config()


class EnochWorktreeLifecycleTests(unittest.TestCase):
    def test_task_branch_uses_main_commit_without_checking_out_main(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "enoch"
            instance = Path(directory) / "enoch-gary"
            source.mkdir()
            _git(source, "init", "-b", "main")
            _git(source, "config", "user.name", "Enoch Test")
            _git(source, "config", "user.email", "enoch@example.com")
            (source / ".gitignore").write_text(".agent/instance.yaml\n.enoch/\n", encoding="utf-8")
            (source / "README.md").write_text("first\n", encoding="utf-8")
            _git(source, "add", ".")
            _git(source, "commit", "-m", "first")
            _git(source, "worktree", "add", "-b", "agent/enoch-gary", str(instance), "main")

            (source / "README.md").write_text("latest main\n", encoding="utf-8")
            _git(source, "add", "README.md")
            _git(source, "commit", "-m", "advance main")
            main_head = _git(source, "rev-parse", "main").stdout.strip()
            resident_head = _git(instance, "rev-parse", "agent/enoch-gary").stdout.strip()
            self.assertNotEqual(main_head, resident_head)

            metadata = instance / ".agent" / "instance.yaml"
            metadata.parent.mkdir(parents=True)
            metadata.write_text(
                "\n".join(
                    [
                        "schema_version: 1",
                        "worktree:",
                        f'  path: "{instance}"',
                        f'  source_repo: "{source}"',
                        '  branch: "agent/enoch-gary"',
                        "  kind: agent-instance",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            bot = EnochTelegramBot(load_identity(), instance, _Client())
            note = bot._ensure_action_branch("Update README")
            task_branch = _git(instance, "branch", "--show-current").stdout.strip()

            self.assertEqual(instance_branch(instance), "agent/enoch-gary")
            self.assertTrue(task_branch.startswith("enoch/"))
            self.assertEqual(_git(instance, "rev-parse", "HEAD").stdout.strip(), main_head)
            self.assertEqual(_git(source, "branch", "--show-current").stdout.strip(), "main")
            self.assertIn("Resident branch: agent/enoch-gary.", note)

            cleanup = bot._return_to_resident_and_delete_branch(task_branch)

            self.assertEqual(
                _git(instance, "branch", "--show-current").stdout.strip(),
                "agent/enoch-gary",
            )
            self.assertEqual(_git(source, "branch", "--show-current").stdout.strip(), "main")
            self.assertNotIn(task_branch, _git(instance, "branch", "--list", task_branch).stdout)
            self.assertIn("switched local checkout back to agent/enoch-gary", cleanup)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
    )


if __name__ == "__main__":
    unittest.main()
