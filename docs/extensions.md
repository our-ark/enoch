# Agent extensions

Agent extensions add durable, bounded domain capabilities to an Enoch-based
agent without copying `enoch.app.core` or creating a parallel control plane.
They are intended for descendants such as a project manager, researcher, or
operator that need their own commands and persistent state while reusing
Enoch's providers, authorization, delivery, and task lifecycle.

The version 1 API provides:

- namespaced private state and artifact storage;
- chat commands with capability requirements and `/help` integration;
- application lifecycle hooks;
- a constrained façade over Enoch's single governed workflow.

Extensions do not receive polling ownership, provider selection, task execution,
recovery, publication, or queue mutation APIs. They can enqueue, inspect, and
find work through `ExtensionWorkflow`; Enoch remains the lifecycle owner.

## Profiles and extensions

An application has one active `AgentProfile` and zero or more
`AgentExtension` instances.

- A profile defines the agent-wide operating mode: prompt context, workflow
  policy, authorization policy, presentation, commands, and lifecycle hooks.
- An extension adds a domain module with its own namespace, commands, and
  lifecycle integration.

Use a profile to change how the whole agent operates. Use an extension to add a
capability that can coexist with other capabilities.

## Define an extension

```python
from enoch.extensions import ExtensionCommandSpec, AgentExtension


def project(context):
    goal = context.argument.strip()
    if not goal:
        return "Usage: /project <goal>"

    state = context.storage.private_path("projects.json")
    state.parent.mkdir(parents=True, exist_ok=True)
    job = context.enqueue_task(
        f"Decompose this project into a dependency graph: {goal}",
        context="Define deliverables, acceptance criteria, and owners.",
    )
    return f"Queued project-planning task #{job.id}."


def create_extension(root=None):
    del root
    return AgentExtension(
        name="manager",
        help_heading="Communication & collaboration",
        commands=(
            ExtensionCommandSpec(
                "project",
                "manage a project graph",
                project,
                usage="/project <goal> - create a project plan",
            ),
        ),
    )
```

`context.storage.private_state` resolves beneath
`.enoch/extensions/<extension-name>/`. Artifacts resolve beneath
`.enoch/artifacts/extensions/<extension-name>/`. The software body is shared
and remains read-only by convention during normal extension operation.

`context.enqueue_task()` records extension provenance and routes the task
through the active profile's workflow policy and capability requirements.

## Package and select an extension

Expose a factory through the `our_ark.extensions` entry-point group:

```toml
[project.entry-points."our_ark.extensions"]
manager = "my_agent.manager:create_extension"
```

Select extensions in private instance configuration:

```yaml
agent:
  extensions: manager, another-extension
```

For a one-off process, set `ENOCH_EXTENSIONS=manager,another-extension`.
Applications embedding Enoch may pass `extensions=` directly to
`EnochApplication` or use `register_extension()` for static registration.

Command names must be globally unique across Enoch core commands, the active
profile, and every loaded extension. Invalid or conflicting selections fail at
startup so `/help` remains authoritative.

## Trust and compatibility

Extension packages are trusted Python code, not a sandbox boundary. Capability
checks govern use of Enoch's public provider and workflow surfaces; arbitrary
Python code installed by an operator still has the process's operating-system
permissions.

The current contract is `AGENT_EXTENSION_API_VERSION = 1`. Enoch rejects an
extension that declares another version. Extension command and lifecycle
failures are isolated and recorded as system events.
