from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

from enoch.paths import repo_root


@dataclass(frozen=True)
class VersionControlResult:
    returncode: int
    stdout: str
    stderr: str


class GitVersionControlProvider:
    name = "git"
    provider_kind = "vcs"

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


def create_provider(_root: Path | None = None) -> GitVersionControlProvider:
    return GitVersionControlProvider()


ENOCH_PROVIDERS = (
    {
        "kind": "vcs",
        "name": "git",
        "factory": create_provider,
        "default": True,
    },
)
