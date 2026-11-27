"""Re-pin Enoch's libraries after a merge that changed them.

Pull requests never edit library pin lines (see ``enoch.library_pins``). When
Enoch lands a review into the authoritative branch of its own repository, a
re-pin check is marked pending here, in private state, so it survives the
automatic update's restart. The daemon loop then:

1. fetches the authoritative branch and reads the pins at its tip;
2. does nothing when every pinned ``libraries/<name>`` tree already matches
   that tip. A pin-only merge changes no library tree, so the re-pin PR's own
   landing never opens another one;
3. otherwise commits one change that points every library pin at the tip, in a
   temporary worktree, and publishes it as a single review. Older open re-pin
   reviews are closed as superseded;
4. registers it for merging when ``/pr auto-merge`` is on, so the pending-merge
   watcher lands it once its checks, including descent from the real pins, pass.

Repositories that do not host the pinned libraries (Genesis descendants) or
whose pins name a different repository never re-pin.

This lives in Enoch rather than in a GitHub Actions job because Enoch already
owns the landing path. The review is published with the host's forge
credentials, so its CI runs like any other PR. A PR opened with Actions'
``GITHUB_TOKEN`` triggers no workflows and needs repository settings that let
Actions open PRs. The pending-merge watcher also keeps the branch up to date
and applies ``/pr auto-merge`` without a second merge path.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from typing import Iterator, Sequence

from enoch.library_pins import (
    LIBRARIES_DIRECTORY,
    PIN_FILES,
    LibraryPinError,
    hosts_libraries,
    pin_status,
    read_pins,
    repository_slug,
    resolve_commit,
    rewrite_pin_files,
)
from enoch.paths import private_state_path
from enoch.state import atomic_write, load_json_object


SCHEMA_VERSION = 1
LIBRARY_REPIN_STATE_FILE = "library_repin.json"
REPIN_BRANCH_PREFIX = "enoch/repin-libraries-"
REPIN_TITLE_PREFIX = "Re-pin libraries to "
REPIN = "repin"
CURRENT = "current"
NOT_APPLICABLE = "not-applicable"
_LOCK = threading.Lock()


@dataclass(frozen=True)
class LibraryRepinState:
    # Labels (``#N``) of landed reviews waiting for the check; empty means none.
    pending: tuple[str, ...] = ()
    requested_at: str = ""


@dataclass(frozen=True)
class RepinPlan:
    action: str
    revision: str = ""
    libraries: tuple[str, ...] = ()
    reason: str = ""

    @property
    def short(self) -> str:
        return self.revision[:12]

    @property
    def branch(self) -> str:
        return f"{REPIN_BRANCH_PREFIX}{self.short}"

    @property
    def title(self) -> str:
        return f"{REPIN_TITLE_PREFIX}{self.short}"

    def commit_message(self) -> str:
        return f"{self.title}\n\n{self._explanation()}\n"

    def review_body(self) -> str:
        return (
            f"{self._explanation()}\n\n"
            "Enoch opened this automatically after the merge. It changes only pin "
            "lines, never `libraries/`, so landing it does not ask for another "
            "re-pin. Its depth-2 descent check installs from these real pins, which "
            "verifies that descendants install the merged library code.\n"
        )

    def _explanation(self) -> str:
        if self.libraries:
            changed = f"changed {LIBRARIES_DIRECTORY}/ ({', '.join(self.libraries)})"
        else:
            changed = "is not a descendant of every pinned commit"
        return (
            f"Main at {self.revision} {changed}. This points every library pin in "
            f"{' and '.join(PIN_FILES)} at that one commit."
        )


# State -----------------------------------------------------------------------


def library_repin_path(root: Path | None = None) -> Path:
    return private_state_path(LIBRARY_REPIN_STATE_FILE, root)


def load_library_repin_state(root: Path | None = None) -> LibraryRepinState:
    data = load_json_object(library_repin_path(root))
    pending = data.get("pending")
    labels = (
        tuple(dict.fromkeys(str(item).strip() for item in pending if str(item).strip()))
        if isinstance(pending, list)
        else ()
    )
    return LibraryRepinState(pending=labels, requested_at=str(data.get("requested_at") or ""))


def mark_library_repin_pending(
    label: str,
    requested_at: str,
    root: Path | None = None,
) -> LibraryRepinState:
    with _LOCK:
        state = load_library_repin_state(root)
        pending = state.pending if label in state.pending else (*state.pending, label)
        state = LibraryRepinState(pending=pending, requested_at=state.requested_at or requested_at)
        _save(state, root)
        return state


def clear_library_repin_pending(
    labels: Sequence[str] | None = None,
    root: Path | None = None,
) -> LibraryRepinState:
    """Clear the merges a check covered (all when ``labels`` is None)."""

    with _LOCK:
        state = load_library_repin_state(root)
        covered = state.pending if labels is None else tuple(labels)
        remaining = tuple(label for label in state.pending if label not in covered)
        state = LibraryRepinState(
            pending=remaining,
            requested_at=state.requested_at if remaining else "",
        )
        _save(state, root)
        return state


def _save(state: LibraryRepinState, root: Path | None) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "pending": list(state.pending),
        "requested_at": state.requested_at,
    }
    atomic_write(library_repin_path(root), json.dumps(payload, indent=2, sort_keys=True) + "\n")


# Planning and preparing --------------------------------------------------------


def plan_library_repin(
    root: Path,
    *,
    remote: str,
    base_branch: str,
    repository: str,
) -> RepinPlan:
    """Fetch the authoritative branch and decide whether its pins need a re-pin."""

    tracking = f"refs/remotes/{remote}/{base_branch}"
    _git(root, "fetch", "--quiet", remote, f"+refs/heads/{base_branch}:{tracking}")
    revision = resolve_commit(root, tracking)
    pins = read_pins(root, revision)
    if not pins:
        return RepinPlan(NOT_APPLICABLE, revision, reason="no library pins")
    if not hosts_libraries(root, revision, pins):
        return RepinPlan(NOT_APPLICABLE, revision, reason="the pinned libraries are not hosted here")
    expected = repository.strip().casefold()
    if not expected or any(
        repository_slug(pin.repository).casefold() != expected for pin in pins
    ):
        return RepinPlan(NOT_APPLICABLE, revision, reason="the pins name another repository")
    status = pin_status(root, revision, pins=pins)
    if status.current:
        return RepinPlan(CURRENT, revision, reason="every pin matches the library code")
    reason = (
        f"changed libraries: {', '.join(status.changed)}"
        if status.changed
        else f"non-ancestor pins: {', '.join(status.not_ancestors)}"
    )
    return RepinPlan(REPIN, revision, libraries=status.changed, reason=reason)


def remote_branch_exists(root: Path, remote: str, branch: str) -> bool:
    result = _git(root, "ls-remote", "--exit-code", "--heads", remote, branch, check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise LibraryPinError(
        f"Could not check {remote}/{branch}: {result.stderr.strip() or result.stdout.strip()}"
    )


@contextmanager
def repin_workspace(root: Path, plan: RepinPlan) -> Iterator[Path]:
    """A temporary worktree on ``plan.branch`` holding the one re-pin commit.

    The worktree and its local branch are removed afterwards. The pushed
    branch and the published review stay.
    """

    if plan.action != REPIN:
        raise LibraryPinError(f"Nothing to re-pin: {plan.reason or plan.action}.")
    parent = Path(tempfile.mkdtemp(prefix="enoch-library-repin-"))
    path = parent / "worktree"
    added = False
    try:
        _git(root, "worktree", "add", "--quiet", "-B", plan.branch, str(path), plan.revision)
        added = True
        changed = rewrite_pin_files(path, plan.revision)
        if not changed:
            raise LibraryPinError("The pin files did not change.")
        _git(path, "add", "--", *changed)
        _git(path, "commit", "--quiet", "-m", plan.commit_message())
        yield path
    finally:
        if added:
            _git(root, "worktree", "remove", "--force", str(path), check=False)
            _git(root, "branch", "-D", plan.branch, check=False)
        shutil.rmtree(parent, ignore_errors=True)


def is_repin_review_title(title: str) -> bool:
    return str(title).strip().startswith(REPIN_TITLE_PREFIX)


def opened_message(display_name: str, plan: RepinPlan, label: str) -> str:
    changed = f" ({', '.join(plan.libraries)})" if plan.libraries else ""
    return (
        f"Libraries changed on main{changed}. {display_name} opened {label} to re-pin "
        f"every library to {plan.short}."
    )


def failed_message(display_name: str, error: object) -> str:
    detail = str(error).strip().rstrip(".") or "unknown error"
    return (
        f"{display_name} could not open the library re-pin PR: {detail}. The next "
        "merge into main checks the pins again."
    )


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git failed"
        raise LibraryPinError(f"git {args[0]}: {detail}")
    return result
