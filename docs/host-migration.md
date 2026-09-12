# Host migration

Enoch host migration moves one continuity-bearing installed agent between two
compatible software-body checkouts. It is intentionally separate from changing
the chat or runtime provider: migrate the host first while keeping those
bindings stable, verify continuity, and then evaluate provider substitutions as
separate transitions.

The migration bundle contains portable identity, memory, durable workflow,
schedule, lineage, channel cursor, inbox, and notification-receipt state. It
does not contain `config.yaml`, credentials, daemon process state, downloaded
dependencies, backups, or Codex/Claude native session mappings. Artifact
storage is optional because it can be large and may contain sensitive logs.

## 1. Prepare and export on the source

Commit the software body and stop the source daemon. A migration checkpoint
requires a clean body revision, current private-state schemas, no running task,
and no queued or paused task that still references a host-local worktree.

```text
bin/enoch-daemon stop
bin/enoch state migrate --dry-run
bin/enoch state migrate
bin/enoch migration export /secure/path/enoch-migration.zip
```

Add `--include-artifacts` only when retained evidence must travel with the
agent:

```text
bin/enoch migration export /secure/path/enoch-migration.zip --include-artifacts
```

Export records the exact body revision, source provider bindings, source
authority generation, identity and memory digests, durable task identifiers,
and a checksum for every included file. The bundle is created with mode `0600`.
It is not encrypted; transfer it through an encrypted channel or wrap it in an
encrypted container.

After a successful export, the source is fenced by
`.enoch/migration_source.json`. Enoch refuses to start or acquire another local
daemon epoch there while the fence exists. `bin/enoch migration status` reports
the migration id and current source or target phase, including after an
interrupted command.

## 2. Validate and import on the target

Prepare a clean checkout at the body revision printed by export. Configure
target credentials separately; they are never read from the bundle. Then run a
read-only import preview:

```text
bin/enoch migration inspect /secure/path/enoch-migration.zip
bin/enoch migration import /secure/path/enoch-migration.zip --dry-run
```

The preview verifies the archive paths, manifest, file sizes and SHA-256
digests, clean body checkout, exact body revision, private-state compatibility,
and absence of conflicting installed-agent state. It does not write target
state.

Apply the import only after the preview passes:

```text
bin/enoch migration import /secure/path/enoch-migration.zip
bin/enoch config provider chat slack
bin/enoch config provider runtime codex
# If inspect reported an explicit source model, set that same model here.
bin/enoch doctor
bin/enoch migration activate
bin/enoch migration verify
bin/enoch-daemon start
```

The target keeps its own `config.yaml`, so the example preserves Slack and
Codex through the host migration without transferring their secrets. Import
commits the portable state manifest last and removes every newly written file
if validation fails. Activation establishes an authority generation strictly
greater than the source generation before the daemon resumes. `migration
verify` compares the body revision and all portable-state checksums, confirms
that authority advanced, and writes a machine-readable report under
`.enoch/artifacts/migrations/<migration-id>/migration-report.json`.

## 3. Roll back before target activation

If target import or validation fails before the target becomes active, release
the source fence with the exact migration id:

```text
bin/enoch migration cancel <migration-id> --confirm-no-active-target
bin/enoch-daemon start
```

The confirmation is deliberately explicit. Do not cancel a source fence after
the target has started: two independently running copies do not share a global
authority store. Stronger protection across non-cooperating hosts requires
credential rotation, provider-side leases or fencing, or an external authority
service.

## Experimental sequence

For an axis-separated migration study:

1. migrate Mac mini to Mac Air while retaining Slack and Codex;
2. verify identity, memory, body revision, task identifiers, authority, and
   duplicate-effect counts;
3. switch Codex to Claude and repeat the checks;
4. switch Slack to Telegram and repeat the checks.

This ordering distinguishes host migration from runtime/harness and interaction
surface substitution. A Codex-to-Claude transition generally changes both the
harness and the model family; a same-harness model-version transition is needed
to isolate model substitution.
