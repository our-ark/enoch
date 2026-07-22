# Contributing

Enoch changes should strengthen agent-owned operation, co-evolution,
body/private-state boundaries, reproducible descent, provider portability, or
human-reviewed evolution.

## Local checks

Use Python 3.11 or newer and run:

```bash
python -m unittest discover -s tests
```

The suite includes hermetic evolution tests that use temporary Git repositories
and local substitutes for external services. Core tests must not require live
GitHub, Telegram, or Codex credentials.

Changes to `genesis.toml`, package identity, launchers, or runtime dependencies
must also pass the Genesis cross-artifact release gate from an adjacent clean
Genesis checkout:

```bash
python scripts/verify_enoch_descent.py \
  --source ../enoch \
  --ref HEAD \
  --name my-agent
```

Do not add credentials, memories, conversation logs, chat identifiers,
machine-local paths, task worktrees, or instance configuration to commits,
fixtures, or test output.

## Pull requests

Keep changes focused, explain their effect on the agent body and private-state
boundary, and include the exact checks run. Evolution-generated changes remain
candidates until a human reviews and adopts them.
