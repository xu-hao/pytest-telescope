# Execution and reuse contract

## Status

This guide describes the accepted replacement contract in [ordinary/Bubblewrap routing](bubblewrap.md)
and its [exact API/runtime seam](bubblewrap-api.md). **UNQUALIFIED:** runtime preparation,
mixed routing, lifecycle and the complete backend gate remain required. The bounded
resource proof alone passed. Published releases remain immutable; their Docker behavior
and schema 2 records do not become replacement behavior or cache evidence.

## User interface and scope

A case without an effective literal `inputs`, `bundles` or `distributions` declaration
runs through ordinary pytest, always fresh, in the invoking environment. Inherited
module/class/function/parameter declarations count, including literal empty lists.
Project defaults, absence-only assertions and cache-only markers do not opt into scope.
Absence-only markers still check exactly their named paths. Invalid declarations refuse.

Scoped `case` mode remains the default: each actual scoped pytest item, including each
parametrized variant, has a fresh execution and successful-result cache unit. Explicit
[batch mode](batching.md) groups equal scopes within one suite/module. New files and
actual generated cases must enter the current inventory before reuse.

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

Module/class/function/parameter declarations add together with applicable defaults,
the containing test source and project/suite configuration. Any inherited `cache=False`
disables scoped reuse. `inputs=[]` opts into a restricted view with automatic inputs
and defaults; `cache=False` alone is ordinary and fresh.

Ordinary execution uses the invoking interpreter, installed pytest/plugins, environment,
and each suite's actual cwd, rootdir, configuration and arguments. It never looks up or
records reusable case results. All-ordinary routing reads bounded structural source and
configuration, without walking, copying or hashing unrelated repository content. An
unrelated ignored build tree, socket or directory symlink is not a snapshot error.
Explicit input and absence declarations retain strict path validation.

Scoped inputs exclude version-control metadata (`.git`, `.hg`, `.svn`), telescope state
(`.pytest-telescope` and an explicitly selected in-project cache directory), pytest and
bytecode caches, and host Python environments. Explicit declarations into excluded
namespaces refuse. Declared directories/globs may deliberately include broad content;
that content is bounded and hashed. Directory symlinks and special files in a declared
scope refuse. An ordinary case's host filesystem access is not a scoped input guarantee.

Pyproject stores test/suite settings, shared defaults and reusable bundles. Shared
conftest, application modules, initializers, helpers and parametrization resources must
be declared when restricted collection or execution needs them. Defaults add support
but do not independently route cases. Old central `targets` declarations remain rejected.

Each suite has one fresh ordinary pytest session; ordinary module/class/session fixtures
keep normal lifetime across its ordinary tests. Scoped case sessions recreate them per
case; an explicit scoped batch shares them only within its equal-scope group. No fixture
state crosses the ordinary/scoped boundary or independent suite sessions.

## Static discovery and real pytest agreement

The controller parses bounded test source syntax without importing or evaluating project Python. Supported spellings use `pytest.mark.telescope(...)` directly in function/class decorators, module/class `pytestmark`, or `pytest.param(..., marks=...)` inside literal parametrization declarations. Values are literal string lists and booleans; bundles provide reusable declarations. Arbitrary expressions, computed kwargs, declaration factory functions, aliases and generated telescope markers are unsupported. Ordinary parametrization values and pytest-generated item identities still come from real pytest collection, not an AST approximation.

A pre-import declaration guard compares supported literal calls with registered frozen
source origins and immutable canonical arguments. Generated origins, mutated markers,
duplicate identities and changes after the validated collection checkpoint refuse.
Real pytest supplies dynamic parameter identities and actual selection, not AST guesses.

First perform independent restricted discovery and required preflight for every potential
scoped partition. Its declared collection union never widens because ordinary cases are
possible. A pure unconditional module-inherited scoped file stays out of ordinary
collection. Mixed modules and conftest files may import in ordinary context, and those
imports/hooks are ordinary side effects. Before fixture setup or case bodies, the ordinary
checkpoint removes every scoped item from pytest dispatch and reconciles mixed-file
membership, origins and selection/order against restricted collection. Disagreement
refuses without broader retry; host inventory supplies no scoped cache authority.
Arbitrary ordinary import code can itself call arbitrary Python functions, so this
checkpoint is a dispatch boundary, not import isolation.

