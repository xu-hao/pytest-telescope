# Local development

Use a dedicated Python 3.11+ virtual environment. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]'
python scripts/check.py --node /absolute/path/to/node --artifacts /tmp/telescope-check
```

The check command runs Ruff lint and formatting checks, builds the sdist and a wheel
from it, validates package metadata, builds documentation with warnings as errors,
installs the built wheel without dependencies, verifies that installed candidate, prepares
its functional prerequisites, and runs the behavioral tests and benchmark-harness unit
checks. It requires a virtual environment and uses its interpreter throughout. The
artifact directory must not already exist; use a new directory for another run. Build
dependencies may need network access. No package is uploaded by this command. Each stage
has a five-minute deadline, except prerequisite preparation and the behavioral suite,
which each have fifteen minutes. A timeout fails the gate.

## Installed candidate and functional prerequisites

This is the gate contract for the unreleased frozen-tool candidate. Select an existing
Node executable with an absolute `--node` path or `TELESCOPE_FIXTURE_NODE`; an explicit
`--node` takes precedence. Missing, relative or unusable selections refuse. The gate
does not discover Node through `PATH`, download it, or select a replacement. For example,
the equivalent environment-based invocation is:

```sh
TELESCOPE_FIXTURE_NODE=/absolute/path/to/node \
  python scripts/check.py --artifacts /tmp/telescope-check-next
