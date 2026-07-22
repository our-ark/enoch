# Testing Enoch

Run the complete suite with:

```bash
python -m unittest discover -s tests
python -m unittest discover -s libraries/launchd/tests
python -m unittest discover -s libraries/systemd/tests
```

The service-provider suites verify manifest generation and lifecycle command
delegation independently. GitHub Actions runs them on Linux alongside the core
suite, so systemd support remains part of the required regression gate while
launchd behavior is tested hermetically.

## Hermetic evolution E2E tests

`tests/test_enoch_e2e.py` exercises Enoch's evolution task lifecycle with real
temporary Git repositories, a bare `origin`, and a linked agent worktree. It
uses protocol-compatible local substitutes for Codex, GitHub CLI, and Telegram,
so it needs no network access, GitHub credentials, Codex token, or Telegram
token.

The suite verifies:

- approved evolution publishes a ready-for-review PR with full provenance;
- each task runs in an isolated linked worktree based on the latest
  `origin/main`, even when the resident checkout is dirty;
- successful task worktrees are removed while the resident branch remains
  untouched, and failed task worktrees remain available for inspection;
- active worker leases prevent startup recovery from launching a duplicate
  worker or accepting a stale final status;
- permanent failures such as a dirty worktree fail immediately, while only
  classified transient failures retry with a three-attempt ceiling;
- `/task resume <id|all>` preserves paused task ids, while `/resume` remains
  the resume-all alias;
- a failed task can be retried without rewriting history, and retry reconciles
  journaled or branch-linked PRs before starting duplicate work;
- Codex authentication failure pauses a task and `/resume` completes that same
  task after access returns;
- progress updates edit one Telegram status message;
- failed work creates an experience candidate with its complete causal chain.

The E2E doctor result is deterministic because the outer test run already
executes the complete test suite. Git branch, commit, push, cleanup, queue,
event, provenance, and PR command behavior remain real.

Live GitHub, Telegram, and Codex smoke tests should remain opt-in. They exercise
credentials and external services, so they do not belong in pull request CI.

## Portable installation E2E

`tests/test_enoch_portable_install.py` installs the Enoch wheel surface, shared
contracts, skill catalog, and a temporary third-party provider distribution
into an empty target without network access. The provider contributes only
`chat` and `vcs` entry points. The installed Enoch then uses its built-in Codex
runtime adapter and local forge to complete, validate, and commit a real task
while preserving the unpushed task branch.

This catches packaging metadata conflicts and source-checkout imports that unit
tests can accidentally hide.
