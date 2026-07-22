from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.git_tools import GitError
from enoch.immune import DoctorDiagnosis
from enoch.updater import run_update_doctor, update_from_main


class EnochUpdaterTests(unittest.TestCase):
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
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "src" / "enoch"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "update_doctor.py").write_text(
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

    @patch("enoch.updater.run_update_doctor")
    @patch(
        "enoch.updater.pull_origin_main",
        return_value=(
            "Fast-forward\n"
            " src/enoch/updater.py | 2 +-\n"
            " 1 file changed, 1 insertion(+), 1 deletion(-)"
        ),
    )
    @patch("enoch.updater.current_head", side_effect=["1111111111111111111111111111111111111111", "2222222222222222222222222222222222222222"])
    @patch("enoch.updater.current_branch", return_value="main")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_pulls_runs_doctor_and_requests_restart(
        self,
        ensure_clean_worktree: MagicMock,
        fetch_origin_main: MagicMock,
        current_branch: MagicMock,
        _current_head: MagicMock,
        pull_origin_main: MagicMock,
        run_update_doctor: MagicMock,
    ) -> None:
        run_update_doctor.return_value = _doctor_result()

        result = update_from_main(ROOT)

        ensure_clean_worktree.assert_called_once_with(ROOT)
        fetch_origin_main.assert_called_once_with(ROOT)
        current_branch.assert_called_once_with(ROOT)
        pull_origin_main.assert_called_once_with(ROOT)
        run_update_doctor.assert_called_once_with(ROOT)
        self.assertTrue(result.restart_required)
        self.assertIn("Enoch pulled latest main and doctor passed.", result.message)
        self.assertIn("Restarting now.", result.message)
        self.assertNotIn("Fast-forward", result.message)
        self.assertNotIn("1 file changed", result.message)
        self.assertIn("Fast-forward", result.direct_action_result)
        self.assertIn("Restarting into 2222222.", result.direct_action_result)

    @patch("enoch.updater.run_update_doctor")
    @patch("enoch.updater.pull_origin_main", return_value="Already up to date.")
    @patch("enoch.updater.current_head", side_effect=["1111111111111111111111111111111111111111", "1111111111111111111111111111111111111111"])
    @patch("enoch.updater.current_branch", return_value="main")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_does_not_restart_when_already_up_to_date(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _current_head: MagicMock,
        _pull_origin_main: MagicMock,
        run_update_doctor: MagicMock,
    ) -> None:
        result = update_from_main(ROOT)

        run_update_doctor.assert_not_called()
        self.assertFalse(result.restart_required)
        self.assertEqual(result.message, "Enoch is already up to date.")
        self.assertEqual(result.direct_action_result, "Already up to date.")

    @patch(
        "enoch.updater.stage_promoted_evolve_adoptions",
        return_value=(MagicMock(),),
    )
    @patch(
        "enoch.updater.promotions_pending_adoption",
        return_value=(MagicMock(),),
    )
    @patch("enoch.updater.run_update_doctor")
    @patch("enoch.updater.pull_origin_main", return_value="Already up to date.")
    @patch(
        "enoch.updater.current_head",
        side_effect=[
            "1111111111111111111111111111111111111111",
            "1111111111111111111111111111111111111111",
        ],
    )
    @patch("enoch.updater.current_branch", return_value="main")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_verifies_pending_adoption_even_when_code_is_current(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _current_head: MagicMock,
        _pull_origin_main: MagicMock,
        run_update_doctor: MagicMock,
        _pending_adoption: MagicMock,
        stage_adoptions: MagicMock,
    ) -> None:
        run_update_doctor.return_value = _doctor_result()

        result = update_from_main(ROOT)

        run_update_doctor.assert_called_once_with(ROOT)
        stage_adoptions.assert_called_once_with(
            ROOT,
            "1111111111111111111111111111111111111111",
            health_check="passed",
        )
        self.assertTrue(result.restart_required)
        self.assertIn("adoption checks passed", result.message)
        self.assertIn("verified adoption after restart", result.message)

    @patch(
        "enoch.updater._load_channel_lifecycle_state",
        return_value={
            "status": "running",
            "pid": os.getpid(),
            "started_head": "0000000000000000000000000000000000000000",
        },
    )
    @patch("enoch.updater.run_update_doctor")
    @patch("enoch.updater.pull_origin_main", return_value="Already up to date.")
    @patch("enoch.updater.current_head", side_effect=["1111111111111111111111111111111111111111", "1111111111111111111111111111111111111111"])
    @patch("enoch.updater.current_branch", return_value="main")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_warns_when_running_commit_is_stale_but_disk_is_current(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _current_head: MagicMock,
        _pull_origin_main: MagicMock,
        run_update_doctor: MagicMock,
        _load_lifecycle_state: MagicMock,
    ) -> None:
        result = update_from_main(ROOT)

        run_update_doctor.assert_not_called()
        self.assertFalse(result.restart_required)
        self.assertIn("Enoch is already up to date.", result.message)
        self.assertIn("Telegram daemon started on 0000000", result.message)
        self.assertIn("Run /restart to load the current code.", result.message)
        self.assertIn("Run /restart to load the current code.", result.direct_action_result)

    @patch(
        "enoch.updater._load_channel_lifecycle_state",
        return_value={
            "status": "running",
            "pid": 1,
            "started_head": "0000000000000000000000000000000000000000",
        },
    )
    @patch("enoch.updater.pull_origin_main", return_value="Already up to date.")
    @patch("enoch.updater.current_head", side_effect=["1111111111111111111111111111111111111111", "1111111111111111111111111111111111111111"])
    @patch("enoch.updater.current_branch", return_value="main")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_ignores_lifecycle_for_other_process(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _current_head: MagicMock,
        _pull_origin_main: MagicMock,
        _load_lifecycle_state: MagicMock,
    ) -> None:
        result = update_from_main(ROOT)

        self.assertNotIn("Run /restart", result.message)
        self.assertEqual(result.direct_action_result, "Already up to date.")

    @patch("enoch.updater.run_update_doctor")
    @patch("enoch.updater.reset_hard")
    @patch("enoch.updater.pull_origin_main", return_value="Updating 1111111..2222222")
    @patch("enoch.updater.current_head", side_effect=["1111111111111111111111111111111111111111", "2222222222222222222222222222222222222222"])
    @patch("enoch.updater.current_branch", return_value="main")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_rolls_back_when_doctor_fails(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _current_head: MagicMock,
        _pull_origin_main: MagicMock,
        reset_hard: MagicMock,
        run_update_doctor: MagicMock,
    ) -> None:
        doctor = _doctor_result()
        doctor.passed = False
        doctor.diagnosis = DoctorDiagnosis(
            summary="1 test(s) failed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="Inspect failing tests.",
        )
        run_update_doctor.return_value = doctor

        result = update_from_main(ROOT)

        reset_hard.assert_called_once_with("1111111111111111111111111111111111111111", ROOT)
        self.assertFalse(result.restart_required)
        self.assertIn("doctor failed", result.message)
        self.assertIn("Rolled back to 1111111.", result.message)
        self.assertEqual(result.direct_action_result, "")

    @patch("enoch.updater.head_merged_into_origin_main", return_value=True)
    @patch("enoch.updater.pull_origin_main", return_value="Already up to date.")
    @patch("enoch.updater.current_head", side_effect=["1111111111111111111111111111111111111111", "1111111111111111111111111111111111111111"])
    @patch("enoch.updater.current_branch", return_value="enoch/old-merged-branch")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_fast_forwards_merged_branch_without_switching_to_main(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _current_head: MagicMock,
        pull_origin_main: MagicMock,
        head_merged: MagicMock,
    ) -> None:
        result = update_from_main(ROOT)

        head_merged.assert_called_once_with(ROOT)
        pull_origin_main.assert_called_once_with(ROOT)
        self.assertIn("Enoch is already up to date.", result.message)

    @patch("enoch.updater.head_merged_into_origin_main", return_value=False)
    @patch("enoch.updater.current_branch", return_value="enoch/unmerged-work")
    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree")
    def test_update_refuses_unmerged_feature_branch(
        self,
        _ensure_clean_worktree: MagicMock,
        _fetch_origin_main: MagicMock,
        _current_branch: MagicMock,
        _head_merged: MagicMock,
    ) -> None:
        result = update_from_main(ROOT)

        self.assertIn("has commits that are not merged into origin/main", result.message)

    @patch("enoch.updater.fetch_origin_main")
    @patch("enoch.updater.ensure_clean_worktree", side_effect=GitError("dirty"))
    def test_update_refuses_dirty_worktree(
        self,
        _ensure_clean_worktree: MagicMock,
        fetch_origin_main: MagicMock,
    ) -> None:
        result = update_from_main(ROOT)

        fetch_origin_main.assert_not_called()
        self.assertEqual(result.message, "Enoch could not update: dirty")


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


if __name__ == "__main__":
    unittest.main()
