# Ordinary pytest and Bubblewrap filesystem views

This page explains how pytest-telescope runs each test and what a scoped test can see.
Read it when a scoped test cannot find a file, when you want to know which paths are
read-only, or when you need to know what makes a cached result reusable.

A test is *ordinary* unless it declares what it reads. Ordinary tests run as plain
pytest every time. A *scoped* test declares its inputs. It runs in a Bubblewrap sandbox
that shows only those project files, and pytest-telescope can reuse its earlier
successful result. The sandbox restricts which project files a test can read. It uses
your installed Python environment and leaves resource limits and network policy to the
job running pytest.

See the [API reference](bubblewrap-api.md) for interfaces and configuration, and the
[result reference](result-schema.md) for schema 4 fields.

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

Project `defaults` add files and environment names to scoped cases but never make a case
scoped. An `absent` declaration, also called a *declared absence*, lists paths that must
not exist in the project. It is still checked against the project for every case that
carries it. A
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

Before scoped tests run, pytest-telescope copies the declared project files once per
invocation into a read-only *frozen copy*. A scoped test sees that copy, and only that
copy, at `/project`; this is its *frozen view* of the project.

Every scoped collection and execution runs `bwrap` as an ordinary child process of the
invocation. Inside, the filesystem looks like this:

| Path in the sandbox | Contents | Access |
| --- | --- | --- |
| `/project` | Frozen copy of the effective declared project files for this case or batch | read-only |
| The project directory's host path | Absent: nothing is mounted there | none |
| Host interpreter and environment paths | `sys.prefix`, base and exec prefixes, and every import directory on the interpreter's isolated `sys.path` outside the project, at their host paths | read-only |
| Environment files that name the project directory | Rewritten copies in which that directory reads `/project`, over the originals | read-only |
| `/usr`, `/etc`, `/bin`, `/sbin`, `/lib*` | Host system directories (symbolic links stay links) | read-only |
| `/proc` | A new process filesystem for the sandbox's PID namespace | kernel-controlled |
| `/dev` | Minimal device nodes | read-only, except `/dev/shm` |
| `/tmp`, `/dev/shm` | Private, empty, discarded scratch | writable |
| Everything else, such as `/home` outside the environment and `/run` | Absent | read-only |

