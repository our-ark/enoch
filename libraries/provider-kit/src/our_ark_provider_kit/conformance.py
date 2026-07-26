from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from typing import Any, Protocol

from our_ark_provider_kit.contracts import (
    AgentRuntime,
    AgentRuntimeCancelled,
    AgentRuntimeTimedOut,
    ProviderCapabilities,
    RuntimeExecutionControl,
    RuntimeResult,
    normalize_runtime_result,
)


CONFORMANCE_API_VERSION = 1


class ProviderContractConformanceMixin:
    """Reusable structural checks for one provider implementation.

    Combine this mixin with ``unittest.TestCase`` and implement
    ``create_provider`` plus ``provider_protocol``.
    """

    provider_kind: str
    provider_protocol: type[Protocol]

    def create_provider(self, root: Path) -> Any:
        raise NotImplementedError

    def test_conformance_provider_matches_public_contract(self) -> None:
        with TemporaryDirectory() as directory:
            provider = self.create_provider(Path(directory))

        self.assertIsInstance(provider, self.provider_protocol)
        self.assertEqual(provider.provider_kind, self.provider_kind)
        self.assertTrue(str(provider.name).strip())

    def test_conformance_provider_capabilities_match_kind(self) -> None:
        with TemporaryDirectory() as directory:
            provider = self.create_provider(Path(directory))

        capabilities = getattr(provider, "capabilities", None)
        if capabilities is None:
            return
        if callable(capabilities):
            capabilities = capabilities()
        self.assertIsInstance(capabilities, ProviderCapabilities)
        self.assertEqual(capabilities.provider_kind, self.provider_kind)
        self.assertTrue(
            all(
                capability == self.provider_kind
                or capability.startswith(f"{self.provider_kind}.")
                for capability in capabilities.capabilities
            )
        )


class AgentRuntimeConformanceMixin(ProviderContractConformanceMixin):
    """Behavioral checks for the versioned agent-runtime contract."""

    provider_kind = "runtime"
    provider_protocol = AgentRuntime

    def create_runtime(self, root: Path) -> AgentRuntime:
        raise NotImplementedError

    def create_provider(self, root: Path) -> AgentRuntime:
        return self.create_runtime(root)

    def runtime_identity(self) -> Any:
        return _ConformanceIdentity()

    def test_conformance_runtime_returns_normalizable_results(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.create_runtime(root)
            result = normalize_runtime_result(
                runtime.respond(
                    self.runtime_identity(),
                    "conformance response",
                    cwd=root,
                    execution=RuntimeExecutionControl(request_id="conformance:respond"),
                )
            )

        self.assertIsInstance(result, RuntimeResult)
        self.assertTrue(result.final_text.strip())

    def test_conformance_runtime_honors_precancelled_execution(self) -> None:
        cancellation = threading.Event()
        cancellation.set()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.create_runtime(root)

            with self.assertRaises(AgentRuntimeCancelled):
                runtime.act_in_session(
                    self.runtime_identity(),
                    "must not execute",
                    cwd=root,
                    execution=RuntimeExecutionControl(
                        request_id="conformance:cancelled",
                        cancellation_event=cancellation,
                    ),
                )

    def test_conformance_runtime_honors_pretimed_out_execution(self) -> None:
        timeout = threading.Event()
        timeout.set()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.create_runtime(root)

            with self.assertRaises(AgentRuntimeTimedOut):
                runtime.act_in_session(
                    self.runtime_identity(),
                    "must not execute",
                    cwd=root,
                    execution=RuntimeExecutionControl(
                        request_id="conformance:timed-out",
                        timeout_event=timeout,
                    ),
                )


class _ConformanceIdentity:
    name = "Conformance"
    mission = "Verify the public runtime contract."
