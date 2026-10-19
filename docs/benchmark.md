# Reproducible benchmark

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
| Ordinary pytest | 0.916 | 0.913 | — | — | — |
| Runtime preparation | 0.414 | 0.262 | — | — | — |
| Cold isolated | 4.872 | 0.989 | 2.498 | 0.205 | 2.286 |
| Warm unchanged | 0.164 | 0.191 | 0.000 | 0.000 | 0.000 |
| Scoring Python change | 2.368 | 0.583 | 1.090 | 0.205 | 0.891 |
| Demand JSON change | 2.318 | 0.582 | 1.083 | 0.200 | 0.883 |
| Single-case JSON change | 1.867 | 0.485 | 0.905 | 0.204 | 0.702 |
| Shared helper change | 4.821 | 0.990 | 2.480 | 0.206 | 2.280 |

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
cold-minus-ordinary elapsed delta is 3.956 seconds, measuring complete different
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
