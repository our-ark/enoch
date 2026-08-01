# Storage boundaries

Enoch exposes a versioned `StorageLayout` contract so the software body,
private runtime state, and retained artifacts are not treated as one
undifferentiated workspace. The current contract is
`STORAGE_API_VERSION = 1`.

## Ownership areas

| Area | Default root | Examples | Ownership |
| --- | --- | --- | --- |
| Software body | repository root | source, tests, skills, versioned identity and lineage | reviewed and versioned |
| Private state | `.enoch/` | config, memory, queue, schedules, sessions, provider cursors | local agent instance |
| Artifacts | `.enoch/artifacts/` | conversation and system logs, task/evolve journals, curation and learning evidence | retained evidence and outputs |

The artifact root is a protected namespace inside the default private
container, but it is not part of the private-state namespace. A private-state
path cannot enter `artifacts/`, and a software-body path cannot enter either
private area. Relative path traversal and absolute paths are rejected.

Set `ENOCH_ARTIFACT_HOME` to place retained evidence in an independent
directory. This allows artifacts to have a different backup, retention, or
cleanup policy without moving operational state:

```bash
export ENOCH_ARTIFACT_HOME="$HOME/Library/Application Support/enoch/artifacts"
```

`ENOCH_STATE_REDIRECT_ROOT` and `ENOCH_STATE_HOME` remain the hermetic
private-state redirect used by tests and embedded installations.

## Public API

Profiles receive the same immutable layout in command, prompt, and lifecycle
contexts:

```python
def handle_report(context):
    state = context.storage.private_path("profiles/researcher/report.json")
    evidence = context.storage.artifact_path("profiles/researcher/report.jsonl")
```

Core and extension code should use `private_state_path()`, `artifact_path()`,
or `software_body_path()` from `enoch.paths` instead of joining paths onto the
repository root or `.enoch` directly. `enoch_home()` remains a compatibility
alias for the private-state root.

An explicit root is authoritative. Passing `root=instance` to a path, queue,
event, workflow, or storage API always selects that exact software body, even
when it is nested beneath an unrelated `.git` directory or
`pyproject.toml`. Omit the root to opt into repository discovery from the
current working directory; `discover_repo_root()` exposes that operation
directly for CLI entry points.

## Compatibility and retention

New logs and append-only task, semantic evidence, evidence-scan, evolution,
curation, compatibility experience, and learning records are written below
`.enoch/artifacts/`. Semantic evolution evidence lives in
`evidence.jsonl`, while cursor/audit records live in `evidence_scans.jsonl`.
Readers also load
the corresponding pre-boundary files from `.enoch/`, with legacy records
ordered before new records. Legacy files are not moved or deleted.

This compatibility is deliberately read-only. Versioned private-state schemas,
dry-run migration, validation, and unsupported-state failures use the manifest
contract described below; introducing storage ownership does not pretend that
an old file was migrated.

Deleting artifact storage removes history and evidence but must not stop the
queue from operating. Deleting private state resets local operation and must
never modify the versioned software body. Neither private area belongs in
source control.

## Private-state schemas

`.enoch/state_manifest.json` is the authoritative registry for Enoch-owned
private-state schemas. The current manifest format is
`PRIVATE_STATE_MANIFEST_SCHEMA_VERSION = 1`, and the current aggregate contract
is `PRIVATE_STATE_VERSION = 1`.

The registry versions queue, backlog, cron, declarative extension schedules,
evolution control, semantic evidence batch settings, memory, runtime session,
daemon epoch, channel cursor/lifecycle/inbox/notification, config, and
pending-adoption state.
Provider or profile files outside registered patterns remain owned by those
extensions.

The legacy pending-adoption schema remains readable so old private state does
not block an upgrade. Current evolution hands approved candidates to the normal
task lifecycle and no longer writes that file.

Every application startup validates registered files before claiming a daemon
epoch. Supported legacy versions may continue to run until explicitly
migrated. Corrupt files, invalid shapes, future manifest versions, and future
file schemas fail startup without rewriting or resetting state.

Use the admin CLI to inspect and migrate:

```text
bin/enoch state validate
bin/enoch state migrate --dry-run
bin/enoch-daemon stop
bin/enoch state migrate
```

Validation and dry-run are read-only. Apply refuses to run while the recorded
daemon process is alive. Before changing any file, Enoch copies every migration
target and the prior manifest into
`.enoch/backups/state-v<version>-<timestamp>-<id>/`. Files are normalized and
atomically replaced one at a time; the manifest is committed last. Any failure
restores the original files, preserves the backup, and reports the rollback.
Running the same migration again is a no-op.

Artifact storage is never scanned, backed up, or rewritten by private-state
migration.

Brainstorming has no separate artifact journal. Validated drafts are stored
directly as candidates in `.enoch/evolve_candidates.json`; scheduler cooldown
claims live in `.enoch/evolve_brainstorm_schedule.json`. Both are operational
private state covered by the private-state manifest.
