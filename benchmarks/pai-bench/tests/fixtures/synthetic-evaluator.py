from __future__ import annotations

import json
import os
import re
import sys
import unicodedata


request = json.load(sys.stdin)
response = request["agent_response"]
expectations = request["probe"]["expectations"]


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


results: list[tuple[dict, bool]] = []
for expectation in expectations:
    kind = expectation["type"]
    expected = expectation["value"]
    if kind == "regex":
        passed = re.search(expected, response, flags=re.IGNORECASE | re.MULTILINE) is not None
    elif kind == "exact":
        passed = normalize(response) == normalize(expected)
    elif kind == "contains":
        passed = normalize(expected) in normalize(response)
    else:
        passed = normalize(expected) not in normalize(response)
    results.append((expectation, passed))

failed_gate = any(item.get("gate", False) and not passed for item, passed in results)
total = sum(float(item.get("weight", 1.0)) for item, _passed in results)
earned = sum(
    float(item.get("weight", 1.0))
    for item, passed in results
    if passed
)
score = 0.0 if failed_gate else earned / total

json.dump(
    {
        "protocol_version": 1,
        "score": score,
        "metadata": {
            "fixture": "synthetic-evaluator",
            "profile_id": request["profile_id"],
            "probe_id": request["probe"]["id"],
            "request_keys": sorted(request),
            "has_description": "description" in request,
            "arguments": sys.argv[1:],
            "model": os.environ.get("IDENTITY_BENCHMARK_EVALUATOR_MODEL", ""),
            "reasoning_effort": os.environ.get(
                "IDENTITY_BENCHMARK_EVALUATOR_REASONING_EFFORT", ""
            ),
            "rubric_version": os.environ.get(
                "IDENTITY_BENCHMARK_EVALUATOR_RUBRIC_VERSION", ""
            ),
            "state_home": os.environ.get("IDENTITY_BENCHMARK_STATE_HOME", ""),
        },
    },
    sys.stdout,
)
