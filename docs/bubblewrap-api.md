# Ordinary and Bubblewrap API/runtime proposal

Status: review draft for the canonical Bubblewrap contract, not implemented behavior.
The accepted resource mechanism is commit ee1e35f, hosted run 39503556501. It proved
concurrent declared views, numeric controller/worker/pool limits, CPU throttling, owned
OOM/PID exhaustion, noexec/byte/inode scratch limits and exact cleanup. It did not qualify
runtime preparation, session lifecycle, mixed routing or the complete delivery gate.

## Concrete interfaces

Preserve existing arguments and add only explicit backend/preparation/headroom settings:

```python
controller.run(root, *, force=False, no_cache=False, explain=False, cache_dir=None,
               timeout=60, arguments=(), budget=None, suites=None, execution_mode=None,
               structural_files=None, runtime_profile=None, required_capabilities=None,
               python_profile=None, source_roots=None, backend=None,
               controller_resources=None)
plan.make_plan(root, *, suites=None, structural_files=None, runtime_profile=None,
               required_capabilities=None, python_profile=None, source_roots=None,
               backend=None, execution_mode=None, budget=None, controller_resources=None)
runtime.build_runtime(root, requirements=None, *, runtime_profile=None,
                      interpreter=None, bubblewrap=None, controller_resources=None)
runtime.inspect_runtime(root, *, runtime_profile=None, required_capabilities=None,
                        backend=None)
```

`backend` accepts only `bubblewrap-v1` for the replacement; omission uses configuration.
`docker-v1` is not an automatic fallback. Old Docker artifacts and result records remain
untouched and ineligible. `runtime.build_runtime` retains its public name and CLI `runtime`
command, but produces a frozen file closure, not an image. Preparation explicitly selects
an absolute CPython executable (`interpreter`, default `sys.executable`) and a non-setuid
Bubblewrap executable (`bubblewrap`, default `/usr/bin/bwrap`). Run and inspection never
select PATH tools, download, rebuild or repair missing prepared state.

`runtime.inspect_runtime` keeps its exact three-field `{profile,required_capabilities,identity}`
return; no executable paths leak into that envelope. It validates prepared files and actual
backend compatibility read-only, with at most two retryable inspection attempts and no
source/cache/state writes. Its digest supplies identity, not execution/cache authority.

```toml
[tool.pytest_telescope.runtime]
profile = "python-v2"                 # or explicitly "git-local-v2"
requires = []                         # ["git-local"] for that capability

[tool.pytest_telescope.execution]
backend = "bubblewrap-v1"
mode = "case"                        # batch remains explicit opt-in

[tool.pytest_telescope.execution.worker]
cpus = 1
memory_bytes = 536870912
processes = 128
scratch_bytes = 67108864

[tool.pytest_telescope.execution.budget]
cpus = 4
memory_bytes = 2147483648
processes = 512
scratch_bytes = 268435456

[tool.pytest_telescope.execution.controller]
cpus = 1
memory_bytes = 2147483648
processes = 64
scratch_bytes = 1073741824
```

The existing four budget dimensions retain their meaning: aggregate scoped worker
capacity. `controller_resources` completely replaces the four controller fields, not a
partial merge; it does not subtract from worker capacity. Positive integer validation and
explicit upper bounds remain. Memory/scratch requests must be 4096-byte multiples, so
kernel rounding cannot widen or misreport admission. Existing default values satisfy this.
Worker scratch inode cap is 65,536; controller-owned pool cap is 262,144 entries. Both are
versioned limits and typed refusal boundaries, not inferred host capacities.

CLI `plan` and `run` add `--backend` and all four `--controller-{cpus,memory-bytes,processes,
scratch-bytes}` flags. `runtime` adds `--interpreter` and `--bubblewrap`, plus the controller
flags. Existing suite/selectors, execution-mode, requirements, capability and source-root
flags remain. Structural narrowing stays API-only. The Python lookup default remains the existing string `default`. `python` lookup configuration and its
complete suite replacement/caller field override rules remain unchanged.

