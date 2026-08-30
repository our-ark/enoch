from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {
            key
            for item in value.values()
            for key in _keys(item)
        }
    if isinstance(value, list):
        return {key for item in value for key in _keys(item)}
    return set()


parser = argparse.ArgumentParser()
parser.add_argument("--profile", type=Path, required=True)
args = parser.parse_args()
request = json.load(sys.stdin)

if request.get("operation") == "apply_transition":
    json.dump({"protocol_version": 1, "applied": True}, sys.stdout)
else:
    profile = json.loads(args.profile.read_text(encoding="utf-8"))
    json.dump(
        {
            "protocol_version": 1,
            "response": "ORBIT-A",
            "metadata": {
                "profile_path": str(args.profile.resolve()),
                "profile_keys": sorted(_keys(profile)),
                "request_keys": sorted(request),
            },
        },
        sys.stdout,
    )
