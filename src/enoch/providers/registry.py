from __future__ import annotations

from importlib import import_module, metadata
from inspect import Parameter, signature
import os
from pathlib import Path
from typing import Any, Callable, Literal

from enoch.config import read_section
from enoch.runtime_dependencies import load_runtime_dependencies


ProviderKind = Literal["chat", "runtime", "vcs", "forge", "service"]
ProviderFactory = Callable[[Path | None], Any]
ProviderSetup = Callable[..., str]
PROVIDER_KINDS: tuple[ProviderKind, ...] = ("chat", "runtime", "vcs", "forge", "service")
ENTRY_POINT_GROUP = "enoch.providers"
PLUGIN_ATTRIBUTE = "ENOCH_PROVIDERS"
CORE_PROVIDER_MODULES = (
    "enoch.providers.runtime",
    "enoch.providers.vcs",
    "enoch.providers.forge",
)
SOURCE_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REGISTERED: dict[tuple[ProviderKind, str], ProviderFactory] = {}
_DEFAULTS: dict[ProviderKind, str] = {}
_SETUP_HANDLERS: dict[tuple[ProviderKind, str], ProviderSetup] = {}
_LOADED_PLUGIN_MODULES: set[str] = set()


class ProviderError(RuntimeError):
    pass


class ProviderNotFound(ProviderError):
    pass


def register_provider(
    kind: ProviderKind,
    name: str,
    factory: ProviderFactory,
    *,
    replace: bool = False,
    default: bool = False,
) -> None:
    if kind not in PROVIDER_KINDS:
        raise ProviderError(f"Invalid provider kind {kind!r}.")
    normalized = _provider_id(name)
    key = (kind, normalized)
    if key in _REGISTERED and not replace:
        raise ProviderError(f"Provider {kind}.{normalized} is already registered.")
    _REGISTERED[key] = factory
    if default:
        _DEFAULTS[kind] = normalized


def configure_provider(
    kind: ProviderKind,
    text: str,
    root: Path,
    *,
    name: str = "",
    prompt: Callable[[str], str] | None = None,
    prefix: str = "",
) -> str:
    _load_provider_plugins(root)
    selected = _provider_id(name) if name else provider_name(kind, root)
    handler = _SETUP_HANDLERS.get((kind, selected))
    if handler is None:
        raise ProviderError(f"Provider {kind}.{selected} does not expose setup commands.")
    return handler(text, root, prompt=prompt, prefix=prefix)


def provider_name(kind: ProviderKind, root: Path | None = None) -> str:
    _require_kind(kind)
    _load_provider_plugins(root)
    env_name = f"ENOCH_{kind.upper()}_PROVIDER"
    configured = os.environ.get(env_name, "").strip()
    if not configured:
        configured = read_section("providers", root).get(kind, "").strip()
    if configured:
        return _provider_id(configured)
    if default := _DEFAULTS.get(kind):
        return default
    choices = available_providers(kind, root)
    if len(choices) == 1:
        return choices[0]
    if not choices:
        raise ProviderNotFound(f"No {kind} provider is installed.")
    raise ProviderError(
        f"Multiple {kind} providers are installed. Configure one of: {', '.join(choices)}."
    )


def available_providers(kind: ProviderKind, root: Path | None = None) -> tuple[str, ...]:
    _require_kind(kind)
    _load_provider_plugins(root)
    names = {name for registered_kind, name in _REGISTERED if registered_kind == kind}
    for entry_point in _entry_points():
        parsed = _entry_point_key(entry_point.name)
        if parsed is not None and parsed[0] == kind:
            names.add(parsed[1])
    return tuple(sorted(names))


