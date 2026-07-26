# Evolve Skill Design

Enoch's `evolve` skill is a governed self-evolution selection loop, not a
generic background task runner. Enoch collects raw possible improvements from
several sources, deterministically pre-ranks a bounded pool, and asks the
reasoning engine to recommend the best next small step semantically against her
mission and current evolution theme. The fixed score is input ordering and an
explicit fallback, not the normal recommendation.

## Purpose

Enoch's code is part of Enoch. Self-evolution means changing that code body
deliberately, with memory, lineage, tests, and human review. The evolve skill
helps Enoch grow without turning autonomy into random self-modification.

## Modes

### disabled

Enoch does not initiate self-evolution.

She can still chat, run explicitly requested work, inherit, learn, and update mission when asked. She does not collect, rank, or run self-evolution candidates as her own initiative.

### co-evolve

Enoch may notice opportunities to improve herself and propose evolution candidates, but she waits for human direction before changing code.

This is the recommended default mode. It gives Enoch agency in noticing and reasoning, while the human owns direction and approval.

### auto-evolve

Enoch may initiate bounded self-evolution on her own body.

She can select a low-risk, high-value candidate, queue or run the work, test it, and open a pull request for human review. She should not merge her own evolution changes.

## Evolution Direction

### theme

The current self-evolution theme is the main direction of growth.

Examples:

- become better at autonomous work recovery
- improve Telegram work UX
- make inheritance safer and cleaner
- reduce human coordination burden

The theme acts as evolutionary pressure. It guides semantic curation and
deterministic pre-ranking but is not itself a candidate source. Without a
theme, auto-evolve can drift into random optimization.

## Candidate Sources

Self-evolution candidates can come from six sources.

### backlog

Enoch can inspect backlog items and select the most important candidate that also fits the current theme.

Backlog items are human-visible deferred work, so they are strong candidates when they are relevant and actionable.

### feedback

Human feedback is a major source of evolution.

Feedback includes corrections, frustrations, repeated requests, UX complaints, and explicit preferences. Enoch should treat feedback as a signal for where her body or behavior needs to improve.

### experience

Enoch writes every tracked task transition to the append-only
`.enoch/artifacts/task_events.jsonl`. Events cover `created`, `queued`, `started`,
`retrying`, `paused`, `resumed`, `completed`, `failed`, `cancelled`, `regressed`,
`reverted`, and `forward-fixed`, including the request, result summary, context
source, pull requests, and changed files. Legacy `.enoch/experience.jsonl`
records remain readable.

Task provenance retains three general lifecycle dimensions:

- `source`: `backlog`, `feedback`, `experience`, `inheritance`, `learning`,
  `brainstorming`, `task`, or `chat-task`;
- `initiated_by`: `human` or `agent`; and
- `event_actor`: `human`, `agent`, or `system`.

Schedulers, cron, recovery, approvals, and promotions are recorded as triggers,
not additional sources. The journal keeps successful work visible without turning
every success into an evolve candidate.

Evolve-linked candidates and tasks add explicit provenance fields:

- `evidence_source`: which of the six evolve sources supplied the evidence;
- `signal_actor`: who produced the original signal;
- `candidate_actor`: who turned that signal into a candidate;
- `approval_actor`: who approved a particular execution;
- `parent_candidate_id`: the upstream candidate, when one caused another;
- `source_task_id`: the task whose outcome supplied candidate evidence; and
- task `parent_task_id`: the prior task retried by this execution.

`initiated_by` remains readable for legacy data and ordinary task-origin
statistics, but it does not stand in for these distinct evolve actors.

Evolution decisions have a separate append-only journal at
`.enoch/artifacts/evolve_events.jsonl`. It records `checked`, `proposed`, `selected`,
`queued`, `completed`, `failed`, `cancelled`, `skipped`, `removed`, and
`no-action` decision events, plus `promoted` and `adopted` governance events.
Each event links candidate provenance, decision actor, trigger, mode, theme,
score, proposal id, and task id without conflating an agent-origin idea with an
autonomous scheduler decision.

Experience candidates come from failures, repeated manual steps, confusing flows, missing commands, test failures, recovery friction, and places where Enoch notices she needed human help for something she could safely automate next time.

### brainstorm

Enoch can use an LLM brainstorming pass to generate new self-improvement ideas.

Brainstorm candidates should be treated as speculative. They need ranking and risk checks before becoming approved work.

### inheritance

Enoch can inspect direct-parent changes from Seth.

If Seth gains a useful change that Enoch does not have, that can become an evolve candidate. Inheritance candidates should be filtered to direct-parent changes that are actually applicable and not already present.

### learn

