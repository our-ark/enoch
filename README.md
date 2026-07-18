# Enoch

Enoch is a Genesis-created agent descended from Seth.

## Mission

Seth's self-evolving descendant: walk with her code body, inspect her own work, and grow through safe autonomous improvements while preserving human sovereignty.

## Run

```bash
bin/enoch
```

## Telegram

Create a Telegram bot for Enoch:

1. Open `@BotFather` in Telegram.
2. Send `/newbot`.
3. Use `Enoch` as the bot name.
4. Choose a unique username, such as `genesis_enoch_bot`.
5. Copy the token from BotFather.

Configure and start the local Enoch instance:

```bash
cd /Users/garyzhao/projects/instances/enoch-gary

ENOCH_PYTHON=/Users/garyzhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 bin/enoch setup-token <token>
ENOCH_PYTHON=/Users/garyzhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 bin/enoch setup-chat <your-chat-id>
ENOCH_PYTHON=/Users/garyzhao/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 bin/enoch-daemon start
```

Then open the bot in Telegram and send `/status`.

## Codex configuration

Enoch has her own local runtime configuration in `.enoch/config.yaml`. For the
Codex model and reasoning effort, settings are resolved in this order:

1. `ENOCH_CODEX_MODEL` or `ENOCH_CODEX_REASONING_EFFORT`
2. `codex.model` or `codex.reasoning_effort` in `.enoch/config.yaml`
3. The user-level Codex configuration in `$CODEX_HOME/config.toml` (normally
   `~/.codex/config.toml`)
4. The Codex CLI default

Use `/config` in Telegram to inspect the effective settings and `/config model`
or `/config reasoning-effort` to change Enoch's local overrides.

## Testing

Run the unit and hermetic evolution E2E tests with:

```bash
python -m unittest discover -s tests
```

The E2E design and covered workflows are documented in
[`docs/testing.md`](docs/testing.md).

## Lineage

- created by: Genesis
- ancestor: Seth
- codebase: body
- Git history: lineage
