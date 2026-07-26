# Enoch providers

Enoch separates replaceable infrastructure from agent behavior through five
provider kinds. Only `chat` and `vcs` must be supplied when moving the
core agent into a new environment:

| Kind | Reference provider | Responsibility |
| --- | --- | --- |
| `chat` | `telegram` | Receive normalized chat events and deliver messages |
| `runtime` | `codex` | Answer, edit, resume sessions, report models, and cancel work |
| `vcs` | `git` | Manage repository revisions, working copies, and isolated workspaces |
| `forge` | local fallback; `github` reference | Publish and govern review units; retain local changes when remote review is unavailable |
| `service` | `launchd` / `systemd` | Install, control, inspect, and restart the agent process |

The active providers are configured in the private instance file
`.enoch/config.yaml`:

```yaml
providers:
  chat: telegram
  runtime: codex
  vcs: git
  forge: github
  service: launchd
```

The minimal portable configuration is:

```yaml
providers:
  chat: my-chat
  vcs: my-vcs
```

The built-in Codex runtime and local forge fill the other execution-critical
roles. The local forge runs validation and commits completed changes, but does
not push or open a review. It deliberately preserves the local task branch.
`service` is optional for foreground execution.

The same settings can be inspected and changed with:

```text
/config providers
/config provider runtime claude
/config provider chat slack
/config provider service systemd
/config provider runtime default
```

Before a chat provider is running, use the admin CLI equivalents:

```text
bin/enoch config providers
bin/enoch config provider chat slack
```

Restart Enoch after changing a provider. Environment variables such as
`ENOCH_RUNTIME_PROVIDER`, `ENOCH_CHAT_PROVIDER`, and
`ENOCH_SERVICE_PROVIDER` override the file.

Provider-specific settings live in the provider's existing config section.
For example, the built-in Codex runtime keeps its model, reasoning, and
executable settings together:

```yaml
codex:
  model: gpt-5.6-sol
  reasoning_effort: high
  executable: /Applications/ChatGPT.app/Contents/Resources/codex
```

Inspect, set, or reset the Codex executable with:

```text
/config runtime codex executable
/config runtime codex executable /Applications/ChatGPT.app/Contents/Resources/codex
/config runtime codex executable auto
```

Executable resolution uses `ENOCH_CODEX_BIN`, then `codex.executable` from the
Enoch instance config, then `PATH`, then known macOS app locations. An explicit
but invalid environment or config value fails health checks instead of silently
falling through to another installation. The daemon reads this same instance
config; the executable path is not copied into a service manifest.

## Host services

The core daemon command is independent of the operating system's service
manager. The reference `launchd` provider supports macOS, while the reference
`systemd` provider installs a user service on Linux. Enoch selects the provider
supported by the current host unless `providers.service` explicitly chooses
one.

Both implementations expose the same lifecycle:

```text
bin/enoch-daemon install
bin/enoch-daemon start
bin/enoch-daemon stop
bin/enoch-daemon restart
bin/enoch-daemon status
bin/enoch-daemon logs
bin/enoch-daemon doctor
bin/enoch-daemon manifest
```

On Linux, these commands use `systemctl --user` and logs come from the user
journal. On macOS they use a LaunchAgent and file-backed logs under
`.enoch/artifacts/logs/daemon`. `/restart` and update adoption also delegate to the
selected service provider, so core code does not invoke either service manager.

## Third-party packages

A provider package registers factories with Python package entry points:

```toml
[project.entry-points."our_ark.providers"]
"chat.slack" = "enoch_slack:create_provider"
"runtime.claude" = "enoch_claude:create_provider"
"vcs.jj" = "enoch_jj:create_provider"
"service.container" = "enoch_container:create_provider"
```

The `vcs` entry point may return either the semantic `RepositoryProvider` or
the legacy `VersionControlProvider`. The `forge` entry point may similarly
return `ReviewProvider` or legacy `ForgeProvider`. New integrations should
prefer the semantic contracts; adapters keep Git- and pull-request-shaped
providers compatible during migration.

Factories may accept the Enoch repository root and return an implementation of
the corresponding protocol from `enoch.providers`:

```python
from pathlib import Path

from enoch.providers import ChatEvent


def create_provider(root: Path | None = None):
    return SlackProvider(root)


class SlackProvider:
    name = "slack"
    provider_kind = "chat"

    @property
    def allowed_conversation_id(self):
        return "C012345"

    def receive(self, cursor=None):
        return [
            ChatEvent(
                cursor="next-page-token",
                conversation_id="C012345",
                message_id="1712345.0001",
                text="hello",
            )
        ]

    def send_message(self, conversation_id, text):
        ...

    def edit_message(self, conversation_id, message_id, text):
        ...

    def send_read_ack(self, conversation_id, message_id):
        ...
```

Chat conversation and message identifiers are opaque integers or strings.
Polling cursors are also opaque integers or strings and are persisted separately
under `.enoch/channels/<provider>/`. Providers translate native events into
`ChatEvent`; core command and task code does not parse provider-specific
payloads.

