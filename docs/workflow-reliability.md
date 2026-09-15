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

Before publication, a failed code-health or build-environment doctor check can
return to the runtime for up to two focused repair turns. The task keeps its
workspace, session, original deadline and cancellation controls; repair turns
do not reset the task timeout. Each turn receives the failed check commands,
diagnostics and bounded output, then the framework runs the complete doctor
again. Only a passing result proceeds to publication. Operational failures,
such as missing authentication or state-storage problems, stop this repair
loop; runtime quota loss preserves the normal paused-task behavior.

Instances can set `task.validation_repair_attempts` in their private config to
an integer from 0 to 5 (default 2); 0 disables automatic validation repair.
Exhaustion leaves the task failed with its draft and final diagnostics intact.
Manual `/task retry <id>` also supplies the previous failed task's diagnosis
and result to the runtime, while preserving the original request and context.
Slack uses `.task retry <id>` with `!` retained as a fallback prefix.

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

### Daily cron and lifecycle controls

The examples below use `.` as the configured chat command prefix. Providers
using `/` accept the same commands as `/cron` and `/help cron`.

```text
.cron daily 18:00 America/Los_Angeles summarize today's work
.cron every 2h check queued work
.cron
.cron show 1
.cron pause 1
.cron resume 1
.cron run-now 1
.cron cancel 1
.help cron
```

`daily` requires a 24-hour `HH:MM` time, a resolvable IANA timezone, and a
request. It follows the local calendar, including daylight-saving changes;
`every 1d` continues to mean a fixed 86,400-second interval. Both schedules use
`enoch.schedules` for occurrence calculations. A repeated fall-back time uses
its first occurrence. A skipped spring-forward time runs at the first existing
instant after the jump. Each intended local date contributes one occurrence;
when a timezone gap crosses midnight, execution can fall on the following date.

Creation, listing, and `show` display the next target in both the declared local
timezone and UTC. For example, with the clock frozen at 2026-09-15 19:00 UTC,
the daily example next runs at 2026-09-15 18:00 PDT / 2026-09-16 01:00 UTC.
After the November DST transition it remains at 18:00 local, now 02:00 UTC on
the following day. These values are examples, not installation defaults.

After downtime, daily jobs retain their missed target until one catch-up task
is admitted, then advance to the next future local target. `pause` stops new
admission while preserving the target and any existing claim. `resume` retains
that target, so missed runs coalesce into one catch-up. Already queued or
running tasks continue; task controls manage those separately. Pausing clears
an unclaimed run-now request. Cancelled jobs remain visible in history and
cannot be resumed.

`run-now` requires an active job and requests one occurrence without moving the
regular target. Requests coalesce with a pending claim or run-now request; a
regular occurrence already due takes priority and satisfies both. The last 64
run-now receipt keys are retained to deduplicate retries, including retries
after acknowledgement or pause. Durable claim IDs also prevent duplicate task
creation if the daemon crashes between enqueue and acknowledgement. Outstanding
pending, running, or paused tasks continue to prevent overlap.

Jobs belong to the private instance and capture their chat destination and
conversation context at creation. Cron listing and controls are scoped to that
chat, and queued tasks and their results always use the captured destination.
A missing binding is an explicit error; Enoch never substitutes the current or
default chat. A new instance starts without jobs.

Cron state schema 5 adds cadence, daily time, timezone, and lifecycle/occurrence
metadata. Records without cadence are read as interval jobs, preserving their
IDs, context, bindings, history, and in-flight claims. The private-state migration
command upgrades existing records, and the next successful cron mutation also
writes schema 5. Invalid records or malformed state raise `StateCorruptionError`
and preserve the original file rather than resetting or dropping jobs.

Declarative extension schedules share that scheduler thread and claim/ack
discipline. Their identities, occurrence claims, and task idempotency keys are
scoped by extension. Interval schedules retain fixed-rate anchors; daily
schedules calculate the next local calendar target through their declared IANA
timezone. An outstanding task prevents overlap, and missed targets coalesce.
Pause or extension removal prevents new claims while retaining an in-flight
claim for restart reconciliation. Capability denial and other pre-enqueue
failures are recorded on schedule status and in the system event log before the
cadence advances.

## State safety

All replace-style JSON writes use a unique sibling temporary file, `fsync`, and
an atomic rename. Read-modify-write stores use shared thread and process locks.
Existing malformed JSON or invalid top-level structures raise
`StateCorruptionError`; Enoch preserves the original file instead of silently
replacing it with empty state.

The core test runner redirects resident-checkout state into an isolated
temporary directory. Tests using their own temporary repositories continue to
use those repositories' local `.enoch` state.
