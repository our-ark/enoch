from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from identity_benchmark.contracts import (
    BenchmarkProfile,
    BenchmarkRequest,
    InstanceResponse,
    JsonValue,
    TransitionRequest,
)
from identity_benchmark.runner import run_benchmark


class RescoreError(ValueError):
    """Raised when a saved report cannot be replayed against a profile."""


@dataclass(frozen=True)
class RecordedInstance:
    instance_id: str
    responses: dict[str, InstanceResponse]

    def respond(self, request: BenchmarkRequest) -> InstanceResponse:
        try:
            return self.responses[request.probe_id]
        except KeyError as error:
            raise RescoreError(
                f"saved report has no response for probe {request.probe_id!r}"
            ) from error

    def apply_transition(self, request: TransitionRequest) -> None:
        # Responses are already recorded after the original control-plane update.
        del request


def rescore_saved_report(profile: BenchmarkProfile, path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RescoreError(f"could not load saved benchmark report: {error}") from error
    root = _mapping(value, "saved benchmark report")
    if root.get("profile_id") != profile.profile_id:
        raise RescoreError(
            "saved report profile_id does not match the evaluation profile"
        )
    source_instance_id = root.get("instance_id")
    if not isinstance(source_instance_id, str) or not source_instance_id.strip():
        raise RescoreError("saved report instance_id must be non-empty text")
    raw_results = root.get("results")
    if not isinstance(raw_results, list):
        raise RescoreError("saved report results must be a list")

    responses: dict[str, InstanceResponse] = {}
    for index, raw_result in enumerate(raw_results):
        result = _mapping(raw_result, f"saved report results[{index}]")
        probe_id = result.get("probe_id")
        if not isinstance(probe_id, str) or not probe_id:
            raise RescoreError(f"saved report results[{index}].probe_id must be text")
        if probe_id in responses:
            raise RescoreError(f"saved report repeats probe {probe_id!r}")
        error = result.get("error", "")
        if error:
            raise RescoreError(
                f"saved report probe {probe_id!r} has no valid response: {error}"
            )
        response = result.get("response")
        if not isinstance(response, str):
            raise RescoreError(
                f"saved report probe {probe_id!r} response must be text"
            )
        metadata = result.get("metadata", {})
        if not isinstance(metadata, dict):
            raise RescoreError(
                f"saved report probe {probe_id!r} metadata must be an object"
            )
        replay_metadata = {
            str(key): _json_value(item, f"metadata.{key}")
            for key, item in metadata.items()
        }
        replay_metadata["rescored"] = True
        replay_metadata["source_instance_id"] = source_instance_id
        responses[probe_id] = InstanceResponse(
            response=response,
            metadata=replay_metadata,
        )

    expected_ids = {probe.id for probe in profile.probes}
    actual_ids = set(responses)
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise RescoreError(
            f"saved report probe set differs; missing={missing}, extra={extra}"
        )
    return run_benchmark(
        profile,
        RecordedInstance(
            instance_id=f"{source_instance_id}:rescored",
            responses=responses,
        ),
    )


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RescoreError(f"{label} must be an object")
    return value


def _json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{label}[]") for item in value]
    if isinstance(value, dict):
        return {
            str(key): _json_value(item, f"{label}.{key}")
            for key, item in value.items()
        }
    raise RescoreError(f"{label} is not JSON-compatible")
