import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.learn import (
    LearnError,
    explore_peer_skills,
    learn_command,
    learn_skill_prompt,
    load_peer_learning_observations,
    load_published_skill,
    parse_learn_request,
    record_peer_learning_observation,
)
from enoch.skills import AgentSkills, SkillInfo


class EnochLearnTests(unittest.TestCase):
    def test_parse_learn_skill_from_agent(self) -> None:
        request = parse_learn_request("/learn teach from lucy")

        self.assertIsNotNone(request)
        self.assertEqual(request.skill, "teach")
        self.assertEqual(request.agent, "lucy")

    def test_prompt_tells_enoch_to_adapt_published_skill(self) -> None:
        with patch("enoch.learn._published_text", side_effect=_published_text):
            prompt = learn_skill_prompt("/learn teach from lucy", root=ROOT)

        self.assertIn("published skill teach from Lucy", prompt)
        self.assertIn("Do not copy the source agent blindly", prompt)
        self.assertIn("our-ark/lucy@main", prompt)
        self.assertIn("Package useful improvements", prompt)
        self.assertIn("SKILL.md:", prompt)

    def test_command_reports_published_skill_summary(self) -> None:
        with patch("enoch.learn._published_text", side_effect=_published_text):
            output = learn_command("learn teach from lucy", ROOT, prefix="")

        self.assertIn("Enoch inspected Lucy's teach skill.", output)
        self.assertIn("our-ark/lucy@main", output)
        self.assertIn("SKILL.md:", output)

    def test_command_reports_usage_for_old_lesson_path_shape(self) -> None:
        output = learn_command("learn .enoch/lessons/demo", ROOT, prefix="")

        self.assertEqual(output, "Use learn <skill> from <agent>.")

    def test_refuses_missing_skill(self) -> None:
        with patch("enoch.learn._published_text", side_effect=_published_text):
            with self.assertRaisesRegex(LearnError, "does not declare skill work"):
                load_published_skill("work", "lucy")

    def test_records_peer_learning_as_a_distinct_source(self) -> None:
        request = parse_learn_request("/learn teach from lucy")
        assert request is not None
        with TemporaryDirectory() as temp:
            root = Path(temp)
            first = record_peer_learning_observation(request, root)
            record_peer_learning_observation(request, root)
            loaded = load_peer_learning_observations(root)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].id, first.id)
        self.assertEqual(loaded[0].agent, "lucy")
        self.assertEqual(loaded[0].skill, "teach")

    def test_explores_visible_skills_from_peer_agent(self) -> None:
        peer = AgentSkills(
            name="Enosh",
            root=Path("our-ark/enosh@main"),
            skills=(
                SkillInfo(name="research", path="src/enosh/skills/research"),
                SkillInfo(name="private", path="src/enosh/skills/private", exposure="hidden"),
            ),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            observations = explore_peer_skills(
                "enosh",
                root,
                loader=lambda _agent, root=None: peer,
            )

        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].skill, "research")
        self.assertEqual(observations[0].agent, "enosh")

    def test_peer_exploration_rejects_direct_parent(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            lineage = root / ".agent" / "lineage.yaml"
            lineage.parent.mkdir(parents=True)
            lineage.write_text(
                "parent:\n  name: Seth\n  repo: https://our-ark/seth\n  branch: main\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "inheritance"):
                explore_peer_skills("seth", root, loader=lambda _agent, root=None: None)


def _published_text(agent: str, path: str, **_kwargs) -> str:
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
