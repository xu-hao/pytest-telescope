# Ordinary pytest and Bubblewrap filesystem views

> Per-scope environment inputs, frozen tools, helper ownership and the opt-in fixture
> guard are unreleased 0.1.0a7 interfaces. Published version 0.1.0a6 does not provide
> them. Frozen-checkout/history support is deferred and is not implemented by this candidate.

Tests without a scope declaration run as ordinary pytest every time. Tests with a
declaration run in a Bubblewrap sandbox and can reuse a previous successful result.
The sandbox restricts which project files a test can read. It uses your installed
Python environment and leaves resource limits and network policy to the job running
pytest.

This page defines the development execution contract following the published 0.1.0a6
baseline. See the
[API reference](bubblewrap-api.md) for interfaces and configuration, and the
[result reference](result-schema.md) for schema 4 fields.

## Frozen Git checkout (development contract)

This section preserves a deferred design contract. The current four-component candidate
and published 0.1.0a6 do not implement the selector or frozen-checkout/history capture.
Under the future contract, `git="frozen-checkout-v1"` would scope a case and grant an
owned, integrity-verified read-only Git checkout at `/project` and the existing project
host-path alias. The supported committed source tree, normalized index, HEAD and
reachable local branch and tag history are visible. Untracked files require separate
input declarations. The default without this declaration retains the existing no-Git
view. Actual Git commands such as `rev-parse`, `log`, `show`, `ls-tree`, `diff` and
`status` and `ls-files --stage`, including calls from subprocesses and Python children,
operate on the frozen checkout; writes to it fail. Status reflects the normalized clean
tracked checkout and any separately declared untracked files. External helpers, remotes,
network operations and configuration-dependent behavior are unsupported. A separately created repository in private scratch remains writable.

Version 1 supports clean, nonbare SHA-1 checkouts with committed symbolic local-branch
or detached HEAD, regular tracked files and executable bits, local branches and tags,
and complete reachable objects. Ordinary `.git` directories and validated conventional
linked-worktree indirections are supported. Staged, dirty, deleted, sparse or
filter-transformed tracked state, submodules, excluded tracked namespaces, bare, unborn,
shallow, promisor, alternate, replacement, grafted or unverifiable repositories refuse.
The visible index is normalized from the verified committed tree. Host remotes,
remote-tracking refs, reflogs, unreachable objects, configuration, credentials, helpers,
hooks and live metadata paths are not copied or executed.

Tracked mode `120000` relative leaf symlinks are preserved as links with their exact
link text when their lexical target remains inside the tracked tree and is a regular
tracked file. Parent components are allowed when they remain inside. Link text is at
most 4,096 bytes and existing path depth, length and entry bounds apply. Absolute or
escaping targets, directory or dangling links, link ancestors, chains, cycles and other
indirections refuse. Capture uses bounded no-follow validation of committed blob, live
link and final frozen link with consistent before/after evidence; it never resolves a
link through mutable host paths. Link text, mode and target content bind identity.

Git execution uses the actual system Git resolved from a controlled system PATH. Version
1 supports regular Linux x86_64 ELF64 little-endian glibc Git executables. Its loader and
complete direct and transitive library closure are resolved within fixed system library
directories, captured as owned immutable bytes and modes, and overlaid only in requesting
views. Resolution rejects RPATH, RUNPATH, audit, filter, preload and unsafe dependency
layouts; it does not resolve libraries from the project or invoking environment. The
runtime closure is bounded to 128 files, 64 MiB per file, 256 MiB total and depth 32.
Git, loader, libraries, their original system paths and symlink aliases are bound into
identity and revalidated along with source, paths and links before hits and execution.
Capture commands use the same verified frozen Git implementation and dependencies.
For Git consumers, forwarding `GIT_*`, `SSH_*`, `LD_*`, `DYLD_*`, `HOME`, `PATH`,
`XDG_CONFIG_HOME`, `LANG`, `LANGUAGE`, `LC_*` or `TZ` conflicts with controlled
execution and is invalid; Git uses the C locale and UTC. Non-Git forwarding is unchanged.
This is not a general tool snapshot facility.

