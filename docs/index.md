# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Put input declarations beside your tests. A test with a declaration runs in a Linux
[Bubblewrap](https://github.com/containers/bubblewrap) sandbox that shows it only its
declared project files, and its success is cached. Change a declared input to rerun that
case; unchanged cases reuse their successes. Every other test runs as ordinary pytest.

**Undeclared tests stay ordinary.** A test without a literal `inputs`, `bundles` or
`distributions` declaration runs in a normal pytest session every time, with no sandbox
and no cache. A literal `@pytest.mark.telescope(inputs=[...])` or `bundles=[...]` makes a
case **scoped**: it sees exactly those project files, read-only, while reusing your
installed Python environment read-only. The [execution contract](bubblewrap.md) states
exactly what a scoped test can see.

## Install

This tutorial uses **0.1.0a5**. You need Linux, Python 3.11+, and the `bwrap` command
from Bubblewrap (for example `sudo apt-get install bubblewrap`) on a host that lets it
create user namespaces. There is no runtime to build and no container daemon. Version
0.1.0a3 and earlier used Docker and are documented by their release tags.

```sh
mkdir telescope-demo
cd telescope-demo
python -m venv .venv
. .venv/bin/activate
python -m pip install pytest-telescope==0.1.0a5
mkdir -p app data tests
```

## Copy these files

The complete project is small:

```text
telescope-demo/
├── pyproject.toml
├── app/
│   ├── __init__.py
│   └── pricing.py
├── data/
│   ├── red.json
│   ├── blue.json
│   └── currency.json
├── notes.txt
└── tests/
    └── test_prices.py
```

Start with the tests. Each marker declares the files for that individual case:

### `tests/test_prices.py`

```python
from pathlib import Path

import pytest
from app.pricing import quote


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red():
    assert quote("red", 0)["amount"] == 0
    assert quote("red", 2)["amount"] == 2 * quote("red", 1)["amount"] > 0
    assert quote("red", 1)["currency"] in {"USD", "EUR"}


@pytest.mark.telescope(inputs=["data/blue.json"], bundles=["pricing"])
def test_blue():
    assert quote("blue", 0)["amount"] == 0
    assert quote("blue", 2)["amount"] == 2 * quote("blue", 1)["amount"] > 0
    assert quote("blue", 1)["currency"] in {"USD", "EUR"}


def test_project_note():
    assert Path("notes.txt").read_text().strip()
```

### `pyproject.toml`

```toml
[tool.pytest.ini_options]
pythonpath = ["."]

[tool.pytest_telescope]
test_roots = ["tests"]

[tool.pytest_telescope.defaults]
inputs = ["app/__init__.py"]

[tool.pytest_telescope.bundles.pricing]
inputs = ["app/pricing.py", "data/currency.json"]
```

### `app/__init__.py`

```python
"""Small pricing application for the documentation quickstart."""
```

### `app/pricing.py`

```python
import json
from pathlib import Path


def quote(product, quantity):
    price = json.loads(Path("data", f"{product}.json").read_text())["unit_price"]
    currency = json.loads(Path("data/currency.json").read_text())["code"]
    return {"amount": quantity * price, "currency": currency}
```

### `data/red.json`

```json
{"unit_price": 12}
```

### `data/blue.json`

```json
{"unit_price": 5}
```

### `data/currency.json`

```json
{"code": "USD"}
```

### `notes.txt`

```text
A project file read by an ordinary test.
```

The red and blue tests are in the **same file**, but have separate scopes and cache
results. The pricing bundle adds shared application code and currency data. Project
defaults add the package initializer. The containing test source and `pyproject.toml`
are included automatically; conftest files and other helpers need explicit declarations.

The unmarked note test is **ordinary**: it runs in a normal pytest session with your whole
checkout, every time. Defaults do not make it scoped, and nothing about it is cached. An
explicit empty `inputs=[]` makes a test scoped with only automatic inputs plus defaults;
a `cache=False` marker alone leaves a test ordinary.

## Run cold, then warm

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

`plan` previews static routes and scopes; it does not import project code or enumerate
real parametrized cases. `run` runs the ordinary note test in one ordinary pytest
session, then executes each scoped miss in its own sandbox.

The first run executes **3 cases**. The second executes the ordinary case again and
reuses the **2 scoped cases**. The JSON summary separates routes, executed and reused
outcomes, and fresh and cached passes. A reused case is a previous result, not a test
executed on the current run. These selected fields are from the cold and warm summaries:

```json
{"counts": {"executed": 3, "reused": 0}, "route_counts": {"ordinary": {"executed": 1}, "scoped": {"executed": 2, "reused": 0}}}
{"counts": {"executed": 1, "reused": 2}, "route_counts": {"ordinary": {"executed": 1}, "scoped": {"executed": 0, "reused": 2}}}
```

## See what reruns

Run the following steps in order with the same pytest arguments. Each change passes the
tests while demonstrating a different invalidation boundary.

**One narrow resource:** change the red price.

```sh
python -c 'from pathlib import Path; Path("data/red.json").write_text("{\"unit_price\": 15}\n")'
python -m pytest_telescope run --explain -- -q
```

**Shared resource:** change the currency used by both scoped cases.

```sh
python -c 'from pathlib import Path; Path("data/currency.json").write_text("{\"code\": \"EUR\"}\n")'
python -m pytest_telescope run --explain -- -q
```

**Undeclared file:** change the project note.

```sh
python -c 'from pathlib import Path; Path("notes.txt").write_text("An updated project note.\n")'
python -m pytest_telescope run --explain -- -q
```

| Run | Fresh passes | Cached passes | Why |
| --- | --- | --- | --- |
| Cold | 3 | 0 | No successes recorded yet. |
| Unchanged | 1 | 2 | The ordinary note test always runs; both scopes match. |
| Red price changed | 2 | 1 | Red reruns with the ordinary test; blue reuses. |
| Shared currency changed | 3 | 0 | Both pricing consumers rerun with the ordinary test. |
| Note changed | 1 | 2 | No scoped case declares the note. |

Changing the common test file affects every scoped case in that file. Changing
`pyproject.toml` affects every scoped case because configuration is always an input.
Installing or upgrading a package in the environment also reruns scoped cases, because
their cache key includes the interpreter and the installed distributions, and how an
editable install of the project reaches into it.

## Give each parameter its own input

Create this file with these complete contents:

### `tests/test_variants.py`

```python
import pytest
from app.pricing import quote


@pytest.mark.parametrize(
    "product",
    [
        pytest.param(
            "red",
            marks=pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"]),
            id="red",
        ),
        pytest.param(
            "blue",
            marks=pytest.mark.telescope(inputs=["data/blue.json"], bundles=["pricing"]),
            id="blue",
        ),
    ],
)
def test_variant(product):
    assert quote(product, 2)["amount"] == 2 * quote(product, 1)["amount"] > 0
```

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

There are now **5 actual cases**. The first invocation executes the two new variants and
the ordinary note test; the original red and blue cases reuse. The second reuses all four
scoped cases and runs the note test again. Each `pytest.param` declaration belongs to its
actual variant: the red variant cannot read blue's JSON.

Change the red price once more to check parameter-specific reuse:

```sh
python -c 'from pathlib import Path; Path("data/red.json").write_text("{\"unit_price\": 18}\n")'
python -m pytest_telescope run --explain -- -q
```

The red case, red variant and ordinary note test execute: **3 fresh passes**. The blue
case and blue variant reuse: **2 cached passes**.

## Observe an undeclared file

`notes.txt` exists in the project, but a scoped case only receives its own inputs.
Create this intentional failing test:

### `tests/test_denied.py`

```python
from pathlib import Path

import pytest


@pytest.mark.telescope(inputs=[])
def test_denied():
    Path("notes.txt").read_text()
```

Run just that case:

```sh
python -m pytest_telescope run -- tests/test_denied.py::test_denied
```

The command exits with status **1** and reports one failed case with
`FileNotFoundError`; the other five cases are unselected:

```json
{"collection": {"collected": 6}, "counts": {"failed": 1, "not-selected": 5}, "cases": {"collected": 1, "failed": 1, "passed": 0, "skipped": 0}}
```

The empty input list makes the test scoped, so `notes.txt` does not exist in its view at
all, not even at its host path. Failure evidence identifies the exception and location
without printing file contents or exception messages. Failed results are never reusable.
Remove the intentional failing test when finished:

```sh
rm tests/test_denied.py
```

## Reuse policy and fixture lifetime

`@pytest.mark.telescope(cache=False)` alone leaves a test ordinary. Together with a scope,
as in `inputs=[...], cache=False`, the test stays sandboxed but executes every time and
never records a result. Use `run --force` to execute scoped successes again or
`run --no-cache` to bypass result reading and recording. Literal declarations at module,
class, function and parameter levels add together; any inherited `cache=False` wins.

In the default `case` mode, every scoped case starts a new sandbox and pytest session.
Session/module/class fixtures are recreated for each scoped case. Ordinary tests share one
ordinary pytest session per suite with normal fixture lifetime. Shared import and
parametrization resources must be declared for every scoped consumer, often through
defaults or a bundle. A module-level `pytestmark` scope keeps the whole module, including
its import, inside the sandbox; a module that also has ordinary tests is imported by the
ordinary session.

Optional [batch mode](batching.md) shares session/module/class fixtures among selected
scoped cases with exactly equal scopes in one suite/module. Use `run --execution-mode
batch` only when that fixture lifetime suits the tests. Use `run --workers 4` to run up to
four independent scoped sandboxes at once.

The sandbox controls the filesystem view only. It sets no CPU, memory or process limits
and shares the host network: limit resources in the job that runs pytest, and do not
declare network-dependent tests as scoped. Filesystem isolation does not prove arbitrary
Python deterministic: clocks, randomness and changing external state require explicit
care. Read the [marker and project reference](specification.md) for syntax, defaults,
bundles, absence and environment; the [execution contract](bubblewrap.md) covers routing,
the sandbox view and cache identity; the [API reference](bubblewrap-api.md) lists every
interface.

```{toctree}
:hidden:
:maxdepth: 1

Quickstart <self>
Marker reference <specification>
Ordinary and Bubblewrap execution <bubblewrap>
API and configuration <bubblewrap-api>
Declarations, inventory and reuse <execution-contract>
Optional batches <batching>
Metadata and suites <scoped-execution>
Run result reference <result-schema>
Benchmark <benchmark>
Development <development>
Publishing <publishing>
```
