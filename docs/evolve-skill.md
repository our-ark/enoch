# Evolve skill design

Enoch's evolution system is a governed selection pipeline. It separates what
happened from what should change, and separates a recommendation from authority
to modify the running software body.

```text
conversation turns                 task event histories
        |                                  |
        +---- semantic evidence scans -----+
                           |
                    durable evidence
                           |
                semantic candidate synthesis
                           |
                         candidate pool ------ peer learning
                           |
                 bounded semantic curation
                           |
                   human approve/remove
                           |
             task -> worktree -> commit -> PR
                           |
                human merge -> promotion
                           |
                  update -> adoption
```

## Modes and direction

`disabled` prevents evidence scans, candidate synthesis, and proposals.
Explicit non-evolution work remains available.

`co-evolve` lets Enoch notice and recommend improvements, while a human decides
whether to run or remove them. This is the default.

`auto-evolve` schedules the same proposal pipeline. It does not grant merge
authority and does not bypass human approval for candidate execution, retries,
or removal.

The evolution theme supplies direction to synthesis, curation, and deterministic
fallback scoring. It is not evidence and is not a candidate source.

## Why evidence is separate from candidates

An observation such as “task progress disappeared after resume” is evidence.
“Add a resumed-task progress snapshot” is one possible candidate response to
that evidence. Keeping the two separate has several benefits:

- evidence can support more than one future candidate;
- a weak signal can remain recorded without becoming work;
- the reasoning engine can combine related evidence without rewriting it;
- candidate rationale, risk, and implementation can be audited against the
  original messages or task events; and
- deleting or removing a candidate does not erase what happened.

Backlog is deliberately excluded. A backlog item already represents work the
human chose to defer; treating it as evolution evidence would duplicate the
work and confuse “requested” with “learned.” If backlog execution later exposes
friction, its task history may yield experience evidence.

## Feedback evidence

Conversation turns are stored as JSONL records with a stable unique ID. The
feedback scanner consumes unprocessed turns in batches of 20 by default. The
batch size can be set from 1 to 100.

Each scanner input record contains:

- `conversation:<record-id>`;
- the recorded timestamp;
- the exact user message; and
- Enoch's exact reply.

The scan excludes chat IDs and other unrelated record metadata. Known
credential forms—including bot tokens, bearer credentials, GitHub/OpenAI token
forms, and explicit password/secret assignments—are redacted before invoking
the runtime. Otherwise, the message text is not summarized, pattern-matched, or
rewritten.

The reasoning engine receives the batch in a fresh stateless invocation. It is
asked for possible evidence about improving Enoch, not for candidates or code
changes. Replies help resolve phrases such as “do that,” but the user's words
remain the feedback signal.

This replaces the old textual-pattern pathway. A phrase such as “this is
annoying” is not sufficient on its own, while feedback expressed without a
known keyword can still be understood in context.

## Experience evidence

The experience scanner counts distinct task IDs, not task event rows. Its
default batch contains 20 tasks. Each task snapshot includes the complete
ordered lifecycle currently present in
`.enoch/artifacts/task_events.jsonl`, including request/result summaries,
provenance, retries, failures, publication metadata, and regression resolution.

The cursor is a map:

```text
task ID -> latest scanned task-event ID
```

When a task gets a new event, its marker changes and it becomes pending again.
The next scan includes its entire updated history. Journal append order is
authoritative when events share the same timestamp; random event IDs are never
used to infer lifecycle order.

The semantic prompt asks about durable operational friction, missing
capabilities, unsafe recovery, regressions, and repeated human intervention.
It explicitly says that an ordinary request, successful completion, active
schedule, task failure, or cron definition is not evidence by itself.

This replaces the old hardcoded experience candidates. Failures, repeated
successful workflows, recurring jobs, and skill-work artifacts are no longer
automatically converted into evolution candidates. Cron-created tasks still
enter the ordinary task journal and can yield semantic evidence when their
actual histories justify it.

## Scan timing and cursor rules

The daemon checks thresholds on each event-loop pass after receiving chat
updates and enqueueing due cron work, but before the evolve scheduler and task
worker start.

An automatic threshold scan runs only when:

- evolution is not disabled;
- a conversation is locked;
- no task worker is active; and
- a source has at least its configured number of pending records.

Because pending state is derived from durable records and completed scan
journals, the first loop after restart catches up naturally.

Manual and proposal triggers are different:

- `/evolve scan [source]` forces a scan below threshold and drains every pending
  batch for the selected source.
- `/evolve propose` forces and drains both sources, synthesizes candidates from
  unlinked evidence, then curates the candidate pool.
- A scheduled evolve check uses that same proposal flow.

A valid empty array means “this batch contained no evidence” and advances the
cursor. Malformed JSON, prose around JSON, an invalid schema, unknown
references, timeout, or runtime failure records a failed scan and leaves all
inputs pending. This makes retry lossless.

## Evidence record and retention

Evidence settings live at `.enoch/evidence.json`.

Completed and failed scan attempts are appended to:

```text
.enoch/artifacts/evidence_scans.jsonl
```

Evidence state updates are appended to:

