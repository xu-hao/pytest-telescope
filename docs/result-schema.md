# Run results and explicit refusals

> The schema 4 and per-scope environment interfaces described here are an unreleased
> development contract. Published version 0.1.0a6 does not provide them.

This reference describes result schema 4. Start with the practical
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

A run result has `schema: 4` and `kind: "run"`. These fields distinguish it from a refusal.

| Field | Meaning |
| --- | --- |
| `status` | `passed`, `failed`, `infrastructure_error`, or `no_tests`. |
| `inventory_complete` | Whether a complete, validated case inventory is available for each selected suite's effective file list. |
| `inventory_digest` | Digest of current suite/case/file membership when complete; otherwise `null`. |
| `suites` | Configured suite names included in this invocation. |
| `execution` | Effective `mode` (`case` or `batch`), versioned scoped `profile` (`case-v2` or `batch-v2`), and `workers`. |
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
`files`, collection `outcome`, `collected` count, `inventory_digest`, `worker_seconds`
and `skips`.
An ordinary row's `worker_seconds` is null because its session time belongs to its
ordinary group. Structural file membership is available even before real collection
succeeds. A row not collected has `outcome: "not-started"`, `collected: null`, and a null
inventory digest. `collection.collected` counts unique cases, and `collection.worker_seconds`
sums scoped collection time only. Known files do
not establish a complete case inventory: dynamic and parametrized items require pytest.
A validated structural file can collect no cases, including after a collection skip;
implicit files and explicit file targets do not invent selected cases. An explicit node
selector still requires a genuinely collected alias, even when its file is covered. File
coverage and collection skip evidence remain separate from case inventory and selection.
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

Schema 4 applies to both modes and both routes. When `grouping_complete` is true, every selected known case belongs to exactly one group.
Unselected cases have `group_id: null`; selected known cases can also have null group IDs
when an earlier inventory or group-size refusal prevents completed planning. Groups appear in deterministic suite/module/
first-selected-member order. `case_ids` retains actual selected pytest order after
collection hooks. Hash sorting does not define execution order.

| Group field | Meaning |
| --- | --- |
| `group_id` | Opaque lowercase SHA-256 identity binding profile, ordered members and complete execution context. |
| `suite`, `files`, `case_ids` | Public suite, sorted structural files and ordered selected opaque member identities. A scoped group has one file. |
| `route`, `profile` | `ordinary` with `ordinary-v2` for an ordinary suite session; `scoped` with `case-v2` or `batch-v2`. |
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
| `collection_skips` | Execution-side collection skip evidence, each with `file`, `phase`, and `reason`. |

Each case row adds `group_id`, `result_status`, `complete`, and `reports`. Result status is `passed`,
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
Skips retain the observed phase and bounded digest evidence described below, without
retaining reason text or arbitrary captured output.

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

## Observed phase and collection evidence

Every `units` row has `reports`, an ordered list of zero to three actual final pytest
log reports for `setup`, `call`, and `teardown`. Each object has exactly `phase`, `outcome`
(`passed`, `failed`, or `skipped`) and `skip_reason`. A skipped report requires a
non-null reason; passed and failed reports require null. The reason has exactly
`profile: "pytest-reason-sha256-v1"`, `kind: "skip"` or `"xfail"`, and `sha256`, a
lowercase 64-hex digest. Duplicate, reordered or unknown phases and inconsistent reason
presence are invalid. Empty reports mean no valid observed phase evidence; they never
prove success. Unselected and unavailable members have empty lists; an interrupted member
with a valid result can retain an observed prefix. Fresh and reused reports are identical.

Setup precedes call or teardown; call exists only after passed setup. Teardown may follow
skipped or failed setup without a call. A complete attempted case has actual setup and
teardown, plus call if setup passed. Any failed phase makes its member failed; otherwise
any skipped phase makes it skipped; otherwise a passed call makes it passed, with other
prefixes not-run. Count members, not phase reports. A teardown failure after a skip leaves
the skip visible but makes the member failed. A nonzero session verdict, missing teardown,
failed or incomplete member, descendant leak, or outer cleanup failure prevents reuse
regardless of observed passes or skips. Ordinary members have the same reports but never
reuse them.

