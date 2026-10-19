# Execution and reuse contract

## Status

Version 0.1.0a3 implements this per-case marker contract with Linux Docker
enforcement and lifecycle recovery, plus the [scoped execution extension](scoped-execution.md)
and versioned [result schema](result-schema.md). Version 0.1.0a2 introduced per-case markers;
0.1.0a1 used the earlier file/group target interface. Those releases remain immutable.

## User interface and scope

Every actual pytest item is one independent execution and successful-result cache unit. This includes unmarked items and every parametrized variant. There is no central per-case registry. A new test file or generated case enters the current inventory automatically; it cannot disappear behind an old result.

Use literal declarations beside tests:

```python
import pytest

pytestmark = pytest.mark.telescope(bundles=["application"])


@pytest.mark.telescope(inputs=["data/valid.json"])
def test_valid(): ...


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("a", marks=pytest.mark.telescope(inputs=["data/a.json"]), id="a"),
        pytest.param("b", marks=pytest.mark.telescope(inputs=["data/b.json"]), id="b"),
    ],
)
def test_variant(value): ...
```

Module/class/function/parameter declarations are additive. A literal `inputs` or `bundles` field establishes explicit file scope, including an empty list. The effective scope is the union of those inherited declarations, project defaults, containing test source, and configuration source. The broad implicit scope is replaced, not added. A policy-only `cache=False` marker or absence assertion does not establish an empty allowlist. Any inherited `cache=False` disables successful-result reuse for that item.

Without an explicit file-scope marker, a case sees the whole project-file snapshot, including hidden and Git-ignored project files. Every visible file participates in its key, so any visible project-file change reruns broad cases. This retains the sandbox and clean prepared runtime; it does not expose the host filesystem or host environment.

The excluded namespaces are version-control metadata (`.git`, `.hg`, `.svn`), telescope state (`.pytest-telescope` and an explicitly selected in-project cache directory), pytest/bytecode caches (`.pytest_cache`, `__pycache__`), and host Python environments (`.venv`, `venv`, or a directory with a validated `pyvenv.cfg`). Excluded content is never mounted or silently exposed without a key. Other directories such as build, dist and node_modules are project content and count against snapshot limits. Directory symlinks, special files and unsupported paths fail boundedly rather than being followed or omitted. Narrow declarations into excluded namespaces remain errors.

Pyproject contains `[tool.pytest_telescope]` test roots/environment settings, a `[tool.pytest_telescope.defaults]` table for shared inputs/bundles/absence/cache policy, and reusable `[tool.pytest_telescope.bundles.NAME]` tables. Defaults add common support to explicit scopes; they do not silently narrow otherwise unmarked cases. Conftest, application modules, package initializers, helpers and collection resources must be visible in the effective scope. Broad scope includes them automatically; narrow scope declares them. Existing central `targets` is rejected with a migration message rather than silently treated as per-case configuration.

No new shared-session grouping API is introduced: each case starts a fresh pytest session. Module/class/session fixtures are recreated per case. Tests requiring cross-case mutable state must be rewritten; this version does not promise ordinary pytest's shared-session behavior. The supported filesystem is read-only, and scratch is private to each case.

## Static discovery and real pytest agreement

The controller parses bounded test source syntax without importing or evaluating project Python. Supported spellings use `pytest.mark.telescope(...)` directly in function/class decorators, module/class `pytestmark`, or `pytest.param(..., marks=...)` inside literal parametrization declarations. Values are literal string lists and booleans; bundles provide reusable declarations. Arbitrary expressions, computed kwargs, declaration factory functions, aliases and generated telescope markers are unsupported. Ordinary parametrization values and pytest-generated item identities still come from real pytest collection, not an AST approximation.

