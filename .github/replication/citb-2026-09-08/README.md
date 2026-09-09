# Code Is the Body: frozen replication records

This directory accompanies *Code Is the Body: Agent-Owned Software Bodies for
Recursive Evolution and Descent*. It preserves the 8 September 2026 local
composition experiment and a separately labeled successful Linux CI rerun.
It is outside Enoch's declared inheritable body paths. Publishing these files
does not change the evaluated body or turn the documentation commit into B2.

## Exact public source inputs

| Label | Source | Evaluated revision |
| --- | --- | --- |
| C1 | [Genesis](https://github.com/our-ark/genesis/commit/61723cd936c6d5f9a9ed163cf00321fc3fb79722) | `61723cd936c6d5f9a9ed163cf00321fc3fb79722` |
| B2 | [Enoch](https://github.com/our-ark/enoch/commit/7ffaba854015d0854cfbb543ee934520ef2d5c30) | `7ffaba854015d0854cfbb543ee934520ef2d5c30` |

Use these commits, not a moving branch or package-version label. No new release
tag is required to identify the inputs. Generated descendants are temporary:
their commit identifiers are retained as provenance, not published repositories.
Timestamps, temporary paths, and generated commit hashes can differ on a rerun.

## Evidence and scope

| Record | Environment | Outcome |
| --- | --- | --- |
| `results/instance-snapshot-descent-network.json` | CPython 3.12.14, macOS 26.4.1 ARM64 | Two accepted births, four gates; each 851 passed, eight skipped, no failures/errors |
| `results/ci-b2-recursive-descent.json` | CPython 3.12.14, Linux x86-64, GitHub Actions | Same C1/B2 inputs; two accepted births, four gates; each 851 passed, eight skipped, no failures/errors |
| `results/instance-snapshot-evidence-index.json` | Original local component and composition records | Per-test outcomes, focused subsets, exact ancestry, report checksums |

The Linux confirmation is [run 34297984409](https://github.com/our-ark/enoch/actions/runs/34297984409),
started 9 September UTC / 8 September Pacific. Its Python 3.11-3.14 component
jobs and Python 3.12 depth-two job all passed. The retained report is copied
here so the evidence does not depend on the CI artifact's 30-day retention.
The original macOS trace remains the paper's primary table; the CI trace is
a separate confirmation, not additional independent samples.

Every birth records the exact parent, birth, and evaluated revisions, preserves
dependency declarations, leaves its source unchanged, and excludes shared
library sources. The first birth uses frozen local library paths; the second
resolves inherited public dependency pins. Seven skipped tests need excluded
repository infrastructure and one needs excluded vision-library source; all
eight pass in the original full-source suite. A generated extension passes
eight conformance tests against source Enoch, not a live installed descendant.

The local component inventory is 44 Genesis tests, 859 Enoch core tests, and
118 tests across nine libraries. Focused subsets and repeated birth gates are
not added into a headline total. Reasoning, chat, review, and selected validation
responses use fixtures; results do not establish live adoption, autonomous
improvement, population specialization, or a security sandbox.

## Check the retained records without executing agent code

```sh
python3 verify_records.py
```

`record-manifest.json` includes original and published SHA-256 values. Home,
workspace, and temporary path prefixes have been replaced by `<HOME>`,
`<WORKSPACE>`, `<TEMP>`, or `<CI_HOME>`. Test IDs, counts, revision IDs, timestamps,
and output apart from those prefixes are retained. The public evidence index
uses checksums of the public copies; the manifest retains the original hashes.
Original private working copies were not overwritten. No installed-agent state
or account credentials are included. These are path-redacted records, not a
claim that the published files are byte-identical to the originals.

## Reproduce the current frozen checks

Requirements: Git, CPython 3.12 (the recorded version is 3.12.14), venv/pip,
and network access to public GitHub and PyPI dependencies. No model, chat, or
forge credentials are needed. The build backend is installed from B2's
hash-pinned requirements; transitive runtime packages are not fully hash-locked.
The script executes the selected source's tests and creates temporary bodies.
Run it in a disposable environment after inspecting those public sources.

From this directory, choose a new, nonexistent output directory:

```sh
bash reproduce.sh /absolute/path/to/new-citb-run
```

Set `PYTHON_BIN` if Python 3.12 has another executable name. The script clones
and checks out C1/B2, creates a fresh venv, runs the component suites, and invokes:

```sh
python genesis/scripts/verify_enoch_descent.py \
  --source enoch --ref 7ffaba854015d0854cfbb543ee934520ef2d5c30 \
  --generations 2 --report results/reproduction-depth2.json
```

Outputs are new files under the requested directory; published records are
never overwritten. Commands do not push repositories or launch live agents.
Wall times include dependency/cache costs and are not a performance benchmark.

## Historical failure evidence

1. **Stale dependency declaration.** With Genesis
   `ab7159935692141a9e0fb8fba474b3c58eafcc46` and Enoch
   `e40b28782f5c2633b7b28dfd48e0d7abf1456976`, one birth succeeded and the next
   failed with 857 discovered tests, one error, and seven skips. The inherited
   Telegram pin `9257a57a140950db2b3b3c242c7d2bbc622e26e1` lacked `bot_peers`.
   See `results/recursive-full-body-network.json` and its `.log`.
2. **One-pin control.** A separate local variant changed only the Telegram
   requirement in `genesis.toml` to
   `13c7b266f70c8c5c3b1ba3907130000e6e23966f`. Both births passed; see
   `results/recursive-telegram-aligned.json` and `.log`. Its experimental commit
   `0a3708e5565a4df3afa9279f532c45f681e01cc2` is not an upstream release. For
   reproduction, make and commit that one-line change in a disposable baseline
   clone and pass the resulting hash to the included `recursive_descent.py`.
   That script expects the old Genesis checkout under `sources/genesis` and
   the baseline Enoch checkout under `sources/enoch`, relative to the script.
   Use `run_command.py` to capture its output. Successful validation output was
   not exposed by the old creator API, so do not infer zero skips for that control.
3. **Identity-sensitive inherited tests.** The later Enoch parent
   `4ca144fde569f51335df5b64159fd2061d3fae95` passed ordinary CI but rejected its
   first Noah body with three test failures. See
   `results/ci-noah-name-baseline/recursive-descent.json` and
   [the original CI run](https://github.com/our-ark/enoch/actions/runs/34209509255).
   B2 changes three tests to use body-relative display names and exact output
   assertions; no production body behavior or Genesis rule changes in B2.

The `instance-snapshot-descent*` files without `network` record an initial
dependency/DNS preflight failure before any validation gate. They are retained
for transparency and are not counted as a body-validation failure or success.
The newer C1/B2 check is not the one-line control: other changes intervene.

This package supports mechanism inspection and reproduction. It is not a
population experiment, a complete environment image, or a formal safety proof.
