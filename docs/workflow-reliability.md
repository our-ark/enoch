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

## Task publication

Task results use `WorkOutcome`, separating status, failure code, retryability,
artifacts, and completed stages from chat presentation text. Publication
persists `validated`, `committed`, `pushed`, and `pr_opened` stages.

If push, PR creation, or cleanup fails, the task retains its worktree, branch,
commit, and last completed stage. Automatic retry resumes at that boundary
instead of running the coding agent again. GitHub publication also reconciles
an already-created open PR after an ambiguous `gh pr create` failure.

## Scheduled occurrences

Cron and evolve schedules use claim-and-ack. Claiming a due occurrence does not
advance its next-run time. Task creation or the evolve check must first
succeed; only then does Enoch acknowledge the claim and advance the schedule.
After a crash, the same claim is returned and its idempotency key prevents a
duplicate task.

## State safety

All replace-style JSON writes use a unique sibling temporary file, `fsync`, and
an atomic rename. Read-modify-write stores use shared thread and process locks.
Existing malformed JSON or invalid top-level structures raise
`StateCorruptionError`; Enoch preserves the original file instead of silently
replacing it with empty state.

The core test runner redirects resident-checkout state into an isolated
temporary directory. Tests using their own temporary repositories continue to
use those repositories' local `.enoch` state.
