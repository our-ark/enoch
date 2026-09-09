#!/usr/bin/env python3
"""Run an experiment command verbatim and preserve its exit code and output."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
command = args.command[1:] if args.command[:1] == ["--"] else args.command
output = args.output.resolve()
output.parent.mkdir(parents=True, exist_ok=True)
started = datetime.now(timezone.utc).isoformat()
start = time.monotonic()
with output.with_suffix(".log").open("w") as log:
    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False)
report = {
    "command": command,
    "started_at_utc": started,
    "elapsed_seconds": round(time.monotonic() - start, 3),
    "exit_code": result.returncode,
    "successful": result.returncode == 0,
}
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
raise SystemExit(result.returncode)
