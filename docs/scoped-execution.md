# Metadata, independent suites, and parallel workers

This document extends the [declaration contract](execution-contract.md) and the
[ordinary and Bubblewrap execution contract](bubblewrap.md) with local distribution
metadata, independent suites and parallel scoped workers.

## Static project metadata

Local distribution metadata is a frozen PROJECT input, distinct from the packages
installed in the invoking environment. A project opts into the versioned static translator:

```toml
[tool.pytest_telescope.distributions.core]
metadata = "packages/core/pyproject.toml"
format = "pep621-static-v1"
build_inputs = ["packages/core/build-config.txt"]

[tool.pytest_telescope.distributions.addon]
metadata = "packages/addon/pyproject.toml"
format = "pep621-static-v1"

[tool.pytest_telescope.bundles.core]
distributions = ["core"]
inputs = ["packages/core/core.py"]
```

```python
@pytest.mark.telescope(distributions=["addon"], inputs=["packages/addon/addon.py"])
def test_entry_point():
    from importlib.metadata import entry_points, version

    assert version("example-addon") == "1.0.0"
    assert next(iter(entry_points(group="example.plugins", name="addon"))).load() == "addon"
```

`distributions` is additive in markers, defaults, and bundles. Like `inputs` and
`bundles`, a literal marker field makes a case scoped, including an empty list. A scoped
case receives only its effective distribution names, their metadata source and declared
build inputs. Declaring metadata never implicitly grants the package's application
source. Ordinary cases receive no generated metadata.

Version 1 supports static `[project]` name, version, description, requires-python,
dependencies, optional-dependencies, scripts, gui-scripts and entry-points. All fields
are validated, names and versions normalized, and dependency strings validated using
packaging standards. Unsupported fields and any nonempty `dynamic` declaration are
explicit unsupported results; there is no partial silently guessed metadata. The
`[build-system]` declaration and complete source TOML bytes participate in identity.
Build inputs are explicit files/directories/globs, not executable hooks. No build
backend is imported, installed, or executed to create metadata. A missing project table,
missing static version, backend-path, setup-only metadata, or dynamic build output is
unsupported. Other `[tool]` configuration remains inert keyed data.

