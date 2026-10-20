# Scoped metadata, independent suites, and parallel workers

This guide carries the metadata/suite/resource boundaries into the accepted
[ordinary/Bubblewrap replacement](bubblewrap.md). **UNQUALIFIED:** the resource mechanism
proof does not qualify production runtime, routing, lifecycle or capabilities. Existing
releases remain immutable; see the [API/runtime contract](bubblewrap-api.md).

## Static project metadata

Local distribution metadata is a frozen PROJECT input, distinct from the pinned
third-party runtime. A project opts into the versioned static translator:

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
`bundles`, a literal marker field establishes narrow scope, including an empty list.
Ordinary cases use their invoking environment. A scoped case receives only
its effective distribution names, their metadata source and declared build inputs.
Declaring metadata never implicitly grants the package's application source.

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
access/modification times. The worker mount is read-only. This representation participates
in the exact case key and is mounted at `/metadata` only for that worker. The worker adds this
metadata-only directory to Python's search path before pytest startup. All application
imports still resolve through the declared `/project` snapshot and explicit pytest
configuration. No application source enters the prepared runtime or metadata tree.

The explicit [isolated Python source profile](isolated-source.md) can make selected
metadata and declared source roots discoverable by `python -I` children as well. It
uses a per-view read-only path projection; it is not an application installation and
never grants files merely by naming a source root. The default profile remains unchanged.

The effective metadata set is complete: omitted distributions are absent, including
negative lookups. Duplicate normalized names or conflicts with installed runtime
packages refuse rather than shadowing runtime metadata. Changing a selected metadata
source, build input, entry point, generated representation, or relevant application
source invalidates its consumers. Unrelated narrow resources do not. Missing/deleted
required sources fail validation. Ordinary results are always fresh; explicitly broad scoped inputs key their declared content.

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

Suite names identify separate pytest invocations. Each preserves its own ordinary session and independent scoped collection boundary,
working directory, actual pytest root directory, explicit configuration, options and
plugin activation. `root` sets pytest's root directory. Optional `cwd` sets the worker's
working directory independently and defaults to `root`; both are existing directories
relative to the outer project. For example, `root = "packages/core"` with `cwd = "."`
preserves an invocation from the workspace directory with a nested pytest root.
Configuration supports pytest.ini, .pytest.ini, pyproject.toml,
tox.ini and setup.cfg; unsupported configuration syntax refuses. Suite configuration
and applicable conftest files remain inputs. Narrow cases must declare conftest files
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
collection inputs and runtime all key inventory reuse. Case keys cover the same suite
contract and their own actual scopes. Additions/removals refresh the affected suite's
inventory; unrelated suite resource changes do not invalidate narrow case results.

### Invocation-only structural file narrowing

Python callers can pass `controller.run(root, structural_files={"core":
["tests/core/test_small.py"]})` to narrow configured structural membership for this
invocation before marker AST parsing or isolated collection imports. This is separate
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
unknown or unselected suite keys, and nonfiles refuse before backend startup.

This is a narrowing operation only. It never changes cwd, rootdir, source declarations,
resource limits or runtime capabilities. Input declarations remain beside tests.
`plan.make_plan(root, suites=None, structural_files=None)` supports the same static
validation and effective file preview without importing tests or contacting a backend.
It cannot establish the real parametrized inventory or determine runtime capabilities.

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

Pinned third-party dependencies continue to be prepared from exact wheel pins with a
complete dependency closure. Scoped autoload is disabled. Suite plugins are
explicit import names. Ordinary sessions use invoking-environment plugins. Effective
distributed scheduling (including xdist) refuses; overridden inert addopts and an installed
inactive plugin are not active scheduling. Pytest option precedence remains intact.
Local distribution metadata never satisfies third-party runtime installation.

## Resource-derived fresh parallelism

```toml
[tool.pytest_telescope.execution.worker]
cpus = 1
memory_bytes = 536870912
processes = 128
scratch_bytes = 67108864

[tool.pytest_telescope.execution.budget]
cpus = 4
memory_bytes = 2147483648
processes = 512
scratch_bytes = 268435456
```

Defaults retain one scoped worker. Every supplied worker or budget table includes all
four positive integer dimensions. Memory/scratch values must be 4096-byte multiples.
Concurrency is the minimum aggregate-to-worker ratio, capped at 32; no host CPU inference.
Potential scoped work requires a budget that fits one worker before restricted collection.
A true all-ordinary invocation does not require Bubblewrap, prepared runtime artifacts or
scoped-resource prerequisites, and does not fit-check the configured worker envelope.
Syntax errors remain errors. Unselected scoped cases can still require restricted discovery.

