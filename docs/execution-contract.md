# Execution and reuse contract

## Status

Alpha 0.1.0a1 implements this contract on Linux with a local Docker daemon.
Qualification includes real sandbox integration tests and the [reproducible
benchmark](benchmark.md). The limits and reuse assumptions below are part of the
supported behavior.

## Declarations before imports

Run from the directory containing `pyproject.toml`. The dedicated commands read the
manifest without importing tests or conftest:

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan
python -m pytest_telescope run --explain -- -q
```

```toml
[tool.pytest.ini_options]
pythonpath = ["."]

[tool.pytest_telescope]
test_roots = ["tests"]
env = ["FEATURE_VARIANT"]

[tool.pytest_telescope.bundles.common]
inputs = ["tests/conftest.py", "app/__init__.py", "tests/helpers.py"]
absent = ["app/local_override.json"]

[tool.pytest_telescope.targets.parser]
tests = ["tests/test_parser.py"]
inputs = ["app/parser.py", "tests/data/*.json"]
bundles = ["common"]

[tool.pytest_telescope.targets.clock]
tests = ["tests/test_clock.py"]
inputs = ["app/clock.py"]
bundles = ["common"]
cache = false
```

`targets` is nonempty. Each target has a nonempty `tests` list and optional `inputs`,
`bundles`, `absent`, and `cache` fields. `cache` defaults to true. Bundles have optional
`inputs`, `bundles`, and `absent` lists and can refer to other bundles. Unknown fields,
unresolved references, cycles, malformed types, and paths escaping the project are
errors. Paths are relative to the project root.

Keep pytest configuration in `[tool.pytest.ini_options]` in this same manifest. Other
pytest configuration files (`pytest.ini`, `.pytest.ini`, `tox.ini`, and `setup.cfg`) are
rejected rather than silently introducing another configuration source. Test inventory
honors its `python_files` patterns. Directory symlinks are unsupported. A leaf symlink
must use relative target text and remain inside the declared project namespace. Absolute
target text is rejected even when it points inside the source project, because copying
it would not preserve the worker's project namespace.

`tests` and `inputs` accept existing explicit paths, directories, and globs. Hidden
directories can be selected explicitly in glob components, for example
`data/.assets/*.json`; do not rely on a broad discovery glob to include them.
Declarations expand to a bounded concrete snapshot. File content, frozen modes,
symlinks, and glob membership are part of its identity. Source timestamps and read/write
permission bits are normalized away; source execute bits remain visible and participate
in invalidation. A declared missing input is an error. Use `absent` for a path whose
required state is nonexistence; that assertion is checked and participates in reuse. An
undeclared path being inaccessible is not an assertion that the path is absent in the
source project.

A source file is limited to 64 MiB and a target snapshot to 256 MiB. Discovery visits at
most 20,000 entries, and inventory and expanded declarations are each limited to 4,096
paths. Keep roots narrow enough to fit these bounds. A test file belongs to exactly one
target. A leaf symlink's destination must also be declared as an input or an explicitly
absent path.

The declaration file itself is an input to every target. Include test source, conftest
files, helpers, fixture resources, and application code. No import tracing automatically
expands access. Shared bundles let one dependency invalidate all of its consumers
without duplicating declarations.

`test_roots` defaults to `["."]`. Before every execution or reuse decision, a static
inventory checks the configured roots for test files. Every discovered test file must
belong to the declared targets; a newly added undeclared test is an error, including on
an otherwise warm cache. Configure roots to cover the whole test suite. Static file
inventory does not discover arbitrary custom collection schemes that hide tests outside
those roots.

Only names in `env` are forwarded from the host as explicit user environment inputs, and
their presence and values affect reuse. Controlled Python/pytest variables and all
`TELESCOPE_*` protocol variables cannot be forwarded. Do not use environment forwarding
as a secret-distribution mechanism. Evidence and diagnostics must not print variable
values.

## Execution and fixture grouping

One manifest target is one execution and cache unit, run in a fresh pytest session. All
selected tests in that target share normal pytest fixtures and ordering within that
session. Session fixtures are recreated for each target; state and ordering do not cross
target boundaries. Put tests that require one shared session into the same target.
Larger targets preserve more sharing but rerun more work when any declared input
changes.

Isolation starts before collection and covers imports, fixture setup, test bodies, and
teardown, including native code and descendant processes. Project inputs are copied into
the exact frozen snapshot that is hashed and executed. The complete host checkout and
host virtual environment are never mounted into the worker. Snapshot files are
normalized to read-only mode `0444` plus the source's execute bits; directories use
`0755`. Access and modification times are normalized to zero. Cache identity uses these
actual frozen modes and normalized timestamps, not the source's read/write permission
bits or modification times. Source metadata is still checked to detect concurrent writes
during freezing. Inode, device, change time, and ownership are not portable
deterministic inputs; tests depending on them must be non-cacheable. The invoking
UID/GID is included in reuse identity. The snapshot and container root filesystem are
read-only. Workers run as a nonroot user with all capabilities dropped,
no-new-privileges, no network, and private writable scratch. Process count, memory, CPU,
wall time, scratch, output, and retry limits are enforced. Unintended inherited
descriptors are not passed through to workers. Cleanup stops the worker and its
descendants.

The first backend is Linux Docker using a local Unix-socket endpoint. Remote TCP Docker
endpoints are unsupported because worker snapshots are local bind mounts. The official
base is
`python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`.
Runtime preparation builds a clean image with pinned wheel-only Python dependencies. The
default Python dependency set is pytest 9.1.1, iniconfig 2.3.0, packaging 25.0, pluggy
1.6.0, and Pygments 2.19.2. Plugin auto-loading is disabled; explicit plugins must be
installed in the runtime and requested explicitly. Additional requirements must be exact
`name==version` wheel requirements covering the complete transitive dependency closure.
Installation uses `--no-deps`, then `pip check` rejects an incomplete or incompatible
closure. Editable installs, local application packages, and a host checkout are not
runtime dependencies. Python, installed packages, shared libraries, and executables in
the built image are explicitly part of the runtime boundary. The runtime image
filesystem remains available: the input manifest restricts project files, rather than
hiding Python's standard library or the image's system files. Docker also provides its
ordinary container `/proc` and `/dev` views. Changing live process/kernel metadata is
not a deterministic reuse input. Runtime preparation may access the package index; tests
cannot access a network.

Runtime requirements are limited to 64 KiB and the worker program to 128 KiB. A
completed runtime image must be at most 2 GiB. At most four project-owned runtime images
are retained, and image ownership is tied to the project's location. Prepare a new
runtime after copying or moving a project rather than copying its runtime metadata.
Docker's daemon-wide build cache is shared infrastructure: pytest-telescope does not
prune it. The Docker administrator must configure its storage quotas and garbage
collection; the image retention bound is not a bound on the daemon's shared build-cache
storage.

The initial worker policy gives each target one CPU, 512 MiB memory with no added swap
allowance, 128 processes, 256 open descriptors, 64 MiB of private `/tmp`, and at most 1
MiB of captured worker output. Limits are part of the backend policy; exceeding them
produces failure, not a broader execution boundary.

A container is an OS-enforced filesystem boundary sharing the host kernel. This contract
does not claim VM isolation or protection against kernel vulnerabilities. Workers
execute an exact immutable image ID. Reuse identity is derived from the image's
immutable root-filesystem layer hashes and execution configuration, excluding only the
controller's project-ownership label and image creation provenance. The worker
source/recipe, platform, kernel, Docker daemon and socket identity, security settings,
configured runtimes, and backend policy also participate. Raw image IDs and project
ownership remain available for execution and safe cleanup; ownership-only image
differences do not invalidate reuse.

Identical frozen inputs in different checkouts can share `--cache-dir` on the same
Docker daemon, despite distinct checkout modification times and independently owned
runtime images. Each checkout must prepare its own runtime. Cross-daemon or
cross-machine reuse is not promised. Rebuild the runtime after changing its
dependencies. If Docker cannot establish the required restrictions, report
infrastructure failure without running an unrestricted substitute.

## Reuse identity and outcomes

A cache key includes the effective declarations and exact frozen files; file
content/frozen modes/symlinks and expected absence; glob membership; test source,
conftest, helpers and manifest; runtime identity and backend policy; explicit
environment inputs; full pytest arguments and selectors, including `-m` and `-k`; runner
version and implementation digest; and target identity. A rename, addition, deletion,
symlink or mode change can invalidate a consumer even if another file's content stays
the same. Changing only source timestamps or read/write bits does not change the
normalized snapshot and therefore does not invalidate reuse. An unrelated file outside
all declarations does not invalidate a target, unless it changes the static test
inventory.

Only a complete success after teardown, clean worker exit, a validated bounded result,
and complete invocation cleanup can be recorded. Success is persisted under the stable
cache lock after cleanup finishes. A cleanup failure stops the remaining targets and
cannot publish a success followed by a duplicate error for the same target. Missing,
malformed, incompatible, interrupted, and failed records cannot be hits. Atomic records
and a stable shared cache lock protect concurrent invocations; no invocation may accept
a partially written result. The first backend serializes access through that lock,
including worker execution, to coordinate reuse and bounded garbage collection. It
retains at most 1,024 successful records and 64 MiB of owned records, removing oldest
records when necessary. Owned orphan record temporaries are removed under the same lock;
unrelated files are not cleanup candidates.

The controller distinguishes executed success, reused success, test failure,
infrastructure failure, and unselected targets, and reports actual test case counts.
JSON `counts` contains target outcomes; `cases` contains collected, passed, failed, and
skipped totals; `units` contains each target's outcome, reason, case counts, and timing.
The `cases` totals account for both executed and reused cases;
`case_outcomes.fresh_passed`, `cached_passed`, `fresh_skipped`, and `cached_skipped`
report the distinction directly. Each unit also reports its outcome. Exit statuses are 0
for success, 1 for test failure, 3 for infrastructure failure, 4 for invalid
declarations or arguments, and 5 when no cases are selected anywhere. A target with no
matching cases is `not-selected` and does not fail an otherwise passing selected run.
Reused cases are previous results, never newly executed tests. The target is the reuse
granularity, even when arguments select fewer cases from it.

Use declared test-file paths with optional `::node` selectors, `-k EXPRESSION`, or `-m
EXPRESSION` to select cases. The accepted pytest options are:

- With a value: `-k`, `-m`, `-o`, `--override-ini`, `-p`, `--maxfail`, `--tb`,
  `--capture`, and `--color`. Long options also accept `--option=value`.
- Switches: `-x`/`--exitfirst`, `-q`/`--quiet`, `-v`/`--verbose`, `-s`,
  `--disable-warnings`, `--strict-markers`, `--strict-config`, `--runxfail`,
  `--showlocals`, `--no-header`, and `--no-summary`. Repeated short verbosity
  switches such as `-vv` are accepted.

Unknown flags and directory selectors are declaration errors. Overrides to
`python_files`, `testpaths`, and `addopts` belong in the manifest instead of
command-line overrides, so static inventory and execution use one configuration source.
Pytest display options affect its worker session; the controller emits its structured
summary and bounded failure evidence, not arbitrary pytest output.

- `run --force`: execute despite an eligible successful record.
- `run --no-cache`: execute without reading or writing successful result records.
- `run --explain`: explain the target's execution or reuse decision.
- `run --cache-dir PATH`: select the result-record directory.
- `run --timeout SECONDS`: bound worker wall time; default 60 seconds, allowed
  range 0.1 through 3,600 seconds.
- Target `cache = false`: always execute this target and never record a reusable
  success, regardless of other targets' cache settings.

Filesystem inputs do not prove universal determinism. Tests that depend on clocks,
uncontrolled randomness, native global state, changing external services, or undeclared
environmental assumptions must be made reproducible or marked non-cacheable. Network
services are unavailable in this backend. Cached success is meaningful only under the
documented input and runtime assumptions.

## Failures and troubleshooting

A declared file that is missing or a declared-absent path that exists fails validation.
Fix the declaration or the source state, then rerun. Do not add a broad checkout mount
to make an import succeed. For a collection or import failure, inspect the reported
project file and its imports, then add the actual missing dependency as a narrow file,
directory, or bundle. For a `FileNotFoundError`, inspect the reported file/line and its
resource dependencies. Verify shared conftest and package `__init__.py` files as well as
the obvious application module.

An undeclared/new test-file error requires updating target coverage or correcting
`test_roots`. It cannot be resolved by accepting an old cache record. A changed selector
has a different identity and cannot reuse the unselected run's result.

A failed test remains a failed execution and never becomes a reusable success. Backend
identity preflight gets at most two attempts. If it remains unavailable, every validated
target is reported as an infrastructure error rather than being omitted. A target gets
at most two execution attempts for infrastructure failures. Cache lock acquisition is
bounded by twice the configured worker timeout plus 30 seconds. Startup, deadline,
output, storage, invalid-result, or cleanup failure that exhausts recovery ends with a
distinct infrastructure outcome. Resolve Docker availability, capacity, runtime, or the
limit responsible before rerunning. Retrying forever and silently removing restrictions
are not recovery strategies. Force execution can diagnose a suspected record without
relying on it; removing damaged records should affect reuse only, never turn failing
work into a success.

Each unit can report up to 64 failure entries containing a project-relative file, line
number, phase (`collect`, `setup`, `call`, or `teardown`), exception type, and hashed
case identity. Collection failures use `CollectionError` and may have line zero. Runtime
locations are labeled `<runtime>`. Exception messages, source snippets, parameter
values, stdout/stderr contents, and environment values are not included in this
evidence. Invocation journals identify owned resources for recovery. There are at most
64 active invocation records per project; current worker cleanup has a 30-second budget,
and stale invocation recovery has a 60-second budget. Unfinished recovery is an
infrastructure failure rather than permission to start without cleanup. Cleanup removes
only resources owned by that invocation. The pytest adapter may load other host plugins
before routing to the controller; use `python -m pytest_telescope` to avoid host
pytest/plugin startup entirely.