Collect, execute and tear down each ordinary suite session sequentially. Finish all
ordinary sessions before starting scoped execution. A live ordinary session occupies one
scheduling permit; no restricted worker permit is held then. At concurrency one, no
retained ordinary process can block scoped discovery. A separately bounded scoped
controller may retain its explicitly admitted headroom.

Per-case execution begins a new sandbox with only that case's effective scope, before conftest/test imports. It collects its containing file, verifies the actual item/declaration inventory against the discovery record, and executes exactly the selected item in case mode. Batch execution instead validates the complete containing-module inventory and the requested ordered subset before executing that equal-scope group. A narrow execution missing a shared collection dependency fails with bounded collection evidence; it does not borrow another case's inputs. Common import/parametrization dependencies belong in shared declarations. The fresh worker's collection must agree even when a data-dependent item generator produces different nodes under a narrow scope.

## Inventory, selection and reuse

Scoped inventory and execution have distinct strict result schemas. Ordinary collection is always fresh. A successful collection reports collected nodes and declaration assignments, never executed passes. It becomes reusable only after clean worker exit, complete teardown of the collection process, validated bounded records and invocation cleanup.

The inventory key covers the exact frozen discovery union, current test-file membership, static declaration registry, pytest configuration, relevant environment, full effective arguments/selectors, frozen runtime and backend identity, runner implementation/version, and backend policy. Every scoped invocation re-enumerates and freezes its declared inputs before accepting an inventory hit. File addition/deletion, resource changes or changed dynamic-collection inputs invalidate it. `--force` and `--no-cache` refresh inventory. Any statically present cache=False policy conservatively disables inventory reuse; unrelated cacheable case results may still be reused after fresh collection.

Result keys cover each case's actual node identity, effective literal declarations, exact executed snapshot, validated containing-file inventory/declaration digest, full options/selectors and all existing runtime/environment/runner/backend identities. They do not include an unrelated case's resource bytes merely because discovery saw them. Thus changing one narrow resource refreshes discovery but preserves unrelated case result hits. Ordinary cases have no reusable result keys. Explicitly broad scoped inputs key their declared content. Failures, interrupted/invalid records and incomplete cleanup never become hits.

Full current inventory is accounted for before selectors: selected cases are executed/reused/failed; other cases are explicitly unselected. `-k`, `-m` and node selectors use actual pytest semantics. Summaries report collection separately, actual case counts, fresh versus cached pass/skip outcomes, and no invented file-target granularity. Cache hit acceptance cannot hide new files/cases. Plan remains a nonexecuting static scope preview and explicitly does not claim real collected-case enumeration.

## Bounds and performance

Bounded defaults: 100,000 actual cases; 100,000 visible files; 200,000 visited entries; 64 MiB per source file; 2 GiB total frozen project bytes; 4,096 bytes per node ID/path; 64 path components; 64 MiB inventory/result-summary encoding; 1 MiB individual case execution response; 64 MiB batch execution response/cache record; 4,096 selected members per batch. An oversized group refuses the entire plan before fresh group execution instead of splitting silently. Static parsing accepts at most 4 MiB per Python source file and 64 MiB of Python source in total, 200,000 AST nodes per file and 2,000,000 total, depth 128, and 100,000 literal marker calls. Bounded traversal counts nodes without first constructing an unbounded list; parser-depth and allocation failures become declaration errors. Reusable bundle count/depth limits remain separate from case cardinality.

Retain up to 100,000 successful execution records (singleton cases or atomic batches) within a 256 MiB total cache budget, plus inventory records within a 64 MiB aggregate inventory budget. The inventory record-count bound is `max(8, selected_suite_count)`, at most 64 records. Enforce both count and byte limits under the existing stable lock; scan/GC once per invocation and incrementally account new records. New records remain ineligible pending files until the complete frozen-pool invocation has cleaned successfully. A cleanup failure discards those pending records and preserves previously valid hits. Pending storage has separate bounded budgets of 256 MiB for cases and 64 MiB for inventory; abandoned pending files are recovered under the same stable lock. Eviction changes performance only. Bounded output and record parsing apply during I/O, not after unbounded capture. Limits produce explicit errors and never silently truncate inventory.

