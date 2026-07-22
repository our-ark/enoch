from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Callable, Sequence

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
        image_paths: Sequence[Path] = (),
    ) -> str:
        return self.respond_fn(
            identity,
            message,
            cwd=cwd,
            progress_callback=progress_callback,
            session_key=session_key,
            image_paths=image_paths,
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
    model_catalog_label = "Available GPT-5.6 models:"
    model_example = "gpt-5.6-sol"

    def __init__(self, root: Path | None = None) -> None:
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
            model_options_fn=lambda: tuple(
                option
                for option in codex_model_options(root)
                if option.slug == "gpt-5.6" or option.slug.startswith("gpt-5.6-")
            ),
            reset_usage_fn=reset_token_usage,
            health_fn=lambda health_root=None: _codex_health(health_root or root),
        )

    def configure(
        self,
        args: tuple[str, ...],
        root: Path,
        *,
        prefix: str = "/",
    ) -> str:
        from enoch.brain import resolve_codex_executable_value
        from enoch.config import write_section_value

        if not args:
            return self.config_help(prefix=prefix)
        if args[0].strip().lower().replace("_", "-") != "executable":
            return self.config_help(prefix=prefix)
        if len(args) == 1:
            return self.config_status(root, prefix=prefix)
        if len(args) != 2:
            return self.config_help(prefix=prefix)
        value = args[1].strip()
        if value.lower() in {"auto", "default", "reset"}:
            write_section_value(self.config_section, "executable", None, root)
            message = "Enoch Codex executable reset to automatic discovery."
        else:
            candidate = resolve_codex_executable_value(value)
            if candidate.path is None:
                return f"Enoch could not set the Codex executable: {candidate.detail}"
            write_section_value(self.config_section, "executable", value, root)
            message = f"Enoch Codex executable set to {candidate.path}."
        return "\n\n".join([message, self.config_status(root, prefix=prefix)])

    def config_summary(self, root: Path) -> str:
        from enoch.brain import resolve_codex_executable

        resolution = resolve_codex_executable(root)
        return "\n".join(
            [
                f"Executable: {resolution.path or 'not found'}",
                f"Executable source: {resolution.source}",
            ]
        )

    def config_status(self, root: Path, *, prefix: str = "/") -> str:
        from enoch.brain import resolve_codex_executable

        command = f"{prefix}config"
        resolution = resolve_codex_executable(root)
        lines = [
            "Codex runtime executable:",
            f"- Executable: {resolution.path or 'not found'}",
            f"- Source: {resolution.source}",
        ]
        if resolution.detail:
            lines.append(f"- Detail: {resolution.detail}")
        lines.extend(
            [
                "",
                f"Set with {command} runtime codex executable <path>.",
                f"Reset with {command} runtime codex executable auto.",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def config_help(*, prefix: str = "/") -> str:
        command = f"{prefix}config"
        return "\n".join(
            [
                "Codex runtime config:",
                f"{command} runtime codex executable",
                f"{command} runtime codex executable <path>",
                f"{command} runtime codex executable auto",
            ]
        )


def create_provider(root: Path | None = None) -> CodexRuntime:
    return CodexRuntime(root)


ENOCH_PROVIDERS = (
    {
        "kind": "runtime",
        "name": "codex",
        "factory": create_provider,
        "default": True,
    },
)


def _codex_health(root: Path | None = None) -> ProviderHealth:
    from enoch.brain import resolve_codex_executable

    resolution = resolve_codex_executable(root)
    if resolution.path:
        return ProviderHealth(
            name="codex binary",
            passed=True,
            command="which codex",
            summary=f"{resolution.path} (source: {resolution.source})",
        )
    return ProviderHealth(
        name="codex binary",
        passed=False,
        command="which codex",
        output=" ".join(
            part
            for part in (
                "Codex binary was not found.",
                resolution.detail,
                "Set codex.executable in Enoch config, set ENOCH_CODEX_BIN, "
                "or install codex on PATH.",
            )
            if part
        ),
        summary=f"not found (source: {resolution.source})",
    )
