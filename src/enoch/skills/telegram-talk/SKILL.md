# Telegram Talk

## Purpose

Use this skill when Enoch should talk with the human through Telegram.

The reusable `our-ark-telegram` library owns Telegram transport. This skill is a
thin Enoch integration manifest; channel-neutral commands, tasks, evolution,
sessions, and authorization live in `enoch.application` and are shared by every
chat provider.

For JPEG, PNG, and WebP image messages, use the inherited `telegram-vision`
skill and keep the image turn read-only.

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
codex:
  model: "gpt-5.6-terra"
  reasoning_effort: "medium"
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
- `ENOCH_CODEX_BIN`
- `ENOCH_PROGRESS_INTERVAL`

Optional:

- `telegram.allowed_chat_id`
- `telegram.poll_timeout`
- `codex.model`
- `codex.reasoning_effort`
- `codex.executable` (set with `/config runtime codex executable <path|auto>`)
- `task.timeout_seconds` (defaults to 600 seconds and can be set with `/config task-timeout <duration>`)

`/config model` reads the visible model catalog bundled with the installed
Codex CLI, marks the effective model, and shows only current GPT-5.6 model ids.
It includes a complete setting example such as
`/config model gpt-5.6-sol`. Catalog-external ids remain valid for private or
future rollouts.

## Safety

- Prefer setting `telegram.allowed_chat_id` after the first `/status` message reveals the chat id.
- Do not send secrets through Telegram.
- Use `telegram.allowed_chat_id` before enabling code-changing natural agency.
- Keep remote writes behind explicit human approval.
- Keep a finite task timeout so a stuck agent run cannot consume tokens indefinitely.
- Pause work and show `/resume` when Codex authentication, quota, or rate limits
  are unavailable; do not classify that recoverable condition as task failure.
- Record discovered task regressions and their revert or forward-fix resolution
  through the internal regression signal; never ask the human to maintain those
  statuses with commands.