Freeze each distinct file once per invocation. Derive case keys from immutable frozen-file records and memoized scope identities. Materialize only missed cases, using immutable files from the frozen pool without exposing its broader parent directory. Explicitly broad scopes can share immutable frozen files; every view contains only its selected paths. Do not copy/hash the whole project once per case. The default budget runs one worker; explicit resource envelopes permit bounded parallel fresh workers as specified in the [scoped execution extension](scoped-execution.md). Each miss starts one isolated pytest session per case by default, or per exact-scope group in batch mode; repeated collection of a large parametrized module is a real cost, not hidden by file-group benchmark claims. Warm scoped inventory/result hits start no test workers, but per-invocation runtime verification/freezing remains required. Ordinary collection and execution remain fresh.

## Project configuration

Run from the directory containing `pyproject.toml`. The dedicated CLI avoids host pytest adapter startup, but ordinary collection intentionally
imports project Python in the invoking environment. Proposed commands after explicit
candidate installation/preparation are:

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
The [independent suite declarations](scoped-execution.md) also support explicit
pytest.ini, .pytest.ini, tox.ini and setup.cfg files. Discovery honors configured `python_files`
and `test_roots` (default `["."]`). Bounds fail explicitly; discovery never silently
truncates matching files. On the scoped route, only named `env` variables are forwarded and keyed, including
presence. Ordinary sessions inherit the invoking environment. Controlled Python/pytest variables and `TELESCOPE_*` protocol names are
reserved. Diagnostics never print environment values.

## Runtime and filesystem enforcement

These requirements apply to scoped collection and execution before any project import,
through fixtures, bodies, teardown and descendants. Ordinary sessions are inherited-host
execution and must never be described as sandboxed or limited by the scoped budget.

The proposed backend is `bubblewrap-v1`. It uses namespaces plus separately enforced
cgroup-v2 CPU, memory and process limits and private noexec/nosuid/nodev tmpfs byte/inode
limits. Bubblewrap alone supplies no quotas. Source/runtime are read-only; networking is
disabled. Setup, supervisor, test and descendant processes all count. The aggregate worker
budget and separately admitted controller headroom must both be bounded. Entry-context
namespace preflight happens before any external user service; denial cannot be bypassed
by the user manager. Unsupported host prerequisites refuse without fallback.

