from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from enoch.providers.contracts import EvolutionProvenance
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
