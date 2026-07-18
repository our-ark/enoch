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
cd /path/to/your/enoch-instance

bin/enoch setup-token <token>
bin/enoch setup-chat <your-chat-id>
bin/enoch-daemon start
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

## Providers

Telegram, Codex, Git, and GitHub are built-in providers rather than required
fork points. Installed Python packages can add chat, agent runtime, version
control, and code forge providers through the `enoch.providers` entry-point
group. Select them in `.enoch/config.yaml` or with `/config provider`.

Provider contracts, packaging examples, normalized chat events, and migration
compatibility are documented in [`docs/providers.md`](docs/providers.md).

## Testing

Run the unit and hermetic evolution E2E tests with:

```bash
python -m unittest discover -s tests
```

The E2E design and covered workflows are documented in
[`docs/testing.md`](docs/testing.md).

## Descending from Enoch

Enoch is a public Genesis-compatible reference body. Its `genesis.toml`
declares the Git-tracked body boundary, inherited validation, launchers, source,
packaging metadata, and regression contracts. Runtime credentials, memories,
logs, chat identifiers, and instance configuration under `.enoch/` remain
private state and are excluded from descent.

From an adjacent clean Genesis checkout:

```bash
genesis create my-agent \
  --from enoch \
  --source ../enoch \
  --ref HEAD \
  --mission "Describe the descendant's purpose." \
  --repo ../my-agent
```

Genesis stages the descendant, runs Enoch's inherited tests, and accepts birth
only if validation passes without modifying the staged body.

## Lineage

- created by: Genesis
- ancestor: Seth
- codebase: body
- Git history: lineage

## License

Enoch is licensed under the [Apache License 2.0](LICENSE).
