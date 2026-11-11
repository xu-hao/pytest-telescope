# Metadata, independent suites, and parallel workers

> Per-scope environment inputs, frozen tools, helper ownership and the opt-in fixture
> guard are delivered in version 0.1.0. Frozen-checkout/history support is deferred
> and is not implemented by this version.

Use these settings when tests need local package metadata, when parts of your project
have separate pytest configurations, or when you want to run scoped tests in parallel.
The [declaration reference](execution-contract.md) explains how inputs are combined;
the [execution contract](bubblewrap.md) describes the sandbox.

## Static project metadata

Tests that call `importlib.metadata` can read metadata generated from a declared
`pyproject.toml`. This is a project input, separate from the packages installed in your
test environment. Configure it with `pep621-static-v1`:

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
build inputs. Declare application source separately in `inputs` if the test imports
the package. Ordinary cases receive no generated metadata.

Version 1 supports static `[project]` name, version, description, requires-python,
dependencies, optional-dependencies, scripts, gui-scripts and entry-points. All fields
are validated, names and versions normalized, and dependency strings validated using
packaging standards. Unsupported fields and any nonempty `dynamic` declaration are
reported as unsupported. The
`[build-system]` declaration and complete source TOML bytes participate in identity.
Build inputs are explicit files/directories/globs, not executable hooks. No build
backend is imported, installed, or executed to create metadata. A missing project table,
missing static version, backend-path, setup-only metadata, or dynamic build output is
unsupported. Other `[tool]` configuration remains inert keyed data.

