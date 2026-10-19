# Synthetic analytics example

Seven actual cases exercise deterministic Python computation and JSON inputs: two each
for customer scoring, demand projection, and price schedules, plus one unmarked case
that reads a project note. This is synthetic data, not production data.

Install pytest-telescope 0.1.0a3, then run from this directory:

```sh
python -m pytest -q
python -m pytest_telescope runtime
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The unchanged second isolated run reuses all seven cases and its collection inventory.
Each actual case starts a fresh session, so even the session-scoped data directory
fixture is recreated per case.

Module-level literal markers select reusable scoring, demand, and pricing bundles.
Those bundles include the common helper, package initializer, conftest, and domain
Python/JSON inputs. Only the customer-scores case adds `data/scoring-check.json` with a
function marker. The unmarked project-note case sees and hashes the broad project
snapshot; it therefore reruns for every benchmark mutation.

| Change | Executed cases | Reused cases |
| --- | ---: | ---: |
| `demoapp/scoring.py` | 3: both scoring cases and broad case | 4 |
| `data/demand.json` | 3: both demand cases and broad case | 4 |
| `data/scoring-check.json` | 2: customer-scores case and broad case | 5 |
| `demoapp/common.py` | 7 | 0 |

The repository's `benchmarks/run.py` automates these mutations on fresh copies and
checks every individual case identity and outcome. It measures external elapsed/host
process CPU, and separately records collection and execution worker CPU. Runtime
preparation is measured separately. The [benchmark documentation](https://pytest-telescope.readthedocs.io/en/latest/benchmark/)
explains the results and their limits.
