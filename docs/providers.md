# Enoch providers

Enoch separates replaceable infrastructure from agent behavior through five
provider capabilities. Only `chat` and `vcs` must be supplied when moving the
core agent into a new environment:

| Kind | Reference provider | Responsibility |
| --- | --- | --- |
| `chat` | `telegram` | Receive normalized chat events and deliver messages |
| `runtime` | `codex` | Answer, edit, resume sessions, report models, and cancel work |
| `vcs` | `git` | Run local version-control operations |
| `forge` | local fallback; `github` reference | Retain local branches or create, inspect, list, close, and merge pull requests |
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
`.enoch/logs/daemon`. `/restart` and update adoption also delegate to the
selected service provider, so core code does not invoke either service manager.

## Third-party packages

A provider package registers factories with Python package entry points:

```toml
[project.entry-points."enoch.providers"]
"chat.slack" = "enoch_slack:create_provider"
"runtime.claude" = "enoch_claude:create_provider"
"vcs.jj" = "enoch_jj:create_provider"
"service.container" = "enoch_container:create_provider"
```

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

Forge providers own task publication, pull-request management, evolution
promotion, lineage discovery, and published skill reads. A replacement forge
implements the PR contract plus `read_text` and the lineage methods used by
`LineageProvider`.
