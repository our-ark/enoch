from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Callable

from enoch.identity import Identity
from enoch.providers.contracts import ProgressCallback, ProviderHealth


RespondFn = Callable[..., str]
ActFn = Callable[..., str]
SummaryFn = Callable[[Path | None], str]
OptionsFn = Callable[[], tuple[Any, ...]]
ResetFn = Callable[[], None]
HealthFn = Callable[[Path | None], ProviderHealth]


@dataclass
class FunctionAgentRuntime:
    respond_fn: RespondFn
    act_in_session_fn: ActFn
    model_summary_fn: SummaryFn
    model_options_fn: OptionsFn
    reset_usage_fn: ResetFn
    health_fn: HealthFn | None = None
    name: str = "codex"
    provider_kind: str = "runtime"
    config_section: str = "codex"

    def respond(
        self,
        identity: Identity,
        message: str,
        cwd: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        session_key: str = "",
    ) -> str:
        return self.respond_fn(
            identity,
            message,
            cwd=cwd,
            progress_callback=progress_callback,
            session_key=session_key,
        )

    def act_in_session(
        self,
        identity: Identity,
        message: str,
        cwd: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        sandbox: str = "",
        session_key: str = "",
        cancellation_event: threading.Event | None = None,
        state_root: Path | None = None,
    ) -> str:
        return self.act_in_session_fn(
            identity,
            message,
            cwd=cwd,
            progress_callback=progress_callback,
            sandbox=sandbox,
            session_key=session_key,
            cancellation_event=cancellation_event,
            state_root=state_root,
        )

    def model_summary(self, root: Path | None = None) -> str:
        return self.model_summary_fn(root)

    def model_options(self) -> tuple[Any, ...]:
        return self.model_options_fn()

    def reset_usage(self) -> None:
        self.reset_usage_fn()

    def health(self, root: Path | None = None) -> ProviderHealth:
        if self.health_fn is not None:
            return self.health_fn(root)
        return ProviderHealth(
            name=f"{self.name} runtime",
            passed=True,
            command=f"{self.name} provider health",
            summary="provider loaded",
        )


class CodexRuntime(FunctionAgentRuntime):
    def __init__(self) -> None:
        from enoch.brain import (
            act_in_session,
            codex_model_options,
            model_summary,
            reset_token_usage,
            respond,
        )

        super().__init__(
            respond_fn=respond,
            act_in_session_fn=act_in_session,
            model_summary_fn=model_summary,
            model_options_fn=codex_model_options,
            reset_usage_fn=reset_token_usage,
            health_fn=_codex_health,
        )


def _codex_health(_root: Path | None = None) -> ProviderHealth:
    from enoch.brain import _codex_binary

    binary = _codex_binary()
    if binary:
        return ProviderHealth(
            name="codex binary",
            passed=True,
            command="which codex",
            summary=binary,
        )
    return ProviderHealth(
        name="codex binary",
        passed=False,
        command="which codex",
        output="Codex binary was not found. Set ENOCH_CODEX_BIN or install codex on PATH.",
        summary="not found",
    )
