# Synthetic analytics example

Three targets each contain two tests. Customer scoring, demand projection, and
price schedules perform deterministic Python computation on JSON inputs. Every
target depends on `demoapp/common.py`; resource and application modules are
otherwise separate. This is synthetic qualification data, not production data.

Install pytest-telescope from the repository, then run from this directory:

```sh
python -m pytest -q
python -m pytest_telescope runtime
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The unchanged second isolated run should reuse all three successful targets.
Changing `demoapp/scoring.py` affects scoring only. Changing `data/demand.json`
affects demand only. Changing `demoapp/common.py` affects all three. The
`benchmarks/run.py` harness in the repository automates these scenarios on a copy
and records actual counts and externally measured elapsed/controller CPU time.

The manifest declares conftest, the package initializer, the shared helper, each
test module, and each target's Python/JSON inputs. The target is one fresh pytest
session: its two cases share the fixture, and no fixture state crosses targets.
