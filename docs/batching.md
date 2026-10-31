# Optional identical-scope batches

Batch mode lets scoped tests share a pytest session and its fixtures. It can reduce
startup and fixture setup time when tests in the same module need exactly the same
files. Ordinary cases already share one session per suite; batching applies only to
scoped cases.

The [execution contract](bubblewrap.md) describes routing and the sandbox view. The
[metadata and suite reference](scoped-execution.md) covers their configuration.

## Choose fixture lifetime explicitly

By default, each scoped case that needs to run gets a new sandbox, pytest session and
private scratch. Use `batch` if tests with the same scope can share fixtures:

```toml
[tool.pytest_telescope.execution]
mode = "batch"
```

The CLI override is `pytest-telescope run --execution-mode batch`; the Python override is
`controller.run(root, execution_mode="batch")`. Both accept only `case` or `batch`;
omission uses configured mode, defaulting to `case`. An override changes execution/cache
identity without rewriting project configuration. `plan` reports the configured mode but
cannot predict dynamically collected group membership.

The versioned execution profiles are `case-v1` and `batch-v1`. A batch occupies one
worker slot regardless of member count; `workers` bounds how many cases or batches run at
once. Set `workers` to choose the concurrency; pytest-telescope does not infer it from
the host's CPUs. Batching does not change which source or metadata a test can read.

## Exact scope and deterministic membership

Cases share a batch only when they belong to the same suite and source module and have
exactly equal readable files, declared absence, generated metadata and cache policy.
File comparisons include contents and supported modes. Two declarations can be written
differently and still produce the same scope after expansion. Cases with different
scopes stay in separate batches; pytest-telescope does not combine their inputs.
`cache=False` cases form separate, uncached groups. Their grouping must still be
reproducible.

Each group keeps pytest's selected execution order after collection hooks and selection.
The result records that order as opaque case digests. Sorting digests or raw parameter
strings would give a different order. Collection accounts for all cases in the configured
files, including unselected cases. Before running a batch, pytest-telescope collects its
module again and checks the full inventory and ordered selection. Changed, missing,
additional or reordered members cause a refusal.

A group's SHA-256 identity binds profile, suite/context, source module, expanded scope,
ordered selected membership, containing-file inventory, arguments, explicit plugins,
sandbox identity (policy, interpreter, installed distributions and the environment's
references into the project) and environment. Its members retain their stable
suite-aware case digests. Selection additions/removals, order changes and relevant input
or environment changes cannot reuse another group's evidence. Case and batch records are
mutually ineligible, including singleton batches.

A batch contains at most 4,096 selected members. An oversized group refuses the entire
planned invocation with `UnsupportedCapability("execution:batch-size")` before any fresh
group starts; it is not implicitly chunked. Structured batch results and cache records
are limited to 64 MiB; individual case results retain their 1 MiB limit. The combined
merged worker-output stream is bounded at 64 MiB for batch/collection and 1 MiB for
case execution. This includes structured results and any direct file-descriptor writes;
there is no independent smaller arbitrary-output channel. Ordinary Python/pytest output
is normally suppressed, and arbitrary output is not returned as diagnostics.

A batch uses one fresh sandbox, pytest invocation and scratch directory. Session, module
and class fixtures share state and teardown according to ordinary pytest rules inside
that selected group. Function fixtures and `tmp_path` retain normal per-test pytest
semantics; session scratch and `tmp_path_factory` live for the group. Fixtures do not
share across groups, modules or suites. There is no mutable worker reuse between groups.

## Atomic success and bounded failure

One validated group record contains the ordered requested membership and all member
outcomes from the same completed session. Reuse is all-or-nothing. Missing, malformed,
corrupt, mismatched or unsuccessful evidence reruns the whole selected group. `--force`
and `--no-cache` retain their existing meanings at group granularity. No successful
member record is extracted from a failed or incomplete group for later reuse.

Track setup, call and teardown for every requested member. A shared-fixture setup failure
is reported for each affected member that pytest attempts; a shared-fixture teardown
failure belongs to the member whose teardown pytest reports. The whole group is
nonreusable if any member fails or any requested member lacks completed teardown.
`-x` and `--maxfail` may stop a group partway through: preserve observed passes/failures/
skips, explicitly account for not-run members, and mark the group incomplete. An observed
pass remains an observed pass even when its group cannot be cached. Skips are counted,
but skip reason text and arbitrary captured output are not supported diagnostics.

Infrastructure retries remain bounded at two attempts per group. Only the final valid
attempt supplies member results and worker time. Controller wall time spans all
attempts; `timing_complete` is false after retries or unavailable results, because worker
time from an opaque failed attempt is unknown. These measurements are separate from
member counts. Invalid worker output, collection drift, cancellation,
failed descendant cleanup and failure to clean up the invocation prevent reusable results.
Cancellation stops new submissions and drains all owned active groups. Unstarted
members remain explicitly accounted. No fallback widens the view or runs ordinary.