A pre-import worker guard validates each supported marker factory call against its exact frozen source position and literal arguments. Collected telescope marks must have registered origins and unchanged values, compared with an independent immutable canonical encoding of the static declaration. Mutable lists passed to pytest are never the validation baseline. Code-position tables are memoized per code object so repeated markers do not repeatedly expand the entire table. Hidden dynamic marker construction that cannot be diagnosed statically fails in the already-isolated collection worker; it never expands the snapshot. This is strict declaration validation, while Docker supplies enforcement. Behavioral qualification covers the supported pinned runtime's call-position behavior and pytest 8/9 marker inheritance in behavioral controls; private pytest internals are not assumed stable.

Discovery collects all current test files inside a worker with the explicit union of every potential case scope. When an unmarked item is possible, discovery includes the broad project snapshot from the outset. Function/class/parameter-only declarations cannot prove complete dynamic coverage; a validated inherited module-level file scope can establish a narrow discovery union. Real collected provenance must confirm that guarantee, or collection fails without a broader retry. Discovery can therefore see more files than any one narrow case. This is a separate, documented collection boundary; a case body never runs with the discovery union. Real pytest enumerates dynamic and parametrized items and maps inherited literal origins to effective declarations. Hooks that add, remove or change items after the validated collection checkpoint are rejected. Duplicate/invalid node identities are errors.

Per-case execution begins a new sandbox with only that case's effective scope, before conftest/test imports. It collects its containing file, verifies the actual item/declaration inventory against the discovery record, and executes exactly the selected item. A narrow execution missing a shared collection dependency fails with bounded collection evidence; it does not borrow another case's inputs. Common import/parametrization dependencies belong in shared declarations. The fresh worker's collection must agree even when a data-dependent item generator produces different nodes under a narrow scope.

## Inventory, selection and reuse

Inventory and execution have distinct strict result schemas. A successful collection reports collected nodes and declaration assignments, never executed passes. It becomes reusable only after clean worker exit, complete teardown of the collection process, validated bounded records and invocation cleanup.

The inventory key covers the exact frozen discovery union, current test-file membership, static declaration registry, pytest configuration, relevant environment, full effective arguments/selectors, runtime/image content and daemon identity, runner implementation/version, and backend policy. Every invocation re-enumerates and freezes current project inputs before accepting an inventory hit. File addition/deletion, resource changes or changed dynamic-collection inputs invalidate it. `--force` and `--no-cache` refresh inventory. Any statically present cache=False policy conservatively disables inventory reuse; unrelated cacheable case results may still be reused after fresh collection.

Result keys cover each case's actual node identity, effective literal declarations, exact executed snapshot, validated containing-file inventory/declaration digest, full options/selectors and all existing runtime/environment/runner/backend identities. They do not include an unrelated case's resource bytes merely because discovery saw them. Thus changing one narrow resource refreshes discovery but preserves unrelated case result hits. Broad case keys include every visible project file. Failures, interrupted/invalid records and incomplete cleanup never become hits.

Full current inventory is accounted for before selectors: selected cases are executed/reused/failed; other cases are explicitly unselected. `-k`, `-m` and node selectors use actual pytest semantics. Summaries report collection separately, actual case counts, fresh versus cached pass/skip outcomes, and no invented file-target granularity. Cache hit acceptance cannot hide new files/cases. Plan remains a nonexecuting static scope preview and explicitly does not claim real collected-case enumeration.

## Bounds and performance

Bounded defaults: 100,000 actual cases; 100,000 visible files; 200,000 visited entries; 64 MiB per source file; 2 GiB total frozen project bytes; 4,096 bytes per node ID/path; 64 path components; 64 MiB inventory/result-summary encoding; 1 MiB individual execution response. Static parsing accepts at most 4 MiB per Python source file and 64 MiB of Python source in total, 200,000 AST nodes per file and 2,000,000 total, depth 128, and 100,000 literal marker calls. Bounded traversal counts nodes without first constructing an unbounded list; parser-depth and allocation failures become declaration errors. Reusable bundle count/depth limits remain separate from case cardinality.

