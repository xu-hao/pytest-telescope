# pytest-telescope

> Per-scope environment inputs, frozen tools, helper ownership and the opt-in fixture
> guard are delivered in version 0.1.0. Frozen-checkout/history support is deferred
> and is not implemented by this version.

Contributors qualify host-specific tool installations with the separately invoked
[installed-runtime benchmarks](benchmark.md#installed-runtime-compatibility-benchmarks).
All [actual browser execution](benchmark.md#browser-execution-benchmarks) is benchmark
work outside functional readiness, as are the 14 actual Node and installed-distribution
qualification nodes in the [migration ledger](benchmark.md#program-and-installed-distribution-migration-ledger).
Ordinary Python/pytest/Bubblewrap correctness and portable integration tests remain
functional.

pytest-telescope caches pytest results so repeated checks spend less time running tests
whose inputs haven't changed. If coding agents are adding changes to a merge queue,
reusing those results can reduce the test work each queued change needs.

You declare the project files each test reads. pytest-telescope runs it in a Linux
[Bubblewrap](https://github.com/containers/bubblewrap) sandbox, where other project files
are absent, and saves successful results for reuse. It checks the declared inputs,
interpreter and installed packages before accepting a cached result. Tests without
declarations run every time as ordinary pytest.

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
python -m pip install pytest-telescope==0.1.0a6
```

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

If you keep pytest settings in `pytest.ini`, `.pytest.ini`, `tox.ini` or `setup.cfg`, or
need explicit pytest plugins, use a named suite in place of top-level `test_roots`:

```toml
[tool.pytest_telescope.suites.tests]
root = "."
config = "pytest.ini"
test_roots = ["tests"]
plugins = []
```

Set `config` to your actual configuration file, which can also be `pyproject.toml`.
List required installed plugin modules in `plugins`, for example
`plugins = ["pytest_asyncio.plugin"]`; scoped sessions disable plugin autoloading.
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
add a bundle to `pyproject.toml`:

```toml
[tool.pytest_telescope.bundles.parser]
inputs = ["src/my_package", "tests/conftest.py", "tests/helpers.py"]
```

Replace these paths with files and directories in your checkout. Include applicable
parent `conftest.py` files, imported test helpers and resources used by fixtures. Remove
entries that do not exist: a missing declared path is an error. Starting with the package
directory is convenient; you can narrow it to individual source files later.

Import `pytest` if the test module does not already import it. Put this literal decorator
directly above your existing test function, keeping its body and fixture arguments:

```python
@pytest.mark.telescope(inputs=["tests/data/valid.json"], bundles=["parser"])
```

Use the data file that your test actually reads. Paths are relative to the project root.
The containing test source and configuration are automatic inputs; application source,
conftest files, helpers and data need declarations.

This makes the test *scoped*: it sees the declared project files read-only, plus the
installed environment and system directories. Undeclared project files are absent.
Writes belong in private scratch, for example through pytest's `tmp_path` fixture.
In published 0.1.0a6, tests without an `inputs`, `bundles` or `distributions` declaration remain ordinary,
including other tests in the same file.
Version 0.1.0 also supports the `env` and `tools` scope fields
described in the development references below.

### 4. Run the test twice

From the directory containing `pyproject.toml`:

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- tests/unit/test_parser.py::test_accepts_valid_record -q
python -m pytest_telescope run --explain -- tests/unit/test_parser.py::test_accepts_valid_record -q
```

Replace the node selector with your test's path and name, using the same arguments on both
runs. `plan` reads declarations without importing project code and shows which files
each test can read. Pytest discovers the actual parametrized cases when you run the
tests. A node selector chooses what to run, but pytest may still collect other configured
modules. Use the suite's `include` and `exclude` settings to leave modules out before
they are parsed or imported.

If your test has one case and passes, the first run with an empty cache reports one
executed case. The second reports one reused case. Look for these fields in the JSON
summaries:

```json
{"counts": {"executed": 1, "reused": 0}}
{"counts": {"executed": 0, "reused": 1}}
```

`reused` means pytest-telescope accepted the previous result without running the case
again. Parametrized tests have a result for each case, so their counts can be larger.

If a scoped run fails to import a project module or read a fixture file, check the
files listed by `plan` and add the missing dependency. Pytest fixtures still run normally,
but their files must be declared. If Bubblewrap is missing or namespaces are unavailable, the
run refuses; use a host that meets the [requirements](bubblewrap-api.md#host-requirements).
A refusal can occur after earlier tests have run, so inspect the result before retrying.
The development [late-refusal accounting contract](result-schema.md#retained-observations-after-a-late-refusal)
retains available observations without changing the typed refusal into a successful run.
A plugin's nonzero session verdict can fail a run even when its case bodies passed.
Those observed passes remain visible; a declaration refusal is identified separately
by the [strict worker protocol](result-schema.md#internal-protocol-and-cache-versions).

### 5. Change an input and run again

Make a normal change to one of the test's declared inputs and repeat the command. The
case executes again; a passing result becomes reusable on the next unchanged run.

| Change | Effect on scoped results |
| --- | --- |
| Declared source, helper or data file changes | Its consumers execute again. |
| Containing test file changes | Scoped cases in that file execute again. |
| Project configuration changes | Scoped cases execute again. |
| Installed distributions or interpreter change | Scoped cases execute again. |
| File outside a case's declared inputs changes | It does not invalidate that case. |

Add declarations to more tests as you establish their dependencies. Use a module-level
literal `pytestmark` when every test in a module should share a scope; use
`pytest.param(..., marks=pytest.mark.telescope(...))` for parameter-specific inputs.
Declarations at module, class, function and parameter levels add together for each
case's actual ancestors and selected parameter alternatives. Copying a sibling's Mark
does not make its declaration inherited; see
[additive inheritance](specification.md#additive-inheritance). A literal
`env=["OPTIONAL_FEATURE"]` marker in the development candidate scopes and forwards that
name only to its consumers; `env=[]` also scopes a case. Published 0.1.0a6 supports the
top-level `env` setting, which applies to every scoped case. The
[marker reference](specification.md) shows the supported forms, defaults and bundles.

Run the whole suite with your usual pytest arguments after `--`:

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

Every run executes the selected ordinary cases in one fresh pytest session per suite.
Scoped cases use a cached result when they can and run in a sandbox when they cannot.
`route_counts` breaks down the executed and reused counts for each route.

Frozen-checkout/history capture is deferred and is not implemented by the current
candidate or published 0.1.0a6 package. The separate
[Git design contract](bubblewrap.md#frozen-git-checkout-development-contract) records
that future interface; it is not a runnable current declaration. Selecting a Git
executable as an ordinary frozen tool for an owned scratch repository is a different use.

## Opt into frozen external tools (development contract)

Contributor [program qualification](development.md#controlled-program-qualification)
uses explicit runtime prerequisites for separately selected Node and installed-plugin benchmarks.
The [installed gate](development.md#installed-candidate-and-functional-prerequisites)
verifies the exact candidate wheel without preparing those program profiles.
Full-browser execution is a separately invoked benchmark. These development interfaces
are not part of the published release.

For a candidate implementing frozen tools, register an existing installation manifest
and add the selection to its consuming test:

```toml
[tool.pytest_telescope.tools.frontend]
format = "frozen-tool-v1"
manifest = "tool-locks/frontend.json"
```

```python
@pytest.mark.telescope(tools=["frontend"])
```

The manifest pins the actual installation bytes and required runtime, command, link and
lock relations. The test keeps its existing body and subprocess calls. Selecting it
exposes only the frozen closure and binds its identity to those consumers. An unchanged
package version or a read-only live mount is insufficient. Start with the
[complete data-only example](bubblewrap-api.md#a-complete-data-only-example), then use
the [manifest reference](bubblewrap-api.md#frozen-tool-manifest-development-contract)
for native, browser or installed Python closures. Unsupported layouts refuse; this
feature does not install tools. Published 0.1.0a6 does not implement this opt-in.
The [capture and reuse contract](execution-contract.md#frozen-tool-capture-and-reuse)
distinguishes an independently reusable case record from admission of the structurally
required collection union, including when pytest selectors choose only one consumer.

Selecting a native closure also closes system configuration: `/etc/resolv.conf` is
absent unless the selected manifest pins it and preserves its path. Required certificate
or fixture configuration likewise appears only as declared frozen bytes, alongside the
required loader controls. A data-only tool without ELF files or a loader profile keeps
the existing system view, as do nonconsumers. See
[declared native configuration](bubblewrap-api.md#declared-native-system-configuration)
for the exact trigger and examples; dependencies remain the caller's responsibility.
Native worker startup also requires the
[supported CPython prefix closure](bubblewrap-api.md#invoking-python-startup-closure).
When `/usr` is its base prefix, unselected files throughout `/usr` are absent; pin the
actual worker, stdlib, pytest dependencies and any commands the test needs.
For native libraries, the [verified ordering subset](bubblewrap-api.md#verified-native-library-ordering)
keeps executable process contexts and their declared native resources explicit. It
supports proven search ordering and refuses ambiguous ancestry or loaded-name conflicts;
it does not discover arbitrary program dependencies or promise ABI compatibility.

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

In the default `case` mode, each scoped case that needs to run starts a new sandbox and
pytest session.
Session, module and class fixtures are recreated for each scoped case. Ordinary tests
share normal pytest fixture lifetimes within their suite's ordinary session. A module
containing both routes is imported during ordinary collection; use a module-level scope
when the whole module, including its import, should be collected only in a sandbox.

The required but unreleased [fixture guard](batching.md#required-fixture-guard-unreleased)
will conservatively refuse possible shared user fixture lifetimes split across exact-scope
groups. Its adapter and real Bubblewrap behavior still require qualification.

Optional [batch mode](batching.md) shares fixture lifetimes among selected scoped cases
in the same module and suite with exactly the same readable files, declared absence,
metadata and cache policy. Use `run --execution-mode batch` when those tests can share
fixtures. `run --workers 4` runs up to four independent scoped sandboxes at once.

`@pytest.mark.telescope(cache=False)` alone leaves a test ordinary. Together with a scope,
as in `inputs=[...], cache=False`, it keeps the test sandboxed and fresh on every run,
without recording a reusable result. Use `run --force` to execute scoped successes again
or `run --no-cache` to bypass result reading and recording.

**Not location-blind:** identical checkouts at different paths share scoped results, and
a scoped test sees its project at `/project` wherever the checkout lives.
A test that reads the source directories in the mount table (field 4 of
`/proc/*/mountinfo`) can observe its checkout's location, which its cache key does not
contain. Leave such a test ordinary, or add `cache=False` to its scope.
See [what a scoped test can still see](bubblewrap.md#what-a-scoped-test-can-still-see).

The sandbox controls which files tests can see. Limit CPU, memory, processes and scratch
space in the job that runs pytest. Tests share the host network. Before caching a test
that uses the clock or randomness, give it a reproducible time or seed. The
[execution contract](bubblewrap.md) describes the view and cache identity, and the
[API reference](bubblewrap-api.md) lists the supported interfaces.

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
