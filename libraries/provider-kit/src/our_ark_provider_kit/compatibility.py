from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from our_ark_provider_kit.contracts import (
    AuthoritativeBase,
    ChangeCaptureRequest,
    ChangeCaptureResult,
    ForgeProviderError,
    ProviderCapabilities,
    RepositoryFeatures,
    RepositoryProvider,
    RepositoryProviderError,
    RepositoryRevision,
    RepositoryWorkspace,
    ReviewCloseRequest,
    ReviewFeatures,
    ReviewIdentity,
    ReviewLandRequest,
    ReviewLandResult,
    ReviewProvider,
    ReviewProviderError,
    ReviewRecord,
    ReviewSignal,
    ReviewSubmission,
    ReviewVersion,
    WorkingCopyState,
    WorkspaceRequest,
    require_review_features,
)


class LegacyVersionControlRepositoryAdapter:
    """Expose a branch/staging VCS through the semantic repository contract."""

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
        staging_index=True,
        named_branches=True,
        isolated_workspaces=True,
        immutable_revisions=True,
    )

    def __init__(self, provider: Any) -> None:
        self.legacy = provider
        self.name = str(getattr(provider, "name", "legacy-vcs")).strip() or "legacy-vcs"
        self.capabilities = _combined_capabilities(
            self.capabilities,
            getattr(provider, "capabilities", None),
        )

    def inspect_working_copy(
        self,
        root: Path | None = None,
    ) -> WorkingCopyState:
        revision = self._revision(self.legacy.current_revision(root))
        clean = bool(self.legacy.is_clean(root))
        paths = () if clean else tuple(self.legacy.changed_files(root))
        return WorkingCopyState(
            revision=revision,
            clean=clean,
            changed_paths=paths,
            summary=str(self.legacy.diff_summary(root)),
        )

    def resolve_repository_revision(
        self,
        reference: str,
        root: Path | None = None,
    ) -> RepositoryRevision | None:
        resolved = str(self.legacy.resolve_revision(reference, root)).strip()
        return self._revision(resolved, display=reference) if resolved else None

    def repository_is_ancestor(
        self,
        ancestor: RepositoryRevision,
        descendant: RepositoryRevision,
        root: Path | None = None,
    ) -> bool:
        return bool(self.legacy.is_ancestor(ancestor.id, descendant.id, root))

    def authoritative_base(
        self,
        root: Path | None = None,
        *,
        refresh: bool = False,
    ) -> AuthoritativeBase:
        if refresh:
            self.legacy.refresh_authoritative(root)
        revision = str(self.legacy.authoritative_revision(root)).strip()
        if not revision:
            raise RepositoryProviderError(
                "Legacy VCS returned an empty authoritative revision."
            )
        name = str(self.legacy.authoritative_branch(root)).strip()
        return AuthoritativeBase(
            revision=self._revision(revision, display=name or revision),
            name=name,
            refreshed=refresh,
        )

    def capture_change(
        self,
        request: ChangeCaptureRequest,
        root: Path | None = None,
    ) -> ChangeCaptureResult:
        paths = request.paths or tuple(self.legacy.changed_files(root))
        if not paths:
            raise RepositoryProviderError("No working-copy changes to capture.")
        self.legacy.stage(paths, root)
        self.legacy.commit(request.message, root)
        revision = str(self.legacy.current_revision(root)).strip()
        if not revision:
            raise RepositoryProviderError("Legacy VCS returned an empty revision.")
        return ChangeCaptureResult(
            revision=self._revision(revision),
            changed_paths=tuple(paths),
            summary=request.message,
        )

    def restore_repository_revision(
        self,
        revision: RepositoryRevision,
        root: Path | None = None,
    ) -> None:
        self.legacy.restore_revision(revision.id, root)

    def list_repository_workspaces(
        self,
        root: Path | None = None,
    ) -> tuple[RepositoryWorkspace, ...]:
        revision = self._revision(self.legacy.current_revision(root))
        workspaces = []
        for path in self.legacy.workspace_paths(root):
            current_id = str(self.legacy.current_revision(path)).strip()
            current = self._revision(current_id or revision.id)
            branch = str(self.legacy.current_branch(path)).strip()
            workspaces.append(
                RepositoryWorkspace(
                    id=branch or path.name or str(path),
                    path=path,
                    base_revision=current,
                    current_revision=current,
                )
            )
        return tuple(workspaces)

    def create_repository_workspace(
        self,
        request: WorkspaceRequest,
        root: Path | None = None,
    ) -> RepositoryWorkspace:
        workspace_id = request.workspace_id or f"workspace-{request.path.name}"
        exists = bool(self.legacy.branch_exists(workspace_id, root))
        self.legacy.create_workspace(
            request.path,
            workspace_id,
            root,
            start_point="" if exists else request.base_revision.id,
            create_branch=not exists,
        )
        current_id = str(self.legacy.current_revision(request.path)).strip()
        current = self._revision(current_id or request.base_revision.id)
        return RepositoryWorkspace(
            id=workspace_id,
            path=request.path,
            base_revision=request.base_revision,
            current_revision=current,
        )

    def remove_repository_workspace(
        self,
        workspace: RepositoryWorkspace,
        root: Path | None = None,
        *,
        force: bool = False,
    ) -> None:
        branch = workspace.id if self.repository_features.named_branches else ""
        if force:
            self.legacy.remove_workspace(workspace.path, root, force=True)
        else:
            self.legacy.remove_workspace(workspace.path, root)
        if (
            force
            and branch
            and bool(self.legacy.branch_exists(branch, root))
        ):
            self.legacy.delete_branch(branch, root, force=True)

    @staticmethod
    def _revision(value: object, *, display: str = "") -> RepositoryRevision:
        revision = str(value).strip()
        if not revision:
            raise RepositoryProviderError("Legacy VCS returned an empty revision.")
        return RepositoryRevision(id=revision, display=display)


