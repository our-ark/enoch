from dataclasses import replace
from pathlib import Path
import json
import os
import re
import subprocess
import sys
import tomllib
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.validation_environment import (
    VALIDATION_ENVIRONMENT_HOME,
    VALIDATION_TEST_REQUIREMENTS,
    ValidationEnvironmentError,
    ensure_validation_environment,
    existing_validation_environment,
    validation_environment_status,
)


class EnochValidationEnvironmentTests(unittest.TestCase):
    def test_project_venvs_keep_distinct_dependencies_and_managed_backend_precedence(self) -> None:
        from enoch.immune import _run_check, _test_command

        real_run = subprocess.run
        with TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "project"
            _write_project(project, locked=False)
            (project / "tests").mkdir()
            (project / "tests/test_dependencies.py").write_text(
                "import unittest, runtime_dependency, setuptools\n"
                "class Dependencies(unittest.TestCase):\n"
                "    def test_imports(self):\n"
                "        self.assertIn(runtime_dependency.VALUE, ('alpha', 'beta'))\n"
                "        self.assertEqual(setuptools.VALUE, 'managed')\n"
            )

            def site_packages(python):
                result = real_run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                  text=True, capture_output=True, check=True)
                return Path(result.stdout.strip())

            def fake_backend(path, value):
                package = path / "setuptools"
                package.mkdir(exist_ok=True)
                (package / "__init__.py").write_text(f"VALUE = {value!r}\n")
                (package / "build_meta.py").write_text("# Test build backend\n")

            def offline_run(command, **kwargs):
                if command[1:4] == ["-m", "pip", "install"]:
                    fake_backend(site_packages(command[0]), "managed")
                    return subprocess.CompletedProcess(command, 0, "", "")
                return real_run(command, **kwargs)

            environments = []
            for value in ("alpha", "beta"):
                runtime = base / value
                real_run([sys.executable, "-m", "venv", "--without-pip", str(runtime)], check=True)
                python = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                packages = site_packages(python)
                (packages / "runtime_dependency.py").write_text(f"VALUE = {value!r}\n")
                fake_backend(packages, "runtime")
                with patch.dict(os.environ, {VALIDATION_ENVIRONMENT_HOME: str(base / "managed")}), \
                     patch("enoch.validation_environment.subprocess.run", side_effect=offline_run):
                    managed = ensure_validation_environment(project, base_python=str(python))
                    self.assertEqual(existing_validation_environment(project, base_python=str(python)).root,
                                     managed.root)
                environments.append(managed)
                check = _run_check("tests", _test_command(str(managed.python), root=project), project, 30)
                self.assertTrue(check.passed, check.output)
                self.assertIn("Ran 1 test", check.output)
                probe = real_run([str(managed.python), "-c", "import runtime_dependency; print(runtime_dependency.VALUE)"],
                                 text=True, capture_output=True, check=True)
                self.assertEqual(probe.stdout.strip(), value)
                self.assertIn("'runtime'", (packages / "setuptools/__init__.py").read_text())
            self.assertNotEqual(environments[0].root, environments[1].root)

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
                return subprocess.CompletedProcess(command, 0, "[]" if "json.dumps(sys.path)" in command[-1] else "", "")

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
                return subprocess.CompletedProcess(command, 0, "[]" if "json.dumps(sys.path)" in command[-1] else "", "")

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
                    return subprocess.CompletedProcess(command, 0, "[]" if "json.dumps(sys.path)" in command[-1] else "", "")
                if len(command) > 3 and command[1:4] == ["-m", "pip", "install"]:
                    return subprocess.CompletedProcess(
                        command,
                        1,
                        "",
                        "download failed",
                    )
                return subprocess.CompletedProcess(command, 0, "[]" if "json.dumps(sys.path)" in command[-1] else "", "")

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
                return subprocess.CompletedProcess(command, 0, "[]" if "json.dumps(sys.path)" in command[-1] else "", "")

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


