# Marker and project reference

> The schema 4 and per-scope environment interfaces described here are an unreleased
> development contract. Published version 0.1.0a6 does not provide them.

Declare inputs beside tests, then use defaults and bundles in `pyproject.toml` to share
declarations. If you're setting up pytest-telescope for the first time, start with the
[quickstart](index.md). The [execution contract](bubblewrap.md) describes the sandbox,
and [declarations, inventory and reuse](execution-contract.md) explains how results are
cached.

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
| `git="frozen-checkout-v1"` | Request a frozen read-only Git checkout and history. Makes the case scoped. |
| `distributions=["core"]` | Selected local distribution metadata. Also makes the case scoped, including when empty. |
| `env=["OPTIONAL_FEATURE"]` | Forward and key named environment variables for this scope. Also makes the case scoped, including when empty. |
| `absent=["app/local_override.json"]` | Assert explicit paths are absent in the source project; does not make a case scoped. |
| `cache=False` | Never reuse or record success for this case; does not make a case scoped. |

Without an `inputs`, `bundles`, `distributions` or `env` field on or above it, a case is
ordinary: it runs in an ordinary pytest session every time, with no sandbox and no cache.
Project defaults alone do not make it scoped. A scoped case sees exactly the union of its
inherited declarations, defaults, containing test source and configuration, read-only, in
a [Bubblewrap view](bubblewrap.md). Each actual parametrized item is a separate unit.

Use direct `pytest.mark.telescope(...)` calls with literal lists of strings and booleans;
the Git field takes the literal version string shown above.
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

`test_valid` can read the application bundle, `data/common.json`, `data/valid.json`,
project defaults, its containing test file and configuration. Module, class, function
and parameter markers add together; a marker cannot remove inherited files or environment names. Any inherited
`cache=False` disables caching. A module-level `bundles` field scopes all its cases, even
functions with no individual marker, and keeps the module's import off the host.

Environment names compose in the same places. For example:

```python
pytestmark = pytest.mark.telescope(env=["MODULE_FEATURE"])

@pytest.mark.telescope(env=["CLASS_FEATURE"])
class TestVariants:
    @pytest.mark.telescope(env=["FUNCTION_FEATURE"])
    @pytest.mark.parametrize("value", [
        pytest.param("a", marks=pytest.mark.telescope(env=["PARAM_FEATURE"]))
    ])
    def test_variant(self, value): ...
```

The parameter receives all four names plus its defaults, bundles and global names.
Another case does not receive `PARAM_FEATURE` unless it declares or inherits it.

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
env = ["SHARED_FEATURE"]

[tool.pytest_telescope.bundles.application]
inputs = ["app/parser.py", "tests/helpers.py"]
absent = ["app/local_override.json"]
env = ["PARSER_FEATURE"]
```

| Table or field | Meaning |
| --- | --- |
| `test_roots` | Static test-source discovery roots; default `["."]`. Real pytest inventory includes new files and cases before accepting hits. |
| `env` | Explicit host environment names to forward into scoped sandboxes and key, including presence/absence. Reserved runtime/protocol names are rejected. Ordinary sessions inherit the whole environment. |
| `defaults` | Shared `inputs`, `bundles`, `distributions`, `env`, `absent`, `git`, and `cache` policy. Adds support to scoped cases without making unmarked cases scoped. |
| `bundles.NAME` | Reusable `inputs`, nested `bundles`, `distributions`, `env`, `absent` lists and the Git declaration. Cycles and unknown names are errors. |

Containing test source and configuration are automatic inputs. Scoped cases explicitly
declare application modules, package initializers, conftest files, helpers, and resources.
A configuration change invalidates all scoped cases; a bundle input change invalidates
its consumers. Ordinary cases always run, so nothing invalidates them.

Inputs are project-relative. A declared input that is missing or points outside the
project is an error. `absent` checks that a path does not exist in the source checkout.
Creating that file is a validation error even when a cached result exists.
A leaf symlink must use relative target text, with its destination in the effective
scope. Directory symlinks, special files, and unsupported paths fail explicitly.

Declarations cannot name version-control metadata (`.git`, `.hg`, `.svn`), telescope
state (`.pytest-telescope` and an explicit in-project cache directory), pytest/bytecode
caches (`.pytest_cache`, `__pycache__`), or host Python environments (`.venv`, `venv`, or
a directory with validated `pyvenv.cfg`). Hidden and Git-ignored files can be declared.
Without a Git declaration, pytest-telescope never walks or hashes undeclared project files.
The explicit Git declaration grants the supported committed tree as described below.

The default suite keeps pytest settings in `[tool.pytest.ini_options]`.
[Independent suites](scoped-execution.md) may declare other supported configuration formats. Forwarding environment names is not a secret-distribution mechanism.
Only named user variables are forwarded; runtime-controlled variables remain reserved.
Each `env` is a literal list of at most 4,096 nonempty identifiers of at most 4,096
characters each. `PYTHONPATH`, `PYTHONHOME`, `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD`, and names beginning `TELESCOPE_` are reserved.
The invoking environment is captured once. Absence, an empty value and a nonempty value
are distinct; a change invalidates only consumers of that name, except that top-level
`env` names apply to every scoped case. Defaults add names to scoped cases but do not
scope an ordinary case. List any import, parametrization and collection dependency in
every consumer's shared declarations. The scoped discovery union is never an execution
environment. No raw values are recorded in summaries, worker requests, cache records or
normal diagnostic logs. Ordinary sessions retain the inherited environment.

## Frozen Git checkout (development contract)

The following interface is under development and is not a claim about the published
0.1.0a6 package. Request it explicitly on a case:

```python
@pytest.mark.telescope(git="frozen-checkout-v1")
def test_revision():
    ...
