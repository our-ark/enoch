#!/usr/bin/env python3
"""Exercise two full-body births without patching either frozen implementation."""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "sources/genesis/src"))
from genesis.creator import create_agent, inspect_source


def head(repo):
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


parser = argparse.ArgumentParser()
parser.add_argument("--source", type=Path, default=ROOT / "sources/enoch")
parser.add_argument("--source-ref", default="e40b28782f5c2633b7b28dfd48e0d7abf1456976")
args = parser.parse_args()
source = args.source.resolve()
expected = args.source_ref
if head(source) != expected:
    raise RuntimeError("The reference-body snapshot differs from the frozen revision")
original = tomllib.loads((source / "genesis.toml").read_text())
requirements = [d["requirement"] for d in original["runtime_dependencies"]]
records = []
with tempfile.TemporaryDirectory(prefix="cain-recursive-full-body-") as tmp:
    ancestor = "enoch"
    for depth, name in enumerate(("cainchild", "caingrandchild"), start=1):
        parent = head(source)
        result = create_agent(
            name=name,
            ancestor=ancestor,
            mission=f"Controlled mechanism validation at descent depth {depth}; no live services.",
            repo=Path(tmp) / name,
            source=source,
            source_ref=parent,
        )
        inspection = inspect_source(ancestor=name, source=result.repo, source_ref=head(result.repo))
        manifest = tomllib.loads((result.repo / "genesis.toml").read_text())
        assert result.parent_commit_sha == parent
        assert inspection.package == name
        assert [d["requirement"] for d in manifest["runtime_dependencies"]] == requirements
        assert not (result.repo / "libraries").exists()
        assert head(source) == parent
        record = {key: str(value) for key, value in asdict(result).items() if key not in ("repo", "launcher")}
        record.update(depth=depth, evaluated_commit=head(result.repo), source_unchanged=True,
                      dependency_declarations_unchanged=True, copied_libraries=False,
                      inherited_validation="passed at both birth gates")
        records.append(record)
        print(json.dumps(record), flush=True)
        source = result.repo
        ancestor = name
print(json.dumps({"completed_births": len(records), "maximum_depth": 2, "successful": True}))