Capture bounds are 100,000 visible tracked paths and 500,000 reachable objects,
200,000 walked filesystem and metadata entries, 4,096 selected refs, 64 MiB per
individual source file, Git object or metadata file, and 2 GiB of simultaneously retained
Git capture material. The material limit counts file contents in staging, intermediate
copies, reachable-pack generation and the final snapshot and source, link-target bytes,
and bounded reservations for native-generated index output. Separate retained copies
count separately; hardlinked aliases of the same content count once. Historical objects
streamed without retention are not summed by their uncompressed sizes. This is not a
filesystem allocation quota and does not count directory, inode or slab overhead, all
concurrent case views, other declared-input pools or total job scratch. The separately
bounded runtime closure is described above. Caller jobs remain responsible for actual
CPU, memory, process and scratch limits and accounting.

Reachable history is retained in an owned reachable-only compact pack. Compound pack
files are streamed and bounded by the 2 GiB capture-material aggregate rather than the
64 MiB individual object limit. Copying, hashing and current-tree object bodies are
streamed; each body remains bounded by 64 MiB, and no pack or body stream may exceed
its applicable capture-material aggregate or per-object bound. The SHA-1 object listing is bounded to 20.5 MiB
(500,000 records of at most 41 bytes). All other command output is bounded at 64 MiB.
Complete object and connectivity verification remain required. Paths and link targets
are limited to 4,096 UTF-8 bytes and paths to 64 components. Each command has the lesser
of the invocation timeout and 30 seconds; each complete capture attempt has the lesser
of the invocation timeout and 120 seconds. Limits refuse rather than truncating history.
The exact owned pack representation is deterministic or included in the identity.

Capture and revalidation refuse unavailable, changing, corrupt, partial or unsupported
contexts with bounded evidence and at most two fresh capture attempts. Diagnostics do
not expose raw Git stderr, host configuration, contents or host paths. No mutable host repository is mounted, no
ordinary fallback follows a scoped attempt, and failed cleanup prevents publishing new
pending cache records. Caller resource limits and shared host network policy are
unchanged. The snapshot does not claim full host-system, locale, NSS or network reproducibility.

## Which route a case takes

A case is *scoped* when it has an `inputs`, `bundles`, `distributions`, `env` or `tools` field in its
literal declarations. Declarations inherit from module `pytestmark`,
class, function and `pytest.param(..., marks=...)` levels, and a literal empty list still
counts. Every other case is *ordinary*.

| Declaration on or above the case | Route | Result reuse |
| --- | --- | --- |
| None | ordinary | never |
| `@pytest.mark.telescope(cache=False)` | ordinary | never |
| `@pytest.mark.telescope(absent=["local.json"])` | ordinary | never |
| `@pytest.mark.telescope(inputs=[])` | scoped | yes |
| `@pytest.mark.telescope(tools=[])` | scoped | yes |
| `@pytest.mark.telescope(env=[])` | scoped | yes |
| `@pytest.mark.telescope(inputs=[], cache=False)` | scoped | never |
| module `pytestmark = pytest.mark.telescope(bundles=["shared"])` | scoped (every case in the module) | yes |

Project `defaults` add files and environment names to scoped cases but never make a case scoped. An `absent`
declaration is still checked against the project for every case that carries it. A
malformed, unsupported or mutated declaration refuses the run; it never turns a case
ordinary.

## Ordinary cases

Each suite with ordinary cases runs one fresh pytest session with the interpreter
that runs pytest-telescope, its installed pytest and plugins, the inherited process
environment, and the suite's working directory, root directory, configuration and
arguments. There is no sandbox, no project snapshot, no repository hashing and no cache
record. Ordinary results are always reported as fresh executions; a later run executes
them again. Ordinary sessions do not require Bubblewrap.

