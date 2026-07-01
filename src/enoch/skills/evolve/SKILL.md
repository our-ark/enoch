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

The MVP candidate sources are:

- backlog items from `.enoch/backlog.json`;
- direct-parent inheritance candidates from `.agent/lineage_inbox.json`.

Future sources may include feedback, experience, brainstorming, and learning from non-parent agents.

## Guardrails

Evolve candidates should be small, testable, reversible, and aligned with Enoch's mission and current theme.

Enoch must require human direction before changing identity, mission, secrets, permission boundaries, GitHub settings, daemon configuration, merge behavior, destructive operations, or large architecture.

## Commands

- `/evolve`
- `/evolve disabled`
- `/evolve co-evolve`
- `/evolve auto-evolve`
- `/evolve theme <text>`
