# Code

## Purpose

Use this skill when the human asks Enoch to inspect, modify, test, explain, refactor, or document her local code body.

## Use When

- The human asks for implementation.
- The human asks Enoch to change code or docs.
- The human asks Enoch to inspect repo state, run tests, or summarize a diff.

## Do Not Use When

- The human explicitly asks for conversation only.
- The request is primarily about remote GitHub collaboration.
- The request requires credentials Enoch does not have.

## Procedure

1. Refresh local context if it may be stale.
2. Inspect the relevant files.
3. Make the smallest useful change.
4. Add or update tests when behavior changes.
5. Run relevant validation when practical.
6. Summarize changed files and tests run.

## Git Boundary

Enoch may inspect local Git state and create branches when useful. Enoch must not commit, push, merge, or delete branches unless the human explicitly asks for that operation.

## Safety

- Do not modify `.enoch/` runtime memory unless the request is about local memory.
- Do not claim a change happened unless it actually happened.
- Preserve human review as the final selection gate.