Providers that support attachments implement the optional
`AttachmentProvider` contract. They expose native files as provider-neutral
`Attachment` values and materialize them only when Enoch asks:

```python
def download_attachment(self, attachment, destination, *, max_bytes):
    ...
```

### Capabilities and authorization

Providers may explicitly declare their supported operations with the versioned
`ProviderCapabilities` contract:

```python
from enoch.providers import ProviderCapabilities


class SlackProvider:
    name = "slack"
    provider_kind = "chat"
    capabilities = ProviderCapabilities(
        provider_kind="chat",
        capabilities=frozenset(
            {"chat.receive", "chat.send", "chat.edit", "chat.ack"}
        ),
    )
```

Core capability names are `chat.receive`, `chat.send`, `chat.edit`,
`chat.ack`, `chat.attachment`, `runtime.respond`, `runtime.execute`,
`vcs.read`, `vcs.write`, `forge.read`, `forge.publish`, `forge.maintain`,
`forge.merge`, `service.read`, and `service.manage`.

The versioned semantic repository and review contracts add granular
`vcs.inspect`, `vcs.resolve`, `vcs.ancestry`, `vcs.authoritative`,
`vcs.capture`, `vcs.workspace`, `vcs.restore`, `forge.review`,
`forge.inspect`, `forge.close`, `forge.land`, and `forge.stack` capabilities.
Their typed requests, optional feature discovery, compatibility adapters, and
branchless fixtures are documented in
[`repository-review-providers.md`](repository-review-providers.md).

Ordinary queued tasks require `runtime.execute`, `vcs.inspect`,
`vcs.authoritative`, `vcs.workspace`, `vcs.capture`, and `forge.review`.
Enoch verifies those grants and the required repository features before
creating the task workspace or invoking the runtime.

Providers that predate this contract receive the complete legacy capability set
for their provider kind, preserving installation compatibility. Once a
provider declares capabilities, the declaration is authoritative and missing
requirements fail closed before its method is called.

`EnochApplication` also accepts an `authorization_policy`. The base authorizer
first verifies provider grants, then asks the policy whether to tighten them.
An allow decision cannot restore a capability omitted by the provider.
Authorization therefore remains in the application shell rather than inside a
profile command or task prompt.

### Durable notifications

Core send and edit operations are written to a durable notification journal
before the provider is invoked. Every logical notification has a stable
idempotency key, and every daemon generation has a fencing epoch. An obsolete
daemon cannot claim, send, edit, reconcile, or finalize notification state.

All existing `ChatProvider` implementations remain compatible. A legacy
provider receives at-least-once retries for failures returned before a receipt
is recorded. If Enoch restarts with an ambiguous `in_flight` legacy delivery,
she fails closed and does not resend it, because the provider cannot prove
whether the external effect happened.

Providers that can offer stronger recovery implement the optional, versioned
`DurableNotificationProvider` contract:

```python
from enoch.providers import (
    NotificationCapabilities,
    NotificationReceipt,
)


class SlackProvider:
    notification_capabilities = NotificationCapabilities(
        idempotent_delivery=True,
        reconciliation=True,
    )

    def deliver_notification(self, intent):
        # Use intent.idempotency_key as the provider-native client request id.
        ...
        return NotificationReceipt(
            idempotency_key=intent.idempotency_key,
            status="delivered",
            message_id="1712345.0001",
            provider_reference="slack-request-42",
        )

    def reconcile_notification(self, intent):
        # Return delivered, not_found, or unknown without creating an effect.
        ...
```

`NotificationIntent` supports `send` and `edit`. `NotificationReceipt` reports
`delivered`, `not_found`, or `unknown`. A provider may advertise idempotent
delivery, reconciliation, both, or neither. After ambiguous success, Enoch
reconciles first, then replays only when the provider can prove absence or
guarantee idempotency. Transport-native request ids and lookup mechanisms stay
inside the provider.

The channel-neutral application lives in `src/enoch/app/`. Telegram's
Bot API transport, Enoch config adapter, setup handler, and integration skill
live in `libraries/telegram`. Core code receives only normalized `ChatEvent`
values and does not import that package.

Runtime providers expose `health()` so doctor checks the selected runtime
instead of assuming a Codex binary. They should raise
`AgentRuntimeAccessUnavailable` for recoverable authentication or quota
failures and `AgentRuntimeCancelled` for human cancellation. Forge and VCS
providers should raise their matching provider errors. This preserves Enoch's
pause, resume, failure, and audit behavior across implementations.

Remote forge providers may also expose `health(root) -> ProviderHealth`.
Doctor uses it when available to detect missing clients or expired
authentication before a task reaches the publish stage. Providers without the
optional hook remain compatible and are reported as loaded with authentication
status unavailable.

## Typed runtime results

Runtime contract version 1 uses `RuntimeResult` for both `respond()` and
`act_in_session()`:

