# Ordinary pytest and Bubblewrap filesystem views

Tests without a scope declaration run as ordinary pytest every time. Tests with a
declaration run in a Bubblewrap sandbox and can reuse a previous successful result.
The sandbox restricts which project files a test can read. It uses your installed
Python environment and leaves resource limits and network policy to the job running
pytest.

This page defines the execution rules for pytest-telescope 0.1.0a6. See the
[API reference](bubblewrap-api.md) for interfaces and configuration, and the
[result reference](result-schema.md) for schema 3 fields.

## Which route a case takes

A case is *scoped* when it has an `inputs`, `bundles` or `distributions` field in its
literal declarations. Declarations inherit from module `pytestmark`,
class, function and `pytest.param(..., marks=...)` levels, and a literal empty list still
counts. Every other case is *ordinary*.

| Declaration on or above the case | Route | Result reuse |
| --- | --- | --- |
| None | ordinary | never |
| `@pytest.mark.telescope(cache=False)` | ordinary | never |
| `@pytest.mark.telescope(absent=["local.json"])` | ordinary | never |
| `@pytest.mark.telescope(inputs=[])` | scoped | yes |
| `@pytest.mark.telescope(inputs=[], cache=False)` | scoped | never |
| module `pytestmark = pytest.mark.telescope(bundles=["shared"])` | scoped (every case in the module) | yes |

Project `defaults` add files to scoped cases but never make a case scoped. An `absent`
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
extensions work as on the host, but nothing in the environment can be modified. An import
directory that contains or lies inside the project is never bound from the host.

Installing the project into the
invoking environment in editable form, for example with `pip install -e .`, leaves a
`.pth` path entry or a finder module that points at the checkout's host path. Because
that path shows the frozen copy, the project's packages import from declared files in
the test itself, in `sys.executable` children and in isolated `-I` children, which ignore
`PYTHONPATH` and the working directory. An undeclared module of the package raises
`ModuleNotFoundError`. An editable install of a different directory, such as another
checkout, points outside the view and is absent. The parent pytest process can also
import declared source through pytest `pythonpath`, as before.

An environment inside the project directory, such as `.venv`, keeps its host path:
the sandbox mounts it there on top of the project's view, read-only. It never appears
under `/project`, and the frozen copy on the host is never modified to make room for it.

`/project`, the project's host path, the environment, system directories and the sandbox
root are read-only. Writes there fail with a read-only file system error.
`/tmp` (also `HOME` and `TMPDIR`) and `/dev/shm` are private to one sandbox and removed
when it exits. Pytest's cache provider is disabled and bytecode is not written.

The sandbox environment variables are cleared, then set to a fixed `PATH` (the
interpreter's directory followed by `/usr/local/bin:/usr/bin:/bin`), `HOME=/tmp`,
`TMPDIR=/tmp`, `PYTHONDONTWRITEBYTECODE=1`, `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, and the
names listed in `[tool.pytest_telescope] env`. Suite plugins are loaded explicitly.

Bubblewrap creates a user namespace, a mount namespace and a PID namespace. The PID
namespace is required for a correct view: the host `/proc` would expose other processes'
root and working directories, which are paths into the host filesystem. The pytest worker
is PID 1 of its namespace, so it adopts and reaps orphaned descendants; any descendant
still alive after teardown makes the result unusable, and every remaining process ends
with the worker. No other namespace is created.

## What the sandbox does not do

pytest-telescope sets no CPU, memory, process, open-file or scratch-size limits and
reports no CPU usage. Workers are child processes, so they stay inside the limits your
CI job, container or scheduler applies to the pytest invocation. Measure CPU and memory
there.

Sandboxed tests share the host network, including DNS through the host `/etc`. The cache
key cannot track changes to network services. Leave tests that depend on those services
ordinary, or add `cache=False` to their scope declarations.

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
containing file's case inventory, the suite configuration and arguments, the forwarded
environment values, the runner implementation, the sandbox policy version, the
interpreter identity (implementation, full version and build string, ABI and platform),
the sorted names and versions of every distribution visible to the isolated
interpreter, and how the environment reaches into the project. Installing, upgrading or
removing a package therefore invalidates scoped results. The host system directories and
the host network are not part of the key.

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
