# Local development

Use a dedicated Python 3.11+ virtual environment. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]' 'pytest==9.2.0'
mkdir -m 700 /tmp/telescope-check-scratch
export TMPDIR=/tmp/telescope-check-scratch
python scripts/check.py --artifacts /tmp/telescope-check
```

The check command runs Ruff lint and formatting checks, builds the sdist and a wheel
from it, validates package metadata, builds documentation with warnings as errors,
installs the built wheel without dependencies, verifies that installed candidate,
and runs the behavioral tests and benchmark-harness unit
checks. It requires a virtual environment and uses its interpreter throughout. The
artifact directory must not already exist; use a new directory for another run. Build
dependencies may need network access. No package is uploaded by this command. Each stage
has a five-minute deadline, except the behavioral suite, which has fifteen minutes.
A timeout fails the gate.

## Full qualification and core compatibility

`check.py --functional-profile all` is the default and the only full qualification,
release or guarded-landing verdict. It runs every functional test, including the real
fixture-guard positives. Those positives require an actual supported adapter: pytest
9.1.1 or 9.2.0. An unsupported actual version refuses this profile before test bodies;
the gate does not silently choose a smaller suite.

`check.py --functional-profile core` is an explicit compatibility or diagnostic run.
It retains all core tests, version-independent guard checks and actual unsupported-adapter
refusal controls. It deselects only real positive cases marked `requires_fixture_guard`.
It never skips or marks those cases as expected failures, and its result cannot stand
in for a full verdict. The CI matrix selects `core` for pytest 8 and pins pytest 9.2.0
with `all`; the publishing workflow also requires `all`.

Apply that marker to the smallest case or parameter that actually needs a supported
invoking adapter. This includes real capture/inspection refusal controls and accounting
checks whose fixture first runs an admitted guarded project. Keep pure protocol,
converter and callback controls, invalid declarations rejected before adapter admission,
ordinary/default behavior and unsupported-version refusal tests in `core`. A combined
tool-link test marks only its guarded parameter; its ordinary parameters remain core.
Collection receipts, rather than a static function count, establish the exact partition.

Both `functional_gate.py` modes require the same explicit `--functional-profile`
argument. The `tests` mode additionally requires `--membership` naming a new owned
artifact file; `harness` mode does not accept that artifact argument. Before any test
body, the actual pytest collection records a bounded, exclusively created schema 1
membership object with exactly `schema`, `profile`, `pytest_version`, `selected` and
`deselected`. The last two fields contain complete node-ID lists, not only counts.
The `all` profile admits no deselections. The `core` profile admits exactly its marked
positive cases as deselected; an ambient selector or other unexplained deselection
refuses before execution rather than being credited to the profile.

The final gate result records `functional_profile` and `verdict_kind`: `full` for
`all`, `compatibility` for `core`. A successful `core` result remains compatibility
evidence. The candidate binding remains schema 2 with its existing exact fields;
profile selection is invocation and stage evidence, not a new product or binding field.

Use new, owned scratch and artifact paths for each attempt. The example above creates
the scratch directory before the gate starts; remove that directory after the gate has
terminated and its owned children have been cleaned. The outer job must still enforce
its explicit CPU, memory, scratch-byte and inode limits. When a resource coordinator
already provides an existing absolute `TMPDIR`, retain that claimed directory instead
of assigning a different location.

## Installed candidate and functional prerequisites

This is the gate contract for the unreleased candidate. The default gate requires the
ordinary Python/pytest/Bubblewrap runner and build/documentation dependencies. It does
not accept `--node`, select a program installation, prepare the `node` or `installed`
profiles, or dispatch compatibility benchmarks. Those actual program-qualification
cases are selected and prepared explicitly through the
[benchmark path](benchmark.md#program-and-installed-distribution-migration-ledger).

The outer job must provide an existing absolute `TMPDIR` and explicit resource limits.
All subprocess scratch and temporary test inputs stay below
that actual caller-provided directory. The new artifact directory holds the complete
logs, final result and candidate evidence; it is not a substitute scratch location.
Automated callers must provide these same prerequisites explicitly. A hosted runner's
temporary directory can supply `TMPDIR` when it exists and belongs to that job.
Missing runner prerequisites must stop
before any build, installer or qualification stage. Workflow-entry unit controls load
the actual workflow invocation and exercise gate preflight with owned interpreter,
and stage doubles. They do not qualify hosted actions or run an installed gate.

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
`pytest_telescope` package. The explicit installed-plugin benchmark still supports its normal
explicitly pinned bytecode, including its thirteen selected `.pyc` files, and verifies
those actual bytes under the [installed-Python contract](bubblewrap-api.md#installed-python-distribution-records).
Isolated Python child commands that can import the candidate must pass `-B` explicitly:
`-I` ignores the inherited `PYTHONDONTWRITEBYTECODE` environment setting. These controls
preserve strict installed-package verification; deleting or ignoring generated producer
bytecode after a test is not a successful qualification.

The new candidate binding uses schema 2 with exactly `schema`, `root`, `source`,
`wheel_path`, `wheel` and `interpreter`. It binds source HEAD/tree, complete wheel
identity and the exact invoking interpreter. There is no Node field. Old schema 1,
missing fields and extra fields refuse; there is no compatibility fallback.
After building and installing the candidate, the gate writes a fresh binding and passes
its exact SHA-256 and owned scratch to `functional_gate.py` in `tests` and `harness`
modes. There is no default `prepare` mode, prerequisite receipt or admission argument.

Validate source, wheel, installed membership/version/import origin and interpreter
before importing the producer and again after each stage. A changed source or substituted
installation invalidates the gate even when the test process exits successfully.
Explicit benchmark preparation retains its existing named profiles and exact hashed
receipts; those are separate evidence and cannot stand in for the candidate guard.

Functional pytest has a 900-second deadline. Capture each stage's
complete output once, with an 8 MiB bound; overflow fails rather than silently truncating
successful evidence. On timeout or failure, stop dependent stages, terminate owned
processes with a ten-second TERM grace followed by a ten-second KILL/reap bound, and
clean up only owned scratch. Record one exclusive complete log and result per attempt;
cleanup failure also prevents a successful verdict. Retrying requires a new artifact
directory and a fresh candidate binding. There is no silent fallback, prerequisite skip or
installer called from a test.

Actual [browser execution](benchmark.md#browser-execution-benchmarks) remains outside
this functional gate. The separate benchmark-harness unit checks
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

Functional tests use portable controlled inputs. Their ordinary Python, pytest and
Bubblewrap runner dependencies remain normal test infrastructure; using those programs
does not itself make a test a compatibility benchmark. Classify a case by its purpose
and inputs. A case whose purpose is to qualify a selected host compiler/toolchain,
native loader/ABI, derived preload, installed command or actual checkout/install layout
belongs to a separately selected
[compatibility benchmark](benchmark.md#portable-inputs-and-host-qualification).
Naming, copying or pinning that installation does not establish portability. Such
qualification is not a functional prerequisite. Actual browser execution is always a
separately invoked [benchmark](benchmark.md#browser-execution-benchmarks), including an
owned page with explicit runtime inputs. It is not a functional readiness requirement.
Pinning whatever a developer happens to have installed does not make that installation
a portable fixture.
Host-dependent runtime compatibility checks run separately under
[`benchmarks/frozen_runtime`](benchmark.md#installed-runtime-compatibility-benchmarks),
outside the default functional gate. Portable frozen-input declarations, data views,
identity, transport, error handling and cleanup remain functional controls. An ordinary
program dependency is not a blanket exemption or a blanket ban: assess the actual
case's purpose and whether its asserted behavior has portable owned inputs. Deterministic
doubles must be identified as such; they do
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
model only; it does not establish that CPython starts. A claim about startup of a selected
native worker requires its actual invoking interpreter description and explicitly pinned
runtime, loader cache and pytest dependencies. That qualification claim cannot be
replaced by a double. Adding a dynamic worker runtime to a static-tool test
cannot prove that the static tool alone caused the native profile to activate.

### Derived native witnesses

A tiny owned C source does not make a selected host compiler toolchain portable.
Compiling it against explicit GCC subtools and runtime libraries, then observing an
owned scratch library in a finite-environment child, is an explicit benchmark witness.
Keep its exact source, command flags, tool/runtime closure, symbol/value assertion,
output bounds and cleanup plan together. The child may explicitly name its own derived
scratch output after startup; captured ambient `LD_*` declarations remain invalid and
the fixed empty readonly `/etc/ld.so.preload` rule is unchanged. Such a witness measures
only the actual exercised path, not ABI compatibility or a static proof for future bytes.

An inventory or dry run is planning evidence. An unexecuted compile/load cell remains
unrun and does not block functional qualification. Preserve historical failures and
receipts when reclassifying work; do not relabel them as benchmark passes. A benchmark
campaign requires its own explicit selection and is not started by the default gate.

## Controlled program qualification

The explicitly selected Node and installed-plugin benchmarks use the actual invoking worker runtime
alongside explicitly selected program files. Preparation records every selected
file's hash, type and mode in a bounded private receipt. A check compares its initial
declarations against that exact receipt before starting the controller. A missing or
changed prerequisite fails preparation or the check; it does not skip the control,
download a replacement, or widen the filesystem view.
The two-profile preparation path selects Node and installed-plugin profiles. It does not
select a browser executable or browser resources. Browser preparation is requested
explicitly for a separate benchmark and has its own receipt and verdict. A profile's
name and a matching receipt do not classify its cases as functional. All five Node/program
nodes and nine installed-distribution nodes listed in the
[migration ledger](benchmark.md#program-and-installed-distribution-migration-ledger)
remain explicit compatibility benchmarks. Preserve their real assertions and dependencies
when selecting them separately; their execution is not required by the default gate.

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

The CI matrix covers core compatibility on pytest 8 and complete functional
qualification on the pinned supported pytest 9 adapter, with Python 3.11/3.13 Linux
runners and Bubblewrap installed. Core compatibility is labelled separately and cannot
be reused as the full release or guarded-landing verdict. Keep build and documentation
tools in development dependencies.
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
