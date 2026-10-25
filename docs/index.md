# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Put input declarations beside your existing pytest tests. A declared test runs in a Linux
[Bubblewrap](https://github.com/containers/bubblewrap) sandbox with its declared project
files and your installed Python environment, read-only. Its successful result can be
reused until those inputs change. Tests without declarations run as ordinary pytest on
every invocation.

## Quickstart: add it to an existing project

Start with a project whose tests already pass under pytest. Adopt pytest-telescope one
test or module at a time; keep your application, test bodies, fixtures and dependency
installation workflow.

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

Merge this into the checkout's `pyproject.toml`, adjusting `tests` to your test directory:

```toml
[tool.pytest_telescope]
test_roots = ["tests"]
```

This uses the pytest settings already in `[tool.pytest.ini_options]`. Keep those settings,
including import paths and registered markers. Create `pyproject.toml` if needed; do not
replace your existing project metadata or repeat a table that is already present.

If you keep pytest settings in `pytest.ini`, `.pytest.ini`, `tox.ini` or `setup.cfg`, or
need explicit pytest plugins, use a named suite **instead of** top-level `test_roots`:

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
its name in `env` under `[tool.pytest_telescope]`; its presence and value then participate
in cache identity. See the [project configuration reference](specification.md#project-configuration).

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

This makes that case **scoped**. It sees the declared project files read-only, plus the
installed environment and system directories. Undeclared project files are absent.
Writes belong in private scratch, for example through pytest's `tmp_path` fixture.
Tests without an `inputs`, `bundles` or `distributions` declaration remain ordinary,
including other tests in the same file.

### 4. Preview, then run the selected test twice

From the directory containing `pyproject.toml`:

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- tests/unit/test_parser.py::test_accepts_valid_record -q
python -m pytest_telescope run --explain -- tests/unit/test_parser.py::test_accepts_valid_record -q
```

Replace the node selector with your test's path and name, using the same arguments on both
runs. `plan` previews routes and effective scopes without importing project code; it does
not enumerate actual parametrized cases. Pytest selection happens within the configured
inventory, so other modules may still be collected. To exclude modules before parsing or
importing them, use the suite's structural `include` and `exclude` settings.

For a single selected, passing scoped case with no previous cache entry, the cold run
reports one executed case. The unchanged warm run reports one reused case. These are
selected fields from the JSON summaries:

```json
{"counts": {"executed": 1, "reused": 0}}
{"counts": {"executed": 0, "reused": 1}}
```

A reused success records a previous execution. It is not a test executed on the warm run.
Parametrized tests have one result per actual case, so their counts can be larger.

If a scoped run fails to import a project module or read a fixture file, check the
effective scope and add the missing dependency. Pytest fixtures still run normally, but
their files must be declared. If Bubblewrap is missing or namespaces are unavailable, the
run refuses; use a host that meets the [requirements](bubblewrap-api.md#host-requirements).
A refusal can occur after earlier tests have run, so inspect the result before retrying.

### 5. Verify invalidation, then expand adoption

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
Declarations at module, class, function and parameter levels add together. The
[marker reference](specification.md) shows the supported forms, defaults and bundles.

Run the whole suite with your usual pytest arguments after `--`:

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

Every invocation executes selected ordinary cases in one fresh pytest session per suite.
Scoped cases reuse eligible successes or execute in their sandbox. Check `route_counts`
to distinguish ordinary executions, scoped executions and scoped reuse. A mixed suite
continues to execute its ordinary cases on warm runs. Use this same command in CI after
installing your test dependencies and ensuring Bubblewrap is available.

## Reuse policy and fixture lifetime

In the default `case` mode, every scoped case starts a new sandbox and pytest session.
Session, module and class fixtures are recreated for each scoped case. Ordinary tests
share normal pytest fixture lifetimes within their suite's ordinary session. A module
containing both routes is imported during ordinary collection; use a module-level scope
when the whole module, including its import, should be collected only in a sandbox.

Optional [batch mode](batching.md) shares fixture lifetimes among selected scoped cases
with exactly equal scopes in one suite/module. Use `run --execution-mode batch` when that
lifetime suits the tests. `run --workers 4` runs up to four independent scoped sandboxes
at once.

`@pytest.mark.telescope(cache=False)` alone leaves a test ordinary. Together with a scope,
as in `inputs=[...], cache=False`, it keeps the test sandboxed and fresh on every run,
without recording a reusable result. Use `run --force` to execute scoped successes again
or `run --no-cache` to bypass result reading and recording.

The sandbox controls the filesystem view only. Limit CPU, memory, processes and scratch
in the job that runs pytest. Scoped tests share the host network. Make clock and
randomness assumptions reproducible before relying on reuse. The
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
