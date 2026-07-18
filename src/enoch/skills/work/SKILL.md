# Work

## Purpose

Use this skill when Enoch should manage persistent work instead of treating every request as a single foreground chat turn.

The work skill covers three core execution modes:

- queue: `/task` FIFO background jobs;
- backlog: `/backlog` deferred idle-time work with priority;
- cron: `/cron` recurring scheduled jobs;

It also covers:

- single-message task status updates in Telegram;
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
2. Keep task execution non-blocking for Telegram conversation.
3. Update one Telegram status message with queued, running, paused, completed,
   failed, elapsed time, latest update, and PR URLs.
4. Run queued work through the same authorized repository workflow as foreground `/do` work.
5. Promote backlog items only when the task queue is idle.
6. Claim due cron jobs atomically before enqueueing them, so one due event creates one task.
7. When Codex authentication, quota, or rate limits are unavailable, move the
   active task to `paused`, stop the worker before it consumes later tasks, and
   warn the human. `/resume` moves paused tasks back to the front with the same
   ids and context after access is available again.

## Inheritance

This is Enoch's explicit work capability. Descendant agents can inherit it when they need autonomous background work, scheduled maintenance, or skill-level learning artifacts.

Implicit teaching is part of the work model: Enoch does not expose `/teach`, but successful skill changes can produce inheritable skill artifacts automatically.