```python
from enoch.providers import (
    RuntimeEvent,
    RuntimeOutputReference,
    RuntimeResult,
    RuntimeSideEffect,
    RuntimeUsage,
)


return RuntimeResult(
    final_text="Research complete.",
    session_id="session-42",
    completion_reason="completed",
    usage=RuntimeUsage(
        input_tokens=1200,
        cached_input_tokens=800,
        output_tokens=240,
        reasoning_tokens=60,
    ),
    events=(RuntimeEvent("turn.completed"),),
    output_refs=(
        RuntimeOutputReference("artifact", "artifact://reports/42"),
    ),
    side_effects=(
        RuntimeSideEffect("file", "reports/42.md"),
    ),
)
```

`final_text` is the user-visible result. `session_id` identifies resumable
provider state, while `completion_reason` describes why the invocation ended.
Usage is provider-neutral; provider-native structured events remain available
in `events`. Output references identify durable results, and side effects
describe externally observable actions.

For migration compatibility, Enoch accepts a plain `str` and normalizes it to
`RuntimeResult(final_text=value, completion_reason="completed")`. New runtime
providers should return `RuntimeResult`. Returning any other type is a provider
contract error and does not stop the chat daemon.

Task and evolve audit records persist a bounded runtime trace: provider,
session, completion reason, token usage, event types, output references, and
side-effect references. Full provider event payloads remain in the in-memory
result and are not copied wholesale into private task logs.

## Runtime execution semantics

Runtime execution contract version 1 uses `RuntimeExecutionControl` for both
`respond()` and `act_in_session()`. It gives every invocation the same logical
session, timeout, cancellation, and progress surface:

```python
from enoch.providers import RuntimeExecutionControl


control = RuntimeExecutionControl(
    request_id="task:42:attempt:1",
    session_key="chat:42:task:42",
    timeout_seconds=600,
    cancellation_event=cancel_event,
    timeout_event=timeout_event,
    progress_callback=lambda progress: print(
        progress.stage,
        progress.elapsed_seconds,
    ),
)
```

A non-empty `session_key` identifies persistent logical provider state. The
first invocation starts that state; later invocations with the same key resume
it when available. Providers return their native resumable identifier in
`RuntimeResult.session_id` without exposing it as Enoch's routing key.

Providers emit typed `RuntimeProgress` values. `elapsed_seconds`, `stage`,
`message`, `sandbox`, `session_id`, and provider-neutral metadata are available
without coupling Enoch to provider-native event payloads.

Timeout and human cancellation are distinct terminal conditions:

- Raise `AgentRuntimeTimedOut` when the execution deadline expires.
- Raise `AgentRuntimeCancelled` when a caller requests cancellation.
- Raise `AgentRuntimeAccessUnavailable` when authentication, quota, or rate
  limits require the task to pause.
- Raise `AgentRuntimeError` for other provider failures.
- Return `RuntimeResult` only after a successful invocation has reached its
  completion boundary.

Enoch checks the control before invocation and again before accepting the
result, so a result that arrives after cancellation or timeout is not treated
as successful. Runtime providers should also poll `control.raise_if_stopped()`
while waiting on long-running external work.

For migration compatibility, Enoch adapts providers that do not explicitly
declare the `execution` keyword. Legacy providers continue to receive
`session_key`, `cancellation_event`, and `(elapsed_seconds, sandbox)` progress
callbacks. New providers should explicitly declare `execution` and use the
typed contract.

Runtime-specific settings stay with the provider. A runtime may optionally
implement `configure(args, root, prefix="/")` for
`/config runtime <provider> ...` and `config_summary(root)` for its section in
`/config`. Model and reasoning overrides remain generic through
`config_section`, `model_summary()`, and `model_options()`. Core task and resume
messages refer to agent runtime access rather than a particular implementation.

VCS providers implement repository semantics rather than parsing Git command
arguments: current and switched branches, clean-state and diff inspection,
staging and commit, task base selection, and isolated workspace
creation/removal. Governed evolution additionally uses authoritative branch
discovery and refresh, revision resolution and ancestry, repository update and
rollback. Providers may also expose a sync summary for startup diagnostics.
`run(args, root)` remains a compatibility escape
hatch implemented by the built-in Git provider, not a required provider
contract. Enoch's update and evolution lifecycle do not depend on either
optional capability.

## Provider-owned setup

Provider descriptors may include a `setup` callable alongside their factory.
`bin/enoch setup` forwards provider-specific setup commands to that handler
without constructing a provider first, so credentials can be configured before
the provider is operational. The reference Telegram adapter preserves the
existing `telegram:` settings through this hook.

`bin/enoch-agent` starts whichever chat provider is selected. Provider packages
do not need to modify Enoch core or fork the application.

`load_provider()` validates the selected implementation against its provider
contract before returning it. Missing methods or properties produce an
immediate `ProviderError` naming the incomplete provider and missing members,
instead of failing later in a task.

Forge providers own task publication, pull-request management, evolution
promotion, lineage discovery, and published skill reads. A replacement forge
implements the PR contract plus `read_text` and the lineage methods used by
`LineageProvider`.
