from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from enoch.vcs_tools import changed_files, commit, current_branch, diff_summary, stage_files
from enoch.immune import run_immune_system
from enoch.providers.contracts import (
    EvolutionProvenance,
    ForgeProviderError,
    LocalPublishResult,
    PullRequestResult,
    RemotePublishResult,
)
from enoch.providers.registry import load_provider


@dataclass
class FunctionForgeProvider:
    close_fn: Callable[..., Any]
    create_fn: Callable[..., Any]
    inspect_fn: Callable[..., Any]
    inspect_merge_fn: Callable[..., Any]
    list_fn: Callable[..., tuple[Any, ...]]
    merge_fn: Callable[..., Any]
    name: str = "function-forge"
    provider_kind: str = "forge"

    def close_pull_request(
        self,
        number: int,
        *,
        root: Path | None = None,
        comment: str | None = None,
    ) -> Any:
        return self.close_fn(number, root=root, comment=comment)

    def create_pull_request(self, **kwargs: Any) -> Any:
        return self.create_fn(**kwargs)

    def inspect_pull_request(self, reference: str, root: Path | None = None) -> Any:
        return self.inspect_fn(reference, root)

    def inspect_pull_request_merge(
        self,
        reference: str,
        root: Path | None = None,
    ) -> Any:
        return self.inspect_merge_fn(reference, root)

    def list_open_pull_requests(
        self,
        root: Path | None = None,
        *,
        limit: int = 20,
    ) -> tuple[Any, ...]:
        if limit == 20:
            return self.list_fn(root)
        return self.list_fn(root, limit=limit)

    def merge_pull_request(self, reference: str, root: Path | None = None) -> Any:
        return self.merge_fn(reference, root)


class LocalForgeProvider:
    """Keeps completed work on a local branch when no remote forge is installed."""

    name = "local"
    provider_kind = "forge"
    supports_remote_review = False

    @staticmethod
    def feature_title(text: str) -> str:
        return " ".join(text.strip().split())[:72].strip() or "Enoch feature"

    def prepare_local_publish(
        self,
        commit_message: str,
        *,
        root: Path | None = None,
        allowed_files: list[str] | tuple[str, ...] | None = None,
        **_kwargs: Any,
    ) -> LocalPublishResult:
        commit_message = commit_message.strip()
        if not commit_message:
            raise ForgeProviderError("Commit message cannot be empty.")
        files = changed_files(root)
        if allowed_files is not None:
            allowed = {path for path in allowed_files if path}
            unexpected = sorted(path for path in files if path not in allowed)
            if unexpected:
                raise ForgeProviderError(
                    f"Refusing to publish unexpected files: {', '.join(unexpected[:8])}"
                )
            files = [path for path in files if path in allowed]
        if not files:
            raise ForgeProviderError("No local changes to publish.")
        doctor = run_immune_system(root)
        if not doctor.passed:
            raise ForgeProviderError(f"Doctor failed: {doctor.diagnosis.summary}")
        diff = diff_summary(root)
        stage_files(files, root)
        commit_sha = commit(commit_message, root)
        return LocalPublishResult(
            branch=current_branch(root),
            commit_message=commit_message,
            changed_files=files,
            diff=diff,
            doctor=doctor,
            commit_sha=commit_sha,
        )

    def push_current_branch(self, *, root: Path | None = None, **_kwargs: Any) -> RemotePublishResult:
        return RemotePublishResult(
            branch=current_branch(root),
            remote="local",
            pushed=False,
            ahead_count=0,
            compare_url=None,
        )

    def create_pull_request(self, **kwargs: Any) -> PullRequestResult:
        root = kwargs.get("root")
        branch = current_branch(root)
        return PullRequestResult(
            branch=branch,
            title=str(kwargs.get("title") or "Local change"),
            body=str(kwargs.get("body") or ""),
            created=False,
            url=None,
            fallback_url=None,
            note="No forge provider is configured; the committed branch was kept locally.",
        )

    @staticmethod
    def format_evolution_provenance(provenance: EvolutionProvenance) -> str:
        return "\n".join(
            [
                "## Evolution provenance",
                "",
                f"- Candidate: `{provenance.candidate_id}`",
                f"- Evidence source: `{provenance.evidence_source}`",
                f"- Signal actor: `{provenance.signal_actor}`",
                f"- Candidate actor: `{provenance.candidate_actor}`",
                f"- Approval actor: `{provenance.approval_actor}`",
                f"- Task: `#{provenance.task_id}`",
            ]
        )

    @staticmethod
    def list_open_pull_requests(_root: Path | None = None, *, limit: int = 20) -> tuple[Any, ...]:
        del limit
        return ()

    @staticmethod
    def _unsupported() -> None:
        raise ForgeProviderError("This command requires a configured forge provider.")

    def close_pull_request(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported()

    def inspect_pull_request(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported()

    def inspect_pull_request_merge(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported()

    def merge_pull_request(self, *_args: Any, **_kwargs: Any) -> Any:
        return self._unsupported()

def create_local_provider(_root: Path | None = None) -> LocalForgeProvider:
    return LocalForgeProvider()


OUR_ARK_PROVIDERS = (
    {
        "kind": "forge",
        "name": "local",
        "factory": create_local_provider,
        "default": True,
    },
)


def feature_title(text: str, root: Path | None = None) -> str:
    return load_provider("forge", root).feature_title(text)


def prepare_local_publish(commit_message: str, **kwargs: Any) -> Any:
    return load_provider("forge", kwargs.get("root")).prepare_local_publish(
        commit_message,
        **kwargs,
    )


def push_current_branch(**kwargs: Any) -> Any:
    return load_provider("forge", kwargs.get("root")).push_current_branch(**kwargs)


def format_evolution_provenance(
    provenance: EvolutionProvenance,
    root: Path | None = None,
) -> str:
    return load_provider("forge", root).format_evolution_provenance(provenance)


def close_pull_request(
    number: int,
    *,
    root: Path | None = None,
    comment: str | None = None,
) -> Any:
    return load_provider("forge", root).close_pull_request(
        number,
        root=root,
        comment=comment,
    )


def create_pull_request(**kwargs: Any) -> Any:
    return load_provider("forge", kwargs.get("root")).create_pull_request(**kwargs)


def inspect_pull_request(reference: str, root: Path | None = None) -> Any:
    return load_provider("forge", root).inspect_pull_request(reference, root)


def inspect_pull_request_merge(reference: str, root: Path | None = None) -> Any:
    return load_provider("forge", root).inspect_pull_request_merge(reference, root)


def list_open_pull_requests(
    root: Path | None = None,
    *,
    limit: int = 20,
) -> tuple[Any, ...]:
    return load_provider("forge", root).list_open_pull_requests(root, limit=limit)


def merge_pull_request(reference: str, root: Path | None = None) -> Any:
    return load_provider("forge", root).merge_pull_request(reference, root)
