# Marker and project reference

The [quickstart](index.md) provides every file for a runnable project. The
[ordinary and Bubblewrap execution contract](bubblewrap.md) is authoritative for routing
and the sandbox, and the [declaration contract](execution-contract.md) for inventory and
reuse. The base marker interface is available since 0.1.0a2; old central `targets`
declarations are rejected with a migration message.

## Markers beside cases

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...
```

| Marker field | Meaning |
| --- | --- |
| `inputs=["path", "data/*.json"]` | Explicit project paths, directories, and globs. Makes the case scoped, including when empty. |
| `bundles=["pricing"]` | Reusable project declarations. Also makes the case scoped, including when empty. |
| `distributions=["core"]` | Selected local distribution metadata. Also makes the case scoped, including when empty. |
| `absent=["app/local_override.json"]` | Assert explicit paths are absent in the source project; does not make a case scoped. |
| `cache=False` | Never reuse or record success for this case; does not make a case scoped. |

Without an `inputs`, `bundles` or `distributions` field on or above it, a case is
ordinary: it runs in an ordinary pytest session every time, with no sandbox and no cache.
Project defaults alone do not make it scoped. A scoped case sees exactly the union of its
inherited declarations, defaults, containing test source and configuration, read-only, in
a [Bubblewrap view](bubblewrap.md). Each actual parametrized item is a separate unit.

Use direct `pytest.mark.telescope(...)` calls with literal lists of strings and booleans.
Supported locations are function/class decorators, module/class `pytestmark`, and
`pytest.param(..., marks=...)` inside literal parametrization declarations. Do not use
marker aliases, generated telescope markers, computed arguments, or factory functions.
Ordinary parameter values and item identities still come from real pytest collection.

## Additive inheritance

```python
import pytest

pytestmark = pytest.mark.telescope(bundles=["application"])


@pytest.mark.telescope(inputs=["data/common.json"])
class TestParsing:
    @pytest.mark.telescope(inputs=["data/valid.json"])
    def test_valid(self): ...
```

This case receives the application bundle, common JSON, valid JSON, project defaults,
its containing test file, and configuration. Function or parameter declarations cannot
subtract inherited files. Module/class/function/parameter markers add together, and any
inherited `cache=False` wins. A module-level `bundles` field scopes all its cases, even
functions with no individual marker, and keeps the module's import off the host.

Keep shared import-time and parametrization dependencies in each consumer's scope.
Collection of wholly scoped modules runs with the union of their potential scopes; an
individual execution never borrows another case's inputs. Each execution must collect
the same containing-file case inventory as discovery before running its selected case.

## Project configuration

Use `pyproject.toml` and run from its directory:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]

[tool.pytest_telescope]
test_roots = ["tests"]
env = ["FEATURE_VARIANT"]

[tool.pytest_telescope.defaults]
inputs = ["tests/conftest.py", "app/__init__.py"]

[tool.pytest_telescope.bundles.application]
inputs = ["app/parser.py", "tests/helpers.py"]
absent = ["app/local_override.json"]
```

| Table or field | Meaning |
| --- | --- |
| `test_roots` | Static test-source discovery roots; default `["."]`. Real pytest inventory includes new files and cases before accepting hits. |
| `env` | Explicit host environment names to forward into scoped sandboxes and key, including presence/absence. Reserved runtime/protocol names are rejected. Ordinary sessions inherit the whole environment. |
| `defaults` | Shared `inputs`, `bundles`, `absent`, and `cache` policy. Adds support to scoped cases without making unmarked cases scoped. |
| `bundles.NAME` | Reusable `inputs`, nested `bundles`, and `absent` lists. Cycles and unknown names are errors. |

Containing test source and configuration are automatic inputs. Scoped cases explicitly
declare application modules, package initializers, conftest files, helpers, and resources.
A configuration change invalidates all scoped cases; a bundle input change invalidates
its consumers. Ordinary cases always run, so nothing invalidates them.

Inputs are project-relative; missing declarations and paths escaping the project are
errors. `absent` asserts actual source nonexistence, unlike merely making a path
inaccessible. Creating a declared-absent file is a validation error even on a warm cache.
A leaf symlink must use relative target text, with its destination in the effective
scope. Directory symlinks, special files, and unsupported paths fail explicitly.

Declarations cannot name version-control metadata (`.git`, `.hg`, `.svn`), telescope
state (`.pytest-telescope` and an explicit in-project cache directory), pytest/bytecode
caches (`.pytest_cache`, `__pycache__`), or host Python environments (`.venv`, `venv`, or
a directory with validated `pyvenv.cfg`). Hidden and Git-ignored files can be declared.
pytest-telescope never walks or hashes undeclared project files.

The default suite keeps pytest settings in `[tool.pytest.ini_options]`. Next-alpha
[independent suites](scoped-execution.md) may declare other supported configuration formats. Forwarding environment names is not a secret-distribution mechanism.
Only named user variables are forwarded; runtime-controlled variables remain reserved.

## Select and control execution

```sh
python -m pytest_telescope run -- tests/test_prices.py::test_red
python -m pytest_telescope run -- -k red
python -m pytest_telescope run --force -- -q
python -m pytest_telescope run --no-cache -- -q
```

