from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import os
from pathlib import Path
import re
import sys
import tomllib
from types import ModuleType


MANIFEST_NAME = "genesis.toml"
_PACKAGE_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_IDENTITY_NAME = re.compile(r"(?m)^name:\s*[\"']?([^\"'\n]+)")


class AgentContextError(RuntimeError):
    """Raised when a shared provider cannot identify its owning agent body."""


@dataclass(frozen=True)
class AgentContext:
    root: Path
    body_root: Path
    package: str
    name: str

    @property
    def env_prefix(self) -> str:
        return re.sub(r"[^A-Za-z0-9]", "_", self.package).upper()

    @property
    def private_directory(self) -> str:
        return f".{self.package.replace('_', '-')}"

    @property
    def service_slug(self) -> str:
        return self.package.replace("_", "-").lower()

    def module(self, component: str) -> ModuleType:
        cleaned = component.strip(".")
        if not cleaned or any(not part.isidentifier() for part in cleaned.split(".")):
            raise AgentContextError(f"Invalid agent module component: {component!r}")
        return import_module(f"{self.package}.{cleaned}")


def agent_context(root: Path | None = None) -> AgentContext:
    requested_root = Path(root or Path.cwd()).expanduser().resolve()
    body_root = _manifest_root(requested_root)
    if body_root is not None:
        manifest = body_root / MANIFEST_NAME
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
            raise AgentContextError(f"Could not read agent manifest {manifest}: {error}") from error
        package = str(data.get("package") or "").strip()
    else:
        package = os.environ.get("OUR_ARK_AGENT_PACKAGE", "").strip()
        body_root = _imported_body_root(package)
        if body_root is None:
            package, body_root = _discover_imported_agent()
        if body_root is None:
            raise AgentContextError(f"Could not find {MANIFEST_NAME} from {requested_root}.")
    if not _PACKAGE_NAME.fullmatch(package):
        raise AgentContextError(f"Invalid agent package: {package!r}")
    name = _identity_name(body_root, package) or package.replace("_", "-").title()
    return AgentContext(
        root=requested_root,
        body_root=body_root,
        package=package,
        name=name,
    )


def _manifest_root(start: Path) -> Path | None:
    candidate = start if start.is_dir() else start.parent
    for directory in (candidate, *candidate.parents):
        if directory.joinpath(MANIFEST_NAME).is_file():
            return directory
    return None


def _imported_body_root(package: str) -> Path | None:
    if not _PACKAGE_NAME.fullmatch(package):
        return None
    module = sys.modules.get(package)
    module_file = getattr(module, "__file__", None)
    if not module_file:
        try:
            module = import_module(package)
        except ImportError:
            return None
        module_file = getattr(module, "__file__", None)
    if not module_file:
        return None
    package_directory = Path(module_file).resolve().parent
    for directory in package_directory.parents:
        if directory.joinpath(MANIFEST_NAME).is_file():
            return directory
    return package_directory.parent


def _discover_imported_agent() -> tuple[str, Path | None]:
    candidates: dict[str, Path] = {}
    for package, module in tuple(sys.modules.items()):
        if "." in package or not _PACKAGE_NAME.fullmatch(package):
            continue
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        body_root = _imported_body_root(package)
        if body_root is None:
            continue
        manifest = body_root / MANIFEST_NAME
        if not manifest.is_file():
            continue
        try:
            declared = str(
                tomllib.loads(manifest.read_text(encoding="utf-8")).get("package") or ""
            ).strip()
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        if declared == package:
            candidates[package] = body_root
    if len(candidates) != 1:
        return "", None
    return next(iter(candidates.items()))


def _identity_name(root: Path, package: str) -> str:
    path = root / "src" / package / "identity.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    match = _IDENTITY_NAME.search(text)
    return match.group(1).strip() if match else ""