For a final skipped pytest report, an actual string `wasxfail` supplies the exact reason
and kind `xfail`. A passed XPASS is not a skip. Otherwise kind is `skip`, and the reason
is exactly the third field of an actual `(path: str, line: int, reason: str)` longrepr
tuple, or the exact string longrepr. No arbitrary object representation is evaluated.
The exact string must encode as strict UTF-8 in 0 to 4,096 bytes. Hash those bytes without
stripping prefixes, changing whitespace or truncating. For example, if pytest's final
report actually gives `Skipped: optional feature disabled`, a consumer computes:

```python
import hashlib

expected = hashlib.sha256(b"Skipped: optional feature disabled").hexdigest()
```

Unsupported, missing, unencodable or oversized reason evidence yields a bounded generic
infrastructure result, without emitting the raw reason, longrepr or traceback in protocol
or normal diagnostics; exhausted recovery refuses rather
than fabricating evidence or falling back to ordinary execution. A digest is comparable
to a known expected reason, but is not proof of a skip, current context or final teardown.
Avoid sensitive reasons: a predictable reason can be guessed from its digest.

`collection.skips` contains objects with exactly `suite`, `route` (`ordinary` or
`scoped`), `file` (canonical project-relative path), `phase: "collect"`, and `reason`. Each `collection.suites` row has `skips` with `file`, `phase`
and `reason`; groups have `collection_skips` in that same shape for execution-side
collection. Reasons use the same profile with kind `skip`. Collection skips come from actual final pytest collection reports and do not
invent cases or member successes. The worker allows at most 100,000 entries subject to the
existing aggregate response limit. Collection evidence can be reused only with a valid
current inventory; missing, malformed or stale evidence invalidates the inventory record.

To accept evidence, a consumer checks complete current inventory and its requested
structural/suite/case coverage and selection, opaque case identity, ordered group
membership and context, unit outcome/status and actual phase/reason, unit and group
completeness, final group/run verdict and fresh or reused status. Unknown profiles,
partial evidence or a failed lifecycle cannot establish a skip or authorize a replay.

## Internal protocol and cache versions

Persistent cache envelopes use schema 7. Collection, singleton execution, batch execution
and ordinary-session worker protocols use versions 10, 8, 9 and 2 respectively. Old cache
records and protocols are ineligible; strict producers and validators advance together.
Every request and worker common result carries an exact lowercase 64-hex `context_id`.
For a batch it equals `group_id`; for a singleton or collection it equals the candidate
or inventory cache key; an ordinary invocation uses a fresh nonce-bound digest as its
public group ID. The controller validates the requested context before projection and
requires a persisted result's context to equal its cache key. Requests contain no raw
environment map.

Every worker result has an exact boolean `declaration_error`. It is true only when the
worker's declaration validation failed, and requires `exit_code: 4` and `complete: false`.
For ordinary protocol 2 it also requires `inventory_complete: false`; that protocol's
field set and version are unchanged. Missing, nonboolean, unknown or inconsistent fields
are invalid. Public result schema 4 and the `case-v2`, `batch-v2` and `ordinary-v2` profiles
are unchanged.

When `declaration_error` is false, valid pytest exit codes 0 through 5 retain their
existing per-kind completion and count rules. The flag does not imply success. In
particular, a session hook can set exit 4 after a passing body: its validated phase
reports remain observations, while the group and run fail and that group cannot be
reused. Collection with false/exit 4 is failed collection, not a declaration refusal.
Exit code alone never supplies the declaration flag.

The sandbox validates and returns explicit declaration-error evidence so the controller
can retain known observations before a typed refusal. A typed boundary exception with no
returned worker result instead retains actual started-attempt counts and unresolved
members. Neither path permits ordinary replay or publication of pending successes after
a refusal. The separate unsupported-capability marker keeps its existing meaning.

Worker results also contain `collection_skips`, each exactly `file` and `reason`.
Execution member rows contain exactly `case_id`, `outcome`, `complete`, and `reports`;
singleton protocol 8 includes `case_results` just as batch and ordinary sessions do.
Counts and member order reconcile with these rows and the request. A singleton collection
failure can have zero collected cases and empty `case_ids` and `case_results`; its known
requested public member remains not-run, incomplete and failed under the case-profile
collection verdict. Collection protocol 10 carries inventory items and collection skips,
never executed reports or passes. The existing nonce and exact request identity bind the
worker response to its invocation.

## Refusal envelope

### Retained observations after a late refusal

