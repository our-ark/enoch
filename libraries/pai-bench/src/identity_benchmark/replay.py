from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from identity_benchmark.contracts import (
    BenchmarkProfileError,
    BenchmarkRequest,
    InstanceResponse,
    JsonValue,
    parse_benchmark_report,
    parse_benchmark_request,
)


class ReplayError(ValueError):
    """Raised when a saved response cannot satisfy a benchmark request."""


def recorded_response(path: Path, request: BenchmarkRequest) -> InstanceResponse:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReplayError(f"could not load saved benchmark report: {error}") from error
    root = _mapping(value, "saved benchmark artifact")
    report_value = root.get("report", root)
    report = parse_benchmark_report(report_value)
    if report.profile_id != request.profile_id:
        raise ReplayError(
            "saved report profile_id does not match the benchmark request"
        )
    matches = tuple(
        result for result in report.results if result.probe_id == request.probe_id
    )
    if len(matches) != 1:
        raise ReplayError(
            f"saved report must contain one response for probe {request.probe_id!r}"
        )
    result = matches[0]
    if result.error:
        raise ReplayError(
            f"saved report probe {request.probe_id!r} has no valid response: "
            f"{result.error}"
        )
    metadata: dict[str, JsonValue] = dict(result.metadata)
    metadata.update(
        {
            "replayed": True,
            "source_instance_id": report.instance_id,
            "source_evaluator_id": report.evaluator_id,
        }
    )
    source_run_id = root.get("run_id")
    if isinstance(source_run_id, str) and source_run_id:
        metadata["source_run_id"] = source_run_id
    return InstanceResponse(response=result.response, metadata=metadata)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="identity-benchmark-replay",
        description="Replay one saved agent response without rerunning the target.",
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = parse_benchmark_request(json.load(sys.stdin))
        response = recorded_response(args.report, request)
        json.dump(
            {
                "protocol_version": response.protocol_version,
                "response": response.response,
                "metadata": response.metadata,
            },
            sys.stdout,
            ensure_ascii=False,
        )
    except (
        BenchmarkProfileError,
        ReplayError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        parser.exit(2, f"identity-benchmark-replay: {error}\n")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayError(f"{label} must be an object")
    return value


if __name__ == "__main__":
    sys.exit(main())
