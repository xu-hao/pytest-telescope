# Declarations, inventory and reuse

## Status

This contract covers literal declarations, real pytest inventory, result reuse and
outcome accounting. The [ordinary and Bubblewrap execution contract](bubblewrap.md) owns
routing and the sandbox's filesystem view.

## User interface and scope

In the default `case` mode, every actual scoped pytest item is one independent execution
and successful-result cache unit, including every parametrized variant. There is no
central per-case registry. A new test file or generated case enters the current inventory
automatically; it cannot disappear behind an old result.

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
`bundles` or `distributions` field makes a case scoped, including an empty list. Its
effective scope is the union of those inherited declarations, project defaults,
containing test source and configuration source. A policy-only `cache=False` marker or
absence assertion does not make a case scoped. Any inherited `cache=False` disables
successful-result reuse for that item.

A case without such a field is ordinary: it runs in an ordinary pytest session on every
invocation and is never cached. pytest-telescope does not snapshot, walk or hash
undeclared project files for it.

Declarations cannot name version-control metadata (`.git`, `.hg`, `.svn`), telescope
state (`.pytest-telescope` and an explicitly selected in-project cache directory),
pytest/bytecode caches (`.pytest_cache`, `__pycache__`), or host Python environments
(`.venv`, `venv`, or a directory with a validated `pyvenv.cfg`). Directory symlinks,
special files and unsupported paths fail boundedly rather than being followed or omitted.

Pyproject contains `[tool.pytest_telescope]` test roots/environment settings, a `[tool.pytest_telescope.defaults]` table for shared inputs/bundles/absence/cache policy, and reusable `[tool.pytest_telescope.bundles.NAME]` tables. Defaults add common support to scoped cases; they do not make unmarked cases scoped. Conftest, application modules, package initializers, helpers and collection resources must be visible in a scoped case's effective scope.

The default `case` mode starts a fresh sandbox and pytest session per scoped case and recreates module/class/session fixtures. The optional [batch profile](batching.md) shares ordinary pytest fixture lifetime and private scratch only within one suite/module and an exactly equal expanded scope. Groups never union different readable inputs. Complete group evidence is reused atomically, and every group starts a fresh sandbox. The project files remain read-only in both modes.

## Static discovery and real pytest agreement

The controller parses bounded test source syntax without importing or evaluating project Python. Supported spellings use `pytest.mark.telescope(...)` directly in function/class decorators, module/class `pytestmark`, or `pytest.param(..., marks=...)` inside literal parametrization declarations. Values are literal string lists and booleans; bundles provide reusable declarations. Arbitrary expressions, computed kwargs, declaration factory functions, aliases and generated telescope markers are unsupported. Ordinary parametrization values and pytest-generated item identities still come from real pytest collection, not an AST approximation.

A pre-import guard in every pytest session validates each supported marker factory call against its exact frozen source position and literal arguments. Collected telescope marks must have registered origins and unchanged values, compared with an independent immutable canonical encoding of the static declaration. Mutable lists passed to pytest are never the validation baseline. Hidden dynamic marker construction that cannot be diagnosed statically fails the session; it never widens a scope or changes a route. Private pytest internals are not assumed stable.

The guard identifies a declaration by the calling module's current `__file__` within the
project. Cached assertion-rewrite bytecode can keep an old checkout filename; the guard
refuses missing, invalid or outside-project module origins instead of falling back to
that embedded filename.

A file whose module-level `pytestmark` carries a scope is collected in a sandbox whose view is the union of the declarations in such files of the suite, project defaults and configuration. Real collected provenance must confirm that every item carries the module scope, or collection fails without a broader retry. This collection view is never used to run a case body. Every other test file is collected by the suite's ordinary session, which also identifies the scoped cases of mixed files. Real pytest enumerates dynamic and parametrized items and maps inherited literal origins to effective declarations. Hooks that add, remove or change items after the validated collection checkpoint are rejected. Duplicate/invalid node identities are errors.

Each scoped execution begins a new sandbox with only that case's effective scope, before conftest/test imports. It collects its containing file, verifies the actual item/declaration inventory against discovery, and executes exactly the selected item in case mode. Batch execution instead validates the complete containing-module inventory and the requested ordered subset before executing that equal-scope group. A scoped execution missing a shared collection dependency fails with bounded collection evidence; it does not borrow another case's inputs. Common import/parametrization dependencies belong in shared declarations. In a mixed file, the whole file, including its ordinary cases, must collect identically inside each scoped case's view.

## Inventory, selection and reuse

Inventory and execution have distinct strict result schemas. A successful sandboxed collection reports collected nodes and declaration assignments, never executed passes. It becomes reusable only after the sandbox exits cleanly with validated bounded records. Ordinary session inventories are never reused.

