# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Ordinary tests run fresh in the invoking Python environment. Literal input declarations
opt cases into scoped execution and successful-result reuse. This tutorial keeps the
tests, data and bundle configuration together so each input boundary is visible.

**Replacement status: UNQUALIFIED.** The examples below describe the accepted
[ordinary/Bubblewrap routing contract](bubblewrap.md) and expected results. They are not
an installation recipe for a qualified release. The published 0.1.0a3 Docker release
retains its historical behavior; these routing changes do not alter its artifacts.
The [API/runtime proposal](bubblewrap-api.md) records preparation requirements. Only the
bounded resource mechanism has passed; production runtime and lifecycle controls remain.

## Prepare the example files

Use a Python 3.11+ environment for the project. An all-ordinary invocation uses its
installed pytest/plugins and needs no prepared scoped runtime. This mixed tutorial also
contains scoped tests, so running the proposed controller commands requires the future
qualified replacement candidate and explicit scoped runtime preparation. No replacement
version is currently prescribed for installation.

```sh
mkdir telescope-demo
cd telescope-demo
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

The unmarked note test is ordinary and always fresh; defaults do not opt it into scope.
A literal inherited `inputs`, `bundles` or `distributions` field, even `[]`, opts into
scoped execution. A `cache=False` or `absent` marker alone does not. Absence-only
assertions still validate exactly their named paths without snapshotting unrelated files.
An explicit empty `inputs=[]` receives automatic inputs plus applicable defaults.

## Run cold, then warm

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

These are the proposed commands after candidate installation and explicit preparation.
`runtime` prepares a frozen interpreter/wheel/native closure, not a host venv mount.
`plan` previews static scopes without importing code or enumerating actual parameters.
`run` independently discovers scoped cases in their restricted collection union, then
collects and runs the ordinary suite session before starting scoped execution.

The first run executes **3 cases**. An unchanged second run executes the ordinary note
test and reuses the **2 scoped successes**. Ordinary collection remains fresh even when
all scoped records hit. These selected fields illustrate expected results rather than
recorded qualification receipts:

```json
{"counts": {"executed": 3, "reused": 0}, "case_outcomes": {"fresh_passed": 3, "cached_passed": 0}}
{"counts": {"executed": 1, "reused": 2}, "case_outcomes": {"fresh_passed": 1, "cached_passed": 2}}
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

**Ordinary-only resource:** change the project note.

```sh
python -c 'from pathlib import Path; Path("notes.txt").write_text("An updated project note.\n")'
python -m pytest_telescope run --explain -- -q
```

| Run | Fresh passes | Cached passes | Why |
| --- | --- | --- | --- |
| Cold | 3 | 0 | No successes recorded yet. |
| Unchanged | 1 | 2 | The ordinary note test runs fresh; both scoped results match. |
| Red price changed | 2 | 1 | Red and the ordinary note case run; blue reuses. |
| Shared currency changed | 3 | 0 | Both pricing consumers and the ordinary case run. |
| Note changed | 1 | 2 | The note test always runs; scoped cases do not include this file. |

A changed discovery input refreshes collection without discarding unrelated per-case
results. Changing the common test file invalidates its scoped results. Changing
`pyproject.toml` invalidates scoped results because configuration is always an input.
Ordinary cases execute regardless of either change.

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
the ordinary note case; the original red/blue cases reuse. The
second executes the note case and reuses all four scoped cases. Each `pytest.param` declaration belongs to its actual variant.
The red variant cannot read blue's JSON merely because collection saw both resources.

Change the red price once more to check parameter-specific reuse:

```sh
python -c 'from pathlib import Path; Path("data/red.json").write_text("{\"unit_price\": 18}\n")'
python -m pytest_telescope run --explain -- -q
```

The red case, red variant, and ordinary note case execute: **3 fresh passes**. The blue
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
while result pass/fail totals contain only the selected failing case. `cases.collected`
accounts for all six validated identities:

```json
{"collection": {"collected": 6}, "counts": {"failed": 1, "not-selected": 5}, "cases": {"collected": 6, "failed": 1, "passed": 0, "skipped": 0}}
```

These are selected summary fields. The empty input list establishes
a narrow scope, so `notes.txt` is not visible. Failure evidence identifies the exception
and location without printing file contents or exception messages. Failed results are
never reusable. Remove the intentional failing test when finished:

```sh
rm tests/test_denied.py
```

## Reuse policy and fixture lifetime

`@pytest.mark.telescope(cache=False)` alone is ordinary and fresh.
`@pytest.mark.telescope(inputs=[], cache=False)` is scoped, fresh and nonrecording.
Use `run --force` to refresh successes or `run --no-cache` to bypass result reading and
recording. These flags also refresh collection. Literal declarations at module, class,
function, and parameter levels add together; any inherited `cache=False` wins.

In the default scoped `case` mode, every actual scoped case starts a new pytest session. Session/module/class fixtures are
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

Each suite's ordinary cases instead share one real pytest session and normal fixture
lifetimes. In a mixed module, its imports and conftest may run in ordinary context.
Scoped items are removed before pytest dispatches fixtures or bodies. Restricted
collection must independently agree with mixed-file membership and selection; otherwise
the invocation refuses without widening. Declare shared import/parameter dependencies
for restricted collection even when the host can already import them.

The [scoped execution guide](scoped-execution.md) covers local distribution metadata,
independent suites and explicit parallel budgets. [Schema 3](result-schema.md) separates
actual routes and reports unknown ordinary CPU as null. Runtime and capabilities remain
UNQUALIFIED until the full production controls pass.

```{toctree}
:hidden:
:maxdepth: 1

Quickstart <self>
Marker reference <specification>
Execution contract <execution-contract>
Ordinary and Bubblewrap proposal <bubblewrap>
Proposed Bubblewrap API and runtime <bubblewrap-api>
Optional batches <batching>
Pinned local Git runtime <git-runtime>
Isolated Python source <isolated-source>
Scoped execution extension <scoped-execution>
Run result reference <result-schema>
Benchmark <benchmark>
Development <development>
Publishing <publishing>
```
