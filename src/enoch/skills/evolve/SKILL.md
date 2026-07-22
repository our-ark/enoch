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

`/propose` first refreshes and ranks all six sources. If no actionable candidate
exists and a theme is set, it runs one bounded fallback brainstorm and ranks
again. Automatic fallback attempts have a per-theme 24-hour cooldown persisted
in `.enoch/evolve_brainstorm_fallback.json`; explicit `/evolve brainstorm`
remains available during the cooldown.

Candidates are persisted in `.enoch/evolve_candidates.json` so Enoch can remember whether a candidate is available, running, done, failed, cancelled, or removed. Normal candidate views retain failed candidates as retryable and hide done, cancelled, and removed candidates.

Evolution decisions are appended to `.enoch/evolve_events.jsonl`. The funnel
records checks, proposals, selections, queueing, terminal outcomes, skips, and
removals. Candidate provenance separates `evidence_source`, `signal_actor`, and
`candidate_actor`; execution records `approval_actor`, while `event_actor` and
`trigger` identify who caused each lifecycle decision. `parent_candidate_id`
and `source_task_id` preserve causal links for candidates learned from prior
work, and task `parent_task_id` remains the retry relationship.

## Scheduler

The evolve scheduler stores its frequency and next run time in `.enoch/evolve.json`.

It can run on a fixed interval, once per day at a local HH:MM time, or a cron-style daily expression like `30 9 * * *`.

When the scheduler is due:

- `disabled` mode advances the schedule and takes no action.
- `co-evolve` mode runs the same proposal selection as `/propose` and sends that proposal to the locked chat-provider conversation.
- `auto-evolve` mode runs the same proposal selection as `/propose` and turns
  its top new candidate into a queued task for review-oriented implementation.
  Failed candidates are proposed for explicit human retry instead of being
  retried automatically.

Proposal selection considers candidates whose status is `candidate` or `failed`. Running candidates are not proposed again.
Scheduled co-evolve and auto-evolve checks use the same empty-candidate fallback
and cooldown as `/propose`.

Each top candidate returned by `/propose` or the evolve scheduler receives a
unique `proposal_id` in `.enoch/evolve_events.jsonl`. Proposal dispositions are
tracked independently as `selected`, `removed`, or `no-action`; an unresolved
proposal remains pending, and a newer proposal closes the previous pending one
as `no-action` with reason `superseded-by-new-proposal`. Queued task outcomes
retain the same `proposal_id`, including completion, failure, cancellation, and
regression resolution. `/experience` reports proposal disposition, acceptance
rate, source and trigger distribution, and selected proposal outcomes.

Task completion is not promotion or adoption. `/evolve reconcile <id>` verifies
that a completed candidate's PR was merged by a human and that its merge commit
is contained in trusted `origin/main`, then records a `promoted` event.
`/evolve reconcile <id> backfill` performs the same verification while marking
the evidence as historical backfill. After `/update` passes doctor, Enoch stages
eligible promotions and records `adopted` only when the restarted daemon confirms
it is running the verified version.

Every tracked task writes append-only lifecycle events to `.enoch/task_events.jsonl`.
Events include `created`, `queued`, `started`, `completed`, `failed`, `cancelled`,
`paused`, `resumed`, `regressed`, `reverted`, and `forward-fixed`. Codex access
interruptions are recorded as `paused` and `/resume` transitions without
closing the task or its linked evolve proposal. A regression is recorded after
a task was completed; `reverted` and `forward-fixed` are separate resolution
events so regression counts remain durable. Enoch owns this bookkeeping:
the agent emits an internal structured signal when evidence identifies the
original task, and the Enoch application records it after validating task state.
A human can report a problem naturally and does not maintain lifecycle status
with `/task` commands. Task events retain the original source and initiator,
and evolve-linked tasks add explicit provenance:

- `source` is one of `backlog`, `feedback`, `experience`, `inheritance`, `learning`, `brainstorming`, `task`, or `chat-task`;
- `initiated_by` is `human` or `agent` and remains stable for the task; and
- `event_actor` is `human`, `agent`, or `system`, identifying who caused that lifecycle transition.
- `evidence_source`, `signal_actor`, and `candidate_actor` describe why and by
  whom the candidate was created;
- `approval_actor` identifies who approved this execution; and
- `parent_candidate_id`, `source_task_id`, and `parent_task_id` preserve
  candidate causality, evidence-task causality, and retry causality respectively.

Cron, recovery, backlog promotion, approval, and evolve scheduling are triggers,
not extra sources. Legacy `.enoch/experience.jsonl` records remain readable.
Only actionable failures, started cancellations, repeated successful user
workflows, recurring jobs, and skill-work artifacts become evolve candidates.

## Guardrails

Evolve candidates should be small, testable, reversible, and aligned with Enoch's mission and current theme.

Enoch must require human direction before changing identity, mission, secrets, permission boundaries, forge settings, daemon configuration, merge behavior, destructive operations, or large architecture.

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
- `/evolve retry <id>`
- `/evolve reconcile <id> [backfill]`
- `/evolve remove <id>`
- `/evolve schedule <text>`
- `/evolve schedule once a day`
- `/evolve schedule every <interval>`
- `/evolve schedule daily HH:MM`
- `/evolve schedule cron '30 9 * * *'`
- `/evolve schedule off`
