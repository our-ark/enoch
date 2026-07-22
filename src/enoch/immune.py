from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from enoch.paths import repo_root
from enoch.providers.contracts import AgentRuntimeError
from enoch.providers.registry import ProviderError, load_provider, provider_name


DEFAULT_TEST_ARGS = ["-m", "unittest", "discover", "-s", "tests"]
DEFAULT_TIMEOUT_SECONDS = 120
MAX_OUTPUT_CHARS = 12000
MIN_PYTHON_VERSION = (3, 11)


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


def run_immune_system(root: Path | None = None) -> ImmuneResult:
    root_path = repo_root(root)
    timeout = _timeout_seconds()
    checks = [
        _python_runtime_check(root_path, timeout),
        _run_check("tests", _test_command(), root_path, timeout),
        _run_check("import smoke", _import_smoke_command(), root_path, timeout),
        _runtime_provider_check(root_path),
        _git_worktree_check(root_path, timeout),
        _memory_storage_check(root_path),
    ]
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
                "import enoch.application",
                "import enoch.daemon",
                "import enoch.immune",
                "import enoch.learn",
                "import enoch.lineage",
                "import enoch.memory",
                "import enoch.skills",
                "import enoch.providers",
                "import enoch.update_doctor",
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


def _codex_binary_check(root: Path) -> DoctorCheckResult:
    from enoch.brain import resolve_codex_executable

    resolution = resolve_codex_executable(root)
    if resolution.path:
        return DoctorCheckResult(
            name="codex binary",
            passed=True,
            command="which codex",
            output="",
            category="operational readiness",
            summary=f"{resolution.path} (source: {resolution.source})",
        )
    return DoctorCheckResult(
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
        category="operational readiness",
        summary=f"not found (source: {resolution.source})",
    )


def _runtime_provider_check(root: Path) -> DoctorCheckResult:
    if provider_name("runtime", root) == "codex":
        return _codex_binary_check(root)
    try:
        runtime = load_provider("runtime", root)
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


def _git_worktree_check(root: Path, timeout: float) -> DoctorCheckResult:
    selected = provider_name("vcs", root)
    if selected != "git":
        try:
            provider = load_provider("vcs", root, name=selected)
            provider_result = provider.run(["status", "--porcelain"], root)
        except (ProviderError, OSError) as error:
            return DoctorCheckResult(
                name=f"{selected} worktree",
                passed=False,
                command=f"{selected} status",
                output=str(error),
                category="operational readiness",
                summary="provider unavailable",
            )
        output = str(provider_result.stdout).strip()
        return DoctorCheckResult(
            name=f"{selected} worktree",
            passed=int(provider_result.returncode) == 0,
            command=f"{selected} status",
            output=output,
            category="operational readiness",
            summary="worktree dirty" if output else "worktree clean",
        )
    result = _run_check(
        "git worktree",
        ["git", "status", "--porcelain"],
        root,
        timeout,
    )
    summary = "worktree dirty" if result.output.strip() else "worktree clean"
    return DoctorCheckResult(
        name=result.name,
        passed=result.passed,
        command=result.command,
        output=result.output,
        category="operational readiness",
        summary=summary if result.passed else result.summary,
    )


def _memory_storage_check(root: Path) -> DoctorCheckResult:
    path = root / ".enoch" / ".doctor_write_check"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok\n", encoding="utf-8")
        path.unlink(missing_ok=True)
    except OSError as error:
        return DoctorCheckResult(
            name="memory storage",
            passed=False,
            command=f"write {path}",
            output=str(error),
            category="operational readiness",
            summary="not writable",
        )
    return DoctorCheckResult(
        name="memory storage",
        passed=True,
        command=f"write {path}",
        output="",
        category="operational readiness",
        summary=".enoch writable",
    )


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
        status = "passed" if check.passed else "failed"
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
    return f"{output[:MAX_OUTPUT_CHARS].rstrip()}\n\n[doctor output truncated]"


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
    import_error = _import_error_summary(output)
    if failing_tests:
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
        if "/unittest/" in path or path.endswith("/unittest/case.py"):
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
