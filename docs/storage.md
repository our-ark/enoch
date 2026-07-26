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

## Compatibility and retention

New logs and append-only task, evolution, curation, brainstorming, experience,
and learning evidence are written below `.enoch/artifacts/`. Readers also load
the corresponding pre-boundary files from `.enoch/`, with legacy records
ordered before new records. Legacy files are not moved or deleted.

This compatibility is deliberately read-only. Versioned private-state schemas,
dry-run migration, validation, and unsupported-state failures are a separate
contract; introducing storage ownership does not pretend that an old file was
migrated.

Deleting artifact storage removes history and evidence but must not stop the
queue from operating. Deleting private state resets local operation and must
never modify the versioned software body. Neither private area belongs in
source control.
