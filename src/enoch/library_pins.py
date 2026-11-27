"""The single pin layer for Enoch's own libraries.

Enoch installs its ``libraries/<name>`` packages instead of inheriting them:
``libraries/`` sits outside the Genesis body, so descendants get those
packages by installing pinned requirements of the form

    <distribution> @ git+<repository>@<40-hex commit>#subdirectory=libraries/<name>

Only ``genesis.toml`` (``[[runtime_dependencies]].requirement``) and the root
``pyproject.toml`` carry these pins. Libraries depend on each other by name
only (for example ``our-ark-provider-kit>=0.9``), so pip resolves a library's
dependency to the pinned URL listed next to it at the top level.

Pull requests never edit pin lines. CI tests the pull request's own library
code (:func:`apply_ci_override`). After a merge that changes ``libraries/``,
Enoch opens one follow-up pull request that re-pins every library to that
main commit (``enoch.app.library_repin``). :func:`pin_status` drives both
decisions. A pin-only change leaves every library tree unchanged, so it never
asks for another re-pin.

Run ``python -m enoch.library_pins --help`` for the CI entry points.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable, Mapping, Sequence


PIN_FILES = ("genesis.toml", "pyproject.toml")
LIBRARIES_DIRECTORY = "libraries"
CI_SOURCE_BRANCH = "ci-library-source"
CI_COMMIT_MESSAGE = "CI only: resolve library pins from the checkout under test"
FULL_COMMIT = re.compile(r"[0-9a-fA-F]{40}")
PIN = re.compile(
    r"git\+(?P<repository>[^\s\"'@#]+)"
    r"@(?P<revision>[0-9a-fA-F]{40})"
    r"#subdirectory=" + LIBRARIES_DIRECTORY + r"/(?P<library>[A-Za-z0-9][A-Za-z0-9_.-]*)"
    r"(?![A-Za-z0-9_.-])"
)


class LibraryPinError(RuntimeError):
    pass


@dataclass(frozen=True)
class LibraryPin:
    path: str
    library: str
    repository: str
    revision: str


@dataclass(frozen=True)
class PinStatus:
    """How the pins in ``revision``'s pin files relate to ``revision`` itself."""

    revision: str
    pins: tuple[LibraryPin, ...]
    # Libraries whose code at ``revision`` differs from their pinned commit's
    # code, or whose pinned commit is unknown locally.
    changed: tuple[str, ...]
    # Pinned commits that are not ancestors of ``revision``.
    not_ancestors: tuple[str, ...]

    @property
    def current(self) -> bool:
        return bool(self.pins) and not self.changed and not self.not_ancestors

    @property
    def libraries(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pin.library for pin in self.pins))

    @property
    def repositories(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(pin.repository for pin in self.pins))


@dataclass(frozen=True)
class CiOverride:
    applied: bool
    source_revision: str
    libraries: tuple[str, ...] = ()
    git_config: tuple[tuple[str, str], ...] = ()

    def environment(self) -> dict[str, str]:
        """``GIT_CONFIG_*`` variables that send pinned URLs to the local checkout."""

        if not self.git_config:
            return {}
        values = {"GIT_CONFIG_COUNT": str(len(self.git_config))}
        for index, (key, value) in enumerate(self.git_config):
            values[f"GIT_CONFIG_KEY_{index}"] = key
            values[f"GIT_CONFIG_VALUE_{index}"] = value
        return values


# Parsing and rewriting ------------------------------------------------------


def find_pins(text: str, path: str = "") -> tuple[LibraryPin, ...]:
    return tuple(
        LibraryPin(
            path=path,
            library=match.group("library"),
            repository=match.group("repository"),
            revision=match.group("revision").lower(),
        )
        for match in PIN.finditer(text)
    )


def rewrite_pins(text: str, revision: str) -> str:
    """Point every library pin in ``text`` at ``revision``; nothing else changes."""

    revision = _full_commit(revision)
    return PIN.sub(
        lambda match: (
            f"git+{match.group('repository')}@{revision}"
            f"#subdirectory={LIBRARIES_DIRECTORY}/{match.group('library')}"
        ),
        text,
    )


def rewrite_pin_files(root: Path, revision: str) -> tuple[str, ...]:
    """Rewrite the pin files in a working tree; returns the files that changed."""

    changed = []
    for name in PIN_FILES:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        updated = rewrite_pins(text, revision)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(name)
    return tuple(changed)


def read_pins(root: Path, ref: str | None = None) -> tuple[LibraryPin, ...]:
    """Pins in the pin files of the working tree, or of commit ``ref``."""

    pins: list[LibraryPin] = []
    for name in PIN_FILES:
        if ref is None:
            path = root / name
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        else:
            result = _git(root, "show", f"{ref}:{name}", check=False)
            text = result.stdout if result.returncode == 0 else ""
        pins.extend(find_pins(text, name))
    return tuple(pins)


