from __future__ import annotations

from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
FIXTURES = ROOT / "tests" / "fixtures"

from identity_benchmark.adapters import CommandInstance, InstanceError
from identity_benchmark.cli import main
from identity_benchmark.contracts import (
    BenchmarkProfileError,
    BenchmarkRequest,
    InstanceResponse,
    TransitionRequest,
    load_benchmark_profile,
    parse_benchmark_request,
    parse_benchmark_profile,
    parse_instance_response,
    parse_transition_request,
)
from identity_benchmark.runner import run_benchmark
from identity_benchmark.rescore import RescoreError, rescore_saved_report
from identity_benchmark.scoring import DeterministicScorer, weighted_expectation_score


PROFILE = FIXTURES / "synthetic-profile.json"
INSTANCE = FIXTURES / "synthetic-instance.py"


class IdentityBenchmarkTests(unittest.TestCase):
    def test_synthetic_profile_uses_the_neutral_contract(self) -> None:
        profile = load_benchmark_profile(PROFILE)

        self.assertEqual(profile.profile_id, "synthetic-vela-7")
        self.assertEqual(len(profile.statements), 3)
        self.assertEqual(len(profile.probes), 7)
        self.assertEqual(
            {probe.dimension for probe in profile.probes},
            {
                "recognition",
                "application",
                "consistency",
                "resistance",
                "separation",
                "retention",
                "capability",
            },
        )

    def test_profile_rejects_duplicate_probe_ids(self) -> None:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        value["probes"].append(deepcopy(value["probes"][0]))

        with self.assertRaisesRegex(BenchmarkProfileError, "probe ids must be unique"):
            parse_benchmark_profile(value)

    def test_profile_rejects_unknown_dimensions(self) -> None:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        value["probes"][0]["dimension"] = "custom"

        with self.assertRaisesRegex(BenchmarkProfileError, "dimension must be one of"):
            parse_benchmark_profile(value)

    def test_profile_requires_each_probe_to_end_with_user_input(self) -> None:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        value["probes"][0]["messages"][-1]["role"] = "assistant"

        with self.assertRaisesRegex(BenchmarkProfileError, "must end with a user message"):
            parse_benchmark_profile(value)

    def test_profile_rejects_unknown_expectation_aspect(self) -> None:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        value["probes"][0]["expectations"][0]["aspect"] = "style"

        with self.assertRaisesRegex(BenchmarkProfileError, "aspect must be one of"):
            parse_benchmark_profile(value)

    def test_profile_requires_an_identity_expectation(self) -> None:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        for expectation in value["probes"][0]["expectations"]:
            expectation["aspect"] = "format"

        with self.assertRaisesRegex(BenchmarkProfileError, "at least one identity aspect"):
            parse_benchmark_profile(value)

    def test_response_request_rejects_transition_control_data(self) -> None:
        with self.assertRaisesRegex(BenchmarkProfileError, "unknown fields: after_response"):
            parse_benchmark_request(
                {
                    "protocol_version": 1,
                    "profile_id": "profile-1",
                    "probe_id": "probe-1",
                    "messages": [{"role": "user", "content": "Answer."}],
                    "after_response": {
                        "type": "append-memory",
                        "agent_identity": {"schema_version": 1},
                    },
                }
            )

    def test_transition_request_uses_a_separate_control_protocol(self) -> None:
        request = parse_transition_request(
            {
                "protocol_version": 1,
                "operation": "apply_transition",
                "profile_id": "profile-1",
                "probe_id": "probe-1",
                "transition": {
                    "type": "replace-agent-identity",
                    "agent_identity": {"schema_version": 1},
                },
            }
        )

        self.assertEqual(request.profile_id, "profile-1")
        self.assertEqual(request.transition.type, "replace-agent-identity")

        value = request.to_dict()
        value["transition"]["type"] = "append-memory"
        with self.assertRaisesRegex(BenchmarkProfileError, "type must be one of"):
            parse_transition_request(value)

    def test_runner_applies_transition_only_after_collecting_the_response(self) -> None:
        profile = parse_benchmark_profile(
            {
                "schema_version": 1,
                "profile_id": "transition-order",
                "statements": [{"id": "designation", "content": "ORBIT-A"}],
                "probes": [
                    {
                        "id": "before-update",
                        "dimension": "governance",
                        "messages": [{"role": "user", "content": "Current state?"}],
                        "expectations": [
                            {"type": "exact", "value": "BEFORE", "gate": True}
                        ],
                        "after_response": {
                            "type": "replace-agent-identity",
                            "agent_identity": {"schema_version": 1},
                        },
                    },
                    {
                        "id": "after-update",
                        "dimension": "retention",
                        "messages": [{"role": "user", "content": "Current state?"}],
                        "expectations": [
                            {"type": "exact", "value": "AFTER", "gate": True}
                        ],
                    },
                ],
            }
        )
        instance = _TransitionTrackingInstance()

        report = run_benchmark(profile, instance)

        self.assertEqual(report.score, 1.0)
        self.assertEqual(
            [event[0] for event in instance.events],
            ["respond", "apply_transition", "respond"],
        )
        self.assertNotIn("after_response", instance.events[0][1])
        self.assertNotIn("transition", instance.events[0][1])
        self.assertEqual(
            instance.events[1][1]["operation"], "apply_transition"
        )

    def test_command_instance_runs_every_probe_in_isolation(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        instance = CommandInstance(
            command=(sys.executable, str(INSTANCE)),
            instance_id="synthetic-reference",
            timeout_seconds=10,
        )

        report = run_benchmark(profile, instance)

        self.assertEqual(report.score, 1.0)
        self.assertEqual(report.errors, 0)
        self.assertEqual(set(report.dimension_scores.values()), {1.0})
        self.assertEqual(set(report.metric_scores.values()), {1.0})
        self.assertEqual(
            set(report.metric_scores),
            {
                "identity_recall",
                "behavioral_consistency",
                "conflict_resistance",
                "longitudinal_stability",
            },
        )
        self.assertEqual(len(report.results), len(profile.probes))
        self.assertTrue(
            all(result.metadata.get("fixture") == "synthetic" for result in report.results)
        )

    @unittest.skipIf(sys.platform == "win32", "requires POSIX process groups")
    def test_command_instance_timeout_terminates_descendants_and_is_recorded(
        self,
    ) -> None:
        profile = load_benchmark_profile(PROFILE)
        script = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "time.sleep(30)"
        )
        instance = CommandInstance(
            command=(sys.executable, "-c", script),
            instance_id="hung-descendant",
            timeout_seconds=0.05,
        )

        started = time.monotonic()
        report = run_benchmark(profile, instance)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 3.0)
        self.assertEqual(report.errors, len(profile.probes))
        self.assertTrue(
            all(
                result.error == "Instance command timed out after 0.05 seconds."
                for result in report.results
            )
        )

    def test_runner_records_an_instance_error_without_aborting_the_suite(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        instance = _FailingInstance()

        report = run_benchmark(profile, instance)

        self.assertEqual(report.score, 0.0)
        self.assertEqual(report.errors, len(profile.probes))
        self.assertTrue(all("unavailable" in result.error for result in report.results))

    def test_empty_instance_output_is_scored_instead_of_treated_as_transport_error(self) -> None:
        response = parse_instance_response(
            {"protocol_version": 1, "response": "", "metadata": {}}
        )

        self.assertEqual(response.response, "")

    def test_failed_gate_prevents_secondary_partial_credit(self) -> None:
        value = json.loads(PROFILE.read_text(encoding="utf-8"))
        expectation_values = value["probes"][2]["expectations"]
        profile = parse_benchmark_profile(value)
        probe = profile.probes[2]
        scorer = DeterministicScorer()
        results = tuple(
            scorer.score("Codex", expectation) for expectation in probe.expectations
        )

        self.assertTrue(expectation_values[0]["gate"])
        self.assertEqual([result.passed for result in results], [False, True])
        self.assertEqual(weighted_expectation_score(results), 0.0)

    def test_cli_writes_a_machine_readable_report(self) -> None:
        with TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            output = StringIO()
            with redirect_stdout(output):
                main(
                    [
                        "run",
                        str(PROFILE),
                        "--instance-id",
                        "synthetic-reference",
                        "--json-out",
                        str(report_path),
                        "--",
                        sys.executable,
                        str(INSTANCE),
                    ]
                )

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertIn("Score: 1.000", output.getvalue())
        self.assertEqual(report["profile_id"], "synthetic-vela-7")
        self.assertEqual(report["instance_id"], "synthetic-reference")
        self.assertEqual(report["score"], 1.0)
        self.assertIn("metric_scores", report)
        self.assertEqual(report["errors"], 0)

    def test_saved_responses_can_be_rescored_without_running_the_instance(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        source = run_benchmark(
            profile,
            CommandInstance(
                command=(sys.executable, str(INSTANCE)),
                instance_id="synthetic-source",
                timeout_seconds=10,
            ),
        )
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.json"
            source_path.write_text(
                json.dumps(source.to_dict()),
                encoding="utf-8",
            )

            rescored = rescore_saved_report(profile, source_path)

        self.assertEqual(rescored.score, 1.0)
        self.assertEqual(rescored.instance_id, "synthetic-source:rescored")
        self.assertTrue(
            all(result.metadata["rescored"] for result in rescored.results)
        )
        self.assertEqual(
            {result.metadata["source_instance_id"] for result in rescored.results},
            {"synthetic-source"},
        )

    def test_rescore_rejects_a_different_probe_set(self) -> None:
        profile = load_benchmark_profile(PROFILE)
        with TemporaryDirectory() as directory:
            source_path = Path(directory) / "source.json"
            source_path.write_text(
                json.dumps(
                    {
                        "profile_id": profile.profile_id,
                        "instance_id": "incomplete",
                        "results": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RescoreError, "probe set differs"):
                rescore_saved_report(profile, source_path)


class _FailingInstance:
    instance_id = "unavailable-instance"

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        del request
        raise InstanceError("instance unavailable")


class _TransitionTrackingInstance:
    instance_id = "transition-tracking-instance"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.updated = False

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        self.events.append(("respond", request.to_dict()))
        return InstanceResponse(response="AFTER" if self.updated else "BEFORE")

    def apply_transition(self, request: TransitionRequest) -> None:
        self.events.append(("apply_transition", request.to_dict()))
        self.updated = True


if __name__ == "__main__":
    unittest.main()
