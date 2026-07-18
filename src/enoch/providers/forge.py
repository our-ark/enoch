from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class FunctionForgeProvider:
    close_fn: Callable[..., Any]
    create_fn: Callable[..., Any]
    inspect_fn: Callable[..., Any]
    inspect_merge_fn: Callable[..., Any]
    list_fn: Callable[..., tuple[Any, ...]]
    merge_fn: Callable[..., Any]
    name: str = "github"
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


class GithubForgeProvider(FunctionForgeProvider):
    def __init__(self) -> None:
        from enoch.github.workflow import (
            close_pull_request,
            create_pull_request,
            inspect_pull_request,
            inspect_pull_request_merge,
            list_open_pull_requests,
            merge_pull_request,
        )

        super().__init__(
            close_fn=close_pull_request,
            create_fn=create_pull_request,
            inspect_fn=inspect_pull_request,
            inspect_merge_fn=inspect_pull_request_merge,
            list_fn=list_open_pull_requests,
            merge_fn=merge_pull_request,
        )
