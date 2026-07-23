from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from ast import literal_eval
from typing import Callable, Sequence

from enoch.codex_sessions import (
    CodexSessionState,
    forget_codex_session,
    load_codex_session,
    record_codex_session_turn,
)
from enoch.config import read_section
from enoch.identity import Identity
from enoch.last_codex_input import record_last_codex_input
from enoch.memory.prompt import memory_for_prompt
from enoch.prompt_append import startup_context_note
from enoch.providers.contracts import (
    AgentRuntimeAccessUnavailable,
    AgentRuntimeCancelled,
    AgentRuntimeError,
    AgentRuntimeTimedOut,
    RuntimeEvent,
    RuntimeExecutionControl,
    RuntimeProgress,
    RuntimeResult,
    RuntimeUsage,
)
from enoch.runtime import ACTION_SANDBOX_READ_ONLY, WORKSPACE_WRITE_SANDBOX
from enoch.tasks.config import task_timeout_seconds


DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_PROGRESS_INTERVAL_SECONDS = 60
MODEL_CATALOG_TIMEOUT_SECONDS = 5
DEFAULT_CODEX_PATHS = [
    "/Applications/ChatGPT.app/Contents/Resources/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
]
REASONING_EFFORTS = {"low", "medium", "high"}
ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)


@dataclass(frozen=True)
class CodexModelOption:
    slug: str
    display_name: str
    description: str = ""


@dataclass(frozen=True)
class CodexExecutableResolution:
    path: str | None
    source: str
    configured_value: str = ""
    detail: str = ""


_TOKEN_USAGE: ContextVar[TokenUsage] = ContextVar("enoch_token_usage", default=TokenUsage())


class BrainError(AgentRuntimeError):
    """Raised when Enoch cannot reach her Codex brain."""


class BrainCancelled(BrainError, AgentRuntimeCancelled):
    """Raised when Enoch's human cancels an active Codex run."""


class BrainTimedOut(BrainError, AgentRuntimeTimedOut):
    """Raised when an active Codex run exceeds its execution deadline."""


class CodexAccessUnavailable(BrainError, AgentRuntimeAccessUnavailable):
    """Raised when Codex authentication, quota, or rate limits block a run."""


def reset_token_usage() -> None:
    _TOKEN_USAGE.set(TokenUsage())


def token_usage() -> TokenUsage:
    return _TOKEN_USAGE.get()


def token_usage_line() -> str:
    usage = token_usage()
    lines = [
        f"Input tokens: {usage.input_tokens}",
        f"Cached input tokens: {usage.cached_input_tokens}",
        f"Uncached input tokens: {usage.uncached_input_tokens}",
    ]
    if usage.output_tokens:
        lines.append(f"Output tokens: {usage.output_tokens}")
    if usage.reasoning_output_tokens:
        lines.append(f"Reasoning output tokens: {usage.reasoning_output_tokens}")
    return "\n".join(lines)


def update_token_usage(
    *,
    input_tokens: int = 0,
    cached_input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_output_tokens: int = 0,
) -> None:
    _TOKEN_USAGE.set(
        TokenUsage(
            input_tokens=max(0, input_tokens),
            cached_input_tokens=max(0, cached_input_tokens),
            output_tokens=max(0, output_tokens),
            reasoning_output_tokens=max(0, reasoning_output_tokens),
        )
    )


def model_summary(root: Path | None = None) -> str:
    env_model = os.environ.get("ENOCH_CODEX_MODEL", "").strip()
    env_reasoning = os.environ.get("ENOCH_CODEX_REASONING_EFFORT", "").strip()
    enoch_model = _enoch_model(root)
    enoch_reasoning = _enoch_reasoning_effort(root)
    config_path = _codex_config_path()
    config = _read_codex_config(config_path)
    config_model = config.get("model", "")
    config_reasoning = config.get("model_reasoning_effort", "")

    if env_model:
        model = env_model
        model_source = "ENOCH_CODEX_MODEL"
    elif enoch_model:
        model = enoch_model
        model_source = "Enoch config codex.model"
    elif config_model:
        model = config_model
        model_source = str(config_path)
    else:
        model = "Codex CLI default"
        model_source = (
            "Codex, because ENOCH_CODEX_MODEL, Enoch config codex.model, "
            "and Codex config model are not set"
        )

    lines = [
        f"AI model: {model}",
        f"Model source: {model_source}",
    ]
    if env_reasoning:
        lines.extend(
            [
                f"Reasoning effort: {env_reasoning}",
                "Reasoning source: ENOCH_CODEX_REASONING_EFFORT",
            ]
        )
    elif enoch_reasoning:
        lines.extend(
            [
                f"Reasoning effort: {enoch_reasoning}",
                "Reasoning source: Enoch config codex.reasoning_effort",
            ]
        )
    elif config_reasoning:
        lines.extend(
            [
                f"Reasoning effort: {config_reasoning}",
                f"Reasoning source: {config_path}",
            ]
        )
    else:
        lines.append("Reasoning effort: Codex CLI default")
    if env_model and config_model:
        lines.append(f"Codex config model: {config_model}")
    return "\n".join(lines)


