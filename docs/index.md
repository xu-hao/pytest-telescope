# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Put input declarations beside your tests. By default every actual pytest case gets a fresh Linux
Docker worker and its own cached result, including each parametrized variant. Change a
case's declared input to rerun that case; unchanged cases can reuse their successes.

**Start broad, then narrow.** An unmarked case sees and hashes the whole project-file
snapshot. A literal `@pytest.mark.telescope(inputs=[...])` or `bundles=[...]` narrows its
visible files and cache inputs. The host filesystem and host virtual environment remain
outside the worker. [Excluded project directories and runtime files](execution-contract.md)
have explicit rules.

## Install

This tutorial uses **0.1.0a4**. Version 0.1.0a1 used file/group declarations and does not
support this marker interface. You need Linux, Python 3.11+, and permission to build and
run Linux containers using a local Docker daemon's Unix socket. Runtime preparation
needs network access; test workers have no network.

```sh
mkdir telescope-demo
cd telescope-demo
python -m venv .venv
. .venv/bin/activate
python -m pip install pytest-telescope==0.1.0a4
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
A project file visible to unmarked cases.
```

The red and blue tests are in the **same file**, but have separate resource scopes and
cache results. The pricing bundle adds shared application code and currency data. Project
defaults add the package initializer. The containing test source and `pyproject.toml`
are included automatically; conftest files and other helpers need explicit declarations
for narrow cases.

The unmarked note test stays broad. Defaults do not narrow it. All visible project files,
including hidden and Git-ignored files outside excluded directories, affect that case's
result. An explicit empty `inputs=[]` narrows to automatic inputs plus defaults; a
`cache=False` marker alone retains broad visibility.

## Run cold, then warm

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

`runtime` prepares a clean image with pinned dependencies. `plan` previews static scopes;
it does not import project code or enumerate real parametrized cases. `run` discovers
actual cases in an isolated collection worker, then executes each miss in a separate
worker with its own scope.

The first run executes **3 cases**. The second reuses **3 cases**. The JSON summary
separates collection work, executed/reused outcomes, and fresh/cached passes. A reused
case is a previous result, not a test executed on the current run. A fully warm
inventory-and-result hit starts no worker containers. These selected fields are from
the cold and warm summaries (other fields and timings are omitted):

```json
{"collection": {"outcome": "executed", "collected": 3}, "counts": {"executed": 3, "reused": 0}, "case_outcomes": {"fresh_passed": 3, "cached_passed": 0}}
{"collection": {"outcome": "reused", "collected": 3}, "counts": {"executed": 0, "reused": 3}, "case_outcomes": {"fresh_passed": 0, "cached_passed": 3}}
```

## See what reruns

Run the following steps in order with the same pytest arguments. Each change passes the
tests while demonstrating a different invalidation boundary.

**One narrow resource:** change the red price.

```sh
python -c 'from pathlib import Path; Path("data/red.json").write_text("{\"unit_price\": 15}\n")'
python -m pytest_telescope run --explain -- -q
```

**Shared resource:** change the currency used by both narrow cases.

```sh
python -c 'from pathlib import Path; Path("data/currency.json").write_text("{\"code\": \"EUR\"}\n")'
python -m pytest_telescope run --explain -- -q
```

**Broad-only resource:** change the project note.

```sh
python -c 'from pathlib import Path; Path("notes.txt").write_text("An updated project note.\n")'
python -m pytest_telescope run --explain -- -q
```

| Run | Fresh passes | Cached passes | Why |
| --- | --- | --- | --- |
| Cold | 3 | 0 | No successes recorded yet. |
| Unchanged | 0 | 3 | All scopes and results match. |
| Red price changed | 2 | 1 | Red and the broad note case rerun; blue reuses. |
| Shared currency changed | 3 | 0 | Both pricing consumers and the broad case rerun. |
| Note changed | 1 | 2 | Only the broad case includes this file. |

A changed discovery input refreshes collection without discarding unrelated per-case
results. Changing the common test file affects every case in that file. Changing
`pyproject.toml` affects every case because configuration is always an input.

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
the broad note case, which sees the new file; the original red/blue cases reuse. The
second reuses all five. Each `pytest.param` declaration belongs to its actual variant.
The red variant cannot read blue's JSON merely because collection saw both resources.

Change the red price once more to check parameter-specific reuse:

```sh
python -c 'from pathlib import Path; Path("data/red.json").write_text("{\"unit_price\": 18}\n")'
python -m pytest_telescope run --explain -- -q
```

The red case, red variant, and broad note case execute: **3 fresh passes**. The blue
case and blue variant reuse: **2 cached passes**.

## Observe a denied read

`notes.txt` exists in the project, but a narrow case only receives its own inputs.
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
`FileNotFoundError`; the other five cases are unselected. The inventory has six cases,
while the selected-case totals contain only the failing case:

```json
{"collection": {"collected": 6}, "counts": {"failed": 1, "not-selected": 5}, "cases": {"collected": 1, "failed": 1, "passed": 0, "skipped": 0}}
```

These are selected summary fields. The empty input list establishes
a narrow scope, so `notes.txt` is not visible. Failure evidence identifies the exception
and location without printing file contents or exception messages. Failed results are
never reusable. Remove the intentional failing test when finished:

```sh
rm tests/test_denied.py
```

## Reuse policy and fixture lifetime

`@pytest.mark.telescope(cache=False)` disables result reuse; by itself it does **not**
narrow file visibility.
Use `run --force` to refresh successes or `run --no-cache` to bypass result reading and
recording. These flags also refresh collection. Literal declarations at module, class,
function, and parameter levels add together; any inherited `cache=False` wins.

In the default `case` mode, every actual case starts a new pytest session. Session/module/class fixtures are
recreated for each case, and mutable state does not carry between cases. Collection may
see the union of potential inputs, but each execution starts with its own scope before
imports. Shared import and parametrization resources must be declared for every narrow
consumer, often through defaults or a bundle.

Optional [batch mode](batching.md) shares session/module/class fixtures among selected
cases with exactly equal input scopes in one suite/module. Use `run --execution-mode batch`
only when that fixture lifetime suits the tests. A batch has one fresh worker and one
atomic cached result; differing input scopes are never combined. The tutorial above uses
the default per-case mode and keeps its fresh/reused counts.

Filesystem isolation does not prove arbitrary Python deterministic. Clocks, randomness,
and changing external/native state require explicit care. Read the [marker and project
reference](specification.md) for exact syntax, defaults, bundles, absence, and environment;
the [execution contract](execution-contract.md) covers isolation, collection, cache
identity, limits, and failure recovery.

The 0.1.0a3 [scoped execution guide](scoped-execution.md) adds local distribution
metadata, independent suites and bounded parallel workers. Its [result reference](result-schema.md)
describes versioned inventories and explicit capability refusals. These additions are not
available in version 0.1.0a2.

```{toctree}
:hidden:
:maxdepth: 1

Quickstart <self>
Marker reference <specification>
Execution contract <execution-contract>
Worker lifecycle <worker-lifecycle>
Optional batches <batching>
Pinned local Git runtime <git-runtime>
Isolated Python source <isolated-source>
Scoped execution extension <scoped-execution>
Run result reference <result-schema>
Benchmark <benchmark>
Development <development>
Publishing <publishing>
```
