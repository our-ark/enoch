# Work

## Purpose

Use this skill when Enoch should manage persistent work instead of treating every request as a single foreground chat turn.

The work skill covers three core execution modes:

- queue: `/task` FIFO background jobs;
- backlog: `/backlog` deferred idle-time work with priority;
- cron: `/cron` recurring scheduled jobs;

It also covers:

- single-message task status updates through the configured chat provider;
- automatic skill-only learning artifacts after successful work.

## Boundary

Worker state is local runtime state under `.enoch/`:

- `.enoch/task_queue.json`
- `.enoch/backlog.json`
- `.enoch/cron.json`
- `.enoch/learning/`

Do not treat every successful job as inheritable. Enoch records an inheritable learning artifact only when completed work changes a skill package under `src/<agent>/skills/<skill-name>/`.

## Operation

When work is queued:

1. Preserve the request and any conversation context snapshot.
2. Keep task execution non-blocking for the active chat conversation.
3. Update one chat status message with queued, running, paused, completed,
   failed, elapsed time, latest update, and PR URLs.
4. Run queued work through the same authorized repository workflow as foreground `/do` work.
5. Promote backlog items only when the task queue is idle.
6. Claim due cron jobs atomically before enqueueing them, so one due event creates one task.
7. When Codex authentication, quota, or rate limits are unavailable, move the
   active task to `paused`, stop the worker before it consumes later tasks, and
   warn the human. `/resume` moves paused tasks back to the front with the same
   ids and context after access is available again.
8. Keep the agent-instance branch as the resident control worktree. Give every
   code task its own linked worktree and branch from the latest available
   `origin/main` or local `main`, and run Codex, tests, commits, pushes, and PR
   creation there. Keep `.enoch` queue, memory, and event state in the resident
   worktree. Remove successful task worktrees after handoff; preserve failed or
   paused worktrees for inspection and recovery.
9. `/task retry <id>` retries only a failed task by creating a new task with a
   new id and `parent_task_id`; never rewrite the original failure. Preserve the
   request, context, source, provenance, and any recoverable task
   worktree/branch. Before new execution, reconcile recorded and logged PR
   results with the configured forge; reuse a validated open or merged PR instead of
   duplicating work. If a retry fails, retry that latest failed task so the
   causal chain remains linear.
10. Give each running task a worker lease. Recovery must not requeue a task while
    its owner process is alive, and only the lease owner may publish a terminal
    task transition or final status message.
11. Classify failures before deciding whether to retry. Automatically retry only
    explicit transient failures such as network interruption, rate limiting, or
    temporary upstream unavailability, with bounded backoff and at most three
    attempts. Treat dirty worktrees, validation failures, task timeouts,
    permission or configuration errors, and unknown failures as non-retryable.
    Record attempt, failure code, failure class, and retry disposition in task
    events. Keep `/task retry <id>` as the explicit human override.
12. `/task resume <id|all>` resumes only paused tasks without changing their
    ids or causal history. `/resume` remains the system-level alias for
    `/task resume all`.

## Inheritance

This is Enoch's explicit work capability. Descendant agents can inherit it when they need autonomous background work, scheduled maintenance, or skill-level learning artifacts.

Implicit teaching is part of the work model: Enoch does not expose `/teach`, but successful skill changes can produce inheritable skill artifacts automatically.