Unknown profile/version is `runtime:profile`, unknown backend `backend:profile`, missing
host namespaces `backend:userns`, unsupported manager/controller setup `backend:resources`,
unqualified runtime layout `runtime:layout`, native ABI mismatch `runtime:abi`, unsupported
Bubblewrap binary/security attachment `backend:provenance`. Missing/corrupt prepared state
is infrastructure failure, not unsupported success. Existing declaration/capability error
classes and CLI exits remain. Invalid explicit scoped declarations never become ordinary.

## Before and after routing

Before: an unmarked test creates a broad project snapshot and can reuse scoped evidence.
After: it always executes in ordinary pytest; no runtime preparation, snapshot, cache lookup
or blanket project hash. Literal inherited inputs/bundles/distributions, including empty
lists, opt into scoped execution. Defaults/absence-only/cache-only fields do not.
Absence-only ordinary declarations still check their exact named paths, without walking
unrelated content. Explicitly scoped cache=False is isolated/fresh/nonrecording.

One actual ordinary pytest session per suite uses the invoking interpreter, installed
plugins and inherited environment with that suite's cwd/root/config/arguments. It is not a
clean runtime. Effective xdist scheduling remains refused, including in ordinary mode;
normal `-o addopts=` precedence applies. The direct pytest adapter removes its explicit
controller flag before delegation, never recursively dispatches, and documents that host
plugin startup precedes adapter interception. Scoped plugins remain explicit/autoload-off.

Pure unconditional module-inherited scope files never enter ordinary collection. Mixed
module/conftest imports may run ordinary Python. Pytest dispatch excludes every scoped
fixture/body before setup; arbitrary import code can still call arbitrary functions.
Bounded AST origins and immutable declaration fingerprints validate routing; unsupported
marker mutation/generated origins refuse. Host collection is never scoped cache authority.

First perform independently restricted discovery/preflight for every potential scoped
partition. Then run each ordinary suite session sequentially: fresh collection, membership
checkpoint, execute only ordinary items, teardown, terminate. Compare opaque membership,
origin and selection/order with the restricted inventory for mixed files. A mismatch parks
the invocation without starting scoped bodies. Never reuse ordinary inventories.
Scoped execution begins only after every ordinary suite session has finished. This first
version intentionally does not overlap ordinary sessions with scoped workers. One ordinary
session occupies one scheduling permit while alive; no restricted worker permit is held
then. At concurrency one there is no waiting ordinary process blocking scoped collection.
A bounded scoped controller may remain alive with its separately admitted headroom.

New/removed parameters must appear in real inventory; collection hooks cannot create
unregistered origins. A pure ordinary file does not need restricted collection. A mixed
file's restricted collection still needs all imports/parametrization inputs explicitly
visible; inability to collect it is an explicit scoped refusal, not permission to trust the
host inventory. Structural-file overrides narrow before AST/import; case selectors do not.
No fallback occurs after scoped execution starts. Ordinary effects may already have
happened when a later failure is reported, including import effects before checkpoint.

## Schema 3 exact changes

The [companion JSON contract examples](bubblewrap-schema-examples.json) contain complete
examples for the proposed stable fields below. Digests use synthetic valid 64-hex values;
examples are contracts, not claimed execution receipts.