```

The outer job must provide an existing absolute `TMPDIR` and explicit resource limits.
All preparation copies, subprocess scratch and temporary runtime declarations stay below
that actual caller-provided directory. The new artifact directory holds the complete
logs, final result and prerequisite evidence; it is not a substitute scratch location.

After building and installing the exact candidate wheel with the gate interpreter,
a standard-library-only guard verifies the candidate before importing it. The guard
compares the complete package file membership and bytes against both bounded wheel
members and the current checkout's `src/pytest_telescope`. The installed package must
resolve under that interpreter's actual site-packages, with the distribution version
matching the wheel and source project. Missing, extra, changed or unverifiable package
members refuse. An editable install, checkout import or unrelated installed copy cannot
stand in for the candidate wheel.

The guard bounds wheel/package membership at 4,096 entries, each file at 16 MiB and
total package or wheel content at 64 MiB. Gate evidence is bounded at 4 MiB. Duplicate
or noncanonical wheel paths, links and special files refuse. Owned package/ZIP controls
exercise these guards with explicitly identified source, distribution and origin
doubles; separate controls use real short child processes for deadlines and cleanup.
Those unit controls do not establish that the candidate wheel was installed. Only the
actual installed-candidate gate can provide that evidence.

Install with `--no-compile`, disable bytecode writes during qualification, and reject
residual `.pyc` files in the installed producer package. Otherwise cached bytecode could
bypass verification of its Python source bytes. This guard is specific to the candidate
`pytest_telescope` package. The installed-plugin fixture still supports its normal
explicitly pinned bytecode, including its thirteen selected `.pyc` files, and verifies
those actual bytes under the [installed-Python contract](bubblewrap-api.md#installed-python-distribution-records).

Only after that guard succeeds does the same interpreter perform fresh metadata-only
preparation for exactly the `node` and `installed` profiles. Preparation records the
selected inputs without starting declared programs or running plugin hooks or tests. The prerequisite
receipt retains schema 1 and its existing profiles structure. A separate gate binding
records the source HEAD and tree, exact wheel hash, interpreter identity, prerequisite
receipt path and SHA-256. It does not extend or reinterpret the tool manifest or receipt
schema. Neither a source-snapshot receipt nor an old three-profile receipt qualifies a
new installed candidate.

Validate the complete receipt and binding before functional pytest starts. Check source
identity, installed import origin and bytes, interpreter identity, and receipt integrity
before and after preparation and functional execution. A changed source, substituted
import or changed prerequisite invalidates the gate, even when the test process exits
successfully. A preparation pass alone is not a functional verdict; the
[controlled program cases](#controlled-program-qualification) still have to pass.

Preparation and functional pytest each have a 900-second deadline. Capture each stage's
complete output once, with an 8 MiB bound; overflow fails rather than silently truncating
successful evidence. On timeout or failure, stop dependent stages, terminate owned
processes with a ten-second TERM grace followed by a ten-second KILL/reap bound, and
clean up only owned scratch. Record one exclusive complete log and result per attempt;
cleanup failure also prevents a successful verdict. Retrying requires a new artifact
directory and fresh preparation. There is no silent fallback, prerequisite skip or
installer called from a test.

Actual [browser execution](benchmark.md#browser-execution-benchmarks) remains outside
this functional gate and its preparation. The separate benchmark-harness unit checks
remain required; they do not dispatch the real-browser benchmark. A functional gate
does not establish browser navigation, clicking, layout or rendering coverage.

The behavioral suite needs Linux and a `bwrap` executable that can create user, mount and
PID namespaces. Tests that launch Bubblewrap are required controls: they are never
skipped or marked as expected failures. On a host whose security policy denies those
namespaces, they fail with the `sandbox:namespaces` refusal, and the gate fails. Do not
weaken the host policy to work around that; run the gate on a host that permits
Bubblewrap. Ordinary routing, declarations, planning, the interpreter probe and refusal
classification are tested without launching Bubblewrap and pass on any Linux host.

Behavioral tests cover what a scoped test can observe: declared files present at
`/project` and at the project's host path, undeclared project files absent at both, the
host interpreter and installed packages usable but read-only, isolated child interpreters
importing declared source through editable installs (including an environment inside the
project, without modifying the frozen copy), writes outside private scratch refused,
fresh scratch per sandbox, and descendants. They also cover ordinary routing staying fresh
and unsandboxed, case and batch modes, parallel workers, exact cache invalidation
(declared inputs, interpreter, installed distributions and the environment's references
into the project), reuse across identical checkouts, new test inventory,
corrupt/concurrent records, deadlines, retry exhaustion and cleanup. Every test owns and cleans up its projects. A test that
simulates a host refusal with a stand-in `bwrap` says so in its name. Avoid tests that
assert source code or documentation text instead of observable behavior.

Functional tests use controlled inputs. An owned native tool integration belongs in that
suite only when its relevant runtime and input closure are explicit and the result does
not depend on nonportable ambient installations. Actual browser execution is always a
separately invoked [benchmark](benchmark.md#browser-execution-benchmarks), including an
owned page with explicit runtime inputs. It is not a functional readiness requirement.
Pinning whatever a developer happens to have installed does not make that installation
a portable fixture.
Host-dependent runtime compatibility checks run separately under
[`benchmarks/frozen_runtime`](benchmark.md#installed-runtime-compatibility-benchmarks),
outside the default functional gate. Controlled Node and installed-plugin execution,
frozen-input declarations, data views, identity, transport, error handling and cleanup
remain functional controls. Deterministic doubles must be identified as such; they do
not establish browser navigation, clicking, layout or rendering coverage.

The earlier unlanded ambient-configuration preservation controls are superseded by the
[declared configuration contract](bubblewrap-api.md#declared-native-system-configuration).
Their recorded failures remain historical evidence; removing the superseded mechanism
does not turn those failures or unexecuted proofs into passes.

Replacement functional coverage must exercise actual runtime validation, capture,
projection and Bubblewrap execution with owned configuration fixtures. It must show
exact frozen bytes and modes, readonly access, undeclared and unselected sibling
absence, pinned loader-cache bytes and the fixed empty `0444` preload file. Required
controls also cover static and dynamic profile selection, consumer-only invalidation,
source/copy mutation, safe links and conflicting mappings, unchanged nonconsumer views,
bounded retry/cancellation, and cleanup before publication. Source/configuration
descriptors must not enter mount setup or children. Startup must use the closed view
before interpreter, site or project imports. Do not inventory real `/etc` or use host
credentials as fixture inputs. Data-mode injection alone does not qualify a native
runtime, nor does a source-snapshot check qualify an installed wheel or ambient runtime.

Static trigger coverage is separate from full worker compatibility. A tiny owned static
ELF can exercise actual runtime classification and a direct-program filesystem view.
A synthetic standard prefix description used by such a test qualifies the startup path
model only; it does not establish that CPython starts. Full worker controls must use
the actual invoking interpreter description and explicitly pin its real runtime, loader
cache and pytest dependencies. Adding a dynamic worker runtime to a static-tool test
cannot prove that the static tool alone caused the native profile to activate.

## Controlled program qualification

The functional Node and installed-plugin controls use the actual invoking worker runtime
alongside explicitly selected program files. Preparation records every selected
file's hash, type and mode in a bounded private receipt. A check compares its initial
declarations against that exact receipt before starting the controller. A missing or
changed prerequisite fails preparation or the check; it does not skip the control,
download a replacement, or widen the filesystem view.
Functional preparation selects only Node and installed-plugin profiles. It does not
require a browser executable, browser resources or a successful browser benchmark.
Browser preparation is requested explicitly for a separate benchmark and has its own
receipt and verdict.

The Node driver keeps its `#!/usr/bin/env node` shebang. Its checks exercise the pinned
command directory, argument boundaries, exit status and signal propagation through an
isolated Python CLI child. The installed-plugin controls copy the normally installed
`pluggy` package and dist-info trees into owned fixture storage, including actual RECORD
and usable pyc files. Their source-routing fixture retains the real import locations;
only the owned copies are mutated. The child runs a real plugin hook and reads actual
metadata and resources without a `.pth` file or `PYTHONPATH` override.
An explicitly named Furo metadata tree supplies the unselected sibling control: a
baseline child sees its actual version, while the native consumer cannot discover it.
Only that metadata is used; the control does not execute Furo.

