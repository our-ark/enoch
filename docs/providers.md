# Enoch providers

Enoch separates replaceable infrastructure from agent behavior through four
provider capabilities:

| Kind | Built-in | Responsibility |
| --- | --- | --- |
| `chat` | `telegram` | Receive normalized chat events and deliver messages |
| `runtime` | `codex` | Answer, edit, resume sessions, report models, and cancel work |
| `vcs` | `git` | Run local version-control operations |
| `forge` | `github` | Create, inspect, list, close, and merge pull requests |

The active providers are configured in the private instance file
`.enoch/config.yaml`:

```yaml
providers:
  chat: telegram
  runtime: codex
  vcs: git
  forge: github
```

The same settings can be inspected and changed with:

```text
/config providers
/config provider runtime claude
/config provider chat slack
/config provider runtime default
```

Before a chat provider is running, use the admin CLI equivalents:

```text
bin/enoch config providers
bin/enoch config provider chat slack
```

Restart Enoch after changing a provider. Environment variables such as
`ENOCH_RUNTIME_PROVIDER` and `ENOCH_CHAT_PROVIDER` override the file.

## Third-party packages

A provider package registers factories with Python package entry points:

```toml
[project.entry-points."enoch.providers"]
"chat.slack" = "enoch_slack:create_provider"
"runtime.claude" = "enoch_claude:create_provider"
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
                cursor=1,
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
Providers translate native events into `ChatEvent`; core command and task code
does not parse provider-specific payloads.

Runtime providers expose `health()` so doctor checks the selected runtime
instead of assuming a Codex binary. They should raise
`AgentRuntimeAccessUnavailable` for recoverable authentication or quota
failures and `AgentRuntimeCancelled` for human cancellation. Forge and VCS
providers should raise their matching provider errors. This preserves Enoch's
pause, resume, failure, and audit behavior across implementations.

## Compatibility

The built-in adapters preserve the existing `telegram:` and `codex:` settings.
`bin/enoch-telegram` remains available, while `bin/enoch-agent` starts whichever
chat provider is selected. Provider packages do not need to modify Enoch core or
fork the Telegram bot.

Lineage discovery and cross-agent skill lookup currently use GitHub-specific
source conventions. The replaceable forge boundary covers task publication,
pull-request management, retry reconciliation, and evolution promotion.
