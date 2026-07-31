# Agent extensions

Agent extensions add durable, bounded domain capabilities to an Enoch-based
agent without copying `enoch.app.core` or creating a parallel control plane.
They are intended for descendants such as a project manager, researcher, or
operator that need their own commands and persistent state while reusing
Enoch's providers, authorization, delivery, and task lifecycle.

The version 1 API provides:

- namespaced private state and artifact storage;
- chat commands with capability requirements and `/help` integration;
- typed command outcomes linked to durable tasks and output evidence;
- application lifecycle hooks and durable task-result events;
- a constrained façade over Enoch's single governed workflow.

Extensions do not receive polling ownership, provider selection, task execution,
recovery, publication, finalization, or raw queue mutation APIs. They can
enqueue and inspect work, then request bounded lifecycle transitions through
`ExtensionWorkflow`; Enoch remains the single lifecycle owner.

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
from enoch.extensions import (
    AgentExtension,
    ExtensionCommandResult,
    ExtensionCommandSpec,
)


def project(context):
    goal = context.argument.strip()
    if not goal:
        return ExtensionCommandResult.failure(
            "Usage: /project <goal>",
            code="missing_goal",
        )

    state = context.storage.private_path("projects.json")
    state.parent.mkdir(parents=True, exist_ok=True)
    job = context.enqueue_task(
        f"Decompose this project into a dependency graph: {goal}",
        context="Define deliverables, acceptance criteria, and owners.",
        idempotency_key=f"project:{goal}",
    )
    return ExtensionCommandResult.success(
        f"Queued project-planning task #{job.id}.",
        code="project_plan_queued",
        task_ids=(job.id,),
        output_refs=(f"artifact://projects/{goal}",),
    )


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
through the active profile's workflow policy and capability requirements. Its
optional `idempotency_key` is scoped to the extension. Use a stable,
domain-derived key whenever a command can be retried or can enqueue more than
one durable task; the message identifier remains the fallback for simple
one-task commands.

### Structured task requests

Extensions may attach versioned domain data without encoding it into prompt
text:

```python
from enoch.extensions import ExtensionArtifactReference


job = context.enqueue_task(
    "Build the approved project plan",
    metadata={
        "contract_version": 1,
        "project_id": "project-17",
        "review_policy": {"required": True},
    },
    artifact_refs=(
        ExtensionArtifactReference(
            "project-spec",
            "projects/project-17/spec.md",
            "text/markdown",
        ),
    ),
)
```

Metadata is copied into the durable task and task-event records under the
originating extension's provenance namespace. It must be a JSON object with
lowercase token keys, at most 16 KiB, five levels, 256 values, and 64 members
per object or array. Keys beginning with `_`, `$`, `enoch.`, or `system.` and
the exact keys `enoch`, `system`, and `schema_version` are reserved. Non-JSON
values, non-finite numbers, and oversized values fail before enqueue.

Artifact references are typed relative POSIX paths beneath
`.enoch/artifacts/extensions/<extension-name>/`. Absolute paths, parent
segments, URI-shaped values, backslashes, and paths beginning with
`extensions/` are rejected, so an extension cannot name another extension's
artifact boundary. References are durable identities; declaring one does not
grant filesystem access or copy the artifact into task prompt text.

`ExtensionWorkflow.features` and `supports()` expose optional workflow-engine
support. The local engine advertises `structured_task_metadata` and
`artifact_references`. Requesting either feature from an older engine fails
with `ExtensionWorkflowCapabilityError` before queue mutation; command handlers
receive the stable `workflow_capability_unavailable` enqueue result code.

## Typed command results

An extension command may return `ExtensionCommandResult` with:

- `final_text`, rendered through the active chat presentation;
- `status`, either `succeeded` or `failed`;
- a stable machine-readable `code`;
- durable `task_ids` created by the command;
- bounded `output_refs` for artifacts or other evidence.

The result contract is independently versioned by
`EXTENSION_COMMAND_RESULT_API_VERSION`. Returning a string remains the
backward-compatible shorthand for a successful text-only result with code
`ok`.

