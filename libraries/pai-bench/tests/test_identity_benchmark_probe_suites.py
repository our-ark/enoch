from __future__ import annotations

from copy import deepcopy
import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from identity_benchmark.probe_suites import (
    ProbeSuiteError,
    compile_benchmark_profile,
    parse_identity_profile,
    parse_probe_bindings,
    parse_probe_suite,
)


class IdentityBenchmarkProbeSuiteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.identity = {
            "schema_version": 1,
            "profile_id": "identity-a",
            "statements": [{"id": "designation", "content": "I am ORBIT-A."}],
        }
        self.suite = {
            "schema_version": 1,
            "suite_id": "shared-suite-v1",
            "variables": ["scenario"],
            "probes": [
                {
                    "id": "designation",
                    "dimension": "recognition",
                    "messages": [
                        {
                            "role": "user",
                            "content": "Identify yourself while considering {{scenario}}.",
                        }
                    ],
                    "tags": ["identity-fact"],
                }
            ],
        }
        self.bindings = {
            "schema_version": 1,
            "suite_id": "shared-suite-v1",
            "profile_id": "identity-a",
            "variables": {"scenario": "a routine status check"},
            "oracles": {
                "designation": {
                    "expectations": [
                        {"type": "contains", "value": "ORBIT-A", "gate": True}
                    ]
                }
            },
        }

    def test_compiler_combines_identity_shared_questions_and_private_oracle(self) -> None:
        profile = compile_benchmark_profile(
            parse_identity_profile(self.identity),
            parse_probe_suite(self.suite),
            parse_probe_bindings(self.bindings),
        )

        self.assertEqual(profile.profile_id, "identity-a")
        self.assertEqual(len(profile.probes), 1)
        self.assertEqual(
            profile.probes[0].messages[0].content,
            "Identify yourself while considering a routine status check.",
        )
        self.assertEqual(profile.probes[0].expectations[0].value, "ORBIT-A")

    def test_suite_rejects_an_undeclared_message_variable(self) -> None:
        suite = deepcopy(self.suite)
        suite["variables"] = []

        with self.assertRaisesRegex(ProbeSuiteError, "undeclared variables"):
            parse_probe_suite(suite)

    def test_compiler_rejects_bindings_for_a_different_identity(self) -> None:
        bindings = deepcopy(self.bindings)
        bindings["profile_id"] = "identity-b"

        with self.assertRaisesRegex(ProbeSuiteError, "does not match identity"):
            compile_benchmark_profile(
                parse_identity_profile(self.identity),
                parse_probe_suite(self.suite),
                parse_probe_bindings(bindings),
            )


if __name__ == "__main__":
    unittest.main()