The authority for this translation is the [PyPA static project metadata
specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/):
static fields are project declarations. Generated `METADATA` and `entry_points.txt`
follow the [installed-project metadata
format](https://packaging.python.org/en/latest/specifications/recording-installed-packages/).
This is a deliberately limited metadata projection, not a wheel build or editable
installation. Console-script metadata is discoverable, but no launchers are generated.
Wheel-specific files and arbitrary backend-generated resources are outside this format.

Freeze source bytes before deriving generated files. The generated representation has a
versioned identity, files with mode `0444`, directories with mode `0755`, and epoch
access/modification times. The sandbox mount is read-only. This representation
participates in the exact case key and is mounted at `/metadata` only for that sandbox.
The worker adds this metadata-only directory to Python's search path before pytest
startup. All application imports still resolve to the declared snapshot: through
`/project` and explicit pytest configuration, or through an editable install of the
project, which points at the same snapshot at the project's host path. Child interpreters
do not see `/metadata` unless the test adds it to their path.

The effective metadata set is complete: omitted distributions are absent, including
negative lookups. Duplicate normalized names or conflicts with packages installed in the
invoking environment refuse rather than shadowing installed metadata. Changing a selected
metadata source, build input, entry point, generated representation, or relevant
application source invalidates its consumers. Unrelated narrow resources do not.
Missing/deleted required sources fail validation.

## Independent suites and structural membership

```toml
[tool.pytest_telescope.suites.core]
root = "tests/core"
config = "tests/core/pytest.ini"
test_roots = ["tests/core"]
include = ["tests/core/test_*.py"]
exclude = ["tests/core/test_external_service.py"]
args = ["-m", "unit"]
plugins = ["pytest_asyncio.plugin"]

[tool.pytest_telescope.suites.addon]
root = "tests/addon"
config = "tests/addon/pyproject.toml"
test_roots = ["tests/addon"]
plugins = []
```

Suite names identify separate pytest invocations. Each has its own ordinary session,
sandboxed collection, working directory, actual pytest root directory, explicit
configuration, options and plugin activation. `root` sets pytest's root directory.
Optional `cwd` sets the sessions' working directory independently and defaults to `root`; both are existing directories
relative to the outer project. For example, `root = "packages/core"` with `cwd = "."`
preserves an invocation from the workspace directory with a nested pytest root.
Configuration supports pytest.ini, .pytest.ini, pyproject.toml,
tox.ini and setup.cfg; unsupported configuration syntax refuses. Suite configuration
and applicable conftest files remain inputs. Scoped cases must declare conftest files
and common collection support; their test source and both project and suite configuration
are automatic. With no suites, existing root-suite behavior remains the default.
Explicit suites cannot be combined with top-level `test_roots`.

All configured paths, declarations, selectors, include/exclude patterns and summary files
remain relative to one outer project directory. `test_roots` accepts directories or
explicit Python test files. File patterns use project-relative glob semantics.
Membership is enumerated afresh before reuse: structural exclusions are applied before
source parsing or importing the excluded modules. These modules are not collected
inventory and cannot be selected with `-k`, `-m`, or node selectors. A file may occur in
multiple suites; the pair of suite name and canonical project-relative pytest node ID
is the execution identity. No centralized registry of individual cases is introduced.

Each suite collects its entire structural inventory before ordinary pytest selection.
Configured suite args and command-line args retain their order; pytest supplies `-k`
and `-m` semantics. Node selectors use project-relative paths; a suite without any
matching selected paths accounts its cases as unselected. Nonexistent selectors fail
across the complete configured inventory. A suite selector restricts the set of
invocations explicitly; the summary names those configured invocation boundaries.

The worker hashes a project-relative representation of pytest's root-relative node IDs,
retaining class/function/parameter suffixes without mutating pytest item node IDs.
Fixtures observing `request.node.nodeid` retain ordinary suite-local pytest semantics.
Collection and execution use identical normalization;
marker origin paths and containing-file inventory digests stay project-relative. The
suite identity, root, working directory, configuration bytes, arguments, plugins, structural membership,
collection inputs and sandbox identity all key scoped inventory reuse. Case keys cover
the same suite contract and their own actual scopes. Additions/removals refresh the
affected suite's inventory; unrelated suite resource changes do not invalidate scoped
case results.

### Invocation-only structural file narrowing

Python callers can pass `controller.run(root, structural_files={"core":
["tests/core/test_small.py"]})` to narrow configured structural membership for this
invocation before marker AST parsing or collection imports. This is separate
from `arguments=["tests/core/test_small.py"]`: ordinary selectors retain collection of
the entire configured structural inventory before selecting cases.

Keys must name selected configured suites. An unspecified selected suite retains its
configured structural membership. An explicit empty list makes that suite's effective
inventory empty; it does not trigger pytest's default discovery. An empty mapping has
no effect. List order is normalized lexically and duplicate paths are refused, rather
than treated as aliases. Each list is limited to 4,096 files. Values are lists of canonical project-relative existing regular
Python test files: no absolute paths, `./`, `..`, repeated separators, backslashes,
symlinks or symlink ancestors. A file must satisfy that suite's configured test roots,
include/exclude patterns and Python test-file patterns. Foreign or excluded modules,
unknown or unselected suite keys, and nonfiles refuse before any session starts.

This is a narrowing operation only. It never changes cwd, rootdir, routes, source
declarations or capabilities. Input declarations remain beside tests.
`plan.make_plan(root, suites=None, structural_files=None)` supports the same static
validation and effective file preview without importing tests or starting a sandbox.
It cannot establish the real parametrized inventory.

Suite identity includes the normalized explicit override (including an empty list),
conservatively distinct from an omitted override even if effective files happen to match.
The effective current file list also keys inventory. Expanding a subset or including a
new file refreshes the affected suite/context; files outside a supplied exact list stay
outside that invocation's inventory. Case/group keys retain their existing scope and
context rules. Other suites retain their own unchanged identities.

`collection.suites.files` exposes each effective file list before collection starts.
`inventory_complete` means complete within these effective structural boundaries; it
never claims coverage of files omitted by the override. Omitted files produce neither
cases nor unselected rows. Callers remain responsible for any broader coverage
requirement. Newly added files are discovered automatically only in suites without an
explicit exact-file override, or when the caller adds them to that override.

Scoped sessions use the third-party packages installed in the invoking environment,
read-only, with plugin autoload disabled; suite plugins are explicit import names that
must be installed there. Ordinary sessions load plugins as ordinary pytest does.
Distributed schedulers (including xdist), scheduler options and conflicting plugin
declarations are rejected; telescope alone owns parallel workers. Local distribution
metadata never satisfies third-party installation.

## Parallel scoped workers

```toml
[tool.pytest_telescope.execution]
workers = 4
```

`workers` is the number of independent scoped sandboxes that may run at once: a positive
integer of at most 32, default 1. `run --workers N` and `controller.run(root,
workers=N)` override it for one invocation. It is a concurrency count, not a resource
budget: pytest-telescope sets no CPU, memory, process or scratch limits and does not
infer them from the host. Limit resources in the job that runs pytest; every sandbox is a
child process of the invocation and stays inside those limits.

Collection and ordinary sessions run one at a time; independent scoped case misses or
batches then use up to `workers` sandboxes. By default each scoped case starts a fresh
sandbox and pytest session with private scratch. The optional [batch profile](batching.md)
instead shares one fresh sandbox and scratch only among exactly equal scopes in one
suite/module. The number of workers does not change results and is not part of any cache
key. The controller schedules at most `workers` sessions, without an unbounded future
queue. All summary rows appear in deterministic suite/node order and each current case
is accounted exactly once. Completion order never changes identity or counts.

A single controller owns the stable cache lock and all cache mutation. Worker threads
return fully validated results only after their own sandbox has exited. New records stay
pending until every worker, cancellation handler and frozen-copy cleanup completes. Any
terminal cleanup failure prevents publication of all new records; previously eligible
records remain intact. The scheduler stops submitting work on cancellation or terminal
cleanup failure, drains active work, marks unscheduled selected cases with explicit
infrastructure outcomes, and never accepts half-cleaned successes. Infrastructure retry
remains at most two attempts per scoped case or batch; a retry occupies the same worker
slot.

## Explicit capability refusals

The sandbox provides the [filesystem view](bubblewrap.md): read-only declared project
files (at `/project` and at the project's host path) and metadata, the read-only host
interpreter, environment and system directories, private writable scratch, and the host
network. It does not install tools, build runtimes, limit resources or isolate the
network.

A suite may declare `capabilities = ["python", "project:readonly"]`. Requesting
`network:none`, `scratch:noexec` or any other capability returns a machine-readable
`unsupported` result with that capability
code and exit status 4. A refusal cannot mint a success or a reusable cache entry, and
never silently widens the execution profile or falls back to ordinary execution. A
refusal or incomplete inventory alone does not prove that no earlier case executed. Do
not automatically replay an invocation after a refusal. Missing files or invalid syntax
remain declaration errors; sandbox outages remain infrastructure errors. Runtime code
that invokes a missing executable remains a test failure: telescope does not guess
capabilities from arbitrary test bodies or retry under relaxed restrictions.

## Result and privacy contract

The [run result reference](result-schema.md) defines schema 3, complete/incomplete
inventories, routes, selected-case accounting and typed refusals for CLI and Python
callers. Collection emits opaque case identities, hashed selector aliases and each
selected case's actual execution-order position (null for unselected cases). Raw
collected node IDs remain transient inside each session; execution requests, inventory
records and public results use the opaque identity. Explicit caller-supplied `-k` and
`-m` expressions still reach every session unchanged, as the result reference explains.
Structural file names and configured suite names remain
visible. Terminal cleanup failures preserve known accounting and prevent new cache
publication.

A project supports at most 64 suites and 100,000 combined actual cases across the
selected suites. Concurrent fresh workers are capped at 32. Inventory storage retains
at most `max(8, selected_suite_count)` records, bounded by 64 records and 64 MiB total.
These are independent bounds: increasing suite count never raises the case, worker or
aggregate inventory-byte limit.

Static translation accepts at most 256 distributions, 1 MiB per source pyproject,
4 MiB generated metadata per distribution, 16 MiB generated metadata total, and 4,096
combined dependency/entry-point items per distribution. Optional extras are normalized
and their environment markers combine with the extra condition. Metadata source symlinks
and host-environment paths are not supported inputs.

## Qualification

Behavioral tests cover metadata version and entry-point lookup, source without metadata,
metadata without source, negative lookup of other distributions, independent root/cwd/
conftest behavior, structurally excluded modules and isolated child interpreters
importing declared source through an editable install inside real Bubblewrap sandboxes,
plus routing, selectors, workers, retries and cleanup. See [development](development.md) for
the full gate and its current status.