def repository_slug(url: str) -> str:
    """``owner/name`` of a pin repository URL, or empty when it has no such path."""

    text = url.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
        text = text.split("/", 1)[1] if "/" in text else ""
    elif ":" in text:
        text = text.split(":", 1)[1]
    else:
        return ""
    text = text.strip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text if text.count("/") >= 1 else ""


# Status ---------------------------------------------------------------------


def pin_status(
    root: Path,
    ref: str = "HEAD",
    *,
    pins: Sequence[LibraryPin] | None = None,
) -> PinStatus:
    """Compare ``ref``'s pins (or ``pins``) with the library code at ``ref``.

    A library has changed when the tree of ``libraries/<name>`` at ``ref``
    differs from that tree at its pinned commit. Comparing trees, not commits,
    is what keeps a pin-only commit from asking for another re-pin.
    """

    revision = resolve_commit(root, ref)
    selected = tuple(pins) if pins is not None else read_pins(root, revision)
    changed: list[str] = []
    for pin in selected:
        if pin.library in changed:
            continue
        path = f"{LIBRARIES_DIRECTORY}/{pin.library}"
        pinned = tree_id(root, pin.revision, path)
        if pinned is None or pinned != tree_id(root, revision, path):
            changed.append(pin.library)
    not_ancestors = [
        pinned
        for pinned in dict.fromkeys(pin.revision for pin in selected)
        if not is_ancestor(root, pinned, revision)
    ]
    return PinStatus(
        revision=revision,
        pins=selected,
        changed=tuple(changed),
        not_ancestors=tuple(not_ancestors),
    )


def hosts_libraries(root: Path, ref: str, pins: Iterable[LibraryPin]) -> bool:
    """Whether ``ref`` contains the pinned libraries' source (descendants do not)."""

    return any(
        tree_id(root, ref, f"{LIBRARIES_DIRECTORY}/{pin.library}") is not None
        for pin in pins
    )


# CI -------------------------------------------------------------------------


def apply_ci_override(
    root: Path,
    *,
    ref: str = "HEAD",
    source_branch: str = CI_SOURCE_BRANCH,
) -> CiOverride:
    """Make a CI checkout install the libraries it contains, not older pins.

    When every pinned library tree already matches the checkout, nothing
    changes and CI installs the real pins. Otherwise the pins are rewritten to
    the checkout's commit in a CI-only commit on top of it, and the returned
    Git configuration rewrites the pin repository URL to this checkout, so pip
    and Genesis install the code under test, including for fork pull requests.
    Callers export :meth:`CiOverride.environment` to later steps.
    """

    root = root.resolve()
    status = pin_status(root, ref)
    if not status.pins:
        raise LibraryPinError(f"No library pins found in {', '.join(PIN_FILES)} at {ref}.")
    if not status.changed:
        return CiOverride(applied=False, source_revision=status.revision)
    if resolve_commit(root, "HEAD") != status.revision:
        raise LibraryPinError("The CI override must start from the checked-out commit.")
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty:
        raise LibraryPinError("The CI override needs a checkout without tracked changes.")
    _git(root, "branch", "--force", source_branch, status.revision)
    changed_files = rewrite_pin_files(root, status.revision)
    _git(
        root,
        "-c", "user.name=Enoch CI",
        "-c", "user.email=ci@localhost",
        "-c", "commit.gpgsign=false",
        "commit", "--quiet", "--no-verify", "-m", CI_COMMIT_MESSAGE, "--", *changed_files,
    )
    target = root.as_uri()
    return CiOverride(
        applied=True,
        source_revision=status.revision,
        libraries=status.changed,
        git_config=tuple(
            (f"url.{target}.insteadOf", repository) for repository in status.repositories
        ),
    )


def ancestor_problems(root: Path, ref: str = "HEAD") -> tuple[str, ...]:
    """Reasons ``ref``'s pins are not reproducible from ``ref``'s own history."""

    status = pin_status(root, ref)
    if not status.pins:
        return (f"No library pins found in {', '.join(PIN_FILES)} at {ref}.",)
    problems = []
    for revision in status.not_ancestors:
        libraries = sorted({pin.library for pin in status.pins if pin.revision == revision})
        problems.append(
            f"{revision} (pinned for {', '.join(libraries)}) is not an ancestor of "
            f"{status.revision}."
        )
    return tuple(problems)


def installed_library_revisions() -> dict[str, tuple[str, str]]:
    """``library -> (distribution, commit)`` for libraries installed from Git URLs."""

    from importlib.metadata import distributions

    installed: dict[str, tuple[str, str]] = {}
    for distribution in distributions():
        text = distribution.read_text("direct_url.json")
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        vcs = data.get("vcs_info") if isinstance(data, dict) else None
        subdirectory = str(data.get("subdirectory") or "") if isinstance(data, dict) else ""
        prefix = f"{LIBRARIES_DIRECTORY}/"
        if not isinstance(vcs, dict) or not subdirectory.startswith(prefix):
            continue
        installed[subdirectory[len(prefix):]] = (
            str(distribution.metadata.get("Name") or ""),
            str(vcs.get("commit_id") or "").lower(),
        )
    return installed


