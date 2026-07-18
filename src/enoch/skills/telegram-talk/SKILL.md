# Telegram Talk

## Purpose

Use this skill when Enoch should talk with the human through Telegram.

## Boundary

Plain Telegram messages use Enoch's read-only brain path unless Enoch's model classifies the intent as a repository edit, approved direct action, memory request, ancestor adoption, or GitHub publishing action.

For natural repository requests, Enoch may:

- read the current long-term memory and startup context;
- keep work in the persistent Codex session for the locked Telegram chat;
- refuse code-changing or GitHub work unless Telegram is locked to one chat;
- implement the requested change locally when authorized;
- run relevant tests or doctor when practical;
- coordinate commit, push, PR creation, approval, or merge only when the human asks for that action.

For ancestor commands, Enoch may:

- read `.agent/lineage.yaml` for the direct parent;
- recursively resolve ancestor lineage through GitHub;
- scan parent or ancestor PR history for candidate changes;
- inspect, ignore, or ask Enoch to adapt a selected candidate through the persistent Codex session.

Do not treat ordinary chat messages as approval to mutate code unless they clearly request repository work.

## Configuration

Prefer Enoch's local runtime config:

```yaml
telegram:
  bot_token: "..."
  allowed_chat_id: 123456789
  poll_timeout: 30
task:
  timeout_seconds: 600
```

`.enoch/config.yaml` is local and gitignored.

Environment variables can override local config:

- `ENOCH_TELEGRAM_BOT_TOKEN`
- `ENOCH_TELEGRAM_ALLOWED_CHAT_ID`
- `ENOCH_TELEGRAM_POLL_TIMEOUT`
- `ENOCH_CODEX_MODEL`
- `ENOCH_CODEX_REASONING_EFFORT`
- `ENOCH_PROGRESS_INTERVAL`

Optional:

- `telegram.allowed_chat_id`
- `telegram.poll_timeout`
- `codex.reasoning_effort`
- `task.timeout_seconds` (defaults to 600 seconds and can be set with `/config task-timeout <duration>`)

## Safety

- Prefer setting `telegram.allowed_chat_id` after the first `/status` message reveals the chat id.
- Do not send secrets through Telegram.
- Use `telegram.allowed_chat_id` before enabling code-changing natural agency.
- Keep remote writes behind explicit human approval.
- Keep a finite task timeout so a stuck agent run cannot consume tokens indefinitely.
