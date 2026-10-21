# Benchmark

## Plain pytest versus Bubblewrap scoped execution

This measurement compares the same synthetic workload run as plain pytest, through the
pytest-telescope ordinary route, and as scoped cases in Bubblewrap sandboxes. Its purpose
is to show what the filesystem view costs, not to claim a universal speedup.

| Property | Rule |
| --- | --- |
| Workload | The 16-case project in `benchmarks/workload`, identical on every path. Scoped runs differ only in one module-level `pytest.mark.telescope(bundles=[...])` line per test module. |
| Freshness | Every measurement uses a new copy of the project and no result reuse: plain pytest has no cache and pytest-telescope runs with `--no-cache`. |
| Series | `plain` (`python -m pytest`), `ordinary` (`pytest-telescope run` on the unmarked copy), `scoped-case` and `scoped-batch` (`run --execution-mode batch`). |
| Workers | 1, 2 and 4 for every pytest-telescope series. Plain pytest is one process. Ordinary sessions run one per suite regardless of `--workers`; their rows show that setting has no effect. |
| Outer limits | Every measurement runs under the same caller-imposed CPU, memory and scratch limits. pytest-telescope adds none. |
| Measurements | Wall time and CPU of the whole invocation, measured from outside, for every sample; medians, ranges and standard deviation. |
| Repetitions | Five by default, interleaved: each repetition runs every configuration once, in a rotated order. |

The workload mixes four CPU-bound cases (pure-Python hashing chains and Collatz searches),
four import-heavy cases (each imports a different set of large standard-library modules)
and eight light file-reading cases. All results are fixed and asserted, so a measurement
only counts when all 16 cases pass, every case runs fresh on the intended route, and
scoped runs use the intended case or batch mode.

### What is measured

Wall time is the elapsed time of the whole invocation, from process start to exit, so it
includes interpreter startup, planning, the interpreter probe and Bubblewrap preflight,
collection, sandbox setup, the test sessions and cleanup. CPU time is the user plus
system time of the invocation's whole process tree, read with `RUSAGE_CHILDREN` by the
measuring process that waits for it. That account is complete: the ordinary session, the
probe, every `bwrap` process and each sandboxed worker are descendants that their parents
wait for, the worker reaps its own descendants as PID 1 of its sandbox, and no work runs
in services outside the tree. When the measuring process runs in its own cgroup v2, the
cgroup's `cpu.stat` usage over the same interval is recorded as a cross-check.

The measuring process blocks on the invocation's exit rather than polling for it, so the
wall time carries no polling granularity. An invocation that exceeds the deadline is killed
and its measurement fails.

pytest-telescope's report adds the product's own phases where it exposes them: the
controller's elapsed and setup time, sandboxed collection time, ordinary session time,
scoped session time and the number of sandboxes started.

### Reproduce

Use a Linux host where `bwrap` can create user, mount and PID namespaces, and a virtual
environment with this checkout installed. From the repository root:

```sh
python benchmarks/run.py environment --output /tmp/telescope-benchmark --label my-host
python benchmarks/run.py plan --output /tmp/telescope-benchmark --repetitions 5 > plan.txt
# Run every line of plan.txt under the same outer limits, one invocation at a time.
python benchmarks/run.py summarize --output /tmp/telescope-benchmark
```

Each `measure` line copies the workload, runs one invocation, validates its outcome,
writes one JSON row under `rows/` with the command, exit status, wall and CPU time,
cross-check and phases, keeps the invocation's output under `logs/`, and removes the copy.
`summarize` writes `results.json` and `results.md` and exits non-zero if any row is
invalid. `environment` records the operating system, kernel, CPU model and count, memory,
the measuring cgroup's limits, Python, pytest, pytest-telescope and Bubblewrap versions,
and the source revision and whether the checkout had uncommitted changes, without host or
user names.

The harness checks in `benchmarks/test_run.py` cover the workload variants, whole-tree
CPU accounting, outcome validation, the interleaved plan and the summary. They run the
plain and ordinary series for real and need no Bubblewrap:

```sh
python -m unittest discover -s benchmarks -p 'test_*.py'
```

### Results

Run of 2026-10-21 at source revision `8b2eab2` (a clean checkout), with pytest-telescope
0.1.0a4 from this branch. It has five interleaved repetitions. Each invocation ran alone
under the same outer limits of 4 CPUs (`cpu.max 400000 100000`) and 8 GiB of memory. The
host ran Ubuntu 24.04.5 LTS, kernel 6.8.0, on an AMD Ryzen 9 5900X (24 logical CPUs), with
CPython 3.12.3, pytest 8.3.5 and Bubblewrap 0.9.0. All 50 measurements were valid: 16 cases
executed fresh on the intended route and none were reused. The cgroup `cpu.stat`
cross-check agreed with the process-tree CPU to within 15 ms on every row.

