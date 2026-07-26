from pathlib import Path
import os
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.validation_environment import (
    VALIDATION_ENVIRONMENT_HOME,
    ValidationEnvironmentError,
    ensure_validation_environment,
    existing_validation_environment,
)


class EnochValidationEnvironmentTests(unittest.TestCase):
    def test_provisions_locked_environment_once_and_reuses_it(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            environment_home = root / "managed"
            commands: list[list[str]] = []

            def run(command, **_kwargs):
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    _write_fake_venv_python(Path(command[3]))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(
                os.environ,
                {VALIDATION_ENVIRONMENT_HOME: str(environment_home)},
            ), patch(
                "enoch.validation_environment.subprocess.run",
                side_effect=run,
            ):
                first = ensure_validation_environment(
                    root,
                    base_python=sys.executable,
                )
                second = ensure_validation_environment(
                    root,
                    base_python=sys.executable,
                )
                discovered = existing_validation_environment(
                    root,
                    base_python=sys.executable,
                )
                marker_exists = (first.root / ".complete.json").is_file()

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.root, second.root)
        self.assertEqual(discovered, second)
        self.assertTrue(marker_exists)
        install_commands = [
            command
            for command in commands
            if len(command) > 3 and command[1:4] == ["-m", "pip", "install"]
        ]
        self.assertEqual(len(install_commands), 1)
        self.assertIn("--require-hashes", install_commands[0])
        requirements_argument = Path(
            install_commands[0][install_commands[0].index("-r") + 1]
        )
        self.assertEqual(
            requirements_argument.resolve(),
            (root / ".github" / "requirements" / "test-build.txt").resolve(),
        )

    def test_lock_change_creates_a_new_content_addressed_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            locked = _write_project(root)
            environment_home = root / "managed"

            def run(command, **_kwargs):
                if command[1:3] == ["-m", "venv"]:
                    _write_fake_venv_python(Path(command[3]))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(
                os.environ,
                {VALIDATION_ENVIRONMENT_HOME: str(environment_home)},
            ), patch(
                "enoch.validation_environment.subprocess.run",
                side_effect=run,
            ):
                first = ensure_validation_environment(
                    root,
                    base_python=sys.executable,
                )
                locked.write_text(
                    "setuptools==84.0.0 --hash=sha256:changed\n",
                    encoding="utf-8",
                )
                second = ensure_validation_environment(
                    root,
                    base_python=sys.executable,
                )
                first_exists = first.root.is_dir()
                second_exists = second.root.is_dir()

        self.assertNotEqual(first.fingerprint, second.fingerprint)
        self.assertNotEqual(first.root, second.root)
        self.assertTrue(first_exists)
        self.assertTrue(second_exists)

    def test_failed_install_does_not_publish_partial_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root)
            environment_home = root / "managed"

            def run(command, **_kwargs):
                if command[1:3] == ["-m", "venv"]:
                    _write_fake_venv_python(Path(command[3]))
                    return subprocess.CompletedProcess(command, 0, "", "")
                if len(command) > 3 and command[1:4] == ["-m", "pip", "install"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        "download failed",
                    )
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(
                os.environ,
                {VALIDATION_ENVIRONMENT_HOME: str(environment_home)},
            ), patch(
                "enoch.validation_environment.subprocess.run",
                side_effect=run,
            ):
                with self.assertRaisesRegex(
                    ValidationEnvironmentError,
                    "download failed",
                ):
                    ensure_validation_environment(
                        root,
                        base_python=sys.executable,
                    )

            partials = list(environment_home.glob(".*.tmp-*"))
            completed = list(environment_home.glob("*/.complete.json"))

        self.assertEqual(partials, [])
        self.assertEqual(completed, [])

    def test_falls_back_to_pyproject_requirements_without_repository_lock(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_project(root, locked=False)
            environment_home = root / "managed"
            commands: list[list[str]] = []

            def run(command, **_kwargs):
                commands.append(command)
                if command[1:3] == ["-m", "venv"]:
                    _write_fake_venv_python(Path(command[3]))
                return subprocess.CompletedProcess(command, 0, "", "")

            with patch.dict(
                os.environ,
                {VALIDATION_ENVIRONMENT_HOME: str(environment_home)},
            ), patch(
                "enoch.validation_environment.subprocess.run",
                side_effect=run,
            ):
                ensure_validation_environment(
                    root,
                    base_python=sys.executable,
                )

        install = next(
            command
            for command in commands
            if len(command) > 3 and command[1:4] == ["-m", "pip", "install"]
        )
        self.assertNotIn("--require-hashes", install)
        self.assertIn("setuptools>=77", install)


def _write_project(root: Path, *, locked: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(
        "\n".join(
            [
                "[build-system]",
                'requires = ["setuptools>=77"]',
                'build-backend = "setuptools.build_meta"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    path = root / ".github" / "requirements" / "test-build.txt"
    if locked:
        path.parent.mkdir(parents=True)
        path.write_text(
            "setuptools==83.0.0 --hash=sha256:locked\n",
            encoding="utf-8",
        )
    return path


def _write_fake_venv_python(root: Path) -> None:
    directory = root / ("Scripts" if os.name == "nt" else "bin")
    directory.mkdir(parents=True)
    executable = directory / ("python.exe" if os.name == "nt" else "python")
    executable.write_text("", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
