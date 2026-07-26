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
    ChangeCaptureRequest,
    RepositoryProvider,
    ReviewCloseRequest,
    ReviewLandRequest,
    ReviewProvider,
    ReviewSubmission,
    RepositoryRevision,
    WorkspaceRequest,
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


class RepositoryProviderConformanceMixin:
    """Behavioral checks for branch- and staging-neutral repositories."""

    def create_repository_provider(self, root: Path) -> RepositoryProvider:
        raise NotImplementedError

    def prepare_repository_change(
        self,
        provider: RepositoryProvider,
        root: Path,
    ) -> tuple[str, ...]:
        marker = getattr(provider, "mark_changed", None)
        if marker is None:
            raise NotImplementedError(
                "prepare_repository_change must arrange one hermetic change."
            )
        marker("CONFORMANCE.md")
        return ("CONFORMANCE.md",)

    def test_conformance_repository_uses_opaque_revisions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_repository_provider(root)
            state = provider.inspect_working_copy(root)
            resolved = provider.resolve_repository_revision(state.revision.id, root)
            base = provider.authoritative_base(root, refresh=True)
            ancestor = provider.repository_is_ancestor(
                base.revision,
                state.revision,
                root,
            )

            self.assertIsInstance(provider, RepositoryProvider)
            self.assertEqual(provider.provider_kind, "vcs")
            self.assertEqual(resolved, state.revision)
            self.assertTrue(ancestor)

    def test_conformance_repository_captures_without_staging_assumption(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_repository_provider(root)
            paths = self.prepare_repository_change(provider, root)
            captured = provider.capture_change(
                ChangeCaptureRequest(
                    message="Capture conformance change",
                    paths=paths,
                ),
                root,
            )
            state = provider.inspect_working_copy(root)

        self.assertEqual(captured.changed_paths, paths)
        self.assertEqual(state.revision, captured.revision)
        self.assertTrue(state.clean)

    def test_conformance_repository_manages_isolated_workspace(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_repository_provider(root)
            base = provider.authoritative_base(root)
            workspace = provider.create_repository_workspace(
                WorkspaceRequest(
                    path=root / "workspace",
                    base_revision=base.revision,
                ),
                root,
            )
            self.assertIn(workspace, provider.list_repository_workspaces(root))
            provider.remove_repository_workspace(workspace, root)
            self.assertNotIn(workspace, provider.list_repository_workspaces(root))


class ReviewProviderConformanceMixin:
    """Behavioral checks for reviews independent of PR numbers and branches."""

    def create_review_provider(self, root: Path) -> ReviewProvider:
        raise NotImplementedError

    def test_conformance_review_identity_is_independent_from_revision(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_review_provider(root)
            revision = RepositoryRevision("change-alpha")
            review = provider.publish_review(
                ReviewSubmission(
                    title="Review alpha",
                    body="Evidence.",
                    revision=revision,
                ),
                root,
            )

        self.assertIsInstance(provider, ReviewProvider)
        self.assertEqual(provider.provider_kind, "forge")
        self.assertNotEqual(review.identity.id, revision.id)
        self.assertEqual(review.versions[-1].revision, revision)

    def test_conformance_review_represents_stack_and_versions(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_review_provider(root)
            if not provider.review_features.stacked_changes:
                self.skipTest("provider does not advertise stacked changes")
            first = provider.publish_review(
                ReviewSubmission(
                    title="First change",
                    body="First.",
                    revision=RepositoryRevision("change-1"),
                ),
                root,
            )
            second = provider.publish_review(
                ReviewSubmission(
                    title="Second change",
                    body="Second.",
                    revision=RepositoryRevision("change-2"),
                    dependencies=(first.identity,),
                ),
                root,
            )
            updated = provider.publish_review(
                ReviewSubmission(
                    title="Second change",
                    body="Updated.",
                    revision=RepositoryRevision("change-3"),
                    review=second.identity,
                    dependencies=(first.identity,),
                ),
                root,
            )
            inspected = provider.inspect_review(updated.identity, root)

        self.assertEqual(second.dependencies, (first.identity,))
        self.assertEqual(len(updated.versions), 2)
        self.assertEqual(updated.versions[-1].revision.id, "change-3")
        self.assertEqual(inspected, updated)

    def test_conformance_review_closes_and_lands_by_opaque_identity(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = self.create_review_provider(root)
            if not provider.review_features.landing:
                self.skipTest("provider does not advertise landing")
            closed_target = provider.publish_review(
                ReviewSubmission(
                    title="Close this",
                    body="",
                    revision=RepositoryRevision("close-change"),
                ),
                root,
            )
            landed_target = provider.publish_review(
                ReviewSubmission(
                    title="Land this",
                    body="",
                    revision=RepositoryRevision("land-change"),
                ),
                root,
            )
            closed = provider.close_review(
                ReviewCloseRequest(closed_target.identity, note="Superseded."),
                root,
            )
            landed = provider.land_review(
                ReviewLandRequest(landed_target.identity),
                root,
            )
            inspected_landed = provider.inspect_review(
                landed_target.identity,
                root,
            )

        self.assertEqual(closed.state, "closed")
        self.assertEqual(landed.status, "landed")
        self.assertEqual(landed.revision.id, "land-change")
        self.assertTrue(landed.landed_at)
        self.assertEqual(inspected_landed.state, "landed")
        self.assertEqual(
            inspected_landed.landed_revision.id,
            "land-change",
        )
        self.assertEqual(inspected_landed.landed_at, landed.landed_at)


class _ConformanceIdentity:
    name = "Conformance"
    mission = "Verify the public runtime contract."
