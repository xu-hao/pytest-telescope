# API, CLI and configuration

This reference lists the interfaces of the [ordinary and Bubblewrap execution
contract](bubblewrap.md). The [result reference](result-schema.md) defines every schema 3
field, and [complete examples](bubblewrap-schema-examples.json) show whole reports.

## Python API

```python
from pytest_telescope.controller import run, error_result
from pytest_telescope.plan import make_plan

run(root, *, force=False, no_cache=False, explain=False, cache_dir=None, timeout=60,
    arguments=(), workers=None, suites=None, execution_mode=None, structural_files=None)
make_plan(root, *, suites=None, structural_files=None)
```

- `workers` is the number of concurrent scoped sandboxes, an integer from 1 to 32. `None`
  uses `[tool.pytest_telescope.execution] workers`, default 1.
- `execution_mode` is `"case"` or `"batch"`; `None` uses the configured mode, default
  `"case"`.
- `timeout` bounds each sandboxed session (scoped collection, case or batch), from 0.1 to
  3,600 seconds. Ordinary suite sessions have no pytest-telescope deadline; the job that
  runs pytest bounds them.
- `arguments` forwards supported pytest options and selectors to the suite sessions.
  `suites` selects configured suite names; `None` selects all suites.
- `structural_files` narrows suite file membership before parsing and importing modules
  ([structural narrowing](scoped-execution.md#invocation-only-structural-file-narrowing)).
- `force=True` executes scoped cases again even when a reusable success exists.
  `no_cache=True` bypasses result reading and recording. `explain=True` includes reuse and
  execution reasons in the result.
- `cache_dir` selects the cache directory; `None` uses `.pytest-telescope/cache` under
  `root`.
- `run` raises `DeclarationError` for invalid declarations and `UnsupportedCapability`
  (a `DeclarationError` subclass with a `capability` attribute) for refusals.
  `error_result(error)` turns either, or an `InfrastructureError`, into the refusal
  envelope.

## Command line

```sh
python -m pytest_telescope plan
python -m pytest_telescope run [--workers N] [--execution-mode case|batch] [--suite NAME]...
    [--force] [--no-cache] [--explain] [--cache-dir PATH] [--timeout SECONDS] [-- PYTEST-ARGS]
pytest --telescope [PYTEST-ARGS]
pytest --telescope-plan
```

Exit statuses: 0 passed, 1 test failure, 3 infrastructure failure,
4 declaration error or unsupported capability,
5 no selected cases.

## Configuration

```toml
[tool.pytest_telescope.execution]
mode = "case"   # or "batch"
workers = 4     # concurrent scoped sandboxes, 1-32, default 1
```

The [marker and project reference](specification.md#project-configuration) defines
`test_roots`, `env`, `defaults` and `bundles`. The
[metadata and suite reference](scoped-execution.md) defines `distributions` and `suites`.
A suite's `capabilities` list accepts `python` and `project:readonly`;
`network:none` and `scratch:noexec` refuse with that capability code.

## Host requirements

Ordinary-only projects need Linux and Python 3.11 or newer. Projects with scoped cases
also need the `bwrap` executable from Bubblewrap on `PATH`, and a kernel and security
policy that let it create user, mount and PID namespaces without privileges. Some
distributions restrict unprivileged user namespaces; for example, Ubuntu 24.04 sets
`kernel.apparmor_restrict_unprivileged_userns=1`, and `bwrap` then works only where the
administrator's AppArmor policy grants it user namespaces. pytest-telescope never changes
that policy. Check a host with `pytest-telescope run` on one scoped test: a
`sandbox:namespaces` refusal reports Bubblewrap's own error. The interpreter environment
must not contain the project directory, and the project must not live inside `/usr` or
`/etc`. Because the sandbox also shows the project at its own host path, the project
directory must not be `/`, `/tmp` or `/dev/shm`, and must not lie inside `/metadata`,
`/telescope`, `/proc`, `/dev` (other than `/dev/shm`) or a subdirectory of `/project`.
The project may be `/project` itself.

## Capability codes

| Code | Meaning |
| --- | --- |
| `sandbox:bubblewrap` | No `bwrap` executable on `PATH`. |
| `sandbox:namespaces` | `bwrap` could not create its namespaces; the message includes its error. |
| `sandbox:environment` | The interpreter could not start in the view, the environment layout overlaps the project or the sandbox's own mount points, or the project directory cannot also be shown at its host path. |
| `network:none`, `scratch:noexec` | A suite requested an isolation the sandbox does not provide. |
| `pytest:distributed-scheduling` | Nested or external distributed scheduling was requested; telescope owns scoped workers. |
| `pytest:configuration` | Unsupported pytest configuration format. |
| `execution:batch-size` | A batch exceeds 4,096 selected members. |
| `metadata:pep621-static-v1` | Local distribution metadata cannot be translated from the supported static PEP 621 fields. |
| `project-metadata:runtime-conflict` | Declared local metadata conflicts with a distribution in the installed environment. |
| `project:links` | A project input contains an unsupported directory symlink. |

## Result schema 3

Run and refusal results use schema 3. The [result reference](result-schema.md) defines
all fields and accounting rules.

- `execution` reports the execution mode, profile and configured `workers`.
- `sandbox` reports `{"backend": "bubblewrap",
  "policy": "bubblewrap-view-v2", "required": bool, "identity": digest-or-null}`.
  `identity` is null until the interpreter probe and Bubblewrap preflight succeed.
- Every unit and group has `route`, `"ordinary"` or `"scoped"`. `route_counts` gives the
  same seven count keys as `counts` for each route.
- Groups list `files`; an ordinary group is one suite session
  with profile `ordinary-v1`, never cache eligible.
- `collection.suites` has one row per suite and route, each with `route` and `files`.
- Timings report wall-clock durations. CPU and resource accounting belong to the caller.