Selectors apply actual pytest semantics, after accounting for the full current inventory.
Other cases are reported unselected. Full options and selectors affect cache identity;
a filtered invocation cannot reuse an unfiltered run's result. `--force` refreshes
inventory and executes scoped successes again; `--no-cache` also prevents recording them.
`--workers N` runs up to N independent scoped sandboxes at once. `plan` is only a
nonexecuting static route and scope preview, not actual case enumeration.

Python callers can narrow a suite's files before marker parsing or collection imports:

```python
from pathlib import Path
from pytest_telescope.controller import run
from pytest_telescope.plan import make_plan

files = {"core": ["tests/core/test_prices.py"]}
preview = make_plan(Path("."), suites=["core"], structural_files=files)
result = run(Path("."), suites=["core"], structural_files=files)
```

Unlike a node selector in `arguments`, this exact list excludes other test modules from
the invocation's inventory. `collection.suites` reports the effective files, and
`inventory_complete` covers only that boundary. Omitted files have no unselected rows.
This API does not change routing or any case's input scope. It also preserves suite cwd,
root and capabilities.

Unspecified suites keep their configured membership; `{}` changes nothing and `[]`
makes one suite empty. Lists contain at most 4,096 canonical existing regular Python
test files within configured roots and patterns; duplicates, symlinks and foreign or
excluded paths are refused. List order is normalized. An explicit list has its own
cache identity even if it matches automatic discovery; changing it refreshes that
suite's context while unchanged suites retain theirs. New files enter an exact-list
invocation only when added to the list. See the
[structural narrowing contract](scoped-execution.md#invocation-only-structural-file-narrowing)
for validation and coverage rules. This override is available through the Python API.

Each scoped case gets a new sandbox and pytest session. Fixtures with broader pytest
scopes are recreated for each scoped case in default `case` mode. Optional
[batch mode](batching.md) shares ordinary pytest fixture lifetime only inside equal-scope
suite/module groups. Ordinary cases share one ordinary session per suite. See the
[declaration contract](execution-contract.md) for supported options, bounded failures,
exact cache identity, and outcome accounting.


## Metadata and independent suites

A literal `distributions=["core"]` marker adds selected local metadata and makes the case
scoped, including an empty list. Distribution names also compose through defaults and
bundles. Metadata does not grant application source: declare package modules through
`inputs` as usual. Ordinary cases see the checkout and installed environment as they are.

The [metadata and suite guide](scoped-execution.md) provides versioned static metadata
and independent suite configuration. A suite's `root`, `config`,
`test_roots`, `include` and `exclude` paths are all outer-project-relative. Exclusions
remove files before parsing or importing them. `args` preserve argument order; `plugins`
name installed explicit plugins with autoload disabled. A project using explicit suites
cannot also set top-level `test_roots`.

Configured `addopts` are resolved before scheduler validation. For a suite configuration
containing `addopts = -n auto`, an explicit `-o addopts=` removes that option; the run
then uses telescope's `workers` setting. Without that override, nested scheduler options
return an explicit unsupported capability. This does not enable xdist workers.


## Choose execution and fixture lifetime

`[tool.pytest_telescope.execution] mode = "case"` is the default. Set `mode = "batch"`
to share one fresh sandbox and pytest session among selected scoped cases with exactly
equal effective scope, metadata and cache policy in one suite/module. Module/class/session
fixtures and scratch share only inside that group; function `tmp_path` remains per test.

`run --execution-mode case|batch` and `controller.run(root, execution_mode="case"|"batch")`
override the configuration without editing it. `plan` reports the configured mode/profile,
not dynamically collected group membership. Each group occupies one worker slot. Group
records are atomic: failures, partial runs or corrupted records cause the whole selected
group to rerun. See [batching](batching.md) for ordering, 4,096-member limits and
lifecycle rules, and [result schema 3](result-schema.md) for member/group outcomes,
partial pytest stops and honest shared-worker timing.

## Host tools and child interpreters

Scoped tests run the host's own tools from the read-only system directories, for example
`git` from `/usr/bin`, with `HOME` and `TMPDIR` set to private `/tmp`. Create
repositories and other writable state in `/tmp` or `tmp_path`, copying declared inputs
there when needed; project source stays read-only. Host tools and system libraries are
not part of the cache key, so mark a scoped test `cache=False` when its result depends on
their version.

`subprocess.run([sys.executable, ...])` starts the same host interpreter with the same
installed packages. A child started with `-I` ignores `PYTHONPATH` and the working
directory, so it cannot import declared project source. To import declared source in a
child, start it without `-I` and pass the source location explicitly:

```python
import os
import subprocess
import sys

import pytest


@pytest.mark.telescope(inputs=["src/example_core.py"])
def test_child_imports_declared_source():
    environment = {**os.environ, "PYTHONPATH": os.path.abspath("src")}
    completed = subprocess.run(
        [sys.executable, "-c", "import example_core; assert example_core.value == 7"],
        env=environment,
        timeout=10,
    )
    assert completed.returncode == 0
```

The child sees the same view as its test: undeclared neighbouring modules stay absent.
The unreleased `git-local-v1` runtime profile and `isolated-source-v1` lookup profile were
withdrawn with the Docker runtime; their configuration tables are rejected.
