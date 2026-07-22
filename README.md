# Enoch

Enoch is a Genesis-created agent descended from Seth.

## Mission

Seth's self-evolving descendant: walk with her code body, inspect her own work, and grow through safe autonomous improvements while preserving human sovereignty.

## Requirements

- Python 3.11, 3.12, or 3.13
- Git
- A working Codex CLI login for reasoning and evolution tasks
- GitHub CLI authentication when publishing branches or pull requests
- Telegram credentials only when using the optional Telegram interface

## Quick Start

Clone the repository and start Enoch from its checked-out software body:

```bash
git clone https://github.com/our-ark/enoch.git
cd enoch
bin/enoch --help
bin/enoch
```

The launchers select a supported Python interpreter and keep runtime state,
downloaded dependencies, credentials, memories, and logs under ignored local
paths. Do not commit files from `.enoch/` or `.agent/instance.yaml`.

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

The Codex executable is resolved independently in this order:

1. `ENOCH_CODEX_BIN`
2. `codex.executable` in `.enoch/config.yaml`
3. `codex` on `PATH`
4. Known Codex locations inside the ChatGPT or Codex macOS app

Use `/config runtime codex executable <path|auto>` to configure or restore
automatic discovery. The daemon reads this instance setting directly.

## Providers

Codex and Git are core defaults. Telegram and GitHub are reference provider
packages under `libraries/`. Installed Python packages can add or replace chat,
agent runtime, version control, and code forge providers through the
`enoch.providers` entry-point group. Select them in `.enoch/config.yaml` or with
`/config provider`.

Provider contracts, packaging examples, provider-specific settings, normalized
chat events, and migration compatibility are documented in
[`docs/providers.md`](docs/providers.md).

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

Reusable provider and skill implementations may live under `libraries/`, outside
the inherited body. Enoch's Telegram, Telegram vision, and GitHub integrations
use this model: descendants inherit provider contracts, configuration, and core
behavior, while `genesis.toml` keeps immutable dependencies on selected provider
commits instead of copying concrete integrations into every descendant body.

## Lineage

- created by: Genesis
- ancestor: Seth
- codebase: body
- Git history: lineage

Seth is a private predecessor and is not part of the v1 open-source release.
Enoch is the first publicly released reference body; using or descending from
Enoch does not require access to Seth.

## Security and Autonomy

Enoch can invoke local agent runtimes and Git tooling, create branches, and
prepare changes for human review. Run it only in repositories and accounts you
intend it to access, keep credentials in ignored instance state, and inspect
proposed changes before adoption. See [SECURITY.md](SECURITY.md) for the trust
boundary and private reporting process.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development checks and contribution
guidelines.

## License

Enoch is licensed under the [Apache License 2.0](LICENSE).
