# Testing Enoch

Run the complete suite with:

```bash
python -m unittest discover -s tests
```

## Hermetic evolution E2E tests

`tests/test_enoch_e2e.py` exercises Enoch's evolution task lifecycle with real
temporary Git repositories, a bare `origin`, and a linked agent worktree. It
uses protocol-compatible local substitutes for Codex, GitHub CLI, and Telegram,
so it needs no network access, GitHub credentials, Codex token, or Telegram
token.

The suite verifies:

- approved evolution publishes a ready-for-review PR with full provenance;
- task branches start at the latest `origin/main` without checking out `main`;
- the agent returns to its resident branch after publishing;
- a failed candidate can be retried without rewriting failed task history;
- Codex authentication failure pauses a task and `/resume` completes that same
  task after access returns;
- progress updates edit one Telegram status message;
- failed work creates an experience candidate with its complete causal chain.

The E2E doctor result is deterministic because the outer test run already
executes the complete test suite. Git branch, commit, push, cleanup, queue,
event, provenance, and PR command behavior remain real.

Live GitHub, Telegram, and Codex smoke tests should remain opt-in. They exercise
credentials and external services, so they do not belong in pull request CI.
