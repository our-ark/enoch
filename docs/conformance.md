# Extension conformance

Enoch publishes versioned `unittest` mixins for provider, runtime, workflow,
and profile implementations. The suites are executable specifications of the
public extension contracts; downstream packages should run the corresponding
suite in their own CI.

`CONFORMANCE_API_VERSION` is currently `1`. Provider-only packages can import
the lightweight suites from `our_ark_provider_kit.conformance`. Packages that
extend the complete agent can import the same provider suites plus workflow and
profile suites from `enoch.conformance`.

## Provider contract

Combine `ProviderContractConformanceMixin` with `unittest.TestCase`, declare the
provider kind and runtime-checkable protocol, and return an isolated provider:

```python
from pathlib import Path
import unittest

from our_ark_provider_kit import ChatProvider
from our_ark_provider_kit.conformance import ProviderContractConformanceMixin


class MyChatConformance(ProviderContractConformanceMixin, unittest.TestCase):
    provider_kind = "chat"
    provider_protocol = ChatProvider

    def create_provider(self, root: Path):
        return MyChatProvider(state_root=root)
```

The suite verifies the public protocol, stable provider identity, declared
kind, and capability namespace. Enoch's Telegram, GitHub, launchd, and systemd
reference packages run this suite.

## Runtime behavior

Runtime packages should use `AgentRuntimeConformanceMixin` and implement
`create_runtime`. In addition to the structural provider checks, this suite
requires:

- a result accepted by the versioned typed-result normalizer;
- cancellation before execution to raise `AgentRuntimeCancelled`;
- an expired deadline before execution to raise `AgentRuntimeTimedOut`.

The fixture must be hermetic. It should use a fake transport or executable and
must not spend tokens or contact an external service.

## Workflow reliability

Workflow implementations should use `WorkflowEngineConformanceMixin` and
implement `create_workflow(root, epoch=...)`. The suite exercises the complete
enqueue, start, claim, heartbeat, and finalize lifecycle plus:

- duplicate request suppression through an idempotency key;
- cancellation of pending work;
- restart recovery without creating a second task;
- rejection of stale daemon fencing tokens;
- containment of a partial failure so later queued work remains runnable.

An engine using a different fencing-token representation may override
`begin_fencing_epoch`, while still rejecting stale mutations with
`StaleDaemonEpoch`.

## Durable notifications

Durable chat providers should use `DurableNotificationConformanceMixin` and
provide a hermetic provider fixture, one retryable-failure injection hook, and
an attempt counter. The suite verifies that a completed idempotency key is not
delivered twice, a retryable partial failure is recovered after daemon restart,
and a stale daemon cannot invoke the provider.

## Profile integration

Profiles should use `ProfileConformanceMixin`, return their `AgentProfile` from
`create_profile`, and optionally return a `ProfileCommandCase`. The suite
checks API-version compatibility, command discovery, governed task submission,
prompt contribution, and lifecycle hooks in an isolated storage layout.

```python
class ResearcherProfileConformance(
    ProfileConformanceMixin,
    unittest.TestCase,
):
    def create_profile(self):
        return create_profile()

    def command_case(self):
        return ProfileCommandCase(
            command="research",
            argument="fencing tokens",
            expected_request="Research fencing tokens",
            expected_capabilities=("runtime.execute",),
        )
```

The core test suite applies all behavioral suites to Enoch's built-in runtime,
workflow engine, and a representative profile. The offline wheel E2E also
imports the conformance API from the installed artifact, preventing accidental
source-checkout-only publication.

Repository and review implementations use
`RepositoryProviderConformanceMixin` and `ReviewProviderConformanceMixin`.
The included `BranchlessRepositoryFixture` and `IndependentReviewFixture`
prove that the portable contracts do not require staging, branches, or
pull-request identities. See
[`repository-review-providers.md`](repository-review-providers.md).
