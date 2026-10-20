# Pinned local Git runtime profile

This is the 0.1.0a4 contract for a separately qualified runtime profile.
Version 0.1.0a3 and earlier releases remain immutable. The mechanism proof and targeted
qualification below establish the profile boundary; the release also requires the
complete guarded installed-distribution gate.

## Selected capability and API

The existing runtime becomes the explicitly named `python-v1` default. An invocation
may select `git-local-v1`, initially qualified for Linux/amd64 only:

```toml
[tool.pytest_telescope.runtime]
profile = "git-local-v1"
requires = ["git-local"]
```

`runtime.build_runtime(root, requirements=None, *, runtime_profile=None)` prepares the
configured profile or explicit override. `controller.run(..., runtime_profile=None,
required_capabilities=None)` and `plan.make_plan(..., runtime_profile=None,
required_capabilities=None)` use the same normalization. CLI `runtime`, `run` and `plan`
accept `--runtime-profile`; `run` and `plan` accept repeated `--require-capability`.
Overrides are invocation-only and never rewrite configuration. Configured requirements
and caller requirements are a set union; a caller cannot remove a declared requirement.
Each requirements value is a list of at most 16 strings, normalized to a sorted set.
The only additional capability in this version is `git-local`; unknown profiles
(including unknown profile versions) produce `runtime:profile`, and unknown or unmet
capabilities produce `runtime:capability`. Neither selects a broader runtime.

These are invocation-level requirements, not a central case registry. Structural suite
selection remains the mechanism for choosing a supported partition. Selecting a Git
profile makes its immutable tool payload available throughout that invocation, while
per-case/group project file visibility remains unchanged. It does not confer application
source visibility. No marker syntax changes are required.

Known profile/requirement incompatibility is checked before Docker, collection, tests,
or cache lookup: `python-v1` with `requires=["git-local"]` produces typed unsupported
`runtime:capability`. Unsupported architecture produces `runtime:platform`. Missing,
stale, mismatched or corrupted prepared runtime state is an infrastructure failure,
with the existing bounded recovery policy; it is not silently prepared during `run`.
The prepared profile must exactly match the requested profile, even when a wider image
happens to contain the requested executable. `plan` proves static compatibility only.

`runtime.inspect_runtime(root, *, runtime_profile=None, required_capabilities=None)`
returns the same three-field `runtime` envelope as a successful run. It applies the same
requirements union and requested-profile validation, and owns at most two retryable
inspection attempts (including the backend platform probe). Typed unsupported mismatches
are immediate. Inspection never prepares, downloads, recovers, collects, accesses the
case cache or writes project/state files. Missing/stale/corrupt prepared state is an
infrastructure failure. Malformed inspection JSON, missing/non-table ownership labels
and invalid platform encoding also refuse with bounded typed infrastructure evidence;
inspection never removes a foreign or unlabeled image. The opaque identity supplies no execution, cache or cleanup
authority; callers must not reconstruct it or multiply its bounded retries. Preparation
likewise permits at most two backend platform probes before any build.