After an established invocation has started a session, a typed `DeclarationError` or
`UnsupportedCapability` retains the observations the controller has actually received.
The exception's `accounting` property is an immutable, bounded producer-owned value;
`render()` returns a fresh JSON value. Early exceptions can have `accounting: None`.
The CLI and `error_result` retain that same evidence in the refusal envelope. They do
not turn a refusal into a successful run. Before exposing the exception the controller
drains started work and discards newly staged successes; pre-existing cache records remain.

For the existing unguarded schema 4 profiles, available accounting is exactly
`{"version": 1, "state": "available", "observations": ...}`. The observations contain
the established run's execution, suites, inventory and grouping completeness, counts,
case and collection evidence, units, groups, and timing. The execution profile remains
`case-v2` or `batch-v2`. If the evidence exceeds its output or cardinality bound, the
accounting is exactly `{"version": 1, "state": "unavailable", "reason": "bounds"}`.

For the unreleased guarded schema 5 profile, available accounting is exactly
`{"version": 2, "state": "available", "observations": ...}`. Its observations have
the same fields as version 1 plus exactly `fixture_guard`; execution mode is `batch` and
profile is `batch-available-v1`. The outer refusal has the same `fixture_guard` object as
the retained observations. Its profile is `available-v1`, its state is the actual
producer-observed `admitted` or `unavailable` state, and its conditions are the actual
privacy-safe case/group/matched rows. Each row refers to a selected case and its actual
group; admitted rows have matched successfully. An unavailable guard has no condition
rows. Outer inventory completeness and digest agree exactly with the observations.

If bounded guard accounting cannot be represented, the accounting is exactly
`{"version": 2, "state": "unavailable", "reason": "bounds"}` and the outer guard
object has state `unavailable` and empty conditions. This is an evidence limitation,
not a claim that admission or test bodies did not happen. Known outer inventory fields
remain truthful. Invalid internal observations are rejected as an infrastructure
failure, not coerced into the bounds case. No raw environment values are retained.

If no complete inventory can be established for a typed refusal, the CLI emits:

```json
{"schema": 4, "kind": "refusal", "status": "unsupported", "capability": "sandbox:namespaces", "inventory_complete": false, "inventory_digest": null, "error": "Bubblewrap could not create the user, mount and PID namespaces that scoped tests need: bwrap: setting up uid map: Permission denied"}
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

## Required guard result schema 5 (unreleased)

Schema 5 is the required opt-in development contract, not a shipped or qualified
feature. Unguarded runs and refusals continue to use schema 4. A guarded run preserves
schema 4 fields and adds exactly:

```json
{"profile": "available-v1", "state": "admitted", "conditions": []}
```

This object is the `fixture_guard` field. A guarded run may instead have state
`unavailable` and empty `conditions` when the producer returns real partial collection or
infrastructure observations before admission. That state makes no inference that bodies
were unrun; the run retains its actual available inventory and observations. `admitted`
records admission of the original complete pre-dispatch plan, not eventual completion. Condition rows, in caller order,
contain exactly `case_id`, `group_id` and `matched`; they bind an actually selected case
to its actual group and never disclose environment names, operations, values or hashes.
All checks must match before dispatch or reuse. A cache hit evaluates conditions again
from the current invocation's capture, before accepting the hit. Match evidence alone
is not a pass, skip, teardown or final-session verdict.

`error_result(error, fixture_guard="available-v1")` produces a schema 5 refusal with
existing schema 4 refusal fields and this exact additional field:

```json
{"profile": "available-v1", "state": "unavailable", "conditions": []}
```

`unavailable` means the converter has no validated phase, admission or attempt evidence.
It does not establish that admission failed, no collection occurred, no body ran, or
that replay is safe. It adds no fabricated units, groups or zero-attempt assertion.
`inventory_complete: false` likewise means inventory is unknown. There is no
`not-admitted` state inferred from an exception. `error_result(error)` remains schema 4;
a parser failure before a valid guarded CLI invocation also retains schema 4.

Expected execution-side drift or another controlled late infrastructure failure is
reported as a schema 5 run with `admitted` for the original plan and the actual prior
attempts, group outcomes and infrastructure status retained. No new staged successes
are published. An unexpected exception escaping after possible execution can still be
converted, but the resulting `unavailable` refusal makes no claim about phase or bodies.
