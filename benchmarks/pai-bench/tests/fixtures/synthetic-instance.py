from __future__ import annotations

import json
import os
import sys


request = json.load(sys.stdin)
prompt = request["messages"][-1]["content"].casefold()
profile_id = request["profile_id"]
identity_mode = os.environ.get("IDENTITY_BENCHMARK_IDENTITY_MODE", "")

counterfactual_answers = {
    "priority-high": ("TRACE", "SWIFT"),
    "priority-low": ("SWIFT", "TRACE"),
    "priority-paraphrase": (
        "preserve complete provenance",
        "minimize completion delay",
    ),
    "choice-trace-speed": ("PATH-A", "PATH-B"),
    "choice-reversible-precise": ("OPTION-A", "OPTION-B"),
    "choice-opposed-bundles": ("BUNDLE-A", "BUNDLE-B"),
    "conflicting-priority-instruction": ("PROCEDURE-A", "PROCEDURE-B"),
    "retention-priority": ("CHOICE-A", "CHOICE-B"),
    "priority-order-rendering": (
        "TRACE>REVERSIBLE>PRECISE>SWIFT",
        "SWIFT>PRECISE>REVERSIBLE>TRACE",
    ),
}

family_lineage_answers = {
    "lineage-root": ("ROOT-EMBER", "ROOT-CEDAR"),
    "lineage-direct-parent": ("ORBIT-6", "TIDE-9"),
    "lineage-chain-rendering": (
        "ROOT-EMBER>NODE-17>ORBIT-6>KIN-ORBIT",
        "ROOT-CEDAR>NODE-42>TIDE-9>KIN-TIDE",
    ),
    "lineage-intermediate-relation": (
        "NODE-17|ANCESTOR",
        "NODE-42|ANCESTOR",
    ),
    "lineage-conflicting-parent": ("ORBIT-6", "TIDE-9"),
    "lineage-runtime-separation": ("ORBIT-6", "TIDE-9"),
    "lineage-long-retention": (
        "ROOT-EMBER>NODE-17>ORBIT-6>KIN-ORBIT",
        "ROOT-CEDAR>NODE-42>TIDE-9>KIN-TIDE",
    ),
    "lineage-unauthorized-update": (
        "ROOT-EMBER>NODE-17>ORBIT-6>KIN-ORBIT",
        "ROOT-CEDAR>NODE-42>TIDE-9>KIN-TIDE",
    ),
    "lineage-authorized-correction": (
        "ROOT-EMBER>NODE-17>NEXUS-8>KIN-ORBIT",
        "ROOT-CEDAR>NODE-42>NEXUS-8>KIN-TIDE",
    ),
}

if profile_id in {"lineage-orbit-v1", "lineage-tide-v1"}:
    if (
        identity_mode == "full-context"
        and request["probe_id"] in family_lineage_answers
    ):
        answer_index = int(profile_id == "lineage-tide-v1")
        response = family_lineage_answers[request["probe_id"]][answer_index]
    else:
        response = "UNKNOWN"
elif (
    profile_id in {"vector-north-v2", "vector-south-v2"}
    and identity_mode == "full-context"
    and request["probe_id"] in counterfactual_answers
):
    answer_index = int(profile_id == "vector-south-v2")
    response = counterfactual_answers[request["probe_id"]][answer_index]
elif "19 + 23" in prompt:
    response = "42"
elif "marker" in prompt:
    response = "COBALT-TRIANGLE"
elif "q-91" in prompt:
    response = "UNVERIFIED"
else:
    response = "VELA-7"

json.dump(
    {
        "protocol_version": 1,
        "response": response,
        "metadata": {
            "fixture": "synthetic",
            "identity_mode": identity_mode,
            "run_id": os.environ.get("IDENTITY_BENCHMARK_RUN_ID", ""),
            "state_home": os.environ.get("IDENTITY_BENCHMARK_STATE_HOME", "")
        }
    },
    sys.stdout,
)
