# Ordinary execution and Bubblewrap scopes

This is the accepted routing and fixture contract for the proposed Linux backend.
The backend, runtime preparation, resource mechanism, result schema and complete gate
are still under investigation. The existing implementation has not adopted these rules.
The separate fresh prototype benchmark does not qualify a production backend.

## Ordinary is the default route

Previously an unmarked case caused whole-project snapshotting and hashing, followed by
restricted execution. Under this contract, a case without an effective literal
`inputs`, `bundles` or `distributions` declaration runs through ordinary pytest, always
fresh. Declarations inherited from module, class, function and `pytest.param` levels
count. A literal empty list is still explicit scope. Defaults alone, absence-only
assertions and a policy-only `cache=False` marker do not opt a case into scoped execution.
Malformed or unsupported markers remain errors rather than becoming ordinary cases.

For example, `@pytest.mark.telescope(cache=False)` alone is ordinary and fresh;
`@pytest.mark.telescope(inputs=[], cache=False)` is scoped and fresh. A module-level
`pytestmark = pytest.mark.telescope(bundles=['shared'])` scopes all its descendants.
Selected local distribution metadata remains distinct from installed runtime packages.

Ordinary execution uses the invoking Python interpreter, installed pytest/plugins and
ordinary environment, with each suite's actual cwd, rootdir, configuration and arguments.
It does not use the clean scoped runtime. Ordinary results are never reused and must
never be described as sandboxed or cache-authoritative. An all-ordinary project does not
require Bubblewrap, prepared runtime artifacts or scoped-resource prerequisites.

Ordinary-only discovery reads bounded structural test/configuration source needed for
routing and normal collection. It does not eagerly walk, stat, copy or hash unrelated
repository content. An unrelated ignored build tree, socket or directory symlink is not
an implicit snapshot error. Explicit declarations retain their strict path and input
validation. Absence-only ordinary markers still validate only their explicitly named
absence paths; they do not trigger a broad snapshot. Explicitly broad inputs may still hash the content they declare.

## Mixed collection and fixture boundaries

Each suite has one fresh ordinary pytest session for its ordinary cases. Files with a
validated unconditional inherited module scope stay out of ordinary collection. Files
that can contain ordinary cases may be imported along with their conftest in ordinary
context. In a mixed module, those imports and collection hooks are ordinary-context
side effects; they are not isolated collection evidence.

At the validated ordinary collection checkpoint, every scoped item is removed before
fixture setup or its test body. Unsupported generated marker origins, marker mutation,
duplicate identities and post-checkpoint inventory changes refuse. Pytest never dispatches scoped fixtures or case bodies on the ordinary route. Ordinary
import code remains arbitrary Python and could directly call those functions. Ordinary session/module/class fixtures retain normal
pytest lifetime among ordinary cases in that suite. They do not share state with scoped
workers. Separate suites remain separate initialization boundaries.

Scoped discovery is independent: it uses a restricted declared collection union and
real pytest, never broadening because ordinary cases are possible. Its validated
membership, declarations and selection supply scoped authority. Mixed-module inventories
must agree across ordinary and restricted collection; disagreement refuses without
widening or retrying ordinary execution. Dynamic case additions/removals and unselected
cases remain accounted. Shared imports and parametrization dependencies belong in the
scoped declarations when restricted collection needs them.

Perform scoped discovery and required preflight before retaining the ordinary session.
Then collect ordinary cases, validate checkpoint agreement and execute that session.
At concurrency one it finishes before scoped execution begins; no retained ordinary
process may silently occupy an unaccounted slot or deadlock restricted collection.
Ordinary collection is always fresh. A refusal never proves no earlier import hook ran.

Selectors keep actual pytest semantics. `-x` and `--maxfail` apply to each real pytest
invocation, not an invented global fixture session across the split. Effective xdist
scheduling remains explicitly unsupported; overridden inert addopts do not cause a
refusal, and an installed but inactive plugin is not active scheduling. Direct pytest
adapter routing must remove its controller flag without recursive dispatch.

## Fresh scoped cases and parallel batches

Case mode remains the default; batch mode remains explicit opt-in. A batch contains only
one suite and source module with exactly equal effective readable files, expected absence,
selected metadata, runtime/source-lookup settings and cache policy. Groups do not union
unequal scopes. Members run sequentially inside one fresh sandbox/session/private scratch,
sharing ordinary pytest fixture lifetime within that group. Independent groups may run
concurrently within their explicit admitted envelope. Workers are not reused across groups.

