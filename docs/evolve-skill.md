# Evolve Skill Design

Enoch should have an `evolve` skill for self-evolution. This skill is not a generic background task runner. It is an evolution selection loop: Enoch collects possible improvements from several sources, ranks them against her current direction, and chooses the best next small step.

## Purpose

Enoch's code is part of Enoch. Self-evolution means changing that code body deliberately, with memory, lineage, tests, and human review. The evolve skill should help Enoch grow without turning autonomy into random self-modification.

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

## Candidate Sources

Self-evolution candidates can come from several sources.

### theme

The current self-evolution theme is the main direction of growth.

Examples:

- become better at autonomous work recovery
- improve Telegram work UX
- make inheritance safer and cleaner
- reduce human coordination burden

The theme acts as evolutionary pressure. Without a theme, auto-evolve can drift into random optimization.

### backlog

Enoch can inspect backlog items and select the most important candidate that also fits the current theme.

Backlog items are human-visible deferred work, so they are strong candidates when they are relevant and actionable.

### feedback

Human feedback is a major source of evolution.

Feedback includes corrections, frustrations, repeated requests, UX complaints, and explicit preferences. Enoch should treat feedback as a signal for where her body or behavior needs to improve.

### experience

Enoch writes every tracked task transition to the append-only
`.enoch/task_events.jsonl`. Events cover `created`, `queued`, `started`,
`completed`, `failed`, `cancelled`, and `reverted`, including the request, result
summary, context source, pull requests, and changed files. Legacy
`.enoch/experience.jsonl` records remain readable.

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
`.enoch/evolve_events.jsonl`. It records `checked`, `proposed`, `selected`,
`queued`, `completed`, `failed`, `cancelled`, `skipped`, and `removed` events.
Each event links candidate provenance, decision actor, trigger, mode, theme,
score, and task id without conflating an agent-origin idea with an autonomous
scheduler decision.

Experience candidates come from failures, repeated manual steps, confusing flows, missing commands, test failures, recovery friction, and places where Enoch notices she needed human help for something she could safely automate next time.

### brainstorm

Enoch can use an LLM brainstorming pass to generate new self-improvement ideas.

Brainstorm candidates should be treated as speculative. They need ranking and risk checks before becoming approved work.

### inheritance

Enoch can inspect direct-parent changes from Seth.

If Seth gains a useful change that Enoch does not have, that can become an evolve candidate. Inheritance candidates should be filtered to direct-parent changes that are actually applicable and not already present.

### learn

Enoch can learn from other agents.

Learn candidates come from useful skills, patterns, or implementations in other published Our-Ark agents. Learning is different from inheritance: it can come from non-parent agents and should be adapted rather than blindly copied.

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

When Enoch evolves, she should choose one candidate as the next task. Selection should rank candidates rather than follow a fixed source order.

Suggested scoring:

```text
score(candidate) =
  value_to_mission
+ alignment_with_theme
+ urgency_or_pain
+ feasibility
+ testability
+ reversibility
+ small_step_size
- risk
- scope_creep
- requires_human_decision
```

The proposed candidate should usually be:

- aligned with the current theme
- small enough to review
- testable
- reversible
- low risk
- clearly valuable to Enoch's mission

## Mode Behavior

### disabled

- do not collect candidates
- do not rank candidates
- do not run self-evolution work

### co-evolve

- collect candidates
- rank candidates
- show the top candidate and rationale
- wait for the human to approve or redirect before running work

### auto-evolve

- collect candidates
- rank candidates
- select one bounded candidate
- queue or run the work
- test the change
- open a pull request for human review
- stop if `/stop` is used

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
- GitHub settings
- daemon configuration
- permission boundaries
- merge behavior
- destructive operations
- large architectural rewrites

Enoch should prefer small pull requests with a clear rationale and test plan.

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
/evolve remove <id>
/evolve schedule <text>
```

`/feedback` shows the human feedback signals available to evolution. `/experience`
shows candidates derived from Enoch's task history, recurring workflows, and
successful skill work. `/propose` refreshes all six sources, ranks new and failed
candidates, and presents the strongest actionable candidate without selecting or
running it. Failed candidates remain available for `/evolve retry <id>`, which
creates a new linked task without rewriting the failed task's history. When no
actionable candidate exists and a theme is set, `/propose` runs one bounded
fallback brainstorm and ranks again. Automatic fallback attempts have a per-theme
24-hour cooldown; explicit `/evolve brainstorm` bypasses that cooldown. Scheduled
co-evolve and auto-evolve checks use the same proposal selection, so running
candidates are not proposed or queued again, failed candidates require an
explicit human retry, and empty scheduled proposals use the same fallback
brainstorm policy.

## Principle

Auto-evolve is not "do whatever." It is candidate selection under a theme, with bounded execution, tests, and human review.
