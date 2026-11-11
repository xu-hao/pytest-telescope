# Declarations, inventory and reuse

> Per-scope environment inputs, frozen tools, helper ownership and the opt-in fixture
> guard are delivered in version 0.1.0. Frozen-checkout/history support is deferred
> and is not implemented by this version.

This page explains how pytest-telescope reads declarations, discovers cases and decides
whether to reuse their results. The [execution contract](bubblewrap.md) describes which
tests run in a sandbox and which files they can see.

## User interface and scope

In the default `case` mode, each scoped pytest case runs and caches its result separately.
That includes each variant of a parametrized test. Declarations stay beside the tests;
there is no separate list of cases to maintain. Discovery includes new test files and
generated cases before accepting cached results.

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

Module/class/function/parameter declarations are additive. A literal `inputs`,
`bundles`, `distributions`, `env` or `tools` field makes a case scoped, including an empty list.
Its effective scope is the union of those inherited declarations, project defaults,
containing test source and configuration source. A policy-only `cache=False` marker or
absence assertion does not make a case scoped. Any inherited `cache=False` disables
successful-result reuse for that item.

A case without such a field is ordinary: it runs in an ordinary pytest session on every
invocation and is never cached. pytest-telescope does not snapshot, walk or hash
undeclared project files for it.

Declarations cannot name version-control metadata (`.git`, `.hg`, `.svn`), telescope
state (`.pytest-telescope` and an explicitly selected in-project cache directory),
pytest/bytecode caches (`.pytest_cache`, `__pycache__`), or host Python environments
(`.venv`, `venv`, or a directory with a validated `pyvenv.cfg`). Directory symlinks
require an exact selected frozen-tool binding; all other directory symlinks, special
files and unsupported paths are rejected.

Put test roots and environment settings in `[tool.pytest_telescope]`, shared settings in
`[tool.pytest_telescope.defaults]`, and reusable declarations in
`[tool.pytest_telescope.bundles.NAME]`. Defaults add inputs to scoped cases but do not
make unmarked cases scoped. Include any conftest files, application modules, package
initializers, helpers and resources that collection or the test needs.

The default `case` mode starts a fresh sandbox and pytest session for each scoped case
that needs to run. Module, class and session fixtures are recreated for each case.
Optional [batch mode](batching.md) lets tests share fixtures and scratch within the same
suite and module, provided their expanded scopes are exactly equal. Each batch starts
a fresh sandbox, and its results are reused together. Cases with different inputs stay
in separate groups. Project files are read-only in both modes.

## Static discovery and real pytest agreement