Retain up to 100,000 successful case records within a 256 MiB total cache budget, plus inventory records within a 64 MiB aggregate inventory budget. The inventory record-count bound is `max(8, selected_suite_count)`, at most 64 records. Enforce both count and byte limits under the existing stable lock; scan/GC once per invocation and incrementally account new records. New records remain ineligible pending files until the complete frozen-pool invocation has cleaned successfully. A cleanup failure discards those pending records and preserves previously valid hits. Pending storage has separate bounded budgets of 256 MiB for cases and 64 MiB for inventory; abandoned pending files are recovered under the same stable lock. Eviction changes performance only. Bounded output and record parsing apply during I/O, not after unbounded capture. Limits produce explicit errors and never silently truncate inventory.

Freeze each distinct file once per invocation. Derive case keys from immutable frozen-file records and memoized scope identities. Materialize only missed cases, using immutable files from the frozen pool without exposing its broader parent directory. Broad scopes can reuse a complete immutable snapshot; narrow snapshots contain only their own selected paths. Do not copy/hash the whole project once per case. The default budget runs one worker; explicit resource envelopes permit bounded parallel fresh workers as specified in the [scoped execution extension](scoped-execution.md). Each miss starts one isolated pytest session per case; repeated collection of a large parametrized module is a real cost, not hidden by file-group benchmark claims. Warm inventory/result hits start no worker containers.

## Project configuration

Run from the directory containing `pyproject.toml`. The dedicated CLI does not import
project Python on the host:

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

[tool.pytest_telescope.defaults]
inputs = ["tests/conftest.py"]

[tool.pytest_telescope.bundles.application]
inputs = ["app/__init__.py", "app/parser.py"]
absent = ["app/local_override.json"]
```

Paths are project-relative. Inputs accept explicit files, directories and globs.
Missing declared inputs, unknown fields/bundles, cyclic bundle references, malformed
values and paths escaping the project are errors. Expected-absent paths must be
absent in the source project and participate in identity. Relative leaf symlinks
require their destination in the effective scope; absolute links and directory
symlinks are unsupported. Explicit hidden glob components are supported.

The default suite keeps pytest configuration in this pyproject's `[tool.pytest.ini_options]`.
The 0.1.0a3 [independent suite declarations](scoped-execution.md) also support explicit
pytest.ini, .pytest.ini, tox.ini and setup.cfg files. Discovery honors configured `python_files`
and `test_roots` (default `["."]`). Bounds fail explicitly; discovery never silently
truncates matching files. Only named `env` variables are forwarded and keyed, including
presence. Controlled Python/pytest variables and `TELESCOPE_*` protocol names are
reserved. Diagnostics never print environment values.

## Runtime and filesystem enforcement

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

The initial worker policy gives each case one CPU, 512 MiB memory with no added swap
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

## Outcome accounting

The 0.1.0a3 [result schema](result-schema.md) adds versioned complete/incomplete
inventory accounting, opaque case identities and explicit unsupported-capability refusals.


The controller reports current collected cases, selection, independent execution units,
and collection work separately. Each selected case has one outcome: executed success,
reused success, test failure, or infrastructure failure. Other current cases are
unselected. Fresh/cached passed and skipped counts remain distinct. A cached case is
not described as a newly executed test. Exit statuses remain 0 for success, 1 for test
failure, 3 for infrastructure failure, 4 for invalid declarations/arguments, and 5
when no cases are selected.

Use discovered test-file paths with optional `::node` selectors, `-k EXPRESSION`, or `-m
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
- `run --explain`: explain the case's execution or reuse decision.
- `run --cache-dir PATH`: select the result-record directory.
- `run --timeout SECONDS`: bound worker wall time; default 60 seconds, allowed
  range 0.1 through 3,600 seconds.
- Marker `cache=False`: always execute the case and never record a reusable
  success, regardless of other cases' cache settings.

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

New test files and cases are included in the refreshed real inventory. A collection
mismatch or unsupported declaration cannot be resolved by accepting an old record.
A changed selector has a different identity and cannot reuse the unselected run's result.

A failed test remains a failed execution and never becomes a reusable success. Backend
identity preflight gets at most two attempts. If it remains unavailable, every validated
case is reported as an infrastructure error rather than being omitted. A case gets
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
