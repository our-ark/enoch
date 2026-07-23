# Evolve

## Purpose

Use this skill when Enoch should reason about her next small self-evolution step.

The evolve skill is a selection loop, not a generic task runner. It collects possible improvements, deterministically pre-ranks them, and asks the reasoning engine to semantically curate a bounded candidate pool against the current mission and evolution theme.

## Modes

- `disabled`: do not collect, rank, or propose self-evolution candidates.
- `co-evolve`: collect and rank candidates, then wait for human approval before changing code.
- `auto-evolve`: schedule semantic proposal checks under guardrails; still require human approval before queueing or removing candidates, and never merge self-evolution changes.

The default mode is `co-evolve`.

## Candidate Sources

Enoch collects exactly six semantic candidate sources:

- **backlog** from `.enoch/backlog.json`;
- **feedback** extracted conservatively from local conversation logs, including corrections, preferences, complaints, and repeated requests;
- **experience** from the durable task experience journal, failed tasks, recurring workflows, repeated successful user workflows, and successful skill-work artifacts;
- **inheritance** from direct-parent changes in `.agent/lineage_inbox.json`;
- **learning** from skills explicitly inspected with `/learn`, recorded in `.enoch/learning/peers.jsonl`; and
- **brainstorming** from bounded, structured LLM ideas generated under the current mission and evolution theme.

The theme is semantic curation context and deterministic pre-ranking pressure, not a seventh source. `/evolve brainstorm` requires a non-empty theme, asks the reasoning engine for a small JSON list, validates the result, and persists only structured candidates in `.enoch/evolve_brainstorms.jsonl`.

`/propose` refreshes all six sources, applies deterministic pre-ranking, and
passes a bounded set of structured candidate fields and provenance to semantic
curation. The curator may recommend one existing ID with narrower scope, risk,
and test guidance; suggest duplicate, superseded, obsolete, already-resolved,
context-only, or not-actionable candidates for removal; and suggest up to three
new bounded candidates. New suggestions are persisted with
`brainstorming`/`agent` provenance and never masquerade as human feedback.

Semantic curation is stored separately in `.enoch/evolve_curations.jsonl`. It
references raw candidate IDs and immutable provenance instead of rewriting raw
evidence. Deterministic ranking remains only for bounded context ordering and an
explicitly labelled fallback when the reasoning engine is unavailable, times
out, returns malformed JSON, or produces no valid result. The legacy empty-pool
brainstorm fallback remains available to direct callers, while the application
proposal flow uses semantic curation for both existing and newly suggested work.

Candidates are persisted in `.enoch/evolve_candidates.json` so Enoch can remember whether a candidate is available, running, done, failed, cancelled, or removed. Normal candidate views retain failed candidates as retryable and hide done, cancelled, and removed candidates.

Evolution decisions are appended to `.enoch/evolve_events.jsonl`. The funnel
records checks, proposals, selections, queueing, terminal outcomes, skips, and
human removals. Proposal events link `curation_id` and `recommendation_kind` to
the separate curation journal. Candidate provenance separates `evidence_source`, `signal_actor`, and
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
- `auto-evolve` mode runs the same proposal selection as `/propose`, sends it to
  the locked conversation, and waits for explicit human approval. It does not
  queue new candidates, retry failures, or apply remove suggestions automatically.

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
is contained in the VCS provider's trusted authoritative branch, then records a
`promoted` event.
`/evolve reconcile <id> backfill` performs the same verification while marking
the evidence as historical backfill. After `/update` passes doctor, Enoch stages
eligible promotions and records `adopted` only when the restarted daemon confirms
it is running the verified version.

Every tracked task writes append-only lifecycle events to `.enoch/task_events.jsonl`.
Events include `created`, `queued`, `started`, `completed`, `failed`, `cancelled`,
`paused`, `resumed`, `regressed`, `reverted`, and `forward-fixed`. Agent runtime access
interruptions are recorded as `paused` and `/task resume` transitions without
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

All curator output must pass strict JSON schema, known-ID, bounded-field,
test-plan, protected-scope, and dangerous-action validation. Unknown IDs and
suggestions that change identity, mission, secrets, credentials, permissions,
access control, merge authority, deployment, forge settings, daemon
configuration, destructive behavior, or large architecture are rejected.

The reasoning engine only recommends. It cannot approve, queue, run, retry,
remove, merge, change the mission, or alter permissions. `/evolve approve`,
`/evolve retry`, and `/evolve remove` are explicit human state-change entries.
Removal records status, human actor, reason, and event while preserving the raw
candidate and provenance in the candidate store and append-only journals.

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
- `/evolve remove <id> [reason]`
- `/evolve schedule <text>`
- `/evolve schedule once a day`
- `/evolve schedule every <interval>`
- `/evolve schedule daily HH:MM`
- `/evolve schedule cron '30 9 * * *'`
- `/evolve schedule off`
