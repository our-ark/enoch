"""The single library pin layer: parsing, rewriting, CI override, ancestry.

Fixtures are temporary Git repositories with their own ``libraries/`` and pin
files, so these tests also run in descendants without the real libraries.
"""

from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from enoch.library_pins import (  # noqa: E402
    CI_COMMIT_MESSAGE,
    CI_SOURCE_BRANCH,
    LibraryPin,
    LibraryPinError,
    ancestor_problems,
    apply_ci_override,
    find_pins,
    installed_pin_problems,
    main,
    pin_status,
    read_pins,
    repository_slug,
    rewrite_pins,
)


PIN_URL = "https://example.invalid/acme/agent.git"
LIBRARIES = ("kit", "chat")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false",
            "-c", "init.defaultBranch=main",
            "-C", str(root),
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def requirement(library: str, revision: str, url: str = PIN_URL) -> str:
    return f"acme-{library} @ git+{url}@{revision}#subdirectory=libraries/{library}"


def write_pin_files(root: Path, revision: str, url: str = PIN_URL) -> None:
    genesis = ['schema_version = 1', 'package = "agent"', ""]
    genesis += [
        "[[runtime_dependencies]]",
        'name = "pypdf"',
        'requirement = "pypdf==6.21.4"',
        'import_name = "pypdf"',
        "",
    ]
    for library in LIBRARIES:
        genesis += [
            "[[runtime_dependencies]]",
            f'name = "{library}"',
            f'requirement = "{requirement(library, revision, url)}"',
            f'import_name = "acme_{library}"',
            f'local_source = "libraries/{library}/src"',
            "",
        ]
    (root / "genesis.toml").write_text("\n".join(genesis), encoding="utf-8")
    dependencies = ",\n".join(f'  "{requirement(library, revision, url)}"' for library in LIBRARIES)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "agent"\ndependencies = [\n  "pypdf==6.21.4",\n{dependencies},\n]\n',
        encoding="utf-8",
    )


def write_library(root: Path, library: str, text: str) -> None:
    path = root / "libraries" / library / "src" / f"acme_{library}" / "__init__.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(root: Path, message: str) -> str:
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


def pinned_repository(root: Path, url: str = PIN_URL) -> str:
    """A repository whose second commit pins every library to its first."""

    git(root, "init", "-q")
    for library in LIBRARIES:
        write_library(root, library, f"VERSION = '{library}-1'\n")
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    first = commit_all(root, "Add libraries")
    write_pin_files(root, first, url)
    commit_all(root, "Pin libraries")
    return first


