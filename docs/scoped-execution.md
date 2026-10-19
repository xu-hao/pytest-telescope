# Scoped metadata, independent suites, and parallel workers

This document specifies the next-alpha extension to the
[execution contract](execution-contract.md). Implementation and qualification must
satisfy these boundaries before publication. Existing releases are immutable.

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
    assert entry_points(group="example.plugins", name="addon")[0].load() == "addon"
```

`distributions` is additive in markers, defaults, and bundles. Like `inputs` and
`bundles`, a literal marker field establishes narrow scope, including an empty list.
Broad cases receive all configured distribution metadata. A narrow case receives only
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
versioned identity and read-only normalized modes/timestamps, participates in the exact
case key, and is mounted at `/metadata` only for that worker. The worker adds this
metadata-only directory to Python's search path before pytest startup. All application
imports still resolve through the declared `/project` snapshot and explicit pytest
configuration. No application source enters the shared image or metadata tree.

The effective metadata set is complete: omitted distributions are absent, including
negative lookups. Duplicate normalized names or conflicts with installed runtime
packages refuse rather than shadowing runtime metadata. Changing a selected metadata
source, build input, entry point, generated representation, or relevant application
source invalidates its consumers. Unrelated narrow resources do not. Missing/deleted
required sources fail validation. Broad cases retain broad invalidation.

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

Suite names identify separate pytest invocations. Each has its own collection worker,
working directory, actual pytest root directory, explicit configuration, options and
plugin activation. Configuration supports pytest.ini, .pytest.ini, pyproject.toml,
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

The worker normalizes pytest root-relative node IDs using the actual item path, retaining
class/function/parameter suffixes. Collection and execution use identical normalization;
marker origin paths and containing-file inventory digests stay project-relative. The
suite identity, root, configuration bytes, arguments, plugins, structural membership,
collection inputs and runtime all key inventory reuse. Case keys cover the same suite
contract and their own actual scopes. Additions/removals refresh the affected suite's
inventory; unrelated suite resource changes do not invalidate narrow case results.

Pinned third-party dependencies continue to be prepared from exact wheel pins with a
complete dependency closure and `pip check`. Autoload is disabled. Suite plugins are
explicit import names. Distributed schedulers (including xdist), scheduler options and
conflicting plugin declarations are rejected; telescope alone owns parallel workers.
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

Defaults retain one worker with the existing resource policy. When a worker or budget
table is supplied, every dimension is explicit. Positive integer dimensions are validated
before collection or any cache hit. Concurrency is the minimum of each aggregate budget
divided by its per-worker limit, capped at 32 to reserve bounded lifecycle journal
capacity. An envelope that cannot fit one worker refuses. Host CPU discovery does not
set concurrency. Collection runs sequentially within one worker slot, then independent
case misses use the computed bound. Each case starts a fresh container and pytest
session with private scratch. Aggregate limits bound active worker containers; bounded
host control, frozen pool storage and output retain their separate documented limits.

Per-worker CPU, memory (without extra swap), process and scratch quotas are Docker
limits and are cache identity. Aggregate scheduling budgets affect scheduling; because project configuration is an
automatic input, changing a budget conservatively invalidates results too. This does not enable mutable-session reuse or shared-case groups.
The controller schedules at most the worker bound, without an unbounded future queue.
All summary rows appear in deterministic suite/node order and each current case is
accounted exactly once. Completion order never changes identity or counts.

A single controller owns the existing stable cache lock and all cache mutation. Worker
threads return fully validated results only after their own container/client/descendant
cleanup. New records stay pending until every worker, cancellation handler, frozen pool,
and invocation cleanup completes. Any terminal lifecycle failure prevents publication
of all new records; previously eligible records remain intact. The scheduler stops
submitting work on cancellation/terminal cleanup failure, cancels and drains owned active
work, marks unscheduled selected cases with explicit infrastructure outcomes, and never
accepts half-cleaned successes. Infrastructure retry remains at most two attempts per
case; retry attempts occupy the same worker slot. No global Docker pruning is permitted.

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
success or a reusable cache entry. Consumers may route that result to an ordinary run
under their own policy. Missing files or invalid syntax remain declaration errors;
backend outages remain infrastructure errors. Runtime code that invokes an undeclared
missing executable remains a test failure: telescope does not guess capabilities from
arbitrary test bodies or retry under relaxed restrictions.

## Qualification

The synthetic Docker investigation in `scripts/probe_boundaries.py` checks standard
version/entry-point lookup and load, source without metadata, metadata without source,
negative lookup of the other distribution, independent root/cwd/conftest behavior,
explicit asynchronous fixtures, and a deliberately unimportable structurally excluded
module. The implementation gate must additionally cover frozen provenance and mutations,
unsupported metadata, membership changes, selectors, parallel quota/isolation/cancellation,
retry exhaustion, cleanup, supported Python/pytest, existing regressions, and useful-scale
cold/warm/selective benchmarks. Measurements must report startup overhead honestly.
