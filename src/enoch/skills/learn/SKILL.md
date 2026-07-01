# Learn

## Purpose

Use this skill when Enoch should adapt and integrate a published skill from Lucy, Adam, Enoch, a descendant, or another trusted Our-Ark agent.

## Use When

- The human asks Enoch to learn a named skill from another agent with `/learn <skill> from <agent>`.
- The source agent publishes that skill on GitHub main under `our-ark/<agent>`.
- Enoch should translate the useful idea into her own body instead of copying another agent blindly.

## Do Not Use When

- The human wants a direct dependency update, branch merge, or parent inheritance.
- The source is untrusted or the lesson depends on secrets.
- The skill has no clear portable improvement for Enoch.

## Procedure

1. Inspect the published skill with `/learn <skill> from <agent>`.
2. Read the source agent's declared skill metadata and `SKILL.md` from GitHub main.
3. Decide whether Enoch should adapt the idea.
4. If adapting, express a concise repository edit request.
5. Let Enoch's normal edit workflow create a branch, modify files, run doctor, commit, push, and open a PR.
6. Preserve human review as the absorption boundary.

## Boundary

Learning is not synchronization or inheritance. Enoch should adapt published skills into her own structure and identity, not overwrite herself with another agent's body.

## Validation

Run:

```bash
python3 -m unittest discover -s tests
```
