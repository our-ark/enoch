from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from our_ark_provider_kit import (
    AgentIdentity,
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    AgentRuntimeTimedOut,
    ProgressCallback,
    ProviderCapabilities,
    ProviderHealth,
    RuntimeEvent,
    RuntimeExecutionControl,
    RuntimeProgress,
    RuntimeResult,
    RuntimeSideEffect,
    RuntimeUsage,
)


DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_PROGRESS_INTERVAL_SECONDS = 30
HEALTH_TIMEOUT_SECONDS = 5
REASONING_EFFORTS = ("low", "medium", "high", "xhigh", "max")
READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "WebSearch", "WebFetch")
WORKSPACE_TOOLS = (*READ_ONLY_TOOLS, "Edit", "Write", "NotebookEdit", "Bash")
KNOWN_CLAUDE_PATHS = (
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)
SESSION_SCHEMA_VERSION = 1
_MAX_EVENT_TEXT = 1_000

SettingsReader = Callable[[Path | None], Mapping[str, str]]
SettingWriter = Callable[[str, str | None, Path | None], object]


@dataclass(frozen=True)
class ClaudeModelOption:
    slug: str
    display_name: str
    description: str = ""
    supported_reasoning_efforts: tuple[str, ...] = REASONING_EFFORTS


@dataclass(frozen=True)
class ClaudeExecutableResolution:
    path: str | None
    source: str
    configured_value: str = ""
    detail: str = ""


class ClaudeRuntimeError(AgentRuntimeError):
    """Raised when Claude Code cannot complete a runtime request."""


class ClaudeAccessUnavailable(ClaudeRuntimeError, AgentRuntimeAccessUnavailable):
    """Raised when Claude authentication, quota, or rate limits block a run."""


class ClaudeSessionUnavailable(ClaudeRuntimeError):
    """Raised when a stored Claude session can no longer be resumed."""


_LAST_USAGE: ContextVar[RuntimeUsage] = ContextVar(
    "our_ark_claude_usage",
    default=RuntimeUsage(),
)


class ClaudeSessionStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.RLock()

    def load(self, key: str) -> str:
        if not key or self.path is None:
            return ""
        with self._lock:
            sessions = self._read()
            value = sessions.get(key)
            return str(value or "").strip()

    def record(self, key: str, session_id: str) -> None:
        if not key or not session_id or self.path is None:
            return
        with self._lock:
            sessions = self._read()
            sessions[key] = session_id
            self._write(sessions)

    def forget(self, key: str) -> None:
        if not key or self.path is None:
            return
        with self._lock:
            sessions = self._read()
            if key not in sessions:
                return
            sessions.pop(key, None)
            self._write(sessions)

    def _read(self) -> dict[str, str]:
        if self.path is None:
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("schema_version") != SESSION_SCHEMA_VERSION:
            return {}
        raw = payload.get("sessions")
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in raw.items()
            if str(key).strip() and str(value).strip()
        }

    def _write(self, sessions: Mapping[str, str]) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": SESSION_SCHEMA_VERSION,
                    "sessions": dict(sorted(sessions.items())),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)


