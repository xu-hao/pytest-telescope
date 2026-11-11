# Frozen Git checkout design (not implemented)

This page is for contributors. It records a design for giving scoped tests a frozen,
read-only Git checkout with its history. **pytest-telescope does not implement this
design.** There is no `git=` marker field, no frozen-checkout or history capture, and
none of the capability codes listed below. Frozen Git history is not supported.

Scoped tests can still run the host's `git` from the read-only system directories on a
repository they create in private scratch, as described under
[host tools and child interpreters](specification.md#host-tools-and-child-interpreters).
Selecting a Git executable as an ordinary [frozen tool](specification.md#frozen-tools)
for such a scratch repository is a different, supported use.

Everything below describes the design, not current behavior. It is kept so that a future
implementation starts from the same rules.

## Declaration form

This records the future declaration form, not a runnable current example:

```text
@pytest.mark.telescope(git="frozen-checkout-v1")
def test_revision(): ...
```

Under that future contract, the same literal string may appear in defaults or bundles. Module, class,
function and parameter declarations add together; inherited Git cannot be removed.
Defaults alone do not scope an ordinary case. Omission leaves Git disabled; unknown
versions and non-string or dynamic marker values are rejected. `plan` is static and
does not run Git. A requested view includes the supported committed source and Git
history as well as separately declared inputs; untracked files need separate declarations.
The [sandbox view](#sandbox-view) below defines the supported states, safety and refusal
boundaries.

## Sandbox view

Under the future contract, `git="frozen-checkout-v1"` would scope a case and grant an
owned, integrity-verified read-only Git checkout at `/project`, where scoped sessions
see the project. The supported committed source tree, normalized index, HEAD and
reachable local branch and tag history are visible. Untracked files require separate
input declarations. The default without this declaration retains the existing no-Git
view. Actual Git commands such as `rev-parse`, `log`, `show`, `ls-tree`, `diff` and
`status` and `ls-files --stage`, including calls from subprocesses and Python children,
operate on the frozen checkout; writes to it fail. Status reflects the normalized clean
tracked checkout and any separately declared untracked files. External helpers, remotes,
network operations and configuration-dependent behavior are unsupported. A separately
created repository in private scratch remains writable.

### Supported repositories

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

### Git executable

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

### Capture bounds

Capture bounds are 100,000 visible tracked paths and 500,000 reachable objects,
200,000 walked filesystem and metadata entries, 4,096 selected refs, 64 MiB per
individual source file, Git object or metadata file, and 2 GiB of simultaneously retained
Git capture material. The material limit counts file contents in staging, intermediate
copies, reachable-pack generation and the final snapshot and source, link-target bytes,
and bounded reservations for native-generated index output. Separate retained copies
count separately; hardlinked aliases of the same content count once. Historical objects
streamed without retention are not summed by their uncompressed sizes. This is not a
filesystem allocation quota and does not count directory, inode or slab overhead, all
concurrent case views, other declared-input copies or total job scratch. The separately
bounded runtime closure is described above. Caller jobs remain responsible for actual
CPU, memory, process and scratch limits and accounting.

Reachable history is retained in an owned reachable-only compact pack. Compound pack
files are streamed and bounded by the 2 GiB capture-material aggregate rather than the
64 MiB individual object limit. Copying, hashing and current-tree object bodies are
streamed; each body remains bounded by 64 MiB, and no pack or body stream may exceed
its applicable capture-material aggregate or per-object bound. The SHA-1 object listing
is bounded to 20.5 MiB (500,000 records of at most 41 bytes). All other command output is
bounded at 64 MiB. Complete object and connectivity verification remain required. Paths
and link targets are limited to 4,096 UTF-8 bytes and paths to 64 components. Each
command has the lesser of the invocation timeout and 30 seconds; each complete capture
attempt has the lesser of the invocation timeout and 120 seconds. Limits refuse rather
than truncating history. The exact owned pack representation is deterministic or
included in the identity.

### Refusal and recovery

Capture and revalidation refuse unavailable, changing, corrupt, partial or unsupported
contexts with bounded evidence and at most two fresh capture attempts. Diagnostics do
not expose raw Git stderr, host configuration, contents or host paths. No mutable host
repository is mounted, no ordinary fallback follows a scoped attempt, and failed cleanup
prevents publishing new pending cache records. Caller resource limits and shared host
network policy are unchanged. The snapshot does not claim full host-system, locale, NSS
or network reproducibility.

## Cache identity

Under that future contract, `git="frozen-checkout-v1"` would add the verified frozen Git
context and its executable/runtime identity to only consuming case and group keys.
It adds the supported tracked tree to that scope; other cases retain their existing
inputs. Collection uses the union needed by its potential consumers, but execution
uses each case's own scope. A collection refresh caused by Git cannot by itself
invalidate a nonconsumer's result. Equal-scope batching includes Git context in equality;
Git and non-Git scopes cannot share a group. `cache=False` continues to disable reuse.

Freeze once per invocation and validate the relevant source and frozen snapshot before
accepting a hit, before execution and before publication. Changed HEAD, local refs,
reachable history, tracked source, link text or target content, executable or its
relevant runtime changes the consuming identity. Bounded capture, integrity and cleanup
failures refuse or end in infrastructure failure after bounded retries; they never create
reusable success or trigger ordinary fallback. Pending records become eligible only after
owned cleanup succeeds; a terminal cleanup failure suppresses all new pending records
while preserving prior valid records. See the [sandbox view](#sandbox-view) above for the
view and state limits.

## Collection, batches and workers

Under the design, the Git declaration would supply a supported committed tree and history
only to its consumers. Its collection union would not grant that union to narrow
execution or globally key unrelated case results.

In [batch mode](batching.md#exact-scope-and-deterministic-membership), the Git
declaration and context would participate in equality; Git consumers and nonconsumers
remain in separate groups.

## Capability codes

The design separately specifies `git:frozen-checkout-v1`, `git:executable` and
`git:limits`, plus controlled Git/SSH/loader/locale declarations and bounded capture
recovery. None of these is a capability or declaration field that pytest-telescope
implements.
