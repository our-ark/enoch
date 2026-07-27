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
semantic candidate synthesis -----+
assessed learning -----------------+--> candidate pool
validated brainstorm drafts -------+          |
                                      bounded semantic curation
                           |
                   human approve/remove
                           |
              archived handoff record
                           |
       normal task -> worktree -> commit -> PR
                           |
                normal task/PR lifecycle
```

## Modes and direction

`disabled` prevents evidence scans, candidate synthesis, and proposals.
Explicit non-evolution work remains available.

`co-evolve` lets Enoch notice and recommend improvements, while a human decides
whether to run or remove them. This is the default.

`auto-evolve` schedules the same proposal pipeline. It does not grant merge
authority and does not bypass human approval for candidate execution, retries,
or removal.

The evolution theme supplies direction to synthesis, brainstorming, curation,
and deterministic fallback scoring. It is not evidence.

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
- A scheduled evolve check uses that same evidence-first proposal flow and, in
  `auto-evolve` only, may brainstorm when no visible candidate remains.

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
3. `learning` — direct candidates authored by a fresh Codex session after it
   assesses an immutable published skill snapshot as applicable.
4. `brainstorming` — direct bounded ideas generated under mission and theme.

The feedback and experience pathways now use the new evidence layer.
Learning and brainstorming bypass the evidence layer. `/learn` validates
one immutable source snapshot, asks one fresh read-only Codex session for an
applicability decision and candidate contents, and persists only applicable
results. Backlog and inheritance have both been removed as sources. Inheritance
now uses its own Codex-assessed inbox and explicit `/inherit <change_id>` task
workflow.

## Brainstorming

`/evolve brainstorm [theme]` invokes one fresh stateless, read-only Codex
session. The bounded context contains:

- Enoch's mission and selected theme;
- up to 50 declared skills;
- up to 30 existing candidates, including source-theme metadata; and
- up to 12 privacy-cleaned recent completed-work summaries.

The session may inspect the repository read-only to determine whether an idea
already exists. It must return only an exact JSON array containing zero to three
complete candidate drafts. Enoch validates the schema, length bounds, protected
scope, dangerous actions, and within-response duplicates. Valid drafts are
deduplicated against stored candidates and written directly to
`.enoch/evolve_candidates.json`.

There is no intermediate brainstorm artifact and no later collection pass.
Each stored brainstorm candidate records the source theme, SHA-256 hash of the
bounded input context, and creation timestamp. Theme changes do not delete
brainstorm candidates; an exact theme match is a ranking bonus.

`/evolve propose` never brainstorms. In `co-evolve`, brainstorming is explicit.
On a scheduled `auto-evolve` run, feedback and experience scans plus evidence
synthesis run first. Only when that leaves no visible candidate may the same
brainstorming pathway run, with one claim per theme per 24 hours. A generated
candidate still waits for human approval.

## Candidate curation

Candidate synthesis decides what possible changes the evidence supports.
Curation is a separate third stateless reasoning invocation that decides which
bounded candidate, if any, should be recommended now.

Deterministic scores only select and order the bounded input. The curator sees
immutable provenance, mission, theme, and privacy-cleaned completion evidence.
It may:

- recommend one known candidate with scope/risk/test guidance;
- suggest that a human remove candidates as duplicate, superseded, obsolete,
  already resolved, context-only, or not actionable.

It cannot invent a candidate. New agent-authored ideas must pass through the
dedicated brainstorming pathway so their input context and provenance remain
auditable.

Unknown IDs, unsafe scope, invalid resolution evidence, or malformed output
produce an explicitly labeled deterministic fallback. Suggestions never mutate
state. Approval and removal require explicit commands.

Curations are appended to `.enoch/evolve_curations.jsonl`. Candidate state is
stored in `.enoch/evolve_candidates.json`, and proposal, decision, and handoff
events are appended to `.enoch/artifacts/evolve_events.jsonl`.

## Candidate-to-task handoff

`/evolve approve <id>` queues a normal isolated task with candidate and evidence
provenance, archives the candidate as `approved`, and appends an immutable
candidate-to-task event. The candidate immediately leaves the active pool.

From that boundary onward, the task is the sole owner of queued, running,
paused, completed, failed, cancelled, retry, worktree, commit, push, and review
state. Use `/tasks` to inspect it and `/task retry <task_id>` after a failure.
Evolution does not mirror those states and does not require reconciliation
after the handoff. `/evolve candidates all` retains the archived candidate
snapshot for provenance and deduplication.

## Command surface

```text
/evolve
/evolve evidence [feedback|experience|all]
/evolve scan [feedback|experience|all]
/evolve candidates [all]
/evolve propose
/evolve brainstorm [theme]
/evolve approve <id>
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