A test file whose module-level `pytestmark` scopes every case is never passed to the
ordinary session, so it is never imported on the host. A *mixed* file, with ordinary
cases and individually scoped cases, is imported and collected by the ordinary session.
Its import and collection hooks run on the host. The ordinary session removes the scoped
cases before any fixture setup, so pytest never runs a scoped case's fixtures or body on
the host. Ordinary session, module and class fixtures keep normal pytest lifetime among
the suite's ordinary cases and share nothing with scoped cases.

## Scoped cases: the filesystem view

Every scoped collection and execution runs `bwrap` as an ordinary child process of the
invocation. Inside, the filesystem looks like this:

| Path in the sandbox | Contents | Access |
| --- | --- | --- |
| `/project` | Frozen copy of the effective declared project files for this case or batch | read-only |
| The project directory's host path | The same frozen copy as `/project` | read-only |
| Host interpreter and environment paths | `sys.prefix`, base and exec prefixes, and every import directory on the interpreter's isolated `sys.path`, at their host paths | read-only |
| `/usr`, `/etc`, `/bin`, `/sbin`, `/lib*` | Host system directories (symbolic links stay links) | read-only |
| `/proc` | A new process filesystem for the sandbox's PID namespace | kernel-controlled |
| `/dev` | Minimal device nodes | read-only, except `/dev/shm` |
| `/tmp`, `/dev/shm` | Private, empty, discarded scratch | writable |
| Everything else, such as `/home` outside the environment and `/run` | Absent | read-only |

