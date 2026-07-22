from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.immune import diagnose_output, run_immune_system


def _check(
    name: str,
    *,
    passed: bool = True,
    output: str = "",
    category: str = "code health",
):
    from enoch.immune import DoctorCheckResult

    return DoctorCheckResult(
        name=name,
        passed=passed,
        command=name,
        output=output,
        category=category,
        summary="ok" if passed else "failed",
    )


class EnochImmuneTests(unittest.TestCase):
    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_runs_default_test_command(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        memory_storage_check.return_value = _check("memory storage", category="operational readiness")
        run.return_value.returncode = 0
        run.return_value.stdout = "OK"
        run.return_value.stderr = ""

        result = run_immune_system(ROOT)

        self.assertTrue(result.passed)
        self.assertIn("-m unittest discover -s tests", result.command)
        self.assertIn("import enoch.cli", result.command)
        self.assertIn("import enoch.providers", result.command)
        self.assertIn("import enoch.lineage", result.command)
        self.assertIn("import enoch.learn", result.command)
        self.assertIn("import enoch.skills", result.command)
        self.assertIn("import enoch.app.core", result.command)
        self.assertIn("OK", result.output)
        self.assertEqual(
            [check.name for check in result.checks],
            ["python runtime", "tests", "import smoke", "codex binary", "git worktree", "memory storage"],
        )
        self.assertEqual(
            [check.category for check in result.checks],
            [
                "code health",
                "code health",
                "code health",
                "operational readiness",
                "operational readiness",
                "operational readiness",
            ],
        )
        self.assertEqual(result.diagnosis.summary, "All configured health checks passed.")

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_empty_configured_test_command_fails_without_crashing(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        memory_storage_check.return_value = _check("memory storage", category="operational readiness")
        run.return_value.returncode = 0
        run.return_value.stdout = "OK"
        run.return_value.stderr = ""

        with patch.dict("os.environ", {"ENOCH_TEST_COMMAND": ""}):
            result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        self.assertEqual(result.checks[1].name, "tests")
        self.assertEqual(result.checks[1].command, "<empty command>")
        self.assertIn("Doctor check command is empty", result.output)

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_timeout_is_reported_as_failed_check(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        memory_storage_check.return_value = _check("memory storage", category="operational readiness")
        passed = MagicMock(returncode=0, stdout="OK", stderr="")
        run.side_effect = [
            passed,
            subprocess.TimeoutExpired(["python3", "-m", "unittest"], timeout=1, output="partial"),
            passed,
            passed,
        ]

        with patch.dict("os.environ", {"ENOCH_DOCTOR_TIMEOUT_SECONDS": "1"}):
            result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        self.assertIn("Timed out after 1 second(s).", result.output)
        self.assertFalse(result.checks[1].passed)

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.load_provider")
    @patch("enoch.immune.subprocess.run")
    def test_dirty_worktree_is_reported_without_failing_doctor(
        self,
        run: MagicMock,
        load_provider: MagicMock,
        codex_binary_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        memory_storage_check.return_value = _check("memory storage", category="operational readiness")
        clean = MagicMock(returncode=0, stdout="OK", stderr="")
        run.side_effect = [clean, clean, clean]
        load_provider.return_value.is_clean.return_value = False

        result = run_immune_system(ROOT)

        self.assertTrue(result.passed)
        self.assertEqual(result.checks[4].name, "git worktree")
        self.assertEqual(result.checks[4].summary, "worktree dirty")

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_operational_failure_gets_specific_diagnosis(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check(
            "codex binary",
            passed=False,
            output="Codex binary was not found.",
            category="operational readiness",
        )
        memory_storage_check.return_value = _check("memory storage", category="operational readiness")
        run.return_value.returncode = 0
        run.return_value.stdout = "OK"
        run.return_value.stderr = ""

        result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        self.assertEqual(result.diagnosis.summary, "1 doctor check(s) failed: codex binary.")
        self.assertIn("failed doctor checks", result.diagnosis.suggested_action)

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_python_runtime_must_satisfy_project_minimum(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        memory_storage_check.return_value = _check("memory storage", category="operational readiness")
        old_python = MagicMock(returncode=0, stdout="Python 3.9.6\n", stderr="")
        passed = MagicMock(returncode=0, stdout="OK", stderr="")
        run.side_effect = [old_python, passed, passed, passed]

        result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        self.assertEqual(result.checks[0].name, "python runtime")
        self.assertIn("below required >= 3.11", result.checks[0].summary)
        self.assertEqual(result.diagnosis.summary, "1 doctor check(s) failed: python runtime.")

    def test_diagnoses_unittest_failures(self) -> None:
        output = """
FAIL: test_help_output (tests.test_enoch_cli.EnochCliTests.test_help_output)
Traceback (most recent call last):
  File "/repo/tests/test_enoch_cli.py", line 42, in test_help_output
    self.assertTrue(False)
AssertionError: False is not true

FAILED (failures=1)
"""

        diagnosis = diagnose_output(output, passed=False)

        self.assertEqual(diagnosis.summary, "1 test(s) failed.")
        self.assertEqual(
            diagnosis.failing_tests,
            ["tests.test_enoch_cli.EnochCliTests.test_help_output"],
        )
        self.assertIn("/repo/tests/test_enoch_cli.py", diagnosis.likely_files)
        self.assertIn("repair pass", diagnosis.suggested_action)

    def test_diagnoses_pytest_failures(self) -> None:
        output = "FAILED tests/test_enoch_cli.py::EnochCliTests::test_help - AssertionError"

        diagnosis = diagnose_output(output, passed=False)

        self.assertEqual(diagnosis.failing_tests, ["tests/test_enoch_cli.py::EnochCliTests::test_help"])
        self.assertIn("tests/test_enoch_cli.py", diagnosis.likely_files)

    def test_diagnoses_missing_imports(self) -> None:
        output = "ModuleNotFoundError: No module named 'enoch.telegram_client'"

        diagnosis = diagnose_output(output, passed=False)

        self.assertEqual(diagnosis.summary, "Import smoke failed: missing module enoch.telegram_client.")
        self.assertIn("stale module path", diagnosis.suggested_action)

    def test_diagnoses_stale_import_names(self) -> None:
        output = "ImportError: cannot import name 'missing_symbol' from 'enoch.example'"

        diagnosis = diagnose_output(output, passed=False)

        self.assertEqual(
            diagnosis.summary,
            "Import smoke failed: cannot import missing_symbol from enoch.example.",
        )
        self.assertIn("broken import", diagnosis.suggested_action)


if __name__ == "__main__":
    unittest.main()
