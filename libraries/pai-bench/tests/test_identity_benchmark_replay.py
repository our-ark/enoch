from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from identity_benchmark.contracts import BenchmarkRequest, Message
from identity_benchmark.replay import ReplayError, recorded_response


class IdentityBenchmarkReplayTests(unittest.TestCase):
    def test_replays_response_from_experiment_run_wrapper(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            path.write_text(
                json.dumps({"run_id": "run-0042", "report": self._report()}),
                encoding="utf-8",
            )

            response = recorded_response(path, self._request())

        self.assertEqual(response.response, "recorded answer")
        self.assertTrue(response.metadata["replayed"])
        self.assertEqual(response.metadata["source_run_id"], "run-0042")
        self.assertEqual(response.metadata["source_instance_id"], "source-agent")

    def test_rejects_profile_mismatch(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(self._report()), encoding="utf-8")

            with self.assertRaisesRegex(ReplayError, "profile_id"):
                recorded_response(
                    path,
                    BenchmarkRequest(
                        profile_id="different-profile",
                        probe_id="probe-one",
                        messages=(Message(role="user", content="question"),),
                    ),
                )

    @staticmethod
    def _request() -> BenchmarkRequest:
        return BenchmarkRequest(
            profile_id="profile-one",
            probe_id="probe-one",
            messages=(Message(role="user", content="question"),),
        )

    @staticmethod
    def _report() -> dict[str, object]:
        return {
            "benchmark_version": 1,
            "instance_protocol_version": 1,
            "profile_id": "profile-one",
            "instance_id": "source-agent",
            "evaluator_id": "source-judge",
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": "2026-01-01T00:00:01Z",
            "score": 1.0,
            "dimension_scores": {"recognition": 1.0},
            "metric_scores": {},
            "errors": 0,
            "results": [
                {
                    "probe_id": "probe-one",
                    "dimension": "recognition",
                    "score": 1.0,
                    "weight": 1.0,
                    "response": "recorded answer",
                    "expectations": [],
                    "metadata": {"source": "fixture"},
                    "evaluation_metadata": {},
                    "component_scores": {"identity": 1.0},
                    "error": "",
                }
            ],
        }
