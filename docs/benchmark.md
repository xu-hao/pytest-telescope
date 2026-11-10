# Benchmark

## Plain pytest versus Bubblewrap scoped execution

This benchmark measures the cost of starting a sandbox for each test. It runs the same
synthetic workload through plain pytest, pytest-telescope's ordinary route, and scoped
Bubblewrap execution. Every test runs fresh, so the results measure execution overhead.
To measure the benefit of caching, also compare cold and warm runs of your own suite.

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

The measuring process waits for the invocation to exit without polling. Its wall-time
measurement therefore has no polling delay. If an invocation exceeds the deadline, the
harness kills it and rejects the measurement.

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

For your own suite, record executed and reused case counts alongside cold and warm
timings. A warm run can finish sooner because it reuses results; those cases have not
run again. The balance between execution overhead and reuse depends on your tests and
how often their inputs change.

## Installed-runtime compatibility benchmarks

The unreleased frozen-tool profile also has explicit host-compatibility controls in
`benchmarks/frozen_runtime`. They exercise a real native library, unchanged env/Node
commands, full Chromium navigation and clicking through an isolated CLI child, and a
normally installed Python distribution in an isolated child. The small C sources, wheel
and browser page are owned fixtures. Their compiler, installed Node/browser, glibc loader
cache, stdlib and native-library layout still depend on the selected host, so these
controls are invoked separately from the default functional suite.

Supply actual installed executable paths; the harness neither downloads a browser nor
substitutes a headless-shell binary for full Chromium:

```sh
TELESCOPE_TEST_NODE=/absolute/path/to/node \
TELESCOPE_TEST_CHROMIUM=/absolute/path/to/chrome \
python -m pytest -q benchmarks/frozen_runtime --basetemp /tmp/telescope-runtime-benchmark
```

Use a new owned temporary directory and explicit caller resource limits for each run.
The fixture names every source root and byte pin, retains actual link and command
semantics, closes native library search directories, and refuses unsupported layouts.
The native configuration contract also requires explicit frozen configuration inputs;
host DNS, certificate and font settings are not implicitly available through `/etc`.
The authored compatibility controls require qualification against that contract before
any successful-runtime claim; earlier ambient-preservation evidence does not qualify it.
Missing prerequisites and failed required controls are failures, never skips or evidence
that the runtime is supported. Record the selected source revision, executable and
closure digests, complete output, resource receipt and observed outcomes with each
benchmark. Report compatibility only for the actual closure exercised; these checks do
not establish arbitrary installed-runtime support or a portable performance result.

### Portable inputs and host qualification

The default functional suite uses portable owned fixtures to test declarations,
protocols, ownership, environment composition, cache identity and lifecycle behavior.
Pinning bytes is an execution contract, not a test-portability classification. A fixture
whose result depends on the selected host compiler, native loader, preload behavior,
installed command or real checkout/install layout belongs to an explicitly selected
compatibility benchmark. Complete pins and owned source do not turn that host
qualification into a functional prerequisite.

Keep a benchmark's real oracle and required dependencies intact, including output and
cleanup bounds. Metadata or planning probes establish only their observed planning
facts. A benchmark left unrun supplies no runtime or performance verdict, and moving it
out of functional discovery does not turn a historical failure into a pass. The default
gate still runs the benchmark harness's portable correctness tests without dispatching
the host-qualification campaign.

One planned derived-library witness uses a header-free owned C function, selected
compiler/subtools with explicit flags and runtime pins, and a finite-environment Python
child that observes the expected symbol value from owned scratch output. Its compiler,
loader and derived-load assertions remain benchmark work. It has no compile/load
qualification result until explicitly executed; a compiler inventory or dry run is not
that result. See [derived native witnesses](development.md#derived-native-witnesses).

## Browser execution benchmarks

Actual browser execution belongs to separately invoked benchmarks, even when the page,
driver and runtime closure are explicit owned inputs. The default functional suite and
its prerequisite preparation must not launch or require a browser. Portable frozen-input,
transport, lifecycle, error and cleanup controls remain functional. Program and installed
plugin fixtures follow the same portable-input rule above. Such controls do not claim
browser UI coverage.

The controlled Chromium case retains the real env/Node driver, isolated Python CLI child
and CDP pipe. Its oracle requires navigation, a real mouse click that updates the DOM,
argument preservation, unchanged driver bytes and warm result reuse. It uses full
Chromium; a different headless-shell executable does not qualify that case. Keep all
existing executable selections, flags, assertions, output bounds and cleanup when
invoking it separately. Missing prerequisites or a failed browser startup fail that
benchmark; they do not become skips, functional failures or permission to widen the
sandbox. Preserve previous failures separately from new benchmark results.

Prepare a browser-only receipt with the exact interpreter and checkout that will run
the benchmark. Supply `TELESCOPE_FIXTURE_NODE` and `TELESCOPE_FIXTURE_CHROMIUM` as explicit
installed executable paths. Use a new owned preparation directory and receipt path
inside caller-provided scratch; preparation inventories files but launches no browser.
Review the complete receipt and bind its SHA-256 through `TELESCOPE_FIXTURE_ADMISSION`
and `TELESCOPE_FIXTURE_ADMISSION_SHA256` before invoking the case:

```sh
python tests/frozen_tool_prepare_programs.py --profile browser \
  --directory "$TMPDIR/browser-preparation" --output "$TMPDIR/browser-prerequisites.json"
# Review the complete receipt; set its exact path and SHA-256 admission variables.
python -m pytest -q \
  benchmarks/frozen_runtime/test_browser.py::test_full_chromium_real_navigation_click_through_isolated_cli \
  --basetemp "$TMPDIR/browser-benchmark"
```

Each invocation needs explicit caller resource limits, a finite deadline, complete
bounded private output and cleanup of only its owned resources. Do not reuse an old
receipt after the interpreter, selected program bytes or fixture changes. This is a
compatibility benchmark; no timing comparison or broad runtime-support claim follows
from a successful case. It is not included by `scripts/check.py` or default pytest
collection. The benchmark harness's separate unit checks remain part of the gate.

### Browser case migration ledger

| Case | Previous path | Current path | Classification and reason |
| --- | --- | --- | --- |
| Controlled full Chromium navigation/click through an isolated CLI | `tests/test_frozen_tool_programs.py::test_full_chromium_real_navigation_click_through_isolated_cli` | `benchmarks/frozen_runtime/test_browser.py::test_full_chromium_real_navigation_click_through_isolated_cli` | Moved out of functional discovery because it executes a real browser. Preserve the entire oracle and bounded failure observations. Its prior sandbox-startup failure remains a failure; no successful browser result is implied. |
| Earlier installed-runtime Chromium navigation/click | `benchmarks/frozen_runtime/test_native.py::test_full_chromium_navigation_and_click_through_isolated_cli_child` | Same path | Already a separately invoked host-compatibility benchmark. No test movement or new qualification is claimed. Its previously unexecuted status is unchanged. |

Shared driver and runtime-declaration helpers remain in `tests/` because functional
Node controls use them too. Storing or pinning browser-driver bytes does not execute a
browser. Browser-specific runtime preparation is opt-in; ordinary functional preparation
selects only the Node and installed-plugin profiles. Private historical check wrappers
and receipts retain their original paths and verdicts; they are not current functional
readiness requirements or executable migration instructions.
