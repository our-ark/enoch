# GitHub

## Purpose

Use this skill when Enoch needs to coordinate work with GitHub: branches, pull requests, issues, reviews, comments, checks, and merges.

## Use When

- The human asks Enoch to open, update, or approve a pull request.
- The human asks Enoch to inspect GitHub issues, PRs, comments, or checks.
- The human uses `/pr` to list open pull requests or `/pr show` to inspect one.
- Local code changes need to be published for review.

## Do Not Use When

- The task only requires local code editing.
- GitHub credentials or tools are unavailable.
- The human has not approved a remote write action.
- The human is asking for a manual command instead of a natural workflow; prefer understanding the intended outcome.

## Procedure

1. Confirm the target repository and branch.
2. Inspect local Git state before publishing.
3. Run local health checks before publishing when practical.
4. Commit only files that belong to the requested change.
5. Push only the intended branch.
6. Create or update the PR with a clear summary and validation notes.
7. Approve a PR only when the human explicitly asks for approval.
8. Treat the PR as the human review boundary.
9. Never merge without an explicit human `/pr merge <PR number or GitHub PR URL>` command from the locked chat-provider conversation.

## Explicit Merge Approval

`/pr merge` is the only system command that authorizes Enoch to merge a pull request. The command must name one PR by positive number or full GitHub PR URL; never infer a target from the current branch or conversation.

Before merging, inspect that exact PR and refuse closed, already-merged, draft, conflicting, blocked, inaccessible, or otherwise unmergeable targets. Do not mark drafts ready, approve PRs, change their content, enable auto-merge, bypass protections, delete branches, or update local branches as part of this command. Pin the inspected head commit during the merge so changed content requires a new human command.

Natural-language requests, task text, and prior approval do not authorize a merge.

## Natural Workflow

Enoch should guide the human through this path:

```text
ask for a change -> doctor -> commit -> push when intended -> open a PR when intended -> human review
```

After a local commit, suggest pushing the branch.
After pushing the branch, suggest opening a PR.
After opening a PR, stop at human review.
If the human decides to merge it, they must name the exact target with `/pr merge <PR number or GitHub PR URL>`.
Use `/pr` and `/pr show <PR number or GitHub PR URL>` for read-only status checks.

## Local Publish Prep

The reference implementation lives in `our_ark_github.workflow`.
Use `prepare_local_publish()` when the human explicitly asks Enoch to prepare local changes for publication.

The helper:

- refuses protected branches by default
- refuses empty diffs
- runs doctor before committing
- stages only detected changed files
- creates a local commit
- does not push, open PRs, or merge

## Safety

- Treat remote writes as higher risk than local code edits.
- Do not expose secrets in PR bodies, comments, logs, or commit messages.
- Refuse to publish from main unless the human explicitly asks for a direct main push.
- Publish completed, validated work as ready for review by default.
- Use a draft PR only when work is intentionally incomplete or the human explicitly requests a draft.
- When an evolve task supplies an `## Evolution provenance` section, include it verbatim in the PR body. It separates evidence source, signal actor, candidate actor, approval actor, task id, and any available candidate/task causal links.
- Preserve human approval for push, PR creation, PR approval, comments, and merges.
- Treat an authorized `/pr merge ...` command as approval for that exact merge only.
- Never mark drafts ready, approve, enable auto-merge, alter PR content, or clean up branches as part of that command.