The accepted resource mechanism passed [run 39503556501](https://github.com/xu-hao/pytest-telescope/actions/runs/39503556501).
This does not qualify runtime preparation, root filesystem completeness, lifecycle or
capabilities. The [API/runtime contract](bubblewrap-api.md) defines exact administrator
prerequisites, controller/pool accounting and supported runtime layouts. Production
launch never silently provisions its host or uses a privileged helper.

Project snapshots are copied independently of mutable host source, with read-only mode
`0444` plus source execute bits, directories `0755`, and epoch access/modification times.
Identity covers actual frozen bytes/modes/timestamps; source metadata fences concurrent
writes. Inode/device/ctime/ownership are not portable deterministic inputs. Runtime
files instead retain coherent bytecode-relevant timestamps, modes and symlink topology.
No live host checkout, host venv or mutable original runtime is mounted into workers.

`python-v2` proposes a clean frozen CPython interpreter, stdlib/native closure and exact
wheel dependency set. Both distro split-layout and dedicated-prefix preparation require
production controls. Application installs, host site-packages, editable installs, sdists
and guessed PATH executables are excluded. Additional wheel requirements must cover an
exact pinned transitive closure. Scoped plugin autoload remains disabled. Prepared
`git-local-v2` and `isolated-source-v1` child lookup remain in the required qualification
queue; no Bash, browser, service or executable-scratch capability is added.

### Explicit rebuilds after runtime upgrades

Prepare the replacement explicitly using the [proposed runtime interfaces](bubblewrap-api.md).
Old `python-v1`/`git-local-v1` Docker artifacts and schema 2 records remain untouched and
ineligible; no automatic migration or Docker fallback occurs. `run` and read-only
inspection never prepare, download, repair or rebuild missing/stale/corrupt state.
Inspection returns only `{profile, required_capabilities, identity}`, with at most two
retryable attempts, and supplies no execution/cache authority.

Preparation publishes a complete content manifest atomically after validation and owned
cleanup; failure preserves prior valid state. Runtime size remains bounded at 2 GiB and
100,000 entries, with at most four retained owned generations. Controller storage must
fit runtime, input pool, views and concurrent old/new preparation copies within explicit
byte/entry limits; exceeding it refuses before execution. Actual storage failure is an
infrastructure incident, never permission to accept partial state.

Worker defaults remain one CPU, 512 MiB without extra swap, 128 processes and 64 MiB
private scratch. Scoped collection/batch output is bounded to 64 MiB; individual case
output to 1 MiB, including structured protocol and direct descriptor writes. Protocol
capture drains concurrently and remains bounded during I/O. Resource reporting separates
configured requests from actual observed limits; unsupported enforcement cannot be
reported as success. See [schema 3](result-schema.md).

## Outcome accounting

The proposed [schema 3](result-schema.md) separates ordinary/scoped routes, keeps opaque
identities and conserved outcomes, and reports incomplete selection/routing honestly.


The controller reports current collected cases, selection, independent execution units,
and collection work separately. Each selected case has one outcome: executed success,
reused success, test failure, or infrastructure failure. Other current cases are
unselected. Fresh/cached passed and skipped counts remain distinct. A cached case is
not described as a newly executed test. Exit statuses remain 0 for success, 1 for test
failure, 3 for infrastructure failure, 4 for invalid declarations/arguments or unsupported capabilities, and 5
when no cases are selected.

Use discovered test-file paths with optional `::node` selectors, `-k EXPRESSION`, or `-m
EXPRESSION` to select cases. The accepted pytest options are:

- With a value: `-k`, `-m`, `-o`, `--override-ini`, `-p`, `--maxfail`, `--tb`,
  `--capture`, and `--color`. Long options also accept `--option=value`.
- Switches: `-x`/`--exitfirst`, `-q`/`--quiet`, `-v`/`--verbose`, `-s`,
  `--disable-warnings`, `--strict-markers`, `--strict-config`, `--runxfail`,
  `--showlocals`, `--no-header`, and `--no-summary`. Repeated short verbosity
  switches such as `-vv` are accepted.

Unknown flags and directory selectors are declaration errors. Effective options follow pytest precedence, including an explicit `-o addopts=`.
Effective xdist scheduling is unsupported; an installed inactive plugin or overridden
inert addopts is not active scheduling. Structural membership remains a separate boundary.
`-x` and `--maxfail` apply to each actual pytest invocation, not a synthetic global
session across ordinary/scoped routes. Pytest display options affect the actual session;
the controller emits its structured summary and bounded failure evidence. The direct
`pytest --telescope` adapter removes its controller flag before delegation to prevent
recursive dispatch; other host plugins may already have run during adapter startup.

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
case is reported as an infrastructure error rather than being omitted. A scoped case/group gets
at most two execution attempts for infrastructure failures. An ordinary session is never
automatically replayed after infrastructure failure; its effects may already have occurred. Cache lock acquisition is
bounded by twice the configured worker timeout plus 30 seconds. Startup, deadline,
output, storage, invalid-result, or cleanup failure that exhausts recovery ends with a
distinct infrastructure outcome. Resolve backend availability, capacity, runtime, or the
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
before routing to the controller; use `python -m pytest_telescope` to avoid that adapter startup. Ordinary suite sessions
still load their actual invoking-environment pytest/plugins.

The scoped supervisor must reap adopted completed descendants throughout long sessions
without stealing test-owned child statuses. Live descendants after teardown refuse.
Cancellation stops submissions and terminates only owned resources, with bounded cleanup
before any new evidence becomes eligible. Complete scoped CPU comes once from the final
owned cgroup sample after empty-state synchronization. `RUSAGE_CHILDREN` does not measure
all sandbox descendants; ordinary CPU remains null until separately qualified measurement
exists. A refusal or missing result never proves that earlier imports or tests did not run.
