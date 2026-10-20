# Synthetic analytics example

Seven actual cases exercise deterministic Python computation and JSON inputs: two each
for customer scoring, demand projection, and price schedules, plus one unmarked case
that reads a project note. This is synthetic data, not production data.

This example describes accepted replacement behavior, currently **UNQUALIFIED**.
The historical benchmark used Docker and broad unmarked caching. It does not qualify
these new semantics. After candidate installation and explicit scoped preparation,
the intended commands from this directory are:

```sh
python -m pytest -q
python -m pytest_telescope runtime
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

An unchanged second run executes the ordinary note case and reuses six scoped successes.
Each scoped case starts a fresh session by default, recreating its session fixture.
Ordinary tests use their one suite session and always collect fresh.

Module-level literal markers select reusable scoring, demand, and pricing bundles.
Those bundles include the common helper, package initializer, conftest, and domain
Python/JSON inputs. Only the customer-scores case adds `data/scoring-check.json` with a
function marker. The unmarked project-note case runs ordinary pytest fresh on every invocation, without
a broad snapshot or reusable result.

| Change | Executed cases | Reused cases |
| --- | ---: | ---: |
| `demoapp/scoring.py` | 3: both scoring cases and ordinary case | 4 |
| `data/demand.json` | 3: both demand cases and ordinary case | 4 |
| `data/scoring-check.json` | 2: customer-scores case and ordinary case | 5 |
| `demoapp/common.py` | 7 | 0 |

The existing `benchmarks/run.py` and retained receipts describe the historical Docker
contract. Replacement benchmarking must update its expected outcomes and compare matched
fresh sessions/scopes/budgets at 1/2/4 workers, including per-invocation runtime freezing,
planning, materialization and cleanup. Ordinary CPU remains unknown without separately
qualified measurement. The [benchmark documentation](https://pytest-telescope.readthedocs.io/en/latest/benchmark/)
explains the results and their limits.