Preserve outcome/count spellings and opaque case IDs, with the following explicit
schema3 extension to top-level run status. A kind=run status is exactly passed, failed,
no_tests, infrastructure_error, unsupported or declaration_error. The last two allow a
typed post-membership failure to retain already validated rows. Kind=refusal status is
exactly infrastructure_error, unsupported or declaration_error. CLI exits are passed=0,
failed=1, infrastructure_error=3, unsupported/declaration_error=4, no_tests=5, for either
kind where applicable. Cleanup/cancellation infrastructure takes precedence over typed
refusal, which takes precedence over test failure; an otherwise successful empty selected
invocation is no_tests. `error` appears on typed terminal failure and `capability` only
on unsupported; successful envelopes omit both. Add `route` to every
known unit and group: `ordinary` or `scoped`; null is allowed only for a known case whose
routing did not complete. Groups never have unknown route. `route_counts` has exactly
`ordinary`, `scoped`, `unresolved`, each containing the same seven count keys as `counts`.
A unit becomes reportable only after its opaque identity and actual selected/not-selected
observation are validated. Earlier raw collection is unavailable collection evidence, not
a unit with an invented selection flag. Routing reconciliation may subsequently fail,
leaving such a reportable unit with route=null. For each key, counts equals the sum of the three partitions. Each known unit contributes
exactly one outcome plus selected if true. Unknown/uncollected cases are not fabricated.
`counts.selected + counts.not-selected == len(units)`; all six outcome counts sum to that
length. A reused outcome can only belong to scoped. `routing_complete` is independent of
inventory_complete; complete routing requires every known case resolved AND all configured
effective structural partitions accounted. An empty complete inventory satisfies both.

Groups replace schema2 singular `file` with `files`: sorted unique structural file list.
A scoped group has exactly one file; an ordinary group can contain several files in its
suite. `case_ids` always preserves actual selected pytest order. Ordinary group profile is
`ordinary-v1`; scoped group profile is `case-v1` or `batch-v1`. Top-level execution keeps exactly configured mode/profile; those settings describe scoped
policy, not an ordinary sandbox claim. Add a separate exact three-field
`backend={name:"bubblewrap",profile:"bubblewrap-v1",identity:null|digest}`. Its identity is
null until supported entry-context preflight and backend provenance/resource validation
complete. A valid digest binds launcher bytes/closure, versioned systemd/resource/lifecycle
mechanism and observed supported host attachment. It grants no cache authority by itself.
No duplicate execution.backend field is emitted. Runtime retains its existing exact three fields. Ordinary group IDs bind suite/session context and ordered opaque
membership for reconciliation only, never cache authority. Every selected known case has
exactly one group once grouping_complete; unselected cases have null group_id.

Ordinary member outcome remains executed/failed/unsupported/infrastructure_error/not-selected with
actual passed/failed/skipped/not-run status, rather than inventing a competing fresh enum.
Fresh ordinary versus fresh scoped is explicit in route_counts and route. Ordinary groups
always cache_eligible=false, attempts=1 after their single session launches; they are never
automatically replayed after infrastructure failure. All their per-case timings are null.
Scoped group retry/atomic evidence and batch/case timing rules remain as previously defined.

Unmeasured timings become null, not zero. Reuse has measured current execution cost zero;
unknown CPU is null. Ordinary group wall interval covers its ONE collection+execution+
teardown session. Ordinary collection rows refer to accounting_group_id and have null
CPU/worker time, preventing double counting. Ordinary CPU is null/timing_complete=false
until a separately qualified complete measurement exists; RUSAGE_CHILDREN is insufficient.
Scoped CPU comes once from the final owned cgroup sample after empty-state synchronization.
The top `collection.cpu_seconds` sums only scoped collection observations when no ordinary
observation exists, otherwise null; it never includes execution group CPU.
Top worker_cpu_seconds is null if any actual session CPU is unavailable; add
known_scoped_cpu_seconds for explicitly partial scoped totals. It must not be called total
invocation CPU. A run of reused groups can still incur fresh collection/setup CPU.

Collection observations have unique identity `[name, route]` (suite name and actual
collection route), with `files`, outcome, complete, accounting_group_id and nullable
timings. A mixed file appears in BOTH observations; files and observed item counts do NOT
partition. Only reconciled case routes/counts partition. `collection.collected` counts
unique validated identities, never the sum of duplicate observations. The collection
aggregate is reused only when all required observations are valid reused records; it is
executed when complete observations include any fresh collection. Partial/failed
observations preserve failed/infrastructure_error/not-started rather than claim completion. Top `suites` rows are
`{name, files}` and expose the exact effective structural file lists before imports.
Ordinary observation timings belong only to its referenced session group; collection-only
ordinary sessions with no selected members still have an ordinary group with empty case_ids
and the actually collected files. No corresponding empty scoped execution group is invented. Inventory digest includes route and profile versions as
well as suite/file/case membership. Cache records additionally bind backend/runtime,
ordered group context, declarations, absence, metadata, source lookup, frozen source,
actual normalized limits and runner implementation. Schema2 records remain ineligible.

