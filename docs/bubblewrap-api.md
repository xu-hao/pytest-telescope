# API, CLI and configuration

This reference lists the Python API, command line, configuration tables, frozen-tool
manifest format, host requirements and capability codes. Use it when you call
pytest-telescope from code, register frozen tools, or handle a refusal. The behavior
behind these interfaces is described in [ordinary and Bubblewrap
execution](bubblewrap.md). The [result reference](result-schema.md) defines every schema
4 field, and [complete examples](bubblewrap-schema-examples.json) show whole reports.

## Python API

```python
from pytest_telescope.controller import run, error_result
from pytest_telescope.plan import make_plan

run(root, *, force=False, no_cache=False, explain=False, cache_dir=None, timeout=60,
    arguments=(), workers=None, suites=None, execution_mode=None, structural_files=None,
    fixture_guard=None, condition_checks=())
make_plan(root, *, suites=None, structural_files=None)
error_result(error, *, fixture_guard=None)
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
- `structural_files` narrows suite file membership before structural test parsing and
  pytest collection
  ([structural narrowing](scoped-execution.md#invocation-only-structural-file-narrowing)).
  Explicitly declared helper inputs remain readable and their frozen literal calls are
  authenticated without adding helper tests to the inventory.
- `force=True` executes scoped cases again even when a reusable success exists.
  `no_cache=True` bypasses result reading and recording. `explain=True` includes reuse and
  execution reasons in the result.
- `cache_dir` selects the cache directory; `None` uses `.pytest-telescope/cache` under
  `root`.
- `run` raises `DeclarationError` for invalid declarations and `UnsupportedCapability`
  (a `DeclarationError` subclass with a `capability` attribute) for refusals.
  After a session has started, these typed exceptions can retain immutable, versioned
  [refusal accounting](result-schema.md#retained-observations-after-a-late-refusal).
  Early refusals leave their explicit `accounting` property `None`.
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

Frozen Git history is not supported: there is no `git=` marker field. Selecting a Git
executable as a frozen tool for an owned scratch repository is a different, supported
use. `plan` is a static preview of implemented declarations.

## Frozen tool manifest

A *frozen tool* is an existing installation outside the project, such as a Node runtime,
a browser or an installed Python package, that scoped tests may use. Its *manifest* is a
JSON file in the project that pins the exact files of that installation: their bytes,
modes, links and runtime relations. A test selects the tool by name, and
pytest-telescope shows only those pinned files, read-only. The literal marker
`tools=["frontend"]` selects a registered frozen closure. Registration has exactly two
string fields:

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
and the [filesystem view](bubblewrap.md#frozen-tools-and-runtime-files).

### Native tools at a glance

A *native* selection is a tool whose pinned closure contains any supported ELF file (an
executable, library or extension module), or whose `loader` is not `null`. Selecting a
native closure also closes system configuration: `/etc/resolv.conf` is absent unless the
selected manifest pins it and preserves its path. Required certificate or fixture
configuration likewise appears only as declared frozen bytes, alongside the required
loader controls. A data-only tool without ELF files or a loader profile keeps the
existing system view, as do nonconsumers. See [declared native
configuration](#declared-native-system-configuration) for the exact trigger and
examples; dependencies remain the caller's responsibility. Native worker startup also
requires the [supported CPython prefix closure](#invoking-python-startup-closure). When
`/usr` is its base prefix, unselected files throughout `/usr` are absent; pin the actual
worker, stdlib, pytest dependencies and any commands the test needs. For native
libraries, the [verified ordering subset](#verified-native-library-ordering) keeps
executable process contexts and their declared native resources explicit. It supports
proven search ordering and refuses ambiguous ancestry or loaded-name conflicts; it does
not discover arbitrary program dependencies or promise ABI compatibility.

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
  using individual file mounts or the closed directory projections described below.

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

A files-mode original alias containing a symlink needs an owned directory projection
because binding the link as a file follows its target. Project the link's nearest
declared parent directory with only the explicitly pinned entries beneath it, preserving
their exact link text and frozen file/directory modes. For example, selected launcher
links may require a closed `/usr/bin` or environment `bin` view; no unselected sibling
survives in that projected directory. The complete projected contents, source-directory
mode and original/logical mappings bind identity. Conflicting selected projections
refuse; mount order cannot choose a winner. This neither mounts a whole source anchor
nor enumerates or exposes its live siblings. The same rule composes with the closed
library-search directories below, and protected mount roots remain excluded.

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

The native configuration profile applies when validation finds any supported ELF file
in the selected tool's pinned closure, or when its `loader` is nonnull. Validation
examines all pinned regular files, including libraries and Python extensions, rather
than just executable entry points. Static ELF also selects the profile; a null loader
or cache does not opt it out. A script's declared interpreter/runtime closure determines
whether it is native under the same rule. A data-only tool with no ELF files and
`loader=null` keeps the existing system view. A discovery union or execution view uses
the closed configuration profile if any of its selected tools requires it.

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
accepted only when the closure has no dynamic ELF. Every candidate for a required
SONAME must resolve to the exact declared frozen file or be absent in the closed view;
candidates outside closed directories refuse. Irrelevant cache entries need not have
files. Multiple available compatible candidates require verified loader ordering or an
ambiguity refusal, never a plausible name match. The supported proof is defined in
[verified native library ordering](#verified-native-library-ordering) below. The profile overlays
`/etc/ld.so.preload` with an owned zero-byte readonly regular file, mode `0444`.
Its exact empty contents, type, mode and destination bind identity; physical absence is
not claimed. The loader cannot read or execute the host preload contents, and explicitly
declared nonempty preload inputs are unsupported. The exact overlay destination must
be verified; unsupported path indirection refuses. Other required loader-control inputs
must be precisely controlled or explicitly pinned. The
[closed configuration view](#declared-native-system-configuration) contains only selected
frozen configuration and these loader controls. All selected loader profiles, cache
bytes/paths and colliding search entries must agree.

### Verified native library ordering

This profile proves a bounded sufficient condition for the declared
processes. It does not emulate arbitrary linker behavior or infer ABI compatibility.
`DT_NEEDED` order, `DT_SONAME`, `DT_RPATH` and `DT_RUNPATH` remain distinct pinned ELF
metadata. At most one of each of the latter three tags is supported; duplicate tags,
simultaneous RPATH and RUNPATH, malformed strings and unsupported path or dynamic-token
syntax refuse. The existing absolute-path and `$ORIGIN` syntax and native limits apply.

Each dependency first receives a provisional target without inherited search paths.
An absolute dependency names its exact pinned path. Otherwise the requesting object's
own active RPATH or RUNPATH directories are examined in their encoded order. A directory
slot is usable only when every selected descendant with that name that could represent
a hardware or platform alternative is absent or identifies the same pinned file as the
direct entry. A different alternative refuses; the runner never guesses host capabilities.
The first verified occupied slot wins. If all own slots are empty, the pinned cache and
default-directory candidates must identify one unambiguous file. Cache ordering is not
guessed. An edge requiring inherited RPATH to find any target is outside this subset.
All potential slots, including shadowed lower-priority locations, must lie in closed
declared directories; selected candidates must be readable supported regular ELF files.

Each declared native executable has a separate fresh execution context containing its
`DT_NEEDED` closure, the pinned interpreter and its closure, and its explicitly declared
native `resources` and their closures. A resource reference can name a native object or
a subtree containing native objects. In particular, the invoking Python executable must
associate retained native stdlib extensions through its existing `resources` references.
Existing Python runtime/package associations also contribute their native resources.
Resources add proof inputs; they do not add filesystem grants or silently change the
exact transitive `libraries` declaration. A script's interpreter and command executables
retain their separate execution contexts. An unattached ELF still requires valid
dependencies, but that alone does not certify loading it into an unspecified process.
Arbitrary dynamic loading and undeclared import contexts remain the caller's responsibility.

For an edge whose own search list finds a verified target, inherited RPATH cannot change
that selection. A requester's RUNPATH also excludes inherited RPATH. For other edges
that use the unique fallback target, **every active RPATH block in the same process
context must either miss or select that same target**. This includes executable and
resource blocks. Checking all blocks deliberately covers more ancestry combinations
than a particular load sequence might use. A differing inherited candidate refuses;
the runner does not choose a convenient ancestry. Cycles use finite set closure.

Within each context, needed names, reached SONAMEs and usable original/logical load-path
aliases must each identify one exact file. This prevents already-loaded name reuse from
substituting a different object. Conflicting names refuse even if local RPATH lists
would independently choose different files. Distinct executable contexts may use
different files with the same SONAME, for example Python's system OpenSSL and a Node
child's bundled OpenSSL. This does not prove symbol-version or application compatibility.

The complete proof runs for original and logical aliases: dependency targets and context
name mappings must agree, including each inherited block's owner-relative expansion.
It runs again on a selected collection union. A changed or ambiguous winner refuses;
a provably shadowed lower-priority candidate is permitted. Declarations of the same
executable token combine their native resources for this proof, even across tools;
they cannot masquerade as independent processes. Different executables mapped at the
same original path continue to conflict under the ordinary view rules.

The proof retains at most 65,536 context/object/dependency/name/alias state tuples.
Each expansion, candidate, ancestry comparison and name comparison also consumes the
existing 200,000-visit budget and observes cancellation/deadline checks. Bounds apply
before allocation or traversal; exhaustion refuses without truncating the proof.
Existing 4,096 ELF/relation, 32-link-hop, 64-loader-directory and metadata bounds remain.
Memoized outcomes must bind the full view, content, context and alias identity. Public
reports and cache metadata retain opaque identities, not raw library paths or names.

Runtime `dlopen`, data, imports and subprocess dependencies remain the caller's explicit
responsibility. Byte validation and mandatory ELF/shebang relations do not infer arbitrary
program dependencies or certify package-manager installation semantics.

### Declared native system configuration

Native-tool consumers receive only explicitly declared frozen system configuration and
required pinned or fixed loader controls. Their `/etc` is a wholly owned readonly
hierarchy built from selected entries, plus the pinned loader cache when required or
declared and the fixed empty regular `ld.so.preload` with mode `0444`. An undeclared
entry is absent, including host resolver and certificate configuration. Declared
directories retain their source mode with write bits removed; required empty synthetic
ancestors use `0555` and grant no host contents. The controller
does not inventory ambient `/etc`, preserve its live entries or consult it for a fallback.

Configuration uses the existing root and entry records, with no additional manifest
field or arbitrary destination remapping. To expose `/etc/resolv.conf`, declare its
existing regular file or its complete supported link closure, pin its bytes and modes,
and preserve the original path. A `coverage="files"` root at `/etc` can name that file
and its explicit ancestor records without selecting siblings. A narrow complete
configuration subtree can use `coverage="tree"`; a whole `/etc` tree remains unsupported.
Only selected roots with `preserve_source=true` contribute their original `/etc` paths;
other selected roots remain available at their logical tool paths.

| Configuration input | Native consumer's `/etc` view |
| --- | --- |
| No resolver declaration | `/etc/resolv.conf` is absent, even if it exists on the host. |
| Selected, pinned resolver file | `/etc/resolv.conf` contains exactly the frozen bytes, with write bits removed. |
| Selected `/etc/ssl/certs/example.pem`, with required ancestors and link targets | That certificate has its declared bytes and type; unselected sibling certificates are absent. |
| Configuration registered only for another case | Its original entries are absent from this case's closed view. |
| No dynamic loader and no cache declaration | `/etc/ld.so.cache` is absent; `/etc/ld.so.preload` is still an empty regular `0444` file. |

Every configuration link preserves its literal text and must resolve through the same
tool's combined declared frozen closure at both logical and original paths. Missing or
escaping targets refuse; a link never discovers or grants a host target. All selected
configuration mappings must agree on bytes, type, mode and link topology. Conflicting
parents, leaf types, loader-cache mappings or preload contents refuse before launch;
mount order does not resolve a conflict. Explicit preload declarations are compatible
only when they describe the same empty regular file with frozen mode `0444`.

Capture and revalidation use the same bounded no-follow reads and immutable owned copies
as other tool inputs. No live configuration parent is bound, and no source/configuration
descriptor is passed to mount setup, pytest or tool children. Controller-local capture
handles are closed through owned cleanup. The final mount plan, including environment
overlays, must preserve the declared configuration and runtime closure before any
interpreter, site or project imports. Startup cannot borrow a live file and validate it
later; there is no private configuration-identity bootstrap.

The closed-view rule, selected bytes, source modes, links, original/logical mappings and
exact loader controls bind consuming collection and execution identity. Unselected
ambient `/etc` membership and metadata are not inputs. Source mutation or owned-copy
corruption follows the existing bounded refusal/recovery rules; cleanup must complete
before pending results publish. Public evidence retains opaque identities and failure
categories, never configuration contents or raw source inventories.

Ordinary cases, scopes without a native selection and data-only tools retain the
existing system view. The native profile closes `/etc` and declared runtime search
paths; it does not discover every configuration location an arbitrary program may use.
Callers must declare other required configuration and resource dependencies explicitly.
Network policy is unchanged: a loopback-only test need not select resolver settings,
while a program needing DNS or certificate files must declare its required inputs.

### Invoking Python startup closure

A native view also closes the invoking interpreter's filesystem startup search. The
supported profile is Linux x86_64 CPython 3.11 through 3.13, without ABI flags, with
`platlibdir="lib"`, equal prefix/exec-prefix pairs and the standard
`BASE_PREFIX/lib/python3.MINOR` library layout. Other layouts refuse before launch;
ordinary and data-only views retain their existing interpreter support.

The actual invoking and base prefixes become owned sparse readonly projections of
explicitly selected preserved entries. If the base prefix is `/usr`, this closes `/usr`
itself, including its `bin` and library directories: unselected commands, packages and
resources there are absent. A concrete virtual-environment prefix is handled the same
way. This permits those exact projection destinations, not whole-prefix source capture,
implicit dependency selection or runtime construction. `/`, protected roots, project
overlaps and protocol/device/control paths cannot be startup prefixes.

The invoking and base interpreter paths must be selected executable files or declared
aliases named `python`, `python3` or `python3.MINOR` in their respective `bin`
directories. Their resolved targets must be pinned files in the base prefix's `bin`.
Pin required launcher link text and targets without inventing aliases. Selected regular
`os.py`, `site.py` and `encodings/__init__.py` under the base stdlib are mandatory
landmarks. All observed isolated search paths, site paths, purelib and platlib must be
canonical paths inside the closed prefixes. These observations check consistency;
they do not infer the files needed by pytest, installed packages or their resources.
The caller must select those actual bytes too.

Both potential `pyvenv.cfg` locations, the adjacent `bin` directory and its prefix,
are closed. An adjacent `bin/pyvenv.cfg` is unsupported. A base-only interpreter has no
selected virtual-environment configuration. A virtual environment requires its selected
regular `PREFIX/pyvenv.cfg`, at most 64 KiB of UTF-8, with `home=BASE_PREFIX/bin` and
`include-system-site-packages=true` or `false`. Only standard `home`,
`include-system-site-packages`, `version`, `executable`, `command` and `prompt` keys are
accepted, without duplicates. If present, `executable` must name a pinned alias and
target in the base `bin`. A nested base configuration is unsupported.

Startup zip libraries, `._pth`, `.pth`, `.egg-link`, `sitecustomize` and `usercustomize`
modules/packages/extensions/bytecode, and build-tree startup files are unsupported as
selected inputs. Unselected candidates are absent because the whole supported prefix
is closed, including nonexistent zip and site search locations. This closes startup
candidates before imports; no hook can add a live external path. Worker startup remains
the invoking executable with `-I -B /telescope/worker.py`.

There are at most 64 observed search paths and 64 site paths. The actual path description,
interpreter aliases, prefix destinations and startup flags bind view identity. Every
environment bind destination must lie within an owned closed projection; those live
binds are suppressed. Any uncovered bind refuses before a view is accepted or launched.
No later environment overlay may reopen a closed startup or configuration path.

Static native selection and a static invoking interpreter are different claims. A
static tool selects closed configuration, but a dynamic invoking Python still needs
its own explicit dynamic runtime and pinned cache. A successful whole pytest worker
with no dynamic cache would require a supported static invoking interpreter. Testing
only a static tool's runtime classification does not qualify Python worker startup.

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
Repeated selection of the same tool is deduplicated. Distinct tool names have distinct
`/tools/TOOL/...` mappings, so identical executable bytes in differently named tools do
not make their same-named commands interchangeable.

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

This installed-distribution profile requires regular package directories with
`__init__.py`, plus their complete dist-info trees. Single-file module distributions,
console-script launcher files outside those trees and distributions with RECORD data
outside their package/dist-info trees are unsupported. Pinning an executable separately
does not make an otherwise unsupported installed-distribution record valid. These are
profile limits, not a promise to discover arbitrary program imports or dependencies.

RECORD rows are bounded and checked against exact pinned files. Its normal hashless
self-row is allowed because the manifest independently hashes RECORD. Generated pyc is
usable only when its bytes, type, mode and source/runtime relations are pinned; missing
optional pyc cannot fall back to a live file. Unpinned executable/source/resource rows
refuse. RECORD parent-path normalization is confined to metadata parsing and must end
at explicitly selected installed-distribution roots; manifest paths still forbid `..`.
Unsupported editable/finder/namespace/import-hook layouts refuse. The package must
already be installed: no wheel installation, runtime build or `PYTHONPATH` override is
performed. See [isolated children](scoped-execution.md#frozen-installed-python-tools).

### Runtime files outside the installed-distribution profile

The existing `coverage="files"` roots can explicitly select a single-file module,
plugin, pytest's `py.py`, package files and needed dist-info bytes at their original
paths, with `preserve_source=true`. Keep `python=[]` when the actual installed layout
does not satisfy the narrower installed-distribution record contract above. Such a
manifest binds those exact files and mappings; it does not assert that their installed
RECORD layout was validated by the `python` profile. No unselected site-packages sibling
is exposed or discovered.

Select the invoking interpreter, required stdlib and pytest dependencies too. Its
executable record associates retained native extensions through `resources` and names
its exact transitive `libraries`. A suite plugin is still an explicit import name in
`plugins`; its required source and runtime files must be in the selected view. Declaring
metadata does not install it, activate plugin autoloading or supply missing source.
Missing support refuses or produces the bounded collection/import failure applicable
to that stage; it never reopens a live prefix.

### Project links and lock correspondence

Each `links` entry has exactly:

```json
{"path":"node_modules","target":"/opt/example-install","root":"install","subpath":"."}
```

`path` is a canonical project-relative symlink with no symlink ancestor; `target` is its
exact existing text. Bounded source resolution must reach `root`/`subpath`, whose root
requires `preserve_source=true`. Selecting the tool automatically includes this actual
project link at its place below `/project`, the only path that shows the project. Inputs
at or below it are allowed only with that selected binding. Relative link text is
resolved from the link's location twice: below the project's host path, where the link
is captured, and below `/project`, where the sandbox shows it. Text that does not reach
the declared `root`/`subpath` from both refuses, for example a shortest relative path to
a directory beside the checkout. Relative text with enough leading `..` steps to reach
the filesystem root from either location is accepted. A link is never rewritten or
changed into a directory to make it work.
These links grant the selected frozen data view, including for a link named with a
`.py` suffix. They do not authenticate external targets as project helper source.

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

This creates a small external
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
    "roots": [
        {
            "id": "install",
            "source": str(source),
            "coverage": "tree",
            "preserve_source": False,
            "entries": [
                {"path": ".", "kind": "directory", "mode": 0o755},
                {
                    "path": "data.txt",
                    "kind": "file",
                    "mode": 0o644,
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            ],
        }
    ],
    "executables": [],
    "python": [],
    "links": [],
    "locks": [],
    "commands": [],
    "loader": None,
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
must not contain the project directory. The project must not live inside `/usr` or
`/etc` and must not be `/`, because the sandbox shares those system directories with the
host and would show the project's live files. Nothing is mounted at the project's own
path, so every other location is accepted, including `/project` itself, a subdirectory
of `/project`, `/tmp` and `/dev/shm`. An environment directory outside the project must
not lie inside `/project`, which shows the project, or overlap `/metadata`, `/telescope`,
`/proc` or `/dev`; an environment inside the project is shown at the same relative path
below `/project`. A frozen-tool source or original-path alias may not lie inside
`/project`; it refuses with `tool:layout`.

## Capability codes

| Code | Meaning |
| --- | --- |
| `tool:frozen-v1` | Unsupported frozen-tool manifest/version. |
| `tool:layout` | Unsupported or unverifiable source, executable, loader or installed-package layout. |
| `tool:bounds` | A bounded manifest, capture, entry, byte, path or relation limit was exceeded. |
| `tool:identity` | A declared lock, runtime or content pin does not match. |
| `sandbox:bubblewrap` | No `bwrap` executable on `PATH`. |
| `sandbox:namespaces` | `bwrap` could not create its namespaces; the message includes its error. |
| `sandbox:environment` | The interpreter could not start in the view, the environment contains the project or overlaps the sandbox's own mount points, the sandbox would show the project's live files (the project is `/` or inside `/usr` or `/etc`), an environment file that names the project directory is too large to show rewritten, or an environment inside the project depends on its host location (see [an environment inside the project](bubblewrap.md#an-environment-inside-the-project)). |
| `network:none`, `scratch:noexec` | A suite requested an isolation the sandbox does not provide. |
| `pytest:distributed-scheduling` | Nested or external distributed scheduling was requested; telescope owns scoped workers. |
| `pytest:configuration` | Unsupported pytest configuration format. |
| `execution:batch-size` | A batch exceeds 4,096 selected members. |
| `metadata:pep621-static-v1` | Local distribution metadata cannot be translated from the supported static PEP 621 fields. |
| `project-metadata:runtime-conflict` | Declared local metadata conflicts with a distribution in the installed environment. |
| `project:links` | A project input contains an unsupported directory symlink. |

Frozen Git history is not supported, so there are no `git:` capability codes.

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
  "policy": "bubblewrap-view-v3", "required": bool, "identity": digest-or-null}`.
  `identity` is null until the interpreter probe and Bubblewrap preflight succeed.
  Policy `bubblewrap-view-v3` shows the frozen project view only at `/project` and shows
  environment files that name the project directory rewritten to `/project`. It replaces
  `bubblewrap-view-v2`, which also showed the view at the project's host path; results
  recorded under the earlier policy are never reused.
