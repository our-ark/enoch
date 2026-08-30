# Our Ark PAI-Bench

This library provides the provider-neutral runner, command-adapter protocol,
evaluator interface, deterministic scoring, experiment matrix, replay, and
clustered-bootstrap tooling for PAI-Bench.

Install it independently from the Enoch repository:

```bash
python3 -m pip install ./libraries/pai-bench
```

The frozen public dataset lives at `benchmarks/pai-bench/v1.0/`. Target agents
and model judges connect through JSON-over-stdin command adapters, so neither
the library nor the scoring protocol imports Enoch.

Experiment processes receive these neutral environment variables:

- `IDENTITY_BENCHMARK_STATE_HOME`
- `IDENTITY_BENCHMARK_MODEL`
- `IDENTITY_BENCHMARK_REASONING_EFFORT`
- `IDENTITY_BENCHMARK_IDENTITY_MODE`
- `IDENTITY_BENCHMARK_RUN_ID`

Evaluator commands additionally receive the documented
`IDENTITY_BENCHMARK_EVALUATOR_*` variables. See
`docs/identity-benchmark.md` for the request and response contracts.