Precollection errors use schema3 kind=refusal with empty units/groups and zero counts, null inventory
and routing incomplete; no case route is invented. If structural membership is already
known, preserve top suites and the planned [suite, route] collection observations with
exact files, outcome=not-started, collected=null and null timings/identity. A truly
pre-manifest error has no invented structural rows and may have null unresolved runtime,
backend, execution, python and resource objects. A planned collection route is distinct
from an unresolved case route and does not claim collection ran. If any validated identity AND actual selection observation is already known,
return kind=run with partial inventories/nullable routing and explicit failure, preserving
known rows and whether any groups executed. A declaration failure discovered after actual
selection can retain unresolved-route units with outcome=unsupported, reason=declaration-invalid,
result_status=null, complete=false and attempts=0; top status remains declaration_error.
This is not a test failure or a fabricated call. API exceptions before report creation convert
through error_result. Refusal never implies no earlier host import or test execution.

## Resource claims and process layout

`resources.scoped` contains required, validated, configured worker/budget/concurrency and
nullable enforced fields. All-ordinary means no potential scoped work anywhere in the effective structural boundary,
not merely no selected scoped cases. Unselected scoped cases can still require restricted
inventory and set required=true. A true all-ordinary report sets required=false, validated=false,
concurrency=null, enforced=null, even if configuration contains a worker budget. It does
not contact Bwrap/systemd or validate whether that envelope fits a scoped worker. Syntax
errors remain configuration errors. Mixed/scoped validates before any restricted import. Before contacting an external user
service, the host entry performs the supported namespace/capability preflight INSIDE its
invoking admission context. An inherited denial refuses there; probing only in a less
restricted service would be a bypass and is forbidden. Read-only inspect creates no units
and claims no enforced resources; it validates identity and read-only compatibility evidence.
Enforced values are populated only from actual cgroup and tmpfs observations.

`resources.controller` separates configured headroom from nullable enforced values.
`resources.ordinary` states enforcement=inherited-host, slot_limit=1 and actual peak_sessions.
It does not claim the scoped budget limits ordinary tests. The caller must admit ordinary
work separately under its host policy; the benchmark will enclose both fresh paths in the
same outer cgroup when comparing authoritative CPU/memory.

A trusted host entry process retains the invoking environment and launches ordinary pytest.
For invocations with potential scoped work, a separate owned user service runs the bounded
scoped controller (default 1CPU/2GiB/64 processes) and owns cache locking/staging, preparation,
frozen views and worker dispatch. The API caller itself is outside that owned cgroup and
inherits host limits; reporting never claims otherwise. No project Python is imported in
the scoped controller. Structured controller IPC is nonce-bound and limited to64MiB; raw
collected node IDs are transient only in their collecting sessions.

The scoped worker pool is a sibling subtree under the owned total slice. Its cgroup enforces
worker budget CPU/memory/processes; each group/collection unit has worker limits and
quota-bound noexec tmpfs. Total slice limits equal controller plus worker CPU/memory/PIDs.
The lightweight host entry and ordinary sessions are explicitly outside that scoped slice.
No retained ordinary process is hidden in the worker-budget arithmetic.

Controller frozen-pool storage is host-visible, owner-only, engine-written and read-only
in workers; byte/entry limits are enforced by bounded streaming writes before allocation,
not claimed to be a kernel disk quota. This is trusted-engine storage, not writable test
scratch. No hardlinks to mutable host source. Reflink or copy-and-hash makes independent
files; fences detect changes during reads. Runtime+unique input+view materialization must
fit the explicit controller scratch/entry ceiling before workers launch. Retained generations,
verified wheel/archive downloads, temporary extraction/staged runtime, current invocation
freeze, source pool and view entries all count, including simultaneous old/new copies.
Preparation uses the same accounting and refuses before exceeding the ceiling; bounded
writes still handle actual ENOSPC as an infrastructure incident without corrupting the
prior valid runtime. Otherwise typed
`backend:pool-size` refusal. Inode accounting includes hardlinked view directory entries.
The host's ordinary execution is not a security boundary against hostile same-UID host
actors; the declared-view guarantee applies inside restricted workers. No live checkout
or mutable original runtime is mounted into those workers.