class EnochPersistentTestEnvironmentTests(unittest.TestCase):
    def test_installs_locked_test_tools_and_resident_libraries_in_editable_mode(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            worktree = base / "worktree"
            resident = base / "resident"
            for checkout in (worktree, resident):
                _write_project(checkout, test_tools=True, libraries=("beta", "alpha"))
            commands: list[list[str]] = []

            with patch.dict(os.environ, {VALIDATION_ENVIRONMENT_HOME: str(base / "managed")}), \
                 patch("enoch.validation_environment.subprocess.run",
                       side_effect=_recording_run(commands)):
                managed = ensure_validation_environment(
                    worktree, base_python=sys.executable, source_root=resident,
                )
                marker = json.loads((managed.root / ".complete.json").read_text())

        installs = [command for command in commands if command[1:4] == ["-m", "pip", "install"]]
        self.assertEqual(len(installs), 3)
        build, tools, editable = installs
        self.assertEqual(_requirements_argument(build), worktree.resolve() / ".github/requirements/test-build.txt")
        self.assertIn("--require-hashes", tools)
        self.assertEqual(_requirements_argument(tools), worktree.resolve() / VALIDATION_TEST_REQUIREMENTS)
        for flag in ("--no-index", "--no-deps", "--no-build-isolation"):
            self.assertIn(flag, editable)
        editable_paths = [
            editable[index + 1] for index, part in enumerate(editable) if part == "--editable"
        ]
        expected = [str(resident.resolve() / "libraries" / name) for name in ("alpha", "beta")]
        self.assertEqual(editable_paths, expected)
        self.assertEqual(marker["editable_sources"], expected)

    def test_worktrees_of_the_same_revision_share_one_environment(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            resident = base / "resident"
            first, second = base / "task-1", base / "task-2"
            for checkout in (resident, first, second):
                _write_project(checkout, test_tools=True, libraries=("alpha",))
            with patch.dict(os.environ, {VALIDATION_ENVIRONMENT_HOME: str(base / "managed")}), \
                 patch("enoch.validation_environment.subprocess.run", side_effect=_recording_run([])):
                built = ensure_validation_environment(
                    first, base_python=sys.executable, source_root=resident,
                )
                shared = existing_validation_environment(
                    second, base_python=sys.executable, source_root=resident,
                )

        self.assertEqual(shared, replace(built, created=False))

    def test_status_reports_missing_ready_and_stale_without_subprocesses(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            _write_project(root, test_tools=True, libraries=("alpha",))
            home = {VALIDATION_ENVIRONMENT_HOME: str(base / "managed")}

            def status():
                with patch(
                    "enoch.validation_environment.subprocess.run",
                    side_effect=AssertionError("status must not run subprocesses"),
                ):
                    return validation_environment_status(root, base_python=sys.executable)

            with patch.dict(os.environ, home):
                missing = status()
                with patch("enoch.validation_environment.subprocess.run",
                           side_effect=_recording_run([])):
                    built = ensure_validation_environment(root, base_python=sys.executable)
                ready = status()
                stale_inputs = {}
                for name, path in {
                    "test tools": root / VALIDATION_TEST_REQUIREMENTS,
                    "build lock": root / ".github/requirements/test-build.txt",
                    "root pyproject": root / "pyproject.toml",
                    "library pyproject": root / "libraries/alpha/pyproject.toml",
                }.items():
                    original = path.read_text()
                    path.write_text(original + "# changed\n")
                    stale_inputs[name] = status()
                    path.write_text(original)
                ready_again = status()

        self.assertEqual(missing.state, "missing")
        self.assertIsNone(missing.environment)
        self.assertFalse(missing.ready)
        self.assertEqual(ready.state, "ready")
        self.assertTrue(ready.ready)
        self.assertEqual(ready.environment.python, built.python)
        for name, result in stale_inputs.items():
            with self.subTest(changed=name):
                self.assertEqual(result.state, "stale")
                self.assertNotEqual(result.fingerprint, ready.fingerprint)
                self.assertIn("changed", result.detail)
        self.assertEqual(ready_again.state, "ready")

    def test_removed_editable_checkout_marks_environment_stale_and_is_repaired(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            _write_project(root, libraries=("alpha",))
            commands: list[list[str]] = []
            with patch.dict(os.environ, {VALIDATION_ENVIRONMENT_HOME: str(base / "managed")}), \
                 patch("enoch.validation_environment.subprocess.run",
                       side_effect=_recording_run(commands)):
                first = ensure_validation_environment(root, base_python=sys.executable)
                marker = first.root / ".complete.json"
                payload = json.loads(marker.read_text())
                payload["editable_sources"] = [str(base / "deleted-worktree/libraries/alpha")]
                marker.write_text(json.dumps(payload))
                stale = validation_environment_status(root, base_python=sys.executable)
                repaired = ensure_validation_environment(root, base_python=sys.executable)
                ready = validation_environment_status(root, base_python=sys.executable)

        self.assertEqual(stale.state, "stale")
        self.assertTrue(repaired.created)
        self.assertEqual(repaired.root, first.root)
        self.assertEqual(ready.state, "ready")
        venvs = [command for command in commands if command[1:3] == ["-m", "venv"]]
        self.assertEqual(len(venvs), 2)

    # The lock lives under .github/, which is repository tooling outside the
    # inheritable body (genesis.toml body_paths); descendants run without it
    # and the managed environment treats it as optional.
    @unittest.skipUnless(
        (ROOT / ".github").is_dir(),
        "repository test-tool locks are outside the inheritable agent body",
    )
    def test_repository_test_tools_lock_is_hashed_and_matches_runtime_pins(self) -> None:
        lock = (ROOT / VALIDATION_TEST_REQUIREMENTS).read_text(encoding="utf-8")
        pins = dict(re.findall(r"(?m)^([A-Za-z0-9_.-]+)==([^\s;\\]+)", lock))
        self.assertIn("pytest", pins)
        requirements = [line for line in lock.replace("\\\n", " ").splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
        for line in requirements:
            with self.subTest(requirement=line.split()[0]):
                self.assertIn("--hash=sha256:", line)
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        runtime_pins = dict(
            requirement.split("==", 1)
            for requirement in pyproject["project"]["dependencies"]
            if "==" in requirement and "@" not in requirement
        )
        for name, version in runtime_pins.items():
            with self.subTest(runtime_pin=name):
                self.assertEqual(pins.get(name), version)


def _recording_run(commands: list[list[str]]):
    def run(command, **_kwargs):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            _write_fake_venv_python(Path(command[3]))
        stdout = "[]" if "json.dumps(sys.path)" in command[-1] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    return run


def _requirements_argument(command: list[str]) -> Path:
    return Path(command[command.index("-r") + 1])


def _write_project(
    root: Path,
    *,
    locked: bool = True,
    test_tools: bool = False,
    libraries: tuple[str, ...] = (),
) -> Path:
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
    if test_tools:
        tools = root / VALIDATION_TEST_REQUIREMENTS
        tools.parent.mkdir(parents=True, exist_ok=True)
        tools.write_text("pytest==9.2.0 --hash=sha256:locked\n", encoding="utf-8")
    for name in libraries:
        library = root / "libraries" / name
        library.mkdir(parents=True)
        (library / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "1.0"\n',
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
