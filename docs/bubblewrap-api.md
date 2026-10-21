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
- `timeout` bounds each session the controller starts, from 0.1 to 3,600 seconds.
- `arguments`, `suites`, `structural_files`, `force`, `no_cache`, `explain` and `cache_dir`
  keep their 0.1.0a3 meanings ([structural narrowing](scoped-execution.md#invocation-only-structural-file-narrowing)).
- `run` raises `DeclarationError` for invalid declarations and `UnsupportedCapability`
  (a `DeclarationError` subclass with a `capability` attribute) for refusals.
  `error_result(error)` turns either, or an `InfrastructureError`, into the refusal
  envelope.

Removed from 0.1.0a3: the `budget` argument, `runtime.build_runtime`,
`runtime.inspect_runtime`, and the unreleased `runtime_profile`, `required_capabilities`,
`python_profile` and `source_roots` arguments. There is no runtime to prepare or inspect.

## Command line

```sh
python -m pytest_telescope plan
python -m pytest_telescope run [--workers N] [--execution-mode case|batch] [--suite NAME]...
    [--force] [--no-cache] [--explain] [--cache-dir PATH] [--timeout SECONDS] [-- PYTEST-ARGS]
pytest --telescope [PYTEST-ARGS]
pytest --telescope-plan
```

The `runtime` command and the `--budget-*`, `--runtime-profile`, `--require-capability`,
`--python-profile` and `--source-root` flags are removed. Exit statuses: 0 passed,
1 test failure, 3 infrastructure failure, 4 declaration error or unsupported capability,
5 no selected cases.

## Configuration

```toml
[tool.pytest_telescope.execution]
mode = "case"   # or "batch"
workers = 4     # concurrent scoped sandboxes, 1-32, default 1
```

Every other table keeps its 0.1.0a3 meaning: `test_roots`, `env`, `defaults`, `bundles`,
`distributions` and `suites`. A suite's `capabilities` list accepts `python` and
`project:readonly`; `network:none` and `scratch:noexec` are no longer provided and refuse
with that capability code.

These settings are rejected with a message naming the replacement:

| Removed setting | Replacement |
| --- | --- |
| `[tool.pytest_telescope.execution.worker]`, `[...execution.budget]` | Limit resources in the job that runs pytest; set `execution.workers` |
| `[tool.pytest_telescope.runtime]` | None: scoped tests use the invoking interpreter and environment |
| `[tool.pytest_telescope.python]` and suite `python` tables | Set `PYTHONPATH` or `sys.path` for child interpreters explicitly |

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
`/etc`.

## Capability codes

| Code | Meaning |
| --- | --- |
| `sandbox:bubblewrap` | No `bwrap` executable on `PATH`. |
| `sandbox:namespaces` | `bwrap` could not create its namespaces; the message includes its error. |
| `sandbox:environment` | The interpreter could not start in the view, or the environment layout overlaps the project or the sandbox's own mount points. |
| `network:none`, `scratch:noexec` | A suite requested an isolation the sandbox does not provide. |
| `pytest:distributed-scheduling`, `pytest:configuration`, `execution:batch-size`, `metadata:pep621-static-v1`, `project-metadata:runtime-conflict`, `project:links` | Unchanged from 0.1.0a3. |

## Result schema 3

Schema 3 replaces schema 2 for run and refusal results. Compared with schema 2:

- `execution` gains `workers`.
- `sandbox` replaces `runtime`, `python` and `resources`: `{"backend": "bubblewrap",
  "policy": "bubblewrap-view-v1", "required": bool, "identity": digest-or-null}`.
  `identity` is null until the interpreter probe and Bubblewrap preflight succeed.
- Every unit and group has `route`, `"ordinary"` or `"scoped"`. `route_counts` gives the
  same seven count keys as `counts` for each route.
- Groups list `files` instead of a single `file`; an ordinary group is one suite session
  with profile `ordinary-v1`, never cache eligible.
- `collection.suites` has one row per suite and route, each with `route` and `files`.
- Every `cpu_seconds` field and `worker_cpu_seconds` are removed. Wall-clock timings stay.

Cache records written by earlier versions are not eligible for reuse.
