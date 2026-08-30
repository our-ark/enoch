from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "tests" / "fixtures"

from identity_benchmark import (
    AgentAdapter,
    CommandAgentAdapter,
    CommandEvaluator,
    CommandInstance,
    DeterministicEvaluator,
)
from identity_benchmark.contracts import BenchmarkRequest, InstanceResponse
from identity_benchmark.evaluators import EvaluationResult, EvaluatorError
from identity_benchmark.experiments import load_experiment_spec, run_experiment
from identity_benchmark.runner import run_benchmark
from identity_benchmark.contracts import load_benchmark_profile


PROFILE = FIXTURES / "synthetic-profile.json"
AGENT = FIXTURES / "synthetic-instance.py"
EVALUATOR = FIXTURES / "synthetic-evaluator.py"
MODEL_EVALUATOR_MANIFEST = FIXTURES / "model-evaluator-smoke-experiment.json"


class IdentityBenchmarkEvaluatorTests(unittest.TestCase):
    def test_public_agent_adapter_names_preserve_v1_implementations(self) -> None:
        self.assertIs(CommandAgentAdapter, CommandInstance)
        self.assertTrue(hasattr(AgentAdapter, "respond"))

    def test_evaluation_result_rejects_non_finite_or_boolean_scores(self) -> None:
        with self.assertRaises(EvaluatorError):
            EvaluationResult(score=float("nan"))
        with self.assertRaises(EvaluatorError):
            EvaluationResult(score=True)

    def test_deterministic_evaluator_records_provenance(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        agent = CommandAgentAdapter(
            command=(sys.executable, str(AGENT)),
            instance_id="synthetic-agent",
        )

        report = run_benchmark(
            profile,
            agent,
            evaluator=DeterministicEvaluator(),
        )

        self.assertEqual(report.evaluator_id, "deterministic-v1")
        self.assertEqual(report.score, 1.0)
        self.assertTrue(
            all(
                result.evaluation_metadata["evaluator_id"] == "deterministic-v1"
                for result in report.results
            )
        )

    def test_command_evaluator_receives_complete_probe_context(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        agent = CommandAgentAdapter(
            command=(sys.executable, str(AGENT)),
            instance_id="synthetic-agent",
        )
        evaluator = CommandEvaluator(
            command=(sys.executable, str(EVALUATOR)),
            evaluator_id="synthetic-judge-v1",
            environment={
                "IDENTITY_BENCHMARK_EVALUATOR_MODEL": "judge-model",
                "IDENTITY_BENCHMARK_EVALUATOR_REASONING_EFFORT": "xhigh",
            },
        )

        report = run_benchmark(profile, agent, evaluator=evaluator)

        self.assertEqual(report.evaluator_id, "synthetic-judge-v1")
        self.assertEqual(report.score, 1.0)
        self.assertTrue(all(not result.expectations for result in report.results))
        self.assertEqual(
            {result.evaluation_metadata["model"] for result in report.results},
            {"judge-model"},
        )
        self.assertEqual(
            {
                result.evaluation_metadata["reasoning_effort"]
                for result in report.results
            },
            {"xhigh"},
        )
        self.assertTrue(
            all(
                result.evaluation_metadata["has_description"] is False
                for result in report.results
            )
        )
        self.assertEqual(
            {
                tuple(result.evaluation_metadata["request_keys"])
                for result in report.results
            },
            {
                (
                    "agent_response",
                    "probe",
                    "profile_id",
                    "protocol_version",
                    "statements",
                )
            },
        )

    def test_evaluator_failure_is_separate_from_agent_response(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        agent = _StaticAgent()

        report = run_benchmark(profile, agent, evaluator=_FailingEvaluator())

        self.assertEqual(report.errors, len(profile.probes))
        self.assertTrue(
            all(result.response == "agent output" for result in report.results)
        )
        self.assertTrue(
            all("evaluation failed" in result.error for result in report.results)
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_evaluator_timeout_terminates_descendants_and_is_recorded(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        script = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "time.sleep(30)"
        )
        evaluator = CommandEvaluator(
            command=(sys.executable, "-c", script),
            evaluator_id="hung-judge",
            timeout_seconds=0.05,
        )

        started = time.monotonic()
        report = run_benchmark(profile, _StaticAgent(), evaluator=evaluator)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        self.assertEqual(report.errors, len(profile.probes))
        self.assertTrue(
            all(
                result.error
                == (
                    "evaluation failed: evaluator command timed out after "
                    "0.05 seconds"
                )
                for result in report.results
            )
        )
        self.assertTrue(
            all(result.response == "agent output" for result in report.results)
        )

    def test_manifest_pins_reference_evaluator_model_and_harness(self) -> None:
        spec = load_experiment_spec(MODEL_EVALUATOR_MANIFEST)

        self.assertIsNotNone(spec.evaluator)
        assert spec.evaluator is not None
        self.assertEqual(spec.evaluator.evaluator_id, "codex-sol-xhigh-v1")
        self.assertEqual(spec.evaluator.harness, "codex-cli")
        self.assertEqual(spec.evaluator.model, "gpt-5.6-sol")
        self.assertEqual(spec.evaluator.reasoning_effort, "xhigh")
        self.assertEqual(spec.evaluator.rubric_version, "pai-model-judge-v1")

    def test_matrix_records_evaluator_in_reports_and_aggregates(self) -> None:
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            manifest = temporary / "experiment.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "experiment_id": "evaluator-fixture",
                        "profile": str(PROFILE),
                        "body_root": str(ROOT),
                        "instance_command": [sys.executable, str(AGENT)],
                        "models": ["agent-model"],
                        "reasoning_efforts": ["low"],
                        "identity_modes": ["full-context"],
                        "evaluator": {
                            "id": "synthetic-judge-v1",
                            "harness": "fixture",
                            "command": [
                                sys.executable,
                                str(EVALUATOR),
                                "{state_home}",
                            ],
                            "model": "judge-model",
                            "reasoning_effort": "xhigh",
                            "rubric_version": "fixture-rubric-v1",
                        },
                        "timeout_seconds": 10,
                    }
                ),
                encoding="utf-8",
            )

            report = run_experiment(
                load_experiment_spec(manifest),
                temporary / "reports",
            )
            saved = json.loads(
                (temporary / "reports" / "experiment-report.json").read_text(
                    encoding="utf-8"
                )
            )
            state_homes = {
                result.evaluation_metadata["state_home"]
                for run in report.runs
                for result in run.report.results
            }
            evaluator_arguments = {
                result.evaluation_metadata["arguments"][0]
                for run in report.runs
                for result in run.report.results
            }

        self.assertEqual(report.runs[0].report.evaluator_id, "synthetic-judge-v1")
        self.assertEqual(report.aggregates[0]["evaluator_id"], "synthetic-judge-v1")
        self.assertEqual(saved["evaluator_ids"], ["synthetic-judge-v1"])
        self.assertTrue(all(not Path(path).exists() for path in state_homes))
        self.assertEqual(evaluator_arguments, state_homes)


class _StaticAgent:
    instance_id = "static-agent"

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        del request
        return InstanceResponse(response="agent output")


class _FailingEvaluator:
    evaluator_id = "failing-evaluator"

    def evaluate(self, request) -> EvaluationResult:
        del request
        raise EvaluatorError("judge unavailable")


if __name__ == "__main__":
    unittest.main()
