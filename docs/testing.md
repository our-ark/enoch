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

`tests/test_enoch_e2e.py` exercises the reference evolution stack with real
temporary Git repositories, a bare `origin`, and a linked agent worktree. It
uses protocol-compatible local substitutes for the Codex runtime, GitHub forge,
and Telegram channel, so it needs no network access or external credentials.
These named tools are fixtures for the reference providers, not dependencies of
Enoch core.

The suite verifies:

- approved evolution publishes a ready-for-review PR with full provenance;
- each reference Git task runs in an isolated linked worktree based on that
  fixture's authoritative `origin/main`, even when the resident checkout is
  dirty;
- successful task worktrees are removed while the resident branch remains
  untouched, and failed task worktrees remain available for inspection;
- active worker leases prevent startup recovery from launching a duplicate
  worker or accepting a stale final status;
- permanent failures such as a dirty worktree fail immediately, while only
  classified transient failures retry with a three-attempt ceiling;
- `/task resume <id|all>` preserves paused task ids and can resume one or all
  paused tasks;
- a failed task can be retried without rewriting history, and retry reconciles
  journaled or branch-linked PRs before starting duplicate work;
- reference-runtime authentication failure pauses a task and `/task resume`
  completes that same task after access returns;
- reference-channel progress updates edit one Telegram status message;
- failed work creates an experience candidate with its complete causal chain.

The E2E doctor result is deterministic because the outer test run already
executes the complete test suite. Git branch, commit, push, cleanup, queue,
event, provenance, and PR command behavior remain real.

Live GitHub, Telegram, and Codex smoke tests should remain opt-in reference
provider checks. They exercise credentials and external services, so they do
not belong in pull request CI.

## Portable installation E2E

`tests/test_enoch_portable_install.py` independently builds wheels for Enoch,
the shared contracts, the skill catalog, a third-party chat provider, and a
third-party VCS provider, plus a separate researcher profile package. It
installs only those wheel artifacts into an empty target without network
access. The provider and profile distributions expose independent entry
points; the VCS provider implements semantic repository operations without
subclassing Enoch's Git provider or exposing raw command compatibility.

The installed Enoch starts with those providers, sends a startup notification,
loads the researcher profile, and handles its custom `/research` command. The
command submits work to Enoch's single queue, then the built-in Codex runtime
adapter and local forge complete, validate, commit, and clean up the task while
preserving its profile trigger, context provenance, and unpushed task branch.

This catches packaging metadata conflicts and source-checkout imports that unit
tests can accidentally hide.
