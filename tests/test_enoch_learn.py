import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.learn import (
    LearnError,
    learn_command,
    learn_skill_prompt,
    load_published_skill,
    parse_learn_request,
)


class EnochLearnTests(unittest.TestCase):
    def test_parse_learn_skill_from_agent(self) -> None:
        request = parse_learn_request("/learn teach from lucy")

        self.assertIsNotNone(request)
        self.assertEqual(request.skill, "teach")
        self.assertEqual(request.agent, "lucy")

    def test_prompt_tells_enoch_to_adapt_published_skill(self) -> None:
        with patch("enoch.learn._github_text", side_effect=_github_text):
            prompt = learn_skill_prompt("/learn teach from lucy", root=ROOT)

        self.assertIn("published skill teach from Lucy", prompt)
        self.assertIn("Do not copy the source agent blindly", prompt)
        self.assertIn("github.com/our-ark/lucy@main", prompt)
        self.assertIn("Package useful improvements", prompt)
        self.assertIn("SKILL.md:", prompt)

    def test_command_reports_published_skill_summary(self) -> None:
        with patch("enoch.learn._github_text", side_effect=_github_text):
            output = learn_command("learn teach from lucy", ROOT, prefix="")

        self.assertIn("Enoch inspected Lucy's teach skill.", output)
        self.assertIn("github.com/our-ark/lucy@main", output)
        self.assertIn("SKILL.md:", output)

    def test_command_reports_usage_for_old_lesson_path_shape(self) -> None:
        output = learn_command("learn .enoch/lessons/demo", ROOT, prefix="")

        self.assertEqual(output, "Use learn <skill> from <agent>.")

    def test_refuses_missing_skill(self) -> None:
        with patch("enoch.learn._github_text", side_effect=_github_text):
            with self.assertRaisesRegex(LearnError, "does not declare skill work"):
                load_published_skill("work", "lucy")


def _github_text(agent: str, path: str) -> str:
    if agent != "lucy":
        raise AssertionError(agent)
    if path == "src/lucy/identity.yaml":
        return "\n".join(
            [
                "name: Lucy",
                "skills:",
                "  - name: teach",
                "    description: Package useful improvements.",
                "    path: src/lucy/skills/teach",
            ]
        )
    if path == "src/lucy/skills/teach/skill.yaml":
        return "summary: Package useful improvements.\n"
    if path == "src/lucy/skills/teach/SKILL.md":
        return "# Teach\n\nPackage useful improvements for descendants.\n"
    raise AssertionError(path)


if __name__ == "__main__":
    unittest.main()
