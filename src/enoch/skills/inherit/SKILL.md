# Inherit

## Purpose

Use this skill when Enoch should inherit changes from her direct parent.

Inheritance is lineage-level parent adoption. It is separate from learning:

- `inherit` discovers and adapts direct-parent candidate changes;
- `learn` adapts lessons or non-lineage learning inputs into Enoch's own body;
- `work` lets Enoch run queue, backlog, cron, and skill-only automatic learning artifacts.

## Operations

Enoch uses this skill through ancestor commands:

- `/ancestors`
- `/inherit`
- `/inherit inbox`
- `/inherit inspect <change_id>`
- `/inherit <change_id>`
- `/inherit ignore <change_id>`

## Boundary

Inheritance only flows through Enoch's direct parent. If Enoch's parent has not inherited a grandparent change, Enoch should not inherit it directly.

`/inherit` discovers direct-parent PRs and commits, stores them in private state,
and queues fresh Codex assessment sessions in a durable background worker. The
Telegram handler replies immediately and remains available while assessment
runs. Progress and the completed inbox are delivered as durable notifications.
Assessment is advisory and never starts implementation work.

Discovery advances a durable per-parent commit cursor only after a complete
scan. It paginates up to `lineage.scan_limit` (default `500`) and reports an
error without moving the cursor if parent activity exceeds that bound. Codex
assessment runs in fresh batches controlled by
`lineage.assessment_batch_size` (default `10`). Failed assessment never removes
the discovered change and is retried by a later `/inherit`.

The initial cursor starts at `parent.commit_at_birth` when lineage metadata
provides it. For older descendants without that provenance, the first scan
intentionally baselines from the newest 20 parent commits rather than treating
the parent's entire history as new.

An interrupted running assessment is reclaimed by the next daemon epoch and
continues with only records that still need assessment. `/inherit inbox` reads
the stored inbox without contacting the forge or Codex and without advancing
the scan cursor.

`/inherit inspect <change_id>` displays the durable assessment and adds it to the normal conversation context for follow-up questions. `/inherit <change_id>` is explicit human authorization to queue one adaptation through the standard task, worktree, validation, commit, push, and PR workflow. `/inherit ignore <change_id>` dismisses a pending change without deleting its history.

Inheritance changes are governed by their own inbox and lifecycle. They are not Evolution evidence or Evolution candidates.

Teaching is implicit: Enoch's descendants can inspect Enoch's skills and lineage changes, and Enoch's work skill can emit inheritable skill artifacts without exposing a user-facing `/teach` command.