Preparation runs in a separately admitted owned controller unit with the same explicit
headroom. Worker tmpfs mounts are real kernel byte/inode ceilings; readonly snapshots and
runtime bytes cannot be written by tests. Local native checks fail unsupported userns or
controller prerequisites; no service bypasses the local admission policy.

## Immutable CPython preparation: distro and dedicated layouts

The selected interpreter is a trusted explicit input, not guessed from PATH. Support
Linux x86-64 glibc CPython3.11–3.13, both distro split layout and dedicated prefix, before
claiming replacement complete. Query only that interpreter with `-I -S` and fixed code to
obtain executable/real executable, prefixes, stdlib, platstdlib and extension suffixes.
Reject arbitrary sys.path injection, unsupported symlink escape or layout ambiguity.

Copy exactly the executable/validated aliases, stdlib source+data+extensions and their
native closure. Exclude every host site-packages/dist-packages directory, user site,
sitecustomize/usercustomize, host .pth startup entries and unrelated /usr programs/data.
Never copy /usr wholesale. Discover ELF PT_INTERP/DT_NEEDED/RPATH with a bounded parser,
resolve each SONAME uniquely against the explicit platform paths, and recursively pin
actual bytes and symlink topology. Do not execute ldd on arbitrary files. Unsupported
external dlopen/data dependencies refuse or need an explicit profile declaration; do not
silently mount /etc, /usr/lib or a host venv to repair them.

Keep copied stdlib file timestamps/modes and bytecode bytes coherent; project snapshots
remain separately epoch-normalized. Build a new clean venv layout using the copied
interpreter metadata and freshly installed exact wheel pins, not the caller's site-packages.
Use wheel-only/no-deps resolution with a fully pinned transitive requirements file; record
actual wheel archive SHA256 and platform tags. No sdist, editable install, local application
install or arbitrary build hook. Preparation writes a complete content manifest containing
source identity, native closure, package/wheel provenance, modes/mtime_ns/symlinks, policy,
runner/supervisor/projection bytes and backend executable identity. Repeating with identical
inputs produces the same manifest; changed host packages produce a different identity.

Reproducible preparation commands (proposed CLI; implementation must qualify both):

```sh
# A normal Debian12 system CPython3.11, provisioned by its administrator/package manager.
python3.11 -m venv .telescope-tools
.telescope-tools/bin/python -m pip install pytest-telescope==<qualified-version>
.telescope-tools/bin/python -m pytest_telescope runtime --interpreter /usr/bin/python3.11 --requirements telescope-runtime.txt --bubblewrap /usr/bin/bwrap
# A separately installed dedicated-prefix CPython (the same API, no special host mounts).
.telescope-tools/bin/python -m pytest_telescope runtime --interpreter /opt/cpython/3.12.9/bin/python3.12 --requirements telescope-runtime.txt --bubblewrap /usr/bin/bwrap
```

The dedicated-prefix provisioning path already used by the hosted proof is pinned
`actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1` with
`python-version: '3.12.9'`; its `python-path` output is the explicit `--interpreter`
input. This supplies an executable installation path, not a requirement to compile Python
or use an unspecified existing `/opt` directory. A distro installation instead selects
its package-managed `/usr/bin/python3.11`. Both selected byte closures must be qualified
by production tests. Installation of pytest-telescope uses the reviewed candidate wheel
until a qualified version exists; the command placeholder does not name a published version.

`telescope-runtime.txt` contains these exact lines:

```text
pytest==8.3.5
packaging==24.2
pluggy==1.5.0
iniconfig==2.0.0
```

