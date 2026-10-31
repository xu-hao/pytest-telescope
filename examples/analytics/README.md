# Synthetic analytics example

This example has six scoped tests that calculate customer scores, demand projections
and price schedules from synthetic JSON data. A seventh test reads a project note and
runs every time. Use it to try changing inputs and see which tests rerun.

Install pytest-telescope 0.1.0a6 and Bubblewrap, then run from this directory:

```sh
python -m pytest -q
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The unchanged second run reuses the six scoped cases and their collection inventory, and
runs the ordinary project-note case again. Each scoped case starts a fresh sandbox and
session, so even the session-scoped data directory fixture is recreated per case.

Each scoped module declares its scoring, demand or pricing bundle in a module-level
marker, so pytest collects those modules only inside sandboxes. The bundles include the common helper,
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
