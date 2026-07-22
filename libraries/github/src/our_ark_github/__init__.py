from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

from our_ark_github import workflow
from our_ark_provider_kit import ForgeProviderError


class GithubForgeProvider:
    name = "github"
    provider_kind = "forge"

    def __init__(self, gh: str | None = None) -> None:
        self.gh = gh or shutil.which("gh")

    feature_title = staticmethod(workflow.feature_title)
    format_evolution_provenance = staticmethod(workflow.format_evolution_provenance)

    def prepare_local_publish(self, commit_message: str, **kwargs):
        return workflow.prepare_local_publish(commit_message, **kwargs)

    def push_current_branch(self, **kwargs):
        return workflow.push_current_branch(**kwargs)

    def close_pull_request(self, number: int, *, root=None, comment=None):
        return workflow.close_pull_request(number, root=root, comment=comment)

    def create_pull_request(self, **kwargs):
        return workflow.create_pull_request(**kwargs)

    def inspect_pull_request(self, reference: str, root=None):
        return workflow.inspect_pull_request(reference, root)

    def inspect_pull_request_merge(self, reference: str, root=None):
        return workflow.inspect_pull_request_merge(reference, root)

    def list_open_pull_requests(self, root=None, *, limit: int = 20):
        return workflow.list_open_pull_requests(root, limit=limit)

    def merge_pull_request(self, reference: str, root=None):
        return workflow.merge_pull_request(reference, root)

    def read_text(self, repo: str, path: str, ref: str = "main") -> str:
        content = self._content_text(repo, path, ref)
        if content is None:
            raise ForgeProviderError(f"Could not read {path} from {repo}@{ref}.")
        return content

    def remote_parent(self, repo: str, branch: str):
        from enoch.lineage.core import LINEAGE_PATH, parse_lineage_parent

        content = self._content_text(repo, LINEAGE_PATH.as_posix(), branch)
        return parse_lineage_parent(content) if content is not None else None

    def latest_commit(self, repo: str, branch: str) -> str:
        data = self._json(["api", f"repos/{repo}/commits/{branch}"])
        sha = str(data.get("sha") or "").strip()
        if not sha:
            raise ForgeProviderError(f"Could not read latest commit for {repo}:{branch}.")
        return sha

    def declared_skills(self, repo: str, branch: str) -> tuple[str, ...]:
        from enoch.lineage.core import parse_declared_skills

        name = repo.split("/")[-1]
        content = self._content_text(repo, f"src/{name}/identity.yaml", branch)
        return parse_declared_skills(content) if content is not None else ()

    def merged_prs(self, repo: str, branch: str, limit: int = 20) -> list[dict[str, Any]]:
        return list(
            self._json(
                [
                    "pr", "list", "--repo", repo, "--state", "merged", "--base", branch,
                    "--limit", str(limit), "--json",
                    "number,title,body,labels,mergedAt,mergeCommit,url",
                ]
            )
        )

    def commits(self, repo: str, branch: str, limit: int = 20) -> list[dict[str, Any]]:
        return list(self._json(["api", f"repos/{repo}/commits?sha={branch}&per_page={limit}"]))

    def commit_files(self, repo: str, sha: str) -> tuple[str, ...]:
        data = self._json(["api", f"repos/{repo}/commits/{sha}"])
        return tuple(
            str(item.get("filename") or "")
            for item in data.get("files", [])
            if item.get("filename")
        )

    def pr_files(self, repo: str, number: int) -> tuple[str, ...]:
        data = self._json(["pr", "view", str(number), "--repo", repo, "--json", "files"])
        return tuple(str(item.get("path") or "") for item in data.get("files", []) if item.get("path"))

    def _json(self, args: list[str]) -> Any:
        if self.gh is None:
            raise ForgeProviderError("GitHub CLI is not available.")
        result = subprocess.run([self.gh, *args], text=True, capture_output=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "GitHub CLI command failed."
            raise ForgeProviderError(detail)
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as error:
            raise ForgeProviderError("GitHub CLI returned invalid JSON.") from error

    def _content_text(self, repo: str, path: str, ref: str) -> str | None:
        data = self._json(["api", f"repos/{repo}/contents/{path}?ref={ref}"])
        content = str(data.get("content") or "")
        if not content:
            return None
        try:
            return base64.b64decode(content).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as error:
            raise ForgeProviderError(
                f"Forge returned invalid text content for {repo}:{path}@{ref}."
            ) from error


def create_provider(_root: Path | None = None) -> GithubForgeProvider:
    return GithubForgeProvider()


ENOCH_PROVIDERS = (
    {
        "kind": "forge",
        "name": "github",
        "factory": create_provider,
        "default": True,
    },
)


__all__ = ["GithubForgeProvider", "create_provider", "workflow"]
