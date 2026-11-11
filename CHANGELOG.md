# Changelog

## 0.1.1

- Static-parse totals now scale with the frozen project: 2 GiB of Python source (was
  64 MiB) and 268,435,456 AST nodes (was 2,000,000). The per-file limits of 4 MiB,
  200,000 nodes and depth 128 and the total of 100,000 literal marker calls are
  unchanged. Each distinct file is parsed once per invocation; a symlink alias adds only
  its own marker records. Measured with Python 3.12 at the node total, parsing ran at
  0.47 to 0.66 million nodes per second with a peak resident set of at most 159 MB, as
  documented in the execution contract's static parse budget.
- `plan` now parses and bounds declared Python sources exactly as `run` does, in the
  same static pass. A project over a parse total, or a declared source that cannot be
  parsed or exceeds a per-file size, node or depth limit, refuses with a declaration
  error that names the exceeded total or the cause and file, before any sandbox is
  prepared or any case runs. Previously `plan` accepted such a project and `run` refused
  it after preparing the sandbox. Freezing refuses a declared source that changed after
  it was parsed.
- Scoped sessions show the frozen project at one path, the project's host path. The
  working directory, pytest's `-c` and `--rootdir`, the test-file arguments, the
  worker's project root and editable installs all name the same files, so a test that
  checks that an imported package module lies below its checkout passes as it does in
  ordinary pytest. Nothing is mounted at `/project` unless the project lives there, and
  scoped tests now observe the checkout's location, for example in `Path.cwd()` and
  `__file__`. The sandbox view policy is `bubblewrap-view-v3` (was
  `bubblewrap-view-v2`), so results recorded by earlier versions are not reused.
- Identical checkouts at different paths still share results: no cache key or record
  contains the project's path, and fixture-guard identities now record project source
  files and conftest plugin names relative to the project.
- A project inside `/project`, an environment under `/project`, and relative
  project-link text that resolved differently from `/project` are no longer refused.
  Relative link text resolves from the link's location below the project's host path
  and must still reach its declared root.
- Failure evidence names a location in a project module imported through an editable
  install by its project-relative file instead of `<runtime>`.
- `scripts/check.py --functional-workers N` runs the functional stage on `N`
  pytest-xdist workers after recording the functional membership from one serial
  collection, which every worker must reproduce exactly. The default stays one serial
  process; the CI and release workflows use four workers, and pytest-xdist joins the
  `dev` extra.
- The benchmark ran on the same workstation under the same outer limits as the
  2026-10-21 campaign: 4 CPUs, 8 GiB and five interleaved repetitions, with all 50
  measurements valid (`benchmarks/results/2026-11-11-bubblewrap.json`). Every series
  was slower and none was faster. Median wall time rose from 0.854 to 0.966 s (a
  factor of 1.13) for plain pytest, by factors of 1.09 to 1.14 for the ordinary route,
  from 3.92, 2.22 and 1.36 s to 5.05, 2.90 and 1.86 s (1.29 to 1.37) for scoped cases on
  1, 2 and 4 workers, and from 1.62, 1.26 and 1.28 s to 2.05, 1.52 and 1.56 s (1.20 to
  1.26) for scoped batches. Median CPU time rose by factors of 1.09 to 1.33. Relative to
  plain pytest, scoped cases took 5.23, 3.01 and 1.92 times its wall time (was 4.59,
  2.60 and 1.59) and scoped batches 2.12, 1.57 and 1.62 times (was 1.90, 1.48 and
  1.49); the ordinary route stayed at 1.22 to 1.26 times. The two campaigns differ in
  more than this release: the earlier one measured pytest-telescope 0.1.0a4 with pytest
  8.3.5 and pluggy 1.5.0, this one 0.1.1 with pytest 9.2.0 and pluggy 1.7.0, and the
  host was running other jobs at a one-minute load average of 13.2 to 14.1 on its 24
  logical CPUs, while the earlier campaign's host load was not recorded. Plain pytest,
  which runs no pytest-telescope code, was itself 13% slower, so these results do not
  isolate the cost of this release.

## 0.1.0 (2026-11-10)

- Add per-scope environment declarations that distinguish absent, empty and valued
  inputs and affect only their consumers.
- Add explicit frozen-tool manifests, selected runtime/configuration files and closed
  native startup views without installing a tool or widening undeclared inputs.
- Authenticate declared imported helpers separately from a case's actual module,
  class/callable and selected literal-parameter ownership.
- Add the opt-in `available-v1` batch fixture guard, preserving precise scopes and
  conservatively refusing possible shared fixture or package lifetimes split across groups.
- Retain observed phases, reason digests and actual attempts after late refusals.
  Default results use schema 4/accounting 1; guarded results use schema 5/accounting 2.
  Strict worker protocols and cache envelope 8 reject incompatible older evidence.
- Bind the installed-candidate gate to exact source, wheel, interpreter and import bytes
  with schema 2. Default checks require no Node selection or program-profile preparation.
  Move five Node/program and nine installed-distribution qualification nodes intact to
  explicit compatibility benchmarks; their new compatibility execution remains unrun.
- Keep `all` as the default and sole full/release qualification profile. Explicit `core`
  compatibility checks retain ordinary behavior and unsupported-adapter refusals while
  recording the exact real fixture-guard positives excluded from that profile.
  Verify final membership after collection callbacks finish and refuse late changes
  before test bodies or a success receipt.
  Preserve the complete core partition across exclusion callbacks and reject additional
  callback deselections before bodies.
- Preserve qualification bytecode suppression through isolated pytest fixtures and
  their ordinary-session children; keep installed-package verification strict.
  Allow twenty minutes for the functional stage while retaining five-minute limits
  for other stages and the existing bounded process cleanup.

New performance is unmeasured. Frozen-checkout/history capture and its proposed
selector are deferred and are not implemented by this release.
Prior 0.1.0a6 guarantees and historical qualification remain unchanged.

## 0.1.0a6

- Moving a checkout with pytest assertion-rewrite bytecode no longer refuses valid
  literal telescope declarations. Declaration validation uses the module's current
  project location while preserving source-position, literal-argument and provenance
  checks.
