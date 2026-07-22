# Security Policy

## Reporting a vulnerability

Please report vulnerabilities through the repository's private GitHub security
advisory flow. Do not open a public issue for a suspected credential leak,
arbitrary-code-execution path, unsafe repository mutation, provenance bypass,
or private-state disclosure.

Include the affected Enoch commit, operating system, Python version, provider
configuration, and the smallest safe reproduction you can provide.

## Runtime trust boundary

Enoch is a local software agent. It can invoke configured agent runtimes and
Git tooling, create worktrees and branches, and prepare changes for review.
Those capabilities are not a semantic sandbox. Run Enoch only with credentials,
repositories, tools, and provider plugins you intend it to access.

Installed provider plugins and pinned runtime dependencies execute local code.
Review their source and immutable version before enabling them. Human review is
the adoption boundary for evolution-generated changes.

## Private state

Credentials, memories, logs, chat identifiers, instance configuration, and task
worktrees belong outside the tracked software body. They must remain in ignored
state such as `.enoch/` or `.agent/instance.yaml` and outside the paths declared
by `genesis.toml`.

Before publishing a fork or descendant, audit its current tree, all reachable
Git refs, and complete public history.