Explicit frozen tool declarations add only their documented views. A selected
native tool replaces `/etc` and may also replace library-search directories with the
narrower frozen view [below](#frozen-tools-and-runtime-files-development-contract);
without it the table is unchanged.

An undeclared project file does not exist at `/project/...` or at its host path.
`open`, `stat`, `os.path.exists`, native code and subprocesses all see it as absent.
Trying to open it gives a missing-file error.

The project's host path shows the same frozen copy. If the project lives at
`/home/dev/my_project`, then `/home/dev/my_project/src/my_package/__init__.py` inside the
sandbox is the same declared, read-only file as `/project/src/my_package/__init__.py`, and
an undeclared `/home/dev/my_project/notes.txt` does not exist. The live checkout is never
mounted, and the working directory stays `/project`. Code that reaches the project by its
host path therefore sees exactly the declared files. When `/project` is itself the
project directory, there is one view.

Scoped tests run with the same executable
and installed distributions as the invoking pytest-telescope process, including the
active virtual environment, mounted read-only. There is no runtime build or wheel download.
Installed packages, `sys.executable` subprocesses and native
extensions use that environment, subject to explicitly selected native tools' closed
configuration and runtime views below. Nothing in the environment can be modified. An import
directory that contains or lies inside the project is never bound from the host.

In a scope without a native tool selection, installing the project into the
invoking environment in editable form, for example with `pip install -e .`, leaves a
`.pth` path entry or a finder module that points at the checkout's host path. Because
that path shows the frozen copy, the project's packages import from declared files in
the test itself, in `sys.executable` children and in isolated `-I` children, which ignore
`PYTHONPATH` and the working directory. An undeclared module of the package raises
`ModuleNotFoundError`. An editable install of a different directory, such as another
checkout, points outside the view and is absent. The parent pytest process can also
import declared source through pytest `pythonpath`, as before.

A selected native tool instead closes the supported interpreter prefixes to its
explicit frozen runtime. Selected `.pth`, finder and other startup hooks are unsupported;
an editable installation cannot reopen them. The worker can import declared project
source through explicit pytest configuration. A child receives only paths established
by its own invocation and the closed filesystem view; generated metadata and the parent's
pytest import path are not automatically inherited as Python search-path entries. See
the [native startup contract](bubblewrap-api.md#invoking-python-startup-closure).

An environment inside the project directory, such as `.venv`, keeps its host path:
the sandbox mounts it there on top of the project's view, read-only. It never appears
under `/project`, and the frozen copy on the host is never modified to make room for it.

`/project`, the project's host path, the environment, system directories and the sandbox
root are read-only. Writes there fail with a read-only file system error.
`/tmp` (also `HOME` and `TMPDIR`) and `/dev/shm` are private to one sandbox and removed
when it exits. Pytest's cache provider is disabled and bytecode is not written.

The sandbox environment variables are cleared, then set to a fixed `PATH` (the
interpreter's directory followed by `/usr/local/bin:/usr/bin:/bin`, with duplicate
directories removed), `HOME=/tmp`,
`TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and the
effective names declared globally and for that case or equal-scope group. Scoped
discovery receives the potential collection union; execution receives only its own
effective names. All five fixed names are reserved and cannot be declared, even when
the invoking variable is absent or empty. This applies to global, defaults, bundle and
all marker declarations; invalid names refuse before any session starts. See the
[complete reserved-name rules](specification.md#project-configuration).
Suite plugins are loaded explicitly; autoload remains disabled.

Selected tools may prepend their readonly command directory to the controlled PATH.
Conflicting effective environment declarations refuse rather than being overwritten;
the [tool reference](bubblewrap-api.md#scripts-and-pinned-command-lookup) defines this
scope-local rule. Nonconsumers retain the environment behavior above.

Bubblewrap creates a user namespace, a mount namespace and a PID namespace. The PID
namespace is required for a correct view: the host `/proc` would expose other processes'
root and working directories, which are paths into the host filesystem. The pytest worker
is PID 1 of its namespace, so it adopts and reaps orphaned descendants; any descendant
still alive after teardown makes the result unusable, and every remaining process ends
with the worker. No other namespace is created.

## Frozen tools and runtime files (development contract)

Contributor qualification keeps actual browser execution in separately invoked
[benchmarks](benchmark.md#browser-execution-benchmarks), outside functional readiness.
That classification does not change the filesystem contract or permit broader access.

`tools=["frontend"]` opts a case into an explicitly registered `frozen-tool-v1` closure.
The [manifest reference](bubblewrap-api.md#frozen-tool-manifest-development-contract)
defines the exact records. The controller captures and verifies the declared existing
files into owned immutable material. It never binds the live installation, hard-links
source files, installs a runtime or infers arbitrary program dependencies.

| Selected view | Contents |
| --- | --- |
| `/tools/NAME/ROOT` | Only the selected root's pinned files, directories and unchanged safe links. |
| Verified original-path aliases | The same frozen entries where a declared project link, executable loader or installed Python import requires them. |
| `/tools/.commands/SCOPE_DIGEST` | Readonly command-name symlinks to exact pinned executables; first in the controlled PATH. |
| Declared native library-search directories | Closed readonly trees synthesized from the selected runtime entries; all other host siblings are absent. |
| Native profile's closed `/etc` | Only selected frozen configuration entries, the loader cache when required or declared, and the fixed empty regular `0444` preload file; undeclared host entries are absent. |
| Native invoking/base interpreter prefixes | Owned sparse projections containing only selected runtime entries, including exact aliases, stdlib, pytest dependencies and supported `pyvenv.cfg`; other startup candidates are absent. |

For declared frozen entries, source modes bind identity; files/directories use the source rwx mode with write
bits removed. Writes to any of these views fail. A link remains a link with the exact
declared text: a project `node_modules` link is not replaced by a directory. Logical and
original aliases must resolve only to pinned entries of the same closure. A selected
project-link binding admits traversal only through that exact link/target. Omission
keeps the existing directory-link refusal and adds no external view.

Narrow original aliases may live below `/tmp` or `/dev/shm`; their undeclared live parent
contents are never exposed and the scratch roots remain private. Controller, project,
device and protocol paths stay protected. Conflicting selected mappings refuse instead
of depending on mount order. All source content and mode checks use bounded no-follow
capture and revalidation; an immutable owned view is verified before it is used.

The native profile supports declared Linux x86_64 glibc ELF loaders/libraries and simple
pinned script interpreters. An unchanged `#!/usr/bin/env node` executes the actual pinned
env and Node through kernel shebang handling and the explicit command map. No script,
argument, assertion, exit code, signal or child transport is rewritten. Its dynamic
loader uses actual pinned cache and loader bytes; declared search directories exclude
unbound host libraries, including competing hardware-capability candidates. Unsupported
or ambiguous lookup refuses. [Verified library ordering](bubblewrap-api.md#verified-native-library-ordering)
uses explicit process/resource contexts, ordered own search paths and a conservative
proof that inherited RPATH blocks cannot change fallback targets. Separate executable
processes can retain different library variants; one process cannot have conflicting
needed names or SONAMEs. The selected closure must also include the invoking Python
worker's native/runtime needs; a preflight failure occurs before project imports.
The [native configuration contract](bubblewrap-api.md#declared-native-system-configuration)
replaces `/etc` with an owned readonly hierarchy of declared frozen entries and exact
loader controls. Supported static or dynamic ELF in a tool's closure, or a nonnull
loader profile, activates this view; data-only tools without either keep the baseline.
For example, `/etc/resolv.conf` is absent unless selected and pinned. Declared certificate
files expose exact frozen bytes without granting unselected siblings. No ambient `/etc`
inventory or source/configuration descriptor transport is used. The complete mount plan,
including environment overlays, preserves this closure before interpreter, site and
project imports. Nonconsumers retain their baseline view.

Native worker startup currently supports the
[standard Linux x86_64 CPython 3.11–3.13 layout](bubblewrap-api.md#invoking-python-startup-closure).
Its actual invoking and base prefixes close to selected files. In particular, a base
prefix of `/usr` closes all of `/usr`; baseline fallback commands and packages there
are unavailable unless explicitly pinned. Site hooks and startup zip layouts are
unsupported. This is a narrower native view, not an automatic environment snapshot.

Browser executables, driver/bootstrap scripts, subprocesses and resources follow these
same records. A browser name or version is not sufficient. Its actual executable must
fit the bounded profile; changing a full browser to a different headless executable is
not a preservation of the caller's command. Already-installed Python package/data and
dist-info records similarly preserve real import and metadata paths, including in
`sys.executable -I` children. Neither feature constructs an interpreter environment.

Tool selection does not remove packages already present in the baseline environment
from nonconsumers. It adds frozen identity for explicitly claimed files. A case without
the tool gets no new `/tools` entries or external aliases and retains the baseline
system view. Only consumers key the selected manifest/lock bytes, mappings, actual file
bytes/modes/link text, executable/runtime graph, command map and closed configuration
and loader views. Unselected ambient configuration is not a consuming input.
The existing invoking-interpreter and installed-distribution identity remains in every
scoped key; names and versions alone do not identify claimed mutable runtime files.

Capture, retry and terminal cleanup obey the
[bounded lifecycle](execution-contract.md#frozen-tool-capture-and-reuse). A failure never
widens the view or causes ordinary replay. CPU, memory, process and scratch limits remain
the caller's responsibility; network policy is unchanged.

## What the sandbox does not do

pytest-telescope sets no CPU, memory, process, open-file or scratch-size limits and
reports no CPU usage. Workers are child processes, so they stay inside the limits your
CI job, container or scheduler applies to the pytest invocation. Measure CPU and memory
there.

Sandboxed tests share the host network. The default system view includes host resolver
configuration; native-tool consumers instead need explicitly declared frozen resolver
and other required configuration. This filesystem difference does not isolate the
network or promise DNS availability. The cache key cannot track changes to network
services. Leave tests that depend on those services ordinary, or add `cache=False`
to their scope declarations.

The view prevents accidental reads of undeclared files and accidental writes. It does
not protect against code that deliberately attacks the kernel or shared network.

If Bubblewrap is missing or cannot create namespaces, the run refuses. Scoped cases
never fall back to ordinary execution, and nothing is replayed in ordinary mode after
a scoped runner starts.

## Collection, cache identity and reuse

Scoped discovery for files whose module-level declaration scopes every case runs in one
sandboxed pytest collection per suite. Its view is the union of those files'
declarations, project defaults, configuration and the files themselves. That union is a collection
view only: no case body ever runs with it. Scoped cases in mixed files are identified by
the ordinary session's collection. Every scoped execution collects its containing file
again inside its own narrow view and refuses when that file's case inventory or the
selected membership differs.

A scoped result is keyed by the case's actual pytest identity, the frozen bytes and
modes of its effective declared files, declared absence, selected metadata, the
containing file's case inventory, the suite configuration and arguments, the effective
forwarded environment names and their captured presence and values, the runner
implementation, the sandbox policy version, the
interpreter identity (implementation, full version and build string, ABI and platform),
the sorted names and versions of every distribution visible to the isolated
interpreter, and how the environment reaches into the project. Installing, upgrading or
removing a package therefore invalidates scoped results. Only explicitly selected frozen
Git/tool runtime files add host-tool byte identity; the remaining host system directories
and the host network are not part of the key.

How the environment reaches into the project is recorded relative to the project: the
`.pth` files of the interpreter's site directories and the modules they load at startup
(such as an editable install's finder) that name the project directory, with that
directory replaced by a placeholder, and the import directories that lie inside the
project outside every environment prefix. Re-pointing an editable install, or moving a
checkout whose editable install still names the old path, reruns its scoped cases.
Identical checkouts at different paths with equivalent environments share results.
Without an editable install the project's path is not part of the key.

`cache=False` keeps a scoped case sandboxed and fresh: it executes every time and never
records a reusable result. `--force` reruns cached cases; `--no-cache` also prevents
recording new results. Ordinary cases
are never looked up in or written to the cache.

## Case mode, batches and parallel workers

Case mode is the default: every scoped case gets its own sandbox, pytest session and
scratch. Batch mode is opt-in. A batch contains selected scoped cases of one suite and one
module with exactly identical effective scope, metadata and cache policy. Members run
sequentially in one sandbox with normal pytest fixture lifetime; unequal scopes are never
merged into a union.

Independent cases or batches run in parallel up to the configured number of `workers`
(default 1, at most 32). Workers is a count of concurrent sandboxes, not a resource
budget. Collection and ordinary sessions run one at a time before scoped execution
starts.

## Order of an invocation

1. Parse declarations statically, without importing project code, and route every file.
2. If any scoped case is possible, probe the interpreter environment and run a Bubblewrap
   preflight. A failure refuses the whole run before any test code executes.
3. Collect files that are scoped as a whole, in a sandbox (or reuse a valid inventory).
4. Run each suite's ordinary session; it also identifies scoped cases in mixed files.
5. Reuse or execute scoped cases and batches, up to `workers` at a time.

`--timeout` bounds each sandboxed session: a scoped collection, case or batch. Ordinary
suite sessions have no pytest-telescope deadline, like normal pytest; the job that runs
pytest bounds them, as it bounds resources. A scoped collection, case or batch gets at
most two attempts for infrastructure failures, with fresh scratch each time. An ordinary
session is attempted once, because replaying it could repeat its side effects. A run
that refuses or stops after ordinary sessions started does not prove that no test ran.

## Refusals

| Situation | Status and capability |
| --- | --- |
| `bwrap` is not installed | `unsupported`, `sandbox:bubblewrap` |
| Bubblewrap cannot create the user, mount or PID namespace | `unsupported`, `sandbox:namespaces` |
| The interpreter cannot start in the view, an environment prefix contains the project, the project lives in `/usr` or `/etc`, or an environment directory overlaps a sandbox mount point | `unsupported`, `sandbox:environment` |
| The project directory cannot also be shown at its host path: it is `/`, `/tmp` or `/dev/shm`, contains a directory the sandbox mounts, or lies inside `/project`, `/metadata`, `/telescope`, `/proc` or `/dev` (other than `/dev/shm`) | `unsupported`, `sandbox:environment` |
| A suite requests `network:none` or `scratch:noexec` | `unsupported`, that capability |
| Invalid, unsupported or mutated declaration | `declaration_error` |

The namespace refusal includes Bubblewrap's own message. Enable unprivileged user
namespaces for `bwrap` on that host, or run scoped tests on a host where they are
permitted. Never weaken a host security policy only to make a test run pass.