```text
.enoch/artifacts/evidence.jsonl
```

An evidence item has this conceptual shape:

```yaml
id: evidence-0123456789abcdef
source: feedback|experience
observation: What the cited records demonstrate
evidence_type: Semantic category
affected_area: Enoch subsystem or workflow
desired_outcome: Observable improvement without implementation prescription
confidence: 0.0..1.0
explicit: true|false
evidence_refs:
  - conversation:<id>
  - task:<id>
  - task-event:<id>
status: active|linked|dismissed|resolved|superseded
candidate_ids: []
created_at: timestamp
updated_at: timestamp
```

The journals are append-only. Linking evidence writes a later `linked` version
with candidate IDs; it does not remove the original row. There is currently no
evidence-delete command. Removing a candidate also does not delete its evidence.

## Evidence-to-candidate synthesis

`/evolve propose` sends all currently unlinked active evidence to a second
fresh stateless reasoning invocation. That pass also receives the mission,
theme, and bounded existing-candidate summaries so it can avoid duplication.

The generator may return an empty array or up to five candidates. Every item
must cite known evidence IDs and supply:

- title;
- rationale;
- proposed change;
- expected benefit;
- risk; and
- test plan.

One candidate cannot combine feedback and experience evidence. Output is
rejected if it references unknown evidence, changes protected scope, proposes a
dangerous action, omits a required field, or returns anything except the exact
JSON array schema.

Candidate IDs are stable hashes of source, evidence IDs, and title. Candidate
records preserve both `evidence_ids` and source record references. Once a
candidate is stored, the cited evidence gets a linked-state journal update. The
linker also repairs the narrow crash case where the candidate write succeeded
but the linkage append did not.

Legacy feedback/experience candidates that lack evidence IDs remain auditable
but are retired from the actionable pool.

## The four candidate pathways

After this redesign, the candidate pool has four source labels, but the
sources do not all enter at the same stage:

1. `feedback` — semantic evidence, then candidate synthesis.
2. `experience` — semantic evidence, then candidate synthesis.
3. `learning` — direct candidates from peer observations explicitly recorded
   through `/learn`.
4. `brainstorming` — direct bounded ideas generated under mission and theme.

The feedback and experience pathways now use the new evidence layer.
Learning and brainstorming still use direct candidate adapters. Backlog and
inheritance have both been removed as sources. Inheritance now uses its own
Codex-assessed inbox and explicit `/inherit <change_id>` task workflow.

## Candidate curation

Candidate synthesis decides what possible changes the evidence supports.
Curation is a separate third stateless reasoning invocation that decides which
bounded candidate, if any, should be recommended now.

Deterministic scores only select and order the bounded input. The curator sees
immutable provenance, mission, theme, and privacy-cleaned completion evidence.
It may:

- recommend one known candidate with scope/risk/test guidance;
- suggest up to three new bounded brainstorming candidates; or
- suggest that a human remove candidates as duplicate, superseded, obsolete,
  already resolved, context-only, or not actionable.

Unknown IDs, unsafe scope, invalid resolution evidence, or malformed output
produce an explicitly labeled deterministic fallback. Suggestions never mutate
state. Approval, retry, and removal require explicit commands.

Curations are appended to `.enoch/evolve_curations.jsonl`. Candidate state is
stored in `.enoch/evolve_candidates.json`, and lifecycle decisions are appended
to `.enoch/artifacts/evolve_events.jsonl`.

## Work, promotion, and adoption

`/evolve approve <id>` queues a normal isolated task with candidate and evidence
provenance. The task then follows Enoch's standard workspace, validation,
revision capture, and review-publication workflow.

Task completion means the worker finished and, when configured, published
reviewable work. It does not prove that the review was landed or that the
resident daemon runs it.

`/evolve reconcile <id>` verifies human-approved review landing and confirms
that its immutable revision is contained by the refreshed authoritative
revision before recording `promoted`. The event stores `review_id`,
`review_urls`, `revision_id`, `authoritative_revision_id`, and
`authoritative_name`; schema-6 Git/PR fields remain readable. The `backfill`
form marks historical reconstruction explicitly. After `/update`, doctor,
restart, and version confirmation, the change can be recorded as `adopted`.

## Command surface

```text
/evolve
/evolve evidence [feedback|experience|all]
/evolve scan [feedback|experience|all]
/evolve candidates [all]
/evolve propose
/evolve brainstorm [theme]
/evolve approve <id>
/evolve retry <id>
/evolve reconcile <id> [backfill]
/evolve remove <id> [reason]
/evolve config
/evolve config mode <disabled|co-evolve|auto-evolve>
/evolve config theme <text>
/evolve config feedback-batch <1-100>
/evolve config experience-batch <1-100>
/evolve config schedule <text>
```

`/evolve` is read-only: it does not scan or refresh candidates. `/help evolve`
shows the canonical surface.

The removed top-level aliases are `/feedback`, `/experience`, and `/propose`.
The removed overlapping forms are `/evolve list`, `/evolve mode`,
`/evolve theme`, and `/evolve schedule`; their responsibilities now live under
`evidence`, `candidates`, `propose`, or `config`.
