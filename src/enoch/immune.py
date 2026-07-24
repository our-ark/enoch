from __future__ import annotations

from dataclasses import dataclass
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from enoch.paths import enoch_home, repo_root
from enoch.providers.contracts import (
    AgentRuntimeError,
    ForgeProviderError,
    VersionControlProviderError,
)
from enoch.providers.registry import ProviderError, load_provider, provider_name
from enoch.state import StateCorruptionError, load_json_object


DEFAULT_TEST_ARGS = ["-m", "unittest", "discover", "-s", "tests", "-t", "."]
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 12000
MIN_PYTHON_VERSION = (3, 11)
TEST_BUILD_REQUIREMENTS = Path(".github/requirements/test-build.txt")
STATE_OBJECT_FIELDS: dict[str, dict[str, type]] = {
    "backlog.json": {"pending": list, "history": list},
    "codex_sessions.json": {"sessions": dict},
    "cron.json": {"active": list, "history": list},
    "evolve_candidates.json": {"candidates": list},
    "inbox.json": {"events": dict},
    "long_term.json": {"memories": list},
    "task_queue.json": {
        "pending": list,
        "paused": list,
        "history": list,
    },
}


@dataclass(frozen=True)
class DoctorDiagnosis:
    summary: str
    failing_tests: list[str]
    likely_files: list[str]
    suggested_action: str


@dataclass(frozen=True)
class ImmuneResult:
    passed: bool
    command: str
    output: str
    diagnosis: DoctorDiagnosis
    checks: list["DoctorCheckResult"]


@dataclass(frozen=True)
class DoctorCheckResult:
    name: str
    passed: bool
    command: str
    output: str
    category: str = "code health"
    summary: str = ""
    skipped: bool = False


def run_immune_system(
    root: Path | None = None,
    *,
    state_root: Path | None = None,
) -> ImmuneResult:
    root_path = repo_root(root)
    operational_root = repo_root(state_root or root_path)
    timeout = _timeout_seconds()
    checks = [
        _python_runtime_check(root_path, timeout),
    ]
    test_check: DoctorCheckResult
    if os.environ.get("ENOCH_TEST_COMMAND") is None:
        build_backend = _build_backend_check(root_path, timeout)
        checks.append(build_backend)
        if build_backend.passed:
            test_check = _run_check("tests", _test_command(), root_path, timeout)
        else:
            test_check = DoctorCheckResult(
                name="tests",
                passed=True,
                command=shlex.join(_test_command()),
                output="",
                summary="not run until the build-backend prerequisite passes",
                skipped=True,
            )
    else:
        test_check = _run_check("tests", _test_command(), root_path, timeout)
    checks.extend(
        [
            test_check,
            _run_check("import smoke", _import_smoke_command(), root_path, timeout),
            _runtime_provider_check(operational_root, timeout),
            _forge_provider_check(operational_root),
            _vcs_workspace_check(root_path, timeout),
            _memory_storage_check(operational_root),
        ]
    )
    output = _combined_output(checks)
    passed = all(check.passed for check in checks)
    return ImmuneResult(
        passed=passed,
        command="; ".join(check.command for check in checks),
        output=output,
        diagnosis=_diagnose_checks(checks, output, passed=passed),
        checks=checks,
    )


def _test_command() -> list[str]:
    configured = os.environ.get("ENOCH_TEST_COMMAND")
    if configured is not None:
        return _split_configured_command(configured)
    return [_python_executable(), *DEFAULT_TEST_ARGS]


def _split_configured_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _import_smoke_command() -> list[str]:
    return [
        _python_executable(),
        "-c",
        "; ".join(
            [
                "import enoch.cli",
                "import enoch.app.core",
                "import enoch.operations.daemon",
                "import enoch.immune",
                "import enoch.learn",
                "import enoch.lineage",
                "import enoch.memory",
                "import enoch.skills",
                "import enoch.providers",
                "import enoch.operations.update_doctor",
            ]
        ),
    ]


def _python_executable() -> str:
    return os.environ.get("ENOCH_PYTHON") or sys.executable


