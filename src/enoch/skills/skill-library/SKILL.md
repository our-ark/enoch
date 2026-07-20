---
name: skill-library
description: Extract an agent-neutral skill implementation into a reusable, versioned public library while keeping identity, private state, provider errors, and runtime wiring in a thin inheritable adapter. Use when a skill is duplicated across agent repositories, should be shared with descendants without copying its implementation, or needs a stable library contract and immutable dependency pin.
---

# Skill Library

Turn a proven skill implementation into a reusable public artifact without
turning descendants into copies of the library owner.

## Boundary

Keep these concerns in the shared library:

- agent-neutral data types and algorithms;
- stable public functions and protocols;
- validation that does not depend on one agent's identity;
- library-level tests.

Keep these concerns in the agent adapter:

- identity and personality;
- private state paths and credentials;
- provider-specific errors and configuration;
- command surfaces, permissions, and lifecycle wiring.

Do not extract a library merely to move code. Extract only when the contract is
useful across agents or implementations.

## Workflow

1. Define a small versioned contract before moving code.
2. Create the library under `libraries/<name>/`, outside every
   `genesis.toml` `body_paths` entry.
3. Give the library a neutral package and import name. Include focused tests
   and an explicit license.
4. Replace the original implementation with a thin adapter that activates the
   dependency, supplies agent-owned paths and configuration, and translates
   library errors into local provider errors.
5. Commit the standalone library first. Treat that immutable commit as the
   dependency release.
6. Pin the full library commit in both package metadata and
   `[[runtime_dependencies]]`; set `local_source` so the owning repository can
   validate its checkout without downloading itself.
7. Record `library_owner`, `library_package`, `library_contract`, and
   `library_commit` in the skill metadata.
8. Validate the library, adapter, runtime bootstrap, full agent suite, and a
   real Genesis descendant.

## Inheritance Contract

Genesis descendants inherit the skill declaration, adapter, and regression
tests. They do not inherit `libraries/`. At runtime they resolve the exact
pinned public library commit into private instance state.

A descendant may keep the shared contract, select another compatible
implementation, or replace the skill entirely. The dependency is reusable
knowledge, not permanent ownership of the descendant.

## Guardrails

- Never place secrets, memories, logs, chat identifiers, or instance state in
  a shared library.
- Never use a moving branch or partial commit reference as a dependency.
- Preserve upstream provenance, license terms, and attribution.
- Do not rewrite the library owner when transforming descendant identity.
- Keep the library commit separate from the later adapter-and-pin commit so
  the dependency can resolve immutably.
- Require human review before publishing, changing permission boundaries, or
  adopting externally sourced code.