The separately invoked browser benchmark retains full Chromium, the unchanged Node CLI
driver and its real CDP pipe. Its oracle requires navigation to an owned data-URL page and
mouse input that changes the DOM. It needs no external page, DNS lookup or shared browser
service. Browser resources,
the named font configuration and fonts, Node, native dependencies and the worker runtime
are explicit inputs. The browser fixture permits at most 768 MiB of selected program
bytes, 512 MiB per file and 10,000 entries; the worker fixture retains its separate
256 MiB bound. Owned projections hard-link already frozen copies and grant no live
source siblings. The caller still provides aggregate memory, scratch and time limits.
The independent fixture ELF reader permits at most 16 MiB of dynamic string-table
metadata, matching the producer's existing metadata bound. Unique address/size and
in-file mapping, complete reads, string offsets, termination and length checks still
apply; this does not raise the program file or aggregate byte limits.
Each dynamic tool declaration contains its invoking worker runtime, even when the case
also selects a separate worker tool. The frontend fixture must retain Node's actual
bundled dependencies alongside the worker's system libraries, selecting dependencies
per requesting object under the [bounded ordering proof](bubblewrap-api.md#verified-native-library-ordering).
A common system-library override can be ABI-incompatible and is not a valid substitute.
The invoking Python executable explicitly associates every retained native stdlib
extension through its resources. These declarations still need actual program checks;
metadata preparation and graph admission do not establish ABI compatibility. Its sparse
Node prefix preserves relative `bin`/`lib` lookup paths without exposing live siblings.
The empty local-font search directory is not selected. It contributes no page resource
and stays absent in the closed prefix; its source permissions are never rewritten.

Preparation and the individual controls have separate verdicts. Authoring these tests,
or passing a smaller worker or static-program control, does not qualify the Node,
installed-plugin or browser path. Reclassifying an actual browser case does not turn a
previous failure or an unrun case into a pass. Preserve its program, flags, assertions,
bounded cleanup and historical result when moving it out of functional collection.
See the [browser migration ledger](benchmark.md#browser-case-migration-ledger) for exact
case paths. Browser startup permission failures do not authorize sandbox-policy changes.

For documentation alone:

```sh
python -m pip install -r docs/requirements.txt
python -m sphinx -n -W --keep-going -b dirhtml docs /tmp/telescope-docs
```

The documentation uses [Sphinx](https://www.sphinx-doc.org/),
[Furo](https://pradyunsg.me/furo/), and [MyST Markdown](https://myst-parser.readthedocs.io/).
The pinned versions in `docs/requirements.txt` also appear in the development extra.
Sphinx 8.2 supports the project's Python 3.11 development baseline. `-n -W` makes
unresolved references and other warnings fail the build. The directory-HTML builder
preserves page URLs such as `/benchmark/`. Serve the generated directory to inspect it:

```sh
python -m http.server --directory /tmp/telescope-docs 8000
```

Check the quickstart, sidebar links, search, source links, and mobile navigation in a
real browser after layout changes. Stop the local server when finished. Edit navigation
in the hidden MyST toctree at the end of `docs/index.md`; keep runnable examples first.

The CI matrix covers supported host Python/pytest versions on Linux runners with
Bubblewrap installed. Keep build and documentation tools in development dependencies.
Use the [benchmark](benchmark.md) to compare fresh execution with plain pytest and
Bubblewrap on the same workload.

## Qualification status

Version 0.1.0a6 passed the complete gate with 412 behavioral tests and nine benchmark
harness checks on a host that permits Bubblewrap namespaces. The hosted Python 3.11/3.13
and pytest 8/9 matrix passed on both the pull request and merged source. The published
wheel and sdist matched the reviewed artifacts, a clean wheel installation passed seven
focused CLI controls, and the matching Read the Docs build succeeded. See the
[release](https://github.com/xu-hao/pytest-telescope/releases/tag/v0.1.0a6).

Run the gate again for each candidate change.
