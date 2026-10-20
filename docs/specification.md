# Marker and project reference

The [quickstart](index.md) provides every file for a runnable project. The
[execution contract](execution-contract.md) is authoritative for the supported boundary.
This reference describes the accepted replacement behavior, currently **UNQUALIFIED**.
The [ordinary/Bubblewrap contract](bubblewrap.md) and [exact API/runtime seam](bubblewrap-api.md)
define the transition. Published releases and old central-target migration remain unchanged.

## Markers beside cases

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...
```

| Marker field | Meaning |
| --- | --- |
| `inputs=["path", "data/*.json"]` | Explicit project paths, directories, and globs. Opts into scoped execution, including when empty. |
| `bundles=["pricing"]` | Reusable project declarations. Also opts into scoped execution, including when empty. |
| `distributions=["core"]` | Selected static project metadata; opts into scope, including when empty. |
| `absent=["app/local_override.json"]` | Assert explicit paths are absent in the source project; does not opt into scope alone. |
| `cache=False` | Never reuse or record success for this case; does not opt into scope alone. |

Without an effective literal `inputs`, `bundles` or `distributions` marker field,
a case runs ordinary pytest fresh in the invoking environment. Defaults alone do not
opt into scope. Inherited module/class/function/parameter declarations count; explicit
empty lists count too. An opted-in scope combines inherited declarations, defaults,
containing test source and project/suite configuration. Absence-only ordinary markers
still check exactly their named paths without snapshotting unrelated content.

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
functions with no individual marker.

Keep shared import-time and parametrization dependencies in each consumer's scope.
Restricted discovery independently runs with the declared union of potential scoped inputs; an individual execution never borrows
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
| `defaults` | Shared `inputs`, `bundles`, `distributions`, `absent`, and `cache` policy. Adds scoped support without opting otherwise ordinary cases into scope. |
| `bundles.NAME` | Reusable `inputs`, nested `bundles`, `distributions`, and `absent` lists. Cycles and unknown names are errors. |

Containing test source and configuration are automatic inputs. Narrow cases explicitly
declare application modules, package initializers, conftest files, helpers, and resources.
Ordinary cases always run fresh. A configuration change invalidates scoped results;
a bundle input change invalidates its scoped consumers.

Inputs are project-relative; missing declarations and paths escaping the project are
errors. `absent` asserts actual source nonexistence, unlike merely making a path
inaccessible. Creating a declared-absent file is a validation error even on a warm cache.
A leaf symlink must use relative target text, with its destination in the effective
scope. Directory symlinks, special files, and unsupported paths fail explicitly.

Scoped inputs exclude version-control metadata, telescope state, pytest/bytecode caches
and host Python environments. Explicit declarations into those namespaces are errors.
Declared broad inputs can include hidden/Git-ignored content and large build trees,
which then count toward snapshot limits. Ordinary-only routing never implicitly scans
those unrelated trees, follows directory symlinks or snapshots special files.

The default suite keeps pytest settings in `[tool.pytest.ini_options]`. [Independent suites](scoped-execution.md) may declare other supported configuration formats. Forwarding environment names is not a secret-distribution mechanism.
Only named user variables are forwarded on the scoped route; runtime-controlled
variables remain reserved. Ordinary sessions inherit the invoking environment.

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
This API does not change a case's input scope: unmarked cases still run ordinary pytest fresh. It also preserves suite cwd, root, resources and capabilities.

Unspecified suites keep their configured membership; `{}` changes nothing and `[]`
makes one suite empty. Lists contain at most 4,096 canonical existing regular Python
test files within configured roots and patterns; duplicates, symlinks and foreign or
excluded paths are refused. List order is normalized. An explicit list has its own
cache identity even if it matches automatic discovery; changing it refreshes that
suite's context while unchanged suites retain theirs. New files enter an exact-list
invocation only when added to the list. See the
[structural narrowing contract](scoped-execution.md#invocation-only-structural-file-narrowing)
for validation and coverage rules. This override is available through the Python API.

Each suite has one ordinary session with normal pytest fixture lifetimes. Each scoped
case gets a new session in default `case` mode. Optional [batch mode](batching.md) shares ordinary
pytest fixture lifetime only inside equal-scope suite/module groups. See the
[execution contract](execution-contract.md) for supported options, runtime requirements,
bounded failures, exact cache identity, and outcome accounting.


## Scoped metadata and suites

A literal `distributions=["core"]` marker adds selected local metadata and establishes
narrow scope, including an empty list. Distribution names also compose through defaults
and bundles. Metadata does not grant application source: declare package modules through
`inputs` as usual. Ordinary cases use their invoking environment, not a generated scoped metadata projection.

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


## Choose execution and fixture lifetime

`[tool.pytest_telescope.execution] mode = "case"` is the default. Set `mode = "batch"`
to share one fresh pytest session among selected cases with exactly equal effective
scope, metadata and cache policy in one suite/module. Module/class/session fixtures and
scratch share only inside that group; function `tmp_path` remains per test.

`run --execution-mode case|batch` and `controller.run(root, execution_mode="case"|"batch")`
override the configuration without editing it. `plan` reports the configured mode/profile,
not dynamically collected group membership. Each group occupies one resource slot and
has one worker's limits. Group records are atomic: failures, partial runs or corrupted
records cause the whole selected group to rerun. See [batching](batching.md) for ordering,
4,096-member limits and lifecycle rules, and [result schema 3](result-schema.md) for
member/group outcomes, partial pytest stops and honest shared-worker timing.

## Proposed local Git migration

The proposed, UNQUALIFIED [local Git profile](git-runtime.md) selects immutable tooling
for an invocation while preserving each case or group's declared project inputs:

```toml
[tool.pytest_telescope.runtime]
profile = "git-local-v2"
requires = ["git-local"]
```

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan --require-capability git-local
python -m pytest_telescope run --require-capability git-local
```

