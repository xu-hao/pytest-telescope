# Reproducible benchmark

The original seven-case measurements below describe version 0.1.0a2. The
[128-case scoped execution measurement](#scoped-execution-at-128-cases) qualifies
version 0.1.0a3 with independent suites, local metadata and explicit worker budgets.

## Workload

The `examples/analytics` project contains **seven actual cases**. Six original cases
exercise customer scoring, demand projections, and price schedules using deterministic
Python computation and JSON resources. A seventh unmarked case reads a project note and
uses the broad project snapshot. There are no artificial sleeps or external services.

Module literal markers select domain bundles, which include a shared helper and
conftest. A function marker adds `data/scoring-check.json` to just one scoring case.
Each case is an independent fresh pytest session and cache unit. A session fixture is
therefore recreated seven times on a cold run, rather than shared across these cases.

The harness measures ordinary pytest, runtime preparation, cold isolated execution,
unchanged warm reuse, a scoring Python change, a demand JSON change, a single-case JSON
change, and a shared helper change. The mutations preserve passing assertions while
changing actual input content. Each phase runs the same seven cases. The source example
is never modified.

## Reproduce

Install this checkout into a virtual environment and ensure a local Linux Docker daemon
is available. From the repository root with that environment active:

```sh
python benchmarks/run.py --output /tmp/telescope-benchmark --repetitions 3
```

The output directory must be new. Every trial retains its copied project, command logs,
ordinary pytest JUnit report, and structured results. Runtime preparation occurs once
per trial; cached Docker layers can accelerate it. The final `results.json` includes
all measurements and phase medians. The harness leaves prepared runtime images available
for inspection; remove only its recorded project-owned images when finished, following
normal Docker administration practices. It never prunes shared daemon build caches.

The harness validates every case's opaque identity and relative source file, exactly one
passed case per unit, the exact executed/reused identities after each mutation, complete
collection inventory, and fresh/cached case totals. It rejects duplicate identities,
wrong same-file consumers, incomplete collection, and inconsistent worker accounting.

```sh
python -m unittest discover -s benchmarks -p 'test_*.py'
```

These behavioral harness checks exercise outcome validation and real external process
accounting. They do not substitute for the Docker benchmark.

## Measurement method

Elapsed time uses an external monotonic `time.perf_counter` clock. Host process CPU is
the delta of `resource.getrusage(RUSAGE_CHILDREN)` around each command. This includes the
CLI and its reaped children, but excludes the Docker daemon's workers and builder.

Worker CPU uses OS `getrusage` accounting inside each worker, including its reaped
children. The report separates collection CPU from the sum of independently executed
case-worker CPU. Their sum reconciles with the controller's reported total worker CPU;
reused collection or case records contribute zero current worker time/CPU. None of
these columns claims total Docker daemon resource consumption.

Cold runs include frozen input construction, separate inventory collection, seven fresh
case sessions, teardown, and recording. Warm runs still enumerate/freeze project inputs
and check keys, but a fully warm inventory-and-result hit starts no containers. Changed
input content refreshes collection while unrelated case results can remain reusable.

The report also retains controller setup time through inventory resolution, excluding
the collection worker-session duration, and external elapsed minus all collection/case
worker-session durations. The latter includes CLI/bootstrap,
container lifecycle, cache handling and cleanup. It is not a pure startup measurement;
these timing categories overlap and must not be added together.

## Measured results

Measured on 2026-10-19 with **three complete trials** using an installed
pytest-telescope 0.1.0a2 wheel. Every trial passed all identity, case-count, invalidation,
and collection-accounting checks. Times below are medians in seconds. Each column is
computed independently, so component medians need not sum exactly to the total median.

| Scenario | External elapsed | Host process CPU | Total worker CPU | Collection CPU | Case-worker CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ordinary pytest | 0.965 | 0.943 | — | — | — |
| Runtime preparation | 0.815 | 0.267 | — | — | — |
| Cold isolated | 6.524 | 1.019 | 2.527 | 0.205 | 2.322 |
| Warm unchanged | 0.214 | 0.198 | 0.000 | 0.000 | 0.000 |
| Scoring Python change | 3.270 | 0.605 | 1.120 | 0.199 | 0.922 |
| Demand JSON change | 3.119 | 0.598 | 1.099 | 0.206 | 0.888 |
| Single-case JSON change | 2.518 | 0.492 | 0.919 | 0.203 | 0.715 |
| Shared helper change | 6.474 | 1.012 | 2.541 | 0.202 | 2.339 |

| Scenario | Collection | Fresh / cached passing cases |
| --- | --- | ---: |
| Cold isolated | executed | 7 / 0 |
| Warm unchanged | reused | 0 / 7 |
| Scoring Python change | executed | 3 / 4 |
| Demand JSON change | executed | 3 / 4 |
| Single-case JSON change | executed | 2 / 5 |
| Shared helper change | executed | 7 / 0 |

All runs have seven collected/passed cases and no failed, skipped, unselected, or
infrastructure-error cases. The counts are identical across all three trials. Ordinary
pytest executes all seven in one host session and has no telescope result-reuse counts.

The scoring Python and demand JSON changes each rerun their two domain cases **plus the
broad case**, leaving four cached successes. The single-case resource change reruns its
one scoring consumer **plus the broad case**, leaving five cached successes. It does not
rerun only one physical case. The shared helper change reruns all seven.

Runtime preparation is separate from cold execution and benefited from preexisting
Docker layers. It does not estimate a first image download on a new host. The observed
cold-minus-ordinary elapsed delta is 5.558 seconds, measuring complete different
execution paths rather than pure startup overhead. Small cases expose container and
collection costs; these measurements do not predict gains for a different suite.

### Environment and evidence

- Host: Linux 6.8.0-147-generic, x86_64, 24 logical CPUs.
- Host interpreter: Python 3.12.3, pytest 9.1.1, pytest-telescope 0.1.0a2.
- Workers use the pinned Python 3.12 runtime and pytest 9.1.1 specified in the
  [execution contract](execution-contract.md), with one CPU and 512 MiB per worker.
- Ordinary pytest disables plugin auto-loading and explicitly loads telescope only to
  register its markers; it does not enable isolated execution. Host and worker Python
  builds can differ, so this is not a controlled same-interpreter startup comparison.
- Each trial prepares a project-owned image. Equivalent runtime contents may have
  different immutable image IDs because ownership differs. Recorded images from this
  qualification were removed after measurement; shared Docker build caches were left
  untouched.

The [structured measurements](https://github.com/xu-hao/pytest-telescope/blob/main/benchmarks/results/linux-docker.json)
contain every trial, runtime identity, installed runner digest, case outcomes, collection
work, and medians. Raw command logs and copied projects remain local and are not release
artifacts. These per-case a2 results replace the earlier file/group measurements.

## Scoped execution at 128 cases

Version 0.1.0a3 was measured on 2026-10-19 using a newly generated synthetic project:
128 parametrized cases in 16 modules, two independent pytest suites and two static local
distributions. Each case loads its package through a real metadata entry point, performs
deterministic computation and reads its own declared JSON resource. Metadata is projected
without building or installing application source into the runtime. The ordinary baseline
runs the two suites separately with temporary generated metadata.

```sh
python benchmarks/scaling.py --output /tmp/telescope-scaling --cases 128 --parallel 4
```

Each worker has one CPU, 512 MiB memory, 128 processes and 64 MiB private scratch.
The one-worker aggregate envelope equals those dimensions. The four-worker envelope is
four CPUs, 2 GiB memory, 512 processes and 256 MiB scratch. Concurrency comes from these
explicit budgets, not the host CPU count. Every fresh case still starts its own pytest
session and container. Cold phases force all cases to execute. Each selective phase
uses previously unseen content in one case's JSON input; reverting to already proven
content can correctly reuse an older successful cache record.

This single complete trial used the installed 0.1.0a3 wheel, host Python 3.12.3 and
pytest 9.1.1. Times are seconds. Ordinary execution of both suites took **1.129 s**
(1.048 s host process CPU). Runtime preparation took **0.914 s** separately and benefited
from existing Docker layers.

| Worker budget | Phase | External elapsed | Host process CPU | Worker CPU | Fresh / reused cases |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | Cold | 99.985 | 14.234 | 32.446 | 128 / 0 |
| 1 | Warm | 0.414 | 0.378 | 0.000 | 0 / 128 |
| 1 | One resource changed | 2.167 | 0.616 | 0.727 | 1 / 127 |
| 4 | Cold | 34.724 | 14.501 | 33.530 | 128 / 0 |
| 4 | Warm | 0.414 | 0.378 | 0.000 | 0 / 128 |
| 4 | One resource changed | 2.117 | 0.617 | 0.721 | 1 / 127 |

All phases passed exact case identity, suite/file membership, complete-inventory, resource
envelope and outcome checks. Selective phases reran exactly the changed resource's case.
Warm phases reused collection and all cases with zero current worker CPU. Collection
refreshed after resource changes without discarding unrelated case successes. The
benchmark removes its own runtime image and retains command logs, generated source and
per-phase timing receipts locally.

Parallelism reduced cold elapsed time in this fixture, but both cold modes were much
slower than ordinary suite execution. Fresh container/session startup and lifecycle work
dominate these small cases. This was one trial on a shared development host with other
qualification activity during part of the trial, not an exclusive-host or repeated
statistical study. The columns exclude Docker daemon CPU and do not establish universal
speedups, linear scaling, or performance for a different workload. The
[structured scoped measurements](https://github.com/xu-hao/pytest-telescope/blob/main/benchmarks/results/scoped-linux-docker.json)
retain exact budgets, counts, inventory digests and timings.
