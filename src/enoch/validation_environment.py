from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
from uuid import uuid4

from enoch.paths import repo_root
from enoch.state import atomic_write, file_transaction


VALIDATION_ENVIRONMENT_SCHEMA_VERSION = 1
VALIDATION_REQUIREMENTS = Path(".github/requirements/test-build.txt")
VALIDATION_ENVIRONMENTS_DIRECTORY = "validation"
VALIDATION_ENVIRONMENT_HOME = "ENOCH_VALIDATION_ENVIRONMENT_HOME"
PROVISION_TIMEOUT_SECONDS = 180
VERIFY_TIMEOUT_SECONDS = 30


class ValidationEnvironmentError(RuntimeError):
    """Raised when Enoch cannot prepare its isolated test environment."""


@dataclass(frozen=True)
class ValidationEnvironment:
    root: Path
    python: Path
    fingerprint: str
    created: bool = False


@dataclass(frozen=True)
class _EnvironmentSpec:
    root: Path
    parent: Path
    target: Path
    fingerprint: str
    base_python: str
    backend: str
    requirements_file: Path | None
    requirements: tuple[str, ...]
    requirements_digest: str


def existing_validation_environment(
    root: Path,
    *,
    base_python: str,
) -> ValidationEnvironment | None:
    spec = _environment_spec(
        root,
        base_python=base_python,
    )
    if not _environment_is_complete(spec):
        return None
    return _environment_result(spec, created=False)


def ensure_validation_environment(
    root: Path,
    *,
    base_python: str,
) -> ValidationEnvironment:
    """Create or repair the content-addressed environment used by Doctor tests."""

    try:
        spec = _environment_spec(
            root,
            base_python=base_python,
        )
        spec.parent.mkdir(parents=True, exist_ok=True)
        with file_transaction(spec.parent / ".provision"):
            if _environment_is_complete(spec) and _backend_is_available(
                _venv_python(spec.target),
                spec.backend,
                root=spec.root,
            ):
                return _environment_result(spec, created=False)

            if spec.target.exists():
                shutil.rmtree(spec.target)
            temporary = spec.parent / f".{spec.fingerprint}.tmp-{uuid4().hex}"
            try:
                _create_environment(spec, temporary)
                temporary.replace(spec.target)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
        return _environment_result(spec, created=True)
    except ValidationEnvironmentError:
        raise
    except OSError as error:
        raise ValidationEnvironmentError(
            f"Could not prepare Enoch's managed validation environment: {error}"
        ) from error


def _environment_spec(
    root: Path,
    *,
    base_python: str,
) -> _EnvironmentSpec:
    root_path = repo_root(root)
    requirements_file, requirements, requirements_digest = _build_requirements(
        root_path
    )
    backend = _build_backend(root_path)
    resolved_python = _resolved_executable(base_python)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
                "python": resolved_python,
                "backend": backend,
                "requirements": requirements,
                "requirements_digest": requirements_digest,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    parent = _environment_parent()
    return _EnvironmentSpec(
        root=root_path,
        parent=parent,
        target=parent / fingerprint,
        fingerprint=fingerprint,
        base_python=resolved_python,
        backend=backend,
        requirements_file=requirements_file,
        requirements=requirements,
        requirements_digest=requirements_digest,
    )