def load_provider(
    kind: ProviderKind,
    root: Path | None = None,
    *,
    name: str = "",
) -> Any:
    _load_provider_plugins(root)
    selected = _provider_id(name) if name else provider_name(kind, root)
    factory = _REGISTERED.get((kind, selected))
    if factory is None:
        factory = _entry_point_factory(kind, selected)
    if factory is None:
        available = ", ".join(available_providers(kind, root)) or "none"
        raise ProviderNotFound(
            f"Unknown {kind} provider {selected!r}. Available providers: {available}."
        )
    provider = factory(root) if _factory_accepts_root(factory) else factory()
    actual_kind = str(getattr(provider, "provider_kind", "")).strip().lower()
    if actual_kind != kind:
        raise ProviderError(
            f"Provider {kind}.{selected} returned provider_kind={actual_kind or 'missing'}."
        )
    return provider


def _load_provider_plugins(root: Path | None) -> None:
    modules = [*CORE_PROVIDER_MODULES]
    dependencies = load_runtime_dependencies(root)
    if not dependencies and root is not None:
        dependencies = load_runtime_dependencies(SOURCE_PROJECT_ROOT)
    modules.extend(
        dependency.import_name
        for dependency in dependencies
        if dependency.import_name
    )
    for module_name in modules:
        if module_name in _LOADED_PLUGIN_MODULES:
            continue
        try:
            module = import_module(module_name)
        except ImportError:
            continue
        _register_module_plugins(module_name, getattr(module, PLUGIN_ATTRIBUTE, ()))
        _LOADED_PLUGIN_MODULES.add(module_name)


def _register_module_plugins(module_name: str, specs: object) -> None:
    if not isinstance(specs, (tuple, list)):
        raise ProviderError(f"{module_name}.{PLUGIN_ATTRIBUTE} must be a list or tuple.")
    for spec in specs:
        if not isinstance(spec, dict):
            raise ProviderError(f"{module_name} provider descriptors must be mappings.")
        kind = str(spec.get("kind") or "").strip().lower()
        name = str(spec.get("name") or "").strip()
        factory = spec.get("factory")
        if kind not in PROVIDER_KINDS or not callable(factory):
            raise ProviderError(f"{module_name} exports an invalid provider descriptor.")
        supports = spec.get("supports")
        if supports is not None:
            if not callable(supports):
                raise ProviderError(f"{module_name} provider supports must be callable.")
            if not supports():
                continue
        register_provider(
            kind,  # type: ignore[arg-type]
            name,
            factory,
            replace=True,
            default=bool(spec.get("default")),
        )
        setup = spec.get("setup")
        if setup is not None:
            if not callable(setup):
                raise ProviderError(f"{module_name} provider setup must be callable.")
            _SETUP_HANDLERS[(kind, _provider_id(name))] = setup  # type: ignore[index]


def _entry_point_factory(kind: ProviderKind, name: str) -> ProviderFactory | None:
    expected = f"{kind}.{name}"
    for entry_point in _entry_points():
        if entry_point.name == expected:
            loaded = entry_point.load()
            if not callable(loaded):
                raise ProviderError(f"Entry point {expected} must export a provider factory.")
            return loaded
    return None


def _factory_accepts_root(factory: ProviderFactory) -> bool:
    try:
        parameters = signature(factory).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind
        in {
            Parameter.POSITIONAL_ONLY,
            Parameter.POSITIONAL_OR_KEYWORD,
            Parameter.VAR_POSITIONAL,
        }
        for parameter in parameters
    )


def _entry_points() -> tuple[metadata.EntryPoint, ...]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=ENTRY_POINT_GROUP))
    return tuple(discovered.get(ENTRY_POINT_GROUP, ()))


def _entry_point_key(value: str) -> tuple[ProviderKind, str] | None:
    kind_text, separator, name = value.partition(".")
    if not separator or kind_text not in PROVIDER_KINDS:
        return None
    return kind_text, _provider_id(name)  # type: ignore[return-value]


def _require_kind(kind: str) -> None:
    if kind not in PROVIDER_KINDS:
        raise ProviderError(f"Invalid provider kind {kind!r}.")


def _provider_id(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not normalized or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
        for character in normalized
    ):
        raise ProviderError(f"Invalid provider name {value!r}.")
    return normalized