Enoch records `agent_extension_command_result` for every invocation. The audit
event retains the result version, status, stable code, task IDs, and output
references; chat receives only `final_text`. Authorization denial, enqueue
failure, capability unavailability, invalid typed output, and an isolated
handler exception are normalized to stable failure codes rather than escaping
the chat event loop. Internal exception text is not part of the chat contract.

## Extension-scoped task controls

`ExtensionWorkflow` exposes `status(task_id)`, `cancel(task_id)`,
`retry(task_id)`, and `rerun(task_id, idempotency_key=...)`. Each returns an
immutable `ExtensionTaskStatus`. A control request is accepted only when the
task carries the exact immutable provenance marker
`context_source=extension:<extension-name>`. Core tasks and tasks owned by
another extension fail before mutation with `ExtensionWorkflowControlError`;
its stable `code`, `operation`, and `task_id` fields are suitable for a typed
command result.

The lifecycle semantics are:

- `cancel` accepts owned pending, paused, or running work. It signals an active
  in-process worker, records the normal durable `cancelled` task event, and is
  idempotent once the task is cancelled.
- `retry` accepts only an owned failed task whose durable failure metadata says
  `retryable=True`. It creates the normal linked retry task with a new task ID;
  the original remains in history. A second live retry of the same failure is
  rejected by the workflow engine. Retry always preserves the failed task's
  metadata and artifact references because it is another attempt at the same
  durable request.
- `rerun` accepts any owned terminal task, including a regressed task. It
  creates a fresh task with `parent_task_id` pointing to the terminal source
  and preserves request, context, provenance, policy, and capability
  requirements. Metadata and artifact references are preserved unless the
  caller supplies explicit replacement values. The caller must provide a
  stable idempotency key. Repeating the same key returns the same rerun after a
  process restart with the first request envelope; a deliberate additional
  rerun or replacement uses a new key.

Every mutation still passes through the selected `WorkflowEngine`, so daemon
epoch fencing, atomic queue persistence, task events, and restart recovery are
unchanged. Extensions never receive finalization or storage mutation methods.

## Lifecycle and observability

Lifecycle hooks receive isolated storage and the extension-scoped workflow
façade. For each `EnochApplication` process:

- `on_initialize` runs when the extension is registered;
- `on_startup` runs exactly once when `start()` first establishes the
  application lifecycle, independently of whether a locked chat exists or a
  startup notification is sent;
- `on_shutdown` runs during application shutdown.

Calling `start()` or `notify_startup()` again does not repeat `on_startup`.
`/status` lists every active extension and its declared API version.

### Durable task events

An extension may register `on_task_event(context, event)` to observe work that
it submitted through `ExtensionWorkflow`. Enoch routes `queued`, `started`,
`completed`, `failed`, and `cancelled` events only to the originating
extension. Events include a stable ID, request and result summaries, failure
metadata, revision and review identities, runtime output references, and an
extension-scoped `delivery_key`. They also retain the original structured
metadata and artifact references across restart and at-least-once replay.

```python
from enoch.extensions import ExtensionLifecycleHooks


def task_event(context, event):
    if event.event == "completed":
        record_deliverable(
            task_id=event.task_id,
            result=event.result_summary,
            revision=event.revision_id,
            reviews=event.review_urls,
        )


extension = AgentExtension(
    name="manager",
    lifecycle=ExtensionLifecycleHooks(on_task_event=task_event),
)
```

Delivery is durable and ordered per extension. Enoch writes a success receipt
after the hook returns. A hook failure, daemon restart, or crash before that
receipt replays the same stable event ID, so handlers must apply the event
idempotently. This at-least-once contract avoids losing a completion while
remaining honest about the crash window between an extension's state change
and Enoch's receipt.

Receipts belong to
`.enoch/extensions/<extension-name>/task_event_receipts.jsonl`; the canonical
task events remain in the selected workflow engine's artifact storage.

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

The current contracts are `AGENT_EXTENSION_API_VERSION = 1` and
`EXTENSION_COMMAND_RESULT_API_VERSION = 1`. Enoch rejects unsupported versions.
Extension command and lifecycle failures are isolated and recorded as system
events.

Downstream extension packages should inherit
`AgentExtensionConformanceMixin` and, when possible, provide an
`ExtensionCommandCase`. See [Extension conformance](conformance.md).