Enoch can learn from other agents.

Learn candidates come from useful skills, patterns, or implementations in other published OurArk agents. Learning is different from inheritance: it can come from non-parent agents and should be adapted rather than blindly copied.

## Candidate Shape

Each candidate should be stored with enough context to explain why it exists and how to evaluate it.

```yaml
id: evo_001
source: backlog|feedback|experience|brainstorming|inheritance|learning
evidence_source: feedback
signal_actor: human|agent|system
candidate_actor: human|agent|system
parent_candidate_id: optional upstream candidate id
source_task_id: optional evidence task id
title: Short candidate title
rationale: Why this candidate matters
proposed_change: What Enoch would change
expected_benefit: What improves if this lands
risk: What could go wrong
test_plan: How Enoch will verify the change
requires_human_approval: true
status: candidate|running|done|failed|cancelled|regressed|reverted|forward-fixed|removed
```

## Selection

The six source adapters discover raw candidates and preserve their provenance.
They do not make the final recommendation. `/propose` deterministically
pre-ranks and bounds the pool, then gives the reasoning engine the candidate
fields, mission, theme, provenance, and privacy-cleaned completion evidence.
The reasoning engine may recommend at most one existing candidate, suggest
bounded new candidates, or propose removal classifications for human review.

The semantic recommendation should usually be:

- aligned with the current theme
- small enough to review
- testable
- reversible
- low risk
- clearly valuable to Enoch's mission

If semantic curation is unavailable, times out, or returns invalid output,
Enoch may use the highest safe deterministic pre-ranked candidate only as an
explicitly labelled fallback. Neither an LLM recommendation nor a fallback
changes candidate state: a human still approves execution or removal.

## Mode Behavior

### disabled

- do not collect candidates
- do not rank candidates
- do not run self-evolution work

### co-evolve

- collect candidates
- pre-rank a bounded candidate pool
- ask the reasoning engine for a semantic recommendation and rationale
- wait for the human to approve or redirect before running work

### auto-evolve

- collect candidates
- pre-rank a bounded candidate pool
- schedule the same semantic proposal used by `/propose`
- wait for explicit human approval before queueing or removing a candidate
- never merge self-evolution work

## Guardrails

Auto-evolution should be bounded.

Enoch may:

- change her own code body
- add or improve tests
- improve commands, docs, skills, memory handling, work queues, and recovery flows
- open pull requests for review

Enoch should require human direction before changing:

- mission
- identity
- secrets or tokens
- forge or remote-repository settings
- host-service configuration
- permission boundaries
- merge behavior
- destructive operations
- large architectural rewrites

Enoch should prefer small pull requests with a clear rationale and test plan.

## Governed Lifecycle

Candidate completion means the agent finished its task and published reviewable
work. It does not mean the change became authoritative or entered the running
instance.

- `promoted` means a human merged the candidate PR and Enoch verified its merge
  revision is contained in the VCS provider's trusted authoritative branch.
- `adopted` means the instance updated to a version containing that promotion,
  passed doctor, restarted, and confirmed the running version.

`/evolve reconcile <id>` records promotion evidence for a completed candidate.
Historical reconciliation uses `/evolve reconcile <id> backfill` and writes
`recording_mode: backfill`; it never presents reconstructed evidence as a
realtime observation.

## Command Surface

Source visibility:

```text
/feedback
/experience
```

Candidate selection and control:

```text
/propose
/evolve
/evolve mode <mode>
/evolve theme [text]
/evolve brainstorm
/evolve list
/evolve approve <id>
/evolve retry <id>
/evolve reconcile <id> [backfill]
/evolve remove <id>
/evolve schedule <text>
```

`/feedback` shows the human feedback signals available to evolution. `/experience`
shows candidates derived from Enoch's task history, recurring workflows, and
successful skill work. `/propose` refreshes all six sources, pre-ranks new and
failed candidates into a bounded pool, and asks the reasoning engine for a
semantic recommendation without selecting or running it. Failed candidates
remain available for `/evolve retry <id>`, which
creates a new linked task without rewriting the failed task's history. When no
actionable candidate exists and a theme is set, `/propose` runs one bounded
fallback brainstorm and curates again. Automatic fallback attempts have a per-theme
24-hour cooldown; explicit `/evolve brainstorm` bypasses that cooldown. Scheduled
co-evolve and auto-evolve checks use the same proposal selection, so running
candidates are not proposed or queued again, failed candidates require an
explicit human retry, and empty scheduled proposals use the same fallback
brainstorm policy.

## Principle

Auto-evolve is not "do whatever." It is semantic recommendation under a theme,
with bounded execution, tests, and human review.