def codex_model_options(root: Path | None = None) -> tuple[CodexModelOption, ...]:
    codex = _codex_binary(root)
    if codex is None:
        return ()
    try:
        result = subprocess.run(
            [codex, "debug", "models", "--bundled"],
            text=True,
            capture_output=True,
            timeout=MODEL_CATALOG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return ()
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return ()
    options = []
    seen = set()
    for raw in models:
        if not isinstance(raw, dict) or raw.get("visibility") != "list":
            continue
        slug = str(raw.get("slug") or "").strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        options.append(
            CodexModelOption(
                slug=slug,
                display_name=str(raw.get("display_name") or slug).strip() or slug,
                description=str(raw.get("description") or "").strip(),
            )
        )
    return tuple(options)


def _codex_config_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home) / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _read_codex_config(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            break
        key, separator, value = stripped.partition("=")
        if not separator:
            continue
        key = key.strip()
        if key in {"model", "model_reasoning_effort"}:
            values[key] = _parse_toml_string(value.strip())
    return values


def _parse_toml_string(value: str) -> str:
    try:
        parsed = literal_eval(value)
    except (SyntaxError, ValueError):
        return value.strip().strip('"').strip("'")
    return parsed if isinstance(parsed, str) else str(parsed)


def respond(
    identity: Identity,
    message: str,
    cwd: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    session_key: str = "",
    image_paths: Sequence[Path] = (),
    execution: RuntimeExecutionControl | None = None,
) -> str:
    return respond_result(
        identity,
        message,
        cwd,
        progress_callback=progress_callback,
        session_key=session_key,
        image_paths=image_paths,
        execution=execution,
    ).final_text


def respond_result(
    identity: Identity,
    message: str,
    cwd: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    session_key: str = "",
    image_paths: Sequence[Path] = (),
    execution: RuntimeExecutionControl | None = None,
) -> RuntimeResult:
    control = _runtime_execution_control(
        execution,
        session_key=session_key,
        progress_callback=progress_callback,
    )
    if control.session_key:
        return _respond_with_persistent_session_result(
            identity,
            message,
            cwd,
            session_key=control.session_key,
            image_paths=image_paths,
            execution=control,
        )
    return _run_codex_result(
        identity,
        _build_persistent_human_message(message),
        cwd,
        sandbox=ACTION_SANDBOX_READ_ONLY,
        image_paths=image_paths,
        execution=control,
    )


def _respond_with_persistent_session_result(
    identity: Identity,
    message: str,
    cwd: Path | None,
    *,
    session_key: str,
    image_paths: Sequence[Path] = (),
    execution: RuntimeExecutionControl,
) -> RuntimeResult:
    state = load_codex_session(session_key, cwd)
    prompt = _build_persistent_prompt(message, cwd, state)
    try:
        result = _run_codex_result(
            identity,
            prompt,
            cwd,
            sandbox=ACTION_SANDBOX_READ_ONLY,
            persist_session=True,
            session_id=state.session_id if state else "",
            image_paths=image_paths,
            execution=execution,
        )
    except (BrainCancelled, BrainTimedOut, CodexAccessUnavailable):
        raise
    except BrainError:
        if state is None:
            raise
        forget_codex_session(session_key, cwd)
        recovery_prompt = _build_persistent_recovery_prompt(message, cwd)
        result = _run_codex_result(
            identity,
            recovery_prompt,
            cwd,
            sandbox=ACTION_SANDBOX_READ_ONLY,
            persist_session=True,
            image_paths=image_paths,
            execution=execution,
        )
        state = None

    if result.session_id:
        record_codex_session_turn(
            session_key,
            result.session_id,
            cwd,
            previous=state,
        )
    return result


def act_in_session(
    identity: Identity,
    message: str,
    cwd: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    sandbox: str = WORKSPACE_WRITE_SANDBOX,
    session_key: str = "",
    cancellation_event: threading.Event | None = None,
    state_root: Path | None = None,
    execution: RuntimeExecutionControl | None = None,
) -> str:
    return act_in_session_result(
        identity,
        message,
        cwd,
        progress_callback=progress_callback,
        sandbox=sandbox,
        session_key=session_key,
        cancellation_event=cancellation_event,
        state_root=state_root,
        execution=execution,
    ).final_text


def act_in_session_result(
    identity: Identity,
    message: str,
    cwd: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    sandbox: str = WORKSPACE_WRITE_SANDBOX,
    session_key: str = "",
    cancellation_event: threading.Event | None = None,
    state_root: Path | None = None,
    execution: RuntimeExecutionControl | None = None,
) -> RuntimeResult:
    control = _runtime_execution_control(
        execution,
        session_key=session_key,
        cancellation_event=cancellation_event,
        progress_callback=progress_callback,
    )
    if not control.session_key:
        return act_result(
            identity,
            message,
            cwd=cwd,
            sandbox=sandbox,
            state_root=state_root,
            execution=control,
        )
    return _act_with_persistent_session_result(
        identity,
        message,
        cwd,
        sandbox=sandbox,
        session_key=control.session_key,
        state_root=state_root,
        execution=control,
    )


def _act_with_persistent_session_result(
    identity: Identity,
    message: str,
    cwd: Path | None,
    *,
    sandbox: str,
    session_key: str,
    state_root: Path | None = None,
    execution: RuntimeExecutionControl,
) -> RuntimeResult:
    state_root = state_root or cwd
    state = load_codex_session(session_key, state_root)
    prompt = _build_persistent_prompt(message, state_root, state)
    try:
        result = _run_codex_result(
            identity,
            prompt,
            cwd,
            sandbox=sandbox,
            persist_session=True,
            session_id=state.session_id if state else "",
            state_root=state_root,
            execution=execution,
        )
    except (BrainCancelled, BrainTimedOut, CodexAccessUnavailable):
        raise
    except BrainError:
        if state is None:
            raise
        forget_codex_session(session_key, state_root)
        recovery_prompt = _build_persistent_recovery_prompt(message, state_root)
        result = _run_codex_result(
            identity,
            recovery_prompt,
            cwd,
            sandbox=sandbox,
            persist_session=True,
            state_root=state_root,
            execution=execution,
        )
        state = None

    if result.session_id:
        record_codex_session_turn(
            session_key,
            result.session_id,
            state_root,
            previous=state,
        )
    return result


def _build_persistent_prompt(
    message: str,
    root: Path | None,
    state: CodexSessionState | None,
) -> str:
    if state is None:
        return _build_persistent_startup_message(message, root)
    return _build_persistent_human_message(message)


def _build_persistent_human_message(message: str) -> str:
    return f"Human message:\n{message}"


def _build_persistent_recovery_prompt(message: str, root: Path | None) -> str:
    return _build_persistent_startup_message(message, root)


def _build_persistent_startup_message(message: str, root: Path | None) -> str:
    return "\n\n".join(
        [
            startup_context_note(memory_for_prompt(root)),
            "Human message:",
            message,
        ]
    )


def act(
    identity: Identity,
    message: str,
    cwd: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    sandbox: str = WORKSPACE_WRITE_SANDBOX,
    cancellation_event: threading.Event | None = None,
    state_root: Path | None = None,
    execution: RuntimeExecutionControl | None = None,
) -> str:
    return act_result(
        identity,
        message,
        cwd,
        progress_callback=progress_callback,
        sandbox=sandbox,
        cancellation_event=cancellation_event,
        state_root=state_root,
        execution=execution,
    ).final_text


def act_result(
    identity: Identity,
    message: str,
    cwd: Path | None = None,
    progress_callback: ProgressCallback | None = None,
    sandbox: str = WORKSPACE_WRITE_SANDBOX,
    cancellation_event: threading.Event | None = None,
    state_root: Path | None = None,
    execution: RuntimeExecutionControl | None = None,
) -> RuntimeResult:
    control = _runtime_execution_control(
        execution,
        cancellation_event=cancellation_event,
        progress_callback=progress_callback,
    )
    return _run_codex_result(
        identity,
        _build_persistent_human_message(message),
        cwd,
        sandbox=sandbox,
        state_root=state_root,
        execution=control,
    )


def _run_codex(
    identity: Identity,
    prompt: str,
    cwd: Path | None,
    sandbox: str,
    progress_callback: ProgressCallback | None = None,
    cancellation_event: threading.Event | None = None,
    state_root: Path | None = None,
    *,
    image_paths: Sequence[Path] = (),
) -> str:
    return _run_codex_result(
        identity,
        prompt,
        cwd,
        sandbox,
        progress_callback=progress_callback,
        cancellation_event=cancellation_event,
        state_root=state_root,
        image_paths=image_paths,
    ).final_text


def _run_codex_result(
    identity: Identity,
    prompt: str,
    cwd: Path | None,
    sandbox: str,
    progress_callback: ProgressCallback | None = None,
    *,
    persist_session: bool = False,
    session_id: str = "",
    cancellation_event: threading.Event | None = None,
    state_root: Path | None = None,
    image_paths: Sequence[Path] = (),
    execution: RuntimeExecutionControl | None = None,
) -> RuntimeResult:
    control = _runtime_execution_control(
        execution,
        cancellation_event=cancellation_event,
        progress_callback=progress_callback,
    )
    control.raise_if_stopped()
    state_root = state_root or cwd
    resolution = resolve_codex_executable(state_root)
    codex = resolution.path
    if codex is None:
        detail = f" {resolution.detail}" if resolution.detail else ""
        raise BrainError(
            "Enoch cannot find the Codex CLI."
            f"{detail} Configure it with `/config runtime codex executable <path>` "
            "or expose `codex` on PATH."
        )

    with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=True) as output:
        args = _codex_exec_args(
            codex,
            cwd,
            sandbox,
            output.name,
            persist_session=persist_session,
            session_id=session_id,
            image_paths=image_paths,
        )
        prompt_marker = args.pop()

        model = _configured_model(state_root)
        if model:
            args.extend(["--model", model])
        reasoning_effort = _configured_reasoning_effort(state_root)
        if reasoning_effort:
            args.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        args.append(prompt_marker)

        record_last_codex_input(
            prompt,
            state_root,
            sandbox=sandbox,
            persist_session=persist_session,
            session_id=session_id,
            resumed=bool(session_id),
        )

        timeout = control.timeout_seconds or int(
            os.environ.get("ENOCH_CODEX_TIMEOUT", task_timeout_seconds(state_root))
        )
        if (
            control.progress_callback is not None
            or control.cancellation_event is not None
            or control.timeout_event is not None
        ):
            result_stdout, result_stderr, returncode = _run_with_progress(
                args=args,
                prompt=prompt,
                timeout=timeout,
                sandbox=sandbox,
                execution=control,
            )
            result = subprocess.CompletedProcess(args, returncode, result_stdout, result_stderr)
        else:
            try:
                result = subprocess.run(
                    args,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise BrainTimedOut("Enoch waited too long for Codex to answer.") from exc

        output.seek(0)
        answer = output.read().strip()
        usage = _record_token_usage(result.stdout)
        result_session_id = _session_id_from_jsonl(result.stdout) or session_id
        runtime_result = {
            "session_id": result_session_id,
            "completion_reason": _completion_reason_from_jsonl(result.stdout),
            "usage": RuntimeUsage(
                input_tokens=usage.input_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                output_tokens=usage.output_tokens,
                reasoning_tokens=usage.reasoning_output_tokens,
            ),
            "events": _runtime_events_from_jsonl(result.stdout),
        }
        if answer:
            return RuntimeResult(final_text=answer, **runtime_result)

        if result.returncode != 0:
            stderr = result.stderr.strip() or result.stdout.strip()
            access_reason = _codex_access_unavailable_reason(stderr)
            if access_reason:
                raise CodexAccessUnavailable(access_reason)
            raise BrainError(f"Codex did not answer successfully: {stderr}")

        return RuntimeResult(
            final_text=_final_message_from_jsonl(result.stdout),
            **runtime_result,
        )


def _codex_exec_args(
    codex: str,
    cwd: Path | None,
    sandbox: str,
    output_path: str,
    *,
    persist_session: bool,
    session_id: str,
    image_paths: Sequence[Path] = (),
) -> list[str]:
    if session_id:
        args = [
            codex,
            "exec",
            "resume",
            session_id,
            "-c",
            f'sandbox_mode="{sandbox}"',
            "--json",
            "--output-last-message",
            output_path,
        ]
        for image_path in image_paths:
            args.extend(["--image", str(image_path)])
        args.append("-")
        return args

    args = [
        codex,
        "exec",
        "--cd",
        str(cwd or Path.cwd()),
        "--sandbox",
        sandbox,
        "--color",
        "never",
        "--json",
    ]
    if not persist_session:
        args.append("--ephemeral")
    args.extend(["--output-last-message", output_path])
    for image_path in image_paths:
        args.extend(["--image", str(image_path)])
    args.append("-")
    return args


def _configured_reasoning_effort(root: Path | None = None) -> str:
    env_reasoning = os.environ.get("ENOCH_CODEX_REASONING_EFFORT", "").strip()
    if env_reasoning:
        return env_reasoning
    return _enoch_reasoning_effort(root)


def _codex_access_unavailable_reason(details: str) -> str:
    normalized = " ".join(details.lower().split())
    authentication_markers = (
        "not logged in",
        "codex login",
        "authentication required",
        "unauthorized",
        "401 unauthorized",
        "invalid_api_key",
        "incorrect api key",
        "missing api key",
        "api key is missing",
        "access token is missing",
        "access token has expired",
        "access token expired",
        "no access token",
        "token is missing",
        "token has expired",
        "login expired",
        "authentication failed",
        "missing bearer or basic authentication",
        "credentials are missing",
        "refresh token",
    )
    if any(marker in normalized for marker in authentication_markers):
        return "Codex authentication is unavailable."
    quota_markers = (
        "insufficient_quota",
        "quota exceeded",
        "usage limit",
        "billing hard limit",
        "credit balance",
        "out of credits",
        "no credits remaining",
    )
    if any(marker in normalized for marker in quota_markers):
        return "Codex usage quota is currently unavailable."
    rate_limit_markers = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "429 too many requests",
    )
    if any(marker in normalized for marker in rate_limit_markers):
        return "Codex is temporarily rate-limited."
    return ""