class ClaudeRuntime:
    name = "claude"
    provider_kind = "runtime"
    config_section = "claude"
    model_catalog_label = "Available Claude model aliases:"
    model_example = "sonnet"
    capabilities = ProviderCapabilities(
        provider_kind="runtime",
        capabilities=frozenset({"runtime.respond", "runtime.execute"}),
    )

    def __init__(
        self,
        root: Path | None = None,
        *,
        read_settings: SettingsReader | None = None,
        write_setting: SettingWriter | None = None,
        session_path: Path | None = None,
        env_prefix: str = "ENOCH",
        agent_name: str = "Enoch",
    ) -> None:
        self.root = Path(root).resolve() if root is not None else None
        self._read_settings = read_settings or (lambda _root=None: {})
        self._write_setting = write_setting
        self._sessions = ClaudeSessionStore(session_path)
        self._env_prefix = env_prefix.strip().upper() or "ENOCH"
        self._agent_name = agent_name.strip() or "the agent"

    def respond(
        self,
        identity: AgentIdentity,
        message: str,
        cwd: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        session_key: str = "",
        image_paths: Sequence[Path] = (),
        execution: RuntimeExecutionControl | None = None,
    ) -> RuntimeResult:
        control = _execution_control(
            execution,
            session_key=session_key,
            progress_callback=progress_callback,
        )
        return self._invoke(
            identity,
            message,
            cwd=cwd,
            sandbox="read-only",
            image_paths=image_paths,
            execution=control,
        )

    def act_in_session(
        self,
        identity: AgentIdentity,
        message: str,
        cwd: Path | None = None,
        progress_callback: ProgressCallback | None = None,
        sandbox: str = "workspace-write",
        session_key: str = "",
        cancellation_event: threading.Event | None = None,
        state_root: Path | None = None,
        execution: RuntimeExecutionControl | None = None,
    ) -> RuntimeResult:
        del state_root
        control = _execution_control(
            execution,
            session_key=session_key,
            cancellation_event=cancellation_event,
            progress_callback=progress_callback,
        )
        return self._invoke(
            identity,
            message,
            cwd=cwd,
            sandbox=sandbox or "workspace-write",
            execution=control,
        )

    def model_summary(self, root: Path | None = None) -> str:
        settings = self._settings(root)
        model, model_source = self._setting_with_source(
            settings,
            "model",
            f"{self._env_prefix}_CLAUDE_MODEL",
            "OUR_ARK_CLAUDE_MODEL",
        )
        effort, effort_source = self._setting_with_source(
            settings,
            "reasoning_effort",
            f"{self._env_prefix}_CLAUDE_REASONING_EFFORT",
            "OUR_ARK_CLAUDE_REASONING_EFFORT",
        )
        lines = [
            f"AI model: {model or 'Claude CLI default'}",
            f"Model source: {model_source or 'Claude CLI default'}",
            f"Reasoning effort: {effort or 'Claude CLI default'}",
        ]
        if effort_source:
            lines.append(f"Reasoning source: {effort_source}")
        return "\n".join(lines)

    def model_options(self) -> tuple[ClaudeModelOption, ...]:
        return (
            ClaudeModelOption(
                slug="sonnet",
                display_name="Sonnet",
                description="latest balanced Claude model",
            ),
            ClaudeModelOption(
                slug="opus",
                display_name="Opus",
                description="latest high-capability Claude model",
            ),
            ClaudeModelOption(
                slug="fable",
                display_name="Fable",
                description="latest fast Claude model alias when available",
            ),
        )

    def reasoning_efforts(self, root: Path | None = None) -> tuple[str, ...]:
        del root
        return REASONING_EFFORTS

    def reset_usage(self) -> None:
        _LAST_USAGE.set(RuntimeUsage())

    def health(self, root: Path | None = None) -> ProviderHealth:
        resolution = self.resolve_executable(root)
        if resolution.path is None:
            return ProviderHealth(
                name="Claude runtime",
                passed=False,
                command="claude auth status",
                output=resolution.detail,
                summary="Claude Code CLI was not found.",
            )
        command = [resolution.path, "auth", "status"]
        try:
            result = subprocess.run(
                command,
                cwd=str(root or self.root or Path.cwd()),
                text=True,
                capture_output=True,
                timeout=HEALTH_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return ProviderHealth(
                name="Claude runtime",
                passed=False,
                command=" ".join(command),
                output=str(error),
                summary="Claude authentication status could not be checked.",
            )
        output = result.stdout.strip() or result.stderr.strip()
        logged_in = False
        try:
            payload = json.loads(result.stdout)
            logged_in = bool(payload.get("loggedIn")) if isinstance(payload, dict) else False
        except json.JSONDecodeError:
            logged_in = result.returncode == 0
        return ProviderHealth(
            name="Claude runtime",
            passed=result.returncode == 0 and logged_in,
            command=" ".join(command),
            output=_clip(output),
            summary=(
                f"Claude Code is authenticated ({resolution.source})."
                if result.returncode == 0 and logged_in
                else "Claude Code is installed but not authenticated. Run `claude auth login`."
            ),
        )

    def configure(
        self,
        args: tuple[str, ...],
        root: Path,
        *,
        prefix: str = "/",
    ) -> str:
        if not args:
            return self.config_status(root, prefix=prefix)
        setting = args[0].strip().lower().replace("_", "-")
        if setting in {"help", "-h", "--help"}:
            return self.config_help(prefix=prefix)
        if setting == "executable":
            if len(args) == 1:
                return self.config_status(root, prefix=prefix)
            if len(args) != 2:
                return self.config_help(prefix=prefix)
            value = args[1].strip()
            if value.lower() in {"auto", "default", "reset"}:
                self._write("executable", None, root)
                message = f"{self._agent_name} Claude executable reset to automatic discovery."
            else:
                resolution = _resolve_executable_value(value)
                if resolution.path is None:
                    return f"Could not set the Claude executable: {resolution.detail}"
                self._write("executable", value, root)
                message = f"{self._agent_name} Claude executable set to {resolution.path}."
            return "\n\n".join([message, self.config_status(root, prefix=prefix)])
        if setting in {"max-budget", "budget"}:
            if len(args) == 1:
                return self.config_status(root, prefix=prefix)
            if len(args) != 2:
                return self.config_help(prefix=prefix)
            value = args[1].strip().lower()
            if value in {"off", "none", "default", "reset"}:
                self._write("max_budget_usd", None, root)
                message = f"{self._agent_name} Claude invocation budget limit cleared."
            else:
                try:
                    budget = float(value)
                except ValueError:
                    return "Claude max budget must be a positive USD amount or off."
                if budget <= 0:
                    return "Claude max budget must be a positive USD amount or off."
                rendered = format(budget, ".6g")
                self._write("max_budget_usd", rendered, root)
                message = f"{self._agent_name} Claude invocation budget set to ${rendered}."
            return "\n\n".join([message, self.config_status(root, prefix=prefix)])
        return self.config_help(prefix=prefix)

    def config_summary(self, root: Path) -> str:
        resolution = self.resolve_executable(root)
        budget = self._max_budget(root)
        return "\n".join(
            [
                f"Executable: {resolution.path or 'not found'}",
                f"Executable source: {resolution.source}",
                f"Per-invocation budget: {f'${budget:g}' if budget is not None else 'not limited'}",
                "Execution policy: restricted tools; permission bypass disabled",
            ]
        )

    def config_status(self, root: Path, *, prefix: str = "/") -> str:
        command = f"{prefix}config"
        return "\n".join(
            [
                "Claude runtime config:",
                self.config_summary(root),
                "",
                f"Set the CLI with {command} runtime claude executable <path>.",
                f"Reset discovery with {command} runtime claude executable auto.",
                f"Set a per-invocation limit with {command} runtime claude max-budget <usd>.",
                f"Clear it with {command} runtime claude max-budget off.",
                "Authenticate separately with `claude auth login`.",
            ]
        )

    @staticmethod
    def config_help(*, prefix: str = "/") -> str:
        command = f"{prefix}config"
        return "\n".join(
            [
                "Claude runtime config:",
                f"{command} runtime claude",
                f"{command} runtime claude executable <path|auto>",
                f"{command} runtime claude max-budget <usd|off>",
            ]
        )

    def resolve_executable(self, root: Path | None = None) -> ClaudeExecutableResolution:
        settings = self._settings(root)
        for environment_name in (
            f"{self._env_prefix}_CLAUDE_BIN",
            "OUR_ARK_CLAUDE_BIN",
            "CLAUDE_BIN",
        ):
            value = os.environ.get(environment_name, "").strip()
            if value:
                return _resolve_executable_value(value, source=environment_name)
        configured = str(settings.get("executable") or "").strip()
        if configured:
            return _resolve_executable_value(
                configured,
                source=f"agent config {self.config_section}.executable",
            )
        discovered = shutil.which("claude")
        if discovered:
            return ClaudeExecutableResolution(discovered, "PATH")
        for candidate in KNOWN_CLAUDE_PATHS:
            path = Path(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                return ClaudeExecutableResolution(str(path), "known install path")
        return ClaudeExecutableResolution(
            None,
            "automatic discovery",
            detail=(
                "Set the provider executable with `/config runtime claude executable <path>` "
                "or expose `claude` on PATH."
            ),
        )

    def _invoke(
        self,
        identity: AgentIdentity,
        message: str,
        *,
        cwd: Path | None,
        sandbox: str,
        image_paths: Sequence[Path] = (),
        execution: RuntimeExecutionControl,
    ) -> RuntimeResult:
        del identity
        execution.raise_if_stopped()
        work_root = Path(cwd or self.root or Path.cwd()).resolve()
        resolution = self.resolve_executable(self.root)
        if resolution.path is None:
            raise ClaudeRuntimeError(
                "The Claude Code CLI could not be found. " + resolution.detail
            )
        session_key = execution.session_key
        session_id = self._sessions.load(session_key)
        try:
            result = self._run_once(
                resolution.path,
                message,
                cwd=work_root,
                sandbox=sandbox,
                session_id=session_id,
                persistent=bool(session_key),
                image_paths=image_paths,
                execution=execution,
            )
        except ClaudeSessionUnavailable:
            if not session_id:
                raise
            self._sessions.forget(session_key)
            result = self._run_once(
                resolution.path,
                message,
                cwd=work_root,
                sandbox=sandbox,
                session_id="",
                persistent=True,
                image_paths=image_paths,
                execution=execution,
            )
        execution.raise_if_stopped()
        if session_key and result.session_id:
            self._sessions.record(session_key, result.session_id)
        _LAST_USAGE.set(result.usage)
        return result

    def _run_once(
        self,
        executable: str,
        message: str,
        *,
        cwd: Path,
        sandbox: str,
        session_id: str,
        persistent: bool,
        image_paths: Sequence[Path],
        execution: RuntimeExecutionControl,
    ) -> RuntimeResult:
        args = self._execution_args(
            executable,
            sandbox=sandbox,
            session_id=session_id,
            persistent=persistent,
            image_paths=image_paths,
        )
        prompt = _human_prompt(message, image_paths)
        stdout, stderr, returncode, payloads = _run_streaming_process(
            args,
            prompt,
            cwd=cwd,
            sandbox=sandbox,
            execution=execution,
        )
        parsed = _parse_result(payloads)
        if returncode != 0 or parsed.error:
            detail = parsed.error or stderr.strip() or _non_json_output(stdout)
            detail = detail or f"Claude Code exited with status {returncode}."
            if _session_unavailable(detail):
                raise ClaudeSessionUnavailable(_clip(detail))
            if _access_unavailable(detail, parsed.subtype):
                raise ClaudeAccessUnavailable(_access_message(detail))
            raise ClaudeRuntimeError(f"Claude Code did not answer successfully: {_clip(detail)}")
        if not parsed.final_text.strip():
            raise ClaudeRuntimeError("Claude Code completed without a final response.")
        return RuntimeResult(
            final_text=parsed.final_text.strip(),
            session_id=parsed.session_id or session_id,
            completion_reason=_completion_reason(parsed.subtype),
            usage=parsed.usage,
            events=tuple(_runtime_event(payload) for payload in payloads),
            side_effects=parsed.side_effects,
        )

    def _execution_args(
        self,
        executable: str,
        *,
        sandbox: str,
        session_id: str,
        persistent: bool,
        image_paths: Sequence[Path],
    ) -> list[str]:
        workspace_write = sandbox.strip().lower() not in {"read-only", "readonly", "read"}
        tools = WORKSPACE_TOOLS if workspace_write else READ_ONLY_TOOLS
        args = [
            executable,
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--safe-mode",
            "--restricted",
            "--strict-mcp-config",
            "--no-chrome",
            "--disable-slash-commands",
            "--permission-mode",
            "dontAsk" if workspace_write else "plan",
            "--tools",
            ",".join(tools),
        ]
        if workspace_write:
            args.extend(["--allowedTools", ",".join(tools)])
        if session_id:
            args.extend(["--resume", session_id])
        elif not persistent:
            args.append("--no-session-persistence")
        settings = self._settings(self.root)
        model, _ = self._setting_with_source(
            settings,
            "model",
            f"{self._env_prefix}_CLAUDE_MODEL",
            "OUR_ARK_CLAUDE_MODEL",
        )
        effort, _ = self._setting_with_source(
            settings,
            "reasoning_effort",
            f"{self._env_prefix}_CLAUDE_REASONING_EFFORT",
            "OUR_ARK_CLAUDE_REASONING_EFFORT",
        )
        if model:
            args.extend(["--model", model])
        if effort:
            if effort not in REASONING_EFFORTS:
                raise ClaudeRuntimeError(
                    f"Claude reasoning effort must be one of: {', '.join(REASONING_EFFORTS)}."
                )
            args.extend(["--effort", effort])
        budget = self._max_budget(self.root)
        if budget is not None:
            args.extend(["--max-budget-usd", f"{budget:g}"])
        image_directories = dict.fromkeys(
            str(Path(path).expanduser().resolve().parent)
            for path in image_paths
        )
        for directory in image_directories:
            args.extend(["--add-dir", directory])
        return args

    def _settings(self, root: Path | None) -> Mapping[str, str]:
        return self._read_settings(root or self.root)

    def _setting_with_source(
        self,
        settings: Mapping[str, str],
        key: str,
        *environment_names: str,
    ) -> tuple[str, str]:
        for name in environment_names:
            value = os.environ.get(name, "").strip()
            if value:
                return value, name
        value = str(settings.get(key) or "").strip()
        if value:
            return value, f"agent config {self.config_section}.{key}"
        return "", ""

    def _max_budget(self, root: Path | None) -> float | None:
        settings = self._settings(root)
        value, _ = self._setting_with_source(
            settings,
            "max_budget_usd",
            f"{self._env_prefix}_CLAUDE_MAX_BUDGET_USD",
            "OUR_ARK_CLAUDE_MAX_BUDGET_USD",
        )
        if not value:
            return None
        try:
            budget = float(value)
        except ValueError as error:
            raise ClaudeRuntimeError("Claude max budget must be a positive USD amount.") from error
        if budget <= 0:
            raise ClaudeRuntimeError("Claude max budget must be a positive USD amount.")
        return budget

    def _write(self, key: str, value: str | None, root: Path) -> None:
        if self._write_setting is None:
            raise ClaudeRuntimeError("This Claude provider cannot write agent configuration.")
        self._write_setting(key, value, root)


@dataclass(frozen=True)
class _ParsedClaudeResult:
    final_text: str
    session_id: str
    subtype: str
    usage: RuntimeUsage
    side_effects: tuple[RuntimeSideEffect, ...]
    error: str = ""


def _parse_result(payloads: Sequence[dict[str, Any]]) -> _ParsedClaudeResult:
    session_id = ""
    result_payload: dict[str, Any] | None = None
    assistant_text = ""
    effects: list[RuntimeSideEffect] = []
    seen_effects: set[tuple[str, str]] = set()
    for payload in payloads:
        native_session = str(payload.get("session_id") or "").strip()
        if native_session:
            session_id = native_session
        event_type = str(payload.get("type") or "").strip()
        if event_type == "assistant":
            message = payload.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    text = "".join(
                        str(item.get("text") or "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ).strip()
                    if text:
                        assistant_text = text
                    for item in content:
                        effect = _side_effect(item)
                        if effect is None:
                            continue
                        key = (effect.kind, effect.reference)
                        if key not in seen_effects:
                            seen_effects.add(key)
                            effects.append(effect)
        if event_type == "result":
            result_payload = payload
    result_payload = result_payload or {}
    final_value = result_payload.get("result")
    final_text = final_value if isinstance(final_value, str) else assistant_text
    subtype = str(result_payload.get("subtype") or "success").strip()
    is_error = bool(result_payload.get("is_error")) or subtype.startswith("error")
    error = final_text if is_error else ""
    return _ParsedClaudeResult(
        final_text=final_text,
        session_id=str(result_payload.get("session_id") or session_id).strip(),
        subtype=subtype,
        usage=_runtime_usage(result_payload),
        side_effects=tuple(effects),
        error=error,
    )


def _runtime_usage(payload: Mapping[str, Any]) -> RuntimeUsage:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        raw = _aggregate_model_usage(payload.get("modelUsage"))
    input_tokens = _integer(raw.get("input_tokens"))
    cache_creation = _integer(raw.get("cache_creation_input_tokens"))
    cache_read = _integer(raw.get("cache_read_input_tokens"))
    return RuntimeUsage(
        input_tokens=input_tokens + cache_creation + cache_read,
        cached_input_tokens=cache_read,
        output_tokens=_integer(raw.get("output_tokens")),
        reasoning_tokens=max(
            _integer(raw.get("thinking_tokens")),
            _integer(raw.get("reasoning_tokens")),
        ),
    )


def _aggregate_model_usage(value: object) -> dict[str, int]:
    totals: dict[str, int] = {}
    if not isinstance(value, dict):
        return totals
    for model in value.values():
        if not isinstance(model, dict):
            continue
        for key in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "output_tokens",
            "thinking_tokens",
            "reasoning_tokens",
        ):
            totals[key] = totals.get(key, 0) + _integer(model.get(key))
    return totals


def _side_effect(item: object) -> RuntimeSideEffect | None:
    if not isinstance(item, dict) or item.get("type") != "tool_use":
        return None
    tool = str(item.get("name") or "").strip()
    tool_input = item.get("input")
    if not isinstance(tool_input, dict):
        return None
    if tool in {"Edit", "Write", "NotebookEdit"}:
        reference = str(
            tool_input.get("file_path")
            or tool_input.get("notebook_path")
            or ""
        ).strip()
        if reference:
            return RuntimeSideEffect(
                kind="file",
                reference=reference,
                metadata={"tool": tool},
            )
    return None


def _runtime_event(payload: Mapping[str, Any]) -> RuntimeEvent:
    event_type = str(payload.get("type") or "claude.event").strip() or "claude.event"
    data: dict[str, Any] = {}
    for key in ("subtype", "is_error", "duration_ms", "duration_api_ms", "num_turns"):
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)):
            data[key] = value
    message = payload.get("message")
    if isinstance(message, dict):
        data["role"] = str(message.get("role") or "")
        content = message.get("content")
        if isinstance(content, list):
            tools = [
                str(item.get("name") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "tool_use"
            ]
            if tools:
                data["tools"] = tools
    return RuntimeEvent(type=event_type, data=data)


def _run_streaming_process(
    args: Sequence[str],
    prompt: str,
    *,
    cwd: Path,
    sandbox: str,
    execution: RuntimeExecutionControl,
) -> tuple[str, str, int, tuple[dict[str, Any], ...]]:
    execution.raise_if_stopped()
    try:
        process = subprocess.Popen(
            list(args),
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except OSError as error:
        raise ClaudeRuntimeError(f"Could not start Claude Code: {error}") from error
    queue: Queue[tuple[str, str | None]] = Queue()
    readers = (
        threading.Thread(
            target=_read_stream,
            args=("stdout", process.stdout, queue),
            daemon=True,
        ),
        threading.Thread(
            target=_read_stream,
            args=("stderr", process.stderr, queue),
            daemon=True,
        ),
    )
    for reader in readers:
        reader.start()
    try:
        if process.stdin is None:
            raise ClaudeRuntimeError("Claude Code stdin was not available.")
        try:
            process.stdin.write(prompt)
        except BrokenPipeError:
            pass
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        payloads: list[dict[str, Any]] = []
        open_streams = {"stdout", "stderr"}
        last_progress = execution.started_at_monotonic
        while open_streams or process.poll() is None:
            try:
                execution.raise_if_stopped()
                if (
                    execution.timeout_seconds is None
                    and time.monotonic() - execution.started_at_monotonic >= DEFAULT_TIMEOUT_SECONDS
                ):
                    raise AgentRuntimeTimedOut("Claude Code execution timed out.")
            except (AgentRuntimeCancelled, AgentRuntimeTimedOut):
                _stop_process(process)
                raise
            now = time.monotonic()
            if now - last_progress >= DEFAULT_PROGRESS_INTERVAL_SECONDS:
                execution.emit_progress(
                    RuntimeProgress(
                        elapsed_seconds=int(now - execution.started_at_monotonic),
                        stage="running",
                        sandbox=sandbox,
                    )
                )
                last_progress = now
            try:
                stream_name, line = queue.get(timeout=0.1)
            except Empty:
                continue
            if line is None:
                open_streams.discard(stream_name)
                continue
            if stream_name == "stderr":
                stderr_lines.append(line)
                continue
            stdout_lines.append(line)
            payload = _json_payload(line)
            if payload is None:
                continue
            payloads.append(payload)
            progress = _progress_from_payload(
                payload,
                elapsed_seconds=int(time.monotonic() - execution.started_at_monotonic),
                sandbox=sandbox,
            )
            if progress is not None:
                execution.emit_progress(progress)
                last_progress = time.monotonic()
        returncode = process.wait(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
        return (
            "".join(stdout_lines),
            "".join(stderr_lines),
            returncode,
            tuple(payloads),
        )
    except BaseException:
        _stop_process(process)
        raise
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def _read_stream(
    name: str,
    stream: Any,
    output: Queue[tuple[str, str | None]],
) -> None:
    if stream is None:
        output.put((name, None))
        return
    try:
        for line in stream:
            output.put((name, line))
    finally:
        output.put((name, None))


def _progress_from_payload(
    payload: Mapping[str, Any],
    *,
    elapsed_seconds: int,
    sandbox: str,
) -> RuntimeProgress | None:
    event_type = str(payload.get("type") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    if event_type == "system" and payload.get("subtype") == "init":
        model = str(payload.get("model") or "").strip()
        return RuntimeProgress(
            elapsed_seconds=elapsed_seconds,
            stage="started",
            message=f"Claude started{f' with {model}' if model else ''}.",
            sandbox=sandbox,
            session_id=session_id,
        )
    if event_type == "assistant":
        message = payload.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        tools = [
            str(item.get("name") or "").strip()
            for item in content or ()
            if isinstance(item, dict) and item.get("type") == "tool_use"
        ]
        return RuntimeProgress(
            elapsed_seconds=elapsed_seconds,
            stage="working",
            message=f"Claude is using {tools[-1]}." if tools else "Claude is working.",
            sandbox=sandbox,
            session_id=session_id,
        )
    if event_type == "result":
        return RuntimeProgress(
            elapsed_seconds=elapsed_seconds,
            stage="completed" if not payload.get("is_error") else "failed",
            message="Claude finished the runtime turn.",
            sandbox=sandbox,
            session_id=session_id,
        )
    return None


def _execution_control(
    execution: RuntimeExecutionControl | None,
    *,
    session_key: str = "",
    cancellation_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> RuntimeExecutionControl:
    if execution is None:
        typed_progress = None
        if progress_callback is not None:
            typed_progress = lambda progress: progress_callback(
                progress.elapsed_seconds,
                progress.sandbox,
            )
        return RuntimeExecutionControl(
            session_key=session_key,
            cancellation_event=cancellation_event,
            progress_callback=typed_progress,
        )
    updates: dict[str, Any] = {}
    if not execution.session_key and session_key:
        updates["session_key"] = session_key
    if execution.cancellation_event is None and cancellation_event is not None:
        updates["cancellation_event"] = cancellation_event
    if execution.progress_callback is None and progress_callback is not None:
        updates["progress_callback"] = lambda progress: progress_callback(
            progress.elapsed_seconds,
            progress.sandbox,
        )
    return replace(execution, **updates) if updates else execution


def _resolve_executable_value(
    value: str,
    *,
    source: str = "configured value",
) -> ClaudeExecutableResolution:
    cleaned = value.strip()
    if not cleaned:
        return ClaudeExecutableResolution(None, source, detail="Executable value is empty.")
    expanded = Path(cleaned).expanduser()
    looks_like_path = expanded.is_absolute() or os.sep in cleaned
    resolved = str(expanded.resolve()) if looks_like_path else shutil.which(cleaned)
    if not resolved:
        return ClaudeExecutableResolution(
            None,
            source,
            configured_value=cleaned,
            detail=f"{cleaned!r} is not an executable file or command on PATH.",
        )
    path = Path(resolved)
    if not path.is_file() or not os.access(path, os.X_OK):
        return ClaudeExecutableResolution(
            None,
            source,
            configured_value=cleaned,
            detail=f"{path} is not an executable file.",
        )
    return ClaudeExecutableResolution(
        str(path),
        source,
        configured_value=cleaned,
    )


def _human_prompt(message: str, image_paths: Sequence[Path]) -> str:
    lines = ["Human message:", str(message)]
    if image_paths:
        lines.extend(
            [
                "",
                "Local image files attached to this request:",
                *(f"- {Path(path).expanduser().resolve()}" for path in image_paths),
                "Inspect these files with the Read tool when they are relevant.",
            ]
        )
    return "\n".join(lines)


def _json_payload(line: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _non_json_output(stdout: str) -> str:
    return "\n".join(
        line.strip()
        for line in stdout.splitlines()
        if line.strip() and _json_payload(line) is None
    )


def _completion_reason(subtype: str) -> str:
    cleaned = subtype.strip().lower().replace("_", "-")
    return "completed" if cleaned in {"", "success"} else cleaned


def _session_unavailable(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        pattern in lowered
        for pattern in (
            "no conversation found",
            "session not found",
            "unknown session",
            "invalid session id",
        )
    )


def _access_unavailable(detail: str, subtype: str = "") -> bool:
    lowered = f"{subtype} {detail}".lower()
    return any(
        pattern in lowered
        for pattern in (
            "not logged in",
            "auth login",
            "authentication",
            "oauth token",
            "api key",
            "credit balance",
            "billing",
            "rate limit",
            "rate_limit",
            "usage limit",
            "quota",
            "overloaded",
            "max budget",
            "max_budget",
            "429",
        )
    )


def _access_message(detail: str) -> str:
    lowered = detail.lower()
    if any(pattern in lowered for pattern in ("not logged in", "auth login", "oauth", "api key")):
        return (
            "Claude runtime access is unavailable. Authenticate with "
            "`claude auth login`, then resume the task."
        )
    return f"Claude runtime access is temporarily unavailable: {_clip(detail)}"


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _clip(value: str, limit: int = _MAX_EVENT_TEXT) -> str:
    cleaned = " ".join(str(value).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip() + "..."


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