def installed_pin_problems(
    pins: Iterable[LibraryPin],
    installed: Mapping[str, tuple[str, str]],
) -> tuple[str, ...]:
    """Pinned libraries that were not installed from exactly their pinned commit.

    A provider's bare ``our-ark-provider-kit`` dependency must resolve to the
    top-level pinned URL, never to an index or a second copy.
    """

    problems = []
    for pin in {pin.library: pin for pin in pins}.values():
        found = installed.get(pin.library)
        if found is None:
            problems.append(f"{pin.library} was not installed from its pinned Git URL.")
        elif found[1] != pin.revision:
            problems.append(
                f"{pin.library} ({found[0]}) was installed from {found[1] or 'an unknown commit'}, "
                f"not its pin {pin.revision}."
            )
    return tuple(problems)


# Git ------------------------------------------------------------------------


def resolve_commit(root: Path, ref: str) -> str:
    result = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    revision = result.stdout.strip()
    if result.returncode != 0 or not FULL_COMMIT.fullmatch(revision):
        raise LibraryPinError(f"Unknown commit: {ref}")
    return revision.lower()


def tree_id(root: Path, revision: str, path: str) -> str | None:
    result = _git(root, "rev-parse", "--verify", "--quiet", f"{revision}:{path}", check=False)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    if _git(root, "cat-file", "-e", f"{ancestor}^{{commit}}", check=False).returncode != 0:
        return False
    result = _git(root, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    return result.returncode == 0


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git failed"
        raise LibraryPinError(f"git {' '.join(args)}: {detail}")
    return result


def _full_commit(revision: str) -> str:
    text = str(revision).strip()
    if not FULL_COMMIT.fullmatch(text):
        raise LibraryPinError(f"Library pins need a full 40-character commit, not {revision!r}.")
    return text.lower()


# Command line ---------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m enoch.library_pins",
        description="Inspect, check, and rewrite Enoch's library pins.",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    commands = parser.add_subparsers(dest="command", required=True)

    show = commands.add_parser("status", help="Show the pins and whether they match the code.")
    show.add_argument("--ref", default="HEAD")

    check = commands.add_parser(
        "check-ancestors",
        help="Fail when a pinned commit is not an ancestor of --ref.",
    )
    check.add_argument("--ref", default="HEAD")

    rewrite = commands.add_parser("rewrite", help="Point every pin in the working tree at a commit.")
    rewrite.add_argument("revision")

    override = commands.add_parser(
        "ci-override",
        help="Make this CI checkout install its own libraries when they differ from the pins.",
    )
    override.add_argument("--ref", default="HEAD")
    override.add_argument(
        "--github-env",
        type=Path,
        help="Append the Git URL rewrite variables to this GITHUB_ENV file.",
    )

    commands.add_parser(
        "verify-installed",
        help="Fail unless this interpreter installed every pyproject.toml pin at its commit.",
    )

    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "status":
            status = pin_status(root, args.ref)
            for pin in status.pins:
                print(f"{pin.path}: {pin.library} @ {pin.revision}")
            print(f"changed libraries: {', '.join(status.changed) or 'none'}")
            print(f"non-ancestor pins: {', '.join(status.not_ancestors) or 'none'}")
            return 0
        if args.command == "check-ancestors":
            problems = ancestor_problems(root, args.ref)
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            if problems:
                print(
                    "Library pins must point at commits on main. Re-pin with "
                    "`python -m enoch.library_pins rewrite <main commit>`.",
                    file=sys.stderr,
                )
                return 1
            print("Every library pin is an ancestor of the checked commit.")
            return 0
        if args.command == "verify-installed":
            pins = tuple(pin for pin in read_pins(root) if pin.path == "pyproject.toml")
            if not pins:
                raise LibraryPinError("No library pins found in pyproject.toml.")
            installed = installed_library_revisions()
            problems = installed_pin_problems(pins, installed)
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            if problems:
                return 1
            for library, (name, revision) in sorted(installed.items()):
                print(f"{name} ({LIBRARIES_DIRECTORY}/{library}) installed at {revision}")
            return 0
        if args.command == "rewrite":
            changed = rewrite_pin_files(root, args.revision)
            print(f"Rewrote {', '.join(changed) or 'nothing'}.")
            return 0
        if args.command == "ci-override":
            result = apply_ci_override(root, ref=args.ref)
            if not result.applied:
                print("Library pins already match this checkout; CI installs the real pins.")
                return 0
            print(
                "Libraries differ from their pins ("
                + ", ".join(result.libraries)
                + f"); CI installs them from this checkout at {result.source_revision}."
            )
            if args.github_env is not None:
                with args.github_env.open("a", encoding="utf-8") as stream:
                    for key, value in result.environment().items():
                        stream.write(f"{key}={value}\n")
            return 0
    except LibraryPinError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