```

The same literal string may appear in project defaults or bundles. Module, class,
function and parameter declarations add together; inherited Git cannot be removed.
Defaults alone do not scope an ordinary case. Omission leaves Git disabled; unknown
versions and non-string or dynamic marker values are rejected. `plan` is static and
does not run Git. A requested view includes the supported committed source and Git
history as well as separately declared inputs; untracked files need separate declarations.
The [sandbox contract](bubblewrap.md#frozen-git-checkout-development-contract) defines
the supported states, safety and refusal boundaries.

## Select and control execution

```sh
python -m pytest_telescope run -- tests/test_prices.py::test_red
python -m pytest_telescope run -- -k red
python -m pytest_telescope run --force -- -q
python -m pytest_telescope run --no-cache -- -q
```

Pytest applies selectors after collecting the configured files. Other cases are reported
as unselected. The cache key includes the full options and selectors, so a different
selection needs results saved for that selection, even if the files are unchanged.
`--force` refreshes inventory and executes scoped successes again; `--no-cache` also
prevents recording them.
`--workers N` runs up to N independent scoped sandboxes at once. `plan` reads declarations
and previews routes and files without running pytest collection.

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
lifecycle rules, and [result schema 4](result-schema.md) for member/group outcomes,
partial pytest stops and timing for shared workers.

## Host tools and child interpreters

Scoped tests run the host's own tools from the read-only system directories, for example
`git` from `/usr/bin`, with `HOME` and `TMPDIR` set to private `/tmp`. Create
repositories and other writable state in `/tmp` or `tmp_path`, copying declared inputs
there when needed; project source stays read-only. Except for the explicitly requested
frozen Git context described above, host tools and system libraries are not part of the
cache key, so mark a scoped test `cache=False` when its result depends on
their version.

`subprocess.run([sys.executable, ...])` starts the same host interpreter with the same
installed packages. When the project is installed into that environment in editable
form, for example with `pip install -e .`, a child imports declared source through the
install, including an isolated child started with `-I`. The install points at the
project's host path, and the sandbox shows the frozen declared files there:

```python
import subprocess
import sys

import pytest


@pytest.mark.telescope(inputs=["src/example_core/__init__.py", "src/example_core/worker.py"])
def test_isolated_child_imports_declared_source():
    completed = subprocess.run(
        [sys.executable, "-I", "-m", "example_core.worker"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
```

The setuptools finder module and `.pth` path entries are both supported, because both
name the project's host path. Without an editable install, a child started with `-I`
ignores `PYTHONPATH` and the working directory, so it cannot import declared project
source. Start it without `-I` and pass the source location explicitly:

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

Either way the child sees the same view as its test: undeclared neighbouring modules stay
absent, and the live checkout is never visible.
