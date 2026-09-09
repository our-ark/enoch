"""Verify published record hashes and the main CITB depth-two acceptance trace."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
C1 = "61723cd936c6d5f9a9ed163cf00321fc3fb79722"
B2 = "7ffaba854015d0854cfbb543ee934520ef2d5c30"


def read(name: str):
    return json.loads((ROOT / name).read_text())


def validate(report: dict) -> None:
    assert report["genesis_revision"] == C1 and report["source_revision"] == B2
    assert report["successful"] and report["requested_generations"] == 2
    assert report["genesis_tracked_changes"] == ""
    assert report["source_tracked_changes_before"] == report["source_tracked_changes_after"] == ""
    assert len(report["births"]) == 2 and len(report["validation_gates"]) == 4
    parent = B2
    for birth in report["births"]:
        assert birth["parent_at_birth"] == parent
        assert birth["source_unchanged"] and birth["dependency_declarations_unchanged"]
        assert not birth["shared_libraries_copied"]
        parent = birth["evaluated_commit"]
    assert [(gate["depth"], gate["phase"]) for gate in report["validation_gates"]] == [
        (1, "body"), (1, "provenance"), (2, "body"), (2, "provenance")]
    for gate in report["validation_gates"]:
        count = gate["unittest"]
        assert gate["returncode"] == 0 and count["status"] == "OK"
        assert count["tests_run"] == 859 and count["skipped"] == 8
        assert count["failures"] == count["errors"] == 0
    assert report["extension_conformance"]["returncode"] == 0


def main() -> None:
    manifest = read("record-manifest.json")
    for name, record in manifest["records"].items():
        path = (ROOT / name).resolve()
        assert path.is_relative_to(ROOT), name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["public_sha256"], name
    index = read("results/instance-snapshot-evidence-index.json")
    assert index["snapshots"] == {"C1": C1, "B2": B2}
    for item in index["records"].values():
        assert hashlib.sha256((ROOT / item["path"]).read_bytes()).hexdigest() == item["sha256"]
    validate(read("results/instance-snapshot-descent-network.json"))
    if (ROOT / "results/ci-b2-recursive-descent.json").exists():
        validate(read("results/ci-b2-recursive-descent.json"))
    failed = read("results/ci-noah-name-baseline/recursive-descent.json")
    assert not failed["successful"] and not failed["births"]
    assert failed["validation_gates"][0]["unittest"]["failures"] == 3
    assert read("results/recursive-full-body-network.json")["exit_code"] == 1
    assert read("results/recursive-telegram-aligned.json")["exit_code"] == 0
    print(f"Verified {len(manifest['records'])} published files and two-generation acceptance records.")


if __name__ == "__main__":
    main()