The repository includes a runnable
[private local Git example](https://github.com/xu-hao/pytest-telescope/tree/main/examples/git_local) with a literal input marker,
fixed author/committer dates, a scratch repository and a local bare-origin round trip.

## Tool provenance and reproducible payload

The profile extends the existing immutable Python base, not the host installation:
`python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1`.
The initial Git payload is selected from Debian's amd64 package
`git_2.47.3-0+deb13u1_amd64.deb`, exact size 8,861,572 bytes, SHA256
`3e35662fd5c46add561703e54031a1d8ad9df45811927689f0a51122b13be722`.
[Debian publishes the package version, size and digest](https://packages.debian.org/trixie/amd64/git/download).
The upstream/debian source is identified by
[the matching source package](https://sources.debian.org/src/git/1%3A2.47.3-0%2Bdeb13u1/).

Project only the native `git` executable, its required local transport aliases and the
package copyright/license record. Do not run package maintainer scripts, apt resolution,
Git build hooks or host binaries. Do not install Perl commands, remote helper plugins,
SSH clients, credential helpers, editors, Bash/browser/service tooling or package-wide
optional features. The native dependency closure consists of the verified executable
and the exact loader/shared-library files already in the digest-pinned base; record and
verify those paths, hashes and base package versions before publishing the profile.
If any dependency is missing or differs, preparation fails instead of resolving a new
package dynamically. The decisive proof records the actual closure before implementation.

The payload is reproducible from these immutable inputs. Image metadata/build timestamps
need not be bit-identical: the actual immutable image/rootfs identity remains part of
cache evidence. Each profile revision pins its payload, architecture, projection format,
controlled environment and preparation policy; changing those requires a new keyed
identity. Profiles do not accept arbitrary executable URLs, host paths or user packages.

Preparation may download the exact archive from the fixed official Debian pool URL.
Network retrieval is bounded to two attempts, 30 seconds per attempt and 16 MiB; verify
size and SHA256 before extraction. Hash/format mismatch is terminal, not retried as a
network fault. No mirror guessing or floating-version fallback. Extract only in the
owned Docker build context/container, with the existing 300-second build deadline and
2 GiB final-image bound. Git-profile builds allow at most two complete
attempts, with a fresh owned context after confirmed cleanup. The verified bounded archive
is retained between these attempts; retrieval remains limited to two attempts across the
whole preparation. An immutable candidate image permits at most two read-only ownership
inspections in its existing context, without rebuilding it; an integrity mismatch or
unconfirmed cleanup is terminal. There is no unbounded stage retry or broader recipe.
Commit runtime state atomically only after closure validation,
image ownership inspection and complete preparation cleanup. Retry exhaustion reports
infrastructure failure and leaves no accepted new runtime state. Existing valid state
must not be replaced by partial preparation. Existing per-project image quota and
preparation lock/journal recovery remain authoritative.

Explicit preparation also follows the shared
[known-policy upgrade path](execution-contract.md#explicit-rebuilds-after-runtime-upgrades).
At the retention limit, verified historical project-owned images can be inspected for
cleanup using their published label format; they do not need Git-profile labels that
their release never wrote. This grants no execution authority under the new policy.
The selected runtime stays protected, and unknown ownership/policy or unconfirmed
removal stops preparation without a broader recipe or blind cleanup retry.

Preparation runs `docker build` in the daemon, outside the four-dimensional case-worker
envelope. The caller must reserve build CPU/memory capacity and daemon image/build-cache
disk separately; a host coordinator process limit does not enclose Docker builders.
Preparation enforces the stated 300-second build deadline, at most two build attempts,
archive/output/image size bounds and retained-image quota. It does not claim a separate
builder CPU, memory, process or scratch cgroup limit. The existing 1 MiB captured build
output bound applies; daemon build-cache storage is not covered by the final-image quota.

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
The existing base's shell may be used internally by Git's local transport; this does not
qualify a general shell-script or Bash capability. External networking is prevented by
Docker regardless of Git configuration. SSH/HTTP transports and external helpers are
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

Bind profile name/version, normalized required capabilities, package/source provenance,
projection and closure hashes, controlled environment, platform, preparation recipe,
actual image/rootfs identity and runner implementation to runtime/inventory/result
identities. A result prepared under another profile is never eligible, including
singleton groups. Case-v1/batch-v1 grouping and staged publication remain unchanged;
Git does not create mutable sessions between workers. Profile/requirement changes key
reuse even when selected case IDs happen to match. Config bytes retain conservative
invalidation; invocation overrides have explicit normalized identity.

The existing schema2 `execution.profile` continues to mean case-v1/batch-v1. Add a
separate `runtime` object to plan/run results with exact fields `profile` (string),
`required_capabilities` (sorted unique string list) and `identity` (null before backend
validation, otherwise an opaque SHA256 of the validated immutable runtime identity).
Static plans always have null identity. Do not overload the execution profile. Runtime
preparation returns its existing image/recipe metadata plus the selected profile and
verified provenance identity. Refusals retain the schema2 envelope and typed capability
field; they do not pretend to contain a validated runtime identity.
No paths to host executables, credentials or arbitrary Git stderr become public evidence.

The foreground mechanism proof covers successful local operations, missing origins,
lock/FETCH_HEAD obstructions and explicit recovery, read-only/undeclared-input denial,
noexec scratch, blocked networking and exact-owned cleanup. It establishes mechanism,
not production cache correctness. Subsequent installed-distribution qualification must
cover default-profile preflight before tests/cache hits, archive tampering and bounded
retrieval exhaustion, closure changes, both execution modes, atomic group/cache behavior,
worker failure/cancellation/drain, supported host/worker corners and canonical package
publication. Retain failures honestly and run one guarded complete gate before landing.


## Recorded mechanism checkpoint

On 2026-10-20, `scripts/probe_git.py` passed two independent restricted workers using
the exact archive above. Both runs produced identical commit IDs and native-file hashes.
The [proof manifest](https://github.com/xu-hao/pytest-telescope/blob/main/scripts/git_profile_proof.json)
records the executable, loader and three shared-library hashes, exact base package
versions and projection aliases. No additional library package was installed.

The proof exercised owned author configuration, add/commit, branch/checkout, local
remote/set-url, push/fetch/clone, merge and ancestor checks, revision/status queries and
worktree add/remove. Four deliberate faults (missing origin, index lock, ref lock and
FETCH_HEAD directory) returned nonzero; removing each owned fault restored the operation.
Both workers denied project writes, lacked the undeclared source file, refused direct
execution of a native binary copied into scratch, and had inspected Docker network mode
`none`, loopback-only interfaces and no default route. A controlled bridge-mode worker
had a non-loopback interface and a default route, distinguishing the network control
from an unreachable test endpoint. The TEST-NET connection failure is only an ancillary
observation, not the isolation evidence.
The default base contained no Git. Exact owned containers and the proof image were
removed and absence verified. An earlier proof-launch failure from a mode 0600 control
script was retained separately; setting the control script explicitly 0444 corrected
that preparation error. None of this substitutes for production preflight, cache,
retrieval-exhaustion or installed-package qualification.


## Qualification checkpoint

The restricted mechanism proof exercised two fresh sessions with identical controlled
Git object identities, all supported local repository operations and owned-fault repair.
Network evidence includes inspected `NetworkMode=none`, loopback-only interfaces and
no default route, plus a controlled bridge-mode negative check. An unreachable external
address alone is not isolation evidence. The initial unreadable control-file preparation
failure is retained separately from the corrected proof.

Installed 0.1.0a4 candidate controls exercise case and batch execution, warm reuse without
workers or preparation, explicit no-cache policies, requested-profile/capability refusal,
metadata/source visibility, read-only identity inspection and actual sibling cancellation.
The same local operation fixture also passes with a pinned pytest 8.3.5 worker. Native
artifact controls cover malformed archives, missing projected files and missing closure
files as terminal failures; network/build outages remain bounded retries. A real
at-quota preparation failure leaves the oldest currently selected runtime usable.

The complete release verdict is recorded with the
[0.1.0a4 release](https://github.com/xu-hao/pytest-telescope/releases/tag/v0.1.0a4).
These functional controls do not claim a universal speedup or qualify remote transports,
executable scratch, host tools, browser/service dependencies or arbitrary Git hooks.
