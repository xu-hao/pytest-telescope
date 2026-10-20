# Worker process lifecycle

This is the 0.1.0a4 worker lifecycle contract. Release qualification requires the
complete guarded installed-distribution gate. The [execution contract](execution-contract.md)
continues to govern source visibility, isolation, result eligibility and cleanup.

## Process ownership

Each collection, case and batch container starts one versioned supervisor as PID 1.
The supervisor is shipped in the prepared image and starts one separate Python pytest
worker. It is not Docker's host-supplied `--init` binary. Both processes count against
the existing 128-process limit, CPU, memory and private scratch envelope. No limit is
raised. Read-only source/root, nonroot execution, no network and noexec scratch remain.

The supervisor only waits for its own children: the pytest worker and descendants
adopted after their previous parent exits. A test's ordinary direct children remain
owned by that test process. The engine does not install a SIGCHLD handler, call a
background `waitpid(-1)`, or change subprocess return statuses inside pytest. Tests
retain normal `Popen.poll`, `Popen.wait`, explicit `waitpid`, signal and fixture behavior.
Session/module/class fixture sharing remains exactly the selected case/batch policy.

While pytest runs, PID 1 nonblockingly reaps completed adopted children. Each drain
turn is bounded to 4,096 wait calls and 50 ms, followed by bounded output handling;
a live forking process cannot extend a turn indefinitely. SIGCHLD wakes the supervisor,
with a bounded polling interval as a backstop. The controller's existing invocation
deadline bounds the whole session. There is no waiting for live children to finish,
background killing to make a result pass, or disabling native application behavior.

## Completion and protocol

The pytest worker emits only provisional evidence after all pytest teardown. PID 1
drains its pipe while waiting for worker exit, so a large collection or batch result
cannot deadlock behind a full pipe. Captured arbitrary output and structured output
share the existing per-invocation bound: 1 MiB for a case and 64 MiB for collection or
batch. The supervisor accepts exactly one recognized result or unsupported frame;
malformed, duplicate, truncated or oversized records cannot become successful evidence.
The mode and request are read from the same bounded immutable request used by pytest.

After observing and reaping the pytest worker, PID 1 performs one final bounded drain
of already completed children and available output. It scans `/proc`, excluding only
itself, and requires a fully drained closed pipe when no descendants remain. Any
remaining live process or zombie makes the invocation ineligible. The worker's own
exit/signal and record exit status must agree. PID 1 does not emit eligible evidence
before this synchronization. Outer owned-container cleanup still precedes controller
validation/staging and cache commit.

`TELESCOPE_UNSUPPORTED` retains its specific supported capability refusal only after
clean worker termination and final lifecycle validation; lifecycle failure takes
precedence. Unexpected worker exits/signals, missing/malformed records, output limits,
supervisor faults and remaining descendants are infrastructure failures. An ordinary
pytest assertion failure with matching exit status 1 remains failed test evidence,
not an infrastructure incident. Existing
bounded fresh-attempt retry and cancellation/drain rules apply, with no new nested
retry owner. A cancelled/failed attempt supplies no reusable partial success. Cleanup
may terminate an already failed owned container; that does not turn it into success.

An exhausted retryable worker descendant/output incident makes that execution
noncacheable. If its owned Docker cleanup succeeds, an independently successful
collection can still publish its inventory. A terminal `CleanupError` or cancellation
blocks publication of every newly staged record, including inventory. These are
separate outcomes; worker refusal alone does not imply failed container cleanup.

Supervisor faults use one bounded internal `TELESCOPE_INFRASTRUCTURE` frame with
exact fields `kind` and `nonce`. The kind is one of `descendant`, `output` or
`protocol`, and the nonce must match the invocation. Duplicate, conflicting or
malformed frames remain generic infrastructure failure; they cannot supply test
results or successful cache evidence. This internal diagnostic framing adds no
public result fields and shares the invocation's output bound.

## CPU, timing and identity

The authoritative CPU sample is PID 1 `RUSAGE_SELF` plus its final
`RUSAGE_CHILDREN`, once, after worker and completed adopted children are reaped. The
kernel child aggregate includes descendants already waited for by their own parents.
The worker's provisional CPU sample is not added again. Wall time starts before the
worker is launched and ends at final lifecycle sampling; final serialization and
outer Docker removal are outside that worker interval. Existing group attempt/wall
accounting and incomplete timing semantics remain unchanged.

The proof also records each `wait4` user/system time independently. Linux truncates
nanoseconds to microseconds for each reported component. For N reaped processes,
aggregate microseconds minus the sum of individual microseconds lies between zero
and N-1 separately for user and system time. That bound follows integer truncation,
not a relaxed floating-point tolerance. See the primary Linux
[getrusage implementation](https://github.com/torvalds/linux/blob/v6.8/kernel/sys.c),
[time conversion](https://github.com/torvalds/linux/blob/v6.8/kernel/time/time.c), and
[child accounting](https://github.com/torvalds/linux/blob/v6.8/kernel/exit.c).

The in-image lifecycle mechanism is `pid1-reaper-v1`. Its version and exact supervisor
bytes join the runtime recipe, prepared state, image identity and runner implementation
hash. Current execution/inspection requires the matching lifecycle identity. Existing
prepared images require an explicit rebuild; neither execution nor read-only inspection
prepares them implicitly. Changed runtime identity invalidates inventories and case or
batch cache evidence. No public API argument or result field is added: the three-field
`runtime` envelope, `python.suites`, execution profiles and case/group schemas remain.

The isolation policy remains `docker-nonroot-ro-none-v4`; this correction changes the
keyed lifecycle implementation, not the filesystem/network/privilege policy. Retained
owned v4 images remain eligible for strict historical cleanup under their known label
format, as do published v2/v3 images. They gain no current execution authority. Explicit
rebuild retains current-image protection, bounded preparation/inspection retries,
registry quota checks and terminal ambiguous-removal refusal.

## Mechanism evidence and behavioral qualification

A synthetic completed-orphan workload exhausted the old fixed limit after 126
iterations: 126 state-Z children had PPID 1, and creation failed with errno 11. Final
reaping removed all of them, too late for the session. A separate 160-operation local
Git-status control created no accumulation. The evidence attributes the failure to
completed adopted children, not to Git status itself.

The scratch supervisor proof exercises collection, a 160-iteration case, a 160-case
session, original child statuses, fixture setup/teardown, live-child refusal, abnormal
worker exit/signal, early unsupported framing, large-result drainage, explicit output
exhaustion and cancellation. Numeric process counts, per-reap accounting and exact
owned cleanup are retained. Initial proof instrumentation failures are retained too:
a strict float-sum assertion did not account for per-report truncation, and diagnostic
capture initially tried to serialize bytes. A later embedded-script edit had an
indentation error; embedded proof sources are now compiled before preparation. Those
failures are not production verdicts.

Mechanism evidence alone does not qualify the production controller, cache or runtime
builder. Installed-distribution behavioral controls cover both runtime profiles, long
collection/case/batch sessions, direct child exit and signal statuses, session fixture
teardown, worker-free warm reuse, live descendants and actual sibling cancellation.
A clean collection can retain its separate inventory when later execution fails;
failed execution supplies no reusable case or group evidence. Terminal outer cleanup
failure prevents all newly staged cache publication.

Qualification also requires image/runner identity and explicit upgrade, malformed and
oversized IPC, bounded retry/exhaustion, atomic cache commit, supported host/worker
corners and the existing isolation regressions. Targeted internal draft results remain
separate from the complete guarded installed-distribution release verdict.
