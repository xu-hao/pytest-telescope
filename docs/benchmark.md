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

### Interpret your measurements

Keep the generated environment record with the results and identify the exact
pytest-telescope version and source revision. Compare the median and range for each
series under the same outer limits. Fresh scoped execution starts additional interpreters
and sandboxes, so short tests can cost more than plain pytest. More workers can reduce
wall time while total CPU stays the same or grows. Batch mode can reduce session startup
cost when tests have exactly equal scopes and shared fixture lifetimes suit them.

This harness measures fresh execution only. Measure cold and warm runs of your own suite
separately to assess the benefit of result reuse; a warm result is a reused success, not
fresh test execution. Synthetic workload timings do not establish a speedup for an
existing project.