The scoped inventory key covers the exact frozen collection view, current test-file membership, static declaration registry, pytest configuration, relevant environment, full effective arguments/selectors, the sandbox identity (policy, interpreter, installed distributions and the environment's references into the project) and runner implementation/version. Every invocation re-freezes current declared inputs before accepting an inventory hit. File addition/deletion, declared-resource changes or changed dynamic-collection inputs invalidate it. `--force` and `--no-cache` refresh inventory. Any statically present scoped cache=False policy conservatively disables inventory reuse; unrelated cacheable case results may still be reused after fresh collection.

Result keys cover each scoped case's actual node identity, effective literal declarations, exact executed snapshot, validated containing-file inventory/declaration digest, full options/selectors and the sandbox, environment and runner identities. They do not include an unrelated case's resource bytes merely because collection saw them. Thus changing one declared resource refreshes collection but preserves unrelated case result hits. Failures, interrupted/invalid records and incomplete cleanup never become hits.

Full current inventory is accounted for before selectors: selected cases are executed/reused/failed; other cases are explicitly unselected. `-k`, `-m` and node selectors use actual pytest semantics. Summaries report collection separately, actual case counts by route, fresh versus cached pass/skip outcomes, and no invented file-target granularity. Cache hit acceptance cannot hide new files/cases. Plan remains a nonexecuting static preview and explicitly does not claim real collected-case enumeration.

## Bounds and performance

Bounded defaults: 100,000 actual cases; 100,000 declared paths; 200,000 visited entries while expanding declared directories and globs; 64 MiB per source file; 2 GiB total frozen project bytes; 4,096 bytes per node ID/path; 64 path components; 64 MiB inventory/result-summary encoding; 1 MiB individual case execution response; 64 MiB batch execution response/cache record; 4,096 selected members per batch. An oversized group refuses the entire plan before fresh group execution instead of splitting silently. Static parsing accepts at most 4 MiB per Python source file and 64 MiB of Python source in total, 200,000 AST nodes per file and 2,000,000 total, depth 128, and 100,000 literal marker calls. Bounded traversal counts nodes without first constructing an unbounded list; parser-depth and allocation failures become declaration errors. Reusable bundle count/depth limits remain separate from case cardinality.

Retain up to 100,000 successful execution records (singleton cases or atomic batches) within a 256 MiB total cache budget, plus inventory records within a 64 MiB aggregate inventory budget. The inventory record-count bound is `max(8, selected_suite_count)`, at most 64 records. Enforce both count and byte limits under the existing stable lock; scan/GC once per invocation and incrementally account new records. New records remain ineligible pending files until the complete invocation has cleaned successfully. A cleanup failure discards those pending records and preserves previously valid hits. Pending storage has separate bounded budgets of 256 MiB for cases and 64 MiB for inventory; abandoned pending files are recovered under the same stable lock. Eviction changes performance only. Bounded output and record parsing apply during I/O, not after unbounded capture. Limits produce explicit errors and never silently truncate inventory.

Freeze each distinct declared file once per invocation. Derive case keys from immutable frozen-file records and memoized scope identities. Materialize only missed scoped cases, using hard links to immutable frozen files without exposing the broader frozen pool. Do not copy/hash the project once per case. Each scoped miss starts one sandboxed pytest session per case by default, or per exact-scope group in batch mode; repeated collection of a large parametrized module is a real cost. Warm inventory/result hits start no sandbox. Ordinary sessions always run.

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
require their destination in the effective scope; absolute links and directory
symlinks are unsupported. Explicit hidden glob components are supported.

The default suite keeps pytest configuration in this pyproject's `[tool.pytest.ini_options]`.
[Independent suite declarations](scoped-execution.md) also support explicit
pytest.ini, .pytest.ini, tox.ini and setup.cfg files. Discovery honors configured `python_files`
and `test_roots` (default `["."]`). Bounds fail explicitly; discovery never silently
truncates matching files. Only named `env` variables are forwarded into scoped sandboxes
and keyed, including presence. Controlled Python/pytest variables and `TELESCOPE_*`
protocol names are reserved. Diagnostics never print environment values.

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

Filesystem inputs do not prove universal determinism. Tests that depend on clocks,
uncontrolled randomness, native global state, network services, host tools, or undeclared
environmental assumptions must be made reproducible, marked non-cacheable, or left
ordinary. Scoped tests share the host network. Cached success is meaningful only under
the documented input and environment assumptions.

## Failures and troubleshooting

A declared file that is missing or a declared-absent path that exists fails validation.
Fix the declaration or the source state, then rerun. For a collection or import failure
in a scoped case, inspect the reported project file and its imports, then add the actual
missing dependency as a narrow file, directory, or bundle. For a `FileNotFoundError`,
inspect the reported file/line and its resource dependencies. Verify shared conftest and
package `__init__.py` files as well as the obvious application module.

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
failure that exhausts recovery ends with a distinct infrastructure outcome. Retrying
forever and silently removing restrictions are not recovery strategies. Force execution
can diagnose a suspected record without relying on it; removing damaged records should
affect reuse only, never turn failing work into a success.

Each unit can report up to 64 failure entries containing a project-relative file, line
number, phase (`collect`, `setup`, `call`, or `teardown`), exception type, and hashed
case identity. Collection failures use `CollectionError` and may have line zero.
Locations outside the project are labeled `<runtime>`. Exception messages, source
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
