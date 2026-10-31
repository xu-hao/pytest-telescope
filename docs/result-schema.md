# Run results and explicit refusals

This reference describes result schema 3. Start with the practical
[quickstart](index.md). The [API and configuration reference](bubblewrap-api.md) lists the
interfaces that produce these results, and
[complete examples](bubblewrap-schema-examples.json) show whole reports.

The Python API raises `DeclarationError` for invalid declarations and
`UnsupportedCapability` for unsupported capabilities. `UnsupportedCapability` subclasses
`DeclarationError` and exposes `capability`. Infrastructure failures during an established
run are accounted in its result where possible. `controller.error_result(error)` converts a
typed declaration, capability or infrastructure exception into the refusal response below.
Use the status and capability fields to handle these errors; message text can change.

## Complete and incomplete run inventories

A run result has `schema: 3` and `kind: "run"`. These fields distinguish it from a refusal.

| Field | Meaning |
| --- | --- |
| `status` | `passed`, `failed`, `infrastructure_error`, or `no_tests`. |
| `inventory_complete` | Whether a complete, validated case inventory is available for each selected suite's effective file list. |
| `inventory_digest` | Digest of current suite/case/file membership when complete; otherwise `null`. |
| `suites` | Configured suite names included in this invocation. |
| `execution` | Effective `mode` (`case` or `batch`), versioned scoped `profile` (`case-v1` or `batch-v1`), and `workers`. |
| `sandbox` | Exactly `backend` (`bubblewrap`), `policy` (`bubblewrap-view-v2`), `required`, and `identity`. |
| `grouping_complete` | All suite inventories and group-size preflight are validated; selected cases then belong to exactly one planned group. |
| `groups` | Ordered ordinary sessions and scoped execution/cache groups with atomic member outcomes and wall-clock timing. |
| `collection` | Collection outcome, counts, timings, bounded failure evidence and per-route suite inventories. |
| `units` | One row for each known real case, in deterministic suite/node order. |
| `counts` | Exactly `executed`, `reused`, `failed`, `unsupported`, `infrastructure_error`, `not-selected`, and `selected`. |
| `route_counts` | Exactly `ordinary` and `scoped`, each with the same seven keys as `counts`; for each key the two routes sum to `counts`. |
| `cases` | Collected, passed, failed and skipped totals from selected case results. |
| `case_outcomes` | Separate fresh/cached passed and skipped totals. |

`sandbox.required` is true when the effective structural boundary contains any scope
declaration, even if no scoped case is selected; an all-ordinary run never starts
Bubblewrap. `sandbox.identity` is null until the interpreter probe and Bubblewrap
preflight succeed; afterward it is an opaque SHA-256 digest of the view policy, the
interpreter identity, the installed distributions and how the environment reaches into
the project. A matching sandbox identity alone is not enough to reuse a result.

`collection.suites` has one row per suite and route that has files. The `ordinary` row
covers the files of the suite's ordinary session; the `scoped` row covers files whose
module-level declaration scopes every case. Each row contains `name`, `route`, structural
`files`, collection `outcome`, `collected` count, `inventory_digest` and `worker_seconds`.
An ordinary row's `worker_seconds` is null because its session time belongs to its
ordinary group. Structural file membership is available even before real collection
succeeds. A row not collected has `outcome: "not-started"`, `collected: null`, and a null
inventory digest. `collection.collected` counts unique cases, and `collection.worker_seconds`
sums scoped collection time only. Known files do
not establish a complete case inventory: dynamic and parametrized items require pytest.
With `structural_files`, these are the effective exact file lists. Files outside them
produce neither case rows nor `not-selected` counts; a complete inventory does not claim
coverage of those files. Adding a new file outside an explicit list does not add it to
that invocation's inventory. The caller must expand the list or omit the override to
discover it.

Each `units` row has `suite`, `file`, opaque `case_id`, `route`, `selected`, `outcome`,
`attempts`, counts, `worker_seconds` and bounded failure evidence. `name` is the same
opaque case identity. `route` is `ordinary` or `scoped`. An ordinary case is never
`reused`; its `worker_seconds` is null because it shares its session.
Outcomes are `executed`, `reused`, `failed`, `infrastructure_error`, or `not-selected`;
`counts` also reserves `unsupported`. `counts.selected` includes every selected known
case, including cases left unrun after a terminal failure. A scoped case left unexecuted because
a collection stopped the run keeps outcome `infrastructure_error`, reason `execution not
started` and a null `group_id`, but the run status follows the collection outcome: a
failed collection is `failed`, not an infrastructure outage. `counts.not-selected` accounts for
current cases excluded by ordinary pytest selectors. A complete inventory can accompany
an infrastructure failure: failed worker or outer cleanup prevents publication of new
records without erasing the known inventory.

