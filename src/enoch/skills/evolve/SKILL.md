# Evolve

## Purpose

Use this skill when Enoch should inspect, propose, or govern a small
self-evolution step. Evolution is an evidence-to-change pipeline, not a generic
task runner:

```text
records -> evidence -> candidate -> recommendation -> human approval
        -> isolated task -> review -> promotion -> adoption
```

Backlog entries are ordinary deferred work and are never evolution evidence by
themselves.

## Modes

- `disabled`: do not scan, synthesize, or propose self-evolution.
- `co-evolve`: notice and recommend changes, but wait for human approval.
- `auto-evolve`: schedule the same semantic proposal flow, but still wait for
  human approval before queueing, removing, or merging anything.

The default is `co-evolve`. The current theme guides synthesis, curation, and
deterministic fallback ranking; it is context, not an evidence source.

## Evidence pathways

Feedback and experience are two-stage pathways. They first create durable
evidence, then a separate reasoning pass may turn unlinked evidence into
candidate changes.

### Feedback

The feedback scanner reads unprocessed conversation turns in configurable
batches, defaulting to 20 user messages. Each record contains the exact user
message and Enoch reply, plus a stable `conversation:<id>` reference. Credential
values are redacted before the records leave storage.

The scanner runs in a fresh stateless runtime invocation. It receives no normal
conversation session and returns a strict JSON array of zero or more
observations. Simple words, phrases, and regular expressions do not classify
feedback.

### Experience

The experience scanner reads configurable batches of distinct changed task
IDs, defaulting to 20. Each record contains that task's complete ordered event
history from `.enoch/artifacts/task_events.jsonl`, not merely the last event.

The scan cursor records the latest event ID for each task. A later lifecycle
event makes that task pending again, so a completion followed by a regression
is rescanned with the full causal history. A failed task, successful task,
repeated workflow, or cron definition is not evidence by itself; the semantic
scanner must identify durable operational friction or an improvement signal.

### Scan contract

Both scanners:

- may return an empty array;
- may record at most ten evidence items per batch;
- must use the exact JSON schema and cite only supplied record references;
- record confidence and whether a human stated the signal explicitly;
- describe an observation and desired outcome, not a proposed implementation;
- advance their cursor only after a valid response; and
- leave every input pending after timeout, malformed JSON, unknown references,
  or another failed scan.

Threshold scans run from the daemon loop when evolution is enabled, the chat is
locked, no task worker is active, and a source reaches its configured batch
size. This also catches up after restart. `/evolve scan` forces and drains
unprocessed batches even below the threshold. `/evolve propose` and scheduled
evolve checks also force and drain both sources before selection.

## Evidence storage

Evidence settings are stored in `.enoch/evidence.json`.

Evidence is append-only in:

- `.enoch/artifacts/evidence.jsonl`
- `.enoch/artifacts/evidence_scans.jsonl`

An evidence item contains:

- `id`, `source`, observation type, and affected area;
- desired outcome, confidence, and explicit-human flag;
- immutable conversation, task, and task-event references;
- status and timestamps; and
- candidate IDs linked from that evidence.

Evidence is not deleted when it becomes a candidate. A linkage update changes
its latest status to `linked` while the earlier journal record remains. There
is currently no command that deletes evidence.

## Candidate pathways

Enoch has five candidate sources:

- `feedback`: synthesized from feedback evidence;
- `experience`: synthesized from task-history evidence;
- `inheritance`: applicable direct-parent changes from the lineage inbox;
- `learning`: peer skill observations explicitly recorded through `/learn`;
- `brainstorming`: bounded ideas generated under the mission and theme.

Backlog is not a source. Active cron jobs, generic task failures, repeated
successes, and learning artifacts do not become hardcoded candidates. Cron,
recovery, backlog promotion, approval, and scheduling remain task triggers; a
task they create can later be semantically scanned through experience.

Candidate synthesis is a second fresh stateless reasoning invocation. It sees
only unlinked evidence, mission, theme, and a bounded summary of existing
candidates. It may return an empty array. Every returned candidate must:

- cite known evidence IDs;
- use evidence from only one source;
- be small, reversible, testable, and about improving Enoch;
- provide rationale, proposed change, benefit, risk, and test plan; and
- pass protected-scope and dangerous-action validation.

Candidate records preserve `evidence_ids` and original `evidence_refs`.
Feedback/experience candidates from the retired hardcoded pathways are retained
for audit but removed from the actionable pool when they lack evidence IDs.

The other three pathways still create structured candidates directly:
inheritance from the lineage inbox, learning from peer observations, and
brainstorming from a dedicated bounded generation pass.

## Recommendation

After synthesis, semantic curation is a third fresh stateless reasoning
invocation. Deterministic scoring only bounds and orders its input and supplies
an explicitly labelled fallback.

The curator can recommend one known candidate, suggest bounded brainstorming
candidates, or suggest that a human remove an item as duplicate, superseded,
obsolete, already resolved, context-only, or not actionable. It cannot approve,
queue, retry, remove, merge, deploy, change permissions, or mutate mission or
identity.

Curations are appended to `.enoch/evolve_curations.jsonl`. Candidates are
stored in `.enoch/evolve_candidates.json`, and lifecycle decisions are appended
to `.enoch/artifacts/evolve_events.jsonl`.

## Governed lifecycle

Candidate status and body adoption are separate:

- `candidate`: available for proposal and human approval.
- `running`: linked work is active.
- `done`: the task completed; this alone does not prove the change was merged.
- `failed`: available for an explicit human retry.
- `removed`, `cancelled`, `regressed`, `reverted`, or `forward-fixed`: retained
  lifecycle state.
- `promoted`: a human landed the review and Enoch verified its revision in the
  trusted authoritative history.
- `adopted`: an update passed doctor, restarted, and confirmed a version that
  contains the promotion.

`/evolve reconcile <id>` verifies realtime promotion.
`/evolve reconcile <id> backfill` records equivalent historical evidence
without pretending it was observed in realtime.

## Commands

- `/evolve` — read-only dashboard
- `/evolve evidence [feedback|experience|all]`
- `/evolve scan [feedback|experience|all]`
- `/evolve candidates [all]`
- `/evolve propose`
- `/evolve brainstorm [theme]`
- `/evolve approve <id>`
- `/evolve retry <id>`
- `/evolve reconcile <id> [backfill]`
- `/evolve remove <id> [reason]`
- `/evolve config`
- `/evolve config mode <disabled|co-evolve|auto-evolve>`
- `/evolve config theme <text>`
- `/evolve config feedback-batch <1-100>`
- `/evolve config experience-batch <1-100>`
- `/evolve config schedule <text>`

There are no top-level `/feedback`, `/experience`, or `/propose` aliases, and
the older `/evolve list`, `/evolve mode`, `/evolve theme`, and
`/evolve schedule` forms are not commands. `/help evolve` is the authoritative
command reference.
