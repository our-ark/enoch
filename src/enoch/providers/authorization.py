from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Callable, Iterable

from enoch.providers.contracts import (
    AuthorizationDecision,
    AuthorizationPolicy,
    AuthorizationRequest,
    ProviderCapabilities,
    TaskRequirements,
)


DEFAULT_PROVIDER_CAPABILITIES = {
    "chat": frozenset(
        {
            "chat.receive",
            "chat.send",
            "chat.edit",
            "chat.ack",
            "chat.attachment",
        }
    ),
    "runtime": frozenset({"runtime.respond", "runtime.execute"}),
    "vcs": frozenset({"vcs.read", "vcs.write"}),
    "forge": frozenset(
        {
            "forge.read",
            "forge.publish",
            "forge.maintain",
            "forge.merge",
        }
    ),
    "service": frozenset({"service.read", "service.manage"}),
}

DEFAULT_TASK_REQUIREMENTS = TaskRequirements(
    capabilities=("runtime.execute", "vcs.write", "forge.publish"),
    reason="Execute, validate, and publish a tracked repository task.",
)


class CapabilityAuthorizationError(PermissionError):
    def __init__(
        self,
        request: AuthorizationRequest,
        decision: AuthorizationDecision,
    ) -> None:
        denied = ", ".join(decision.denied_capabilities) or "requested capabilities"
        detail = decision.reason or f"Denied capabilities: {denied}."
        if decision.denied_capabilities and denied not in detail:
            detail = f"{detail} Denied capabilities: {denied}."
        super().__init__(f"Authorization denied for {request.action}: {detail}")
        self.request = request
        self.decision = decision


class CapabilityAuthorizer:
    """Resolve provider grants, then apply an optional deny-only policy."""

    def __init__(
        self,
        provider_resolver: Callable[[str], object],
        *,
        policy: AuthorizationPolicy | None = None,
        profile_name: str = "",
    ) -> None:
        self.provider_resolver = provider_resolver
        self.policy = policy
        self.profile_name = profile_name.strip().lower()

    def require(
        self,
        action: str,
        requirements: TaskRequirements | Iterable[str],
        *,
        task_id: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> AuthorizationRequest:
        normalized = (
            requirements
            if isinstance(requirements, TaskRequirements)
            else TaskRequirements(tuple(requirements))
        )
        kinds = tuple(
            dict.fromkeys(
                capability.split(".", 1)[0]
                for capability in normalized.capabilities
            )
        )
        grants = tuple(self._provider_capabilities(kind) for kind in kinds)
        granted = frozenset(
            capability
            for provider in grants
            for capability in provider.capabilities
        )
        missing = tuple(
            capability
            for capability in normalized.capabilities
            if capability not in granted
        )
        request = AuthorizationRequest(
            action=action,
            requirements=normalized,
            provider_capabilities=grants,
            task_id=task_id,
            profile_name=self.profile_name,
            metadata=dict(metadata or {}),
        )
        if missing:
            raise CapabilityAuthorizationError(
                request,
                AuthorizationDecision(
                    allowed=False,
                    reason=(
                        "The selected provider does not declare every required "
                        "capability."
                    ),
                    denied_capabilities=missing,
                ),
            )
        if self.policy is None:
            return request
        decision = self.policy.authorize(request)
        if not isinstance(decision, AuthorizationDecision):
            raise TypeError(
                "Authorization policy must return AuthorizationDecision."
            )
        if not decision.allowed:
            raise CapabilityAuthorizationError(request, decision)
        return request

    def _provider_capabilities(self, kind: str) -> ProviderCapabilities:
        provider = self.provider_resolver(kind)
        declared = inspect.getattr_static(provider, "capabilities", None)
        if declared is None:
            return ProviderCapabilities(
                provider_kind=kind,
                capabilities=DEFAULT_PROVIDER_CAPABILITIES.get(kind, frozenset()),
            )
        raw = getattr(provider, "capabilities")
        if callable(raw):
            raw = raw()
        if isinstance(raw, ProviderCapabilities):
            if raw.provider_kind != kind:
                raise ValueError(
                    f"Selected {kind} provider declares kind {raw.provider_kind}."
                )
            return raw
        return ProviderCapabilities(
            provider_kind=kind,
            capabilities=frozenset(raw),
        )


@dataclass(frozen=True)
class CompositeAuthorizationPolicy:
    policies: tuple[AuthorizationPolicy, ...]

    def authorize(self, request: AuthorizationRequest) -> AuthorizationDecision:
        for policy in self.policies:
            decision = policy.authorize(request)
            if not isinstance(decision, AuthorizationDecision):
                raise TypeError(
                    "Authorization policy must return AuthorizationDecision."
                )
            if not decision.allowed:
                return decision
        return AuthorizationDecision(allowed=True)