The main controller retains the global cache lock and pending-record staging. A group
record is staged only for a complete successful group, then becomes eligible only after
all required workers and outer invocation cleanup complete. A terminal cleanup failure
prevents publication of all new records, including other groups' successes.

## Machine accounting

Both execution modes and typed refusals use result schema 3. The
[run result reference](result-schema.md) defines the inventory and case digests and
count fields.

Run results add `execution` with `mode` and `profile`, and `groups`, in deterministic
suite/module/first-selected-member order. `grouping_complete` remains false until every suite inventory and group-size preflight
has validated. Once it is true, every selected known case belongs to exactly one group.
Unselected cases have `group_id: null`; selected known cases may also have null group IDs
if planning was interrupted before completion. A group row contains:

| Field | Meaning |
| --- | --- |
| `group_id` | Opaque lowercase SHA-256 identity bound to the exact execution context above. |
| `suite`, `files`, `route` | Public structural membership: one file and route `scoped` for case and batch groups. |
| `case_ids` | Ordered selected opaque member identities. |
| `outcome` | `executed`, `reused`, `failed`, `incomplete`, or `infrastructure_error`. |
| `complete` | Every requested member produced a validated final teardown outcome. |
| `cache_eligible` | Complete successful evidence accepted after lifecycle and enabled reuse policy; false for `--no-cache` or `cache=False`. |
| `attempts` | Worker attempts for this group; zero on reuse or before worker start. |
| `attempt_seconds` | Controller wall time across group attempts and retries. |
| `worker_seconds` | Final valid attempt worker wall time, counted once; zero on reuse or when unavailable. |
| `timing_complete` | False when retries or unavailable worker measurements prevent complete timing accounting. |
| `passed`, `failed`, `skipped`, `not_run`, `unresolved` | Final-attempt member totals; distinguish known unrun members from attempted members without a valid result. |

Each case row adds `group_id`, `result_status` (`passed`, `failed`, `skipped`, `not-run`,
or `null` for unselected/unresolved infrastructure), and `complete`. Case `attempts`
reports its group's attempt count; do not sum member attempts to count workers. In batch
mode every case row, including unselected and unresolved members, has `worker_seconds`
set to `null`: shared fixture and worker costs cannot be separated into per-case
measurements. Case mode retains singleton timings.
Group `failures` retains bounded phase/location/type/opaque-ID evidence, including
collection errors whose IDs do not identify a requested member. A nonzero pytest
session verdict fails the group/run even if every member report passed; those observed
passes do not become reusable evidence. See the result reference for the preserved
case-profile collection-failure verdict and separate not-run member counts.

Use group timings for execution totals. pytest-telescope reports no CPU
usage; measure it for the whole invocation in the job that runs pytest.

Case `outcome` keeps the existing vocabulary. Observed passed/skipped members are
`executed` even in a failed/incomplete group; observed failures are `failed`; missing
members are `infrastructure_error` with `result_status: "not-run"`. An intentional
pytest stop is distinguished by group `outcome: "incomplete"` and `stop_reason:
"pytest-stop"`; execution-side collection failure uses `"collection"`, dependency outages use `"infrastructure"`, cancellation
uses `"cancelled"`, and complete groups use `null`. A pytest-stop run reports status
`failed`, not a dependency outage, even though missing-member count vocabulary is retained. Reuse applies to all
members together and preserves their passed/skipped statuses. Group `cache_eligible`
distinguishes observed results from reusable evidence. Complete inventory does not imply
complete execution. A refusal or incomplete inventory never proves no earlier member
ran; choose ordinary execution through structural/preflight routing before a run.

If a worker attempt supplies no valid result, its members may already have executed:
`unresolved` counts those members, `not_run` is zero, and their `result_status` is null.
A group never submitted after cancellation has known `not_run` members with status
`not-run`. A valid final worker result has `unresolved: 0`. These categories reconcile
the whole membership without claiming that missing evidence proves no execution.

All persistent membership uses opaque case/group identities. Raw collected node IDs,
parameter values, skip reasons and exception messages do not enter group records or
summaries. Explicit caller selector expressions still reach sandboxes unchanged.
The existing bounded inventories, output and cache storage remain bounded; groups are
limited by the same 100,000-case invocation bound and validated serialized record limits.

## Qualification

Behavioral controls run real Bubblewrap sandboxes and cover ordinary shared
session/module/class setup and teardown within a group, distinct function `tmp_path`,
shared scratch, absent undeclared files, equal-scope eligibility, unequal-scope
separation, partial execution for `-x`/`--maxfail` and shared teardown failure,
selection/membership/order changes, partial or corrupt records, noncacheable groups,
bounded retries and cleanup. Measure startup overhead and fixture setup time in your own
suite when deciding whether to use batching.
