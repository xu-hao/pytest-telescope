# pytest-telescope

pytest-telescope is a pytest plugin that avoids rerunning tests whose inputs have not
changed. It is for Linux projects whose tests run often, for example in CI or in a merge
queue that checks many changes, where rerunning unaffected tests wastes time.

It treats each test in one of two ways:

- A *scoped* test declares the project files it reads. It runs in a
  [Bubblewrap](https://github.com/containers/bubblewrap) sandbox that shows only those
  project files, read-only, together with the installed Python environment and system
  directories. When it passes, pytest-telescope saves the result. A later run reuses that
  result instead of running the test again, as long as the declared files, the
  interpreter and the installed packages are unchanged.
- An *ordinary* test declares nothing. It runs every time as plain pytest, with no
  sandbox and no cache.

You can adopt it gradually: scope one test, then another, in any order. Every test you
have not scoped keeps running as before.

## A minimal example

This example assumes pytest-telescope is installed in your test environment; see
[Add it to your project](#add-it-to-your-project) below. `pyproject.toml` tells pytest-telescope where the tests are:

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

Run `python -m pytest_telescope run` twice from the directory that contains
`pyproject.toml`. Each run prints a JSON report in place of pytest's usual output. The
first run executes both tests. The second runs `test_note` again and reuses the result
of `test_red`. Edit `data/red.json` and `test_red` runs again. Inside its sandbox,
`test_red` can read `data/red.json`, its own test file and `pyproject.toml`; other
project files do not exist there, so a test cannot quietly read an undeclared data file.
Tests can write temporary files in private scratch, including through pytest's
`tmp_path` fixture.

## Add it to your project

Requirements: Linux, Python 3.11+, and `bwrap` from Bubblewrap on a host that lets it
create user, mount and PID namespaces. From your existing checkout, activate the
environment you normally use for tests:

```sh
python -m pytest -q
python -m pip install pytest-telescope
```

Follow the
[quickstart](https://pytest-telescope.readthedocs.io/en/latest/#quickstart-add-it-to-an-existing-project)
to connect your existing pytest configuration and declare one test's inputs. You can
adopt it a test at a time, using the fixtures and dependencies you already have.

Then, from the directory that contains your `pyproject.toml`:

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

`plan` shows which tests will be scoped and which files they can read, without importing
your code. Pytest arguments go after `--`, and `--explain` adds the reason each case was
executed or reused to the report. With an empty cache, the first `run` executes the
tests. The second runs ordinary tests again and reuses successful scoped results whose
inputs still match. The JSON report counts executed and reused cases separately. Every
run looks for new test files and new parametrized cases before it accepts a cached
result, so new tests always run.

In CI, keep the result cache between jobs, or use `run --cache-dir PATH` to choose its
location. The default is `.pytest-telescope/cache`. Each run checks whether its inputs
match a saved result; restoring a cache alone does not make a result reusable. Measure
cold and warm runs of your own suite: starting a sandbox and pytest session for each
scoped test adds overhead, especially for short tests.

## How scoped tests run

The sandbox controls which files tests can see. Limit and measure CPU, memory, processes
and scratch space in the job that runs pytest. Tests share the host network; leave tests
that depend on changing network services ordinary, or disable caching for them.
A scoped test cannot see the checkout's `.git` directory or its history: declarations
cannot name it.
The cache also checks the interpreter and installed distributions, so installing or
upgrading a package reruns scoped tests. An editable install of the project
(`pip install -e .`) resolves to the declared files, including in
isolated `python -I` child processes.

The default `case` mode creates a fresh sandbox and pytest session for every scoped case
that needs to run. Session, module and class fixtures are recreated for each case.
Optional [batch mode](https://pytest-telescope.readthedocs.io/en/latest/batching/) lets
selected scoped cases share ordinary pytest fixture lifetimes, but only cases in the same
suite and module whose scopes are exactly equal: the same readable files, declared
absent paths, metadata and cache policy. `run --workers N` runs up to N independent
sandboxes at once.

A run *refuses* when it cannot proceed safely: it stops with an explicit result and
never falls back to ordinary execution. If Bubblewrap is missing or cannot create
namespaces, a run with scoped cases refuses before any test runs. Some other refusals
are found only after ordinary tests have run, so read the result before you retry.

A cache is useful only when the inputs explain the result. If a test depends on the
current time, randomness or external state, make that dependency reproducible or leave
the test uncached. Adding `cache=False` to a scoped declaration keeps the sandbox but
runs the test every time. `cache=False` alone leaves a test ordinary. Use `run --force`
to rerun cached tests or `run --no-cache` to bypass reading and recording results.

A scoped test is meant to be *location-blind*: it cannot tell where its checkout lives,
so identical checkouts at different paths share scoped results. It sees its project at
`/project`, wherever the checkout lives.

**Not location-blind:** two things a scoped test can read still name its checkout's
location, which its cache key does not contain:

- the source directories in the mount table (field 4 of `/proc/*/mountinfo`);
- the raw bytes of cached bytecode files (`__pycache__/*.pyc`) of an environment inside
  the project.

A reused result may have been produced by a checkout at another path, where the test
would have read a different value.
Leave a test that reads either one ordinary, or add `cache=False` to its scope. See
[what a scoped test can still see](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap/#what-a-scoped-test-can-still-see).

You can also use `pytest --telescope` and `pytest --telescope-plan`. Other pytest plugins
may run during startup with these commands; `python -m pytest_telescope` avoids that
initial host pytest session. Running pytest without telescope mode keeps its usual
behavior. `-p no:telescope` disables the plugin entirely.

## Learn more

- [Ordinary and Bubblewrap execution](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap/):
  what a scoped test can see and what makes a saved result reusable.
- [Marker and project reference](https://pytest-telescope.readthedocs.io/en/latest/specification/):
  every marker field and `pyproject.toml` setting.
- [API and configuration](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap-api/):
  the Python API, command line and capability codes.
- [Declarations, inventory and reuse](https://pytest-telescope.readthedocs.io/en/latest/execution-contract/):
  the exact rules for declarations and cache reuse.
- [Development guide](https://pytest-telescope.readthedocs.io/en/latest/development/) and
  [publishing guide](https://pytest-telescope.readthedocs.io/en/latest/publishing/), for
  contributors and maintainers.

Licensed under the [MIT License](https://github.com/xu-hao/pytest-telescope/blob/main/LICENSE).