def _python_runtime_check(root: Path, timeout: float) -> DoctorCheckResult:
    check = _run_check("python runtime", [_python_executable(), "--version"], root, timeout)
    if not check.passed:
        return check
    version = _python_version(check.output)
    if version is None or version >= MIN_PYTHON_VERSION:
        return check
    required = ".".join(str(part) for part in MIN_PYTHON_VERSION)
    found = ".".join(str(part) for part in version)
    return DoctorCheckResult(
        name=check.name,
        passed=False,
        command=check.command,
        output=check.output,
        category=check.category,
        summary=f"Python {found} is below required >= {required}",
    )


def _build_backend_check(root: Path, timeout: float) -> DoctorCheckResult:
    backend, distribution, minimum_version = _project_build_backend(root)
    command = [
        _python_executable(),
        "-c",
        (
            "import importlib, importlib.metadata; "
            f"importlib.import_module({backend!r}); "
            f"print({distribution!r} + ' ' + importlib.metadata.version({distribution!r}))"
        ),
    ]
    check = _run_check("build backend", command, root, timeout)
    install_command = _test_prerequisite_install_command(root)
    if not check.passed:
        return DoctorCheckResult(
            name=check.name,
            passed=False,
            command=check.command,
            output=_join_output(
                f"Doctor test environment is missing build backend {backend}.",
                f"Install the locked test prerequisite with:\n{install_command}",
                check.output,
            ),
            category="environment readiness",
            summary=f"missing {backend}",
        )

    found = _distribution_version(check.output, distribution)
    if (
        minimum_version
        and found
        and _numeric_version(found) < _numeric_version(minimum_version)
    ):
        return DoctorCheckResult(
            name=check.name,
            passed=False,
            command=check.command,
            output=_join_output(
                (
                    f"Doctor test environment has {distribution} {found or 'unknown'}, "
                    f"but the project requires at least {minimum_version}."
                ),
                f"Install the locked test prerequisite with:\n{install_command}",
            ),
            category="environment readiness",
            summary=f"{distribution} {found}; requires >= {minimum_version}",
        )
    return DoctorCheckResult(
        name=check.name,
        passed=True,
        command=check.command,
        output=check.output,
        category="environment readiness",
        summary=f"{distribution} {found or 'available'}",
    )


def _project_build_backend(root: Path) -> tuple[str, str, str]:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return "setuptools.build_meta", "setuptools", ""
    build_system = data.get("build-system")
    if not isinstance(build_system, dict):
        return "setuptools.build_meta", "setuptools", ""
    backend = str(build_system.get("build-backend") or "setuptools.build_meta").strip()
    distribution = backend.split(".", 1)[0].replace("_", "-")
    requirements = build_system.get("requires")
    minimum_version = ""
    if isinstance(requirements, list):
        requirement = next(
            (
                str(item)
                for item in requirements
                if re.match(
                    rf"^{re.escape(distribution)}\s*>=",
                    str(item),
                    re.IGNORECASE,
                )
            ),
            "",
        )
        match = re.match(
            rf"^{re.escape(distribution)}\s*>=\s*([A-Za-z0-9_.+-]+)",
            requirement,
            re.IGNORECASE,
        )
        if match:
            minimum_version = match.group(1)
    return backend, distribution, minimum_version


def _numeric_version(value: str) -> tuple[int, ...]:
    return tuple(
        int(part)
        for part in re.findall(r"\d+", value)
    )


