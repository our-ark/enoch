# Multiple independent Enoch instances

Enoch supports separate installed agents using the same software-body
revision. Give every installed agent its own checkout or linked worktree,
private state, and chat-provider configuration. A personal identity in
`.enoch/self.json` is distinct from the shared body identity in `body.yaml`.

## Create and run

From the source checkout:

```bash
bin/enoch init --instance work --worktree ../enoch-work
bin/enoch init --instance life --worktree ../enoch-life
```

Each worktree starts at the current committed `HEAD`; uncommitted changes are
not copied. Install the fixed service-provider revision before expecting the
behavior described here. Separate clones are also supported. Linked worktrees
share Git objects and refs, not `.enoch/`; use separate clones when independent
repository administration is required.

In each directory, configure its own chat provider, credentials, runtime, and
optional personal identity using the normal setup workflow. Then run:

```bash
bin/enoch-agent             # foreground, in this installation
```

Or use its background service:

```bash
bin/enoch-daemon start
bin/enoch-daemon status
bin/enoch-daemon manifest   # inspect the exact service name and target
bin/enoch-daemon logs
bin/enoch-daemon stop
```

Run these commands from the desired instance's directory, using that
directory's launcher. `--instance` is a worktree label, not a switch for
multiple state namespaces inside one directory. Repeating `init --instance`
without `--worktree` relabels the current installation; it does not create a
second agent.

## Service identity and existing installations

New services use the first 16 hexadecimal characters of SHA-256 of the
resolved, absolute installation path:

- macOS: `com.ourark.enoch.<path-id>`;
- Linux: `our-ark-enoch-<path-id>.service`.

The launchers remain `bin/enoch-agent` and `bin/enoch-daemon`. The path ID is a
host-service address, not the agent's personal identity. Symlinks to the same
directory resolve to the same service. Two installations may use the same
friendly instance name without sharing a service.

An existing package-only service (`com.ourark.enoch` or
`our-ark-enoch.service`) is retained only when its generated manifest's
working directory and executable identify this installation. A second
installation receives a path-scoped service and leaves that legacy manifest
alone. A conflicting or invalid manifest at the selected scoped path is an
error, not permission to overwrite it. This compatibility check recognizes
provider-generated manifests, not arbitrary hand-written service definitions.

Before moving an installation directory, stop and uninstall its service from
the old location; install/start it from the new location afterward. Moving
private state between hosts is a separate [migration workflow](host-migration.md).

## Isolation boundary

- Keep independent `.enoch/` directories and distinct channel endpoints (for
  example, a separate Telegram bot per agent). Do not point independent
  agents at the same private-state or artifact directory through environment
  overrides. Those advanced storage redirects are not a named-instance
  deployment interface.
- Multiple processes must not act as simultaneous owners of the same private
  state. Starting a replacement advances that state's daemon epoch and fences
  the old owner. It does not fence agents with other private-state roots.
- This is local multi-instance deployment, not shared-state active/active
  replication or an OS security sandbox. Agents running as the same operating
  system user can still access resources permitted to that user.
- Runtime accounts and external-provider quotas can still be shared. These
  deployment boundaries do not promise independent external capacity.

## Regression coverage

The launchd and systemd suites use temporary home directories and mocked
service-manager commands. They verify independent manifests and lifecycle
targets, legacy ownership, symlink stability, and conflicting-manifest refusal.
`tests/test_enoch_multi_instance.py` starts two real Python processes, exercises
their local workflow engines concurrently, checks separate identity/config and
task history, and verifies that takeover of one agent leaves the other valid.
It does not invoke a live model, chat endpoint, or host service manager.
