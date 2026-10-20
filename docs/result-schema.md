# Run results and explicit refusals

This reference describes accepted replacement **schema 3**, currently **UNQUALIFIED**.
The [exact API/runtime seam](bubblewrap-api.md) and
[complete JSON examples](bubblewrap-schema-examples.json) define its concrete shapes.
The examples are contract fixtures, not execution receipts. Schema 2 records remain
ineligible; published artifacts retain their historical format. Start with the practical
[quickstart](index.md). The [scoped execution contract](scoped-execution.md)
defines metadata, suite and resource boundaries.

## Invocation interfaces

The dedicated CLI emits one JSON result. Ordinary collection imports project Python
in the invoking environment; restricted discovery independently supplies scoped authority:

```sh
python -m pytest_telescope run --suite core --explain -- -k selected
python -m pytest_telescope run --budget-cpus 2 --budget-memory-bytes 1073741824 --budget-processes 256 --budget-scratch-bytes 134217728
```

Repeat `--suite` to select several configured suites. An omitted suite selection runs all
configured suites. A CLI budget override supplies every dimension; partial, noninteger,
nonpositive envelopes are invalid. Potential scoped work also requires an envelope that
fits a worker before restricted collection/cache acceptance. All-ordinary execution
validates configuration syntax but does not enforce or fit-check a scoped envelope.

`--execution-mode case` or `--execution-mode batch` overrides the configured mode without
rewriting project configuration. The default is `case`; [batch mode](batching.md) shares
fixtures only within one suite/module and an exactly equal expanded scope. `plan` reports
`execution.mode` and `execution.profile`, but cannot predict real group membership.

The equivalent Python entry point is `pytest_telescope.controller.run(root, *, force=False,
no_cache=False, explain=False, cache_dir=None, timeout=60, arguments=(), budget=None,
suites=None, execution_mode=None, structural_files=None, runtime_profile=None,
required_capabilities=None, python_profile=None, source_roots=None, backend=None,
controller_resources=None)`. `root` identifies the outer project directory. `budget` is a dictionary of
`cpus`, `memory_bytes`, `processes`, and `scratch_bytes`; `suites` is a list of configured
names. Arguments and node-selector paths remain relative to the outer project, including
when a suite uses another working directory.

