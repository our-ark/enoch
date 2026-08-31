# Application composition

`ApplicationComposition` is Enoch's versioned startup boundary for descendant
agents. It lets a descendant own identity and domain selection while Enoch
continues to own polling, authorization, recovery, task execution,
publication, delivery, and shutdown.

The current contract is `APPLICATION_COMPOSITION_API_VERSION = 1`.

## Descendant launcher

```python
from enoch.application import (
    ApplicationComposition,
    ApplicationPresentation,
    run_application,
)
from enoch.identity import load_body_identity


NOAH = ApplicationComposition(
    name="noah",
    identity_loader=load_body_identity,
    identity_path_resolver=lambda root: root / "src/noah/body.yaml",
    presentation=ApplicationPresentation(
        display_name="Noah",
        ready_message="Noah is ready to coordinate.",
    ),
    required_extensions=("noah-manager",),
)


def main():
    run_application(NOAH)
```

`identity_path_resolver` identifies the descendant's canonical mutable body
file in the instance body. The resolved path must remain under the explicit
instance root. Enoch passes that path to `identity_loader` on every startup, and
`/mission` reads and writes the same path instead of assuming
`src/enoch/body.yaml`. This preserves body mission changes across restarts.

Personal identity is separate private instance state in `self.json`. Enoch
reloads it into startup context for every fresh session, while `body.yaml`
remains the versioned executable-body contract. Legacy descendant
`identity.yaml` files remain readable during migration.

`ApplicationPresentation` intentionally contains only bounded, single-line
application strings. Domain command wording belongs to profiles or extensions,
not to the startup composition.

## Selection and precedence

A composition may declare:

- `profile_name`, overriding profile selection from private configuration;
- `required_extensions`, always loaded before configured optional extensions;
- `include_configured_extensions=False`, for a closed extension set;
- explicit `chat`, `runtime`, `vcs`, and `forge` provider names through
  `ApplicationProviderSelection`;
- a deny-only `authorization_policy`;
- a `workflow_factory(root, daemon_epoch)` for an alternate versioned workflow.

An explicit `chat_provider_name` passed to `run_application` overrides the
composition's chat selection. Empty provider and profile names continue to use
the existing environment and private configuration rules.

Required and configured extensions are de-duplicated by extension identity.
Required extensions retain declaration order, followed by any additional
configured extensions. Unknown, duplicate, or API-incompatible components
fail before the application begins polling.

## Lifecycle ownership

`ApplicationComposition.resolve(root)` returns immutable
`ApplicationComponents` containing the selected identity, presentation,
providers, profile, extensions, daemon epoch, workflow, and optional
authorization policy.

The workflow factory receives the current daemon epoch. Alternate workflows
must apply that epoch to every mutation, including when their state lives
outside the application root. `run_application()` then hands the resolved
components to Enoch's normal runner. A descendant does not subclass
`EnochApplication` and does not receive polling, worker, recovery,
finalization, publication, or shutdown ownership.

Direct `EnochApplication(...)` construction remains supported for focused
embedding and tests. Enoch's own executable uses the default
`ApplicationComposition`, so the descendant path and the built-in path share
one startup implementation.

## Conformance and packaging

Descendant packages can inherit
`ApplicationCompositionConformanceMixin` and return their composition from
`create_composition()`. The suite validates the public version, identity
loader, mutable identity boundary, and resolved presentation.

Enoch's offline wheel test installs independent provider, profile, and
extension packages, resolves them through `ApplicationComposition`, injects a
fenced workflow with isolated state, and completes real profile and extension
tasks. This prevents the composition API from depending on the source checkout.
