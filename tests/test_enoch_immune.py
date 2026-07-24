from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.formatting import doctor_output_excerpt
from enoch.immune import (
    MAX_OUTPUT_CHARS,
    _build_backend_check,
    _codex_binary_check,
    _forge_provider_check,
    _limit_output,
    _memory_storage_check,
    diagnose_output,
    run_immune_system,
)
from enoch.providers.registry import ProviderError


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
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune._build_backend_check")
    @patch("enoch.immune.load_provider")
    @patch("enoch.immune.subprocess.run")
    def test_missing_build_backend_skips_tests_without_masking_failure(
        self,
        run: MagicMock,
        load_provider: MagicMock,
        build_backend_check: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "OK"
        run.return_value.stderr = ""
        load_provider.return_value.is_clean.return_value = True
        build_backend_check.return_value = _check(
            "build backend",
            passed=False,
            category="environment readiness",
        )
        codex_binary_check.return_value = _check(
            "codex runtime",
            category="operational readiness",
        )
        forge_provider_check.return_value = _check(
            "github forge",
            category="operational readiness",
        )
        memory_storage_check.return_value = _check(
            "state storage",
            category="operational readiness",
        )

        result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        tests = next(check for check in result.checks if check.name == "tests")
        self.assertTrue(tests.skipped)
        self.assertTrue(tests.passed)
        self.assertFalse(
            any(
                "-m unittest discover" in " ".join(call.args[0])
                for call in run.call_args_list
            )
        )

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._vcs_workspace_check")
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._runtime_provider_check")
    @patch("enoch.immune._run_check")
    @patch("enoch.immune._python_runtime_check")
    def test_task_doctor_uses_resident_root_for_operational_state(
        self,
        python_runtime_check: MagicMock,
        run_check: MagicMock,
        runtime_provider_check: MagicMock,
        forge_provider_check: MagicMock,
        vcs_workspace_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        passed = _check("passed")
        for mocked_check in (
            python_runtime_check,
            run_check,
            runtime_provider_check,
            forge_provider_check,
            vcs_workspace_check,
            memory_storage_check,
        ):
            mocked_check.return_value = passed
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            worktree = base / "task-worktree"
            resident = base / "resident"
            worktree.mkdir()
            resident.mkdir()
            with patch.dict("os.environ", {"ENOCH_TEST_COMMAND": "true"}):
                result = run_immune_system(worktree, state_root=resident)

        self.assertTrue(result.passed)
        runtime_provider_check.assert_called_once_with(resident.resolve(), 120)
        forge_provider_check.assert_called_once_with(resident.resolve())
        memory_storage_check.assert_called_once_with(resident.resolve())
        vcs_workspace_check.assert_called_once_with(worktree.resolve(), 120)

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_runs_default_test_command(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        forge_provider_check.return_value = _check("github forge", category="operational readiness")
        memory_storage_check.return_value = _check("state storage", category="operational readiness")
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
            [
                "python runtime",
                "build backend",
                "tests",
                "import smoke",
                "codex binary",
                "github forge",
                "git worktree",
                "state storage",
            ],
        )
        self.assertEqual(
            [check.category for check in result.checks],
            [
                "code health",
                "environment readiness",
                "code health",
                "code health",
                "operational readiness",
                "operational readiness",
                "operational readiness",
                "operational readiness",
            ],
        )
        self.assertEqual(result.diagnosis.summary, "All configured health checks passed.")

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_empty_configured_test_command_fails_without_crashing(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        forge_provider_check.return_value = _check("github forge", category="operational readiness")
        memory_storage_check.return_value = _check("state storage", category="operational readiness")
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
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_timeout_is_reported_as_failed_check(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        forge_provider_check.return_value = _check("github forge", category="operational readiness")
        memory_storage_check.return_value = _check("state storage", category="operational readiness")
        passed = MagicMock(returncode=0, stdout="OK", stderr="")
        backend = MagicMock(returncode=0, stdout="setuptools 83.0.0", stderr="")
        run.side_effect = [
            passed,
            backend,
            subprocess.TimeoutExpired(["python3", "-m", "unittest"], timeout=1, output="partial"),
            passed,
            passed,
        ]

        with patch.dict("os.environ", {"ENOCH_DOCTOR_TIMEOUT_SECONDS": "1"}):
            result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        self.assertIn("Timed out after 1 second(s).", result.output)
        self.assertFalse(result.checks[2].passed)

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.load_provider")
    @patch("enoch.immune.subprocess.run")
    def test_dirty_worktree_is_reported_without_failing_doctor(
        self,
        run: MagicMock,
        load_provider: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        forge_provider_check.return_value = _check("github forge", category="operational readiness")
        memory_storage_check.return_value = _check("state storage", category="operational readiness")
        clean = MagicMock(returncode=0, stdout="OK", stderr="")
        backend = MagicMock(returncode=0, stdout="setuptools 83.0.0", stderr="")
        run.side_effect = [clean, backend, clean, clean]
        load_provider.return_value.is_clean.return_value = False

        result = run_immune_system(ROOT)

        self.assertTrue(result.passed)
        self.assertEqual(result.checks[6].name, "git worktree")
        self.assertEqual(result.checks[6].summary, "worktree dirty")

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_operational_failure_gets_specific_diagnosis(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check(
            "codex binary",
            passed=False,
            output="Codex binary was not found.",
            category="operational readiness",
        )
        forge_provider_check.return_value = _check("github forge", category="operational readiness")
        memory_storage_check.return_value = _check("state storage", category="operational readiness")
        run.return_value.returncode = 0
        run.return_value.stdout = "OK"
        run.return_value.stderr = ""

        result = run_immune_system(ROOT)

        self.assertFalse(result.passed)
        self.assertEqual(result.diagnosis.summary, "1 doctor check(s) failed: codex binary.")
        self.assertIn("failed doctor checks", result.diagnosis.suggested_action)

    @patch("enoch.immune._memory_storage_check")
    @patch("enoch.immune._forge_provider_check")
    @patch("enoch.immune._codex_binary_check")
    @patch("enoch.immune.subprocess.run")
    def test_python_runtime_must_satisfy_project_minimum(
        self,
        run: MagicMock,
        codex_binary_check: MagicMock,
        forge_provider_check: MagicMock,
        memory_storage_check: MagicMock,
    ) -> None:
        codex_binary_check.return_value = _check("codex binary", category="operational readiness")
        forge_provider_check.return_value = _check("github forge", category="operational readiness")
        memory_storage_check.return_value = _check("state storage", category="operational readiness")
        old_python = MagicMock(returncode=0, stdout="Python 3.9.6\n", stderr="")
        passed = MagicMock(returncode=0, stdout="OK", stderr="")
        backend = MagicMock(returncode=0, stdout="setuptools 83.0.0", stderr="")
        run.side_effect = [old_python, backend, passed, passed, passed]

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

    @patch("enoch.immune.subprocess.run")
    def test_build_backend_failure_reports_locked_install_command(
        self,
        run: MagicMock,
    ) -> None:
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = (
            "pip._vendor.pyproject_hooks._impl.BackendUnavailable: "
            "Cannot import 'setuptools.build_meta'"
        )

        check = _build_backend_check(ROOT, 1)

        self.assertFalse(check.passed)
        self.assertEqual(check.category, "environment readiness")
        self.assertIn("missing setuptools.build_meta", check.summary)
        self.assertIn("--require-hashes -r .github/requirements/test-build.txt", check.output)

    @patch("enoch.immune.subprocess.run")
    def test_build_backend_rejects_version_below_project_requirement(
        self,
        run: MagicMock,
    ) -> None:
        run.return_value.returncode = 0
        run.return_value.stdout = "setuptools 76.0.0"
        run.return_value.stderr = ""

        check = _build_backend_check(ROOT, 1)

        self.assertFalse(check.passed)
        self.assertIn("requires >= 77", check.summary)
        self.assertIn("requires at least 77", check.output)

    def test_environment_failure_outranks_resulting_test_failure(self) -> None:
        output = """
Doctor test environment is missing build backend setuptools.build_meta.
FAIL: test_wheel (tests.test_portable.PortableTests.test_wheel)
"""

        diagnosis = diagnose_output(output, passed=False)

        self.assertEqual(
            diagnosis.summary,
            "Test environment unavailable: missing build backend setuptools.build_meta.",
        )
        self.assertEqual(
            diagnosis.failing_tests,
            ["tests.test_portable.PortableTests.test_wheel"],
        )

    def test_long_doctor_output_preserves_beginning_and_root_cause(self) -> None:
        output = "beginning\n" + ("x" * MAX_OUTPUT_CHARS) + "\nROOT CAUSE"

        limited = _limit_output(output)
        excerpt = doctor_output_excerpt(limited)

        self.assertLessEqual(len(limited), MAX_OUTPUT_CHARS)
        self.assertIn("beginning", limited)
        self.assertIn("ROOT CAUSE", limited)
        self.assertIn("ROOT CAUSE", excerpt)

    def test_state_storage_detects_and_preserves_corrupt_json(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_file = root / ".enoch" / "task_queue.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text('{"pending": [', encoding="utf-8")

            check = _memory_storage_check(root)

            self.assertFalse(check.passed)
            self.assertEqual(check.name, "state storage")
            self.assertIn("invalid JSON", check.output)
            self.assertEqual(state_file.read_text(encoding="utf-8"), '{"pending": [')

    @patch("enoch.brain.resolve_codex_executable")
    @patch("enoch.immune.subprocess.run")
    def test_codex_runtime_check_verifies_login(
        self,
        run: MagicMock,
        resolve_codex: MagicMock,
    ) -> None:
        resolve_codex.return_value = SimpleNamespace(
            path="/Applications/Codex.app/codex",
            source="configured",
            detail="",
        )
        run.return_value.returncode = 0
        run.return_value.stdout = "Logged in using ChatGPT"
        run.return_value.stderr = ""

        check = _codex_binary_check(ROOT, timeout=30)

        self.assertTrue(check.passed)
        self.assertIn("authenticated", check.summary)
        self.assertEqual(
            run.call_args.args[0],
            ["/Applications/Codex.app/codex", "login", "status"],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    @patch("enoch.brain.resolve_codex_executable")
    @patch("enoch.immune.subprocess.run")
    def test_codex_runtime_check_reports_expired_login(
        self,
        run: MagicMock,
        resolve_codex: MagicMock,
    ) -> None:
        resolve_codex.return_value = SimpleNamespace(
            path="/Applications/Codex.app/codex",
            source="configured",
            detail="",
        )
        run.return_value.returncode = 1
        run.return_value.stdout = ""
        run.return_value.stderr = "Not logged in."

        check = _codex_binary_check(ROOT)

        self.assertFalse(check.passed)
        self.assertIn("not authenticated", check.summary)
        self.assertIn("codex login", check.output)

    @patch("enoch.immune.provider_name", side_effect=ProviderError("bad provider config"))
    def test_forge_provider_configuration_failure_does_not_crash_doctor(
        self,
        _provider_name: MagicMock,
    ) -> None:
        check = _forge_provider_check(ROOT)

        self.assertFalse(check.passed)
        self.assertEqual(check.name, "forge provider")
        self.assertIn("bad provider config", check.output)


if __name__ == "__main__":
    unittest.main()