class LegacyForgeReviewAdapter:
    """Expose pull-request operations through opaque review identities."""

    provider_kind = "forge"
    capabilities = ProviderCapabilities(
        provider_kind="forge",
        capabilities=frozenset(
            {
                "forge.review",
                "forge.inspect",
                "forge.close",
                "forge.land",
            }
        ),
    )
    review_features = ReviewFeatures(
        stacked_changes=False,
        signals=True,
        landing=True,
        mutable_versions=True,
    )

    def __init__(
        self,
        provider: Any,
        *,
        publish_revision: Callable[[Path | None], Any] | None = None,
    ) -> None:
        self.legacy = provider
        self.name = str(getattr(provider, "name", "legacy-forge")).strip() or "legacy-forge"
        self.capabilities = _combined_capabilities(
            self.capabilities,
            getattr(provider, "capabilities", None),
        )
        self.supports_remote_review = bool(
            getattr(provider, "supports_remote_review", True)
        )
        self._publish_revision = publish_revision

    def publish_review(
        self,
        request: ReviewSubmission,
        root: Path | None = None,
    ) -> ReviewRecord:
        if request.dependencies:
            require_review_features(self, "stacked-changes")
        publish_revision = getattr(
            self.legacy,
            "publish_revision_for_review",
            None,
        )
        if not callable(publish_revision):
            publish_revision = getattr(self.legacy, "push_current_branch", None)
        if callable(publish_revision):
            publish_revision(root=root)
        elif self._publish_revision is not None:
            self._publish_revision(root)
        options = {
            "title": request.title,
            "body": request.body,
            "root": root,
            "draft": request.draft,
        }
        base_name = str(request.metadata.get("base_name") or "").strip()
        if base_name:
            options["base_branch"] = base_name
        provenance = request.metadata.get("evolution_provenance")
        if provenance is not None:
            options["evolution_provenance"] = provenance
        result = self.legacy.create_pull_request(**options)
        url = str(getattr(result, "url", "") or getattr(result, "fallback_url", "") or "").strip()
        review_id = url or str(request.metadata.get("legacy_reference") or "").strip()
        if not review_id:
            review_id = f"legacy-review:{request.revision.id}"
        identity = ReviewIdentity(
            id=review_id,
            url=url,
            metadata={
                "legacy_reference": review_id,
                "revision_id": request.revision.id,
            },
        )
        return ReviewRecord(
            identity=identity,
            title=str(getattr(result, "title", request.title)),
            body=str(getattr(result, "body", request.body)),
            state="open" if bool(getattr(result, "created", False)) else "unpublished",
            versions=(ReviewVersion(id=request.revision.id, revision=request.revision),),
            dependencies=request.dependencies,
            draft=bool(getattr(result, "draft", request.draft)),
        )

    def inspect_review(
        self,
        review: ReviewIdentity,
        root: Path | None = None,
    ) -> ReviewRecord:
        inspect_landing = getattr(self.legacy, "inspect_pull_request_merge", None)
        result = (
            inspect_landing(review.id, root)
            if callable(inspect_landing)
            else self.legacy.inspect_pull_request(review.id, root)
        )
        return self._record(result, fallback=review)

    def list_open_reviews(
        self,
        root: Path | None = None,
        *,
        limit: int = 20,
    ) -> tuple[ReviewRecord, ...]:
        return tuple(
            self._record(result)
            for result in self.legacy.list_open_pull_requests(root, limit=limit)
        )

    def close_review(
        self,
        request: ReviewCloseRequest,
        root: Path | None = None,
    ) -> ReviewRecord:
        reference = self._legacy_number(request.review)
        result = self.legacy.close_pull_request(
            reference,
            root=root,
            comment=request.note or None,
        )
        revision = self._identity_revision(request.review)
        identity = ReviewIdentity(
            id=request.review.id,
            url=str(getattr(result, "url", "") or request.review.url),
            metadata=request.review.metadata,
        )
        return ReviewRecord(
            identity=identity,
            title="",
            body="",
            state="closed" if bool(getattr(result, "closed", True)) else "open",
            versions=(ReviewVersion(id=revision.id, revision=revision),),
        )

    def land_review(
        self,
        request: ReviewLandRequest,
        root: Path | None = None,
    ) -> ReviewLandResult:
        behind = self._land_behind_review(request, root)
        if behind is not None:
            return behind
        result = self.legacy.merge_pull_request(request.review.id, root)
        inspected = self.inspect_review(request.review, root)
        revision_id = str(getattr(result, "merge_commit", "") or "").strip()
        revision = inspected.landed_revision
        if revision is None and revision_id:
            revision = RepositoryRevision(revision_id)
        landed = inspected.state == "landed"
        return ReviewLandResult(
            review=inspected.identity,
            status="landed" if landed else "requested",
            revision=revision,
            landed_at=inspected.landed_at if landed else "",
            message=str(getattr(result, "message", "") or ""),
        )

    def _land_behind_review(
        self,
        request: ReviewLandRequest,
        root: Path | None,
    ) -> ReviewLandResult | None:
        """Update a mergeable review that is only behind its base.

        Called only for the exact review a human asked to land. Returns
        ``None`` for every other state so the forge's normal merge checks and
        refusals (conflicts, blocked, draft, closed) still apply. The forge
        never merges here: at most it enables its own auto-merge, which lands
        only after the required checks pass.
        """

        inspect_landing = getattr(self.legacy, "inspect_pull_request_merge", None)
        if not callable(inspect_landing):
            return None
        status = inspect_landing(request.review.id, root)
        if not _is_cleanly_behind(status):
            return None
        number = int(getattr(status, "number", 0) or 0)
        label = f"PR #{number}" if number else f"Review {request.review.id}"
        base = str(getattr(status, "base_branch", "") or "").strip() or "its base branch"
        head = str(getattr(status, "head_sha", "") or "").strip()
        update = self._optional_forge_operation(
            "update_review_branch",
            "forge.update-branch",
        )
        if update is None:
            raise ReviewProviderError(
                f"{label} is behind {base} and must be updated before it can merge. "
                "This forge cannot update review branches automatically."
            )
        updated = update(request.review.id, expected_head_sha=head, root=root)
        new_head = str(getattr(updated, "head_sha", "") or "").strip()
        identity = ReviewIdentity(
            id=request.review.id,
            url=str(getattr(status, "url", "") or request.review.url),
            metadata=request.review.metadata,
        )
        revision = RepositoryRevision(new_head) if new_head else None
        enable = self._optional_forge_operation("enable_auto_merge", "forge.auto-merge")
        auto_merge = None
        auto_merge_error = ""
        if enable is not None and new_head:
            try:
                auto_merge = enable(request.review.id, expected_head_sha=new_head, root=root)
            except ForgeProviderError as error:
                # The branch is already updated; report that instead of hiding it.
                auto_merge_error = str(error).strip() or "the forge reported an error"
        if auto_merge is not None and bool(getattr(auto_merge, "enabled", False)):
            return ReviewLandResult(
                review=identity,
                status="auto-merge-enabled",
                revision=revision,
                message=(
                    f"{label} was behind {base}. I updated its branch and enabled "
                    "auto-merge; it will land when CI passes. If CI fails it will not merge."
                ),
            )
        if enable is None:
            reason = "This forge cannot enable auto-merge."
        elif not new_head:
            reason = "The updated head could not be verified yet, so auto-merge was not enabled."
        elif auto_merge_error:
            reason = f"Auto-merge could not be enabled: {auto_merge_error.rstrip('.')}."
        else:
            reason = (
                str(getattr(auto_merge, "message", "") or "").strip()
                or "Auto-merge was not enabled."
            )
        return ReviewLandResult(
            review=identity,
            status="branch-updated",
            revision=revision,
            message=(
                f"{label} was behind {base}. I updated its branch and CI is running. "
                f"{reason} It has not been merged; ask me to merge it again once CI is green."
            ),
        )

    def _optional_forge_operation(
        self,
        method: str,
        capability: str,
    ) -> Callable[..., Any] | None:
        operation = getattr(self.legacy, method, None)
        if not callable(operation):
            return None
        declared = getattr(self.legacy, "capabilities", None)
        if callable(declared):
            declared = declared()
        if not isinstance(declared, ProviderCapabilities) or not declared.supports(capability):
            return None
        return operation

    def _record(
        self,
        result: Any,
        *,
        fallback: ReviewIdentity | None = None,
    ) -> ReviewRecord:
        target = getattr(result, "target", None)
        number = int(getattr(result, "number", 0) or getattr(target, "number", 0) or 0)
        url = str(getattr(result, "url", "") or (fallback.url if fallback else "")).strip()
        review_id = url or (str(number) if number else (fallback.id if fallback else ""))
        if not review_id:
            raise ReviewProviderError("Legacy forge returned no review identity.")
        revision_id = str(
            getattr(result, "head_oid", "")
            or getattr(result, "head_sha", "")
            or (fallback.metadata.get("revision_id", "") if fallback else "")
            or f"review:{review_id}"
        ).strip()
        identity = ReviewIdentity(
            id=review_id,
            url=url,
            metadata={
                "number": number,
                "legacy_reference": review_id,
                "revision_id": revision_id,
            },
        )
        signals = tuple(
            ReviewSignal(name=name, status=value)
            for name, value in (
                ("mergeable", str(getattr(result, "mergeable", "") or "").lower()),
                (
                    "merge-state",
                    str(getattr(result, "merge_state_status", "") or "").lower(),
                ),
            )
            if value
        )
        raw_state = str(getattr(result, "state", "") or "open")
        state = "landed" if raw_state.strip().lower() == "merged" else raw_state
        landed_revision_id = str(getattr(result, "merge_commit", "") or "").strip()
        return ReviewRecord(
            identity=identity,
            title=str(getattr(result, "title", "") or ""),
            body=str(getattr(result, "body", "") or ""),
            state=state,
            versions=(
                ReviewVersion(
                    id=revision_id,
                    revision=RepositoryRevision(revision_id),
                    created_at=str(getattr(result, "updated_at", "") or ""),
                ),
            ),
            signals=signals,
            draft=bool(getattr(result, "is_draft", False)),
            landed_revision=(
                RepositoryRevision(landed_revision_id)
                if landed_revision_id
                else None
            ),
            landed_at=str(getattr(result, "merged_at", "") or ""),
        )

    @staticmethod
    def _legacy_number(review: ReviewIdentity) -> int:
        raw = review.metadata.get("number")
        if raw is None:
            raw = review.id.rstrip("/").rsplit("/", 1)[-1]
        try:
            number = int(raw)
        except (TypeError, ValueError) as error:
            raise ReviewProviderError(
                f"Legacy forge requires a numeric review reference, got {review.id!r}."
            ) from error
        if number <= 0:
            raise ReviewProviderError("Legacy review number must be positive.")
        return number

    @staticmethod
    def _identity_revision(review: ReviewIdentity) -> RepositoryRevision:
        revision_id = str(review.metadata.get("revision_id") or f"review:{review.id}").strip()
        return RepositoryRevision(revision_id)