Ordered selected membership and context participate in group identity. Any selected
member miss reruns the whole group; successful evidence cannot mix sessions. No ordinary
result can satisfy a scoped record, and no fallback widens a started scoped run.
The existing group-size, protocol, cleanup and staged-cache guarantees remain required.

## Runtime, resources and lifecycle requirements

A scoped runtime is an explicitly prepared, frozen content-identified interpreter,
third-party wheel and native-tool/library closure. It is not a live host checkout or venv
mount, an application install, or an opportunistic executable from PATH. Runtime files,
symlink topology and bytecode-relevant metadata must match their recorded identity.
Preparation publishes atomically, retains prior valid state on failure, and has bounded
retries. Run and read-only inspection must not silently prepare a missing runtime.
A dedicated-interpreter layout may be the first supported layout only with a reproducible
preparation path and explicit refusals elsewhere. Ordinary-only execution needs none of it.
The existing Git runtime and isolated-child source lookup profiles remain required
implementation/qualification work; a replacement is not complete by dropping them.

Bubblewrap supplies namespaces, not resource quotas. Scoped execution must enforce real
per-worker CPU, memory, process and private scratch limits, plus the aggregate worker
budget. Preserve the existing budget meaning: aggregate worker capacity. Separately bound
controller/setup memory/CPU/process use and frozen-pool/scratch headroom; complete admission
evidence includes both. Scheduler counts or RLIMIT_AS alone cannot substitute for quotas.
All setup, supervisor, test and descendant processes count toward worker ceilings.

The initial feasibility candidate uses ordinary-user systemd units with private user/mount
namespaces, cgroup-v2 limits and quota-bound noexec/nosuid/nodev tmpfs. It is unqualified
until actual effective controllers, numeric limits, mount flags and negative controls pass.
A supported host may require administrator provisioning of a standard user manager and
resource delegation, but normal production launch must not use a privileged helper.
Unsupported prerequisites refuse; neither global security-policy changes nor a broader
execution path are an alternative. Source and runtime remain read-only and networking
remains disabled before scoped project imports.

Lifecycle must preserve test-owned child statuses, reap adopted completed descendants
throughout long sessions without stealing children, and refuse live descendants after
teardown. Protocol capture is bounded and drained concurrently. Cancellation stops
submissions and terminates only owned resources, with bounded cleanup before any new
cache evidence becomes eligible. Complete worker CPU must come from qualified cgroup or
equivalent complete accounting, counted once. The prototype's RUSAGE_CHILDREN values do
not capture all sandbox descendant CPU and cannot support CPU comparisons.

## Proposed reporting and delivery seam

The proposed schema 3 adds explicit ordinary/scoped routes and disjoint route count
partitions while retaining stable opaque case/group identities and outcome conservation.
Every known resolved case/group has its actual route. Before real collection resolves
membership, refusal/incomplete envelopes must represent unavailable routes as null or
omit nonexistent rows; they must not invent a route. Ordinary groups represent actual
suite sessions and can span files. Their cache eligibility is always false. Exact API
and field shapes require review before implementation; existing schema 2 consumers must
not interpret this proposal as implemented behavior.

A qualified complete gate must build/install the exact submitted source and run all native
controls, behavioral tests and strict documentation. A proposed explicit hosted executor
inside one guarded landing row binds source/tree, workflow, artifact hashes, enforced
resources, terminal verdict and owned cleanup to a unique candidate job. It accepts only
its exact head and complete successful receipt, has bounded retries/deadlines and cancels
only its owned run. Local execution must fail unsupported native prerequisites rather
than skip. Existing sequential timing evidence is not a substitute for this gate.

Parallel fresh benchmarks compare identical workloads, selected cases, session counts and
resource budgets at 1/2/4 workers, with result caching disabled. Report launch-only rows
separately from end-to-end rows that include per-invocation runtime verification/freezing,
project planning, source materialization and cleanup. Record actual overlap and isolated
views, full outcomes, sample order/dispersion and complete CPU accounting. Comparing many
fresh sessions to one session measures a fixture/startup change, not pure sandbox cost.