Additional platform/Python-dependent requirements must also be exact pins; an incomplete
transitive closure refuses rather than performing dependency resolution during run. The wheel lock/manifest is retained; exact interpreter/native bytes are
explicit local input identities, not a claim that floating OS package installation is
reproducible. A first-run preparation must prove the frozen copy runs with no original
interpreter tree/site-packages mounted, including stdlib extension imports, -I children
and absent undeclared host files. Dedicated-prefix copying alone is not sufficient.

Prepared runtime cap:2GiB/100,000 entries (same prior2GiB runtime ceiling). One invocation
may need a larger explicit controller storage envelope than its default1GiB; refuse before
execution when runtime+inputs cannot fit. At most4 retained owned runtime generations;
protect current valid state until replacement succeeds. Atomic staged publication, fsync,
manifest checksums and current-run independent freeze preserve old valid state on failure.
Per invocation, verify/freeze the runtime even on warm results; do not claim the prototype's
one-time freeze is the final per-run cost. Benchmarks must include that cost.

## Pinned Git ABI and isolated children

Do not reinterpret git-local-v1. Its Debian13 Git2.47.3 binary/libc2.41 closure is not the
Ubuntu22/glibc2.35 host Python closure. Proposed `git-local-v2` preserves the local Git
operation set but packages that exact native tool closure in a private runtime directory.
Its explicit loader launches Git with private `--library-path`, `--inhibit-cache` and
`--argv0`, leaving Python's native loader/library paths untouched. Trusted runtime Python
entry scripts (not scratch scripts; no shell) dispatch git and the three local transport
aliases to that private loader with exact argv0, then exec. Generated launcher source and
bytes are part of the v2 manifest and cache identity. No global LD_LIBRARY_PATH modification.

Fetch and verify the exact Git/libc/pcre2/zlib Debian archives before inert extraction;
project only the existing native-file allowlist, SONAME links, copyright and empty templates.
No package scripts or apt inside workers. Existing Git archive and native-file hashes remain
comparison anchors; independent library archive hashes must be fixed and reviewed before
proof. Debian's libc6_2.41-12+deb13u4 archive is2,846,580bytes/SHA256
967aa62605721081c3eb2a17650611a792aa802d76a6511d1840242623d204c9. Other archive identities
will be recorded from primary package metadata and actual verified bytes, not guessed.

This loader/trampoline mechanism is UNPROVEN: qualify every alias, local clone/fetch/push,
merge/worktree operation, controlled fault/recovery, local child execution and Python -I
alongside it before production integration. If direct loader invocation conflicts with
Git executable discovery, stop with actual evidence rather than broadening host mounts or
silently replacing Git. Profile v1→v2 is an explicit configuration/API migration and complete
cache invalidation; requirement string remains git-local. Bash/browser/services/hooks and
executable scratch remain unsupported. No PATH Git substitution.

`isolated-source-v1` remains default-off, ordered per-suite roots, fixed verified empty
runtime .pth slot, read-only per-view data-only path projection, <=64 roots/256KiB.
Root/source metadata collision and startup-hook preflight run on ALL views before workers;
ordinary sessions do not receive the projection and use their real host import environment.
Runtime-only collision inspection excludes selected projected metadata. -I retains normal
isolation flags; -S retains standard disabled-site behavior. Root ordering, namespace packages,
metadata omissions and source denial need new backend qualification.

Primary sources for loader/package design:
https://man7.org/linux/man-pages/man8/ld.so.8.html
https://packages.debian.org/trixie/amd64/libc6/download
https://packages.debian.org/trixie/amd64/zlib1g/download

## Bubblewrap provenance and failure ownership

Preparation records the actual supplied non-setuid/non-setgid Bubblewrap executable bytes,
version, architecture, ELF closure, path identity and security attachment observed by a
bounded real launcher preflight. First qualified version is upstream0.9.0, with the existing
source archive/commit pins; distribution builds require explicit qualification, not a
version-string-only assumption. Systemd version/controllers and namespace capability are
measured. File changes invalidate prepared identity; unknown/denied host profiles refuse.
Never relocate a path-attached LSM executable merely to escape a policy. The launcher is
an explicit trusted host prerequisite; runtime application content is still frozen.