- Every unit and group has `route`, `"ordinary"` or `"scoped"`. `route_counts` gives the
  same seven count keys as `counts` for each route.
- Groups list `files`; an ordinary group is one suite session
  with profile `ordinary-v2`, never cache eligible.
- `collection.suites` has one row per suite and route, each with `route` and `files`.
- Timings report wall-clock durations. CPU and resource accounting belong to the caller.

## Fixture guard API

The [fixture guard](batching.md#fixture-guard) is an opt-in check for batch mode. In the
Python signature above, `fixture_guard` is exactly `None` or `"available-v1"`; the guard
requires batch mode and refuses ordinary or mixed structural routes. The CLI opts in
with `run --fixture-guard available-v1`; it accepts no condition expectation values.
Existing calls and CLI behavior with the guard omitted retain schema 4 and use cache
envelope 8. The [joint protocol
table](result-schema.md#internal-protocol-and-cache-versions) applies to both guarded
and unguarded records.

With the guard, `condition_checks` is an actual tuple or list of at most 4,096 exact
dictionaries, in output order. Each has exactly `case_id`, `name`, and `op`; `equals`
adds exactly `value`. `case_id` is a lowercase 64-character hexadecimal case ID, `name`
is a valid non-reserved declared environment name, and `op` is `absent`, `present`, or
`equals`. The `equals` value is an exact UTF-8-encodable string of at most 4,096 bytes;
the total value bytes are at most 1 MiB. Duplicate `(case_id, name, op)` rows, extra
keys, coercion and subclasses refuse as declaration errors. Conditions must be empty
without the guard. Every referenced case must identify one actually selected case and
each name must belong to that case's effective inherited environment declaration.
Distinct operations may check the same case and name, for example `present` and
`equals`; repeating the same `(case_id, name, op)` is invalid.

Evaluate conditions from the environment captured once for the invocation, before any
selected body or cache acceptance. A mismatch raises
`UnsupportedCapability("execution:condition-mismatch", ...)`. Values are transient
controller memory: do not place raw values or unsalted hashes of individual values in
requests, cache, results, logs, exceptions or representations. Group identities and cache
keys are deterministic fingerprints of the wider execution context, including captured
environment values. Someone who knows enough other key inputs may use those fingerprints
to verify guesses of low-entropy values offline; they are not password-hardening or an
authentication mechanism. A match proves only the bound condition for that
captured case and group; it does not establish a skip, pass or teardown. Inspect the
actual reports, group lifecycle and final run status too.

`run` retains its typed exceptions. Callers may use
`error_result(error, fixture_guard="available-v1")` to request a schema 5 refusal.
A typed late refusal can carry bounded version 2 accounting with the actual inventory,
guard state, and observed attempts; the converter preserves it. A refusal without
accounting reports guard state `unavailable` and does not assert that no body ran.
Guarded accounting requires explicit guard context; omitting it uses the unguarded
schema 4 converter. A mismatch between the context and retained accounting raises
`InfrastructureError`, and an invalid converter option raises `DeclarationError`.
Controlled late infrastructure failures can instead return run results with observed
attempts. The CLI supplies guard context to its existing error handlers only after
successful parsing; parser failures keep schema 4.

Guard capability refusals use `execution:fixture-profile`, `execution:fixture-version`,
`execution:fixture-metadata`, `execution:fixture-sharing`, `execution:fixture-limit`,
and `execution:condition-mismatch`. Unsupported fixtures and uncertain sharing refuse
rather than changing the view. Dependency errors and malformed worker records follow
bounded infrastructure retry and incident handling. See the
[fixture guard contract](batching.md#fixture-guard) for scope,
version and lifecycle limits.
