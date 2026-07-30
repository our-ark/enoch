"""Reusable conformance suites for Enoch extension implementations."""

from enoch.runtime_dependencies import activate_runtime_dependencies


activate_runtime_dependencies()

from our_ark_provider_kit.conformance import (
    CONFORMANCE_API_VERSION,
    AgentRuntimeConformanceMixin,
    ProviderContractConformanceMixin,
    RepositoryProviderConformanceMixin,
    ReviewProviderConformanceMixin,
)

from enoch.conformance.profile import ProfileCommandCase, ProfileConformanceMixin
from enoch.conformance.extension import (
    AgentExtensionConformanceMixin,
    ExtensionCommandCase,
)
from enoch.conformance.notification import DurableNotificationConformanceMixin
from enoch.conformance.workflow import WorkflowEngineConformanceMixin


__all__ = [
    "CONFORMANCE_API_VERSION",
    "AgentRuntimeConformanceMixin",
    "AgentExtensionConformanceMixin",
    "DurableNotificationConformanceMixin",
    "ExtensionCommandCase",
    "ProfileCommandCase",
    "ProfileConformanceMixin",
    "ProviderContractConformanceMixin",
    "RepositoryProviderConformanceMixin",
    "ReviewProviderConformanceMixin",
    "WorkflowEngineConformanceMixin",
]