| Series | Workers | n | Wall median s (min-max) | CPU median s (min-max) | Wall/plain | CPU/plain |
| --- | ---: | ---: | --- | --- | ---: | ---: |
| plain | 1 | 5 | 0.854 (0.837-0.885) | 0.854 (0.837-0.885) | 1.00 | 1.00 |
| ordinary | 1 | 5 | 1.074 (1.013-1.082) | 1.074 (1.013-1.082) | 1.26 | 1.26 |
| ordinary | 2 | 5 | 1.068 (1.039-1.107) | 1.068 (1.039-1.108) | 1.25 | 1.25 |
| ordinary | 4 | 5 | 1.080 (1.061-1.089) | 1.080 (1.062-1.089) | 1.26 | 1.26 |
| scoped-case | 1 | 5 | 3.922 (3.806-3.953) | 3.917 (3.807-3.936) | 4.59 | 4.59 |
| scoped-case | 2 | 5 | 2.223 (2.180-2.251) | 3.905 (3.850-3.992) | 2.60 | 4.57 |
| scoped-case | 4 | 5 | 1.359 (1.335-1.406) | 3.993 (3.904-4.061) | 1.59 | 4.68 |
| scoped-batch | 1 | 5 | 1.622 (1.615-1.665) | 1.620 (1.611-1.663) | 1.90 | 1.90 |
| scoped-batch | 2 | 5 | 1.264 (1.256-1.298) | 1.634 (1.605-1.670) | 1.48 | 1.91 |
| scoped-batch | 4 | 5 | 1.276 (1.214-1.300) | 1.645 (1.560-1.655) | 1.49 | 1.93 |

pytest-telescope's own phases, as medians over the same five repetitions:

| Series | Workers | Sandboxes started | Setup s | Sandboxed collection s | Scoped session s | Ordinary session s |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ordinary | 1 | 0 | 0.01 | 0.00 | 0.00 | 0.89 |
| ordinary | 2 | 0 | 0.01 | 0.00 | 0.00 | 0.89 |
| ordinary | 4 | 0 | 0.01 | 0.00 | 0.00 | 0.89 |
| scoped-case | 1 | 16 | 0.10 | 0.10 | 1.69 | 0.00 |
| scoped-case | 2 | 16 | 0.10 | 0.10 | 1.69 | 0.00 |
| scoped-case | 4 | 16 | 0.10 | 0.10 | 1.73 | 0.00 |
| scoped-batch | 1 | 3 | 0.10 | 0.10 | 0.90 | 0.00 |
| scoped-batch | 2 | 3 | 0.10 | 0.10 | 0.90 | 0.00 |
| scoped-batch | 4 | 3 | 0.10 | 0.10 | 0.89 | 0.00 |

#### Reading the results

- The ordinary route (unscoped tests) costs about 0.22 s over plain pytest on this 16-case
  workload, about 1.25 times the wall time and CPU. It does not depend on `workers`,
  because unscoped tests run in one ordinary session.
- Case mode starts one sandbox per case, 16 here. Its CPU is about 3.9 to 4.0 s at every
  worker count, about 4.6 times plain. The difference over plain is roughly 0.19 s of CPU
  per case, the cost of a fresh sandboxed interpreter and pytest session for each case.
  Workers only spread that work: wall time falls from 3.92 s at one worker to 2.22 s at
  two and 1.36 s at four (1.59 times plain).
- Batch mode (opt-in) starts one sandbox per group of cases with exactly equal scope,
  three here, one per test module. Its CPU is about 1.9 times plain. Wall time is 1.62 s at
  one worker and about 1.27 s at two and four: with three batches, the largest batch
  bounds the wall time once there are two workers.
- These are fixed per-case and per-sandbox costs, so they matter most for short tests.
  For tests that do substantial work, the relative overhead should shrink; that follows
  from the per-case cost above and was not measured.

The complete samples and environment are in `benchmarks/results/2026-10-21-bubblewrap.json`.

## Earlier Docker measurements

Versions 0.1.0a2 and 0.1.0a3 were measured with the Docker backend, using a different
harness and per-worker resource accounting. Those results describe only those releases;
see the [0.1.0a3 benchmark page](https://github.com/xu-hao/pytest-telescope/blob/v0.1.0a3/docs/benchmark.md).
