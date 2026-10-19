# Run results and explicit refusals

This reference describes the 0.1.0a3 schema. Start with the practical
[quickstart](index.md). The [scoped execution contract](scoped-execution.md)
defines metadata, suite and resource boundaries.

## Invocation interfaces

The dedicated CLI emits one JSON result and does not import project Python on the host:

```sh
python -m pytest_telescope run --suite core --explain -- -k selected
python -m pytest_telescope run --budget-cpus 2 --budget-memory-bytes 1073741824 --budget-processes 256 --budget-scratch-bytes 134217728
```

Repeat `--suite` to select several configured suites. An omitted suite selection runs all
configured suites. A CLI budget override supplies every dimension; partial, noninteger,
nonpositive or insufficient envelopes fail before collection or cache acceptance.

`--execution-mode case` or `--execution-mode batch` overrides the configured mode without
rewriting project configuration. The default is `case`; [batch mode](batching.md) shares
fixtures only within one suite/module and an exactly equal expanded scope. `plan` reports
`execution.mode` and `execution.profile`, but cannot predict real group membership.

The equivalent Python entry point is `pytest_telescope.controller.run(root, *, force=False,
no_cache=False, explain=False, cache_dir=None, timeout=60, arguments=(), budget=None,
suites=None, execution_mode=None)`. `root` identifies the outer project directory. `budget` is a dictionary of
`cpus`, `memory_bytes`, `processes`, and `scratch_bytes`; `suites` is a list of configured
names. Arguments and node-selector paths remain relative to the outer project, including
when a suite uses another working directory.

The Python API raises `DeclarationError` for invalid declarations and
`UnsupportedCapability` for unqualified capabilities. `UnsupportedCapability` subclasses
`DeclarationError` and exposes `capability`. Infrastructure failures during an established
run are accounted in its result where possible. `controller.error_result(error)` converts a
typed declaration, capability or infrastructure exception into the refusal envelope below.
Consumers should dispatch on the explicit status and capability, without parsing messages.

## Complete and incomplete run inventories

A run result has `schema: 2` and `kind: "run"`. These fields distinguish it from a refusal.

| Field | Meaning |
| --- | --- |
| `status` | `passed`, `failed`, `infrastructure_error`, or `no_tests`. |
| `inventory_complete` | Whether every selected suite produced a validated real inventory. |
| `inventory_digest` | Digest of current suite/case/file membership when complete; otherwise `null`. |
| `suites` | Configured suite names included in this invocation. |
| `execution` | Effective `mode` (`case` or `batch`) and versioned `profile` (`case-v1` or `batch-v1`). |
| `grouping_complete` | All suite inventories and group-size preflight are validated; selected cases then belong to exactly one planned group. |
| `groups` | Ordered execution/cache groups with atomic member outcomes and worker accounting. |
| `resources` | Per-worker dimensions, aggregate budget and computed concurrency. |
| `collection` | Collection outcome, counts, timings, bounded failure evidence and suite inventories. |
| `units` | One row for each known real case, in deterministic suite/node order. |
| `counts` | Exactly `executed`, `reused`, `failed`, `unsupported`, `infrastructure_error`, `not-selected`, and `selected`. |
| `cases` | Collected, passed, failed and skipped totals from selected case results. |
| `case_outcomes` | Separate fresh/cached passed and skipped totals. |

`collection.suites` contains each suite's `name`, structural `files`, collection `outcome`,
`collected` count, `inventory_digest`, `worker_seconds`, and `cpu_seconds`. Structural file
membership is available even before real collection succeeds. A suite not collected has
`outcome: "not-started"`, `collected: null`, and a null inventory digest. Known files do
not establish a complete case inventory: dynamic and parametrized items require pytest.

Each `units` row has `suite`, `file`, opaque `case_id`, `selected`, `outcome`, `attempts`,
counts, timings and bounded failure evidence. `name` is the same opaque case identity.
Outcomes are `executed`, `reused`, `failed`, `infrastructure_error`, or `not-selected`;
`counts` also reserves `unsupported`. `counts.selected` includes every selected known
case, including work parked after a terminal failure. `counts.not-selected` accounts for
current cases excluded by ordinary pytest selectors. A complete inventory can accompany
an infrastructure failure: failed worker or outer cleanup prevents publication of new
records without erasing the known inventory.

