#!/usr/bin/env bash
# Reproduce C1/B2 in a new directory; never overwrite retained evidence.
set -euo pipefail
KIT="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
if [ "$#" -ne 1 ] || [ -e "$1" ]; then
  printf '%s\n' 'Usage: bash reproduce.sh NEW_OUTPUT_DIRECTORY (must not exist)' >&2
  exit 2
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Use CPython 3.12"'
mkdir -p -- "$1"
cd -- "$1"
WORK="$(pwd)"
git clone https://github.com/our-ark/genesis.git genesis
git -C genesis checkout --detach 61723cd936c6d5f9a9ed163cf00321fc3fb79722
git clone https://github.com/our-ark/enoch.git enoch
git -C enoch checkout --detach 7ffaba854015d0854cfbb543ee934520ef2d5c30
"$PYTHON_BIN" -m venv .venv
PYTHON="$WORK/.venv/bin/python"
"$PYTHON" -m pip install --disable-pip-version-check --require-hashes \
  -r enoch/.github/requirements/test-build.txt
export GENESIS_CACHE_DIR="$WORK/cache/genesis"
export PIP_CACHE_DIR="$WORK/cache/pip"
"$PYTHON" "$KIT/run_unittest.py" --root genesis --start tests \
  --output results/reproduction-genesis.json
"$PYTHON" "$KIT/run_unittest.py" --root enoch --start tests --top . \
  --output results/reproduction-enoch-core.json
for library in claude github launchd provider-kit skill-catalog slack systemd telegram telegram-vision; do
  "$PYTHON" "$KIT/run_unittest.py" --root enoch --start "libraries/$library/tests" \
    --output "results/reproduction-provider-$library.json"
done
"$PYTHON" "$KIT/run_command.py" --output results/reproduction-depth2-command.json -- \
  "$PYTHON" genesis/scripts/verify_enoch_descent.py --source enoch \
  --ref 7ffaba854015d0854cfbb543ee934520ef2d5c30 --generations 2 \
  --report results/reproduction-depth2.json
printf '%s\n' "New reproduction records: $WORK/results"
