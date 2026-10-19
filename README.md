# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

pytest-telescope uses one manifest to define which project files a test target can read
and which changes invalidate its previous successful result. Each target is a fresh
pytest session inside a Linux Docker container. Successful unchanged targets can reuse
their recorded results; reused cases are reported separately from executed cases.

**Alpha (0.1.0a1):** implemented and qualified on Linux with real Docker workers. The
[benchmark](https://github.com/xu-hao/pytest-telescope/blob/main/docs/benchmark.md)
records measured cold, warm, and selective reruns, including their overhead. The
supported boundary and reuse assumptions are explicit; arbitrary Python determinism is
not guaranteed.

## Quickstart

Requirements: Linux, Python 3.11+, and a working local Docker daemon exposed through a
Unix socket, with permission to build and run Linux containers. Runtime preparation
needs network access; test workers have no network. Install from this checkout in a
virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

From your project's root, add a manifest to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]

[tool.pytest_telescope]
test_roots = ["tests"]

[tool.pytest_telescope.bundles.common]
inputs = ["tests/conftest.py", "app/__init__.py"]

[tool.pytest_telescope.targets.parser]
tests = ["tests/test_parser.py"]
inputs = ["app/parser.py", "tests/data/input.json"]
bundles = ["common"]
```

Declare every fixture, helper, application module, and resource your target needs. Then
prepare a clean runtime and execute:

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The second identical run can reuse complete successful targets. A missing input is an
error, not permission to expose more of your checkout. See the
[specification](https://github.com/xu-hao/pytest-telescope/blob/main/docs/specification.md)
for declarations, grouping, invalidation, limits, and troubleshooting, and the
[synthetic
example](https://github.com/xu-hao/pytest-telescope/blob/main/examples/analytics/README.md)
for an executable three-target project.

```sh
python -m pytest_telescope run --force -- -q
python -m pytest_telescope run --no-cache -- -q
python -m pytest_telescope run --timeout 120 -- -k parser
```

`--force` executes even when a successful record exists. `--no-cache` disables reuse and
recording. For permanently non-cacheable targets, set `cache = false`. Clocks,
randomness, environment, native state, and external services can invalidate assumptions
about reuse: filesystem isolation does not prove arbitrary Python programs
deterministic.

## Runtime and pytest integration

The runtime contains a pinned Python base image and wheel-installed dependencies, not
your host virtual environment or an editable copy of your application. For additional
third-party packages, pass a requirements file containing the complete transitive
dependency closure as exact `name==version` wheel dependencies:

```sh
python -m pytest_telescope runtime --requirements requirements.txt
```

The dedicated CLI starts no host pytest session. `pytest --telescope` also routes to
isolated workers, but other installed plugins may execute during host pytest startup.
`pytest --telescope-plan` remains available for static planning. Ordinary pytest stays
ordinary when telescope mode is disabled; use `-p no:telescope` to disable plugin
loading entirely.

See
[development](https://github.com/xu-hao/pytest-telescope/blob/main/docs/development.md),
[benchmark
methodology](https://github.com/xu-hao/pytest-telescope/blob/main/docs/benchmark.md),
and
[publishing](https://github.com/xu-hao/pytest-telescope/blob/main/docs/publishing.md).
Licensed under the [MIT
License](https://github.com/xu-hao/pytest-telescope/blob/main/LICENSE).
