#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "libraries" / "pai-bench" / "src"))

from identity_benchmark.pai_bench import (  # noqa: E402
    PaiBenchError,
    write_pai_bench,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate or verify the frozen PAI-Bench v1.0 release."
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "benchmarks"
            / "pai-bench"
            / "v1.0"
        ),
    )
    args = parser.parse_args()
    try:
        paths = write_pai_bench(args.output.resolve(), check=args.check)
    except PaiBenchError as error:
        parser.exit(1, f"pai-bench: {error}\n")
    verb = "Verified" if args.check else "Generated"
    print(f"{verb} PAI-Bench v1.0: {len(paths)} files.")


if __name__ == "__main__":
    main()
