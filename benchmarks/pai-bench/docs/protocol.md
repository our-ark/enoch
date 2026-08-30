# PAI-Bench

PAI-Bench evaluates whether a deployed agent can recover, compose, enact, and
govern a persistent identity. It separates the identity installed in the
target from the questions and evaluator-private scoring rules.

The public v1.0 release contains 24 synthetic identities in 12 matched pairs,
three prespecified splits, and 32 shared probe templates. See
[`benchmarks/pai-bench/`](../benchmarks/pai-bench/).

## Install the runner

```bash
python3 -m pip install ./benchmarks/pai-bench
identity-benchmark --help
```

Inside this checkout, `benchmarks/pai-bench/bin/identity-benchmark` runs the
same CLI without an installation.

## Three-part benchmark source

Each runnable case is compiled from:

1. an identity-only profile containing the stable contract;
2. `probe-suite.json`, containing the shared question templates; and
3. a private binding file containing variables, observable expectations, and
   authorized state transitions for that identity.

The target receives only a probe's conversation messages. It never receives
the reference statements, expected answer, evaluator rubric, or private
binding. A target adapter may install the identity through its normal identity
mechanism before answering.

The release also retains self-contained compiled profiles. They are immutable
compatibility snapshots for reproducing the original experiment and are not
the preferred authoring format.

## Target adapter protocol

An experiment launches a fresh target command for every probe and writes one
JSON request to standard input:

```json
{
  "protocol_version": 1,
  "profile_id": "profile-001",
  "probe_id": "designation-atomic",
  "messages": [{"role": "user", "content": "Identify yourself."}],
  "after_response": null
}
```

The command returns exactly one JSON object:

```json
{
  "protocol_version": 1,
  "response": "I am CEDAR-ARCH-03.",
  "metadata": {"adapter": "my-agent-v1"}
}
```

Adapters receive the selected model, reasoning effort, isolated state path,
identity mode, and run ID through `IDENTITY_BENCHMARK_*` environment
variables. The interface can wrap a local process, HTTP endpoint, message
transport, or another agent harness.

## Evaluator interface

Deterministic expectations score exact, inclusion, exclusion, pattern, and
format constraints. Open responses can additionally use a replaceable command
evaluator. The evaluator receives the frozen reference contract, probe,
observable expectations, and target response, then returns a score and
provenance metadata. Target and evaluator adapters are independent.

Capability probes are controls, not identity measurements. They are reported
separately and excluded from the headline identity score.

## Experiment matrix

An experiment manifest defines target models, reasoning efforts, identity
modes, repetitions, command adapters, timeouts, and an optional evaluator.

```bash
benchmarks/pai-bench/bin/identity-benchmark matrix EXPERIMENT.json \
  --output-dir .pai-bench/reports/run-001 \
  --batch-size 4 --batch-index 1 --resume
```

Use `--plan` before launching a campaign. Runs are atomic and resumable.
Reports include per-probe results, identity dimensions, counterfactual-pair
metrics, capability controls, constraint agreement, error counts, and adapter
provenance.

Completed reports can be replayed through a second evaluator or analyzed with
clustered bootstrap confidence intervals without calling the target again.

## Frozen release policy

The development split permits pipeline work. The factorial-test and
source-challenge splits are frozen. Do not tune prompts, adapters, or judging
rules after inspecting frozen responses. `protocol.json` records this policy,
and `benchmarks/pai-bench/bin/release --check` verifies every generated release
file.

The retained `identity-publication-v4.1` strings are internal provenance IDs
for the freeze that became public PAI-Bench v1.0; they are not separate public
versions.
