# Repository and review providers

Enoch defines provider-neutral repository and review contracts alongside the
original Git- and pull-request-shaped protocols. The semantic contracts are a
versioned public surface in `our_ark_provider_kit`; they do not require a
staging index, named branches, pull-request numbers, or a relationship between
a review identity and a branch identity.

`REPOSITORY_CONTRACT_VERSION` and `REVIEW_CONTRACT_VERSION` are currently `1`.
The original `VersionControlProvider` and `ForgeProvider` remain supported
during migration.

Ordinary queued tasks now use the semantic contracts end to end:

```text
authoritative revision
  -> isolated repository workspace
  -> runtime edit
  -> validation
  -> captured revision
  -> published review
  -> workspace cleanup
```

The task workflow does not stage files, create or inspect a named branch, push
a branch, parse a pull-request result, or require a review id to match a
revision or workspace id. Git and GitHub preserve those implementation details
inside their compatibility adapters.

Task persistence uses the same semantic model. Queue and task-event records
store opaque `workspace_id`, `revision_id`, `review_id`, and review URLs rather
than branch, commit, or pull-request fields. Existing schema 11 queue data and
task events through schema 6 remain readable, while new writes use queue schema
12 and task-event schema 7.

## Repository contract

`RepositoryProvider` operates on opaque `RepositoryRevision` values and typed
working-copy, change-capture, authoritative-base, and workspace requests:

```python
from our_ark_provider_kit import (
    ChangeCaptureRequest,
    RepositoryProvider,
    WorkspaceRequest,
)


state = repository.inspect_working_copy(root)
base = repository.authoritative_base(root, refresh=True)
captured = repository.capture_change(
    ChangeCaptureRequest(
        message="Add bounded behavior",
        paths=("src/agent/behavior.py",),
    ),
    root,
)
workspace = repository.create_repository_workspace(
    WorkspaceRequest(
        path=root.parent / "task-workspace",
        base_revision=base.revision,
    ),
    root,
)
```

Providers declare `RepositoryFeatures`. `staging_index` and `named_branches`
are optional implementation details; `BranchlessRepositoryFixture` implements
the complete portable contract with both disabled.

Portable repository capabilities use the existing `vcs` provider namespace:

- `vcs.inspect`
- `vcs.resolve`
- `vcs.ancestry`
- `vcs.authoritative`
- `vcs.capture`
- `vcs.workspace`
- `vcs.restore`

Call `require_repository_features` before an operation that truly needs an
optional feature. Unsupported requirements raise `UnsupportedProviderFeature`
before a provider side effect.

## Review contract

`ReviewProvider` uses opaque `ReviewIdentity` values. A review carries one or
more typed versions, dependencies on other reviews, signals, and an independent
lifecycle:

```python
from our_ark_provider_kit import (
    ReviewLandRequest,
    ReviewSubmission,
)


first = reviews.publish_review(
    ReviewSubmission(
        title="Foundation change",
        body="Validated evidence.",
        revision=foundation_revision,
    ),
    root,
)
second = reviews.publish_review(
    ReviewSubmission(
        title="Dependent change",
        body="Validated evidence.",
        revision=dependent_revision,
        dependencies=(first.identity,),
    ),
    root,
)
landed = reviews.land_review(ReviewLandRequest(second.identity), root)
```

`IndependentReviewFixture` generates identities such as `review-1` regardless
of revision or workspace identity. It supports multiple versions and a
two-change stack without inventing a pull-request number.

Portable review capabilities retain the `forge` namespace during provider
registry migration:

- `forge.review`
- `forge.inspect`
- `forge.close`
- `forge.land`
- `forge.stack`

## Legacy adapters

`as_repository_provider` returns an existing `RepositoryProvider` unchanged and
otherwise wraps a `VersionControlProvider` in
`LegacyVersionControlRepositoryAdapter`. Staging and branches remain inside the
adapter. The public result is a canonical revision, even when the legacy commit
method returns an abbreviated Git hash.

`as_review_provider` similarly wraps `ForgeProvider` in
`LegacyForgeReviewAdapter`. Pull-request numbers, branch discovery, and legacy
result shapes remain private to the adapter; callers use typed review requests
and opaque identities. For a legacy remote forge, the adapter publishes the
captured revision before creating its pull request.

These adapters provide coexistence, not reverse emulation. A branchless
repository is not forced to implement the old branch API, and a review service
without pull requests is not forced to manufacture PR fields. Core task,
publication, existing-reference handoff, retry reconciliation, `/pr`, update,
and evolution promotion/adoption flows now use the semantic contracts. The
legacy protocols remain adapter inputs for existing Git and GitHub provider
packages, not application workflow requirements.

Review records carry verified landing evidence as `landed_revision` and
`landed_at`. A legacy forge adapter obtains that evidence by inspecting the
landed review after requesting the merge. Evolution promotion therefore checks
an opaque landed revision against the repository's refreshed authoritative
revision without parsing Git or GitHub result fields.

The provider registry accepts either `RepositoryProvider` or
`VersionControlProvider` for `vcs`, and either `ReviewProvider` or
`ForgeProvider` for `forge`. This lets a branchless repository and an
independent review service load from normal provider entry points and instance
configuration without implementing compatibility methods they do not support.

Queued tasks preflight `isolated_workspaces` and `immutable_revisions`, then
authorize `vcs.inspect`, `vcs.authoritative`, `vcs.workspace`, `vcs.capture`,
and `forge.review` before creating a workspace or invoking the runtime.

## Conformance

`RepositoryProviderConformanceMixin` checks opaque revision resolution, change
capture without a staging assumption, and isolated workspace lifecycle.
`ReviewProviderConformanceMixin` checks identity independence, stacked reviews,
version updates, close, and verified landing behavior.

Both reference fixtures run these suites in provider-kit CI. Git's repository
adapter and GitHub's review adapter also have integration tests, preserving
existing behavior behind the adapters. Enoch's application suite also loads
both fixtures through the provider registry and executes task publication,
existing-reference handoff, review maintenance, update, and evolution
lifecycle checks without a Git repository, staging index, named branch, or
pull-request identity.
