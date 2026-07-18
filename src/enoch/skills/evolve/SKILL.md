# Evolve

## Purpose

Use this skill when Enoch should reason about her next small self-evolution step.

The evolve skill is a selection loop, not a generic task runner. It collects possible improvements, ranks them against the current evolution theme, and proposes the best bounded next step.

## Modes

- `disabled`: do not collect, rank, or propose self-evolution candidates.
- `co-evolve`: collect and rank candidates, then wait for human approval before changing code.
- `auto-evolve`: select bounded candidates under guardrails; do not merge self-evolution changes.

The default mode is `co-evolve`.

## Candidate Sources

Enoch collects exactly six semantic candidate sources:

- **backlog** from `.enoch/backlog.json`;
- **feedback** extracted conservatively from local conversation logs, including corrections, preferences, complaints, and repeated requests;
- **experience** from the durable task experience journal, failed tasks, recurring workflows, repeated successful user workflows, and successful skill-work artifacts;
- **inheritance** from direct-parent changes in `.agent/lineage_inbox.json`;
- **learning** from skills explicitly inspected with `/learn`, recorded in `.enoch/learning/peers.jsonl`; and
- **brainstorming** from bounded, structured LLM ideas generated under the current mission and evolution theme.

The theme is ranking pressure, not a seventh source. `/evolve brainstorm` requires a non-empty theme, asks the reasoning engine for a small JSON list, validates the result, and persists only structured candidates in `.enoch/evolve_brainstorms.jsonl`.

`/propose` first refreshes and ranks all six sources. If no active candidate
exists and a theme is set, it runs one bounded fallback brainstorm and ranks
again. Automatic fallback attempts have a per-theme 24-hour cooldown persisted
in `.enoch/evolve_brainstorm_fallback.json`; explicit `/evolve brainstorm`
remains available during the cooldown.

Candidates are persisted in `.enoch/evolve_candidates.json` so Enoch can remember whether a candidate is available, running, done, failed, cancelled, or removed. Normal candidate views hide done, failed, cancelled, and removed candidates.

## Scheduler

The evolve scheduler stores its frequency and next run time in `.enoch/evolve.json`.

It can run on a fixed interval, once per day at a local HH:MM time, or a cron-style daily expression like `30 9 * * *`.

When the scheduler is due:

- `disabled` mode advances the schedule and takes no action.
- `co-evolve` mode runs the same proposal selection as `/propose` and sends that proposal to the locked Telegram chat.
- `auto-evolve` mode runs the same proposal selection as `/propose` and turns its top new candidate into a queued task for review-oriented implementation.

Proposal selection only considers candidates whose status is `candidate`. Running candidates are not proposed again.
Scheduled co-evolve and auto-evolve checks use the same empty-candidate fallback
and cooldown as `/propose`.

Every tracked task writes append-only lifecycle events to `.enoch/task_events.jsonl`.
Events include `created`, `queued`, `started`, `completed`, `failed`, `cancelled`,
and `reverted`. Each event keeps three independent provenance dimensions:

- `source` is one of `backlog`, `feedback`, `experience`, `inheritance`, `learning`, `brainstorming`, `task`, or `chat-task`;
- `initiated_by` is `human` or `agent` and remains stable for the task; and
- `event_actor` is `human`, `agent`, or `system`, identifying who caused that lifecycle transition.

Cron, recovery, backlog promotion, approval, and evolve scheduling are triggers,
not extra sources. Legacy `.enoch/experience.jsonl` records remain readable.
Only actionable failures, started cancellations, repeated successful user
workflows, recurring jobs, and skill-work artifacts become evolve candidates.

## Guardrails

Evolve candidates should be small, testable, reversible, and aligned with Enoch's mission and current theme.

Enoch must require human direction before changing identity, mission, secrets, permission boundaries, GitHub settings, daemon configuration, merge behavior, destructive operations, or large architecture.

## Commands

- `/feedback`
- `/experience`
- `/propose`
- `/evolve`
- `/evolve mode <mode>`
- `/evolve theme [text]`
- `/evolve brainstorm`
- `/evolve list`
- `/evolve list all`
- `/evolve approve <id>`
- `/evolve remove <id>`
- `/evolve schedule <text>`
- `/evolve schedule once a day`
- `/evolve schedule every <interval>`
- `/evolve schedule daily HH:MM`
- `/evolve schedule cron '30 9 * * *'`
- `/evolve schedule off`