Raw parametrized node IDs stay inside each session. A `case_id` is the lowercase SHA-256
hex digest of UTF-8 canonical JSON `[suite_name, project_relative_pytest_node_id]`, with
no spaces between JSON separators and standard JSON string escaping. The same file in
two suites produces different identities. Selector aliases in collection records are
hashed too. Collected node IDs and aliases are not persisted in cached inventories,
summaries, or execution requests; callers can still pass ordinary node selectors.
Explicit caller-supplied `-k` and `-m` expressions necessarily reach every session in
request arguments. Those expressions are not redacted and may themselves contain
a parameter label; consumers must avoid echoing them into public receipts. File paths and suite names are public
project structure and should not themselves contain private values.

`inventory_digest` digests every case's suite, identity, file and route; it describes
membership, not test success. Consumers must also inspect
`inventory_complete`, selection and outcomes. A reused pass is previous evidence accepted
under the current identity, not a newly executed case.

## Atomic groups and member outcomes

Schema 3 applies to both modes and both routes. When `grouping_complete` is true, every selected known case belongs to exactly one group.
Unselected cases have `group_id: null`; selected known cases can also have null group IDs
when an earlier inventory or group-size refusal prevents completed planning. Groups appear in deterministic suite/module/
first-selected-member order. `case_ids` retains actual selected pytest order after
collection hooks. Hash sorting does not define execution order.

| Group field | Meaning |
| --- | --- |
| `group_id` | Opaque lowercase SHA-256 identity binding profile, ordered members and complete execution context. |
| `suite`, `files`, `case_ids` | Public suite, sorted structural files and ordered selected opaque member identities. A scoped group has one file. |
| `route`, `profile` | `ordinary` with `ordinary-v1` for an ordinary suite session; `scoped` with `case-v1` or `batch-v1`. |
| `outcome` | `executed`, `reused`, `failed`, `incomplete`, or `infrastructure_error`. |
| `complete` | Every requested member has a validated final teardown outcome. |
| `cache_eligible` | Complete successful evidence accepted after lifecycle cleanup with reuse enabled. Always false for ordinary groups, `--no-cache` and `cache=False`. |
| `attempts` | Group session attempts; zero for reuse or before a session starts. Ordinary groups have one. |
| `attempt_seconds` | Controller wall time across group attempts, including retries. |
| `worker_seconds` | Final valid attempt session wall time, counted once per group; zero on reuse or when unavailable. |
| `timing_complete` | False when retries or unavailable measurements prevent a complete timing account. |
| `passed`, `failed`, `skipped`, `not_run`, `unresolved` | Member totals: known unrun members are distinct from attempted members with no valid result. |
| `stop_reason` | `null` for complete groups; otherwise `pytest-stop`, `collection`, `infrastructure`, or `cancelled`. |
| `failures` | Bounded phase/location/type/opaque-ID evidence, including execution-side collection failures that do not belong to a requested member. |

Each case row adds `group_id`, `result_status`, and `complete`. Result status is `passed`,
`failed`, `skipped`, `not-run`, or `null` for unselected/unresolved infrastructure cases.
Case `attempts` repeats its group count: summing case attempts does not count workers.
Every batch-mode case row, including unselected and unresolved members, has
`worker_seconds` set to `null`, because shared fixture costs cannot be measured separately
for each member. Case mode retains per-case measurements. Results
contain no CPU fields: resource accounting belongs to the caller.

An ordinary group is one suite session. Its selected ordinary cases are its members; an
ordinary session that selects no ordinary case still appears as a group with empty
`case_ids` when it ran. Ordinary members follow the same `-x`/`--maxfail`, failure and
not-run rules below, are never cache eligible, and are never retried.

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

## Refusal envelope

If no complete inventory can be established for a typed refusal, the CLI emits:

```json
{"schema": 3, "kind": "refusal", "status": "unsupported", "capability": "sandbox:namespaces", "inventory_complete": false, "inventory_digest": null, "error": "Bubblewrap could not create the user, mount and PID namespaces that scoped tests need: bwrap: setting up uid map: Permission denied"}
```

Refusal status is `unsupported`, `declaration_error`, or `infrastructure_error`.
`capability` exists only for unsupported results. The
[API reference](bubblewrap-api.md#capability-codes) lists the capability codes, including
`sandbox:bubblewrap`, `sandbox:namespaces` and `sandbox:environment`. A refusal creates no
reusable result. The runner does not widen the view or fall back to ordinary execution.

A refusal or `inventory_complete: false` alone does not prove that no earlier case
executed: ordinary sessions may already have run. Sandbox prerequisites are checked
before any session starts, so a `sandbox:*` refusal happens before test code runs. Do not
automatically replay a run after a refusal: some tests may have run even if their results
are missing.

CLI exit statuses are 0 for success, 1 for test failure, 3 for infrastructure failure,
4 for invalid declarations or unsupported capabilities, and 5 for no selected cases.
Cancellation and bounded storage/parsing failures use the infrastructure refusal shape
when a complete run result cannot be returned. Exception text is diagnostic, not a stable
machine interface.
