"""Capability contract fallback for installations pinned to provider-kit 0.3."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


CAPABILITY_CONTRACT_VERSION = 1


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_kind: str
    capabilities: frozenset[str]
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        kind = _segment(self.provider_kind)
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError("Unsupported provider capability contract version.")
        capabilities = frozenset(_capability(value) for value in self.capabilities)
        if any(value.split(".", 1)[0] != kind for value in capabilities):
            raise ValueError("Provider capabilities must match the provider kind.")
        object.__setattr__(self, "provider_kind", kind)
        object.__setattr__(self, "capabilities", capabilities)

    def supports(self, capability: str) -> bool:
        return _capability(capability) in self.capabilities


@dataclass(frozen=True)
class TaskRequirements:
    capabilities: tuple[str, ...] = ()
    reason: str = ""
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError("Unsupported task requirement contract version.")
        object.__setattr__(
            self,
            "capabilities",
            tuple(dict.fromkeys(_capability(value) for value in self.capabilities)),
        )
        object.__setattr__(self, "reason", str(self.reason).strip())


@dataclass(frozen=True)
class AuthorizationRequest:
    action: str
    requirements: TaskRequirements
    provider_capabilities: tuple[ProviderCapabilities, ...]
    task_id: int | None = None
    profile_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError("Unsupported authorization request contract version.")
        action = str(self.action).strip().lower().replace("_", "-")
        if not action:
            raise ValueError("Authorization action is required.")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "profile_name", self.profile_name.strip().lower())


@dataclass(frozen=True)
class AuthorizationDecision:
    allowed: bool
    reason: str = ""
    denied_capabilities: tuple[str, ...] = ()
    contract_version: int = CAPABILITY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != CAPABILITY_CONTRACT_VERSION:
            raise ValueError("Unsupported authorization decision contract version.")
        denied = tuple(
            dict.fromkeys(_capability(value) for value in self.denied_capabilities)
        )
        if self.allowed and denied:
            raise ValueError("Allowed decisions cannot deny capabilities.")
        object.__setattr__(self, "allowed", bool(self.allowed))
        object.__setattr__(self, "reason", str(self.reason).strip())
        object.__setattr__(self, "denied_capabilities", denied)


@runtime_checkable
class AuthorizationPolicy(Protocol):
    def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision: ...


@runtime_checkable
class CapabilityProvider(Protocol):
    @property
    def capabilities(self) -> ProviderCapabilities: ...


def _capability(value: object) -> str:
    parts = str(value).strip().lower().replace("_", "-").split(".")
    if len(parts) != 2:
        raise ValueError("Capabilities must use <provider-kind>.<operation>.")
    return ".".join(_segment(part) for part in parts)


def _segment(value: object) -> str:
    text = str(value).strip().lower().replace("_", "-")
    if (
        not text
        or not text[0].isalpha()
        or any(not (character.isalnum() or character == "-") for character in text)
    ):
        raise ValueError(f"Invalid capability segment {value!r}.")
    return text
