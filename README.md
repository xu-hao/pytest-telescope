# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Put input declarations beside pytest cases. A case with a declaration runs in a Linux
[Bubblewrap](https://github.com/containers/bubblewrap) sandbox that shows it only its
declared project files, and has its own reusable successful result. Two cases in the
same file can declare different resources and rerun independently. Every other case runs
as ordinary pytest.

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...


@pytest.mark.telescope(inputs=["data/blue.json"], bundles=["pricing"])
def test_blue(): ...


def test_note(): ...  # ordinary: runs every time, no sandbox, no cache
```

**Undeclared cases stay ordinary.** A case without a literal `inputs`, `bundles` or
`distributions` field runs in an ordinary pytest session on every invocation. A declared
case is **scoped**: it sees its declared project files, the installed Python environment
and the system directories, all read-only, plus private scratch. Undeclared project
files do not exist in its view. Declarations at module, class, function, and parameter
levels add together with project defaults. `cache=False` alone leaves a case ordinary.

## Try it

Requirements: Linux, Python 3.11+, and `bwrap` from Bubblewrap on a host that lets it
create user namespaces. There is no runtime image to build.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install pytest-telescope==0.1.0a6
```

Follow the [complete runnable quickstart](https://pytest-telescope.readthedocs.io/en/latest/)
for copyable application code, tests, JSON, and TOML. The base files are also in
[examples/quickstart](https://github.com/xu-hao/pytest-telescope/blob/main/examples/quickstart).

From your configured project root:

```sh
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

`plan` previews static routes and scopes. `run` runs ordinary cases in one ordinary
pytest session per suite and each scoped miss in its own sandbox. The second run executes
the ordinary cases again and reuses unchanged scoped successes. Summaries distinguish
routes, collection, fresh execution, cached results, failures, and unselected cases. New
files or parametrized cases cannot hide behind an old result.

## Know the boundary

The sandbox controls the **filesystem view only**. It sets no CPU, memory, process or
scratch limits and reports no CPU usage: limit and measure resources in the job that runs
pytest. Scoped tests share the host network, so do not declare network-dependent tests
as scoped. Scoped results are keyed by their declared files, the interpreter and the
installed distributions; installing or upgrading a package reruns them. An editable
install of the project (`pip install -e .`) resolves to the declared files, including in
isolated `python -I` child processes.

The default `case` mode creates a fresh sandbox and pytest session for every scoped case.
Optional [batch mode](https://pytest-telescope.readthedocs.io/en/latest/batching/) shares
ordinary pytest fixture lifetimes only among selected scoped cases with exactly equal
scopes in one suite/module. `run --workers N` runs up to N independent sandboxes at once.
If Bubblewrap is missing or cannot create namespaces, a run with scoped cases refuses
before any test runs; it never falls back to ordinary execution.

Filesystem isolation does not establish universal determinism. Make clock, randomness,
and native/external-state assumptions reproducible. Use `run --force` to execute again or
`run --no-cache` to bypass reuse and recording.

The dedicated CLI starts no host pytest session before its own sessions. `pytest
--telescope` also routes to the controller, but other plugins may execute during host
pytest startup. `pytest --telescope-plan` is a static planning adapter. Ordinary pytest
stays ordinary when telescope mode is disabled; `-p no:telescope` disables plugin loading
entirely.

**Version note:** 0.1.0a6 accepts moved checkouts whose pytest assertion-rewrite bytecode
still names the original path. 0.1.0a5 shows each scoped test's frozen declared files at the project's
own host path as well as at `/project`, so an editable install of the project resolves to
declared source in isolated child interpreters; see the
[changelog](https://github.com/xu-hao/pytest-telescope/blob/main/CHANGELOG.md).
0.1.0a4 replaced the Docker backend of 0.1.0a1 to 0.1.0a3 with ordinary
pytest plus Bubblewrap filesystem views; the `runtime` command, resource budgets and
runtime profiles are gone. The marker interface introduced in 0.1.0a2 remains supported.
Immutable 0.1.0a1 used file/group targets; central `targets` declarations must migrate to
markers, defaults, and bundles.

See the [execution contract](https://github.com/xu-hao/pytest-telescope/blob/main/docs/bubblewrap.md),
[API reference](https://github.com/xu-hao/pytest-telescope/blob/main/docs/bubblewrap-api.md),
[marker/project reference](https://github.com/xu-hao/pytest-telescope/blob/main/docs/specification.md),
[development guide](https://github.com/xu-hao/pytest-telescope/blob/main/docs/development.md), and
[publishing guide](https://github.com/xu-hao/pytest-telescope/blob/main/docs/publishing.md).
Licensed under the [MIT License](https://github.com/xu-hao/pytest-telescope/blob/main/LICENSE).
