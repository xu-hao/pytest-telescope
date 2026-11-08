# API, CLI and configuration

> Schema 4, per-scope environment inputs, frozen Git and frozen tools are unreleased
> development interfaces. Published version 0.1.0a6 does not provide them.

This reference lists the interfaces of the [ordinary and Bubblewrap execution
contract](bubblewrap.md). The [result reference](result-schema.md) defines every schema 4
field, and [complete examples](bubblewrap-schema-examples.json) show whole reports.

## Python API

```python
from pytest_telescope.controller import run, error_result
from pytest_telescope.plan import make_plan

run(root, *, force=False, no_cache=False, explain=False, cache_dir=None, timeout=60,
    arguments=(), workers=None, suites=None, execution_mode=None, structural_files=None)
make_plan(root, *, suites=None, structural_files=None)
```

- `workers` is the number of concurrent scoped sandboxes, an integer from 1 to 32. `None`
  uses `[tool.pytest_telescope.execution] workers`, default 1.
- `execution_mode` is `"case"` or `"batch"`; `None` uses the configured mode, default
  `"case"`.
- `timeout` bounds each sandboxed session (scoped collection, case or batch), from 0.1 to
  3,600 seconds. Ordinary suite sessions have no pytest-telescope deadline; the job that
  runs pytest bounds them.
- `arguments` forwards supported pytest options and selectors to the suite sessions.
  `suites` selects configured suite names; `None` selects all suites.
