# Synthetic analytics example

Seven actual cases exercise deterministic Python computation and JSON inputs: two each
for customer scoring, demand projection, and price schedules, plus one unmarked case
that reads a project note. This is synthetic data, not production data.

Install pytest-telescope 0.1.0a4 and Bubblewrap, then run from this directory:

```sh
python -m pytest -q
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The unchanged second run reuses the six scoped cases and their collection inventory, and
runs the ordinary project-note case again. Each scoped case starts a fresh sandbox and
session, so even the session-scoped data directory fixture is recreated per case.

Module-level literal markers select reusable scoring, demand, and pricing bundles, so
those modules are collected only inside sandboxes. The bundles include the common helper,
package initializer, conftest, and domain Python/JSON inputs. Only the customer-scores
case adds `data/scoring-check.json` with a function marker. The unmarked project-note
case is ordinary.

| Change | Executed cases | Reused cases |
| --- | ---: | ---: |
| `demoapp/scoring.py` | 3: both scoring cases and the ordinary case | 4 |
| `data/demand.json` | 3: both demand cases and the ordinary case | 4 |
| `data/scoring-check.json` | 2: customer-scores case and the ordinary case | 5 |
| `data/project-note.txt` | 1: the ordinary case | 6 |
| `demoapp/common.py` | 7 | 0 |
