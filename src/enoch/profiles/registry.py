from __future__ import annotations

from importlib import metadata
from inspect import Parameter, signature
import os
from pathlib import Path
from typing import Callable

from enoch.config import read_section
from enoch.profiles.contracts import AgentProfile, ProfileError


ENTRY_POINT_GROUP = "our_ark.profiles"
ProfileFactory = Callable[[Path | None], AgentProfile]
_REGISTERED: dict[str, ProfileFactory] = {}


def register_profile(
    name: str,
    factory: ProfileFactory,
    *,
    replace: bool = False,
) -> None:
    normalized = _profile_name(name)
    if normalized in _REGISTERED and not replace:
        raise ProfileError(f"Profile {normalized} is already registered.")
    _REGISTERED[normalized] = factory


def available_profiles() -> tuple[str, ...]:
    names = set(_REGISTERED)
    names.update(entry.name.strip().lower() for entry in _entry_points())
    return tuple(sorted(name for name in names if name))


def load_profile(root: Path | None = None, *, name: str = "") -> AgentProfile:
    selected = name.strip() or os.environ.get("ENOCH_PROFILE", "").strip()
    if not selected:
        selected = read_section("agent", root).get("profile", "").strip()
    if not selected:
        return AgentProfile(name="enoch")
    selected = _profile_name(selected)
    factory = _REGISTERED.get(selected) or _entry_point_factory(selected)
    if factory is None:
        choices = ", ".join(available_profiles()) or "none"
        raise ProfileError(f"Unknown profile {selected!r}. Available profiles: {choices}.")
    profile = factory(root) if _factory_accepts_root(factory) else factory()  # type: ignore[call-arg]
    if not isinstance(profile, AgentProfile):
        raise ProfileError(f"Profile factory {selected!r} did not return AgentProfile.")
    if profile.name != selected:
        raise ProfileError(
            f"Profile entry {selected!r} returned profile name {profile.name!r}."
        )
    return profile


def _entry_point_factory(name: str) -> ProfileFactory | None:
    entry = next((entry for entry in _entry_points() if entry.name.strip().lower() == name), None)
    if entry is None:
        return None
    try:
        factory = entry.load()
    except Exception as error:
        raise ProfileError(f"Could not load profile {name}: {error}") from error
    if not callable(factory):
        raise ProfileError(f"Profile entry point {name} is not callable.")
    return factory


def _entry_points() -> tuple[metadata.EntryPoint, ...]:
    discovered = metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=ENTRY_POINT_GROUP))
    return tuple(discovered.get(ENTRY_POINT_GROUP, ()))


def _factory_accepts_root(factory: ProfileFactory) -> bool:
    try:
        parameters = signature(factory).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(
        parameter.kind in {Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD}
        for parameter in parameters
    ) or any(parameter.kind == Parameter.VAR_POSITIONAL for parameter in parameters)


def _profile_name(value: str) -> str:
    try:
        return AgentProfile(name=value).name
    except ProfileError as error:
        raise ProfileError(f"Invalid profile name {value!r}.") from error