The controller discovers case declarations from structurally selected test source
without importing or evaluating project Python, within the
[parsing limits below](#bounds-and-performance). Use `pytest.mark.telescope(...)`
directly in function or class decorators, module or class `pytestmark`, or
`pytest.param(..., marks=...)` inside literal parametrization declarations. Values must
be literal string lists or booleans. Use bundles to share declarations.

Expressions, computed keyword arguments, marker factories, aliases and generated
telescope markers are unsupported. Pytest collection still supplies ordinary parameter
values and the actual case identities.

A guard installed before project imports checks each marker call against its position
and literal arguments in the frozen source. Collected marks must come from those calls
and have unchanged values. The comparison uses an independent, immutable encoding of
the declaration, so mutating a list passed to pytest cannot change the expected value.
If dynamically constructed markers evade static checks, they fail the session.
They cannot widen a scope or change a route. Source-call authentication and case
ownership are separate checks; authenticating an unchanged call alone does not prove
that its declaration belongs to a particular collected case.

The development contract requires the complete observed declaration set to agree with
the case's legitimate owners before constructing its route or effective grants.
Module and class scopes and function decorators follow the actual module, applicable
class method resolution order and defining callable in the containing test source.
A base class's normally inherited method can have a different lexical owner from the
collected subclass. Same-file membership or a collected-name prefix alone is not
ownership evidence. Reusing an unchanged sibling function/class Mark remains an invalid
extra declaration. Losing any applicable declaration, including an empty scope or
policy-only marker, is also invalid.

For each inline literal `pytest.param` declaration, ownership additionally binds its
enclosing applicable parametrization dimension and selected literal alternative.
Class and function dimensions, independent stacked dimensions and multi-name dimensions
must preserve their actual choices. Every argument in a multi-name dimension must agree
on the same alternative. Parameter values, display IDs, selector aliases and the
function/class owner cannot substitute for that evidence. Only the selected
alternative contributes its Telescope marks; unmarked alternatives remain independent.

Ownership binding uses bounded, verified collection provenance. Implementation-specific
metadata must be checked against its concrete construction and lifecycle in the
supported Python 3.11/3.13 and Pytest 8/9 qualification profiles; it is not assumed
stable across versions. In particular, a collected item's parameter indices need not
retain dimension-alternative indices after Pytest constructs the Cartesian product.
Unknown, malformed, stale or inconsistent ownership evidence must refuse explicitly,
without guessing a shape or choosing an alternative from equal values or node labels.
Ordinary `pytest.Function` collection is the supported boundary; custom item kinds or
collection changes that cannot establish this ownership agreement are unsupported.

An ownership disagreement is a declaration error: the Python API raises
`DeclarationError`, and the CLI reports `declaration_error` with exit status 4.
The runner must neither discard invalid extra origins nor widen a scope, change a route
or retry in ordinary mode to accommodate them. A refusal still does not prove that no
earlier ordinary case executed.

For a scoped view, this source guard also authenticates literal calls in explicitly
readable frozen Python helpers, including package initializers and nested imports.
This helper declaration support is unreleased development; published 0.1.0a6 does not
provide it.
Declare every required helper, initializer and import-time resource in the consumer's
scope; the controller does not resolve imports or search undeclared source. A helper
outside structural test membership can be imported without collecting its tests.

Authentication and inheritance serve different purposes. A helper's literal marker
can be valid at its own exact source position without becoming a declaration inherited
by the consumer. Importing that helper does not grant its own inputs, bundles, local
metadata, environment names, absence assertions or cache policy to the importing case.
Its own `env` declaration does not forward or key host values for that consumer; the
consumer must declare or inherit those names independently. Attaching a foreign
helper marker to a selected case is unsupported; effective case declarations belong to
that case's containing test source.

The guard identifies a declaration by the calling module's current `__file__` within the
project. Cached assertion-rewrite bytecode can keep an old checkout filename; the guard
refuses missing, invalid or outside-project module origins instead of falling back to
that embedded filename. Helper origins use the same exact project-relative identity;
matching a basename or a marker's arguments is insufficient.

Files scoped by module-level `pytestmark` are collected in a sandbox. Its view combines
those files' potential declarations with the suite's project defaults and configuration,
including the union of their declared environment names. Collection
checks that every case inherited its module's scope. If a case did not, collection fails
without retrying with more files. No case body runs in this combined collection view.

The suite's ordinary session collects the other files, including files that mix ordinary
and scoped cases. Pytest discovers dynamic and parametrized cases and associates their
inherited markers with the source declarations. Hooks cannot add, remove or change cases
after collection has been validated. Duplicate or invalid node identities are errors.

Each scoped execution begins a new sandbox with only that case's effective scope, before conftest/test imports. It collects its containing file, verifies the actual item/declaration inventory against discovery, and executes exactly the selected item in case mode. Batch execution instead validates the complete containing-module inventory and the requested ordered subset before executing that equal-scope group. A scoped execution missing a shared collection dependency fails with bounded collection evidence; it does not borrow another case's inputs. Common import/parametrization dependencies belong in shared declarations. In a mixed file, the whole file, including its ordinary cases, must collect identically inside each scoped case's view.

## Inventory, selection and reuse

Inventory and execution have distinct strict result schemas. A successful sandboxed collection reports collected nodes and declaration assignments, never executed passes. It becomes reusable only after the sandbox exits cleanly with validated bounded records. Ordinary session inventories are never reused.

The strict worker `declaration_error` boolean distinguishes a validated failed pytest
session from an actual declaration refusal. A plugin setting exit 4 after a passing body
fails the group and run while preserving its observed reports; it does not trigger a
declaration refusal or cancellation of unrelated work. A true declaration error requires
exit 4 and incomplete evidence. The controller retains available evidence before the
typed refusal, drains owned work and publishes no pending successes. Missing or invalid
flags are refused without inference from exit codes. See the
[protocol and cache versions](result-schema.md#internal-protocol-and-cache-versions).

The scoped inventory key covers the exact frozen collection view, current test-file membership, static declaration registry and readable helper guard registry, pytest configuration, relevant environment, full effective arguments/selectors, the sandbox identity (policy, interpreter, installed distributions and the environment's references into the project) and runner implementation/version. Every invocation re-freezes current declared inputs before accepting an inventory hit. File addition/deletion, declared-resource changes or changed dynamic-collection inputs invalidate it. `--force` and `--no-cache` refresh inventory. Any statically present scoped cache=False policy conservatively disables inventory reuse; unrelated cacheable case results may still be reused after fresh collection.

Result keys cover each scoped case's actual node identity, effective literal declarations, exact executed snapshot, validated containing-file inventory/declaration digest, full options/selectors and the sandbox, environment and runner identities. They do not include an unrelated case's resource bytes or environment values merely because collection saw them. Thus changing one declared resource refreshes collection but preserves unrelated case result hits. Failures, interrupted/invalid records and incomplete cleanup never become hits.

The validated declaration inventory must include bounded structural ownership and selected
alternative evidence. That agreement must bind inventory, case and group contexts, so stale
ownership cannot reuse an earlier inventory or success. Raw parameter values, display
IDs and environment values remain outside persistent ownership records and normal
diagnostics.

The guard registry is bound to the same exact readable view. Changing a declared
helper's bytes invalidates its consumers; a helper available only to another case does
not join this case's registry or result identity. Stale or malformed registry evidence
cannot make an inventory, case or batch record reusable.

A selected frozen-tool project link is a verified data binding, including when its
project name ends in `.py`. Its external target is not an authenticated project helper
origin. Each readable view must select the exact binding and retain its captured link
text; helper authentication does not follow that link into external source.

Full current inventory is accounted for before selectors: selected cases are executed/reused/failed; other cases are explicitly unselected. `-k`, `-m` and node selectors use actual pytest semantics. Summaries report collection separately, actual case counts by route, fresh versus cached pass/skip outcomes, and no invented file-target granularity. Cache hit acceptance cannot hide new files/cases. Plan remains a nonexecuting static preview and explicitly does not claim real collected-case enumeration.

Implicit structural files are collection boundaries, not explicit requests for a case.
A validated collection establishes file coverage independently of collected-case aliases.
An explicit file target can therefore yield zero cases, including when collection skips
the file; retain actual collection skip evidence without inventing a selected case. An
explicit `file::node` selector must match a genuinely collected alias: coverage of its file
alone does not satisfy it. This applies to ordinary and scoped collection and to reused
scoped inventory.

### Frozen Git identity (development contract)

This is a deferred design, not an implemented property of the current candidate.
Under that future contract, `git="frozen-checkout-v1"` would add the verified frozen Git
context and its executable/runtime identity to only consuming case and group keys.
It adds the supported tracked tree to that scope; other cases retain their existing
inputs. Collection uses the union needed by its potential consumers, but execution
uses each case's own scope. A collection refresh caused by Git cannot by itself
invalidate a nonconsumer's result. Equal-scope batching includes Git context in equality;
Git and non-Git scopes cannot share a group. `cache=False` continues to disable reuse.

Freeze once per invocation and validate the relevant source and frozen snapshot before
accepting a hit, before execution and before publication. Changed HEAD, local refs, reachable history, tracked source,
link text or target content, executable or its relevant runtime changes the consuming
identity. Bounded capture, integrity and cleanup failures refuse or end in infrastructure
failure after bounded retries; they never create reusable success or trigger ordinary
fallback. Pending records become eligible only after owned cleanup succeeds; a terminal
cleanup failure suppresses all new pending records while preserving prior valid records.
See the [view and state limits](bubblewrap.md#frozen-git-checkout-development-contract).

## Frozen tool capture and reuse

Literal `tools` names compose through the same inheritance, default and bundle rules as
other scope inputs. Registration/defaults alone do not change an ordinary route. Only
tools needed by structurally possible consumers have their manifests and external
closure prepared. The [versioned manifest](bubblewrap-api.md#frozen-tool-manifest-development-contract)
lists exact supported bytes, types, source modes, link text and runtime relations.

Scoped discovery may receive the union of potential tools for its files, including a
compatible union of command maps, closed library trees and declared configuration.
If any selected runtime requires the native profile, discovery receives its closed
`/etc` view. No case body executes there.
Each case or identical-scope batch receives only its complete effective closure, before
imports and recollection. Missing execution dependencies cannot be borrowed from another
case. Tool names and identities participate in [batch equality](batching.md#exact-scope-and-deterministic-membership).
Conflicting aliases, configuration entries, loader mappings or command names refuse
the union; there is no arbitrary winner or relaxed execution view.

Consumer identity includes selected manifest bytes, automatically included lock files
and project-link relations, coverage mode, source/logical mappings, normalized entry
records, actual content/modes/link texts, executable and Python runtime relations,
loader/cache/search trees, closed configuration selection and command lookup. Exact
configuration bytes, modes, links, mappings and the fixed empty `0444` preload control
are consumer inputs; undeclared ambient `/etc` membership and metadata are not.
The collection identity uses its required union; case/group keys do not inherit unrelated
external bytes from that union. Updating
a valid manifest and its matching installation refreshes the affected consumer. A stale
pin refuses. An unrelated narrow case remains reusable when only another tool's external
contents or manifest changes; editing project configuration retains its existing broader
invalidation. A source mutation during an invocation is never accepted as a new context.

For example, structurally included files contain separate consumers of tools A and B.
Updating B's installation and matching manifest leaves A's case record reusable, while
refreshing collection and B's context. Changing B's bytes without updating its pins
instead refuses the required collection union, even with `-k` or a node selector that
selects only A. Those selectors do not remove B's file from structural discovery.
Previously valid A records survive that refusal; no new result is published. An API
`structural_files` boundary excluding B's separate file removes B from the required
union and permits A alone. Record reusability and admission of an invocation are
distinct: a mid-invocation source change also prevents pending publication.

Capture runs under the invocation's stable cache lock and owned staging. It performs
bounded no-follow entry reads, before/after source checks, a complete final source-record
pass and independent staged-content verification. It never hard-links mutable source
files. Immutable owned material may be shared between its views. Recheck relevant source,
manifest, lock and frozen records before accepting inventory/result hits, before execution,
and before new record publication. Corrupt or partial owned material is infrastructure
failure, not a valid snapshot. Old valid cache records survive unsuccessful new work.

| Frozen-tool bound | Maximum |
| --- | --- |
| Registered/referenced tools | 64 |
| Source anchors per tool | 32 |
| Entries / traversal visits across selected tools | 100,000 / 200,000 |
| Aggregate materialized owned paths, including every view | 200,000 |
| Normal owned-tool cleanup / abandoned recovery | 60 seconds each |
| Path components / UTF-8 path or link bytes / link hops | 64 / 4,096 / 32 |
| Individual file / bytes per tool / aggregate tool bytes | 512 MiB / 2 GiB / 4 GiB |
| Manifest encoding per tool / aggregate manifest bytes | 16 MiB / 32 MiB |
| Executable, library, resource and subprocess relations per tool | 4,096 |
| Declared installed Python distributions | 64 |
| Command names per tool / selected scope | 256 / 1,024 |
| Native loader search-directory declarations per tool | 64 |
| Retained native ordering proof state | 65,536 context/object/dependency/name/alias tuples |
| Native invoking-interpreter observed search paths / site paths | 64 / 64 |
| Selected native invoking-interpreter `pyvenv.cfg` | 64 KiB |

These tool-file bounds are separate from the project snapshot's source-file bound.
Native lookup also follows the [bounded process-context proof](bubblewrap-api.md#verified-native-library-ordering).
Its expansions and comparisons share the existing traversal, deadline and cancellation
budgets. Resource associations, search-tag order, effective targets and context name
mappings bind identity. Collection unions reprove these identities, including combined
resources of the same executable; shadowed files cannot change the winning dependency.
Hash/copy uses bounded chunks; a large admitted executable is not read into one buffer.
Declared system configuration counts toward these same entry, byte, path and traversal
bounds. Its owned readonly projection contains only selected pinned entries and the
fixed loader controls; no separate ambient-configuration inventory is captured. Source
descriptors remain controller-local and are not passed to mount setup or children.
Native view identity also includes the supported invoking-interpreter path description,
aliases, closed prefix destinations and startup flags. These
[startup closures](bubblewrap-api.md#invoking-python-startup-closure) can narrow `/usr`
and the actual virtual-environment prefix to selected entries; no host siblings remain
under those projections. Uncovered environment bind destinations refuse before reuse
or launch, rather than reopening the declared view.
Each capture attempt has the lesser of the configured session timeout and 120 seconds,
with cancellation checks during traversal and hashing. There are at most two attempts,
each with fresh exclusive staging. Bounds refuse explicitly without truncating entries
or installing a smaller/different executable.

Missing declared inputs or malformed declarations are declaration errors. Unsupported
versions/layouts, exceeded bounds and incompatible declared pins use `tool:frozen-v1`,
`tool:layout`, `tool:bounds` and `tool:identity` respectively. Observed source mutation,
I/O failure or corrupt owned capture gets bounded infrastructure recovery. Cleanup
uncertainty is terminal: stop new work, drain owned children, preserve existing records
and publish none of the invocation's pending successes. Remove only owned staging/views;
restore directory traversal/write permissions only on verified owned material after
views finish, never on source paths or symlink targets. A refusal does not authorize an
ordinary replay or prove earlier ordinary cases never ran.

Verification of restrictive owned directories uses read handles acquired before their
final frozen modes are applied; verification does not widen a live view's permissions.
These controller-only handles refer to owned copies and projections, never source
directories, and are not transported to children. Verification checks that each held
inode still corresponds to its declared owned path. Cleanup restores only the verified
owned inode and closes retained handles on every exit and before retry. Descriptor
exhaustion follows the bounded infrastructure-failure path, without changing caller limits.

Materialization reserves a complete bounded set of new owned paths before creating it.
The aggregate includes capture copies, tool and projection roots, empty ancestors,
closed-view members, command directories/symlinks and fixed loader controls. Count each
directory-entry path once; separate hardlink destinations count separately even when
they share an already-owned inode. Reusing an existing view adds no paths. A reservation
that would exceed 200,000 refuses with `tool:bounds` before accepting the view or
publishing results, without truncation or leaving new view members behind.

After a partial failure, retain the reservation until verified owned cleanup removes
its paths. A fresh capture attempt resets accounting only after complete cleanup.
Normal removal and abandoned recovery use the same capacity and a 60-second deadline,
counting each owned path once rather than counting a directory again when traversing
its children. Neither follows symlink targets nor changes source permissions. Cleanup
uncertainty is terminal and preserves previously valid records. These bounds govern
owned tool material, not caller CPU, memory, process or filesystem resource policy.

A late typed declaration/capability refusal preserves its type and capability while
retaining available session observations in the strict versioned
[refusal accounting](result-schema.md#retained-observations-after-a-late-refusal)
object. It is still a refusal, with no nested overall success verdict and no pending
publication. An early refusal has no accounting object; bounded unavailability is explicit.

Public evidence identifies tool names and failure categories, never external source
paths, file contents, secret values or raw tool stderr. The cache and worker protocol
retain opaque context identities, not raw tool-source inventories. The invoking runtime,
normal phase/lifecycle validation and output/session/cardinality limits remain in force.

## Bounds and performance

Bounded defaults: 100,000 actual cases; 100,000 declared paths; 200,000 visited entries while expanding declared directories and globs; 64 MiB per source file; 2 GiB total frozen project bytes; 4,096 bytes per node ID/path; 64 path components; 64 MiB inventory/result-summary encoding; 1 MiB individual case execution response; 64 MiB batch execution response/cache record; 4,096 selected members per batch. An oversized group refuses the entire plan before fresh group execution instead of splitting silently. Static parsing accepts at most 4 MiB, 200,000 AST nodes and depth 128 per Python source file, and in total 2 GiB of Python source, 268,435,456 (2^28) AST nodes and 100,000 literal marker calls, as described [below](#static-parse-budget). Bounded traversal counts nodes without first constructing an unbounded list; parser-depth and allocation failures become declaration errors. Reusable bundle count/depth limits remain separate from case cardinality.

Retain up to 100,000 successful execution records (singleton cases or atomic batches) within a 256 MiB total cache budget, plus inventory records within a 64 MiB aggregate inventory budget. The inventory record-count bound is `max(8, selected_suite_count)`, at most 64 records. Enforce both count and byte limits under the existing stable lock; scan/GC once per invocation and incrementally account new records. New records remain ineligible pending files until the complete invocation has cleaned successfully. A cleanup failure discards those pending records and preserves previously valid hits. Pending storage has separate bounded budgets of 256 MiB for cases and 64 MiB for inventory; abandoned pending files are recovered under the same stable lock. Eviction changes performance only. Bounded output and record parsing apply during I/O, not after unbounded capture. Limits produce explicit errors and never silently truncate inventory.

The controller freezes each distinct declared file once per invocation and reuses its
file record when computing case keys. It prepares a sandbox view only for cases that
need to run, using hard links to the frozen files without exposing the other frozen
inputs. This avoids copying and hashing the project for every case.

Each cache miss still starts a sandboxed pytest session: one per case by default, or
one per group in batch mode. Repeated collection of a large parametrized module can
therefore be expensive. Reusing an inventory or result starts no sandbox. Ordinary
sessions always run.

### Static parse budget

One parse budget covers every structurally selected test file and every declared Python
source that a scoped view could authenticate. A declared source is each `.py` input of the
union of scoped views that is not a frozen tool link, followed through leaf symlinks within
that union, at most 64 links, to a regular file. A link to a declared expected-absent path
has no source, and tool links are never followed. A chain that cycles, exceeds 64 links or
leaves the union is never parsed; a scoped view that selects it refuses the link.
Validating a helper never expands its own declarations into additional inputs.

The totals scale with the frozen project rather than with test files alone. The 2 GiB
source total equals the frozen project byte bound, and the node total allows one AST node
per eight bytes of that source. Installed library source measures about 0.108 nodes per
byte, so the source total binds first for ordinary code, at about 232 million nodes, and
the node total only for unusually dense syntax. The per-file, depth and marker-call limits
are unchanged.

Each distinct regular file is read and parsed at most once per invocation, and its bytes
and nodes count once. Literal marker calls count once per import origin: a leaf-symlink
alias authenticates its own records, with its own `file` and identity, so the marker total
also bounds the records that aliases multiply. An alias's records relabel the single parse
of its file and equal the records of parsing the same bytes under the alias name.
Relabeling costs nothing for a file without markers and about a tenth of a reparse for a
file with 2,000 marker calls, so counting an alias's bytes and nodes again would refuse
projects without bounding any additional work.

`plan` and `run` apply the same budget in the same static pass, before any sandbox is
prepared or any case runs. An over-budget project refuses with a declaration error that
names the exceeded total. A syntax error, a per-file size, node or depth violation, or an
unsupported literal marker call in a test file or declared source refuses with a
declaration error that names the cause and the file. The run then freezes the project and
verifies each declared source's frozen bytes against the hash that static discovery
parsed; a source changed in between refuses as an infrastructure error. Authentication of
the frozen source relabels those parses per import origin and never parses again.

Parsing is single-threaded. Measured with Python 3.12 at the node total, installed library
source repeated to 268 million nodes (about 199,000 files and 2.48 GB, beyond the source
total) took 404 s at 0.66 million nodes per second with a 110 MB peak resident set. Syntax
at the per-file node bound (1,343 files of 199,986 nodes in 200 KB each, 268 million nodes)
took 570 s at 0.47 million nodes per second with a 159 MB peak. Memory is bounded per
file: each parse tree is released before the next file and only literal marker rows are
retained. A project near the totals therefore spends several minutes in static discovery
for each plan or run; the cost scales with declared Python source, not with the number of
cases.

## Project configuration

Run from the directory containing `pyproject.toml`. The dedicated CLI does not import
project Python before its sessions start:

```sh
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

[tool.pytest_telescope.execution]
mode = "case"
workers = 2
```

Paths are project-relative. Inputs accept explicit files, directories and globs.
Missing declared inputs, unknown fields/bundles, cyclic bundle references, malformed
values and paths escaping the project are errors. Expected-absent paths must be
absent in the source project and participate in identity. Relative leaf symlinks
require their destination in the effective scope; other links require the exact
selected frozen-tool binding described above. Explicit hidden glob components are supported.

The default suite keeps pytest configuration in this pyproject's `[tool.pytest.ini_options]`.
[Independent suite declarations](scoped-execution.md) also support explicit
pytest.ini, .pytest.ini, tox.ini and setup.cfg files. Discovery honors configured `python_files`
and `test_roots` (default `["."]`). Bounds fail explicitly; discovery never silently
truncates matching files. Only effective `env` names are forwarded into each scoped execution and keyed. Global
names apply to all scoped cases; literal module, class, function and parameter names add
to defaults and bundles for their consumers. `env=[]` scopes a case; defaults alone do
not. Capture declared names once before sessions, distinguishing absent, present-empty
and present-valued states. Discovery keys and receives the potential collection union;
execution recollects in the narrower actual scope and checks the complete file inventory,
origins and order. Put collection dependencies in every consumer's shared declarations.
An unrelated case can reuse its result after a collection refresh. Controlled
Python/pytest variables, fixed sandbox settings and `TELESCOPE_*` protocol names are
reserved across all declaration routes, regardless of their invoking values; see the
[reserved-name rules](specification.md#project-configuration). Raw environment
values occur transiently in controller memory and actual Bubblewrap arguments/environment,
not worker request JSON, cache envelopes, public summaries or ordinary diagnostic logs.
Plan may display names, not values. Ordinary sessions inherit the process environment.

## Outcome accounting

The [result schema](result-schema.md) defines versioned complete/incomplete inventory
accounting, routes, opaque case identities and explicit unsupported-capability refusals.

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
Pytest display options affect its sessions; the controller emits its structured summary
and bounded failure evidence, not arbitrary pytest output.

- `run --force`: execute scoped cases despite an eligible successful record.
- `run --no-cache`: execute without reading or writing successful result records.
- `run --explain`: explain each case's route and execution or reuse decision.
- `run --cache-dir PATH`: select the result-record directory.
- `run --timeout SECONDS`: bound the wall time of each sandboxed session (scoped
  collection, case or batch); default 60 seconds, allowed range 0.1 through 3,600
  seconds. Ordinary suite sessions have no pytest-telescope deadline, like normal
  pytest; the job that runs pytest bounds them.
- `run --workers N`: run up to N independent scoped sandboxes at once; default 1.
- Marker `cache=False`: always execute the case and never record a reusable
  success, regardless of other cases' cache settings.

Declaring files is not enough for a test that also depends on the clock, uncontrolled
randomness, native global state, network services or host tools. Make those dependencies
reproducible, set `cache=False`, or leave the test ordinary. Scoped tests share the host
network, and the cache cannot detect changes to external state.

**Exceptions to cache reuse — not location-blind.** Identical checkouts at different
paths share scoped results, so a scoped test sees its project at `/project`, wherever the
checkout lives.
Two things a scoped test can read still name its checkout's location, which its cache
key does not contain:

- the source directories in the mount table (field 4 of `/proc/*/mountinfo`);
- the raw bytes of cached bytecode files (`__pycache__/*.pyc`) of an environment inside
  the project.

Leave a test that reads either one ordinary, or add `cache=False` to its scope.
See [what a scoped test can still see](bubblewrap.md#what-a-scoped-test-can-still-see).

## Failures and troubleshooting

A declared file that is missing or a declared-absent path that exists fails validation.
Fix the declaration or the source state, then rerun. For a collection or import failure
in a scoped case, inspect the reported project file and its imports, then add the actual
missing dependency as a narrow file, directory, or bundle. For a `FileNotFoundError`,
inspect the reported file/line and its resource dependencies. Verify shared conftest and
package `__init__.py` files as well as the obvious application module.

Helper source must agree with its frozen file record before its literal calls can be
authenticated. Missing, unreadable, changed or corrupt frozen source and unsupported
record shapes refuse; the controller does not substitute live checkout bytes. Source
and parsing failures use the same bounded refusal, retry and cleanup rules as other
scoped work. Declaration errors never trigger an ordinary replay, and cleanup failure
cannot publish new successful records.

New test files and cases are included in the refreshed real inventory. A collection
mismatch or unsupported declaration cannot be resolved by accepting an old record.
A changed selector has a different identity and cannot reuse the unselected run's result.

A failed test remains a failed execution and never becomes a reusable success. The
interpreter probe and Bubblewrap preflight get at most two attempts for transient
failures; a missing `bwrap` or a namespace denial refuses immediately. A scoped collection,
case or batch gets at most two execution attempts for infrastructure failures; an
ordinary session gets one and has no pytest-telescope deadline. Cache lock acquisition
is bounded by twice the configured timeout plus 30 seconds; a concurrent invocation that
waits behind a long ordinary session can therefore end with an infrastructure outcome. Startup, deadline, output, storage, invalid-result, or cleanup
failure that exhausts recovery ends with an infrastructure outcome. Use `--force` to
rerun a case when you suspect its cached result. Removing a damaged record causes a
rerun; the case must pass again before its result can be reused.

Each unit can report up to 64 failure entries containing a project-relative file, line
number, phase (`collect`, `setup`, `call`, or `teardown`), exception type, and hashed
case identity. Collection failures use `CollectionError` and may have line zero.
Locations outside the project are labeled `<runtime>`. A location in a project file is
project-relative however the file was imported, including a project module loaded
through an editable install. Exception messages, source
snippets, parameter values, stdout/stderr contents, and environment values are not
included in this evidence. Frozen project copies live in a per-invocation directory
under `.pytest-telescope/scratch`, locked while its owner runs; a later invocation
removes directories whose owner has exited. The pytest adapter may load other host
plugins before routing to the controller; use `python -m pytest_telescope` to avoid host
pytest/plugin startup entirely.

A scoped worker is PID 1 of its sandbox. Completed adopted children are reaped after all
pytest teardown with nonblocking `waitpid`, bounded to 4096 calls and 50 ms. Every
process still present afterward, including any live descendant or unconsumed zombie
after the bound, makes the result ineligible; the remaining processes end with the
sandbox. The engine never waits for ongoing child work to turn an incomplete run into a
reusable success.

## Required fixture conservation (unreleased)

The optional [fixture guard](batching.md#required-fixture-guard-unreleased) is implemented
in the unreleased candidate; published 0.1.0a6 does not provide it. It validates all available user
fixture definitions and actual package-collector xunit lifetimes in each exact group view
before selected bodies and conservatively refuses shared lifetimes split across groups
without broadening declared inputs. Mutable
framework fixtures retain their documented per-group lifetime. The discovery union is
not an execution view, and collection hooks can have side effects. Execution-side drift
preserves actual prior attempts and prevents new cache publication. Unguarded behavior
and schema 4 remain unchanged; unguarded records use cache envelope 8. A body-free
fixture capture binds provisional context and membership; its registry and
context-independent content commitments enter
the final group and cache identity before context-bound inspection and execution
revalidation. A guarded run with incomplete pre-admission observations can report guard
state `unavailable` without inferring whether any bodies ran.
