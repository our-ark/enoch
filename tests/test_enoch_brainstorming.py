import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.evolution.sources.brainstorming import (
    BrainstormError,
    parse_brainstorm_response,
    prepare_brainstorm_request,
)


class EnochBrainstormingTests(unittest.TestCase):
    def test_prepares_bounded_context_for_a_fresh_read_only_session(self) -> None:
        request = prepare_brainstorm_request(
            "auditable evolution",
            "Evolve safely",
            current_skills=(
                {
                    "name": "work",
                    "version": "0.2.0",
                    "summary": "Run isolated work.",
                },
            ),
            existing_candidates=(
                {
                    "id": "feedback-1",
                    "source": "feedback",
                    "status": "candidate",
                    "title": "Expose candidate provenance",
                    "proposed_change": "Show provenance.",
                },
            ),
            recent_completed_work=(
                {
                    "task_id": 17,
                    "completion_kind": "authoritative-body-change",
                    "request_summary": "Improve candidate reports.",
                    "changed_files": ["src/enoch/app/reporting.py"],
                },
            ),
        )

        self.assertEqual(request.theme, "auditable evolution")
        self.assertEqual(len(request.context_hash), 64)
        self.assertIn("read-only reasoning turn", request.prompt)
        self.assertIn("Brainstorming is not evidence", request.prompt)
        self.assertIn("Expose candidate provenance", request.prompt)
        self.assertIn("src/enoch/app/reporting.py", request.prompt)
        self.assertIn("Return []", request.prompt)

    def test_parses_complete_strict_candidate_drafts(self) -> None:
        drafts = parse_brainstorm_response(
            json.dumps(
                [
                    {
                        "title": "Expose candidate provenance",
                        "rationale": "Reviewers need an audit trail.",
                        "proposed_change": "Add provenance to the evolve report.",
                        "expected_benefit": "Improves auditability.",
                        "risk": "Adds report noise.",
                        "test_plan": "Add report formatting tests.",
                    }
                ]
            )
        )

        self.assertEqual(len(drafts), 1)
        self.assertEqual(
            drafts[0].proposed_change,
            "Add provenance to the evolve report.",
        )
        self.assertEqual(parse_brainstorm_response("[]"), ())

    def test_requires_theme_and_exact_json_schema(self) -> None:
        with self.assertRaisesRegex(BrainstormError, "theme"):
            prepare_brainstorm_request("", "Evolve safely")
        with self.assertRaisesRegex(BrainstormError, "malformed JSON"):
            parse_brainstorm_response("not json")
        with self.assertRaisesRegex(BrainstormError, "invalid schema"):
            parse_brainstorm_response(
                json.dumps(
                    [
                        {
                            "title": "Incomplete",
                            "proposed_change": "Add something.",
                            "test_plan": "Test it.",
                        }
                    ]
                )
            )

    def test_rejects_protected_and_duplicate_candidate_changes(self) -> None:
        protected = {
            "title": "Change merge authority",
            "rationale": "Faster changes.",
            "proposed_change": "Enable automatic merge authority.",
            "expected_benefit": "Speed.",
            "risk": "High.",
            "test_plan": "Run tests.",
        }
        with self.assertRaisesRegex(BrainstormError, "protected or dangerous"):
            parse_brainstorm_response(json.dumps([protected]))

        safe = {
            "title": "Improve report headings",
            "rationale": "Reports are hard to scan.",
            "proposed_change": "Add clearer report headings.",
            "expected_benefit": "Improves readability.",
            "risk": "Adds output.",
            "test_plan": "Add formatting tests.",
        }
        with self.assertRaisesRegex(BrainstormError, "duplicate"):
            parse_brainstorm_response(
                json.dumps(
                    [
                        safe,
                        {
                            **safe,
                            "title": "Clarify report headings",
                        },
                    ]
                )
            )


if __name__ == "__main__":
    unittest.main()
