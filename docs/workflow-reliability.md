# Workflow reliability

Enoch treats chat delivery, task execution, publication, and scheduling as
durable workflows rather than one uninterrupted function call.

## Chat inbox

Each normalized chat event receives a stable receipt under the configured
channel. A completed receipt stores the response before the provider cursor is
advanced, so redelivery after a restart does not repeat the command. Task,
backlog, and cron creation also use the receipt as an idempotency key.

Unexpected handler failures remain retryable for three deliveries. After the
third failure, Enoch records and acknowledges the poison event and sends a
bounded diagnostic response instead of repeatedly crashing the daemon.

## Notification delivery

Outbound sends and edits use an intent-first journal under
`.enoch/channels/<provider>/notifications.json`. The journal records `pending`,
`in_flight`, `delivered`, `retryable_failure`, and `terminal_failure` states,
including attempts, provider receipts, and the daemon epoch that owns the
claim.

Starting a daemon creates a new monotonically increasing generation with a
random fencing token. The epoch lock is held across each provider side effect
and receipt commit, so a replacement daemon cannot become current midway
through a delivery. Calls from an already stale daemon fail before reaching the
provider.

After restart, Enoch resumes `pending` and `retryable_failure` notifications
and reconciles every `in_flight` notification. A provider with the optional
durable-notification capability can look up the original idempotency key or
replay it idempotently. A provider without either capability fails ambiguous
work closed instead of risking a duplicate. Inbox replies, task status
messages, terminal task reports, and scheduled evolve reports use stable
logical keys across recovery.

Terminal task status is monotonic: late progress callbacks cannot overwrite a
completed, failed, cancelled, or regressed status. Repeating a terminal send
returns the durable receipt for the original logical notification.

## Daemon effect fencing

The same daemon epoch governs task mutations and external effects. Bounded
operations such as workspace mutation, revision capture, review publication or
landing, and durable local learning run under the epoch lock. An obsolete
daemon is rejected before the provider is called; a replacement daemon waits
for an already-authorized bounded operation to finish before taking ownership.

Runtime invocations may last minutes, so they do not hold the takeover lock.
Enoch monitors the active epoch while the runtime executes and sets the
provider-standard cancellation event if ownership changes. On return, it
revalidates the epoch. A stale worker therefore cannot persist runtime
evidence, finalize the task, continue publication, or deliver its final
notification.

## Task publication

Task results use `WorkOutcome`, separating status, failure code, retryability,
artifacts, and completed stages from chat presentation text. Publication
persists `validated`, `captured`, and `review_published` stages with opaque
workspace, revision, and review identities.

If review publication or workspace cleanup fails, the task retains its
workspace, captured revision, review evidence, and last completed stage.
Automatic retry resumes at that boundary instead of running the coding agent
again. Schema 11's `committed`, `pushed`, and `pr_opened` checkpoints remain
readable and are normalized when a legacy Git/GitHub task resumes.

## Scheduled occurrences

Cron and evolve schedules use claim-and-ack. Claiming a due occurrence does not
advance its next-run time. Task creation or the evolve check must first
succeed; only then does Enoch acknowledge the claim and advance the schedule.
After a crash, the same claim is returned and its idempotency key prevents a
duplicate task.

Task cron runs from an independent scheduler thread, so blocked or failed chat
polling does not delay due checks. A due cron task is inserted at the front of
the pending task queue without interrupting the running task. Each schedule may
have at most one pending, running, or paused task; an additional due occurrence
remains claimed until that task reaches a terminal state.

Task cron intervals use fixed-rate UTC targets. `next_run_at` is the next
anchored target, `last_scheduled_at` is the target represented by the latest
admitted task, and `last_run_at` is when that task was handed to the queue.
After daemon downtime, all missed targets are coalesced into one task that is
admitted as soon as the scheduler starts. The following target is the first
anchored interval strictly in the future, preventing acknowledgement-time
drift and unbounded catch-up work.

## State safety

All replace-style JSON writes use a unique sibling temporary file, `fsync`, and
an atomic rename. Read-modify-write stores use shared thread and process locks.
Existing malformed JSON or invalid top-level structures raise
`StateCorruptionError`; Enoch preserves the original file instead of silently
replacing it with empty state.

The core test runner redirects resident-checkout state into an isolated
temporary directory. Tests using their own temporary repositories continue to
use those repositories' local `.enoch` state.
