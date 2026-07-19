from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from enoch.immune import run_immune_system


def main() -> None:
    result = run_immune_system(Path.cwd())
    print(
        json.dumps(
            {
                "passed": result.passed,
                "command": result.command,
                "output": result.output,
                "diagnosis": asdict(result.diagnosis),
                "checks": [asdict(check) for check in result.checks],
            },
            sort_keys=True,
        )
    )
    raise SystemExit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
