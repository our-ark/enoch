# Enoch architecture

Enoch core is organized around domain boundaries rather than infrastructure
brands. Five provider contracts isolate chat, agent runtime, version control,
code forge, and host-service infrastructure from agent behavior. Core includes
portable defaults for Codex, Git, and local-only publication; reference
Telegram, GitHub, launchd, and systemd integrations live under `libraries/`.
Every implementation enters the application through the same provider loading
and validation boundary.

## Core packages

| Package | Responsibility |
| --- | --- |
| `enoch.app` | Provider-neutral event loop, command orchestration, parsing, and presentation |
| `enoch.tasks` | Task queue state, audit events, failure policy, configuration, and isolated worktrees |
| `enoch.evolution` | Evolution state, candidate collection and ranking, event history, and governed lifecycle |
| `enoch.evolution.sources` | Feedback, experience, and brainstorming evidence adapters |
| `enoch.operations` | Background-service facade and software update lifecycle |
| `enoch.providers` | Shared provider contracts, selection, and core adapters |
| `enoch.profiles` | Versioned downstream-agent commands, prompt context, lifecycle hooks, and discovery |
| `enoch.memory` | Durable memory paths, prompts, and storage |
| `enoch.lineage` | Ancestor configuration, discovery, and adoption context |
| `enoch.skills` | Skill catalog code and packaged skill assets |

Small, cohesive capabilities such as backlog and cron remain single top-level
modules. A directory is introduced only when a capability has multiple modules
with a shared lifecycle.

## Dependency direction

Domain packages may depend on foundational configuration, paths, memory, and
provider contracts. Infrastructure libraries implement those contracts and do
not become imports in domain code. Provider-specific configuration and setup
remain owned by each implementation. `enoch.app.core` composes the domains;
domain packages must not import the application orchestrator.

Portable task flows use semantic provider operations rather than parsing Git,
GitHub, Telegram, or service-manager commands. Compatibility escape hatches may
exist inside a concrete provider, but they are not part of the portable core
contract.

The stable executable surfaces remain `bin/enoch`, `bin/enoch-agent`, and
`bin/enoch-daemon`. Internal Python module paths may evolve with the package
boundaries, while these launchers and chat commands remain stable.

Agent profiles sit above domain and provider contracts. They may contribute
commands, context, persisted workflow defaults, bounded presentation labels,
and lifecycle hooks or enqueue governed work, but they do not poll chat,
execute tasks, recover queue state, or persist a parallel control plane. This
keeps downstream product behavior composable while `enoch.app` remains the
single application and workflow owner.
