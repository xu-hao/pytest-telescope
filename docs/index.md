# pytest-telescope

pytest-telescope is a pytest plugin that avoids rerunning tests whose inputs have not
changed. It is for Linux projects whose tests run often, for example in CI or in a merge
queue that checks many changes, where rerunning unaffected tests wastes time.

## Scoped and ordinary tests

pytest-telescope treats each test in one of two ways:

- A *scoped* test declares the project files it reads. It runs in a
  [Bubblewrap](https://github.com/containers/bubblewrap) sandbox that shows only those
  project files, read-only, together with the installed Python environment and system
  directories. When it passes, pytest-telescope saves the result. A later run reuses that
  result instead of running the test again, as long as the declared files, the
  interpreter and the installed packages are unchanged. These belong to the test's
  *cache key*, which pytest-telescope checks before it reuses a result.
- An *ordinary* test declares nothing. It runs every time as plain pytest, with no
  sandbox and no cache.

These two treatments are the scoped and ordinary *routes*. pytest-telescope routes each
collected test separately, and calls it a *case*: a parametrized test has one case per
parameter set. You can adopt pytest-telescope gradually: scope one test, then another,
in any order. Every test you have not scoped keeps running as before.

## A minimal example

This project has one scoped test and one ordinary test. The example assumes
pytest-telescope is [installed](#1-install-in-your-projects-test-environment) in the
environment that runs it. `pyproject.toml` tells pytest-telescope where the tests are:

```toml
[tool.pytest_telescope]
test_roots = ["tests"]
```

`tests/test_colors.py` declares that `test_red` reads `data/red.json`:

```python
import json
from pathlib import Path

import pytest


@pytest.mark.telescope(inputs=["data/red.json"])
def test_red():
    assert json.loads(Path("data/red.json").read_text())["name"] == "red"


def test_note():  # ordinary: runs every time, no sandbox, no cache
    assert 1 + 1 == 2
```

Run the tests twice from the directory that contains `pyproject.toml`:

```sh
python -m pytest_telescope run
python -m pytest_telescope run
```

Each run prints one JSON report of about 200 lines in place of pytest's usual output;
failures appear inside it. The first run executes both tests. The second runs
`test_note` again and reuses the result of `test_red`. This excerpt shows only the
`executed` and `reused` keys of each report's `counts` object:

```json
{"counts": {"executed": 2, "reused": 0}}
{"counts": {"executed": 1, "reused": 1}}
```

Edit `data/red.json` and `test_red` runs again on the next run. Inside the sandbox,
`test_red` can read `data/red.json`, its own test file and the *project configuration*:
`pyproject.toml` and, when a [suite](#2-point-it-at-your-existing-tests-and-pytest-configuration)
names a separate pytest configuration file, that file. Other project files do not exist
there.

## Where to go next

- [Quickstart](#quickstart-add-it-to-an-existing-project): add pytest-telescope to an
  existing project and cache one of its tests.
- [Marker and project reference](specification.md): every marker field and
  `pyproject.toml` setting.
- [Use it in CI](#use-it-in-ci): keep saved results between CI jobs.
- [Ordinary and Bubblewrap execution](bubblewrap.md): what a scoped test can see, and
  what makes a saved result reusable.
- [Optional batches](batching.md): let scoped tests with the same inputs share fixtures.
- [Use frozen external tools](#use-frozen-external-tools): let a scoped test run an
  installation outside the project, such as Node.
- [Benchmark](benchmark.md): measure what the sandbox costs on your suite.
- [Development](development.md): build and test pytest-telescope itself.

Limits: pytest-telescope needs Linux, and scoped tests also need Bubblewrap. All tests
share the host network. A scoped test cannot see the checkout's `.git` directory or its
history: declarations cannot name it. A test that needs a repository can
[create one in private scratch](specification.md#host-tools-and-child-interpreters).

## Quickstart: add it to an existing project

Start with a project whose tests already pass under pytest. The steps below add caching
to one existing test, check that it works, then extend it to more of your suite.

### 1. Install in your project's test environment

You need Linux, Python 3.11+, and the `bwrap` command from Bubblewrap on a host that permits
user, mount and PID namespaces. Scoped tests use the interpreter and packages from the
environment that invokes pytest-telescope.

From your existing checkout, activate the environment you normally use for tests, then:

```sh
python -m pytest -q
python -m pip install pytest-telescope
```

Keep a conda or pixi environment outside the project directory; scoped tests refuse one
inside it.

Keep your normal dependency setup, including `pip install -e .` if your project uses an
editable install. Add pytest-telescope to your test dependency file or dependency group
to reproduce this setup in CI.

### 2. Point it at your existing tests and pytest configuration

Add this table to your `pyproject.toml`, changing `tests` to your test directory:

```toml
[tool.pytest_telescope]
test_roots = ["tests"]
```

This uses the pytest settings already in `[tool.pytest.ini_options]`, including import
paths and registered markers. Create `pyproject.toml` if you don't have one. If a table
already exists, add settings to it; TOML does not allow the same table twice.

Scoped tests do not load installed pytest plugins automatically. A plugin that your
environment normally loads by itself, for example one that provides fixtures such as
`pytest-asyncio`, is active in ordinary tests but not in scoped tests until you name it.

If you keep pytest settings in `pytest.ini`, `.pytest.ini`, `tox.ini` or `setup.cfg`, or
scoped tests need such a plugin, use a named *suite* in place of top-level `test_roots`.
A suite is one pytest configuration together with its test files:

```toml
[tool.pytest_telescope.suites.tests]
root = "."
config = "pytest.ini"
test_roots = ["tests"]
plugins = []
```

Set `config` to your actual configuration file, which can also be `pyproject.toml`.
List the module name of each installed plugin that scoped tests need in `plugins`, for
example `plugins = ["pytest_asyncio.plugin"]`.
The [suite reference](scoped-execution.md#independent-suites-and-structural-membership)
covers separate roots, working directories and suite arguments.

Scoped sessions clear inherited environment variables. If a test needs a variable, list
its name in `env` under `[tool.pytest_telescope]`. Changing its value, setting it or
unsetting it will invalidate cached results. See the
[project configuration reference](specification.md#project-configuration).

### 3. Declare the inputs of one existing test

Choose a test whose result depends on local files and the installed Python environment.
Leave tests that depend on network services or changing external state ordinary. Check
what the chosen test imports, what its fixtures read, and what runs during collection.

For example, if an existing parser test imports your package and uses shared test helpers,
add a bundle to `pyproject.toml`. A *bundle* is a named list of inputs that several tests
can share:

```toml
[tool.pytest_telescope.bundles.parser]
inputs = ["src/my_package", "tests/conftest.py", "tests/helpers.py"]
```

Replace these paths with files and directories in your checkout. Include applicable
parent `conftest.py` files, imported test helpers and resources used by fixtures. Remove
entries that do not exist: a missing declared path is an error. Starting with the package
directory is convenient; you can narrow it to individual source files later.

Import `pytest` if the test module does not already import it. Put this decorator
directly above your existing test function, keeping its body and fixture arguments. It
must be *literal*: written out in the source as `pytest.mark.telescope(...)` with plain
lists of strings and booleans, not built from variables or helper functions, because
pytest-telescope reads it without running your code:

```python
@pytest.mark.telescope(inputs=["tests/data/valid.json"], bundles=["parser"])
```

Use the data file that your test actually reads. Paths are relative to the project root.
The containing test source and configuration are automatic inputs; application source,
conftest files, helpers and data need declarations.

This makes the test *scoped*: it sees a read-only copy of the declared project files at
`/project`, plus the installed environment and system directories. Undeclared project
files are absent. Writes belong in private scratch, for example through pytest's
`tmp_path` fixture. Tests without an `inputs`, `bundles`, `distributions`, `env` or
`tools` declaration remain ordinary, including other tests in the same file.
(`distributions` selects [metadata of local packages](scoped-execution.md#static-project-metadata);
most projects do not need it.)

### 4. Run the test twice

From the directory containing `pyproject.toml`:

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- tests/unit/test_parser.py::test_accepts_valid_record -q
python -m pytest_telescope run --explain -- tests/unit/test_parser.py::test_accepts_valid_record -q
```

Replace the node selector with your test's path and name, using the same arguments on both
runs. `plan` reads declarations without importing project code and shows which files
each test can read. `--explain` adds to the report the reason each case was executed or
reused. Pytest discovers the actual parametrized cases when you run the
tests. A node selector chooses what to run, but pytest may still collect other configured
modules. Use a suite's
[`include` and `exclude` settings](scoped-execution.md#independent-suites-and-structural-membership)
to leave modules out before they are parsed or imported.

If your test has one case and passes, the first run with an empty cache reports one
executed case. The second reports one reused case. Look for these fields in the JSON
reports:

```json
{"counts": {"executed": 1, "reused": 0}}
{"counts": {"executed": 0, "reused": 1}}
```

`reused` means pytest-telescope accepted the previous result without running the case
again. Parametrized tests have a result for each case, so their counts can be larger.

If a scoped run fails to import a project module or read a fixture file, check the
files listed by `plan` and add the missing dependency. Pytest fixtures still run normally,
but their files must be declared.

A run *refuses* when it cannot proceed safely: it stops with an explicit refusal result
and never falls back to running scoped tests as ordinary pytest. When a refusal happens
depends on its cause:

- **Before any test runs.** Declarations that static discovery finds invalid refuse
  first. So does a missing Bubblewrap or unavailable namespaces: sandbox prerequisites
  are checked before any session starts. Fix the declaration, or use a host that meets
  the [requirements](bubblewrap-api.md#host-requirements), and run again.
- **After ordinary tests have run.** A problem found only later, for example while a
  scoped case's view is prepared, refuses the run after the ordinary tests have already
  executed. The result keeps what was observed before such a
  [late refusal](result-schema.md#retained-observations-after-a-late-refusal), but the
  run is still a refusal, not a success. Read the result before you retry, and do not
  retry automatically: a retry runs the ordinary tests, and their side effects, again.

A run can also fail although every test body passed, when a pytest plugin ends its
session with an error status. The report still shows those passes, and none of them is
reused.

### 5. Change an input and run again

Make a normal change to one of the test's declared inputs and repeat the command. The
case executes again; a passing result becomes reusable on the next unchanged run.

| Change | Effect on scoped results |
| --- | --- |
| Declared source, helper or data file changes | The scoped cases that declare it execute again. |
| Containing test file changes | Scoped cases in that file execute again. |
| Project configuration changes | Scoped cases execute again. |
| Installed distributions or interpreter change | Scoped cases execute again. |
| File outside a case's declared inputs changes | It does not invalidate that case. |

Add declarations to more tests as you establish their dependencies. Use a module-level
literal `pytestmark` when every test in a module should share a scope; use
`pytest.param(..., marks=pytest.mark.telescope(...))` for parameter-specific inputs.
A case's scope is the sum of the declarations on its own module, class, function and
selected parameter. Write the declaration on each test that needs it: reusing another
test's marker object does not pass its declaration on and is refused; see
[additive inheritance](specification.md#additive-inheritance). A literal
`env=["OPTIONAL_FEATURE"]` marker scopes a case and forwards that variable only to the
cases that declare it; `env=[]` also scopes a case. The top-level `env` setting applies
to every scoped case. The
[marker reference](specification.md) shows the supported forms, defaults and bundles.

Run the whole suite with your usual pytest arguments after `--`:

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

Every run executes the selected ordinary cases in one fresh pytest session per suite.
Scoped cases use a cached result when they can and run in a sandbox when they cannot.
The summary's `route_counts` field breaks down the executed and reused counts for the
ordinary and scoped routes.

## Use it in CI

Install your test dependencies and Bubblewrap, then use the same `run` command in your
CI job. Save and restore `.pytest-telescope/cache` between jobs, or choose a location with
`run --cache-dir PATH`. Without a saved cache, the next job must run the tests again.

A merge-queue job still checks the current checkout. pytest-telescope compares each
scoped case's inputs and execution settings with the saved result before reusing it.
For example, a change to one parser's data file can rerun that parser's tests while
other scoped tests reuse their results. Ordinary tests continue to run on every job.

Compare a first run with an empty cache (a *cold* run) and a repeated run with unchanged
inputs (a *warm* run) on your own suite. Starting separate pytest sessions costs time;
short tests may cost more to run through the sandbox than through plain pytest. The
[benchmark guide](benchmark.md) explains how to measure that overhead.

## Reuse policy and fixture lifetime

pytest-telescope has two *execution modes* for scoped cases. In the default mode, named
`case`, every scoped case that needs to run gets its own new sandbox and pytest session,
so session, module and class fixtures are recreated for each one. Ordinary tests
share normal pytest fixture lifetimes within their suite's ordinary session. A module
containing both routes is imported during ordinary collection; use a module-level scope
when the whole module, including its import, should be collected only in a sandbox.

The optional mode, named [`batch`](batching.md), runs several scoped cases together so
that they share fixtures. A *batch* is a group of selected scoped cases in the same
module and suite with exactly the same readable files, declared absent paths, metadata
and cache policy; its cases run in one sandbox and one pytest session. Use
`run --execution-mode batch` when those tests can share fixtures. In either mode,
`run --workers 4` runs up to four independent scoped sandboxes at once.

In batch mode, a session, package, module or class fixture that two tests would share in plain
pytest is created once per batch, so tests in different batches get separate instances.
If your tests rely on sharing one instance, turn on the opt-in
[fixture guard](batching.md#fixture-guard): it refuses the run when a fixture might be
split this way. It is conservative and can refuse a run that would have been fine.

`@pytest.mark.telescope(cache=False)` alone leaves a test ordinary. Together with a scope,
as in `inputs=[...], cache=False`, it keeps the test sandboxed and fresh on every run,
without recording a reusable result. Use `run --force` to execute scoped successes again
or `run --no-cache` to bypass result reading and recording.

A scoped test is meant to be *location-blind*: it cannot tell where its checkout lives,
so identical checkouts at different paths share scoped results. It sees its project at
`/project`, wherever the checkout lives.

**Not location-blind:** three things a scoped test can read still name its checkout's
location, which its cache key does not contain:

- the source directories in the mount table (field 4 of `/proc/*/mountinfo`);
- the raw bytes of cached bytecode files (`__pycache__/*.pyc`) of an environment inside
  the project.
- the real path of an environment inside the project that is a symbolic link to a
  directory outside it, such as `.venv -> ../shared-venv`.

A reused result may have been produced by a checkout at another path, where the test
would have read a different value.
Leave a test that reads any of them ordinary, or add `cache=False` to its scope.
See [what a scoped test can still see](bubblewrap.md#what-a-scoped-test-can-still-see).

The sandbox controls which files tests can see. Limit CPU, memory, processes and scratch
space in the job that runs pytest. Tests share the host network. Before caching a test
that uses the clock or randomness, give it a reproducible time or seed.
[Ordinary and Bubblewrap execution](bubblewrap.md) describes the view and cache identity,
and the [API reference](bubblewrap-api.md) lists the supported interfaces.

## Use frozen external tools

You need this section only if a scoped test runs an external program that is installed
outside the Python environment and the system directories, such as a Node runtime below
`/opt`. Other projects can skip it.

A scoped test sees the declared project files, the installed Python environment and the
system directories. An installation elsewhere is absent. To let a test use one, pin its
files as a *frozen tool*: register an existing installation manifest and select it on
the test that uses it:

```toml
[tool.pytest_telescope.tools.frontend]
format = "frozen-tool-v1"
manifest = "tool-locks/frontend.json"
```

```python
@pytest.mark.telescope(tools=["frontend"])
```

The manifest pins the actual installation bytes and required runtime, command, link and
lock relations. The test keeps its existing body and subprocess calls. Selecting the
tool shows the test only those pinned files and makes them part of that test's cache
key. An unchanged
package version or a read-only live mount is insufficient. Start with the [complete
data-only example](bubblewrap-api.md#a-complete-data-only-example), then use the
[manifest reference](bubblewrap-api.md#frozen-tool-manifest) for native, browser or
installed Python closures. Unsupported layouts refuse; this feature does not install
tools. The [capture and reuse
contract](execution-contract.md#frozen-tool-capture-and-reuse) explains when changing
one tool reruns the tests that select it, and why a stale pin can refuse a run even when
pytest selectors choose only a test that uses another tool. Selecting a native tool,
that is one with native executables or libraries, also closes `/etc`, library search
directories and Python startup to pinned files; see [native tools at a
glance](bubblewrap-api.md#native-tools-at-a-glance).

```{toctree}
:hidden:
:maxdepth: 1
:caption: Use pytest-telescope

Overview and quickstart <self>
Marker and project reference <specification>
Ordinary and Bubblewrap execution <bubblewrap>
API and configuration <bubblewrap-api>
Declarations, inventory and reuse <execution-contract>
Optional batches <batching>
Metadata and suites <scoped-execution>
Run result reference <result-schema>
Benchmark <benchmark>
```

```{toctree}
:hidden:
:maxdepth: 1
:caption: Contribute

Development <development>
Publishing <publishing>
Frozen Git design (not implemented) <design-frozen-git>
```
