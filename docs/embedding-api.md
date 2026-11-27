# Application embedding

Hosts that already own their chat loop can use two supported methods on
`enoch.app.core.EnochApplication`. These methods reuse the application's
configured workflow, runtime, authorization, and daemon epoch. They do not
create another queue or transfer workflow ownership to the host.

## Public contract inventory

| Symbol | Contract |
| --- | --- |
| `enoch.embedding.EMBEDDING_API_VERSION` | Currently `1`; also exposed as `app.embedding_api_version` |
| `enoch.embedding.EmbeddedApplication` | Runtime-checkable protocol for the two entry points |
| `app.handle_evolve_command(conversation_id, argument="")` | Synchronous command handling; returns `EvolveCommandResult` |
| `app.resume_pending_tasks(*, max_tasks=1, promote_backlog=False, reconcile_schedules=False)` | Bounded synchronous execution; returns `PendingTaskExecutionResult` |
| `enoch.embedding.EvolveCommandResult` | Immutable `conversation_id` and `reply` |
| `enoch.embedding.PendingTaskExecutionResult` | Immutable `task_ids` and `stop_reason` |
| `enoch.embedding.EmbeddingConversationDenied` | `PermissionError` for a conversation outside the configured chat boundary |
| `enoch.embedding.StaleDaemonEpoch` | The existing typed epoch error, re-exported for hosts |
| `enoch.conformance.EmbeddingConformanceMixin` | Reusable behavioral contract tests |

Use the [application composition](application-composition.md) boundary to
select providers, profile, extensions, and workflow. Direct application
construction is supported for embedding; keep one active application per
instance root. Constructing another application begins a new epoch unless
the composition's existing epoch is explicitly passed.

## Evolve commands

```python
from enoch.embedding import StaleDaemonEpoch

try:
    result = app.handle_evolve_command(conversation_id, "config mode co-evolve")
    host_deliver(result.conversation_id, result.reply)
except StaleDaemonEpoch:
    stop_obsolete_host()
```

Pass only the arguments after `/evolve`, not a command prefix or full chat
event. An empty argument shows the existing evolve report. Existing command
validation, mode rules, approval provenance, and runtime authorization apply.
The returned reply may describe a validation failure; receiving a result does
not mean a candidate was approved or a task completed. The method does not
send the returned reply, poll chat, start a background worker, or reconcile
schedules. Runtime-backed commands can still emit their usual progress updates.

Conversation filtering follows the configured chat provider. Mutating actions
that require a locked conversation retain that requirement. Hosts must
authenticate the human request before calling this API; passing a conversation
ID is not an authentication mechanism. This API is not a chat inbox receipt or
command deduplication service.

## Pending tasks

```python
result = app.resume_pending_tasks(max_tasks=2)
print(result.task_ids, result.stop_reason)
```

This call executes at most `max_tasks` attempts through the existing task
runner, including claim, authorization, runtime timeout/cancellation,
publication, finalization, and task notifications. `max_tasks` must be a
positive integer. Task IDs describe attempted work, not successful work;
automatic retries may report the same ID again. Inspect the normal workflow
status for task outcomes.

Despite its name, `resume_pending_tasks` **does not unpause tasks**. Runtime
access pauses and extension recovery holds remain in force. It also respects
restart, automatic-update, and review-merge holds. The latter continues to
allow eligible CI-fix tasks, just like the daemon worker.

`stop_reason` is one of:

- `idle`: no runnable pending work, including when delayed retries are not due;
- `limit`: the requested attempt budget was reached;
- `paused`: a paused task holds the queue;
- `busy`: the application's worker slot or workflow already has an owner;
- `stopping`, `restart`, `update`, or `review`: an existing lifecycle hold.
- `completion`: a completion gate is waiting and no child is runnable.

An embedded call reserves the same worker slot as the background daemon, and
releases it even if execution raises. A busy call does not replace the owner.
As with the daemon, a pending restart can schedule the configured restart once
its blockers drain. The method does not bypass the selected workflow's
single-owner lifecycle or read/write its raw queue files.

By default, no backlog item is promoted, no scheduler is awakened, and no
schedule reconciliation is performed. Opt-ins are deliberately separate:

- `promote_backlog=True` admits an idle backlog item when the queue is empty;
- `reconcile_schedules=True` wakes the task scheduler and checks cron and
  extension schedules **after each attempted task**, as the daemon worker does.
  It is not a full scheduler tick and does nothing on an empty queue.

Neither option polls chat or runs periodic evidence/evolve scans. Use the normal
daemon lifecycle when full background orchestration is required. Previously
started background threads remain independent of this call; the defaults do
not disable them.

Both entry points check epoch ownership before and after execution; task
mutations and runtime effects use the existing workflow/effect fences.
`StaleDaemonEpoch` propagates to the host, including takeover during a call,
rather than being swallowed as it is by the daemon's background worker. Stop
using that obsolete application; do not create a new epoch just to retry an
operation owned by another live daemon. Invalid API arguments and unexpected
infrastructure exceptions also propagate. Normal task failures/pauses are
recorded through the workflow rather than translated into success.

## Compatibility and conformance

These names, signatures, result fields, and documented semantics are supported
public APIs. A breaking change or removal must be announced in release notes
and documentation **at least one published release before it takes effect**,
with a supported migration path. Where applicable, deprecated use will emit
`DeprecationWarning` during that window. Breaking changes advance the embedding
API version; adding optional behavior does not silently change the defaults.
Underscored worker/command helpers remain implementation details.

Combine `EmbeddingConformanceMixin` with `unittest.TestCase`. Supply
`create_embedded_application(root)` with isolated, network-free providers and
`enqueue_embedding_task(app)` returning a new task ID. Set
`embedding_conversation_id` to the fixture's allowed conversation. The mixin
checks typed command results, idle/bounded execution, and stale-epoch rejection.
Core tests additionally cover pauses, lifecycle holds, concurrent ownership,
opt-in scheduling/backlog, exception cleanup, and real task completion. The
offline installed-wheel suite executes both entry points with independent
provider packages.