- `structural_files` narrows suite file membership before parsing and importing modules
  ([structural narrowing](scoped-execution.md#invocation-only-structural-file-narrowing)).
- `force=True` executes scoped cases again even when a reusable success exists.
  `no_cache=True` bypasses result reading and recording. `explain=True` includes reuse and
  execution reasons in the result.
- `cache_dir` selects the cache directory; `None` uses `.pytest-telescope/cache` under
  `root`.
- `run` raises `DeclarationError` for invalid declarations and `UnsupportedCapability`
  (a `DeclarationError` subclass with a `capability` attribute) for refusals.
  `error_result(error)` turns either, or an `InfrastructureError`, into the refusal
  envelope.

## Command line

```sh
python -m pytest_telescope plan
python -m pytest_telescope run [--workers N] [--execution-mode case|batch] [--suite NAME]...
    [--force] [--no-cache] [--explain] [--cache-dir PATH] [--timeout SECONDS] [-- PYTEST-ARGS]
pytest --telescope [PYTEST-ARGS]
pytest --telescope-plan
```

Exit statuses: 0 passed, 1 test failure, 3 infrastructure failure,
4 declaration error or unsupported capability,
5 no selected cases.

## Configuration

```toml
[tool.pytest_telescope.execution]
mode = "case"   # or "batch"
workers = 4     # concurrent scoped sandboxes, 1-32, default 1
```

The [marker and project reference](specification.md#project-configuration) defines
`test_roots`, `env`, `defaults` and `bundles`. The
[metadata and suite reference](scoped-execution.md) defines `distributions` and `suites`.
A suite's `capabilities` list accepts `python` and `project:readonly`;
`network:none` and `scratch:noexec` refuse with that capability code.

The development marker `git="frozen-checkout-v1"` requests the
[frozen Git view](bubblewrap.md#frozen-git-checkout-development-contract); this is not a
claim of support in the published 0.1.0a6 package. `plan` remains a static preview.

## Frozen tool manifest (development contract)

The literal marker `tools=["frontend"]` selects a registered frozen closure. Registration
has exactly two string fields:

```toml
[tool.pytest_telescope.tools.frontend]
format = "frozen-tool-v1"
manifest = "tool-locks/frontend.json"
```

Tool and root names are case-sensitive ASCII names matching
`[A-Za-z][A-Za-z0-9_-]{0,127}`. The manifest path is a canonical project-relative regular
file, without symlink ancestors or excluded metadata, cache or environment components.
Only structurally possible consumers require its closure. `plan` shows selected names,
project-relative manifest paths and formats; it does not capture external files, execute
tools or expose host source paths. Its `tools` object maps potentially selected names to
exactly `{"format":"frozen-tool-v1","manifest":"tool-locks/frontend.json"}` records.
Each `declarations` row adds `tools`, the sorted names contributed by that declaration
and its referenced bundles; this is a static preview, not collected-case enumeration.
See [composition and reuse](execution-contract.md#frozen-tool-capture-and-reuse)
and the [filesystem view](bubblewrap.md#frozen-tools-and-runtime-files-development-contract).

### Exact root and entry records

Every manifest has exactly these keys; empty lists and `null` are explicit:

```json
{"schema":1,"roots":[],"executables":[],"python":[],"links":[],"locks":[],"commands":[],"loader":null}
```

JSON must be valid UTF-8, with unique object keys, exact field types, no unknown fields,
floats or nonfinite numbers, and no booleans where integers are required. `schema` is
the integer `1`; unsupported versions refuse with `tool:frozen-v1`.

Each root has exactly `id`, `source`, `coverage`, `preserve_source` and `entries`:

```json
{"id":"install","source":"/opt/example-install","coverage":"tree","preserve_source":true,"entries":[]}
```

`source` is a canonical absolute regular-file or directory path with verified ancestors.
The example's empty inventory is only a shape illustration: a real root must record
itself and all required entries. Root IDs are unique within the tool.

- `coverage="tree"` records the complete tree, including hidden application files and
  links. Package, dependency and browser application/resource directories use this form.
- `coverage="files"` names an explicit set of files, links and their ancestor-directory
  records. Its source directory is a descriptor anchor: no recursive sibling discovery
  or live directory mount occurs. A broad system directory may be an anchor solely for
  these narrow entries. The logical directory contains only the declared entries.
- `preserve_source` is a boolean. Every root appears at `/tools/TOOL/ROOT`; `true` also
  preserves its verified original paths. Files-mode aliases expose only pinned entries,
  except where the closed library-directory rules below require a synthesized directory.

Whole filesystem, home, environment prefix, site-packages, live checkout, repository
metadata, credential and control trees are unsupported sources. A narrow installed
package, library or resource tree is supported. A files-mode anchor may contain a
separately declared tree subtree only when all overlapping entries and aliases agree
exactly. Other redundant/conflicting overlaps refuse; mount order never chooses a winner.
This restriction concerns complete source trees: a files-mode environment-prefix
anchor may pin the invoking interpreter's exact launcher links and `pyvenv.cfg`, without
enumerating or exposing unrelated installed packages. Explicit stdlib runtime files may
share a system files-mode anchor with their pinned libraries, preserving existing
relative paths without selecting the surrounding system tree.

Entry shapes are exact tagged objects:

| `kind` | Required fields |
| --- | --- |
| `file` | `path`, `kind`, integer `mode`, integer `size`, lowercase SHA-256 `sha256` |
| `directory` | `path`, `kind`, integer `mode` |
| `symlink` | `path`, `kind`, literal string `target` |

`path="."` identifies the root. Other paths are canonical root-relative slash paths:
no empty, dot or parent components, backslashes, controls or glob syntax. Every parent
directory has an explicit directory record. Entry paths are unique and normalize
lexically; list ordering does not change their identity. File sizes are nonnegative.
Modes range from `0` through `511` and bind the source's rwx bits; setuid, setgid,
sticky modes, devices, sockets and FIFOs refuse. View modes are `source_mode & 0o555`.
File bytes, read/execute bits and link text remain significant.

Links retain their type and exact text. Relative and absolute text are allowed only
when bounded resolution through the combined declared view terminates in pinned entries
of that same tool. The target may belong to another explicitly declared root of the
tool; every traversed link, type, content and original/logical mapping remains bound.
Declaring a link never selects its target automatically. Logical and original aliases
must resolve to the same pinned target, without invented bridge paths or rewritten links.
For example, an absolute link in a selected font-configuration tree may name a file
explicitly pinned by a separate system files-mode root with its original path preserved.
Cycles, dangling targets,
escapes and links into undeclared baseline system/environment contents refuse. Declared
root symlinks must instead be represented through canonical target roots and pinned link
entries. When a selected tool needs one of the fixed baseline prefixes `/bin`, `/sbin`,
`/lib`, `/lib32`, `/lib64` or `/libx32`, its existing symlink becomes a capture-derived
mapping record outside the manifest root entries. Its exact type, link text and resolved
mapping bind that tool's identity and are revalidated before hits and execution. The
target must terminate in the tool's declared pinned original mapping. This does not
introduce an arbitrary alias field, a manifest root at `/`, or a live parent mount.
Narrow original paths below `/tmp` or `/dev/shm` may be preserved, with empty owned
ancestors; the writable mount roots themselves cannot be replaced. Aliases cannot
overlap project or controller paths, `/tools`, `/proc` or protected device namespaces.

A root's tree digest is SHA-256 of its lexically sorted normalized entry list encoded
as UTF-8 JSON with sorted object keys, `ensure_ascii=True` and separators `(',', ':')`.
Coverage, source mapping, alias resolutions and `preserve_source` bind closure identity
separately. Inode/device and change timestamps detect mutation but are not portable
content identity. Original-path aliases do not promise relocatable cache identities.

### Executables, commands and native runtime

A reference is exactly `{"root":"install","path":"bin/tool"}`. It must resolve to
a pinned entry in the same tool; there are no cross-tool references or borrowed
dependencies. File references end at regular files; resource references may end at
directories. Duplicate references in a relation list refuse.

An executable record has exactly these fields:

```json
{"file":{"root":"install","path":"bin/tool"},"format":"elf","interpreter":null,"libraries":[],"resources":[],"commands":[]}
```

`format` is `elf`, `script` or `env`. The file has execute permission. `libraries`,
`resources` and `commands` are reference lists defining the explicit runtime closure;
every subprocess command has its own executable record. Graph traversal is bounded.
ELF validation accepts the supported Linux x86_64 64-bit little-endian layout, verifies
the actual interpreter and transitive `DT_NEEDED` relations, and never invokes `ldd`,
a package manager or another host helper to infer dependencies. Static ELF has a null
interpreter and no libraries. A dynamic ELF interpreter reference must preserve the
exact `PT_INTERP` path and actual loader bytes.

The manifest's `loader` is `null` when there is no native loader, or exactly:

```json
{"profile":"linux-x86_64-glibc-v1","directories":["/usr/lib","/usr/local/lib"],"cache":{"root":"loader_control","path":"ld.so.cache"}}
```

`directories` is a sorted unique list of at most 64 canonical absolute search-directory
subtrees. The list must cover every normalized default search root of the supported
loader, every selected ELF RPATH/RUNPATH location, and every cache candidate directory
for a required SONAME. Nested roots normalize to minimal ancestors without widening into
nonlibrary system roots. Literal absolute search paths and `$ORIGIN` relative to the
executable/library's exact path are supported. Lookup, including `$ORIGIN`, must resolve
to the same pinned bytes from both its logical and original paths, because command-map
symlinks execute logical targets. An incompatible split-root layout refuses; it is not
repaired by rewriting the executable or its search paths. Other variables, audit/preload
modes and unsupported ABI/search syntax refuse with `tool:layout`.

Each listed directory is replaced read-only with an owned tree synthesized solely from
declared frozen entries and tree subdirectories. Undeclared host siblings, including
hardware-capability alternatives, are absent. `/usr/lib` and `/usr/local/lib` are allowed
as these closed sparse views, never as live host mounts or whole source snapshots.
The closure must also contain the invoking Python worker's required libraries, native
stdlib extensions and resources. A missing worker dependency refuses before project
imports; it does not borrow a collection sibling's runtime.

Dynamic ELF requires a nonnull reference to actual pinned `/etc/ld.so.cache` bytes in the
supported bounded glibc cache format. The cache is not regenerated. A null cache is
accepted only with static ELF and no dynamic loader. Every candidate for a required
SONAME must resolve to the exact declared frozen file or be absent in the closed view;
candidates outside closed directories refuse. Irrelevant cache entries need not have
files. Multiple available compatible candidates require verified loader ordering or an
ambiguity refusal, never a plausible name match. `/etc/ld.so.preload` and other loader
control inputs are precisely masked absent or explicitly pinned; the rest of `/etc`,
including the baseline DNS view, is not replaced. All selected loader profiles, cache
bytes/paths and colliding search entries must agree.

Runtime `dlopen`, data, imports and subprocess dependencies remain the caller's explicit
responsibility. Byte validation and mandatory ELF/shebang relations do not infer arbitrary
program dependencies or certify package-manager installation semantics.

### Scripts and pinned command lookup

A script has `libraries=[]` and a pinned interpreter executable relation. Supported
shebangs name either one absolute pinned interpreter with no extra argument, or one
absolute pinned `env` executable followed by one command token. The `env` executable
uses format `env`, with the same ELF/runtime validation as `elf`; its role is explicitly
declared, not inferred from a basename. Assignments, options, `-S` and further shebang
arguments are unsupported.

For example, unchanged `#!/usr/bin/env node` requires pinned `/usr/bin/env`, pinned Node,
their runtime closures, and a top-level command record:

```json
{"name":"node","file":{"root":"node","path":"."}}
```

The command name matches `[A-Za-z0-9][A-Za-z0-9._+-]{0,127}` and is unique per tool.
Its file has an executable record and also appears in the script's `commands` relation.
There are at most 256 command names per tool and 1,024 per selected scope.

The controller makes readonly symlinks from these names to frozen logical executables
in `/tools/.commands/SCOPE_DIGEST`, placed first in the controlled `PATH`, followed by
the baseline controlled path. The kernel executes the original script, actual env and
actual interpreter; no wrapper rewrites arguments, exit status, signals or child
transport. Missing required mappings refuse before execution, with no fallback to a
mutable search candidate. Undeclared commands have no frozen-tool compatibility promise.
Nonconsumers retain their baseline path.

Two selected tools may share a command name only when the resolved bytes, type, source
mode, link topology, logical/original mapping, executable/runtime graph, loader profile,
cache and search mapping are identical. A matching name or executable hash alone is
insufficient. Conflicts refuse even in a collection union.

A selected tool refuses effective declared environment names that conflict with its
controlled `PATH`, `HOME`, `TMPDIR`, loader `LD_*` or relevant Python runtime controls.
Declaring such a name is a conflict even when its captured value is empty or absent;
the runner never silently overwrites the declaration. Existing reserved Python/pytest
and `TELESCOPE_*` names remain reserved. Omitting tools preserves the
[existing environment declaration rules](specification.md#project-configuration).

### Installed Python distribution records

Each `python` record has exactly:

```json
{"name":"example-plugin","version":"1.0.0","metadata":{"root":"metadata","path":"."},"packages":[{"root":"package","path":"."}],"dependencies":[],"runtime":[]}
```

Names use normalized distribution names and versions use packaging versions. `metadata`
names a complete installed dist-info directory; `packages` names complete package and
resource trees. Preserve their original import paths and actual `METADATA`, `RECORD`,
entry-point and resource bytes. `dependencies` names other Python records in the same
tool and must satisfy installed `Requires-Dist` constraints under supported marker
evaluation. `runtime` references the actual invoking interpreter, stdlib and required
native/dependency files; claimed runtime files are byte-bound. A narrow stdlib tree is
allowed, but a tree containing unrelated site-packages/dist-packages cannot be a shortcut
for freezing a whole environment; use explicit files-mode entries there.

RECORD rows are bounded and checked against exact pinned files. Its normal hashless
self-row is allowed because the manifest independently hashes RECORD. Generated pyc is
usable only when its bytes, type, mode and source/runtime relations are pinned; missing
optional pyc cannot fall back to a live file. Unpinned executable/source/resource rows
refuse. RECORD parent-path normalization is confined to metadata parsing and must end
at explicitly selected installed-distribution roots; manifest paths still forbid `..`.
Unsupported editable/finder/namespace/import-hook layouts refuse. The package must
already be installed: no wheel installation, runtime build or `PYTHONPATH` override is
performed. See [isolated children](scoped-execution.md#frozen-installed-python-tools-development-contract).

### Project links and lock correspondence

Each `links` entry has exactly:

```json
{"path":"node_modules","target":"/opt/example-install","root":"install","subpath":"."}
```

`path` is a canonical project-relative symlink with no symlink ancestor; `target` is its
exact existing text. Bounded source resolution must reach `root`/`subpath`, whose root
requires `preserve_source=true`. Selecting the tool automatically includes this actual
project link at both project aliases. Inputs at or below it are allowed only with that
selected binding. Relative text that resolves differently at `/project` and the mirrored
project path refuses; a link is never changed into a directory to make it work.

Each `locks` entry has exactly:

```json
{"path":"package-lock.json","sha256":"<lock-byte-digest>","roots":["install"],"tree_sha256":"<installed-tree-digest>"}
```

The angle-bracket values above denote required lowercase 64-hex digests. The lock is a
canonical regular project file and becomes a consumer input. `roots` is a nonempty,
sorted unique list of **tree-mode** roots. `tree_sha256` hashes canonical JSON of sorted
`[root_id, root_tree_digest]` pairs using the encoding above. Both lock bytes and the
complete installed tree must match. This pins a declared artifact to declared lock
bytes; it does not run or certify npm, pip or another installer. Missing or changed
pins refuse until a matching declaration is supplied; a valid new declaration changes
only consuming tool identities, subject to normal project-configuration invalidation.

### A complete data-only example

With a candidate implementing this development contract, this creates a small external
data tree and its complete manifest. Run it once from your project's root; an existing
example installation causes an error rather than being overwritten:

```python
import hashlib
import json
from pathlib import Path

project = Path.cwd().resolve()
source = project.parent / "telescope-example-install"
source.mkdir(mode=0o755)
source.chmod(0o755)
payload = b"frozen example\n"
(source / "data.txt").write_bytes(payload)
(source / "data.txt").chmod(0o644)
manifest = {
    "schema": 1,
    "roots": [{
        "id": "install", "source": str(source), "coverage": "tree",
        "preserve_source": False,
        "entries": [
            {"path": ".", "kind": "directory", "mode": 0o755},
            {"path": "data.txt", "kind": "file", "mode": 0o644,
             "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()},
        ],
    }],
    "executables": [], "python": [], "links": [], "locks": [],
    "commands": [], "loader": None,
}
(project / "tool-locks").mkdir(exist_ok=True)
(project / "tool-locks/example.json").write_text(json.dumps(manifest), encoding="utf-8")
```

Add `[tool.pytest_telescope.tools.example]` with `format="frozen-tool-v1"` and
`manifest="tool-locks/example.json"` to the existing project configuration. Then use this
test, keeping the external tree while its manifest is in use:

```python
from pathlib import Path
import pytest

@pytest.mark.telescope(tools=["example"])
def test_frozen_data():
    assert Path("/tools/example/install/data.txt").read_text() == "frozen example\n"
```

This example declares data only. Native, browser and installed-Python tools additionally
need the executable, loader and runtime relations above; they are not inferred from it.

## Host requirements

Ordinary-only projects need Linux and Python 3.11 or newer. Projects with scoped cases
also need the `bwrap` executable from Bubblewrap on `PATH`, and a kernel and security
policy that let it create user, mount and PID namespaces without privileges. Some
distributions restrict unprivileged user namespaces; for example, Ubuntu 24.04 sets
`kernel.apparmor_restrict_unprivileged_userns=1`, and `bwrap` then works only where the
administrator's AppArmor policy grants it user namespaces. pytest-telescope never changes
that policy. Check a host with `pytest-telescope run` on one scoped test: a
`sandbox:namespaces` refusal reports Bubblewrap's own error. The interpreter environment
must not contain the project directory, and the project must not live inside `/usr` or
`/etc`. Because the sandbox also shows the project at its own host path, the project
directory must not be `/`, `/tmp` or `/dev/shm`, and must not lie inside `/metadata`,
`/telescope`, `/proc`, `/dev` (other than `/dev/shm`) or a subdirectory of `/project`.
The project may be `/project` itself.

## Capability codes

| Code | Meaning |
| --- | --- |
| `tool:frozen-v1` | Unsupported frozen-tool manifest/version. |
| `tool:layout` | Unsupported or unverifiable source, executable, loader or installed-package layout. |
| `tool:bounds` | A bounded manifest, capture, entry, byte, path or relation limit was exceeded. |
| `tool:identity` | A declared lock, runtime or content pin does not match. |
| `git:frozen-checkout-v1` | Requested repository state, layout, ref, link or object format is unsupported or absent. |
| `git:executable` | System Git or its supported runtime closure is unavailable or unsupported. |
| `git:limits` | A bounded Git capture, path, ref, object or runtime limit was exceeded. |
| `sandbox:bubblewrap` | No `bwrap` executable on `PATH`. |
| `sandbox:namespaces` | `bwrap` could not create its namespaces; the message includes its error. |
| `sandbox:environment` | The interpreter could not start in the view, the environment layout overlaps the project or the sandbox's own mount points, or the project directory cannot also be shown at its host path. |
| `network:none`, `scratch:noexec` | A suite requested an isolation the sandbox does not provide. |
| `pytest:distributed-scheduling` | Nested or external distributed scheduling was requested; telescope owns scoped workers. |
| `pytest:configuration` | Unsupported pytest configuration format. |
| `execution:batch-size` | A batch exceeds 4,096 selected members. |
| `metadata:pep621-static-v1` | Local distribution metadata cannot be translated from the supported static PEP 621 fields. |
| `project-metadata:runtime-conflict` | Declared local metadata conflicts with a distribution in the installed environment. |
| `project:links` | A project input contains an unsupported directory symlink. |

Malformed Git declarations or conflicting forwarded Git, SSH, loader or locale controls raise `DeclarationError`.
Changed previously observed bytes, corrupt or incomplete objects, command, deadline,
read and materialization failures yield bounded `InfrastructureError` evidence after
at most two fresh capture attempts; cleanup failure is terminal.

For frozen tools, missing declared inputs and malformed declarations raise
`DeclarationError`; observed source mutation, I/O outage or corrupt owned capture is
an `InfrastructureError`. Capture gets at most two fresh attempts under the
[tool bounds](execution-contract.md#frozen-tool-capture-and-reuse). Cleanup uncertainty
is terminal and prevents all new record publication. Diagnostics contain categories
and tool names, never raw external paths, file bytes or environment values.

## Result schema 4

Run and refusal results use schema 4. The [result reference](result-schema.md) defines
all fields and accounting rules.

- `execution` reports the execution mode, profile and configured `workers`.
- `sandbox` reports `{"backend": "bubblewrap",
  "policy": "bubblewrap-view-v2", "required": bool, "identity": digest-or-null}`.
  `identity` is null until the interpreter probe and Bubblewrap preflight succeed.
- Every unit and group has `route`, `"ordinary"` or `"scoped"`. `route_counts` gives the
  same seven count keys as `counts` for each route.
- Groups list `files`; an ordinary group is one suite session
  with profile `ordinary-v2`, never cache eligible.
- `collection.suites` has one row per suite and route, each with `route` and `files`.
- Timings report wall-clock durations. CPU and resource accounting belong to the caller.