The proposed default is `python-v2`; old Docker v1 profiles are not silently migrated. `--runtime-profile` selects an invocation-only override
for preparation, planning or execution. Requirements declared in configuration remain
in force when a caller supplies an override; requesting Git with the default Python
profile refuses before collection or cache reuse. See the
[runtime interfaces](result-schema.md#runtime-profiles) for Python arguments
and the distinct runtime identity in results.

Create repositories and linked worktrees in private writable scratch, copying only
declared input content when needed. Project source stays read-only, and host `.git`
metadata remains excluded. The profile supports local-file origins and ordinary
explicit-message commits, not remote-network helpers or executable hooks. Set author,
committer dates and identity explicitly when tests rely on stable commit IDs. Selecting
Git does not change resource limits, noexec scratch, case/batch fixture lifetime, or the
meaning of `cache=False`.

## Declared source in isolated Python children

The retained, UNQUALIFIED opt-in [isolated-source profile](isolated-source.md) lets the pinned Python
interpreter discover declared source and selected static metadata when a test launches
`python -I`. It does not install the application. Add an ordered lookup root:

```toml
[tool.pytest_telescope.python]
profile = "isolated-source-v1"
source_roots = ["src"]
```

For `src/example_core.py` containing `value = 7`, a narrow test can use:

```python
import subprocess
import sys
import pytest


@pytest.mark.telescope(inputs=["src/example_core.py"])
def test_isolated_child():
    completed = subprocess.run(
        [sys.executable, "-I", "-c", "import example_core; assert example_core.value == 7"],
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 0
```

The root controls import lookup; the marker grants the file. Undeclared neighboring
modules remain absent. Selected `distributions` metadata is discoverable by children,
but an entry point can load only source in that same view. Empty source roots support
metadata-only children. Namespace package portions and same-named modules follow the
declared root order, so suite-specific complete replacements can keep independent roots.

Without the table the profile is `default`, which accepts no roots and preserves existing
behavior. `--python-profile` and repeated `--source-root`, or the corresponding Python
API fields, override an invocation without editing configuration. See the
[lookup interfaces and result identity](result-schema.md#isolated-child-source-lookup).
System/runtime modules keep earlier lookup precedence. `-S` disables site initialization
and has no source-discovery guarantee. Visible source-root startup hooks and installed
metadata directories refuse before any worker; arbitrary source-root `.pth` files stay
inert. Read-only source, noexec scratch, limits and case/batch scope boundaries are unchanged.
