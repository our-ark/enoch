from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
from importlib.machinery import PathFinder
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Iterable, Iterator
from uuid import uuid4

from enoch.paths import enoch_home, repo_root


MANIFEST_PATH = Path("genesis.toml")
PRELOADED_PATHS_ENV = "OUR_ARK_RUNTIME_DEPENDENCY_PATHS"
PINNED_VCS_REVISION = re.compile(r"@[0-9a-fA-F]{40}(?=#|$)")
EXACT_PACKAGE_REQUIREMENT = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?:\[[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*\])?"
    r"\s*==\s*[A-Za-z0-9][A-Za-z0-9_.!+-]*"
)


class RuntimeDependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeDependency:
    name: str
    requirement: str
    import_name: str
    local_source: Path | None = None
    optional: bool = False


def activate_runtime_dependencies(root: Path | None = None) -> tuple[Path, ...]:
    paths = runtime_dependency_paths(root)
    for path in reversed(paths):
        rendered = str(path)
        if rendered not in sys.path:
            sys.path.insert(0, rendered)
    return paths


def runtime_dependency_paths(root: Path | None = None) -> tuple[Path, ...]:
    root_path = repo_root(root)
    dependencies = load_runtime_dependencies(root_path)
    if not dependencies:
        return ()

    runtime_paths: list[Path] = []
    unresolved: list[RuntimeDependency] = []
    preloaded_paths = _preloaded_dependency_paths()
    for dependency in dependencies:
        local = root_path / dependency.local_source if dependency.local_source else None
        if local is not None and local.is_dir():
            runtime_paths.append(local.resolve())
            continue
        preloaded = _dependency_path(dependency, preloaded_paths)
        if preloaded is not None:
            runtime_paths.append(preloaded)
            continue
        if not dependency.optional:
            unresolved.append(dependency)

    if not unresolved:
        return _unique_paths(runtime_paths)
    target = _ensure_installed(root_path, unresolved)
    return _unique_paths([*runtime_paths, target])


def load_runtime_dependencies(root: Path | None = None) -> tuple[RuntimeDependency, ...]:
    path = repo_root(root) / MANIFEST_PATH
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeDependencyError(f"Could not read runtime dependencies: {error}") from error

    raw_dependencies = data.get("runtime_dependencies", [])
    if not isinstance(raw_dependencies, list):
        raise RuntimeDependencyError("genesis.toml runtime_dependencies must be an array of tables.")

    dependencies = []
    for raw in raw_dependencies:
        if not isinstance(raw, dict):
            raise RuntimeDependencyError("Each runtime dependency must be a TOML table.")
        name = str(raw.get("name") or "").strip()
        requirement = str(raw.get("requirement") or "").strip()
        import_name = str(raw.get("import_name") or "").strip()
        local_value = str(raw.get("local_source") or "").strip()
        optional_value = raw.get("optional", False)
        if not isinstance(optional_value, bool):
            raise RuntimeDependencyError(
                f"Runtime dependency {name or '<unnamed>'} optional must be a boolean."
            )
        optional = optional_value
        if not name or not requirement or not import_name:
            raise RuntimeDependencyError(
                "Runtime dependencies require name, requirement, and import_name."
            )
        if not _is_immutable_requirement(requirement):
            raise RuntimeDependencyError(
                f"Runtime dependency {name} must pin an exact version or full VCS commit."
            )
        local_source = _safe_relative_path(local_value) if local_value else None
        if local_source is not None:
            local_path = path.parent / local_source
            if local_path.exists() and not local_path.is_dir():
                raise RuntimeDependencyError(
                    f"Runtime dependency {name} local_source must be a directory."
                )
        dependencies.append(
            RuntimeDependency(
                name=name,
                requirement=requirement,
                import_name=import_name,
                local_source=local_source,
                optional=optional,
            )
        )
    return tuple(dependencies)


def _ensure_installed(root: Path, dependencies: list[RuntimeDependency]) -> Path:
    requirements = tuple(dependency.requirement for dependency in dependencies)
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "python": [sys.version_info.major, sys.version_info.minor],
                "requirements": requirements,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:20]
    parent = enoch_home(root) / "dependencies"
    target = parent / fingerprint
    complete = target / ".complete"
    if complete.is_file():
        return target

    parent.mkdir(parents=True, exist_ok=True)
    with _dependency_lock(parent / ".lock"):
        if complete.is_file():
            return target
        if target.exists():
            shutil.rmtree(target)
        temporary = parent / f".{fingerprint}.tmp-{uuid4().hex}"
        try:
            temporary.mkdir()
            _pip_install(requirements, temporary)
            (temporary / ".complete").write_text(
                "\n".join(requirements) + "\n",
                encoding="utf-8",
            )
            if target.exists():
                shutil.rmtree(target)
            temporary.replace(target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
    return target


def _pip_install(requirements: tuple[str, ...], target: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--target",
            str(target),
            *requirements,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or "pip install failed"
    raise RuntimeDependencyError(f"Could not install agent runtime dependencies: {detail}")


def _preloaded_dependency_paths() -> tuple[Path, ...]:
    value = os.environ.get(PRELOADED_PATHS_ENV, "")
    return _unique_paths(
        Path(part).expanduser().resolve()
        for part in value.split(os.pathsep)
        if part and Path(part).expanduser().is_dir()
    )


def _dependency_path(
    dependency: RuntimeDependency,
    paths: tuple[Path, ...],
) -> Path | None:
    top_level_import = dependency.import_name.split(".", 1)[0]
    for path in paths:
        if PathFinder.find_spec(top_level_import, [str(path)]) is not None:
            return path
    return None


def _unique_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(paths))


@contextmanager
def _dependency_lock(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        pass


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise RuntimeDependencyError(f"Unsafe runtime dependency local_source: {value!r}")
    if path.parts[0] in {".git", ".agent"}:
        raise RuntimeDependencyError(f"Protected runtime dependency local_source: {value!r}")
    return path


def _is_immutable_requirement(requirement: str) -> bool:
    if "git+" in requirement:
        return PINNED_VCS_REVISION.search(requirement) is not None
    return EXACT_PACKAGE_REQUIREMENT.fullmatch(requirement) is not None