Explicit frozen tool declarations add only their documented views. A selected
native tool replaces `/etc` and may also replace library-search directories with the
narrower frozen view [below](#frozen-tools-and-runtime-files);
without it the table is unchanged.

An undeclared project file does not exist in the sandbox.
`open`, `stat`, `os.path.exists`, native code and subprocesses all see it as absent.
Trying to open it gives a missing-file error.

The frozen copy appears at `/project` and nowhere else. If the project lives at
`/home/dev/my_project`, then `/project/src/my_package/__init__.py` inside the sandbox is
the declared, read-only frozen file, and `/home/dev/my_project` does not exist there at
all, declared files included. The live checkout is never mounted.

Each scoped session runs at `/project`. The working directory is the suite's `cwd` below
it, and pytest receives the suite's configuration file (`-c /project/...`), root
directory (`--rootdir=/project/...`) and test files there. `Path.cwd()`, `__file__`, the
pytest root path, tracebacks and the modules of an editable install all name `/project`,
whatever the checkout is called and wherever it lives. A test that checks an imported
package module lies below its own project root, for example
`Path(my_package.__file__).is_relative_to(Path(__file__).parents[1])`, passes in a scoped
session as it does in an ordinary one.

### A scoped test cannot tell where its checkout lives

Identical checkouts at different paths share scoped results, so nothing a scoped test
observes may depend on the checkout's path. The view policy `bubblewrap-view-v3` provides
this, with the exceptions listed under
[What a scoped test can still see](#what-a-scoped-test-can-still-see):

- The project is shown only at `/project`; its host path is not mounted.
- Files of the environment that name the project directory are shown as rewritten,
  read-only copies in which the directory reads `/project`. These are the `.pth` files
  of the interpreter's site directories; the modules they load at startup, such as an
  editable install's finder; the installed metadata of a distribution installed from a
  location (every file of a `*.dist-info` directory that has a `direct_url.json`, and
  `.egg-link` files); and, for an environment inside the project, its `pyvenv.cfg` and
  the text files of its scripts directory.
- The directory is matched as a whole path, as it is spelled literally, in a file URL
  with percent escapes in either case (`%C3%A9` or `%c3%a9`), and in a Python or JSON
  string literal. A path directly after `=` is rewritten, for example `VIRTUAL_ENV=…`
  in an environment's `activate` script, `key=…` and `--opt=…`. A longer path or name
  that merely ends or starts with the directory is never rewritten: a match is blocked
  after another path component and after a file-name character, including `~`, `+`, `@`
  and `%`. One rare false match remains: below a directory whose name ends in `=`, as
  in `/mnt/x=/home/dev/my_project`, the path after the `=` is rewritten.
- As a result, identical checkouts at different paths, each with its own environment
  inside the project, have the same sandbox identity and share results, however the
  environment's scripts quote the path.
- The host files are never modified. The copies live in the invocation's private scratch
  and are bound over the originals for that sandbox only.
- Cached bytecode of a rewritten module is shown as an empty file, and the rewritten
  source carries a different modification time from the original, which is the one a
  cache of the original records. The interpreter therefore compiles the rewritten
  source, including when the stale cache is an unchecked hash-based file that is never
  compared with its source. Only cache files that exist when the run identifies the
  environment are shown empty: a timestamp or checked hash-based cache written later is
  rejected by the different modification time, but an unchecked hash-based cache written
  later would be loaded.
- The sandbox environment is cleared; `PWD` is the working directory below `/project`,
  and `OLDPWD`, `VIRTUAL_ENV` and `PYTHONPATH` are unset unless declared. A declared
  variable is forwarded with its value, which is part of the case's key.
- `/proc/self/mounts` and the mount points and options in `/proc/self/mountinfo` name
  only sandbox paths.

The rewritten copies of one environment may total at most 1 MiB; an environment file
beyond that bound that names the project directory refuses with `sandbox:environment`.

### What a scoped test can still see

Two things a scoped test can read still name its checkout. They are outside the
guarantee above. A result cached in one checkout could be reused in another, where a
test that read either one would have seen a different value.

**Not location-blind: the mount table's source directories.** A test that reads the
source directories in the mount table (field 4 of `/proc/*/mountinfo`) can observe its
checkout's location, which its cache key does not contain. Leave such a test ordinary,
or add `cache=False` to its scope.

Field 4 of each mount table line is the bind source's directory within its filesystem.
The frozen copy and the invocation's other files live below the project's
`.pytest-telescope/scratch` directory, so that field names the checkout. With the
project at `/home/dev/my_project`, the line for `/project` reads:

```text
412 398 253:0 /home/dev/my_project/.pytest-telescope/scratch/invocation-<id>/project /project ro,nosuid,nodev,relatime - ext4 /dev/sda2 rw
```

A test that parses field 4, or runs a tool that prints it, sees
`<checkout>/.pytest-telescope/scratch/invocation-<id>/project`. The rest of the mount
table is clean: the mount points (field 5), the mount options and every line of
`/proc/self/mounts` name only sandbox paths.

**Not location-blind: bytecode cached by an environment inside the project.** A test
that reads the raw bytes of cached bytecode files (`__pycache__/*.pyc`) of an
environment inside the project can observe its checkout's location, which its cache key
does not contain. Leave such a test ordinary, or add `cache=False` to its scope.

An [environment inside the project](#an-environment-inside-the-project) keeps the
bytecode it cached for its own modules. Each cache file records the path its source had
when it was compiled, which lies below the checkout. With the project at
`/home/dev/my_project` and its environment in `.venv`, the sandbox shows the cache file
at a sandbox path, but its bytes still contain the host path of its source:

```text
file:   /project/.venv/lib/python3.12/site-packages/__pycache__/example.cpython-312.pyc
bytes:  … /home/dev/my_project/.venv/lib/python3.12/site-packages/example.py …
```

A test that scans the environment's files for absolute paths, or reads a cache file's
bytes itself instead of importing the module, sees `<checkout>/.venv/...`. Everything
the interpreter reports is clean: an imported module's `__file__`, the `co_filename` of
its code and tracebacks through it name sandbox paths below `/project/.venv`. An
environment outside the project is not affected.

Scoped tests run with the same executable
and installed distributions as the invoking pytest-telescope process, including the
active virtual environment, mounted read-only. There is no runtime build or wheel download.
Installed packages, `sys.executable` subprocesses and native
extensions use that environment, subject to explicitly selected native tools' closed
configuration and runtime views below. Nothing in the environment can be modified. An import
directory that contains or lies inside the project is never bound from the host.

In a scope without a native tool selection, installing the project into the
invoking environment in editable form, for example with `pip install -e .`, leaves a
`.pth` path entry or a finder module that points at the checkout's host path. The
sandbox shows those files rewritten to `/project`, so the project's packages import from
the declared files of the frozen copy in the test itself, in `sys.executable` children
and in isolated `-I` children, which ignore `PYTHONPATH` and the working directory. An
undeclared module of the package raises `ModuleNotFoundError`. An editable install of a
different directory, such as another checkout, is not rewritten, points outside the view
and is absent. The parent pytest process can also import declared source through pytest
`pythonpath`; both resolve to the same files below `/project`.

A selected native tool instead closes the supported interpreter prefixes to its
explicit frozen runtime. Selected `.pth`, finder and other startup hooks are unsupported;
an editable installation cannot reopen them. The worker can import declared project
source through explicit pytest configuration. A child receives only paths established
by its own invocation and the closed filesystem view; generated metadata and the parent's
pytest import path are not automatically inherited as Python search-path entries. See
the [native startup contract](bubblewrap-api.md#invoking-python-startup-closure).

### An environment inside the project

An environment inside the project directory, such as `.venv`, keeps its place in the
project: the sandbox mounts it read-only at the same relative path below `/project`, for
example `/project/.venv`, and starts `/project/.venv/bin/python`. `sys.prefix`,
`sys.executable`, `sys.path`, the first `PATH` entry and child interpreters all name
`/project/.venv`. Its `.pth` files, startup modules, installed metadata, `pyvenv.cfg`
and scripts are rewritten like every other file that names the project directory, so a
console script whose first line names the environment's interpreter starts it at its
sandbox path. The environment is not part of the frozen copy, and the frozen copy on
the host is never modified to make room for it.

The environment's other files are shown as they are, and its cached bytecode is used as
it is. The raw bytes of those cache files still name the checkout. This is one of the
two [documented exceptions](#what-a-scoped-test-can-still-see) to location blindness;
what the interpreter reports for the modules is not affected.

`/project`, the environment, the rewritten files, system directories and the sandbox
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

## Frozen tools and runtime files

A *frozen tool* is an existing installation outside the project, such as a Node
runtime or an installed package, whose exact files a scoped test selects by name.
`tools=["frontend"]` opts a case into an explicitly registered `frozen-tool-v1` closure.
The [manifest reference](bubblewrap-api.md#frozen-tool-manifest)
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

A declared closed directory is supported even when its logical path is absent on the
host or its frozen projection is empty. Private readonly mountpoint scaffolding preserves
already admitted baseline siblings outside that closed tree, including their literal
symlinks; it does not write the host, follow new targets, add captured host inputs or
broaden the selected runtime closure. The closed tree still contains only its verified
selected entries.

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

Frozen Git history is not supported. A scoped test that needs a Git repository can create
one in private scratch and run the host's `git` there; see
[host tools](specification.md#host-tools-and-child-interpreters).

If Bubblewrap is missing or cannot create namespaces, the run refuses. Scoped cases
never fall back to ordinary execution, and nothing is replayed in ordinary mode after
a scoped runner starts.

## Collection, cache identity and reuse

Scoped discovery for files whose module-level declaration scopes every case runs in one
sandboxed pytest collection per suite. Its view is the union of those files'
declarations, project defaults, configuration and the files themselves. That union is a collection
view only: no case body ever runs with it. Scoped cases in mixed files are identified by
the ordinary session's collection. A file's *case inventory* is the complete list of
cases pytest collects from it. Every scoped execution collects its containing file
again inside its own narrow view and refuses when that file's case inventory or the
selected membership differs.

A scoped result is keyed by the case's actual pytest identity, the frozen bytes and
modes of its effective declared files, declared absence, selected metadata, the
containing file's case inventory, the suite configuration and arguments, the effective
forwarded environment names and their captured presence and values, the runner
implementation, the sandbox policy version (`bubblewrap-view-v3`, the single `/project`
view with rewritten environment files; results recorded under an earlier policy are
never reused), the
interpreter identity (implementation, full version and build string, ABI and platform),
the sorted names and versions of every distribution visible to the isolated
interpreter, and how the environment reaches into the project. Installing, upgrading or
removing a package therefore invalidates scoped results. pytest-telescope reads the
interpreter and environment identity by starting that interpreter once on the host,
isolated and with bytecode writes off, so identifying an environment never writes into
it, whatever the calling job's own bytecode setting. Only explicitly selected frozen
tool runtime files add host-tool byte identity; the remaining host system directories
and the host network are not part of the key.

How the environment reaches into the project is recorded relative to the project: every
environment file the sandbox shows [rewritten](#a-scoped-test-cannot-tell-where-its-checkout-lives),
with the project directory replaced by a placeholder, and the import directories that
lie inside the project outside every environment prefix. Re-pointing an editable
install, changing its installed origin record, or moving a checkout whose editable
install still names the old path, reruns its scoped cases. Identical checkouts at
different paths with equivalent environments share results. Without such a file the
project's path is not part of the key.

No key or record contains the project's path. Case identities are pytest node IDs
relative to the suite root. Declared inputs, inventories, skip and failure evidence, and
fixture-guard identities name project files relative to the project or below `/project`.

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

1. Parse test declarations and the declared Python sources of scoped views statically,
   within one [parse budget](execution-contract.md#static-parse-budget), without importing
   project code, and route every file.
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
| The interpreter cannot start in the view, an environment prefix contains the project, or an environment directory outside the project overlaps a path the sandbox mounts itself (`/project`, `/metadata`, `/telescope`, `/proc`, `/dev`, or contains `/tmp`) | `unsupported`, `sandbox:environment` |
| The sandbox would show the project's live files: the project lives in `/usr` or `/etc`, or is `/` | `unsupported`, `sandbox:environment` |
| An environment file that names the project directory is too large to show rewritten | `unsupported`, `sandbox:environment` |
| A suite requests `network:none` or `scratch:noexec` | `unsupported`, that capability |
| Invalid, unsupported or mutated declaration | `declaration_error` |

The namespace refusal includes Bubblewrap's own message. Enable unprivileged user
namespaces for `bwrap` on that host, or run scoped tests on a host where they are
permitted. Never weaken a host security policy only to make a test run pass.
