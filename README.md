# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

Put input declarations beside pytest cases. Each actual case, including each parametrized
variant, runs in a fresh Linux Docker worker and has its own reusable successful result.
Two cases in the same file can declare different resources and rerun independently.

```python
import pytest


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red(): ...


@pytest.mark.telescope(inputs=["data/blue.json"], bundles=["pricing"])
def test_blue(): ...
```

**Unmarked cases see and hash the whole project-file snapshot.** Literal `inputs` or
`bundles` marker fields narrow that scope. Declarations at module, class, function, and
parameter levels add together with project defaults. `cache=False` alone stays broad.
The containing test source and configuration are always included.

## Try it

Requirements: Linux, Python 3.11+, and permission to build/run Linux containers through
a local Docker daemon's Unix socket. Runtime preparation needs network; workers have no
network.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install pytest-telescope==0.1.0a3
```

Follow the [complete runnable quickstart](https://pytest-telescope.readthedocs.io/en/latest/)
for copyable application code, tests, JSON, and TOML. It demonstrates cold/warm reuse,
same-file independent inputs, parameter-specific declarations, shared bundles, broad
invalidation, and an undeclared read failure. The base files are also in
[examples/quickstart](https://github.com/xu-hao/pytest-telescope/blob/main/examples/quickstart).

From your configured project root:

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

`plan` previews static scopes. `run` collects actual cases inside an isolated worker;
each execution then starts with only that case's scope. A warm inventory/result hit
starts no worker containers. Summaries distinguish collection, fresh execution, cached
results, failures, and unselected cases. New files or parametrized cases cannot hide
behind an old result.

## Know the boundary

Every case gets a fresh pytest session, so module/class/session fixtures are recreated
per case. Tests relying on cross-case mutable state need rewriting. Shared import and
parametrization resources must be declared for every narrow consumer, typically in
project defaults or bundles. Application files and host virtual environments are not
installed into the prepared runtime.

Filesystem isolation does not establish universal determinism. Make clock, randomness,
and native/external-state assumptions reproducible or mark affected cases `cache=False`.
Use `run --force` to execute again or `run --no-cache` to bypass reuse and recording.

The dedicated CLI starts no host pytest session. `pytest --telescope` also routes to
workers, but other plugins may execute during host pytest startup. `pytest
--telescope-plan` is a static planning adapter. Ordinary pytest stays ordinary when
telescope mode is disabled; `-p no:telescope` disables plugin loading entirely.

**Version note:** 0.1.0a3 adds [scoped project metadata, independent suites and explicit parallel budgets](https://pytest-telescope.readthedocs.io/en/latest/scoped-execution/).
The marker interface introduced in 0.1.0a2 remains supported. Immutable 0.1.0a1 used file/group
targets; central `targets` declarations must migrate to markers, defaults, and bundles.
The [benchmark](https://github.com/xu-hao/pytest-telescope/blob/main/docs/benchmark.md)
records measured per-case cold/warm and selective reruns, including broad-case costs.

See the [marker/project reference](https://github.com/xu-hao/pytest-telescope/blob/main/docs/specification.md), [execution contract](https://github.com/xu-hao/pytest-telescope/blob/main/docs/execution-contract.md),
[development guide](https://github.com/xu-hao/pytest-telescope/blob/main/docs/development.md), and [publishing guide](https://github.com/xu-hao/pytest-telescope/blob/main/docs/publishing.md).
Licensed under the [MIT License](https://github.com/xu-hao/pytest-telescope/blob/main/LICENSE).
