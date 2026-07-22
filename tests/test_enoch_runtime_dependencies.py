from pathlib import Path
import os
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.runtime_dependencies import (
    RuntimeDependencyError,
    load_runtime_dependencies,
    runtime_dependency_paths,
)


PINNED_REQUIREMENT = (
    "example-lib @ git+https://github.com/example/example.git@"
    "0123456789abcdef0123456789abcdef01234567#subdirectory=library"
)


class EnochRuntimeDependencyTests(unittest.TestCase):
    def test_uses_local_source_without_installing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            local = root / "libraries" / "example" / "src"
            local.mkdir(parents=True)
            _write_manifest(root, local_source="libraries/example/src")

            paths = runtime_dependency_paths(root)

        self.assertEqual(paths, (local.resolve(),))

    def test_installs_missing_dependency_once_into_private_state(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_manifest(root)

            def install(_requirements, target):
                (target / "example_lib").mkdir()

            with patch(
                "enoch.runtime_dependencies._pip_install",
                side_effect=install,
            ) as pip:
                first = runtime_dependency_paths(root)
                second = runtime_dependency_paths(root)

            self.assertEqual(first, second)
            self.assertTrue((first[0] / ".complete").is_file())
            self.assertEqual(pip.call_count, 1)
            self.assertTrue(
                str(first[0]).startswith(str((root / ".enoch" / "dependencies").resolve()))
            )

    def test_uses_explicit_preloaded_dependency_during_birth_validation(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            preloaded = root / "birth-dependencies"
            (preloaded / "example_lib").mkdir(parents=True)
            (preloaded / "example_lib" / "__init__.py").write_text(
                "VALUE = 'preloaded'\n",
                encoding="utf-8",
            )
            _write_manifest(root)

            with patch.dict(
                os.environ,
                {"OUR_ARK_RUNTIME_DEPENDENCY_PATHS": str(preloaded)},
            ), patch("enoch.runtime_dependencies._pip_install") as pip:
                paths = runtime_dependency_paths(root)

            self.assertEqual(paths, (preloaded.resolve(),))
            pip.assert_not_called()

    def test_skips_missing_optional_dependency_without_installing(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_manifest(root, optional=True)

            with patch("enoch.runtime_dependencies._pip_install") as pip:
                paths = runtime_dependency_paths(root)

            self.assertEqual(paths, ())
            pip.assert_not_called()

    def test_replaces_incomplete_private_install(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            _write_manifest(root)

            def install(_requirements, temporary):
                fingerprint = temporary.name.removeprefix(".").split(".tmp-", 1)[0]
                incomplete = temporary.parent / fingerprint
                incomplete.mkdir()
                (incomplete / "partial.txt").write_text("partial", encoding="utf-8")
                (temporary / "example_lib").mkdir()

            with patch(
                "enoch.runtime_dependencies._pip_install",
                side_effect=install,
            ):
                paths = runtime_dependency_paths(root)

            self.assertTrue((paths[0] / ".complete").is_file())
            self.assertFalse((paths[0] / "partial.txt").exists())

    def test_rejects_unpinned_vcs_dependency(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "genesis.toml").write_text(
                "\n".join(
                    [
                        "[[runtime_dependencies]]",
                        'name = "example"',
                        'requirement = "example @ git+https://github.com/example/example.git@main"',
                        'import_name = "example_lib"',
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeDependencyError, "full VCS commit"):
                load_runtime_dependencies(root)

    def test_rejects_wildcard_package_version(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "genesis.toml").write_text(
                "\n".join(
                    [
                        "[[runtime_dependencies]]",
                        'name = "example"',
                        'requirement = "example==1.*"',
                        'import_name = "example_lib"',
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeDependencyError, "exact version"):
                load_runtime_dependencies(root)


def _write_manifest(
    root: Path,
    *,
    local_source: str = "",
    optional: bool = False,
) -> None:
    lines = [
        "[[runtime_dependencies]]",
        'name = "example"',
        f'requirement = "{PINNED_REQUIREMENT}"',
        'import_name = "example_lib"',
    ]
    if local_source:
        lines.append(f'local_source = "{local_source}"')
    if optional:
        lines.append("optional = true")
    (root / "genesis.toml").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
