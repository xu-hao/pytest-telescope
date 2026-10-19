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

The equivalent Python entry point is `pytest_telescope.controller.run(root, *, force=False,
no_cache=False, explain=False, cache_dir=None, timeout=60, arguments=(), budget=None,
suites=None)`. `root` identifies the outer project directory. `budget` is a dictionary of
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

A run result has `schema: 1` and `kind: "run"`. These fields distinguish it from a refusal.

| Field | Meaning |
| --- | --- |
| `status` | `passed`, `failed`, `infrastructure_error`, or `no_tests`. |
| `inventory_complete` | Whether every selected suite produced a validated real inventory. |
| `inventory_digest` | Digest of current suite/case/file membership when complete; otherwise `null`. |
| `suites` | Configured suite names included in this invocation. |
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

## Refusal envelope

If no complete inventory can be established for a typed refusal, the CLI emits:

```json
{"schema": 1, "kind": "refusal", "status": "unsupported", "capability": "scratch:exec", "inventory_complete": false, "inventory_digest": null, "error": "runtime profile does not qualify requested capability"}
```

Refusal status is `unsupported`, `declaration_error`, or `infrastructure_error`.
`capability` exists only for unsupported results. Version 1 capability codes include
`metadata:pep621-static-v1`, `project-metadata:runtime-conflict`,
`pytest:distributed-scheduling`, and `pytest:configuration`; an explicitly requested
unqualified profile reports its own capability string. Refusals never mint successful
case records or silently widen the execution profile.

CLI exit statuses are 0 for success, 1 for test failure, 3 for infrastructure failure,
4 for invalid declarations or unsupported capabilities, and 5 for no selected cases.
Cancellation and bounded storage/parsing failures use the infrastructure refusal shape
when a complete run result cannot be returned. Exception text is diagnostic, not a stable
machine interface.
