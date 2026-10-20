# Pinned local Git runtime profile

**Replacement status: UNQUALIFIED.** `git-local-v2` is the explicit proposed migration
from the Docker `git-local-v1` profile. The local Git operation boundary remains required
work; it is not removed to complete the replacement. Historical Git mechanism/candidate
receipts do not qualify the new runtime or establish a 0.1.0a4 release.
The [API/runtime contract](bubblewrap-api.md#pinned-git-abi-and-isolated-children) owns
exact loader, closure and migration rules.

## Selected capability and API

```toml
[tool.pytest_telescope.runtime]
profile = "git-local-v2"
requires = ["git-local"]

[tool.pytest_telescope.execution]
backend = "bubblewrap-v1"
```

`python-v2` is the proposed default; requiring `git-local` with that profile refuses
with `runtime:capability`. Unknown profiles/versions use `runtime:profile`. Runtime
selection is separate from case/batch mode, source lookup and literal scope markers.
Ordinary cases still use their invoking environment and do not receive prepared Git.
Scoped cases still receive only their declared project inputs; choosing Git grants no
application source, host repository, writable source or additional capabilities.

CLI `runtime`, `run` and `plan` retain `--runtime-profile`; `run`/`plan` accept repeated
`--require-capability`. Configured/caller requirements form a sorted unique union, bounded
to 16 strings per supplied list. A caller cannot remove configured requirements. See
[the interface reference](result-schema.md#runtime-profiles) for exact Python signatures.

Preparation is explicit. Missing, stale, mismatched or corrupt prepared state is an
infrastructure failure; neither run nor read-only inspection repairs it. Inspection
returns exactly `{profile, required_capabilities, identity}` and supplies identity only,
with at most two retryable attempts and no state/cache writes. A wider runtime containing
Git is not a substitute for the requested exact profile. Static planning establishes no
prepared-runtime or host-platform qualification.

The [local Git example](https://github.com/xu-hao/pytest-telescope/tree/main/examples/git_local)
keeps the literal input marker, fixed identities/dates, scratch repository and local bare
origin round trip. Its v2 configuration demonstrates the proposed migration, not a
qualified execution receipt.

## Tool provenance and private ABI closure

Do not reinterpret v1. Its Debian 13 Git 2.47.3/libc 2.41 closure is distinct from an
Ubuntu 22/glibc 2.35 Python closure. V2 proposes that exact tool closure in a private
runtime directory, launched through its own explicit loader with `--library-path`,
`--inhibit-cache` and exact `--argv0`. Trusted immutable runtime Python entry scripts
exec Git and its three local transport aliases; they are not scratch scripts and use
no shell. Python's native loader paths remain untouched; no global LD_LIBRARY_PATH or
PATH Git substitution is allowed. Launcher source/bytes bind runtime identity.

The existing Git archive comparison anchor is Debian's
`git_2.47.3-0+deb13u1_amd64.deb`, 8,861,572 bytes, SHA256
`3e35662fd5c46add561703e54031a1d8ad9df45811927689f0a51122b13be722`.
The [API/runtime contract](bubblewrap-api.md) records the accepted libc archive anchor
and remaining exact library archive verification. Verify Git/libc/pcre2/zlib archives
before inert extraction; project only the native allowlist, SONAME links, copyrights
and empty templates. No apt, package scripts, remote helpers, editors or optional tools.
Unknown closure/ABI/layout refuses instead of adding host mounts or floating packages.

Each dependency has one retry owner: at most two retrievals per archive/preparation,
reusing already verified bytes; hash/format/layout mismatch is terminal. Preparation
has a 300-second attempt cap, 16 MiB archive cap and bounded runtime/controller storage.
Atomic publication and cleanup preserve the prior valid runtime on failure. The
[explicit upgrade path](execution-contract.md#explicit-rebuilds-after-runtime-upgrades)
leaves old Docker artifacts untouched and ineligible. V1 to v2 requires explicit
configuration/API changes and complete cache invalidation; `git-local` remains the
capability string.

## Restricted local repository behavior

Within private writable `/tmp`, support `git init`, local `git config`, explicit-message
commits, bare local origins, push, local clone/fetch, ref/revision inspection, `merge-base`
and worktree add/remove. Repositories and worktrees are scratch objects; declared project
inputs stay read-only, and host version-control metadata remains excluded. Tests copy
only declared input content to scratch when they need tracked writable files.

Workers retain nonroot execution, dropped capabilities, network none, IPC none,
read-only root/project mounts and private nosuid/nodev/noexec scratch. A Git profile
never changes CPU, memory, process or scratch budgets. Git subprocesses count within
the worker limits; parallel groups still fit the explicit aggregate envelope. Default
worker limits remain one CPU, 512 MiB, 128 processes and 64 MiB scratch. A larger local
repository needs an explicitly larger admitted scratch/memory envelope, not fallback.

The environment isolates system/global configuration from the host. Private HOME and
Git configuration live in scratch; system configuration is disabled. The profile sets
local-file transport policy and an empty immutable template directory. Hooks are disabled
by default and executable hooks/generated executable scratch programs are unsupported.
Noexec is a mount execution restriction, not a claim that Python cannot interpret data.
The proposed native loader and trusted Python entry scripts require no shell dispatch.
They do not qualify a general shell-script or Bash capability. Scoped networking is
disabled regardless of Git configuration. SSH/HTTP transports and external helpers are
outside this profile even if a caller changes Git settings.

Git object identity can depend on author/committer dates, identity, configuration and
input content. Deterministic tests explicitly set those values and avoid clock-dependent
expectations. The profile does not invent identities or freeze clocks. `cache=False`
retains its existing meaning and is not a determinism workaround.

A missing local origin, an obstructing lock file or a directory at `FETCH_HEAD` yields
ordinary bounded Git failure that a test can assert. It is not a dependency outage and
must not trigger a broader worker or automatic command replay. Removing an intentional
obstruction and retrying inside the test is an explicit test action. Engine retries
remain limited to infrastructure failures, with fresh scratch and complete owned cleanup.

## Cache, accounting and qualification

Runtime identity binds profile, requirements, archive/native closure, launcher bytes,
controlled environment, platform, preparation policy and frozen content. Case/group
identity additionally binds actual scoped inputs, ordered membership, source lookup,
backend and enforced resource policy. Case-v1/batch-v1 atomic staging remains unchanged;
Git never creates mutable sessions between groups. Ordinary evidence is never reusable.
[Schema 3](result-schema.md) keeps runtime's exact three fields separate from backend
and execution identities; no host executable paths or arbitrary Git stderr enter results.

The loader/trampoline mechanism is UNPROVEN. Production controls must exercise every
alias, local clone/fetch/push, merge/worktree operations, controlled faults/recovery,
local child execution and Python `-I` alongside the separate Python closure. A failed
executable-discovery boundary must return for review instead of widening mounts or
substituting Git. Preparation controls include tampering, missing closure, bounded
retrieval exhaustion, quotas and preservation of prior valid state.

Installed controls must additionally cover both execution modes, metadata/source
visibility, warm reuse, no-cache policy, profile/capability refusal, inspection,
cancellation, continuous descendant reaping, complete CPU accounting and exact cleanup.
Only the bounded resource mechanism has passed
[run 39503556501](https://github.com/xu-hao/pytest-telescope/actions/runs/39503556501).
That proof does not qualify Git, runtimes or the complete backend gate. Bash, browsers,
services, remote transports, executable hooks and executable scratch remain unsupported.