def _configured_model(root: Path | None = None) -> str:
    env_model = os.environ.get("ENOCH_CODEX_MODEL", "").strip()
    if env_model:
        return env_model
    return _enoch_model(root)


def _enoch_model(root: Path | None = None) -> str:
    if root is None:
        return ""
    return read_section("codex", root).get("model", "").strip()


def _enoch_reasoning_effort(root: Path | None = None) -> str:
    if root is None:
        return ""
    value = read_section("codex", root).get("reasoning_effort", "").strip().lower()
    return value if value in REASONING_EFFORTS else ""


def _run_with_progress(
    args: list[str],
    prompt: str,
    timeout: int,
    sandbox: str,
    execution: RuntimeExecutionControl | None = None,
    progress_callback: ProgressCallback | None = None,
    cancellation_event: threading.Event | None = None,
) -> tuple[str, str, int]:
    control = _runtime_execution_control(
        execution,
        cancellation_event=cancellation_event,
        progress_callback=progress_callback,
    )
    interval = int(os.environ.get("ENOCH_PROGRESS_INTERVAL", DEFAULT_PROGRESS_INTERVAL_SECONDS))
    start = time.monotonic()
    next_update = start + interval
    deadline = start + timeout

    with tempfile.TemporaryFile("w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile("w+", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()

            try:
                while process.poll() is None:
                    now = time.monotonic()
                    if control.timed_out or now >= deadline:
                        _stop_process(process)
                        raise BrainTimedOut("Enoch waited too long for Codex to answer.")
                    if control.cancelled:
                        _stop_process(process)
                        raise BrainCancelled("Enoch cancelled the active Codex run.")

                    if now >= next_update:
                        control.emit_progress(
                            RuntimeProgress(
                                elapsed_seconds=int(now - start),
                                sandbox=sandbox,
                            )
                        )
                        next_update += interval

                    sleep_for = min(1.0, max(0.1, next_update - now), max(0.1, deadline - now))
                    time.sleep(sleep_for)
            except KeyboardInterrupt as exc:
                _stop_process(process)
                raise BrainCancelled("Enoch cancelled the active Codex run.") from exc

            stdout_file.seek(0)
            stderr_file.seek(0)
            return stdout_file.read(), stderr_file.read(), process.returncode


def _runtime_execution_control(
    execution: RuntimeExecutionControl | None,
    *,
    session_key: str = "",
    cancellation_event: threading.Event | None = None,
    progress_callback: ProgressCallback | None = None,
) -> RuntimeExecutionControl:
    if execution is not None:
        return execution
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


def _record_token_usage(stdout: str) -> TokenUsage:
    usage = _token_usage_from_jsonl(stdout)
    if usage is None:
        return TokenUsage()
    update_token_usage(
        input_tokens=usage.input_tokens,
        cached_input_tokens=usage.cached_input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_output_tokens=usage.reasoning_output_tokens,
    )
    return usage


def _runtime_events_from_jsonl(stdout: str) -> tuple[RuntimeEvent, ...]:
    events: list[RuntimeEvent] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "").strip()
        if event_type:
            events.append(RuntimeEvent(type=event_type, data=event))
    return tuple(events)


