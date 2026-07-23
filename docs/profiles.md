# Agent profiles

Agent profiles extend Enoch's behavior without forking `enoch.app.core` or
creating a second task queue. The version 1 profile API composes four bounded
surfaces:

- `CommandSpec` adds chat commands without replacing core commands.
- `PromptContext` contributors add profile-specific context to conversation,
  image, task-context, and task prompts.
- `LifecycleHooks` observe application initialization, startup, polling runs,
  and shutdown.
- `CommandContext.enqueue_task()` submits work to Enoch's governed task queue.

Profiles do not own polling, task execution, recovery, provider selection, or
state persistence. Those remain under the core application's control.

## Define a profile

```python
from enoch.profiles import AgentProfile, CommandSpec, LifecycleHooks


def research(command):
    if not command.argument:
        return "Use /research <topic>."
    job = command.enqueue_task(
        f"Research {command.argument}",
        context="Prefer primary sources and preserve source URLs.",
    )
    return f"Queued research task #{job.id}."


def research_context(context):
    if context.purpose in {"conversation", "task-context", "task"}:
        return "Approach factual claims as a researcher and preserve provenance."
    return ""


def create_profile(root=None):
    return AgentProfile(
        name="researcher",
        commands=(
            CommandSpec(
                name="research",
                summary="queue a sourced research task",
                usage="/research <topic> - queue a sourced research task",
                handler=research,
            ),
        ),
        prompt_contributors=(research_context,),
        lifecycle=LifecycleHooks(),
    )
```

Command names are lowercase chat-command identifiers. A profile may define
aliases, but it cannot shadow Enoch's core commands. Profile command failures
are reported to the conversation and system log; prompt and lifecycle hook
failures are logged without stopping the daemon.

## Package and select a profile

Third-party packages expose profile factories through the versioned
`our_ark.profiles` entry-point group:

```toml
[project.entry-points."our_ark.profiles"]
researcher = "my_agent.profile:create_profile"
```

Select one profile in private instance configuration:

```yaml
agent:
  profile: researcher
```

The same selection is available through chat or the admin CLI:

```text
/config profiles
/config profile researcher
/config profile default
```

`/config profiles` distinguishes the profile in the running process from the
one selected for the next restart. `/status` reports the active profile. A
profile change is activated only after restarting Enoch.

`ENOCH_PROFILE=researcher` overrides the instance setting. Applications that
embed Enoch can instead pass `profile=` directly to `EnochApplication` or use
`register_profile()` for static registration.

The current contract is `PROFILE_API_VERSION = 1`. A profile must declare that
version (the default) and Enoch rejects unsupported versions at startup rather
than guessing compatibility.

## Context boundaries

Command handlers receive identity, repository root, normalized chat event,
selected runtime and forge providers, and the command argument. The provided
`enqueue_task()` method records the request as a human-created `task`, keeps the
profile command as its trigger, and uses the existing queue lifecycle.

Prompt contributors receive an immutable context and return additional text.
Enoch appends non-empty contributions under a `Profile context` section; the
core safety and work prompts remain intact.

The hermetic portable-install test builds a disposable profile distribution,
discovers it through its installed entry point, executes its command, and
verifies that the resulting task uses Enoch's queue and provenance records.
