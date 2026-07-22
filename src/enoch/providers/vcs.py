from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import subprocess

from enoch.paths import repo_root
from enoch.providers.contracts import VersionControlProviderError


@dataclass(frozen=True)
class VersionControlResult:
    returncode: int
    stdout: str
    stderr: str


class GitVersionControlProvider:
    name = "git"
    provider_kind = "vcs"
    remote = "origin"
    main_branch = "main"

    def run(
        self,
        args: list[str],
        root: Path | None = None,
    ) -> VersionControlResult:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root(root),
            text=True,
            capture_output=True,
            check=False,
        )
        return VersionControlResult(
            result.returncode,
            result.stdout.strip(),
            result.stderr.strip(),
        )

    def current_branch(self, root: Path | None = None) -> str:
        return self._output(["branch", "--show-current"], root, "Could not determine current branch.")

    def is_clean(self, root: Path | None = None) -> bool:
        return not self._output(["status", "--porcelain"], root, "Could not inspect worktree.")

    def changed_files(self, root: Path | None = None) -> list[str]:
        tracked = self._output(
            ["diff", "--name-only", "HEAD"],
            root,
            "Could not inspect changed files.",
        ).splitlines()
        untracked = self._output(
            ["ls-files", "--others", "--exclude-standard"],
            root,
            "Could not inspect untracked files.",
        ).splitlines()
        return [path for path in [*tracked, *untracked] if path]

    def diff_summary(self, root: Path | None = None) -> str:
        summary = self._output(["diff", "--stat", "HEAD"], root, "Could not inspect diff.")
        untracked = self._output(
            ["ls-files", "--others", "--exclude-standard"],
            root,
            "Could not inspect untracked files.",
        ).splitlines()
        parts = [summary] if summary else []
        if untracked:
            parts.append("Untracked files:\n" + "\n".join(f"  {path}" for path in untracked))
        return "\n\n".join(parts) or "No working tree changes."

    def stage(self, files: list[str] | tuple[str, ...], root: Path | None = None) -> None:
        self._output(["add", "--", *files], root, "Could not stage local changes.")

    def commit(self, message: str, root: Path | None = None) -> str:
        self._output(["commit", "-m", message], root, "Could not commit local changes.")
        return self._output(["rev-parse", "--short", "HEAD"], root, "Could not read commit revision.")

    def create_branch(
        self,
        branch: str,
        root: Path | None = None,
        *,
        start_point: str = "",
    ) -> None:
        args = ["switch", "-c", branch]
        if start_point:
            args.append(start_point)
        self._output(args, root, f"Could not create branch {branch}.")

    def switch_branch(self, branch: str, root: Path | None = None) -> None:
        self._output(["switch", branch], root, f"Could not switch to branch {branch}.")

    def delete_branch(
        self,
        branch: str,
        root: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        self._output(
            ["branch", "-D" if force else "-d", branch],
            root,
            f"Could not delete branch {branch}.",
        )

    def branch_exists(self, branch: str, root: Path | None = None) -> bool:
        return self.run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            root,
        ).returncode == 0

    def task_base(self, root: Path | None = None) -> str:
        try:
            self.refresh_authoritative(root)
        except VersionControlProviderError:
            pass
        for revision in (self._authoritative_ref(), self.main_branch):
            if self.resolve_revision(revision, root):
                return revision
        raise VersionControlProviderError(
            f"Could not find {self._authoritative_ref()} or local {self.main_branch}."
        )

    def authoritative_branch(self, root: Path | None = None) -> str:
        del root
        return self.main_branch

    def refresh_authoritative(self, root: Path | None = None) -> str:
        return self._output(
            ["fetch", self.remote, self.main_branch],
            root,
            f"Could not fetch {self._authoritative_ref()}.",
        )

    def authoritative_revision(self, root: Path | None = None) -> str:
        revision = self.resolve_revision(self._authoritative_ref(), root)
        if not revision:
            raise VersionControlProviderError(
                f"Could not resolve authoritative revision {self._authoritative_ref()}."
            )
        return revision

    def current_revision(self, root: Path | None = None) -> str:
        revision = self.resolve_revision("HEAD", root)
        if not revision:
            raise VersionControlProviderError("Could not inspect current revision.")
        return revision

    def resolve_revision(self, revision: str, root: Path | None = None) -> str:
        result = self.run(["rev-parse", revision], root)
        return result.stdout.strip() if result.returncode == 0 else ""

    def is_ancestor(
        self,
        revision: str,
        descendant: str,
        root: Path | None = None,
    ) -> bool:
        return self.run(
            ["merge-base", "--is-ancestor", revision, descendant],
            root,
        ).returncode == 0

    def update_to_authoritative(self, root: Path | None = None) -> str:
        output = self._output(
            ["pull", "--ff-only", self.remote, self.main_branch],
            root,
            f"Could not update from {self._authoritative_ref()}.",
        )
        return output or "Already up to date."

    def restore_revision(self, revision: str, root: Path | None = None) -> None:
        self._output(
            ["reset", "--hard", revision],
            root,
            f"Could not restore revision {revision}.",
        )

    def sync_summary(self, root: Path | None = None) -> str:
        revision = self.resolve_revision(self._authoritative_ref(), root)
        suffix = f" ({self._authoritative_ref()} {revision[:7]})" if revision else ""
        label = f"Last git {self.main_branch} pull observed"
        fetch_path = self._git_path("FETCH_HEAD", root)
        if fetch_path is None or not fetch_path.exists():
            return f"{label}: unavailable{suffix}"
        try:
            content = fetch_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"{label}: unavailable{suffix}"
        if f"branch '{self.main_branch}'" not in content:
            return f"{label}: unavailable{suffix}"
        pulled_at = datetime.fromtimestamp(fetch_path.stat().st_mtime).astimezone()
        return f"{label}: {pulled_at.strftime('%Y-%m-%d %H:%M:%S %Z')}{suffix}"

    def workspace_paths(self, root: Path | None = None) -> tuple[Path, ...]:
        output = self._output(
            ["worktree", "list", "--porcelain"],
            root,
            "Could not inspect Git worktrees.",
        )
        return tuple(
            Path(line.removeprefix("worktree ")).expanduser().resolve()
            for line in output.splitlines()
            if line.startswith("worktree ")
        )

    def create_workspace(
        self,
        path: Path,
        branch: str,
        root: Path | None = None,
        *,
        start_point: str = "",
        create_branch: bool = False,
    ) -> None:
        args = ["worktree", "add"]
        if create_branch:
            args.extend(["-b", branch])
        args.extend([str(path), start_point or branch])
        self._output(args, root, f"Could not create workspace for {branch}.")

    def remove_workspace(self, path: Path, root: Path | None = None) -> None:
        self._output(
            ["worktree", "remove", str(path)],
            root,
            f"Could not remove workspace {path}.",
        )

    def _output(self, args: list[str], root: Path | None, message: str) -> str:
        result = self.run(args, root)
        if result.returncode != 0:
            raise VersionControlProviderError(result.stderr or result.stdout or message)
        return result.stdout.strip()

    def _authoritative_ref(self) -> str:
        return f"{self.remote}/{self.main_branch}"

    def _git_path(self, name: str, root: Path | None) -> Path | None:
        result = self.run(["rev-parse", "--git-path", name], root)
        if result.returncode != 0 or not result.stdout:
            return None
        path = Path(result.stdout)
        return path if path.is_absolute() else repo_root(root) / path


def create_provider(_root: Path | None = None) -> GitVersionControlProvider:
    return GitVersionControlProvider()


OUR_ARK_PROVIDERS = (
    {
        "kind": "vcs",
        "name": "git",
        "factory": create_provider,
        "default": True,
    },
)