class ParsingAndRewritingTests(unittest.TestCase):
    def test_finds_only_full_commit_library_pins(self) -> None:
        revision = "A" * 40
        text = "\n".join(
            [
                requirement("kit", revision),
                "pypdf==6.21.4",
                f"other @ git+{PIN_URL}@{'b' * 40}#subdirectory=tools/other",
                f"short @ git+{PIN_URL}@abc1234#subdirectory=libraries/short",
                f'"{requirement("telegram-vision", "c" * 40)}",',
            ]
        )

        pins = find_pins(text, "pyproject.toml")

        self.assertEqual(
            pins,
            (
                LibraryPin("pyproject.toml", "kit", PIN_URL, "a" * 40),
                LibraryPin("pyproject.toml", "telegram-vision", PIN_URL, "c" * 40),
            ),
        )

    def test_rewrite_changes_only_the_library_commits(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_pin_files(root, "1" * 40)
            original = (root / "genesis.toml").read_text(encoding="utf-8")

            rewritten = rewrite_pins(original, "2" * 40)

        self.assertNotIn("1" * 40, rewritten)
        self.assertEqual(rewritten.count("2" * 40), len(LIBRARIES))
        self.assertEqual(rewritten, original.replace("1" * 40, "2" * 40))
        self.assertIn('requirement = "pypdf==6.21.4"', rewritten)
        self.assertIn('local_source = "libraries/kit/src"', rewritten)

    def test_rewrite_requires_a_full_commit(self) -> None:
        for revision in ("abc1234", "main", "g" * 40, ""):
            with self.subTest(revision=revision), self.assertRaises(LibraryPinError):
                rewrite_pins(requirement("kit", "1" * 40), revision)

    def test_repository_slug(self) -> None:
        self.assertEqual(repository_slug("https://github.com/our-ark/enoch.git"), "our-ark/enoch")
        self.assertEqual(repository_slug("ssh://git@github.com/our-ark/enoch"), "our-ark/enoch")
        self.assertEqual(repository_slug("git@github.com:our-ark/enoch.git"), "our-ark/enoch")
        self.assertEqual(repository_slug("enoch"), "")

    def test_installed_pins_must_match_their_commits(self) -> None:
        pins = (
            LibraryPin("pyproject.toml", "kit", PIN_URL, "a" * 40),
            LibraryPin("pyproject.toml", "chat", PIN_URL, "a" * 40),
        )

        self.assertEqual(
            installed_pin_problems(pins, {"kit": ("acme-kit", "a" * 40), "chat": ("acme-chat", "a" * 40)}),
            (),
        )
        problems = installed_pin_problems(pins, {"kit": ("acme-kit", "b" * 40)})
        self.assertEqual(len(problems), 2)
        self.assertIn("not its pin", problems[0])
        self.assertIn("chat was not installed", problems[1])


class PinStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = pinned_repository(self.root)

    def test_pins_matching_the_library_code_are_current(self) -> None:
        status = pin_status(self.root)

        self.assertTrue(status.current)
        self.assertEqual(status.libraries, LIBRARIES)
        self.assertEqual({pin.path for pin in status.pins}, {"genesis.toml", "pyproject.toml"})

    def test_a_library_change_is_reported_per_library(self) -> None:
        write_library(self.root, "chat", "VERSION = 'chat-2'\n")
        commit_all(self.root, "Change chat")

        status = pin_status(self.root)

        self.assertFalse(status.current)
        self.assertEqual(status.changed, ("chat",))
        self.assertEqual(status.not_ancestors, ())

    def test_pin_only_and_body_only_commits_stay_current(self) -> None:
        write_library(self.root, "chat", "VERSION = 'chat-2'\n")
        changed = commit_all(self.root, "Change chat")
        write_pin_files(self.root, changed)
        commit_all(self.root, "Re-pin")
        (self.root / "README.md").write_text("body change\n", encoding="utf-8")
        commit_all(self.root, "Body only")

        self.assertTrue(pin_status(self.root).current)

    def test_unknown_pins_count_as_changed_and_not_ancestors(self) -> None:
        write_pin_files(self.root, "f" * 40)
        commit_all(self.root, "Pin to nowhere")

        status = pin_status(self.root)

        self.assertEqual(status.changed, LIBRARIES)
        self.assertEqual(status.not_ancestors, ("f" * 40,))

    def test_reads_pins_from_a_commit_or_the_working_tree(self) -> None:
        write_pin_files(self.root, "e" * 40)

        self.assertEqual({pin.revision for pin in read_pins(self.root)}, {"e" * 40})
        self.assertEqual({pin.revision for pin in read_pins(self.root, "HEAD")}, {self.first})


class AncestorCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = pinned_repository(self.root)

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = main(["--root", str(self.root), *args])
        return code, output.getvalue()

    def test_pins_on_the_checked_history_pass(self) -> None:
        self.assertEqual(ancestor_problems(self.root), ())
        code, output = self.run_cli("check-ancestors")
        self.assertEqual(code, 0, output)

    def test_a_pin_to_an_unmerged_commit_fails(self) -> None:
        main_branch = git(self.root, "branch", "--show-current")
        git(self.root, "switch", "-q", "-c", "side")
        write_library(self.root, "kit", "VERSION = 'kit-side'\n")
        side = commit_all(self.root, "Unmerged library work")
        git(self.root, "switch", "-q", main_branch)
        write_pin_files(self.root, side)
        commit_all(self.root, "Pin to the side branch")

        problems = ancestor_problems(self.root)

        self.assertEqual(len(problems), 1)
        self.assertIn(side, problems[0])
        self.assertIn("chat, kit", problems[0])
        code, output = self.run_cli("check-ancestors")
        self.assertEqual(code, 1)
        self.assertIn("is not an ancestor", output)

    def test_an_unknown_commit_fails(self) -> None:
        write_pin_files(self.root, "d" * 40)
        commit_all(self.root, "Pin to an unknown commit")

        self.assertEqual(len(ancestor_problems(self.root)), 1)

    def test_missing_pins_fail(self) -> None:
        (self.root / "genesis.toml").unlink()
        (self.root / "pyproject.toml").unlink()
        commit_all(self.root, "Drop pins")

        self.assertIn("No library pins", ancestor_problems(self.root)[0])


class CiOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.first = pinned_repository(self.root)

    def test_current_pins_are_installed_unchanged(self) -> None:
        head = git(self.root, "rev-parse", "HEAD")

        override = apply_ci_override(self.root)

        self.assertFalse(override.applied)
        self.assertEqual(override.environment(), {})
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)

    def test_changed_libraries_resolve_from_the_checkout_under_test(self) -> None:
        write_library(self.root, "kit", "VERSION = 'kit-2'\n")
        source = commit_all(self.root, "Change the kit without touching pins")

        override = apply_ci_override(self.root)

        self.assertTrue(override.applied)
        self.assertEqual(override.source_revision, source)
        self.assertEqual(override.libraries, ("kit",))
        # One CI-only commit on top of the code under test, pins at that code.
        self.assertEqual(git(self.root, "log", "-1", "--format=%s"), CI_COMMIT_MESSAGE)
        self.assertEqual(git(self.root, "rev-parse", "HEAD^"), source)
        self.assertEqual(
            git(self.root, "diff", "--name-only", "HEAD^", "HEAD").splitlines(),
            ["genesis.toml", "pyproject.toml"],
        )
        self.assertEqual({pin.revision for pin in read_pins(self.root, "HEAD")}, {source})
        self.assertEqual(git(self.root, "status", "--porcelain", "--untracked-files=no"), "")
        self.assertEqual(git(self.root, "rev-parse", CI_SOURCE_BRANCH), source)
        self.assertTrue(pin_status(self.root).current)
        environment = override.environment()
        self.assertEqual(environment["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(environment["GIT_CONFIG_KEY_0"], f"url.{self.root.resolve().as_uri()}.insteadOf")
        self.assertEqual(environment["GIT_CONFIG_VALUE_0"], PIN_URL)

        # The way pip fetches a pinned requirement now reaches this checkout.
        with TemporaryDirectory() as clone_parent:
            clone = Path(clone_parent) / "clone"
            result = subprocess.run(
                ["git", "clone", "--quiet", PIN_URL, str(clone)],
                env={**os.environ, **environment},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            git(clone, "checkout", "--quiet", source)
            self.assertIn(
                "kit-2",
                (clone / "libraries/kit/src/acme_kit/__init__.py").read_text(encoding="utf-8"),
            )

    def test_cli_exports_the_url_rewrite_for_later_steps(self) -> None:
        write_library(self.root, "chat", "VERSION = 'chat-2'\n")
        commit_all(self.root, "Change chat")
        github_env = self.root.parent / f"{self.root.name}-github-env"
        self.addCleanup(lambda: github_env.unlink(missing_ok=True))

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--root", str(self.root), "ci-override", "--github-env", str(github_env)])

        self.assertEqual(code, 0)
        self.assertIn("chat", output.getvalue())
        lines = github_env.read_text(encoding="utf-8").splitlines()
        self.assertIn("GIT_CONFIG_COUNT=1", lines)
        self.assertIn(f"GIT_CONFIG_VALUE_0={PIN_URL}", lines)

    def test_refuses_a_dirty_checkout(self) -> None:
        write_library(self.root, "chat", "VERSION = 'chat-2'\n")
        commit_all(self.root, "Change chat")
        (self.root / "README.md").write_text("uncommitted\n", encoding="utf-8")

        with self.assertRaises(LibraryPinError):
            apply_ci_override(self.root)


if __name__ == "__main__":
    unittest.main()
