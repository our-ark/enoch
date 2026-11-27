# Task completion gates

An extension can keep a successful worker's task in `awaiting_completion` until
child tasks and named evidence are ready. This durable nonterminal state releases
the worker process but blocks unrelated queued/direct work. Declared children can
still run. Tasks without a gate retain the existing lifecycle.

The base workflow API remains v4. Engines opt into `completion_gates` through
`CompletionGateWorkflow`. The independent gate contract is
`COMPLETION_GATE_API_VERSION = 1`.

## Public API

`enoch.extensions` exports `CompletionGate`, `CompletionSnapshot`,
`CompletionDecision`, `CompletionHook`, and `COMPLETION_GATE_API_VERSION`.
The namespaced `ExtensionWorkflow` exposes:

- `declare_completion_gate(task_id, gate)` for an owned pending/running task;
- `record_completion_evidence(task_id, name, reference)` for append-only evidence;
- `status(task_id)` with `completion_gate` and `completion_evidence` fields;
- `cancel(task_id)`, including tasks awaiting completion.

Register `ExtensionLifecycleHooks.on_task_completion` before declaring gates.
The application registers these hooks before recovery. Unsupported engines and
declarations without a registered owner hook fail explicitly.

```python
from datetime import datetime, timedelta, timezone
from enoch.extensions import (
    AgentExtension, CompletionDecision, CompletionGate, ExtensionLifecycleHooks,
)

def confirm_completion(snapshot):
    if any(child.status != "completed" for child in snapshot.children):
        return CompletionDecision("fail", "A required child did not succeed.")
    return CompletionDecision("complete", "Review and cleanup verified.")

extension = AgentExtension(
    name="review-flow",
    lifecycle=ExtensionLifecycleHooks(on_task_completion=confirm_completion),
)

# In a handler, using its namespaced workflow:
parent = workflow.enqueue(conversation_id, "Implement the change")
review = workflow.enqueue(conversation_id, "Independently review the change")
workflow.declare_completion_gate(parent.id, CompletionGate(
    deadline_at=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
    child_task_ids=(review.id,), evidence_keys=("cleanup",),
))

# Later, after the extension verifies the external cleanup:
workflow.record_completion_evidence(parent.id, "cleanup", "cleanup-receipt:123")
```

Declare the gate before allowing the parent worker to finish. Children must
already exist in the same extension and conversation. Dependencies must be
acyclic; nested gates are supported. Unrelated earlier items in the same lane
cannot prevent declared children from running. Existing pause and application
holds still apply. This is global queue admission, not another scheduler.

## Evaluation and authority

All children must be terminal and all named evidence present. Terminal includes
`completed`, `failed`, `cancelled`, and `regressed`; the owner decides whether the
outcomes are acceptable. Evidence references are extension-owned assertions,
not automatically verified external facts. The extension must verify facts
before appending references. Recorded references cannot be overwritten.

The engine evaluates after child finalization/cancellation, evidence appends,
maintenance reconciliation, and before starting queued work. A ready gate invokes
its owner with a detached snapshot of parent, children, and evidence. The owner
may return `CompletionDecision("complete", note)`, `CompletionDecision("fail",
note)`, or `CompletionDecision("extend", note, gate=new_gate)`. A replacement
must change the conditions and/or deadline and have a future deadline.

Only the engine commits transitions. It rechecks epoch and snapshot identity;
concurrent cancellation, evidence changes, or another finalization invalidate
old advice. Final state is committed once. A nested child's completion causes
its parent to be rechecked in the same pass.

Callbacks are bounded, read-only advice (five seconds by default). They must not
invoke a model, enqueue tasks, write files, or perform external actions. Use
ordinary command/lifecycle handlers for actions. Callbacks may repeat after
restart or a concurrent change and must be idempotent. Hooks are trusted Python,
not a security sandbox: a timed-out thread cannot be killed safely, but its late
result cannot finalize a task. No epoch/queue transaction is held while waiting
for a callback.

## Failure and recovery

- Deadlines are absolute timezone-aware ISO timestamps, preserved on restart.
  Worker execution keeps its separate task timeout. Gate expiry is enforced once
  the worker returns; it does not interrupt an active worker.
- Expiry ends as `failed`, with `completion_gate_timeout`. Invalid advice,
  callback errors/timeouts end with `completion_gate_failed`. Both are permanent,
  non-automatically-retryable outcomes and release the parent's admission hold.
- A missing hook after restart holds until restored or expired. Reconciliation
  never reruns an awaiting worker. Recovering durable worker-success evidence,
  including extension recovery advice, still respects its declared gate.
- Cancelling a gated parent **detaches** children: they retain their current
  state and become ordinary queued/running work. A child's own gate remains in
  force. Timeout/failure likewise does not implicitly cancel children.
- Explicit retry/rerun creates a new task. Extensions must declare fresh gates
  and deadlines; old children/evidence are not silently reused as a new gate.

`/queue`, `/task show`, and the task board display waiting tasks. Queue inspection
shows child states, missing evidence, and deadlines. Worker success does not
announce completion while a gate is unmet. Final outcomes use durable delivery.
Extension task-event hooks receive `awaiting_completion` and terminal events
through the existing at-least-once delivery path.

Queue schema 17 persists gates, evidence, and a separate `awaiting` section.
Migration preserves them; older applications reject the newer schema. Host
migration checks waiting tasks for host-local workspace references.

## Conformance

`enoch.conformance.CompletionGateConformanceMixin` checks admission, child
execution, finalization, restart/stale epochs, timeout, and cancellation. Supply
`create_completion_workflow(root, *, epoch, clock)` with a hermetic engine.
Core tests also cover nested gates, concurrency, callback failure, recovery,
migration, ownership, and chat integration. The installed-wheel test loads an
independent extension, declares its gate, and releases it using public APIs.
