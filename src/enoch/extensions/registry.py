from __future__ import annotations

from importlib import metadata
from inspect import Parameter, signature
import os
from pathlib import Path
from typing import Callable, Iterable

from enoch.config import read_section
from enoch.extensions.contracts import AgentExtension, AgentExtensionError


ENTRY_POINT_GROUP = "our_ark.extensions"
ExtensionFactory = Callable[[Path | None], AgentExtension]
_REGISTERED: dict[str, ExtensionFactory] = {}


def register_extension(
    name: str,
    factory: ExtensionFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = AgentExtension(name=name).name
    if normalized in _REGISTERED and not replace:
        raise AgentExtensionError(
            f"Agent extension {normalized} is already registered."
        )
    _REGISTERED[normalized] = factory


def available_extensions() -> tuple[str, ...]:
    names = {*_REGISTERED}
    names.update(entry.name.strip().lower() for entry in _entry_points())
    return tuple(sorted(name for name in names if name))


def load_extensions(
    root: Path | None = None,
    *,
    names: Iterable[str] | None = None,
) -> tuple[AgentExtension, ...]:
    selected = (
        tuple(names)
        if names is not None
        else _configured_extension_names(root)
    )
    extensions = tuple(_load_extension(name, root) for name in selected)
    duplicates = sorted(
        name
        for name in {extension.name for extension in extensions}
        if sum(extension.name == name for extension in extensions) > 1
    )
    if duplicates:
        raise AgentExtensionError(
            "Duplicate agent extensions selected: " + ", ".join(duplicates) + "."
        )
    return extensions


def _load_extension(name: str, root: Path | None) -> AgentExtension:
    selected = AgentExtension(name=name).name
    factory = _REGISTERED.get(selected) or _entry_point_factory(selected)
    if factory is None:
        choices = ", ".join(available_extensions()) or "none"
        raise AgentExtensionError(
            f"Unknown agent extension {selected!r}. "
            f"Available extensions: {choices}."
        )
    extension = (
        factory(root)
        if _factory_accepts_root(factory)
        else factory()  # type: ignore[call-arg]
    )
    if not isinstance(extension, AgentExtension):
        raise AgentExtensionError(
            f"Agent extension factory {selected!r} did not return AgentExtension."
        )
    if extension.name != selected:
        raise AgentExtensionError(
            f"Agent extension entry {selected!r} returned name "
            f"{extension.name!r}."
        )
    return extension


def _configured_extension_names(root: Path | None) -> tuple[str, ...]:
    value = os.environ.get("ENOCH_EXTENSIONS", "").strip()
    if not value:
        value = read_section("agent", root).get("extensions", "").strip()
    return tuple(
        part.strip()
        for part in value.split(",")
        if part.strip()
    )


def _entry_point_factory(name: str) -> ExtensionFactory | None:
    entry = next(
        (
            entry
            for entry in _entry_points()
            if entry.name.strip().lower() == name
        ),
        None,
    )
    if entry is None:
        return None
    try:
        factory = entry.load()
    except Exception as error:
        raise AgentExtensionError(
            f"Could not load agent extension {name}: {error}"
        ) from error
    if not callable(factory):
        raise AgentExtensionError(
            f"Agent extension entry point {name} is not callable."
        )
    return factory


def _entry_points() -> tuple[metadata.EntryPoint, ...]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=ENTRY_POINT_GROUP))
    return tuple(discovered.get(ENTRY_POINT_GROUP, ()))


def _factory_accepts_root(factory: ExtensionFactory) -> bool:
    try:
        parameters = signature(factory).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind
        in {Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD}
        for parameter in parameters
    ) or any(
        parameter.kind == Parameter.VAR_POSITIONAL
        for parameter in parameters
    )