Administrator setup is distinct from run: the measured ephemeral route stopped the exact
UID manager, proved its previous cgroup absent, installed an instance-only /run drop-in,
then started/verified it. Normal package execution never stops/reconfigures a workstation's
user manager, uses sudo, changes AppArmor/sysctls or provisions delegation. Documentation
must give explicit administrator prerequisites, and run must refuse when they are missing.

Each setup/inspection/preparation dependency has one retry owner: at most2 attempts for
retryable I/O/transport; <=2 retrievals total per archive per preparation, reusing already
verified bytes between attempts. Format/hash/layout mismatch is terminal. Preparation wall
cap300s/attempt; source archive cap16MiB each, wheel artifacts bounded by runtime cap.
Worker collection/group attempts remain at most2, with fresh scratch and complete cleanup
between attempts. Ordinary sessions are attempted once because replay can duplicate effects.
No nested retries multiply a bound. Exhaustion returns typed infrastructure evidence.

Port the reviewed pending pid1-reaper-v1 supervisor and behavioral controls into the Bwrap
PID namespace; do NOT retain main's end-only reap. It owns only pytest plus adopted children,
reaps continuously with4096calls/50ms drain turns, preserves direct Popen/waitpid statuses,
concurrently drains bounded framing, and validates final worker exit/result/EOF/live-process
absence before eligibility. Bwrap launch must exec this supervisor as actual PID1, verified
by proof; disable new nested namespaces and retain totalPID limits. Do not use daemon init.

After protocol completion, the trusted wrapper exits; sample owned cgroup CPU once after
pids.current=0 but before deleting the unit/cgroup. It includes wrapper/Bwrap/supervisor/test/
all descendants; never add child-reported CPU again. Read final cpu.stat or fail measurement
explicitly. Keep successful unit cgroup until the controller acknowledges sampling, using
an explicit unit lifetime/RemainAfterExit contract to prove; do not assume auto-GC preserves
counters. Cancellation stops submissions and owned units, waits boundedly for empty groups,
then removes only exact owned names/paths. Failed removal/unknown ownership is terminal,
blocks every new staged record and never force-deletes unrelated resources.

## Remaining foreground proofs and delivery queue

After this exact seam review, create native implementation, delegated behavioral-test,
delegated documentation, matched parallel benchmark and complete gate/publication children.
Implement runtime preparation, Git loader/aliases and ongoing supervisor integration in the
ACTUAL production paths with failing behavioral/native controls, rather than creating a
second disposable prototype implementation. Resource feasibility is already proved.
Keep those runtime/lifecycle mechanisms explicitly unqualified until their controls pass;
a measured contract-boundary failure returns for foreground review before dependent work.
The focused controls include distro/dedicated runtime isolation, local Git aliases, final
owned cgroup CPU lifetime, original child statuses, live-child refusal, bounded malformed/large
protocol and cancellation. No separate unchanged full test run precedes the guarded gate.

The remote gate proposal stays one exact-head guarded row: scripts/check.py --executor
github-actions pushes a unique owned candidate ref and accepts only complete same-source,
workflow, wheel, resource and cleanup receipts. Native tests cannot skip. Before delivery,
prove bounded timeout/cancel/retry/changed-head rejection and run the full installed suite,
strict docs and supported Python/pytest corners on the qualified host. Local denied-userns
is explicit unsupported, never bypassed through a launcher service. Old Docker qualification
and sequential prototype timings do not substitute for the replacement gate.

Matched parallel benchmark uses the same fresh sessions/selected tests/runtime and total
admission at1/2/4 workers, ordinary and Bwrap paths each enclosed in equal outer cgroups.
Measure actual concurrency, deterministic outcomes, isolated views, complete CPU, cleanup,
and end-to-end runtime freeze/project planning. Keep launch-only separately. Do not compare
32 fresh sessions to one batch as pure isolation overhead.
