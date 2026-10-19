# Reproducible benchmark

## Workload

The repository's `examples/analytics` project is a synthetic pure-Python analytics
workload. Three declared targets compute customer scores, demand projections, and price
schedules from JSON resources. Each target has two test cases, including a bounded real
computation. A shared helper is a declared input to every target. There are no
artificial sleeps and no external services.

`benchmarks/run.py` copies the example into a fresh output directory and measures
ordinary pytest, runtime preparation, cold isolated execution, unchanged warm execution,
a narrow Python change, a JSON resource change, and a shared helper change. Changes
preserve the tests' assertions while altering real input content. The original example
is never modified. Every test-running phase uses the same six cases.

## Reproduce

Install this checkout into a virtual environment and ensure Docker is available. From
the repository root, with that environment active:

```sh
python benchmarks/run.py --output /tmp/telescope-benchmark --repetitions 3
```

The output directory must not already exist. Each trial keeps its copied project,
per-command logs, ordinary pytest JUnit report, and structured results. The final
`results.json` contains each measurement and phase medians. Runtime setup happens once
per trial; preexisting Docker image layers may speed up that phase. The harness rejects
a run if target identities, executed/reused counts, or the six passing case counts
differ from the expected sequence.

To validate the harness itself:

```sh
python -m unittest discover -s benchmarks -p 'test_*.py'
```

These behavioral tests check outcome validation and real external process accounting.
They do not substitute for the full Docker benchmark.

## Measurements and interpretation

The harness measures elapsed time with an external monotonic clock and host child CPU
with operating-system process accounting. Docker runs workers through its daemon,
outside the CLI's descendant accounting: host controller CPU must never be mislabeled as
total worker CPU. Runtime preparation is reported separately from test runs. Any worker
accounting must identify its actual measurement source.

Cold execution includes snapshot, worker startup, collection, execution, teardown, and
result recording. Warm execution still inventories files and validates reuse. The
difference between ordinary and cold elapsed time is reported as an observed
elapsed-time delta, not as a pure startup measurement, because runtimes may differ.
Expected target outcomes are three executed for cold; three reused for unchanged; one
executed and two reused after the narrow and resource changes; and three executed after
the shared change. Actual case counts must agree with the workload.

## Measured results

Measured on 2026-10-19, with 3 complete trials. Every scenario passed its expected
target identity and case-count checks. Times below are medians in seconds; no artificial
delays are used.

| Scenario | External elapsed | Host process CPU | Worker CPU | Preflight + snapshot | Non-session elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ordinary pytest | 0.916 | 0.899 | — | — | — |
| Runtime preparation | 0.414 | 0.248 | — | — | — |
| Cold isolated | 2.517 | 0.467 | 1.507 | 0.107 | 1.371 |
| Warm unchanged | 0.164 | 0.179 | 0.000 | 0.105 | 0.164 |
| Narrow Python change | 0.965 | 0.270 | 0.516 | 0.105 | 0.573 |
| JSON resource change | 0.915 | 0.275 | 0.486 | 0.105 | 0.553 |
| Shared helper change | 2.517 | 0.468 | 1.508 | 0.107 | 1.364 |

| Scenario | Executed / reused targets | Fresh / cached passing cases |
| --- | ---: | ---: |
| Ordinary pytest | Not applicable | 6 / 0 |
| Cold isolated | 3 / 0 | 6 / 0 |
| Warm unchanged | 0 / 3 | 0 / 6 |
| Narrow Python change | 1 / 2 | 2 / 4 |
| JSON resource change | 1 / 2 | 2 / 4 |
| Shared helper change | 3 / 0 | 6 / 0 |

All trials had zero failed, skipped, or infrastructure-error cases. The counts above
were identical across trials. Runtime preparation is separate from the cold run and
benefited from already cached Docker base/dependency layers; it does not estimate a
first image download on a new host.

`Preflight + snapshot` is the controller-reported time spent validating the
manifest/runtime and constructing snapshots. `Non-session elapsed` is measured external
elapsed time minus summed worker-session durations; it also includes CLI/bootstrap,
container lifecycle, cache handling, and cleanup. These two columns overlap and must not
be added together. Host process CPU excludes the Docker daemon and builder; worker CPU
is reported separately using worker OS accounting. Neither column claims to measure
total daemon resource consumption.

The measured cold elapsed delta relative to ordinary pytest was 1.602 seconds. That is
an observed difference between two execution paths, not a pure startup measurement. Warm
reuse was faster on this workload; cold execution and the shared-helper rerun cost more.
Narrow changes approached the ordinary baseline. The small six-case workload exposes
fixed costs, and these measurements do not predict the benefit for a different suite.

### Measurement environment

- Host: Linux 6.8.0-147-generic, x86_64, 24 logical CPUs.
- Host interpreter: Python 3.12.3, pytest 8.3.5, pytest-telescope 0.1.0a1.
- Worker limits: one CPU and 512 MiB memory per target; pinned Python 3.12 base
  and runtime contents are specified in the [execution contract](execution-contract.md).
- Ordinary pytest disables third-party plugin auto-loading, matching the
  controlled worker plugin policy. This baseline uses host pytest 8.3.5; workers
  use pytest 9.1.1. Host and worker Python builds can also differ. The comparison
  measures these complete paths, not a controlled same-interpreter startup cost.
- Each trial owns its project runtime; immutable image IDs are recorded per
  trial in the structured data. Project ownership can give equivalent runtime
  recipes distinct image IDs.

The [structured
measurements](https://github.com/xu-hao/pytest-telescope/blob/main/benchmarks/results/linux-docker.json)
include every trial, runtime identity, installed runner implementation hash, counts, and
medians. Raw command logs and copied projects remain local to the benchmark output
directory and are not release artifacts.
