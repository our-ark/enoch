from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolution.sources.brainstorming import generate_brainstorm_ideas, load_brainstorm_ideas


class EnochBrainstormingTests(unittest.TestCase):
    def test_generates_validated_theme_guided_ideas(self) -> None:
        response = """```json
[
  {
    "title": "Expose candidate provenance",
    "rationale": "Reviewers need an audit trail.",
    "proposed_change": "Add provenance to the evolve report.",
    "expected_benefit": "Improves auditability.",
    "risk": "Adds report noise.",
    "test_plan": "Add report formatting tests."
  }
]
```"""
        prompts: list[str] = []
        with TemporaryDirectory() as temp:
            root = Path(temp)

            ideas = generate_brainstorm_ideas(
                "auditable evolution",
                root,
                mission="Evolve safely",
                generator=lambda prompt: prompts.append(prompt) or response,
            )
            loaded = load_brainstorm_ideas(root, theme="auditable evolution")
            other_theme = load_brainstorm_ideas(root, theme="Telegram UX")

        self.assertEqual(len(ideas), 1)
        self.assertEqual(loaded, ideas)
        self.assertEqual(other_theme, ())
        self.assertIn("Current evolution theme: auditable evolution", prompts[0])
        self.assertIn("Mission: Evolve safely", prompts[0])

    def test_requires_theme_and_valid_structured_output(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, "theme"):
                generate_brainstorm_ideas("", root, mission="test", generator=lambda _prompt: "[]")
            with self.assertRaisesRegex(ValueError, "valid bounded"):
                generate_brainstorm_ideas("testing", root, mission="test", generator=lambda _prompt: "not json")

    def test_rejects_ideas_that_target_protected_scope(self) -> None:
        response = """[
          {
            "title": "Change merge authority",
            "rationale": "Faster changes.",
            "proposed_change": "Enable automatic merge authority.",
            "expected_benefit": "Speed.",
            "risk": "High.",
            "test_plan": "Run tests."
          }
        ]"""
        with TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "valid bounded"):
                generate_brainstorm_ideas(
                    "speed",
                    Path(temp),
                    mission="Evolve safely",
                    generator=lambda _prompt: response,
                )


if __name__ == "__main__":
    unittest.main()