The translator reads static fields according to the [PyPA project metadata
specification](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
and writes `METADATA` and `entry_points.txt` in the [installed-project metadata
format](https://packaging.python.org/en/latest/specifications/recording-installed-packages/).
It only generates metadata: it does not build a wheel or install the package.
Tests can discover console-script metadata, but there are no generated launchers.
Wheel-specific files and other backend-generated resources are unsupported.

The controller freezes the source before generating metadata. The generated files have a
versioned identity, file mode `0444`, directory mode `0755`, and epoch
access/modification times. The sandbox mount is read-only. This representation
participates in the exact case key and is mounted at `/metadata` only for that sandbox.
The worker adds this metadata-only directory to Python's search path before pytest
startup. Application imports resolve to separately declared source through `/project`
and explicit pytest configuration. In a scope without a native tool selection, an
editable install of the project reaches the same snapshot at `/project`, because the
sandbox shows the install's files rewritten to that path. Native
startup closes interpreter prefixes and forbids selected startup hooks, as described
[below](#frozen-installed-python-tools-development-contract). Child interpreters do not
see `/metadata` or the parent's pytest import path unless their invocation explicitly
establishes those search paths. Generating metadata does not install a package or plugin.

The effective metadata set is complete: omitted distributions are absent, including
negative lookups. Duplicate normalized names or conflicts with packages installed in the
invoking environment cause a refusal. Changing a selected
metadata source, build input, entry point, generated representation, or relevant
application source invalidates its consumers. Unrelated narrow resources do not.
Missing/deleted required sources fail validation.

## Frozen installed Python tools (development contract)

`distributions` above generates metadata from project inputs. A selected frozen tool's
`python` records instead bind an **already-installed** distribution: actual package
modules, resources, dist-info/RECORD, entry-point metadata, required installed dependencies
and the invoking interpreter/runtime files needed by that closure. See the
[exact record format](bubblewrap-api.md#installed-python-distribution-records).
The runner copies no whole environment and installs no wheel.

Package and dist-info roots retain their actual import paths through narrow readonly
aliases. A scoped parent and a real `sys.executable -I` child therefore use the same
frozen package bytes and metadata without a `PYTHONPATH` override. For an already-installed
example package whose manifest pins the module and resource below, the test keeps its
normal isolated child call:

```python
import subprocess
import sys
import pytest


@pytest.mark.telescope(tools=["example_plugin"])
def test_installed_plugin_in_isolated_child():
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from importlib import metadata, resources; import example_plugin; "
            "assert example_plugin.VALUE == 7; "
            "assert metadata.version('example-plugin') == '1.0.0'; "
            "assert resources.files('example_plugin').joinpath('message.txt').read_text() "
            "== 'ready\\n'",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
```

Changing module, resource, entry-point or RECORD bytes changes the claimed closure even
when the distribution name and version are unchanged. Stale pins refuse; an updated
valid manifest changes the consuming key. The existing invoking-environment identity
still applies to all scoped cases. Nonconsumers may already see the installed package
through the baseline environment; selecting a tool adds frozen byte identity rather
than redefining general installed-package visibility.

The normal hashless RECORD self-entry is supported because the manifest independently
pins RECORD. Usable generated bytecode must be pinned with its source/runtime relations;
an absent optional pyc cannot be read from a live fallback. Missing source/resource pins
and unsupported finder, editable, namespace or import-hook layouts refuse. These
restrictions concern the explicit installed-tool profile. Existing editable installs
of the project itself retain their [baseline behavior](bubblewrap.md#scoped-cases-the-filesystem-view)
only in scopes without a native tool selection. A native consumer must explicitly
select its startup/runtime bytes and cannot use a `.pth` or finder hook. The scoped
parent can still use declared project source through pytest configuration; this does
not promise unchanged editable imports from an isolated child.

A selected native loader profile closes its declared library-search directories, so
its explicit closure also includes the invoking worker's shared libraries, native stdlib
extensions and required runtime resources. A stdlib tree is a permitted narrow runtime
input; use sparse explicit runtime files when that directory also contains unrelated
installed packages. Missing support refuses before project imports and never causes
ordinary replay or construction of another environment.

Supported ELF in the selected Python runtime or extension closure also activates the
[declared native configuration view](bubblewrap-api.md#declared-native-system-configuration).
The isolated child sees only selected frozen `/etc` entries and required loader controls;
resolver, certificate and other needed configuration must be explicit dependencies.
The same rule applies to a static ELF closure. It does not change the baseline system
view of cases that select no native tool.
Native startup uses the [supported prefix closure](bubblewrap-api.md#invoking-python-startup-closure),
including the actual invoking/base interpreter aliases, stdlib and installed pytest
dependencies. The complete prefixes narrow to selected bytes; a live site directory,
`.pth` hook or `PYTHONPATH` cannot supply a missing dependency. The supported virtual
environment's existing `pyvenv.cfg` is pinned, never generated by the runtime.

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
The file list is refreshed before reuse. Excluded modules are omitted from structural
test parsing and pytest's collection targets, and cannot be selected with `-k`, `-m`,
or node selectors. A scoped case may still explicitly declare an excluded Python file
as a readable helper: its frozen literal calls are validated and the consumer can import
it, without collecting its tests or inheriting its declarations. A file may occur in
multiple suites; the pair of suite name and canonical project-relative pytest node ID
identifies each case. You do not need to maintain a separate list of cases.

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
invocation before case-declaration AST parsing or selection of pytest collection
targets. This is separate
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

This option only removes files from the invocation. It leaves cwd, rootdir, routes,
input declarations and capabilities unchanged.
An omitted file explicitly declared as a helper remains readable in that consumer's
scoped view, with its literal calls authenticated against the frozen source. This does
not put the helper's tests back into the inventory or grant its own declarations.
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
declarations are rejected; use telescope's `workers` setting for parallelism. Install
third-party packages in the environment; declaring local metadata does not install them.

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
infrastructure outcomes, and waits for cleanup before accepting results. Infrastructure retry
remains at most two attempts per scoped case or batch; a retry occupies the same worker
slot.

Under the deferred frozen Git design, its declaration would supply a supported committed
tree and history only to its consumers. That design is not implemented by this candidate.
Its collection union would not grant that union to narrow execution or globally key
unrelated case results. See the
[deferred Git view](bubblewrap.md#frozen-git-checkout-development-contract).

## Explicit capability refusals

The sandbox provides the [filesystem view](bubblewrap.md): read-only declared project
files (at `/project` only; nothing is mounted at the project's host path) and metadata,
the read-only host
interpreter, environment and system directories, private writable scratch, and the host
network. It does not install tools, build runtimes, limit resources or isolate the
network.

**Not location-blind.** A scoped declaration lets identical checkouts at different paths
share a result, so the view shows the project at `/project`, wherever the checkout lives.
Two things a scoped test can read still name its checkout's location, which its cache
key does not contain:

- the source directories in the mount table (field 4 of `/proc/*/mountinfo`);
- the raw bytes of cached bytecode files (`__pycache__/*.pyc`) of an environment inside
  the project.

Leave a test that reads either one ordinary, or add `cache=False` to its scope.
See [what a scoped test can still see](bubblewrap.md#what-a-scoped-test-can-still-see).

Explicit [frozen tools](bubblewrap.md#frozen-tools-and-runtime-files-development-contract)
add their declared immutable roots and aliases. Native selections close `/etc` to
declared frozen configuration and loader controls and may narrow native search
directories. The exact closure is per consumer; unselected external tool material is
absent. Unsupported declarations and layouts refuse without adding a live host mount.

A suite may declare `capabilities = ["python", "project:readonly"]`. Requesting
`network:none`, `scratch:noexec` or any other capability returns a machine-readable
`unsupported` result with that capability
code and exit status 4. A refusal creates no reusable result. The runner does not widen
the sandbox's view or fall back to ordinary execution. A
refusal or incomplete inventory alone does not prove that no earlier case executed. Do
not automatically replay an invocation after a refusal. Missing files or invalid syntax
remain declaration errors; sandbox outages remain infrastructure errors. Runtime code
that invokes a missing executable remains a test failure: telescope does not guess
capabilities from arbitrary test bodies or retry under relaxed restrictions.

## Result and privacy contract

The [run result reference](result-schema.md) defines schema 4, complete/incomplete
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

## Required fixture guard boundary (unreleased)

The planned [fixture guard](batching.md#required-fixture-guard-unreleased) applies only
to selected scoped cases within the invocation's effective suites and structural files.
It refuses ordinary or mixed boundaries, preserves exact group inputs and does not claim
conservation for excluded or unselected cases. Existing default routes remain unchanged.
