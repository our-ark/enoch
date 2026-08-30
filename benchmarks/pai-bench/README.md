# PAI-Bench

PAI-Bench is a provider-neutral benchmark for persistent identity in deployed
AI agents. The current public release is **v1.0.0**.

PAI-Bench v1.0 contains 24 synthetic identities in 12 matched counterfactual
pairs, divided into development, frozen factorial-test, and frozen
source-challenge splits. Every identity uses the same 32 question templates.

The folder is self-contained: it includes the installable Python package,
tests, schemas, documentation, release generator, and frozen public data. It
can be extracted from Enoch without importing the Enoch agent package.

## Install

From the Enoch repository root:

```bash
python3 -m pip install ./benchmarks/pai-bench
```

The package exposes the `identity-benchmark` and
`identity-benchmark-replay` commands. The checkout-local launchers under
`bin/` select Python 3.11 or newer without requiring an installation.

## Release layout

The frozen release is stored under [`releases/v1.0/data/`](releases/v1.0/data/):

- `probe-suite.json`: the shared question templates;
- `identities/`: portable identity contracts without benchmark questions;
- `bindings/`: evaluator-private variables, transitions, and scoring oracles;
- `*-decoupled-experiment.json`: frozen reference split manifests;
- `protocol.json`: split, freeze, and no-test-tuning policy; and
- `profiles/`: immutable compiled snapshots retained to reproduce the arXiv v1
  experiments byte for byte.

The `identity-publication-v4.1` generator value and `-publication-v4` profile
suffixes are immutable provenance identifiers from development. They are not
separate public benchmark versions. The public release version is recorded in
[`VERSION`](VERSION).

## Verify the release

```bash
benchmarks/pai-bench/bin/release --check
```

## Run a split

The frozen manifests retain the Eve target and evaluator command names used by
the original paper campaign. Copy the selected decoupled manifest to an
untracked working directory and replace `instance_command` and, when needed,
`evaluator.command` with adapters for the agent and judge being evaluated. Do
not change the identities, probe suite, bindings, split membership, or rubric.

```bash
benchmarks/pai-bench/bin/identity-benchmark matrix \
  .pai-bench/experiments/test.json \
  --output-dir .pai-bench/reports/pai-bench-v1-test \
  --resume
```

Use the development split for pipeline work. Do not tune prompts, adapters, or
evaluation rules after inspecting responses from either frozen split.

## Adapter environment

Experiment processes receive `IDENTITY_BENCHMARK_STATE_HOME`,
`IDENTITY_BENCHMARK_MODEL`, `IDENTITY_BENCHMARK_REASONING_EFFORT`,
`IDENTITY_BENCHMARK_IDENTITY_MODE`, and `IDENTITY_BENCHMARK_RUN_ID`.
Evaluator commands additionally receive the documented
`IDENTITY_BENCHMARK_EVALUATOR_*` variables.
