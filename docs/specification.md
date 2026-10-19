# Marker and project reference

The [quickstart](index.md) provides every file for a runnable project. The
[execution contract](execution-contract.md) is authoritative for the supported boundary.
The base marker interface is available in 0.1.0a2; old central `targets` declarations
are rejected with a migration message. Version 0.1.0a3 additions are specified in the
[scoped execution guide](scoped-execution.md) and [result reference](result-schema.md).

## Markers beside cases

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...
```

| Marker field | Meaning |
| --- | --- |
| `inputs=["path", "data/*.json"]` | Explicit project paths, directories, and globs. Establishes narrow scope, including when empty. |
| `bundles=["pricing"]` | Reusable project declarations. Also establishes narrow scope, including when empty. |
| `absent=["app/local_override.json"]` | Assert explicit paths are absent in the source project; does not narrow scope alone. |
| `cache=False` | Never reuse or record success for this case; does not narrow scope alone. |

Without an `inputs` or `bundles` marker field, a case sees and hashes the broad project
snapshot. Project defaults alone do not narrow it. Explicit file-scope markers replace
that broad implicit scope with the union of inherited declarations, defaults, containing
test source, and configuration. Each actual parametrized item is a separate unit.

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
inherited `cache=False` wins. A module-level `bundles` field narrows all its cases, even
functions with no individual marker.

Keep shared import-time and parametrization dependencies in each consumer's scope.
Discovery runs with the union of potential scopes; an individual execution never borrows
another case's inputs. Each execution must collect the same containing-file case
inventory as discovery before running its selected case.

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
| `env` | Explicit host environment names to forward and key, including presence/absence. Reserved runtime/protocol names are rejected. |
| `defaults` | Shared `inputs`, `bundles`, `absent`, and `cache` policy. Adds support without narrowing otherwise unmarked cases. |
| `bundles.NAME` | Reusable `inputs`, nested `bundles`, and `absent` lists. Cycles and unknown names are errors. |

Containing test source and configuration are automatic inputs. Narrow cases explicitly
declare application modules, package initializers, conftest files, helpers, and resources.
Broad cases already include visible project files. A configuration change invalidates
all cases; a bundle input change invalidates its consumers and broad cases.

Inputs are project-relative; missing declarations and paths escaping the project are
errors. `absent` asserts actual source nonexistence, unlike merely making a path
inaccessible. Creating a declared-absent file is a validation error even on a warm cache.
A leaf symlink must use relative target text, with its destination in the effective
scope. Directory symlinks, special files, and unsupported paths fail explicitly.

Broad scope includes hidden and Git-ignored project files, except version-control
metadata (`.git`, `.hg`, `.svn`), telescope state (`.pytest-telescope` and an explicit
in-project cache directory), pytest/bytecode caches (`.pytest_cache`, `__pycache__`),
and host Python environments (`.venv`, `venv`, or a directory with validated
`pyvenv.cfg`). Other large trees such as `build`, `dist`, and `node_modules` count toward
snapshot limits. Explicit declarations into excluded namespaces are errors.

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
inventory and executes successes again; `--no-cache` also prevents recording them.
`plan` is only a nonexecuting static scope preview, not actual case enumeration.

Each case gets a new pytest session. Fixtures with broader pytest scopes are recreated
for each case; tests depending on cross-case mutable state need rewriting. See the
[execution contract](execution-contract.md) for supported options, runtime requirements,
bounded failures, exact cache identity, and outcome accounting.


## Next-alpha scoped execution

A literal `distributions=["core"]` marker adds selected local metadata and establishes
narrow scope, including an empty list. Distribution names also compose through defaults
and bundles. Metadata does not grant application source: declare package modules through
`inputs` as usual. Broad cases receive every configured distribution.

The [complete scoped guide](scoped-execution.md) provides versioned static metadata,
independent suite and worker-budget configuration. A suite's `root`, `config`,
`test_roots`, `include` and `exclude` paths are all outer-project-relative. Exclusions
remove files before parsing or importing them. `args` preserve argument order; `plugins`
name installed explicit plugins with autoload disabled. A project using explicit suites
cannot also set top-level `test_roots`.

Configured `addopts` are resolved before scheduler validation. For a suite configuration
containing `addopts = -n auto`, an explicit `-o addopts=` removes that option; the run
then uses telescope's declared resource envelope. Without that override, nested scheduler
options return an explicit unsupported capability. This does not enable xdist workers.
