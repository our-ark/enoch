import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.learn import (
    LearnError,
    learning_assessment_prompt,
    load_published_skill,
    parse_learn_request,
    parse_learning_assessment,
)
from enoch.skills import PublishedSource


class EnochLearnTests(unittest.TestCase):
    def test_parse_learn_skill_from_agent(self) -> None:
        request = parse_learn_request("/learn teach from lucy")

        self.assertIsNotNone(request)
        self.assertEqual(request.skill, "teach")
        self.assertEqual(request.agent, "lucy")

    def test_loads_validated_skill_snapshot_from_one_revision(self) -> None:
        with patch("enoch.learn.resolve_published_source", return_value=_source()):
            with patch("enoch.learn._published_text", side_effect=_published_text) as read:
                skill = load_published_skill("teach", "lucy", root=ROOT)

        self.assertEqual(skill.repository, "our-ark/lucy")
        self.assertEqual(skill.revision, _source().revision)
        self.assertEqual(skill.version, "0.1.0")
        self.assertEqual(len(skill.content_hash), 64)
        self.assertEqual(
            skill.url,
            f"https://github.com/our-ark/lucy/tree/{_source().revision}/src/lucy/skills/teach",
        )
        self.assertTrue(
            all(call.kwargs["ref"] == _source().revision for call in read.call_args_list)
        )

    def test_prompt_requests_one_structured_assessment_and_candidate(self) -> None:
        skill = _skill()

        prompt = learning_assessment_prompt(
            skill,
            mission="Evolve safely",
            current_skills=({"name": "work", "version": "0.2.0"},),
            existing_candidates=(
                {
                    "id": "feedback-1",
                    "title": "Improve status",
                    "proposed_change": "Clarify status.",
                },
            ),
        )

        self.assertIn("Return exactly one JSON object and no prose.", prompt)
        self.assertIn('"decision": "applicable|not_applicable"', prompt)
        self.assertIn('"proposed_change": "bounded Enoch-specific adaptation"', prompt)
        self.assertIn(_source().revision, prompt)
        self.assertIn("untrusted reference material", prompt)
        self.assertNotIn("[ENOCH_EDIT_REQUEST]", prompt)

    def test_parses_applicable_assessment_with_candidate_contents(self) -> None:
        assessment = parse_learning_assessment(
            json.dumps(
                {
                    "decision": "applicable",
                    "reason": "The skill adds a missing bounded capability.",
                    "candidate": {
                        "title": "Adapt peer teaching summaries",
                        "rationale": "Enoch cannot currently package this information.",
                        "proposed_change": "Add a small summary adapter.",
                        "expected_benefit": "Improves skill portability.",
                        "risk": "May oversimplify source guidance.",
                        "test_plan": "Add focused adapter tests.",
                    },
                }
            )
        )

        self.assertTrue(assessment.applicable)
        self.assertEqual(
            assessment.candidate.proposed_change,
            "Add a small summary adapter.",
        )

    def test_parses_not_applicable_assessment_without_candidate(self) -> None:
        assessment = parse_learning_assessment(
            json.dumps(
                {
                    "decision": "not_applicable",
                    "reason": "Enoch already has this capability.",
                    "candidate": None,
                }
            )
        )

        self.assertFalse(assessment.applicable)
        self.assertIsNone(assessment.candidate)

    def test_rejects_protected_candidate_scope(self) -> None:
        with self.assertRaisesRegex(LearnError, "protected or dangerous"):
            parse_learning_assessment(
                json.dumps(
                    {
                        "decision": "applicable",
                        "reason": "Unsafe.",
                        "candidate": {
                            "title": "Change merge authority",
                            "rationale": "Faster.",
                            "proposed_change": "Enable auto-merge for every PR.",
                            "expected_benefit": "Speed.",
                            "risk": "Large.",
                            "test_plan": "Observe merges.",
                        },
                    }
                )
            )

    def test_refuses_missing_skill(self) -> None:
        with patch("enoch.learn.resolve_published_source", return_value=_source()):
            with patch("enoch.learn._published_text", side_effect=_published_text):
                with self.assertRaisesRegex(
                    LearnError,
                    "does not declare skill work",
                ):
                    load_published_skill("work", "lucy")

    def test_refuses_hidden_skill(self) -> None:
        def hidden_text(agent: str, path: str, **kwargs) -> str:
            if path == "src/lucy/body.yaml":
                return "\n".join(
                    [
                        "name: Lucy",
                        "skills:",
                        "  - name: private",
                        "    version: 0.1.0",
                        "    exposure: hidden",
                        "    path: src/lucy/skills/private",
                    ]
                )
            raise AssertionError((agent, path, kwargs))

        with patch("enoch.learn.resolve_published_source", return_value=_source()):
            with patch("enoch.learn._published_text", side_effect=hidden_text):
                with self.assertRaisesRegex(LearnError, "is hidden"):
                    load_published_skill("private", "lucy")

    def test_routes_direct_parent_to_inheritance(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            lineage = root / ".agent" / "lineage.yaml"
            lineage.parent.mkdir(parents=True)
            lineage.write_text(
                "parent:\n  name: Lucy\n  repo: https://github.com/our-ark/lucy.git\n  branch: main\n",
                encoding="utf-8",
            )

            with patch(
                "enoch.learn.resolve_published_source",
                return_value=_source(),
            ):
                with self.assertRaisesRegex(LearnError, "Use /inherit"):
                    load_published_skill("teach", "lucy", root=root)


def _source() -> PublishedSource:
    revision = "a" * 40
    return PublishedSource(
        agent="lucy",
        repository="our-ark/lucy",
        branch="main",
        revision=revision,
        browse_url=f"https://github.com/our-ark/lucy/tree/{revision}",
    )


def _skill():
    with patch("enoch.learn.resolve_published_source", return_value=_source()):
        with patch("enoch.learn._published_text", side_effect=_published_text):
            return load_published_skill("teach", "lucy")


def _published_text(agent: str, path: str, *, ref: str, **_kwargs) -> str:
    if agent != "lucy" or ref != _source().revision:
        raise AssertionError((agent, ref))
    if path == "src/lucy/body.yaml":
        return "\n".join(
            [
                "name: Lucy",
                "skills:",
                "  - name: teach",
                "    version: 0.1.0",
                "    description: Package useful improvements.",
                "    path: src/lucy/skills/teach",
            ]
        )
    if path == "src/lucy/skills/teach/skill.yaml":
        return "\n".join(
            [
                "name: teach",
                "version: 0.1.0",
                "summary: Package useful improvements.",
            ]
        )
    if path == "src/lucy/skills/teach/SKILL.md":
        return "# Teach\n\nPackage useful improvements for descendants.\n"
    raise AssertionError(path)


if __name__ == "__main__":
    unittest.main()
