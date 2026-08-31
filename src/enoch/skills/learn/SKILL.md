# Learn

## Purpose

Assess whether a visible skill published by a non-parent Our-Ark agent offers a
bounded capability that Enoch should adapt. An applicable assessment creates an
evolution candidate; it never edits Enoch directly.

## Use When

- The human selects a named skill with `/learn <skill> from <agent>`.
- The source agent publishes the skill through the configured forge.
- Enoch should evaluate a portable capability rather than inherit a direct-parent
  change.

## Procedure

1. Resolve the source agent's current `main` revision to an immutable commit.
2. Read `body.yaml` (or a legacy `identity.yaml`), `skill.yaml`, and `SKILL.md` from that same commit.
3. Reject hidden, missing, inconsistent, oversized, unsafe-path, self, and
   direct-parent packages deterministically.
4. Build a temporary in-memory snapshot containing the source commit, package
   contents, version, link, and content hash.
5. Give that snapshot, Enoch's mission and declared skills, and a bounded list
   of current candidates to one fresh read-only Codex session.
6. Require one structured result: `applicable` with complete candidate fields,
   or `not_applicable` with a reason and no candidate.
7. Validate the returned scope and schema in deterministic code.
8. Persist an applicable result as a `learning` evolution candidate with the
   immutable source provenance attached.
9. Notify the human and leave execution to `/evolve approve <candidate-id>`.

## Boundary

Learning is assessment, not synchronization, inheritance, or execution. Codex
authors the candidate contents, while Enoch validates and persists them. A
not-applicable result creates no candidate or separate assessment record.

## Validation

Run:

```bash
python3 -m unittest discover -s tests -t .
```
