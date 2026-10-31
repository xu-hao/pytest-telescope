# pytest-telescope

pytest-telescope caches pytest results to reduce repeated test work in merge queues.
When coding agents produce changes faster than CI can check them, rerunning tests whose
inputs haven't changed adds to the wait.

Tell pytest-telescope which project files a test needs. It runs the test in a Linux
[Bubblewrap](https://github.com/containers/bubblewrap) sandbox and saves a successful
result. On later runs, it can reuse that result if the declared inputs and test
environment still match. Tests without input declarations run every time as ordinary
pytest.

For example, these two tests use different data files:

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...


@pytest.mark.telescope(inputs=["data/blue.json"], bundles=["pricing"])
def test_blue(): ...


def test_note(): ...  # ordinary: runs every time, no sandbox, no cache
```

Change `data/red.json` and `test_red` runs again; `test_blue` can reuse its result.
Both tests also depend on the files in the configured `pricing` bundle. Changing a
shared input reruns both. `test_note` runs every time.

A test with an input declaration is called *scoped*. Its sandbox shows the declared
project files, the installed Python environment and system directories, all read-only.
Other project files are absent, so a test cannot quietly read an undeclared data file.
Tests can write temporary files in private scratch, including through pytest's
`tmp_path` fixture.

## Add it to your project

Requirements: Linux, Python 3.11+, and `bwrap` from Bubblewrap on a host that lets it
create user, mount and PID namespaces. From your existing checkout, activate the
environment you normally use for tests:

```sh
python -m pytest -q
python -m pip install pytest-telescope==0.1.0a6
```

Follow the [quickstart](https://pytest-telescope.readthedocs.io/en/latest/)
to connect your existing pytest configuration and declare one test's inputs. You can
adopt it a test at a time, using the fixtures and dependencies you already have.

From your configured project root:

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

`plan` shows which tests will be scoped and which files they can read, without importing
your code. With an empty cache, the first `run` executes the tests. The second runs
ordinary tests again and reuses successful scoped results whose inputs still match.
The JSON summary reports
executed and reused cases separately. New test files and parametrized cases are
discovered before cached results are accepted.

In CI, keep the result cache between jobs, or use `run --cache-dir PATH` to choose its
location. The default is `.pytest-telescope/cache`. Each run checks whether its inputs
match a saved result; restoring a cache alone does not make a result reusable. Measure
cold and warm runs of your own suite: starting a sandbox and pytest session for each
scoped test adds overhead, especially for short tests.

## How scoped tests run

The sandbox controls which files tests can see. Limit and measure CPU, memory, processes
and scratch space in the job that runs pytest. Tests share the host network; leave tests
that depend on changing network services ordinary, or disable caching for them.
The cache also checks the interpreter and installed distributions, so installing or
upgrading a package reruns scoped tests. An editable install of the project
(`pip install -e .`) resolves to the declared files, including in
isolated `python -I` child processes.

The default `case` mode creates a fresh sandbox and pytest session for every scoped case
that needs to run. Session, module and class fixtures are recreated for each case.
Optional [batch mode](https://pytest-telescope.readthedocs.io/en/latest/batching/) shares
ordinary pytest fixture lifetimes only among selected scoped cases with exactly equal
scopes in one suite/module. `run --workers N` runs up to N independent sandboxes at once.
If Bubblewrap is missing or cannot create namespaces, a run with scoped cases refuses
before any test runs; it never falls back to ordinary execution.

A cache is useful only when the inputs explain the result. If a test depends on the
current time, randomness or external state, make that dependency reproducible or leave
the test uncached. Adding `cache=False` to a scoped declaration keeps the sandbox but
runs the test every time. `cache=False` alone leaves a test ordinary. Use `run --force`
to rerun cached tests or `run --no-cache` to bypass reading and recording results.

You can also use `pytest --telescope` and `pytest --telescope-plan`. Other pytest plugins
may run during startup with these commands; `python -m pytest_telescope` avoids that
initial host pytest session. Running pytest without telescope mode keeps its usual
behavior. `-p no:telescope` disables the plugin entirely.

See the [execution contract](https://github.com/xu-hao/pytest-telescope/blob/main/docs/bubblewrap.md),
[API reference](https://github.com/xu-hao/pytest-telescope/blob/main/docs/bubblewrap-api.md),
[marker/project reference](https://github.com/xu-hao/pytest-telescope/blob/main/docs/specification.md),
[development guide](https://github.com/xu-hao/pytest-telescope/blob/main/docs/development.md), and
[publishing guide](https://github.com/xu-hao/pytest-telescope/blob/main/docs/publishing.md).
Licensed under the [MIT License](https://github.com/xu-hao/pytest-telescope/blob/main/LICENSE).