def _environment_parent() -> Path:
    override = os.environ.get(VALIDATION_ENVIRONMENT_HOME, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".enoch" / VALIDATION_ENVIRONMENTS_DIRECTORY


def _build_requirements(
    root: Path,
) -> tuple[Path | None, tuple[str, ...], str]:
    locked = root / VALIDATION_REQUIREMENTS
    if locked.is_file():
        try:
            content = locked.read_bytes()
        except OSError as error:
            raise ValidationEnvironmentError(
                f"Could not read locked validation requirements at {locked}: {error}"
            ) from error
        return (
            locked,
            (),
            hashlib.sha256(content).hexdigest(),
        )

    requirements = _pyproject_build_requirements(root)
    rendered = json.dumps(requirements, sort_keys=True).encode("utf-8")
    return None, requirements, hashlib.sha256(rendered).hexdigest()


def _pyproject_build_requirements(root: Path) -> tuple[str, ...]:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValidationEnvironmentError(
            f"Could not read validation build requirements from {path}: {error}"
        ) from error
    build_system = data.get("build-system")
    raw_requirements = (
        build_system.get("requires")
        if isinstance(build_system, dict)
        else None
    )
    if not isinstance(raw_requirements, list):
        raise ValidationEnvironmentError(
            f"{path} does not define build-system.requires."
        )
    requirements = tuple(
        str(requirement).strip()
        for requirement in raw_requirements
        if str(requirement).strip()
    )
    if not requirements:
        raise ValidationEnvironmentError(
            f"{path} has no usable build-system requirements."
        )
    return requirements


def _build_backend(root: Path) -> str:
    path = root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ValidationEnvironmentError(
            f"Could not read validation build backend from {path}: {error}"
        ) from error
    build_system = data.get("build-system")
    backend = (
        build_system.get("build-backend")
        if isinstance(build_system, dict)
        else None
    )
    rendered = str(backend or "").strip()
    if not rendered:
        raise ValidationEnvironmentError(
            f"{path} does not define build-system.build-backend."
        )
    return rendered


def _resolved_executable(executable: str) -> str:
    configured = executable.strip()
    if not configured:
        raise ValidationEnvironmentError(
            "The configured Python executable is empty."
        )
    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        located = shutil.which(configured)
        if located is None:
            raise ValidationEnvironmentError(
                f"Could not find configured Python executable {configured!r}."
            )
        candidate = Path(located)
    try:
        return str(candidate.resolve(strict=True))
    except OSError as error:
        raise ValidationEnvironmentError(
            f"Could not resolve configured Python executable {candidate}: {error}"
        ) from error


def _create_environment(spec: _EnvironmentSpec, temporary: Path) -> None:
    environment = _isolated_subprocess_environment()
    creation = _run(
        [spec.base_python, "-m", "venv", str(temporary)],
        root=spec.root,
        environment=environment,
        timeout=PROVISION_TIMEOUT_SECONDS,
    )
    if creation.returncode != 0:
        raise ValidationEnvironmentError(
            "Could not create Enoch's managed validation environment: "
            + _command_error(creation)
        )

    python = _venv_python(temporary)
    cache = spec.parent / "wheel-cache"
    cache.mkdir(parents=True, exist_ok=True)
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--cache-dir",
        str(cache),
    ]
    if spec.requirements_file is not None:
        command.extend(
            [
                "--require-hashes",
                "-r",
                str(spec.requirements_file),
            ]
        )
    else:
        command.extend(spec.requirements)
    installation = _run(
        command,
        root=spec.root,
        environment=environment,
        timeout=PROVISION_TIMEOUT_SECONDS,
    )
    if installation.returncode != 0:
        raise ValidationEnvironmentError(
            "Could not install Enoch's validation prerequisites: "
            + _command_error(installation)
        )
    if not _backend_is_available(
        python,
        spec.backend,
        root=spec.root,
        environment=environment,
    ):
        raise ValidationEnvironmentError(
            "The managed validation environment was created, but build backend "
            f"{spec.backend} is still unavailable."
        )
    atomic_write(
        temporary / ".complete.json",
        json.dumps(
            {
                "schema": VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
                "fingerprint": spec.fingerprint,
                "python": spec.base_python,
                "backend": spec.backend,
                "requirements_digest": spec.requirements_digest,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )


def _environment_is_complete(spec: _EnvironmentSpec) -> bool:
    marker = spec.target / ".complete.json"
    python = _venv_python(spec.target)
    if not marker.is_file() or not python.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("schema") == VALIDATION_ENVIRONMENT_SCHEMA_VERSION
        and payload.get("fingerprint") == spec.fingerprint
        and payload.get("requirements_digest") == spec.requirements_digest
    )


def _environment_result(
    spec: _EnvironmentSpec,
    *,
    created: bool,
) -> ValidationEnvironment:
    return ValidationEnvironment(
        root=spec.target,
        python=_venv_python(spec.target),
        fingerprint=spec.fingerprint,
        created=created,
    )


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _backend_is_available(
    python: Path,
    backend: str,
    *,
    root: Path,
    environment: dict[str, str] | None = None,
) -> bool:
    result = _run(
        [
            str(python),
            "-c",
            f"import importlib; importlib.import_module({backend!r})",
        ],
        root=root,
        environment=environment or _isolated_subprocess_environment(),
        timeout=VERIFY_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def _isolated_subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _run(
    command: list[str],
    *,
    root: Path,
    environment: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValidationEnvironmentError(str(error)) from error


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    return (
        result.stderr.strip()
        or result.stdout.strip()
        or f"command exited with status {result.returncode}"
    )
