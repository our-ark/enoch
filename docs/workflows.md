# Workflow engines

`enoch.workflows.WorkflowEngine` is the versioned public boundary for Enoch's
single-owner task lifecycle. The current contract is
`WORKFLOW_API_VERSION = 3`.

The engine owns:

- enqueueing queued, front-of-queue, and immediate tracked work;
- starting and claiming one running task;
- durable worker heartbeats;
- cancellation, pause, retry, and terminal finalization;
- interrupted-worker recovery;
- queue inspection and task lookup;
- task status, runtime evidence, workspace, revision, and review records;
- bounded extension request metadata and artifact references;
- persisted task capability requirements used by the application authorizer.

`LocalWorkflowEngine` is the default file-backed implementation. It preserves
the existing `.enoch/task_queue.json` state and serializes mutations through
the queue transaction lock. When Enoch constructs it, every mutation is also
guarded by the current daemon epoch, so an obsolete daemon cannot enqueue,
claim, heartbeat, cancel, finalize, recover, or update task evidence.

## Injection

An embedding application may supply another implementation without changing
`enoch.app.core`:

```python
from enoch.app.core import EnochApplication
from enoch.workflows import LocalWorkflowEngine


class RecordingWorkflow(LocalWorkflowEngine):
    def __init__(self, root):
        super().__init__(root)
        self.claimed = []

    def claim(self, task_id, worker_id, worker_pid):
        claimed = super().claim(task_id, worker_id, worker_pid)
        if claimed is not None:
            self.claimed.append(claimed.id)
        return claimed


workflow = RecordingWorkflow(root)
app = EnochApplication(
    identity,
    root,
    chat,
    runtime=runtime,
    workflow=workflow,
)
```

Enoch validates the runtime-checkable protocol and `api_version` during
application construction. Unsupported versions fail explicitly.

Version 2 replaces the Git-shaped `record_worktree`,
`record_publish_state(**state)`, and `result_has_pull_request` surface with
`record_workspace` and a typed `record_publication(TaskPublicationState)`
operation. Durable `TaskJob` state now exposes `workspace_path`,
`workspace_id`, `revision_id`, `review_id`, `review_url`, `review_urls`, and
`review_published`.

Queue schema 14 reads schema 11's `worktree_path`, `branch_name`,
`commit_sha`, `remote_branch`, `pr_url`, `pr_urls`, and
`published_remotely` keys, then writes only the provider-neutral names. It also
adds extension request metadata, typed artifact references, and canonical
execution lanes; older tasks migrate with empty values.
Read-only Python properties preserve those old attribute names during the
migration window. Workflow API v1 implementations must deliberately adopt the
v2 methods before injection; `LocalWorkflowEngine` retains v1 method adapters
for direct callers.

Version 3 replaces open-ended `Any` / `**options` mutation calls with explicit,
typed arguments for enqueue, retry, pause, resume, regression, and resolution.
This keeps provider and lifecycle evidence visible to alternate workflow
implementations and makes unsupported call shapes fail at the API boundary.
The local engine still exposes the bounded v1 publication adapters for stored
state migration, but injected engines implement only the current typed
protocol.

Task-event schema 9 records the same opaque `workspace_id`, `revision_id`, and
`review_id` values with `review_urls` at every lifecycle transition. Event
readers continue to accept earlier schemas, including Git-shaped
`branch_name`, `commit_sha`, and `pr_urls` evidence. Schema 9 also carries the
extension request payload and execution lane without mixing either into task
prompt text.

Workflow API v3 engines may advertise optional behavior through a `features`
iterable. `workflow_features()` normalizes discovery, and extension workflows
expose it through `features` and `supports()`. The local engine currently
advertises `structured_task_metadata`, `artifact_references`, and
`execution_lanes`. The local scheduler still retains one global running task;
lane support defines ordering and blocking rather than parallel workers. An
older API v3 engine without `features` continues to work until an extension
requests an optional feature, which then fails before enqueue.

Profiles do not receive or own the concrete engine. Their
`CommandContext.enqueue_task()` method routes through the engine selected by
the application, preserving one queue owner and keeping profile packages
portable.

Extension schedules are also application-owned rather than a second workflow
engine. `AgentExtension.schedules` is reconciled into
`.enoch/extension_schedules.json`, and the existing scheduler thread claims due
occurrences alongside user-created cron work. Each occurrence is authorized
against the active profile and providers, then enters the selected engine
through the extension-scoped workflow façade. A persisted claim and scoped
idempotency key prevent duplicate enqueue after process restart.

The schedule layer supports fixed-rate intervals and daily IANA-timezone
calendar targets. Missed targets coalesce into one occurrence, and an
outstanding pending, running, or paused task prevents overlap for that
schedule. Pause, resume, run-now, declaration upgrades, and extension removal
change schedule state without bypassing queue ownership. Task-level retry and
recovery remain responsibilities of the workflow engine.

## Contract behavior

`enqueue()` accepts `mode="queued"`, `"front"`, or `"direct"`. `start_next()`
atomically moves one due task into the running slot, and `claim()` binds that
task to a worker identity and process. `heartbeat()` refreshes the durable
claim. `finalize()` accepts only `completed`, `failed`, or `cancelled`.

An extension may assign a local lane token such as `project-17`. Enoch stores
the canonical key `extension:<extension-name>:<lane>`, preventing two
extensions with the same local token from sharing a concurrency boundary.
Within one lane, pending work remains FIFO: a delayed retry or paused task
blocks later work in that lane. A ready task in another lane may proceed, so
one blocked domain workflow does not stall unrelated extension work. Cancelling
the blocker releases the lane. Retry and interrupted-worker recovery preserve
the lane; extension rerun preserves it by default and may explicitly replace
or clear it.

Every core repository task records `runtime.execute`, `vcs.inspect`,
`vcs.authoritative`, `vcs.workspace`, `vcs.capture`, and `forge.review`
requirements when it is queued. Profiles may add requirements but cannot remove
these core guarantees. Requirements survive pause, restart, recovery, and
retry, and authorization denial is a permanent, non-retryable task failure
recorded before the runtime or repository provider is invoked.

The default engine uses worker identity checks on owned mutations and daemon
epoch fencing around every state-changing operation. `inspect()` and `find()`
are read-only. Implementations may use another storage backend, but must retain
the single-running-task invariant, idempotent enqueue behavior, ownership
checks, and explicit recovery semantics.

Workflow state fencing is paired with `enoch.app.effects.DaemonEffectFence`.
Bounded VCS, forge, notification, and private-state effects execute while
holding the daemon epoch lock, so takeover and the effect have one atomic
order. Long runtime calls do not hold that lock. An epoch monitor instead sets
their standard cancellation event when a replacement daemon takes ownership,
and the stale invocation cannot record a result, finalize its task, publish,
or send a terminal notification.

The core and portable-install suites run the application with an injected fake
runtime and recording workflow implementation. This verifies profile command
enqueueing, claim, execution, evidence recording, and terminal finalization
through only the public APIs.
