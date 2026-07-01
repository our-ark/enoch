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
- `/inherit show`
- `/inherit <change_id>`
- `/inherit all`
- `/inherit ignore <candidate>`

## Boundary

Inheritance only flows through Enoch's direct parent. If Enoch's parent has not inherited a grandparent change, Enoch should not inherit it directly.

`/inherit show` builds and maintains lineage context under `.agent/lineage_inbox.json`. `/inherit <change_id>` and `/inherit all` adapt selected direct-parent changes into Enoch's own body.

Teaching is implicit: Enoch's descendants can inspect Enoch's skills and lineage changes, and Enoch's work skill can emit inheritable skill artifacts without exposing a user-facing `/teach` command.
