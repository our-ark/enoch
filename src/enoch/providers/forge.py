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
    ProviderCapabilities,
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
    # Optional: a forge that can bring a review branch up to date with its base
    # (``forge.update-branch``) and report CI checks for a review head
    # (``forge.check-status``). Enoch waits for and lands a requested review
    # only on forges with these; others keep refusing reviews that are not ready.
    update_branch_fn: Callable[..., Any] | None = None
    verify_update_fn: Callable[..., bool] | None = None
    checks_fn: Callable[..., Any] | None = None
    # Optional ``forge.ci-repair``: failed-check evidence, re-running failed CI
    # jobs, and comparing revisions. Enoch uses them to repair red CI on a
    # review it is watching or landing; forges without them stop and notify.
    check_failures_fn: Callable[..., Any] | None = None
    rerun_checks_fn: Callable[..., Any] | None = None
    compare_fn: Callable[..., Any] | None = None
    # Optional ``forge.base-freshness``: whether a review head contains the
    # current tip of its base, read from version control rather than from a
    # merge status the forge may still be recomputing after the base moved.
    base_freshness_fn: Callable[..., Any] | None = None
    name: str = "function-forge"
    provider_kind: str = "forge"
    capabilities: ProviderCapabilities = ProviderCapabilities(
        provider_kind="forge",
        capabilities=frozenset(
            {
                "forge.read",
                "forge.publish",
                "forge.maintain",
                "forge.merge",
            }
        ),
    )

    def __post_init__(self) -> None:
        optional = {
            capability
            for capability, fn in (
                ("forge.update-branch", self.update_branch_fn),
                ("forge.check-status", self.checks_fn),
                ("forge.base-freshness", self.base_freshness_fn),
                (
                    "forge.ci-repair",
                    self.check_failures_fn
                    if self.rerun_checks_fn is not None and self.compare_fn is not None
                    else None,
                ),
            )
            if fn is not None
        }
        if optional:
            self.capabilities = ProviderCapabilities(
                provider_kind="forge",
                capabilities=self.capabilities.capabilities | optional,
            )

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

    def update_review_branch(
        self,
        reference: str,
        *,
        expected_head_sha: str,
        root: Path | None = None,
    ) -> Any:
        if self.update_branch_fn is None:
            raise ForgeProviderError("This forge cannot update review branches.")
        return self.update_branch_fn(
            reference,
            expected_head_sha=expected_head_sha,
            root=root,
        )

    def verify_review_branch_update(
        self,
        reference: str,
        *,
        previous_head_sha: str,
        head_sha: str,
        root: Path | None = None,
    ) -> bool:
        if self.verify_update_fn is None:
            return False
        return bool(
            self.verify_update_fn(
                reference,
                previous_head_sha=previous_head_sha,
                head_sha=head_sha,
                root=root,
            )
        )

    def inspect_review_checks(self, reference: str, root: Path | None = None) -> Any:
        if self.checks_fn is None:
            raise ForgeProviderError("This forge cannot inspect review checks.")
        return self.checks_fn(reference, root)

    def inspect_review_check_failures(
        self,
        reference: str,
        *,
        head_sha: str,
        root: Path | None = None,
    ) -> Any:
        if self.check_failures_fn is None:
            raise ForgeProviderError("This forge cannot inspect failed review checks.")
        return self.check_failures_fn(reference, head_sha=head_sha, root=root)

    def rerun_failed_review_checks(
        self,
        reference: str,
        *,
        head_sha: str,
        root: Path | None = None,
    ) -> Any:
        if self.rerun_checks_fn is None:
            raise ForgeProviderError("This forge cannot re-run review checks.")
        return self.rerun_checks_fn(reference, head_sha=head_sha, root=root)

    def compare_review_revisions(
        self,
        reference: str,
        *,
        base: str,
        head: str,
        root: Path | None = None,
    ) -> Any:
        if self.compare_fn is None:
            raise ForgeProviderError("This forge cannot compare review revisions.")
        return self.compare_fn(reference, base=base, head=head, root=root)

    def inspect_review_base_freshness(
        self,
        reference: str,
        *,
        head_sha: str = "",
        root: Path | None = None,
    ) -> Any:
        if self.base_freshness_fn is None:
            raise ForgeProviderError("This forge cannot inspect whether a review contains its base tip.")
        return self.base_freshness_fn(reference, head_sha=head_sha, root=root)


class LocalForgeProvider:
    """Keeps completed work on a local branch when no remote forge is installed."""

    name = "local"
    provider_kind = "forge"
    supports_remote_review = False
    capabilities = ProviderCapabilities(
        provider_kind="forge",
        capabilities=frozenset({"forge.read", "forge.publish"}),
    )

    @staticmethod
    def feature_title(text: str) -> str:
        return " ".join(text.strip().split())[:72].strip() or "Enoch feature"

    def prepare_local_publish(
        self,
        commit_message: str,
        *,
        root: Path | None = None,
        allowed_files: list[str] | tuple[str, ...] | None = None,
        validation_result: object | None = None,
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
        doctor = validation_result or run_immune_system(root)
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
