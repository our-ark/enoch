from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from our_ark_provider_kit.contracts import (
    AuthoritativeBase,
    ChangeCaptureRequest,
    ChangeCaptureResult,
    ProviderCapabilities,
    RepositoryFeatures,
    RepositoryProviderError,
    RepositoryRevision,
    RepositoryWorkspace,
    ReviewCloseRequest,
    ReviewFeatures,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewLandResult,
    ReviewProviderError,
    ReviewRecord,
    ReviewSubmission,
    ReviewVersion,
    WorkingCopyState,
    WorkspaceRequest,
)


class BranchlessRepositoryFixture:
    """In-memory repository with immutable revisions and no staging or branches."""

    name = "branchless-fixture"
    provider_kind = "vcs"
    capabilities = ProviderCapabilities(
        provider_kind="vcs",
        capabilities=frozenset(
            {
                "vcs.inspect",
                "vcs.resolve",
                "vcs.ancestry",
                "vcs.authoritative",
                "vcs.capture",
                "vcs.workspace",
                "vcs.restore",
            }
        ),
    )
    repository_features = RepositoryFeatures(
        staging_index=False,
        named_branches=False,
        isolated_workspaces=True,
        immutable_revisions=True,
    )

    def __init__(self) -> None:
        initial = RepositoryRevision("r0", display="initial")
        self.revisions = {initial.id: initial}
        self.parents: dict[str, str] = {}
        self.current = initial
        self.authoritative = initial
        self.changed_paths: tuple[str, ...] = ()
        self.workspaces: dict[str, RepositoryWorkspace] = {}
        self.operation_count = 0

    def mark_changed(self, *paths: str) -> None:
        self.changed_paths = tuple(dict.fromkeys(path for path in paths if path))

    def inspect_working_copy(
        self,
        root: Path | None = None,
    ) -> WorkingCopyState:
        del root
        return WorkingCopyState(
            revision=self.current,
            clean=not self.changed_paths,
            changed_paths=self.changed_paths,
            summary=(
                "No working-copy changes."
                if not self.changed_paths
                else ", ".join(self.changed_paths)
            ),
        )

    def resolve_repository_revision(
        self,
        reference: str,
        root: Path | None = None,
    ) -> RepositoryRevision | None:
        del root
        if reference == "authoritative":
            return self.authoritative
        return self.revisions.get(reference)

    def repository_is_ancestor(
        self,
        ancestor: RepositoryRevision,
        descendant: RepositoryRevision,
        root: Path | None = None,
    ) -> bool:
        del root
        current = descendant.id
        while current:
            if current == ancestor.id:
                return True
            current = self.parents.get(current, "")
        return False

    def authoritative_base(
        self,
        root: Path | None = None,
        *,
        refresh: bool = False,
    ) -> AuthoritativeBase:
        del root
        return AuthoritativeBase(
            revision=self.authoritative,
            name="authoritative",
            refreshed=refresh,
        )

    def capture_change(
        self,
        request: ChangeCaptureRequest,
        root: Path | None = None,
    ) -> ChangeCaptureResult:
        del root
        paths = request.paths or self.changed_paths
        if not paths:
            raise RepositoryProviderError("No working-copy changes to capture.")
        self.operation_count += 1
        revision = RepositoryRevision(
            f"r{len(self.revisions)}",
            display=request.message,
        )
        self.parents[revision.id] = self.current.id
        self.revisions[revision.id] = revision
        self.current = revision
        self.changed_paths = ()
        return ChangeCaptureResult(
            revision=revision,
            changed_paths=tuple(paths),
            summary=request.message,
        )

    def restore_repository_revision(
        self,
        revision: RepositoryRevision,
        root: Path | None = None,
    ) -> None:
        del root
        resolved = self.revisions.get(revision.id)
        if resolved is None:
            raise RepositoryProviderError(f"Unknown revision {revision.id}.")
        self.operation_count += 1
        self.current = resolved
        self.changed_paths = ()

    def list_repository_workspaces(
        self,
        root: Path | None = None,
    ) -> tuple[RepositoryWorkspace, ...]:
        del root
        return tuple(self.workspaces.values())

    def create_repository_workspace(
        self,
        request: WorkspaceRequest,
        root: Path | None = None,
    ) -> RepositoryWorkspace:
        del root
        workspace_id = request.workspace_id or f"workspace-{len(self.workspaces) + 1}"
        if workspace_id in self.workspaces:
            raise RepositoryProviderError(f"Workspace {workspace_id} already exists.")
        self.operation_count += 1
        workspace = RepositoryWorkspace(
            id=workspace_id,
            path=request.path,
            base_revision=request.base_revision,
            current_revision=request.base_revision,
        )
        self.workspaces[workspace_id] = workspace
        return workspace

    def remove_repository_workspace(
        self,
        workspace: RepositoryWorkspace,
        root: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        del root, force
        self.operation_count += 1
        self.workspaces.pop(workspace.id, None)


class IndependentReviewFixture:
    """In-memory review system whose ids are unrelated to revisions or branches."""

    name = "independent-review-fixture"
    provider_kind = "forge"
    capabilities = ProviderCapabilities(
        provider_kind="forge",
        capabilities=frozenset(
            {
                "forge.review",
                "forge.inspect",
                "forge.close",
                "forge.land",
                "forge.stack",
            }
        ),
    )
    review_features = ReviewFeatures(
        stacked_changes=True,
        signals=True,
        landing=True,
        mutable_versions=True,
    )

    def __init__(self) -> None:
        self.reviews: dict[str, ReviewRecord] = {}
        self.operation_count = 0

    def publish_review(
        self,
        request: ReviewSubmission,
        root: Path | None = None,
    ) -> ReviewRecord:
        del root
        self.operation_count += 1
        if request.review is None:
            review_id = f"review-{len(self.reviews) + 1}"
            identity = ReviewIdentity(
                id=review_id,
                url=f"https://reviews.invalid/{review_id}",
            )
            versions: tuple[ReviewVersion, ...] = ()
        else:
            identity = request.review
            previous = self.reviews.get(identity.id)
            if previous is None:
                raise ReviewProviderError(f"Unknown review {identity.id}.")
            versions = previous.versions
        version = ReviewVersion(
            id=f"{identity.id}:v{len(versions) + 1}",
            revision=request.revision,
        )
        record = ReviewRecord(
            identity=identity,
            title=request.title,
            body=request.body,
            state="open",
            versions=(*versions, version),
            dependencies=request.dependencies,
            draft=request.draft,
        )
        self.reviews[identity.id] = record
        return record

    def inspect_review(
        self,
        review: ReviewIdentity,
        root: Path | None = None,
    ) -> ReviewRecord:
        del root
        try:
            return self.reviews[review.id]
        except KeyError as error:
            raise ReviewProviderError(f"Unknown review {review.id}.") from error

    def list_open_reviews(
        self,
        root: Path | None = None,
        *,
        limit: int = 20,
    ) -> tuple[ReviewRecord, ...]:
        del root
        return tuple(
            record
            for record in self.reviews.values()
            if record.state == "open"
        )[:limit]

    def close_review(
        self,
        request: ReviewCloseRequest,
        root: Path | None = None,
    ) -> ReviewRecord:
        del root
        current = self.inspect_review(request.review)
        self.operation_count += 1
        closed = replace(current, state="closed")
        self.reviews[current.identity.id] = closed
        return closed

    def land_review(
        self,
        request: ReviewLandRequest,
        root: Path | None = None,
    ) -> ReviewLandResult:
        del root
        current = self.inspect_review(request.review)
        self.operation_count += 1
        landed_at = datetime.now(timezone.utc).isoformat()
        revision = current.versions[-1].revision
        landed = replace(
            current,
            state="landed",
            landed_revision=revision,
            landed_at=landed_at,
        )
        self.reviews[current.identity.id] = landed
        return ReviewLandResult(
            review=current.identity,
            status="landed",
            revision=revision,
            landed_at=landed_at,
            message=f"Landed with {request.strategy}.",
        )
