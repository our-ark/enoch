from pathlib import Path
import os
import runpy
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.formatting import format_doctor_result
from enoch.immune import DoctorDiagnosis
from enoch.operations.updater import run_update_doctor, update_from_authoritative
from our_ark_provider_kit import (
    BranchlessRepositoryFixture,
    RepositoryRevision,
)


class EnochUpdaterTests(unittest.TestCase):
    @patch("enoch.operations.update_doctor.main")
    def test_legacy_update_doctor_module_delegates_after_package_move(
        self,
        updated_main: MagicMock,
    ) -> None:
        runpy.run_module("enoch.update_doctor", run_name="__main__")

        updated_main.assert_called_once_with()

    def test_post_update_doctor_loads_code_from_updated_worktree(self) -> None:
        payload = {
            "passed": True,
            "command": "updated doctor",
            "output": "updated code ran",
            "diagnosis": {
                "summary": "Updated doctor passed.",
                "failing_tests": [],
                "likely_files": [],
                "suggested_action": "Restart.",
            },
            "checks": [
                {
                    "name": "updated runtime",
                    "passed": True,
                    "command": "updated check",
                    "output": "",
                    "category": "operational readiness",
                    "summary": "loaded from disk",
                    "skipped": True,
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "enoch"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            operations = package / "operations"
            operations.mkdir()
            (operations / "__init__.py").write_text("", encoding="utf-8")
            (operations / "update_doctor.py").write_text(
                "\n".join(
                    [
                        "import json",
                        f"print(json.dumps({payload!r}, sort_keys=True))",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"ENOCH_PYTHON": sys.executable},
                clear=False,
            ):
                result = run_update_doctor(root)

        self.assertTrue(result.passed)
        self.assertEqual(result.command, "updated doctor")
        self.assertEqual(result.diagnosis.summary, "Updated doctor passed.")
        self.assertEqual(result.checks[0].summary, "loaded from disk")
        self.assertTrue(result.checks[0].skipped)
        self.assertIn(
            "- updated runtime: skipped (loaded from disk)",
            format_doctor_result(result),
        )

    @patch("enoch.operations.updater.run_update_doctor")
    def test_update_activates_authoritative_revision_and_requests_restart(
        self,
        run_update_doctor: MagicMock,
    ) -> None:
        repository = _repository_with_update()
        run_update_doctor.return_value = _doctor_result()

        result = update_from_authoritative(ROOT, repository=repository)

        run_update_doctor.assert_called_once_with(ROOT)
        self.assertEqual(repository.current.id, "r1")
        self.assertTrue(result.restart_required)
        self.assertIn(
            "Enoch updated to latest authoritative and doctor passed.",
            result.message,
        )
        self.assertIn("Restarting now.", result.message)
        self.assertIn("Updated repository from r0 to r1", result.direct_action_result)
        self.assertIn("Restarting into r1.", result.direct_action_result)
        self.assertEqual(result.previous_revision_id, "r0")
        self.assertEqual(result.revision_id, "r1")

    @patch("enoch.operations.updater.run_update_doctor")
    def test_update_does_not_restart_when_already_up_to_date(
        self,
        run_update_doctor: MagicMock,
    ) -> None:
        repository = BranchlessRepositoryFixture()
        result = update_from_authoritative(ROOT, repository=repository)

        run_update_doctor.assert_not_called()
        self.assertFalse(result.restart_required)
        self.assertEqual(result.message, "Enoch is already up to date.")
        self.assertEqual(
            result.direct_action_result,
            "Already at authoritative revision r0.",
        )

    @patch(
        "enoch.operations.updater._load_channel_lifecycle_state",
        return_value={
            "status": "running",
            "pid": os.getpid(),
            "started_head": "0000000000000000000000000000000000000000",
        },
    )
    @patch("enoch.operations.updater.run_update_doctor")
    def test_update_warns_when_running_commit_is_stale_but_disk_is_current(
        self,
        run_update_doctor: MagicMock,
        _load_lifecycle_state: MagicMock,
    ) -> None:
        repository = BranchlessRepositoryFixture()
        result = update_from_authoritative(ROOT, repository=repository)

        run_update_doctor.assert_not_called()
        self.assertFalse(result.restart_required)
        self.assertIn("Enoch is already up to date.", result.message)
        self.assertIn("daemon started on 0000000", result.message)
        self.assertIn("Run /restart to load the current code.", result.message)
        self.assertIn("Run /restart to load the current code.", result.direct_action_result)

    @patch(
        "enoch.operations.updater._load_channel_lifecycle_state",
        return_value={
            "status": "running",
            "pid": 1,
            "started_head": "0000000000000000000000000000000000000000",
        },
    )
    def test_update_ignores_lifecycle_for_other_process(
        self,
        _load_lifecycle_state: MagicMock,
    ) -> None:
        repository = BranchlessRepositoryFixture()
        result = update_from_authoritative(ROOT, repository=repository)

        self.assertNotIn("Run /restart", result.message)
        self.assertEqual(
            result.direct_action_result,
            "Already at authoritative revision r0.",
        )

    @patch("enoch.operations.updater.run_update_doctor")
    def test_update_rolls_back_when_doctor_fails(
        self,
        run_update_doctor: MagicMock,
    ) -> None:
        repository = _repository_with_update()
        doctor = _doctor_result()
        doctor.passed = False
        doctor.diagnosis = DoctorDiagnosis(
            summary="1 test(s) failed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="Inspect failing tests.",
        )
        run_update_doctor.return_value = doctor

        result = update_from_authoritative(ROOT, repository=repository)

        self.assertEqual(repository.current.id, "r0")
        self.assertFalse(result.restart_required)
        self.assertIn("doctor failed", result.message)
        self.assertIn("Rolled back to r0.", result.message)
        self.assertEqual(result.direct_action_result, "")

    def test_update_refuses_revision_outside_authoritative_history(self) -> None:
        repository = _repository_with_update()
        divergent = RepositoryRevision("side")
        repository.revisions[divergent.id] = divergent
        repository.current = divergent

        result = update_from_authoritative(ROOT, repository=repository)

        self.assertIn(
            "is not in the history of authoritative revision r1",
            result.message,
        )

    def test_update_refuses_dirty_working_copy(self) -> None:
        repository = BranchlessRepositoryFixture()
        repository.mark_changed("src/enoch/app/core.py")

        result = update_from_authoritative(ROOT, repository=repository)

        self.assertEqual(
            result.message,
            "Enoch could not update: working copy has uncommitted changes: "
            "src/enoch/app/core.py",
        )


def _doctor_result() -> MagicMock:
    return MagicMock(
        passed=True,
        command="python3 -m unittest discover -s tests",
        output="OK",
        diagnosis=DoctorDiagnosis(
            summary="All configured health checks passed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="No repair needed.",
        ),
    )


def _repository_with_update() -> BranchlessRepositoryFixture:
    repository = BranchlessRepositoryFixture()
    revision = RepositoryRevision("r1", display="authoritative update")
    repository.revisions[revision.id] = revision
    repository.parents[revision.id] = repository.current.id
    repository.authoritative = revision
    return repository


if __name__ == "__main__":
    unittest.main()
