# Benchmark

## Plain pytest versus Bubblewrap scoped execution

This measurement compares the same synthetic workload run two ways: as plain pytest, and
as pytest-telescope scoped cases in Bubblewrap sandboxes. Its purpose is to show what the
filesystem view costs, not to claim a universal speedup.

| Property | Rule |
| --- | --- |
| Workload | The same newly written synthetic tests, configuration, selection and assertions on both paths, with identical observable outcomes. |
| Freshness | No result reuse on either path: plain pytest has no cache, and scoped runs use `--no-cache`. |
| Parallelism | 1, 2 and 4 workers: `pytest-telescope run --workers N` and the matching plain pytest parallelism. |
| Outer limits | Both paths run under the same caller-imposed CPU, memory, process and scratch limits. pytest-telescope adds none. |
| Measurements | Wall time and caller-measured CPU of the whole invocation, including every descendant, for each sample; medians, ranges and dispersion. |
| Sessions | Equal session counts are compared separately from one-session-versus-many comparisons, which measure fixture lifetime rather than sandbox cost. |

Results will be added here with their environment, kernel, Bubblewrap version, source
revision and complete samples once the measurement runs on a host that permits Bubblewrap
namespaces.

## Earlier Docker measurements

Versions 0.1.0a2 and 0.1.0a3 were measured with the Docker backend, using a different
harness and per-worker resource accounting. Those results describe only those releases;
see the [0.1.0a3 benchmark page](https://github.com/xu-hao/pytest-telescope/blob/v0.1.0a3/docs/benchmark.md).
