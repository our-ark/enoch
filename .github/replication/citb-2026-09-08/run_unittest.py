#!/usr/bin/env python3
"""Record one unchanged unittest suite from an isolated source snapshot."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.outcomes = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.outcomes.append({"test": test.id(), "status": "passed"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.outcomes.append({"test": test.id(), "status": "failed"})

    def addError(self, test, err):
        super().addError(test, err)
        self.outcomes.append({"test": test.id(), "status": "error"})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.outcomes.append({"test": test.id(), "status": "skipped", "reason": reason})


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--start", default="tests")
    parser.add_argument("--top")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    sys.path.insert(0, str(root))
    before = git(root, "status", "--porcelain", "--untracked-files=no")
    revision = git(root, "rev-parse", "HEAD")
    start = time.monotonic()
    suite = unittest.TestLoader().discover(args.start, top_level_dir=args.top)
    with output.with_suffix(".log").open("w") as stream:
        result = unittest.TextTestRunner(
            stream=stream, verbosity=2, resultclass=RecordingResult
        ).run(suite)
    report = {
        "source_revision": revision,
        "source_tracked_changes_before": before,
        "source_tracked_changes_after": git(root, "status", "--porcelain", "--untracked-files=no"),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "start_directory": args.start,
        "top_level_directory": args.top,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": len(result.unexpectedSuccesses),
        "successful": result.wasSuccessful(),
        "outcomes": result.outcomes,
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "outcomes"}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
