from __future__ import annotations

from importlib import metadata
from inspect import Parameter, signature
import os
from pathlib import Path
from typing import Any, Callable, Literal

from enoch.config import read_section


ProviderKind = Literal["chat", "runtime", "vcs", "forge"]
ProviderFactory = Callable[[Path | None], Any]
ENTRY_POINT_GROUP = "enoch.providers"
DEFAULT_PROVIDERS: dict[ProviderKind, str] = {
    "chat": "telegram",
    "runtime": "codex",
    "vcs": "git",
    "forge": "github",
}
_REGISTERED: dict[tuple[ProviderKind, str], ProviderFactory] = {}


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
) -> None:
    normalized = _provider_id(name)
    key = (kind, normalized)
    if key in _REGISTERED and not replace:
        raise ProviderError(f"Provider {kind}.{normalized} is already registered.")
    _REGISTERED[key] = factory


def provider_name(kind: ProviderKind, root: Path | None = None) -> str:
    env_name = f"ENOCH_{kind.upper()}_PROVIDER"
    configured = os.environ.get(env_name, "").strip()
    if not configured:
        configured = read_section("providers", root).get(kind, "").strip()
    return _provider_id(configured or DEFAULT_PROVIDERS[kind])


def available_providers(kind: ProviderKind) -> tuple[str, ...]:
    names = {name for registered_kind, name in _REGISTERED if registered_kind == kind}
    names.add(DEFAULT_PROVIDERS[kind])
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
    selected = _provider_id(name) if name else provider_name(kind, root)
    factory = _REGISTERED.get((kind, selected))
    if factory is None:
        factory = _builtin_factory(kind, selected)
    if factory is None:
        factory = _entry_point_factory(kind, selected)
    if factory is None:
        available = ", ".join(available_providers(kind)) or "none"
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


def _builtin_factory(kind: ProviderKind, name: str) -> ProviderFactory | None:
    if kind == "runtime" and name == "codex":
        from enoch.providers.runtime import CodexRuntime

        return lambda _root=None: CodexRuntime()
    if kind == "chat" and name == "telegram":
        from enoch.telegram.client import TelegramClient, load_config

        return lambda root=None: TelegramClient(load_config(root))
    if kind == "forge" and name == "github":
        from enoch.providers.forge import GithubForgeProvider

        return lambda _root=None: GithubForgeProvider()
    if kind == "vcs" and name == "git":
        from enoch.providers.vcs import GitVersionControlProvider

        return lambda _root=None: GitVersionControlProvider()
    return None


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
    if not separator or kind_text not in DEFAULT_PROVIDERS:
        return None
    return kind_text, _provider_id(name)  # type: ignore[return-value]


def _provider_id(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not normalized or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in normalized):
        raise ProviderError(f"Invalid provider name {value!r}.")
    return normalized