Raw parametrized node IDs stay inside the worker. A `case_id` is the lowercase SHA-256
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

Schema 2 applies to both modes. When `grouping_complete` is true, every selected known case belongs to exactly one group.
Unselected cases have `group_id: null`; selected known cases can also have null group IDs
when an earlier inventory or group-size refusal prevents completed planning. Groups appear in deterministic suite/module/
first-selected-member order. `case_ids` retains actual selected pytest order after
collection hooks. Hash sorting does not define execution order.

| Group field | Meaning |
| --- | --- |
| `group_id` | Opaque lowercase SHA-256 identity binding profile, ordered members and complete execution context. |
| `suite`, `file`, `case_ids` | Public suite/module and ordered selected opaque member identities. |
| `outcome` | `executed`, `reused`, `failed`, `incomplete`, or `infrastructure_error`. |
| `complete` | Every requested member has a validated final teardown outcome. |
| `cache_eligible` | Complete successful evidence accepted after lifecycle cleanup with reuse enabled. False for `--no-cache` and `cache=False`. |
| `attempts` | Group worker attempts; zero for reuse or before a worker starts. |
| `attempt_seconds` | Controller wall time across group attempts, including retries. |
| `worker_seconds`, `cpu_seconds` | Final valid attempt worker measurements, counted once per group; zero on reuse or when unavailable. |
| `timing_complete` | False when retries or unavailable measurements prevent a complete worker-timing account. |
| `passed`, `failed`, `skipped`, `not_run`, `unresolved` | Member totals: known unrun members are distinct from attempted members with no valid result. |
| `stop_reason` | `null` for complete groups; otherwise `pytest-stop`, `collection`, `infrastructure`, or `cancelled`. |

Each case row adds `group_id`, `result_status`, and `complete`. Result status is `passed`,
`failed`, `skipped`, `not-run`, or `null` for unselected/unresolved infrastructure cases.
Case `attempts` repeats its group count: summing case attempts does not count workers.
Every batch-mode case row, including unselected and unresolved members, has
`worker_seconds` and `cpu_seconds` set to `null`, because shared fixture
costs cannot be attributed honestly to individual members. Case mode retains singleton
measurements. `worker_cpu_seconds` counts each group once plus collection.

A reused group reuses every member together, preserving passed/skipped statuses. Any
missing, corrupt, partial or unsuccessful group evidence reruns its entire selected
membership. Case and batch profiles cannot reuse each other's records, even for a
singleton. Group cache eligibility differs from observed test success.

With `-x` or `--maxfail`, observed passes and skips remain case `outcome: "executed"`;
observed failures are `failed`; unrun requested members are `infrastructure_error` with
`result_status: "not-run"`. The group is `incomplete` with `stop_reason: "pytest-stop"`,
and the run status is `failed`, not an infrastructure outage. Shared setup failures
belong to each attempted affected member; shared teardown failure belongs to the member
whose teardown pytest reports. No member of a failed/incomplete group becomes reusable.
Skips are counted without retaining reason text or arbitrary captured output.

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

## Refusal envelope

If no complete inventory can be established for a typed refusal, the CLI emits:

```json
{"schema": 2, "kind": "refusal", "status": "unsupported", "capability": "scratch:exec", "inventory_complete": false, "inventory_digest": null, "error": "runtime profile does not qualify requested capability"}
```

Refusal status is `unsupported`, `declaration_error`, or `infrastructure_error`.
`capability` exists only for unsupported results. Capability codes include
`metadata:pep621-static-v1`, `project-metadata:runtime-conflict`,
`pytest:distributed-scheduling`, `pytest:configuration`, and `execution:batch-size`; an explicitly requested
unqualified profile reports its own capability string. Refusals never mint successful
case records or silently widen the execution profile.

A refusal or `inventory_complete: false` alone does not prove that no earlier case
executed. Callers that need an ordinary pytest path should choose structural partitions
and preflight routing before starting the run. Do not blindly replay a run after a
runtime refusal; incomplete accounting remains unresolved execution evidence.

CLI exit statuses are 0 for success, 1 for test failure, 3 for infrastructure failure,
4 for invalid declarations or unsupported capabilities, and 5 for no selected cases.
Cancellation and bounded storage/parsing failures use the infrastructure refusal shape
when a complete run result cannot be returned. Exception text is diagnostic, not a stable
machine interface.