def _distribution_version(output: str, distribution: str) -> str:
    match = re.search(
        rf"(?m)^{re.escape(distribution)}\s+([^\s]+)\s*$",
        output,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _test_prerequisite_install_command(root: Path) -> str:
    requirements = root / TEST_BUILD_REQUIREMENTS
    if requirements.is_file():
        return (
            f"{shlex.quote(_python_executable())} -m pip install "
            "--disable-pip-version-check --require-hashes "
            f"-r {TEST_BUILD_REQUIREMENTS.as_posix()}"
        )
    return f"{shlex.quote(_python_executable())} -m pip install setuptools"


def _python_version(output: str) -> tuple[int, int] | None:
    match = re.search(r"Python\s+(\d+)\.(\d+)", output)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _timeout_seconds() -> float:
    raw = os.environ.get("ENOCH_DOCTOR_TIMEOUT_SECONDS")
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return value if value > 0 else DEFAULT_TIMEOUT_SECONDS


def _run_check(name: str, command: list[str], root: Path, timeout: float) -> DoctorCheckResult:
    display_command = shlex.join(command) if command else "<empty command>"
    if not command:
        return DoctorCheckResult(name, False, display_command, "Doctor check command is empty.")

    env = os.environ.copy()
    src_path = str(root / "src")
    env["PYTHONPATH"] = _prepend_path(src_path, env.get("PYTHONPATH", ""))
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as error:
        return DoctorCheckResult(name, False, display_command, str(error))
    except subprocess.TimeoutExpired as error:
        output = _join_output(error.stdout, error.stderr)
        detail = f"Timed out after {timeout:g} second(s)."
        return DoctorCheckResult(name, False, display_command, _limit_output(_join_output(output, detail)))
    except OSError as error:
        return DoctorCheckResult(name, False, display_command, str(error))

    output = _limit_output(_join_output(result.stdout, result.stderr))
    return DoctorCheckResult(
        name,
        result.returncode == 0,
        display_command,
        output,
        summary=_check_summary(name, result.returncode == 0, output),
    )


def _codex_binary_check(
    root: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> DoctorCheckResult:
    from enoch.brain import resolve_codex_executable

    resolution = resolve_codex_executable(root)
    if resolution.path:
        check = _run_check(
            "codex runtime",
            [resolution.path, "login", "status"],
            root,
            min(timeout, 15),
        )
        if not check.passed:
            return DoctorCheckResult(
                name=check.name,
                passed=False,
                command=check.command,
                output=_join_output(
                    check.output,
                    "Authenticate Codex with `codex login`, then run doctor again.",
                ),
                category="operational readiness",
                summary=f"not authenticated ({resolution.path})",
            )
        return DoctorCheckResult(
            name=check.name,
            passed=True,
            command=check.command,
            output=check.output,
            category="operational readiness",
            summary=f"authenticated; {resolution.path} (source: {resolution.source})",
        )
    return DoctorCheckResult(
        name="codex runtime",
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
        category="operational readiness",
        summary=f"not found (source: {resolution.source})",
    )


def _runtime_provider_check(
    root: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> DoctorCheckResult:
    try:
        selected = provider_name("runtime", root)
        if selected == "codex":
            return _codex_binary_check(root, timeout)
        runtime = load_provider("runtime", root, name=selected)
        health = runtime.health(root)
    except (ProviderError, AgentRuntimeError, OSError) as error:
        return DoctorCheckResult(
            name="agent runtime",
            passed=False,
            command="load runtime provider",
            output=str(error),
            category="operational readiness",
            summary="provider unavailable",
        )
    return DoctorCheckResult(
        name=health.name,
        passed=health.passed,
        command=health.command,
        output=health.output,
        category="operational readiness",
        summary=health.summary,
    )


def _forge_provider_check(root: Path) -> DoctorCheckResult:
    try:
        selected = provider_name("forge", root)
        forge = load_provider("forge", root, name=selected)
        health_fn = getattr(forge, "health", None)
        if callable(health_fn):
            health = health_fn(root)
            return DoctorCheckResult(
                name=health.name,
                passed=health.passed,
                command=health.command,
                output=health.output,
                category="operational readiness",
                summary=health.summary,
            )
    except (ProviderError, ForgeProviderError, OSError) as error:
        return DoctorCheckResult(
            name="forge provider",
            passed=False,
            command="load selected forge provider",
            output=str(error),
            category="operational readiness",
            summary="provider unavailable",
        )
    remote_review = bool(getattr(forge, "supports_remote_review", True))
    return DoctorCheckResult(
        name=f"{selected} forge",
        passed=True,
        command=f"load {selected} forge provider",
        output="",
        category="operational readiness",
        summary=(
            "provider loaded; authentication check unavailable"
            if remote_review
            else "local-only publishing"
        ),
    )


def _vcs_workspace_check(root: Path, timeout: float) -> DoctorCheckResult:
    del timeout
    try:
        selected = provider_name("vcs", root)
        clean = bool(load_provider("vcs", root, name=selected).is_clean(root))
    except (ProviderError, VersionControlProviderError, OSError) as error:
        return DoctorCheckResult(
            name="version control workspace",
            passed=False,
            command="load selected version control provider",
            output=str(error),
            category="operational readiness",
            summary="provider unavailable",
        )
    return DoctorCheckResult(
        name=f"{selected} worktree",
        passed=True,
        command=f"{selected} workspace status",
        output="",
        category="operational readiness",
        summary="worktree clean" if clean else "worktree dirty",
    )


def _git_worktree_check(root: Path, timeout: float) -> DoctorCheckResult:
    """Compatibility alias for callers using the original Git-specific name."""
    return _vcs_workspace_check(root, timeout)


def _memory_storage_check(root: Path) -> DoctorCheckResult:
    home = enoch_home(root)
    path = home / ".doctor_write_check"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
        path.unlink(missing_ok=True)
        state_files = _validate_state_files(home)
    except (OSError, StateCorruptionError) as error:
        return DoctorCheckResult(
            name="state storage",
            passed=False,
            command=f"validate {home}",
            output=str(error),
            category="operational readiness",
            summary="unreadable or not writable",
        )
    return DoctorCheckResult(
        name="state storage",
        passed=True,
        command=f"validate {home}",
        output="",
        category="operational readiness",
        summary=f"{home} writable; {state_files} state file(s) readable",
    )


def _validate_state_files(home: Path) -> int:
    count = 0
    for path in sorted(home.rglob("*.json")):
        data = load_json_object(path)
        _validate_state_object_shape(path, data)
        count += 1
    for path in sorted(home.rglob("*.jsonl")):
        _validate_json_lines(path)
        count += 1
    return count


def _validate_state_object_shape(path: Path, data: dict[str, object]) -> None:
    fields = STATE_OBJECT_FIELDS.get(path.name, {})
    for field, expected_type in fields.items():
        if field not in data:
            continue
        if not isinstance(data[field], expected_type):
            raise StateCorruptionError(
                path,
                f"expected {field} to be {expected_type.__name__}",
            )
    if path.name == "task_queue.json":
        running = data.get("running")
        if running is not None and not isinstance(running, dict):
            raise StateCorruptionError(path, "expected running to be an object or null")


def _validate_json_lines(path: Path) -> None:
    try:
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise StateCorruptionError(
                        path,
                        f"invalid JSON at line {line_number}, column {error.colno}",
                    ) from error
                if not isinstance(value, dict):
                    raise StateCorruptionError(
                        path,
                        f"expected a JSON object at line {line_number}",
                    )
    except OSError as error:
        raise StateCorruptionError(path, str(error)) from error


def _prepend_path(first: str, rest: str) -> str:
    if not rest:
        return first
    parts = rest.split(os.pathsep)
    if first in parts:
        return rest
    return os.pathsep.join([first, rest])


def _combined_output(checks: list[DoctorCheckResult]) -> str:
    blocks = []
    for check in checks:
        status = "skipped" if check.skipped else ("passed" if check.passed else "failed")
        lines = [f"[{check.category}: {check.name}] {status}", f"Command: {check.command}"]
        if check.summary:
            lines.append(f"Summary: {check.summary}")
        if check.output:
            lines.extend(["Output:", check.output])
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _join_output(*parts: object) -> str:
    cleaned = []
    for part in parts:
        if isinstance(part, bytes):
            text = part.decode(errors="replace")
        else:
            text = "" if part is None else str(part)
        if text.strip():
            cleaned.append(text.strip())
    return "\n".join(cleaned)


def _limit_output(output: str) -> str:
    if len(output) <= MAX_OUTPUT_CHARS:
        return output
    marker = "\n\n[doctor output truncated; final output preserved]\n\n"
    remaining = MAX_OUTPUT_CHARS - len(marker)
    head = remaining // 2
    tail = remaining - head
    return f"{output[:head].rstrip()}{marker}{output[-tail:].lstrip()}"


def _first_output_line(output: str) -> str:
    for line in output.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _check_summary(name: str, passed: bool, output: str) -> str:
    if name == "tests" and passed:
        return "tests passed"
    if name == "import smoke" and passed:
        return "imports loaded"
    return _first_output_line(output)


def _diagnose_checks(
    checks: list[DoctorCheckResult],
    output: str,
    *,
    passed: bool,
) -> DoctorDiagnosis:
    diagnosis = diagnose_output(output, passed=passed)
    if passed or diagnosis.failing_tests or diagnosis.summary.startswith("Import smoke failed"):
        return diagnosis

    failed_checks = [check for check in checks if not check.passed]
    if not failed_checks:
        return diagnosis

    names = ", ".join(check.name for check in failed_checks[:3])
    if len(failed_checks) > 3:
        names += f", and {len(failed_checks) - 3} more"
    return DoctorDiagnosis(
        summary=f"{len(failed_checks)} doctor check(s) failed: {names}.",
        failing_tests=diagnosis.failing_tests,
        likely_files=diagnosis.likely_files,
        suggested_action="Inspect the failed doctor checks, fix the first concrete issue, then run doctor again.",
    )


def diagnose_output(output: str, passed: bool) -> DoctorDiagnosis:
    if passed:
        return DoctorDiagnosis(
            summary="All configured health checks passed.",
            failing_tests=[],
            likely_files=[],
            suggested_action="No repair needed.",
        )

    failing_tests = _failing_tests(output)
    likely_files = _likely_files(output)
    environment_error = _environment_error_summary(output)
    import_error = _import_error_summary(output)
    if environment_error:
        summary = f"Test environment unavailable: {environment_error}"
        suggested_action = (
            "Install the reported locked test prerequisite, then run doctor again."
        )
    elif failing_tests:
        summary = f"{len(failing_tests)} test(s) failed."
        suggested_action = "Inspect the failing tests, make one focused repair pass, then run doctor again."
    elif import_error:
        summary = f"Import smoke failed: {import_error}"
        suggested_action = "Fix the broken import or stale module path, then run doctor again."
    elif output.strip():
        summary = "Health checks failed before reporting specific failing tests."
        suggested_action = "Read the command output, fix the first concrete error, then run doctor again."
    else:
        summary = "Health checks failed without output."
        suggested_action = "Run the health command manually to collect more detail."

    return DoctorDiagnosis(
        summary=summary,
        failing_tests=failing_tests,
        likely_files=likely_files,
        suggested_action=suggested_action,
    )


def _environment_error_summary(output: str) -> str:
    explicit = re.search(
        r"Doctor test environment is missing build backend ([^\s.]+(?:\.[^\s.]+)*)\.",
        output,
    )
    if explicit:
        return f"missing build backend {explicit.group(1)}."
    backend = re.search(r"BackendUnavailable:\s+Cannot import ['\"]([^'\"]+)['\"]", output)
    if backend:
        return f"missing build backend {backend.group(1)}."
    incompatible = re.search(
        r"Doctor test environment has ([^\s]+) ([^,\s]+), but .* requires at least (\S+)\.",
        output,
    )
    if incompatible:
        return (
            f"{incompatible.group(1)} {incompatible.group(2)} is below "
            f"required version {incompatible.group(3)}."
        )
    return ""


def _failing_tests(output: str) -> list[str]:
    tests = []
    for line in output.splitlines():
        unittest_match = re.match(r"^(?:FAIL|ERROR):\s+\S+\s+\(([^)]+)\)", line)
        if unittest_match:
            tests.append(unittest_match.group(1))
            continue

        pytest_match = re.match(r"^FAILED\s+([^\s]+)", line)
        if pytest_match:
            test_name = pytest_match.group(1)
            if not test_name.startswith("("):
                tests.append(test_name)

    return _dedupe(tests)


def _import_error_summary(output: str) -> str:
    missing_module = re.search(r"ModuleNotFoundError:\s+No module named ['\"]([^'\"]+)['\"]", output)
    if missing_module:
        return f"missing module {missing_module.group(1)}."

    cannot_import = re.search(
        r"ImportError:\s+cannot import name ['\"]([^'\"]+)['\"] from ['\"]([^'\"]+)['\"]",
        output,
    )
    if cannot_import:
        return f"cannot import {cannot_import.group(1)} from {cannot_import.group(2)}."

    generic = re.search(r"(?:ImportError|ModuleNotFoundError):\s+(.+)", output)
    if generic:
        return generic.group(1).strip().rstrip(".") + "."

    return ""


def _likely_files(output: str) -> list[str]:
    files = []
    for match in re.finditer(r'File "([^"]+)", line \d+', output):
        path = match.group(1)
        if (
            path.startswith("<")
            or "/unittest/" in path
            or path.endswith("/unittest/case.py")
            or "/site-packages/" in path
            or "/pip/_internal/" in path
        ):
            continue
        if not any(segment in path for segment in ("/src/", "/tests/", "/libraries/")):
            continue
        files.append(path)

    for match in re.finditer(r"\b(tests/[A-Za-z0-9_./-]+\.py)\b", output):
        files.append(match.group(1))

    for match in re.finditer(r"\b(src/[A-Za-z0-9_./-]+\.py)\b", output):
        files.append(match.group(1))

    return _dedupe(files)[:5]


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