Scoped workers enforce real per-worker CPU, memory without extra swap, process and tmpfs
byte/inode limits, and the aggregate worker pool enforces its budget. Scheduler counts
and RLIMIT_AS alone are insufficient. Controller/setup CPU, memory, processes and frozen
storage are admitted separately; worker budget retains its aggregate-worker meaning.
The [API/runtime contract](bubblewrap-api.md) specifies all four controller fields and
actual resource reporting. Scope identity includes normalized actual limits.

Complete independent restricted discovery/preflight first. Then ordinary suite sessions
run sequentially, each occupying one permit while alive. Every ordinary session ends
before scoped execution starts; at concurrency one no retained host pytest process can
block discovery. Ordinary tests inherit host limits, not the scoped slice. Explicit
case/batch policy affects scoped fixture lifetime only. Independent scoped groups run
concurrently within the admitted envelope, each in a fresh sandbox/session/scratch.

The scoped controller owns the stable cache lock and pending-record staging through all
worker and outer cleanup. New records become eligible only after complete cleanup;
terminal lifecycle failure prevents all new publication while preserving prior valid
records. Cancellation stops submission and drains exact owned resources. Scoped
infrastructure retry remains at most two attempts per group in the same slot; ordinary
sessions are never automatically replayed. Never use broad cleanup or relaxed fallback.

## Explicit capability refusals

The first profile remains pinned Python wheels and their packaged executables, read-only
project inputs, read-only metadata, private noexec scratch, and no network. Runtime and
source files never come from host checkout/environment mounts. Native toolchain installs,
browsers, alternate interpreters, executable scratch, writable source, services, and
unsupported link/metadata forms require a separately specified and qualified profile.
This extension does not add those profiles or silently install missing tools.

A suite may declare `capabilities = ["scratch:exec"]`, for example. This profile
supports `python`, `project:readonly`, `scratch:noexec`, and `network:none`; other
capabilities require a separately qualified profile. Explicit capability declarations
that this profile cannot satisfy return a machine-readable
`unsupported` result with a stable capability code and exit status 4. They cannot mint a
success or a reusable cache entry. Refusal does not silently widen the execution profile.
Literal declarations determine ordinary/scoped routing before execution; restricted
collection supplies independent scoped authority. A refusal or incomplete inventory
alone does not prove that no earlier ordinary import or case executed.
Do not automatically replay an invocation after a runtime refusal.
Missing files or invalid syntax remain declaration errors;
backend outages remain infrastructure errors. Runtime code that invokes an undeclared
missing executable remains a test failure: telescope does not guess capabilities from
arbitrary test bodies or retry under relaxed restrictions.

## Result and privacy contract

The [run result reference](result-schema.md) defines schema 3, complete/incomplete
inventories, selected-case accounting and typed refusals for CLI and Python callers.
Collection emits opaque case identities, hashed selector aliases and each selected case's actual execution-order position (null for unselected cases). Raw
collected node IDs remain transient inside the worker; execution requests, inventory
records and public results use the opaque identity. Explicit caller-supplied `-k` and
`-m` expressions still reach the worker unchanged, as the result reference explains.
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

Production controls must cover metadata projection, omitted distributions, source without
metadata, metadata without source, version/entry-point lookup/load, independent cwd/root/
configuration/plugins, and structural exclusion before imports. They also cover source
mutation, selectors, mixed ordinary/scoped reconciliation, preserved fixture lifetimes,
resource enforcement, cancellation, bounded retries, descendant cleanup and supported
Python/pytest/runtime layouts. Mock responses do not qualify native enforcement.

### Candidate qualification status

**UNQUALIFIED.** The bounded resource mechanism passed
[run 39503556501](https://github.com/xu-hao/pytest-telescope/actions/runs/39503556501).
It proved the measured controller/worker/pool/aggregate quotas, concurrent declared views,
CPU throttling, owned memory/PID exhaustion, scratch limits and exact cleanup. It did
not qualify prepared runtimes, root filesystem closure, mixed routing, lifecycle or
schema integration. Historical Docker candidate receipts remain historical; they do
not qualify the replacement or establish a 0.1.0a4 release.

The [Git runtime](git-runtime.md) and [isolated child lookup](isolated-source.md) remain
required work in the replacement queue. Neither Bash, executable scratch, browsers nor
services are added. See [development](development.md) for remaining production gates and
[benchmark methodology](benchmark.md) for historical measurements and required new trials.
