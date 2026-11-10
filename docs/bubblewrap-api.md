# API, CLI and configuration

> The schema 4 and per-scope environment interfaces described here are an unreleased
> development contract. Published version 0.1.0a6 does not provide them.

This reference lists the interfaces of the [ordinary and Bubblewrap execution
contract](bubblewrap.md). The [result reference](result-schema.md) defines every schema 4
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
  Late typed refusals retain bounded immutable actual observations in their
  `accounting` property when available; see [retained observations](result-schema.md#retained-observations-after-a-late-refusal).
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

## Result schema 4

Run and refusal results use schema 4. The [result reference](result-schema.md) defines
all fields and accounting rules.

- `execution` reports the execution mode, profile and configured `workers`.
- `sandbox` reports `{"backend": "bubblewrap",
  "policy": "bubblewrap-view-v2", "required": bool, "identity": digest-or-null}`.
  `identity` is null until the interpreter probe and Bubblewrap preflight succeed.
- Every unit and group has `route`, `"ordinary"` or `"scoped"`. `route_counts` gives the
  same seven count keys as `counts` for each route.
- Groups list `files`; an ordinary group is one suite session
  with profile `ordinary-v2`, never cache eligible.
- `collection.suites` has one row per suite and route, each with `route` and `files`.
- Timings report wall-clock durations. CPU and resource accounting belong to the caller.

## Required fixture guard API (unreleased)

This section specifies a required development interface; it is not a claim that the
current published package implements or qualifies it. The Python signature extends
`run` with keyword-only `fixture_guard=None, condition_checks=()`. `fixture_guard` is
exactly `None` or `"available-v1"`; the guard requires batch mode and refuses ordinary
or mixed structural routes. The CLI opts in with `run --fixture-guard available-v1`;
it accepts no condition expectation values. Existing calls and CLI behavior with the
guard omitted retain schema 4 and cache envelope 6.

With the guard, `condition_checks` is an actual tuple or list of at most 4,096 exact
dictionaries, in output order. Each has exactly `case_id`, `name`, and `op`; `equals`
adds exactly `value`. `case_id` is a lowercase 64-character hexadecimal case ID, `name`
is a valid non-reserved declared environment name, and `op` is `absent`, `present`, or
`equals`. The `equals` value is an exact UTF-8-encodable string of at most 4,096 bytes;
the total value bytes are at most 1 MiB. Duplicate `(case_id, name, op)` rows, extra
keys, coercion and subclasses refuse as declaration errors. Conditions must be empty
without the guard. Each case must be actually selected exactly once and each name must
belong to that case's effective inherited environment declaration.

Evaluate conditions from the environment captured once for the invocation, before any
selected body or cache acceptance. A mismatch raises
`UnsupportedCapability("execution:condition-mismatch", ...)`. Values are transient
controller memory: do not place raw values or unsalted hashes of individual values in
requests, cache, results, logs, exceptions or representations. Group identities and cache
keys are deterministic fingerprints of the wider execution context, including captured
environment values. Someone who knows enough other key inputs may use those fingerprints
to verify guesses of low-entropy values offline; they are not password-hardening or an
authentication mechanism. A match proves only the bound condition for that
captured case and group; it does not establish a skip, pass or teardown. Inspect the
actual reports, group lifecycle and final run status too.

`run` retains its typed exceptions. Callers may use
`error_result(error, fixture_guard="available-v1")` to request the schema 5 refusal
shape; omitting the option retains the schema 4 converter exactly. An invalid converter
option raises `DeclarationError`. The converter reports guard state `unavailable`: it
has no trusted admission or attempt evidence and does not assert no body ran. Expected
late failures are returned by the invocation as truthful run results with observed
attempts, rather than converted into a pre-body refusal. The CLI supplies guard context
to its existing error handlers only after successful parsing; parser failures keep schema 4.

Guard capability refusals use `execution:fixture-profile`, `execution:fixture-version`,
`execution:fixture-metadata`, `execution:fixture-sharing`, `execution:fixture-limit`,
and `execution:condition-mismatch`. Unsupported fixtures and uncertain sharing refuse
rather than changing the view. Dependency errors and malformed worker records follow
bounded infrastructure retry and incident handling. See the
[fixture guard contract](batching.md#required-fixture-guard-unreleased) for scope,
version and lifecycle limits.
