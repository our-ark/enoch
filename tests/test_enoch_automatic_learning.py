from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.automatic_learning import learning_artifact_path, learning_index_path, record_learning_artifact
from enoch.identity import load_identity


class EnochAutomaticLearningTests(unittest.TestCase):
    def test_records_skill_learning_artifact_index_and_markdown(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            artifact = record_learning_artifact(
                load_identity(),
                request="add a cron skill",
                result="\n".join(
                    [
                        "Enoch opened a pull request.",
                        "PR URL: https://github.com/our-ark/enoch/pull/42",
                        "Files:",
                        "- src/enoch/skills/cron/SKILL.md",
                        "- src/enoch/skills/cron/skill.yaml",
                    ]
                ),
                root=root,
                task_id=7,
                command="/task",
                context_source="chat-snapshot",
            )
            assert artifact is not None
            index = learning_index_path(root).read_text(encoding="utf-8").splitlines()
            markdown = learning_artifact_path(artifact.id, root).read_text(encoding="utf-8")

        payload = json.loads(index[0])
        self.assertEqual(payload["artifact_type"], "skill")
        self.assertEqual(payload["request"], "add a cron skill")
        self.assertEqual(payload["task_id"], 7)
        self.assertEqual(payload["pr_urls"], ["https://github.com/our-ark/enoch/pull/42"])
        self.assertEqual(payload["changed_files"], ["src/enoch/skills/cron/SKILL.md", "src/enoch/skills/cron/skill.yaml"])
        self.assertEqual(payload["skill_names"], ["cron"])
        self.assertIn("## Inheritance Notes", markdown)
        self.assertIn("Skills: cron", markdown)
        self.assertIn("src/enoch/skills/cron/SKILL.md", markdown)

    def test_skips_non_skill_changes(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)

            artifact = record_learning_artifact(
                load_identity(),
                request="fix a bug",
                result="\n".join(["Files:", "- src/enoch/telegram/bot.py"]),
                root=root,
                task_id=3,
                command="/task",
            )

        self.assertIsNone(artifact)
        self.assertFalse(learning_index_path(root).exists())


if __name__ == "__main__":
    unittest.main()
