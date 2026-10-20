# Declared source in isolated Python children

The accepted replacement retains explicit `isolated-source-v1` lookup without application
installation. **UNQUALIFIED:** the runtime/root, projection and child capability require
new production controls. Historical Docker mechanism receipts do not qualify the
replacement. This capability stays in the required delivery queue alongside Git.
See the [exact migration seam](bubblewrap-api.md#pinned-git-abi-and-isolated-children).

## Explicit selection and source scope

```toml
[tool.pytest_telescope.python]
profile = "isolated-source-v1"
source_roots = ["packages/core/src", "packages/addon/src"]
```

With this table absent, `profile = "default"` and existing behavior is unchanged.
The default profile accepts no source roots. The isolated profile also accepts an empty
list, exposing only selected static distribution metadata to Python children. Roots
are an ordered list, not a set: their order controls ordinary Python import precedence.
At most 64 unique roots are accepted, each a canonical project-relative existing
ordinary directory, including `.`. Absolute paths, empty strings, traversal, repeated
separators, control characters, symlinks and symlink ancestors refuse before backend
startup. A root with trailing Unicode whitespace or any line separator is typed
unsupported `python:source-layout`: Python path-file trimming and protocol line parsing
must never silently redirect lookup to a different directory. Internal ordinary spaces
remain supported. A root is an import lookup location, never an input declaration. Per-case
literal declarations, defaults and bundles still determine every readable source file.
A root absent from a particular frozen view adds no files or synthetic application
content; Python ignores that nonexistent search path.

An independent suite may completely replace the project Python configuration:

```toml
[tool.pytest_telescope.suites.core.python]
profile = "isolated-source-v1"
source_roots = ["packages/core/src"]

[tool.pytest_telescope.suites.addon.python]
profile = "isolated-source-v1"
source_roots = ["packages/addon/src"]
```

A suite table specifies both fields; an omitted suite table inherits the project
configuration. Never union roots between suites or between an explicit replacement and
the inherited roots. This permits same-named packages to resolve independently while
each suite still receives only its declared source files.

API `controller.run(..., python_profile=None, source_roots=None)` and
`plan.make_plan(..., python_profile=None, source_roots=None)` override these values for
one invocation across the selected suites, without rewriting configuration. Each supplied
field replaces that field after suite inheritance. An explicit roots override replaces
all selected suites' effective root lists; it does not silently enable a profile. CLI `plan` and `run` accept
`--python-profile` and repeated `--source-root`; API supports the explicit empty list.
Unknown profiles return typed unsupported `python:profile`. Unsupported source layouts
return `python:source-layout`; malformed paths/configuration are declaration errors.
All static validation occurs before collection, execution or cache reuse.

## Mechanism and limits

The pinned CPython runtime prepares an empty, runtime-owned path-file slot in its
verified system site-packages directory. An opted-in worker overlays that one slot with
an owned immutable generated `.pth` file containing only absolute `/project/...` paths
and, when selected metadata exists, `/metadata`. It contains no executable `import`
lines. Source directories remain inside the exact read-only worker snapshot; metadata
remains a separate read-only static projection. No application bytes enter the prepared runtime,
site-packages, host environment or shared global layer. No wheel/editable installation,
build hook, user-supplied path file or generated launcher is executed.

Python's [`-I` option](https://docs.python.org/3.12/using/cmdline.html#cmdoption-I)
ignores environment search paths and user site packages but retains system site
initialization. A system [path configuration file](https://docs.python.org/3.12/library/site.html)
therefore supplies this explicit lookup without dropping isolation. `-S` suppresses
site initialization, so there is no source-discovery guarantee under that standard
Python flag; its negative control must remain negative. The controller does not inspect
or intercept arbitrary child command lines. Alternate interpreters, embedded Python, virtual
environments created inside tests and executable scratch are not qualified here.

Generated files are at most 256 KiB, read-only mode 0444 and epoch timestamps. The
fixed target path, empty-slot digest, encoding/order, source-profile version and actual
projection bytes participate in identity. Mount only an owned regular file and verify
the prepared slot; missing or mismatched slots refuse boundedly rather than guessing
another interpreter directory. The generated mount has no application code and never
widens the source or metadata mounts.

A replacement prepared runtime must contain the exact verified empty slot. Missing or
corrupt preparation is never repaired implicitly by run/inspection. The
[explicit upgrade path](execution-contract.md#explicit-rebuilds-after-runtime-upgrades)
leaves old Docker artifacts untouched and requires explicit preparation. Runtime
migration to python-v2/git-local-v2 does not silently change this lookup profile.
Ordinary sessions receive no projection; they use their real host import environment.

Source roots cannot provide implicit startup hooks or additional installed metadata.
Visible root-level `sitecustomize`/`usercustomize` modules or package directories,
`.dist-info`/`.egg-info` entries and legacy `.egg-link` entries are unsupported,
including case variants. `EGG-INFO` (also case-insensitive) under a source root whose
basename ends in `.egg` is likewise unsupported; this follows actual CPython metadata
discovery rather than a case-sensitive approximation. Validate
all proposed collection and execution views before starting workers; no hook may run
before refusal. This avoids running application startup code merely by starting Python
and prevents source paths from exposing unselected distribution records. Arbitrary
`.pth` files inside source roots remain inert: the roots are lookup paths, not new site
installation directories. Qualification must prove this distinction. This profile
supports ordinary pure-Python modules/packages and namespace-package portions; it does
not qualify native extension builds or package-installation side effects.

System/runtime modules retain their ordinary earlier search precedence. Source-root
ordering is explicit and stable; do not infer roots from pytest `pythonpath`, build
backends or directory names. Pytest's own configured path manipulations remain its
normal behavior. The child's path is the explicit system projection, so the parent
and child need not have identical `sys.path` lists. Selected metadata conflicts with
runtime distributions still refuse: evaluate runtime distributions independently of
the projected metadata/source lookup paths before admitting pytest collection.

## Identity, lifecycle and result contract

Runtime preparation keys the slot recipe and implementation. Default and opted-in
invocations never share collection/result evidence. Normalized profile/root order,
actual generated path bytes, selected metadata and exact frozen source records bind
inventory, case and batch identity. A shared batch still requires exactly equal
readable source/absence/metadata/policy. A root option grants no extra grouping union.
Configuration bytes retain conservative invalidation; invocation overrides are keyed.
Unrelated narrow source/resource changes retain their existing selective reuse rules.

Plan and run results add a `python` object with one `suites` list in selected-suite
order. Each row has `suite`, `profile`, `source_roots` and `identity`. `identity` is null
in static plans and before validated projection preparation; otherwise it is an opaque
SHA256 over the versioned mechanism and that suite's effective settings. The separate
three-field `runtime` envelope and case/batch execution profile remain unchanged.
Per-view path-file bytes and selected metadata additionally bind their case/group
identities; a suite setting identity does not claim its individual views are equal.

Worker CPU/memory/process/scratch quotas, read-only source/root, noexec scratch,
network-none, nonroot execution and descendant cleanup remain unchanged. Child
processes use the same scoped cgroup limits and must finish before evidence is eligible.
Use the existing two-attempt infrastructure retry with a fresh worker and owned path
projection; no extra nested retry or import fallback. Invalid configuration/unsupported
layout is terminal. Projection write/mount/backend failures are infrastructure incidents;
cleanup failure prevents all new cache publication. Warm scoped result reuse starts no execution worker, but per-invocation runtime
verification/freezing remains required. Ordinary collection/imports remain fresh, and
missing preparation is never repaired implicitly.

## Qualification still required

The replacement must compare ordinary children with `python -I` before projection, then
prove opted-in `-I -c`/`-I -m`, version/entry-point lookup/load, source-only and metadata-only
negative controls, omitted packages, root ordering and namespace portions. Read-only
source, undeclared-source denial, ignored host PYTHONPATH/user site, inert source `.pth`,
startup-hook refusal across ALL views and ordinary `-I -S` site-disabled behavior remain
required. Runtime-only metadata collision inspection must exclude selected projections.

Production controls cover both runtime profiles and case/batch modes, source/metadata/
configuration/runtime mutation, selectors, scope separation, warm reuse, corrupt slots,
bounded retries/exhaustion, cancellation, live descendants, protocol bounds and supported
host/runtime layouts. Ordinary tests receive no isolated-source projection. Keep the
historical proof receipts as historical evidence; do not rename them replacement
qualification or claim a release before the complete guarded installed-package gate.
