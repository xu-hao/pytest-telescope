# Product specification

## Status and shared contract

Version 0.1.0a1 provides static manifest planning only. It does not execute isolated
workers or reuse results. `--telescope` refuses with usage status 4. The future
requirements below are acceptance criteria, not claims about current enforcement.

One effective declaration must drive both filesystem access and invalidation of a
previous successful result. OS enforcement is required: Python monkeypatching or
tracing alone cannot constrain native code or subprocesses. Start on Linux with
one existing sandbox mechanism, not a new sandbox implementation.

## Current static planner

Run `python -m pytest --telescope-plan` from the directory containing
`pyproject.toml`. The command reads declarations before initial conftest and test
imports, prints a deterministic JSON plan containing only relative paths, and exits
with status 0. Invalid declarations produce usage status 4. A plan never executes
tests, enforces permissions, establishes complete test inventory, or promises reuse.
Other installed plugins participate in ordinary pytest startup; planning is not a
sandbox. Use a clean environment when inspecting unfamiliar projects.

```toml
[tool.pytest_telescope.bundles.common]
inputs = ["tests/conftest.py", "src/example/__init__.py"]

[tool.pytest_telescope.targets.parser]
tests = ["tests/test_parser.py"]
inputs = ["src/example/parser.py", "tests/data/input.json"]
bundles = ["common"]
```

The `bundles` table is optional. `targets` must be a nonempty table. Each target
requires a nonempty `tests` list; `inputs` and `bundles` default to empty lists.
Bundles accept `inputs` and nested `bundles` lists. Unknown fields, unknown bundle
references, cycles (including unused bundles), malformed values, missing files,
absolute paths, traversal, escaping symlinks, directories, and glob expressions
are rejected. Files must already exist within the project root. Internal symlinks
are permitted, but their meaning is only validated at plan time.

Effective inputs are the sorted union of the target's tests, direct and transitive
bundle inputs, and `pyproject.toml`. Targets and paths are sorted and deduplicated.
The planner caps manifests and rendered plans at 1 MiB each, targets plus bundles
at 256, declaration list entries at 4096, bundle nesting at 32 levels, names at
128 characters, and individual paths at 512 characters. Exceeding a limit is a
usage error, before imports.

All conftest/helper/application dependencies must currently be listed explicitly;
the planner does not discover or import them. It does not hash or freeze files,
check undeclared/new tests, or guarantee absence of races after validation.

The JSON shape is:

```json
{"mode": "plan-only", "targets": [{"name": "parser", "tests": ["tests/test_parser.py"], "inputs": ["pyproject.toml", "src/example/__init__.py", "src/example/parser.py", "tests/conftest.py", "tests/data/input.json", "tests/test_parser.py"]}]}
```

## Future declaration and execution model

Declarations must be available before importing tests or conftest. Reusable bundles
avoid repeated helper/fixture/application dependencies. An execution unit may be
one test or an explicit group preserving shared fixture and ordering semantics.
Report actual execution and caching granularity, and document grouping precisely.
The static planner's target is not yet an enforced execution unit.

The completed manifest model must distinguish an inaccessible undeclared path from
a declared path expected to be absent. A violation must not silently add dependencies
or broaden access. Include test source, conftest, fixtures, helpers, and declaration
files in invalidation. Discover new or undeclared tests so they cannot disappear
from a run. Add explicit always-run/non-cacheable support for unsupported reuse.

## Future sandbox boundary

Each execution unit starts in a fresh worker. Isolation precedes collection,
imports, fixture setup, execution, and teardown. Only declared project inputs and
explicitly identified runtime files are visible. Inputs are immutable. Writable
temporary/output directories are private to the unit.

Restrictions cover native code and every descendant process. Block network by
default and close unintended inherited descriptors. Symlinks and traversal cannot
escape the declared namespace. Never expose the whole checkout or host filesystem
as a convenience. Identify Python, installed packages, shared libraries, plugins,
and executable tools explicitly. Editable application installs or stale runtime
images cannot smuggle application source across the boundary.

A missing sandbox capability or failed startup is an infrastructure error. There
is no unrestricted fallback described as isolated execution. Supported-host tests
must execute the real backend; skipped enforcement tests establish no guarantee.

## Future selection and result records

Successful records are keyed by the effective declaration, exact executed input
snapshot, runtime identity, relevant environment, pytest arguments/configuration,
runner version, and test/unit identity. Hashing a different snapshot from the one
executed is invalid. Content changes, additions, deletions, renames, symlink changes,
relevant metadata, and glob membership invalidate affected units. Shared dependencies
invalidate every consumer; new tests and configuration/runtime changes are accounted for.

Reuse only complete successful records. Missing, corrupt, incompatible, interrupted,
or failed records cause execution instead of a false hit. Store atomically and
coordinate concurrent invocations so partial results cannot be accepted.

Provide opt-in affected execution, forced execution regardless of cache, and an
explanation for each execution/reuse decision. Executed, reused, failed, and
infrastructure-error outcomes are separate. Reuse is never reported as execution.

## Future limits and recovery

Enforced filesystem input boundaries do not prove arbitrary Python determinism.
Document environment, clocks, randomness, native state, and external-service
requirements. Tests outside the supported reuse contract must be explicitly
always-run/non-cacheable. Universal determinism and auditing every native library
are outside the product's scope.

Bound output capture, process lifetime, storage, descendant cleanup, and retries.
Each failing dependency has a retry path and a bounded terminal infrastructure
error that stops the affected run. Preserve useful failure evidence without
recording secrets, arbitrary file contents, or environment values. Cleanup owns
only that invocation's resources.

## Acceptance and qualification

Real sandbox worker tests must cover:

- Declared reads, undeclared access restrictions, and immutable inputs.
- Native file access and subprocesses under the same restrictions.
- Collection, imports, fixtures, execution, and teardown inside the boundary.
- Changed Python/JSON inputs rerunning affected units; unrelated changes reusing
  eligible successes; shared dependencies invalidating every consumer.
- Added/deleted files, globs, symlinks, traversal, new tests, and configuration/runtime changes.
- Failed, interrupted, corrupt, and concurrent recordings.
- Startup failure, deadlines, leaked descendants, bounded retries, and cleanup.
- Unchanged ordinary pytest behavior while telescope mode is inactive.

Ship installation/quickstart, declarations/bundles, platform/backend requirements,
exact isolation and reuse limitations, missing-input troubleshooting, fixture/grouping
semantics, and invalidation/recovery guidance.

Provide a representative synthetic example project and measure ordinary pytest,
cold sandbox execution, unchanged warm execution, a narrow code change, a resource
change, and a shared dependency change. Use an external clock and report real
elapsed/CPU time, setup overhead, and executed/reused counts. Do not manufacture
speedups with sleeps or claim benefits unsupported by measurements. No benchmark
has been run for this initial planner release.