def _completion_reason_from_jsonl(stdout: str) -> str:
    event_types = tuple(event.type for event in _runtime_events_from_jsonl(stdout))
    if "turn.failed" in event_types:
        return "failed"
    if "turn.cancelled" in event_types:
        return "cancelled"
    return "completed"


def _token_usage_from_jsonl(stdout: str) -> TokenUsage | None:
    latest_usage: TokenUsage | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        latest_usage = TokenUsage(
            input_tokens=_usage_int(usage.get("input_tokens")),
            cached_input_tokens=_usage_int(usage.get("cached_input_tokens")),
            output_tokens=_usage_int(usage.get("output_tokens")),
            reasoning_output_tokens=_usage_int(usage.get("reasoning_output_tokens")),
        )
    return latest_usage


def _usage_int(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _session_id_from_jsonl(stdout: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = _session_id_from_event(event)
        if session_id:
            return session_id
    return ""


def _session_id_from_event(event: dict) -> str:
    for key in ("thread_id", "threadId", "session_id", "sessionId"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("thread", "session"):
        value = event.get(key)
        if isinstance(value, dict):
            nested = value.get("id")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _final_message_from_jsonl(stdout: str) -> str:
    final_message = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            final_message = text
    return final_message


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def resolve_codex_executable(root: Path | None = None) -> CodexExecutableResolution:
    configured = os.environ.get("ENOCH_CODEX_BIN", "").strip()
    if configured:
        return resolve_codex_executable_value(configured, "ENOCH_CODEX_BIN")

    enoch_configured = read_section("codex", root).get("executable", "").strip()
    if enoch_configured:
        return resolve_codex_executable_value(
            enoch_configured,
            "Enoch config codex.executable",
        )

    path_codex = shutil.which("codex")
    if path_codex:
        return CodexExecutableResolution(path=path_codex, source="PATH")

    for path in DEFAULT_CODEX_PATHS:
        candidate = Path(path)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return CodexExecutableResolution(path=path, source=f"known macOS path {path}")

    return CodexExecutableResolution(
        path=None,
        source="automatic discovery",
        detail="No executable was found on PATH or in a known macOS app location.",
    )


def resolve_codex_executable_value(
    value: str,
    source: str = "provided executable",
) -> CodexExecutableResolution:
    expanded = str(Path(value).expanduser()) if os.sep in value else value
    resolved = shutil.which(expanded) if os.sep not in expanded else expanded
    if resolved:
        candidate = Path(resolved)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return CodexExecutableResolution(
                path=str(candidate),
                source=source,
                configured_value=value,
            )
    return CodexExecutableResolution(
        path=None,
        source=source,
        configured_value=value,
        detail=f"Configured executable {value!r} does not exist or is not executable.",
    )


def _codex_binary(root: Path | None = None) -> str | None:
    return resolve_codex_executable(root).path