def as_repository_provider(provider: Any) -> RepositoryProvider:
    if isinstance(provider, RepositoryProvider):
        return provider
    return LegacyVersionControlRepositoryAdapter(provider)


def as_review_provider(
    provider: Any,
    *,
    publish_revision: Callable[[Path | None], Any] | None = None,
) -> ReviewProvider:
    if isinstance(provider, ReviewProvider):
        return provider
    return LegacyForgeReviewAdapter(
        provider,
        publish_revision=publish_revision,
    )


def _is_cleanly_behind(status: Any) -> bool:
    """True only for an open, ready, conflict-free review that is merely behind."""

    def text(name: str) -> str:
        return str(getattr(status, name, "") or "").strip().upper()

    return (
        text("merge_state_status") == "BEHIND"
        and text("state") == "OPEN"
        and not str(getattr(status, "merged_at", "") or "").strip()
        and not bool(getattr(status, "is_draft", False))
        and text("mergeable") == "MERGEABLE"
        and bool(text("head_sha"))
    )


def _combined_capabilities(
    semantic: ProviderCapabilities,
    legacy: object,
) -> ProviderCapabilities:
    if callable(legacy):
        legacy = legacy()
    if isinstance(legacy, ProviderCapabilities):
        if legacy.provider_kind != semantic.provider_kind:
            raise ValueError(
                f"Cannot adapt {legacy.provider_kind} capabilities as "
                f"{semantic.provider_kind}."
            )
        declared = legacy.capabilities
    elif legacy is None:
        declared = frozenset()
    else:
        declared = frozenset(str(item) for item in legacy)
    return ProviderCapabilities(
        provider_kind=semantic.provider_kind,
        capabilities=frozenset((*semantic.capabilities, *declared)),
    )