`structural_files` optionally maps selected suite names to exact lists of project-relative
test files for this invocation. It narrows membership before marker parsing and collection
imports; ordinary node selectors do not. Unspecified suites retain configured membership,
`{}` has no effect, and an explicit `[]` gives that suite an empty inventory. Lists are
sorted, must contain no duplicates, and are limited to 4,096 existing regular Python test
files each. Files must satisfy the suite's roots and patterns, with canonical paths and
no symlinks. Unknown or unselected suite keys are errors. See the
[structural narrowing contract](scoped-execution.md#invocation-only-structural-file-narrowing).
The static Python preview `pytest_telescope.plan.make_plan(root, suites=None,
structural_files=None, runtime_profile=None, required_capabilities=None,
python_profile=None, source_roots=None, backend=None, execution_mode=None, budget=None,
controller_resources=None)` applies the same validation without importing tests or contacting
a backend. There is no corresponding CLI structural-file override flag.

The Python API raises `DeclarationError` for invalid declarations and
`UnsupportedCapability` for unqualified capabilities. `UnsupportedCapability` subclasses
`DeclarationError` and exposes `capability`. Infrastructure failures during an established
run are accounted in its result where possible. `controller.error_result(error)` converts a
typed declaration, capability or infrastructure exception into the refusal envelope below.
Consumers should dispatch on the explicit status and capability, without parsing messages.

### Runtime profiles

The replacement proposes `python-v2` and `git-local-v2`, selected separately from
`execution_mode` and the backend. The old `python-v1`/`git-local-v1` Docker profiles
are not silently reinterpreted. Both new runtimes and the retained Git/isolated-child
capabilities require production qualification.

`runtime.build_runtime(root, requirements=None, *, runtime_profile=None, interpreter=None,
bubblewrap=None, controller_resources=None)` explicitly prepares a frozen file closure.
The selected interpreter is an absolute CPython executable (default `sys.executable`),
and Bubblewrap defaults to `/usr/bin/bwrap`; no opportunistic PATH tools are selected.
CLI `runtime` adds `--interpreter`, `--bubblewrap` and the four controller resource flags.
`plan`/`run` add `--backend` and those resource flags. Existing `--runtime-profile` and
repeated `--require-capability` remain. `backend` accepts `bubblewrap-v1`, with omission
using configuration; `docker-v1` is not fallback.

Configured and caller requirements form a sorted unique union; callers cannot remove
configured requirements. Each list remains bounded to 16 strings. `runtime:profile`
means unknown profile/version; `runtime:capability` means unknown/unmet capability.
`backend:profile`, `backend:userns`, `backend:resources`, `backend:provenance`,
`runtime:layout` and `runtime:abi` identify the respective unsupported boundaries.
Missing/corrupt prepared state is infrastructure failure. Invalid scoped declarations
never become ordinary cases.

`runtime.inspect_runtime(root, *, runtime_profile=None, required_capabilities=None,
backend=None)` validates prepared files and read-only backend compatibility with at most
two retryable attempts. It never prepares, downloads, repairs, collects, accesses case
cache or writes state. It returns exactly `{profile, required_capabilities, identity}`,
with an opaque validated runtime digest and no executable paths. Identity is not
execution/cache authority. Static plans and unresolved preparation use null identity.

Run/plan `execution` retains exactly `mode` and `profile` (`case-v1` or `batch-v1`),
describing scoped policy. A separate backend object is exactly
`{name: "bubblewrap", profile: "bubblewrap-v1", identity: null|digest}`. Backend identity
is null until supported entry-context preflight plus provenance/resource validation.
It binds launcher closure, versioned resource/lifecycle mechanism and supported observed
host attachment, granting no cache authority by itself. There is no `execution.backend`.
Runtime retains exactly its three fields, separate from backend and Python lookup.

### Isolated child source lookup

The [isolated-source contract](isolated-source.md) adds `python_profile=None` and
`source_roots=None` to `controller.run` and `plan.make_plan`. CLI `plan` and `run` accept
`--python-profile` and repeated `--source-root`. These are lookup settings, separate from
the immutable runtime profile and case/batch execution mode. They grant no source files.

Project settings live in `[tool.pytest_telescope.python]`. A suite's `python` table
completely replaces them and must contain both `profile` and `source_roots`. Caller
fields replace the corresponding effective field after inheritance across selected
suites. Omitted caller fields preserve inherited settings; API `source_roots=[]`
explicitly clears roots. Supplying roots alone does not enable the isolated profile.
Roots preserve order, are unique, and have a 64-directory bound. Invalid paths or
incomplete configuration raise `DeclarationError`; unknown profiles use
`python:profile`, and unsupported visible startup layouts use `python:source-layout`.

The result's separate `python` object contains exactly one `suites` list in selected
suite order. Every row has `suite`, `profile`, ordered `source_roots`, and `identity`.
Identity is null in static plans and before validated projection preparation; afterward
it is an opaque SHA-256 digest of the versioned mechanism and that suite's effective
settings. It does not assert equal source or metadata between individual views: actual
projection bytes and each selected scope also bind case/group keys. The three-field
`runtime` object and `execution.profile` retain their existing meanings.

Static plan suite entries also expose `python={profile, source_roots}` as part of their
configuration identity, including for default suites. An enabled lookup profile does
not claim that every configured root exists inside every narrow worker view. Python
ignores roots absent from that view; they do not acquire files from another case.

## Complete and incomplete run inventories

A run result has `schema: 3` and `kind: "run"`. These fields distinguish it from a refusal.

| Field | Meaning |
| --- | --- |
| `status` | `passed`, `failed`, `no_tests`, `infrastructure_error`, `unsupported`, or `declaration_error`. |
| `inventory_complete` | Whether every selected suite produced a validated real inventory within its effective structural file boundary. |
| `inventory_digest` | Digest of current suite/case/file membership when complete; otherwise `null`. |
| `suites` | `{name, files}` rows for exact effective structural boundaries before imports. |
| `routing_complete` | Every known route resolved and every effective structural partition accounted. |
| `backend`, `runtime` | Separate validated identities, nullable until their validation completes. |
| `route_counts` | Disjoint ordinary/scoped/unresolved partitions of the seven count fields. |
| `execution` | Effective `mode` (`case` or `batch`) and versioned `profile` (`case-v1` or `batch-v1`). |
| `grouping_complete` | All suite inventories and group-size preflight are validated; selected cases then belong to exactly one planned group. |
| `groups` | Ordered execution/cache groups with atomic member outcomes and worker accounting. |
| `resources` | Separate scoped/controller configured and observed values, plus inherited-host ordinary session accounting. |
| `collection` | Collection outcome, counts, timings, bounded failure evidence and suite inventories. |
| `units` | One row for each known real case, in deterministic suite/node order. |
| `counts` | Exactly `executed`, `reused`, `failed`, `unsupported`, `infrastructure_error`, `not-selected`, and `selected`. |
| `cases` | Unique validated collected identities, plus observed selected passed/failed/skipped totals. |
| `case_outcomes` | Separate fresh/cached passed and skipped totals. |

Collection observations have unique identity `[name, route]`, with `files`, `outcome`,
`complete`, nullable `collected`, `inventory_digest`, `accounting_group_id`, timings and
bounded failures. A mixed file occurs in both ordinary and scoped observations; their
file lists and item counts do not partition. `collection.collected` counts unique
validated identities, never the sum of those observations. Top `suites` rows expose the
structural files even before collection. A planned observation has `outcome: "not-started"`,
`collected: null` and null identity/timings. Files outside an API structural override
produce neither units nor unselected counts. Complete inventory covers only that boundary.

Collection aggregate outcome is `reused` only if all required observations are valid
reused records, or `executed` if complete observations include fresh collection.
Partial/failed observations retain failed/infrastructure_error/not-started evidence.
Ordinary collection is always fresh. Its timing belongs to its one suite session group,
referenced by `accounting_group_id`; collection worker/CPU timings are null to prevent
double counting. Scoped collection has its independent accounting.

Each unit has `suite`, `file`, opaque `case_id` (also `name`), `selected`, `route`,
`outcome`, attempts, counts, nullable timings and bounded failure evidence. Only a
validated opaque identity AND actual selected/not-selected observation make a case
reportable. Never invent a selection flag for raw, unresolved collection. Once reportable,
a case whose routing fails can have `route: null`; resolved cases use `ordinary` or
`scoped`. Unknown/uncollected cases produce no fabricated rows.

The count keys are exactly `executed`, `reused`, `failed`, `unsupported`,
`infrastructure_error`, `not-selected` and `selected`. `route_counts` has exactly
`ordinary`, `scoped`, `unresolved`, each with those seven keys. For each key, global
counts equal the sum of the partitions. Each known unit contributes one outcome and
also selected if true:

```text
counts.selected + counts.not-selected == len(units)
executed + reused + failed + unsupported + infrastructure_error + not-selected == len(units)
```

`reused` can occur only on the scoped route. Routing completeness is separate from
inventory completeness; it also accounts for all effective structural partitions.
An empty complete inventory satisfies both. Cleanup failure preserves known rows while
preventing new cache publication. Membership digests bind route/profile versions;
cache records additionally bind backend/runtime, actual limits and complete scoped context.

Raw parametrized node IDs stay inside their collecting sessions. A `case_id` is the lowercase SHA-256
hex digest of UTF-8 canonical JSON `[suite_name, project_relative_pytest_node_id]`, with
no spaces between JSON separators and standard JSON string escaping. The same file in
two suites produces different identities. Selector aliases in collection records are
hashed too. Collected node IDs and aliases are not persisted in cached inventories,
summaries, or execution requests; callers can still pass ordinary node selectors.
Explicit caller-supplied `-k` and `-m` expressions necessarily reach the isolated worker
in request arguments. Those expressions are not redacted and may themselves contain
a parameter label; consumers must avoid echoing them into public receipts. File paths and suite names are public
project structure and should not themselves contain private values.

`inventory_digest` describes membership, not test success. Consumers must also inspect
`inventory_complete`, selection and outcomes. A reused pass is previous evidence accepted
under the current identity, not a newly executed case.

## Atomic groups and member outcomes

Schema 3 applies to both routes and scoped execution modes. When `grouping_complete` is true, every selected known case belongs to exactly one group.
Unselected cases have `group_id: null`; selected known cases can also have null group IDs
when an earlier inventory or group-size refusal prevents completed planning. Scoped groups appear in deterministic suite/module/
first-selected-member order; ordinary groups represent actual suite sessions. `case_ids` retains actual selected pytest order after
collection hooks. Hash sorting does not define execution order.

| Group field | Meaning |
| --- | --- |
| `group_id` | Opaque lowercase SHA-256 identity binding profile, ordered members and complete execution context. |
| `suite`, `files`, `case_ids` | Suite, sorted unique structural files and selected opaque members in actual pytest order. Scoped groups have one file; ordinary groups may span files. |
| `route`, `profile` | `ordinary`/`ordinary-v1`, or `scoped` with `case-v1`/`batch-v1`. Group routes are never unknown. |
| `outcome` | `executed`, `reused`, `failed`, `incomplete`, or `infrastructure_error`. |
| `complete` | Every requested member has a validated final teardown outcome. |
| `cache_eligible` | Complete successful evidence accepted after lifecycle cleanup with reuse enabled. Always false for ordinary; false for scoped `--no-cache` and `cache=False`. |
| `attempts` | Group worker attempts; zero for reuse or before a worker starts. |
| `attempt_seconds` | Controller wall time across group attempts, including retries. |
| `worker_seconds`, `cpu_seconds` | Final valid attempt worker measurements, counted once per group; zero on reuse, null when unavailable. |
| `timing_complete` | False when retries or unavailable measurements prevent a complete worker-timing account. |
| `passed`, `failed`, `skipped`, `not_run`, `unresolved` | Member totals: known unrun members are distinct from attempted members with no valid result. |
| `stop_reason` | `null` for complete groups; otherwise `pytest-stop`, `collection`, `infrastructure`, or `cancelled`. |
| `failures` | Bounded phase/location/type/opaque-ID evidence, including execution-side collection failures that do not belong to a requested member. |

Each case row adds `group_id`, `result_status`, and `complete`. Result status is `passed`,
`failed`, `skipped`, `not-run`, or `null` for unselected/unresolved infrastructure cases.
Case `attempts` repeats its group count: summing case attempts does not count workers.
Every ordinary case row and every scoped batch-mode case row, including unselected and unresolved members, has
`worker_seconds` and `cpu_seconds` set to `null`, because shared fixture
costs cannot be attributed honestly to individual members. Scoped case mode retains singleton
measurements. Ordinary attempts become one after session launch and are never
automatically replayed. An ordinary collection-only session still has a group with
empty `case_ids` and its collected files; no empty scoped execution group is invented.

A reused group reuses every member together, preserving passed/skipped statuses. Any
missing, corrupt, partial or unsuccessful group evidence reruns its entire selected
membership. Ordinary results never satisfy scoped records. Case and batch profiles cannot reuse each other's records, even for a
singleton. Group cache eligibility differs from observed test success.

With `-x` or `--maxfail`, observed passes and skips remain case `outcome: "executed"`;
observed failures are `failed`; unrun requested members are `infrastructure_error` with
`result_status: "not-run"`. The group is `incomplete` with `stop_reason: "pytest-stop"`,
and the run status is `failed`, not an infrastructure outage. Shared setup failures
belong to each attempted affected member; shared teardown failure belongs to the member
whose teardown pytest reports. No member of a failed/incomplete group becomes reusable.
Skips are counted without retaining reason text or arbitrary captured output.

In the default case profile, a valid nonzero pytest result retains case `outcome:
"failed"`, including an execution-side collection failure before the test body starts.
Such a member has `result_status: "not-run"`, zero passed/failed/skipped test reports and
`complete: false`; this fails the verdict without inventing a call or teardown. The
singleton case row preserves its collection failure evidence. Batch members without a
call after collection failure retain `infrastructure_error`/`not-run` accounting, with
the run verdict `failed` and group `stop_reason: "collection"`. Group `failures` retains
collection evidence in both profiles even when its opaque collection ID is not a member.
No such group or member is cache eligible. A nonzero pytest session verdict also fails
the group and run even when every observed member passed, for example when a plugin
changes session exit status. Member counts retain those actual passes; neither profile
may turn the session failure into reusable success.

An attempted worker with no valid result reports `unresolved` members, `not_run: 0`,
and null member `result_status`: those members may already have executed. Never-submitted
groups have known `not_run` members with `result_status: "not-run"`. Valid final worker
results report `unresolved: 0`. Do not use unavailable results as evidence of no execution.

Each batch is limited to 4,096 selected members. An oversized group refuses the complete
plan with capability `execution:batch-size` before any fresh group starts; it is not
silently split. Batch structured results and cache records are limited to 64 MiB, compared
with 1 MiB for an individual case result. The merged worker stream has the same
64 MiB batch/collection and 1 MiB case limits, including structured protocol output and
direct file-descriptor writes. Ordinary Python/pytest output is normally suppressed;
there is no separate smaller arbitrary-output channel. The 100,000-case invocation bound and aggregate
cache/output bounds continue to apply. See the [batching contract](batching.md) for exact
fixture, scope, lifecycle and identity rules.

## Resources and honest timing

`resources.scoped` contains `required`, `validated`, `configured` (worker/budget),
nullable `concurrency` and nullable `enforced`. Potential scoped work anywhere in the
effective structural boundary requires restricted preflight, even if its cases are
unselected. Only a true all-ordinary invocation sets required=false, validated=false,
concurrency=null and enforced=null without contacting Bubblewrap/systemd or checking
whether the configured budget fits a worker. Configuration syntax is still validated.

`resources.controller` separates configured headroom from nullable enforced values.
`resources.ordinary` is `{enforcement: "inherited-host", slot_limit: 1, peak_sessions: N}`
with actual peak sessions. The scoped slice excludes the host caller and ordinary
sessions; the caller admits those under its own host policy. Worker budget retains its
aggregate-worker meaning; controller headroom is additional, not subtracted from it.
Enforced values come only from actual cgroup/tmpfs observations. The
[complete examples](bubblewrap-schema-examples.json) show exact nested shapes.

Unmeasured times are null, not zero. Ordinary group wall time covers collection,
execution and teardown once; its CPU is null and timing_complete=false until a
separately qualified complete measurement exists. Scoped CPU comes once from a final
owned cgroup sample after empty-state synchronization. `RUSAGE_CHILDREN` is insufficient.
`collection.cpu_seconds` sums scoped observations only when no ordinary observation
exists, otherwise null; it never includes execution group CPU. `worker_cpu_seconds` is
null if any actual session CPU is unavailable. `known_scoped_cpu_seconds` gives only the
known scoped subtotal, never total invocation CPU. Even reused groups can incur fresh
collection/setup costs. Reuse's measured current execution cost is zero.

## Refusal envelope

Before validated membership/selection, a typed error has schema=3, kind=refusal,
empty units/groups, zero counts, null inventory digest and incomplete routing. Preserve
known structural suites and planned collection observations, with not-started outcome,
null collected count and null timings/identity. A pre-manifest failure invents no
structural rows; unresolved runtime/backend/execution/python/resources may be null.
The [precollection example](bubblewrap-schema-examples.json) shows the full envelope.

Once any identity AND actual selection observation is validated, preserve it in kind=run
with partial inventory, nullable routing and the explicit failure. A post-selection
declaration failure may retain route=null, outcome=unsupported, reason=declaration-invalid,
result_status=null, complete=false and attempts=0 while top status is declaration_error.
This does not invent a test call. Groups retain any actual execution evidence.

Kind=refusal status is exactly unsupported, declaration_error or infrastructure_error.
Kind=run also permits passed, failed and no_tests. Cleanup/cancellation infrastructure
has precedence over typed refusal, which precedes test failure; otherwise an empty
selected invocation is no_tests. `error` appears on typed terminal failure and
`capability` only on unsupported. API errors before report creation use `error_result`.

Capability codes retain metadata, pytest and execution boundaries and add the explicit
backend/runtime codes above. Unsupported results never widen the profile or mint cached
success. A refusal or incomplete inventory does not prove that no earlier ordinary import
or test ran; never blindly replay under ordinary pytest after a scoped failure.

CLI exits remain passed=0, failed=1, infrastructure_error=3,
unsupported/declaration_error=4 and no_tests=5. Exception text is diagnostic, not a stable
machine interface. See the [API/runtime contract](bubblewrap-api.md) for exact failure
and resource envelopes, and [batching](batching.md) for partial group semantics.
