# Benchmark

This page is for anyone who wants to measure what the sandbox costs, and for
contributors who qualify pytest-telescope against host-specific installations. The first
section compares plain pytest with ordinary and scoped execution on a fixed workload.
The later sections describe compatibility benchmarks, which run only when selected
explicitly.

Contributors qualify host-specific tool installations with the separately invoked
[installed-runtime benchmarks](#installed-runtime-compatibility-benchmarks).
All [actual browser execution](#browser-execution-benchmarks) is benchmark
work outside the [functional gate](development.md), as are the 14 actual Node and
installed-distribution qualification nodes in the
[migration ledger](#program-and-installed-distribution-migration-ledger).
Ordinary Python/pytest/Bubblewrap correctness and portable integration tests remain
functional.

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

The frozen-tool profile also has explicit host-compatibility controls in
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
Pinning bytes is an execution contract, not a test-portability classification. Classify
the actual case's purpose and inputs. A case intended to qualify a selected host
compiler/toolchain, native loader/ABI, derived preload, installed command or real
checkout/install layout belongs to an explicitly selected compatibility benchmark.
Complete pins and owned source do not turn that host qualification into a functional
prerequisite. Ordinary Python/pytest runner dependencies and portable program-backed
integration tests are not reclassified merely because they execute a program.

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

### Program and installed-distribution migration ledger

The following 14 parametrized nodes move intact from default functional discovery to
explicit compatibility benchmarks. The modules retain their complete bodies, parameters,
fixtures, command transport, assertions, mutation controls, output bounds and cleanup.
Their classification changes; earlier receipts and failures remain attached to their
original source. The move supplies no new compatibility result: execution is **unrun**.

| Original module | Explicit benchmark module | Unchanged module SHA-256 |
| --- | --- | --- |
| `tests/test_frozen_tool_programs.py` | `benchmarks/frozen_runtime/test_programs.py` | `c72f3e3a1eb8ebf43102ca90f24127147f1f5b033ffed1e7acc8b0587f09bc37` |
| `tests/test_frozen_tool_installed.py` | `benchmarks/frozen_runtime/test_installed.py` | `65c63a2df0257f279ba6089de4fd35983f09819a877722b344cabfb203ec1c74` |

Each node below keeps its name and parameter suffix under its module's destination above:

| Module | Node |
| --- | --- |
| programs | `test_env_node_unchanged_script_arguments_exit_signal_and_warm_reuse` |
| programs | `test_command_selection_change_invalidates_only_consumer` |
| programs | `test_env_node_conflict_refuses_before_project_import[missing_command]` |
| programs | `test_env_node_conflict_refuses_before_project_import[path_override]` |
| programs | `test_changed_driver_refuses_stale_pin_without_reuse` |
| installed | `test_normally_installed_plugin_metadata_resources_and_bytecode_in_child` |
| installed | `test_changed_installed_bytes_refuse_stale_pins[module]` |
| installed | `test_changed_installed_bytes_refuse_stale_pins[record]` |
| installed | `test_changed_installed_bytes_refuse_stale_pins[bytecode]` |
| installed | `test_installed_metadata_inconsistency_refuses_before_project_import[missing_resource]` |
| installed | `test_installed_metadata_inconsistency_refuses_before_project_import[missing_metadata]` |
| installed | `test_installed_metadata_inconsistency_refuses_before_project_import[version_conflict]` |
| installed | `test_installed_metadata_inconsistency_refuses_before_project_import[record_hash_conflict]` |
| installed | `test_updated_installed_resource_invalidates_consumer_only` |

The five program nodes retain `ProgramConfiguration(WorkerConfiguration(...))`,
`capture_evidence`, the actual env/Node shebang and original/logical aliases, isolated
Python child, exact empty/space/literal arguments, executable hash, command symlink and
readonly directory, exit 23 and SIGTERM, build output, cold/warm reuse, consumer-only
invalidation, preimport conflicts and stale-pin cache preservation.

The nine installed nodes retain `InstalledConfiguration`, `capture_evidence`, normally
installed pluggy package/dist-info/RECORD and usable bytecode, exact version and import
locations, real hook answer 12, resource bytes, readonly modes and distinct frozen inode,
isolated child, Furo metadata baseline/native absence, stale-pin cache preservation,
preimport inconsistencies and consumer-only resource mutation. Owned copies and source
routing remain labelled; real child/plugin execution is not replaced by a double.

Shared helpers and payloads remain in `tests/`; the existing benchmark conftest supplies
their import path without changing the product import path. Preparation uses the existing
named profiles and exact fresh receipt. Select an existing Node executable explicitly,
then prepare and review the chosen inputs before the benchmark:

```sh
export TELESCOPE_FIXTURE_NODE=/absolute/path/to/node
python tests/frozen_tool_prepare_programs.py --profile node --profile installed \
  --directory "$TMPDIR/program-preparation" --output "$TMPDIR/program-prerequisites.json"
# Review the receipt and set TELESCOPE_FIXTURE_ADMISSION to its exact path,
# and TELESCOPE_FIXTURE_ADMISSION_SHA256 to its SHA-256, before explicit execution.
python -m pytest -q benchmarks/frozen_runtime/test_programs.py \
  benchmarks/frozen_runtime/test_installed.py --basetemp "$TMPDIR/program-benchmark"
```

Preparation inventories but does not run the programs or install a dependency. Before
reporting readiness, the existing preparation helper validates the exact selected profile
set, each profile's required documents, the real strict tool-manifest schema and the
recomputed declaration identity. Receipt reads are bounded to 4 MiB and reject duplicate
keys, nonfinite values, wrong types and changed hashes. Consumers retain the exact
selected-profile identity/hash check. These controls use the explicit preparation path;
they are not coupled to the default candidate gate. Missing or changed prerequisites refuse;
do not discover a replacement, install, download, skip a node or reuse stale readiness.
Use owned scratch, explicit caller resources and finite deadlines. The default gate
does not require these profiles or receipt and does not dispatch these nodes. A bounded
collect-only check can establish node membership; it is not benchmark execution.

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

Shared driver and runtime-declaration helpers remain in `tests/` for portable helper
controls and explicit benchmarks. Storing or pinning browser-driver bytes does not execute
a browser. All program-profile preparation is explicitly selected; the default functional
gate prepares neither Node nor installed-plugin profiles. Private historical check wrappers
and receipts retain their original paths and verdicts; they are not current requirements
of the functional gate or executable migration instructions.
