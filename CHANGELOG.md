# Changelog

## 0.1.1

Version 0.1.0 was tagged but never published to PyPI, because its release build's
functional stage exceeded its deadline. 0.1.1 is therefore the first official release on
PyPI after 0.1.0a7. It includes every change listed under 0.1.0 below.

### Summary

Three terms are used below. A *scoped* test declares the project files it needs. It runs
in a Bubblewrap sandbox, and its successful result can be reused. An *ordinary* test has
no such declaration and runs every time as plain pytest. The *frozen view* is the
read-only copy of its declared project files that a scoped test sees in its sandbox.

- Larger projects fit. The totals for statically parsed Python source rise from 64 MiB to
  2 GiB, and from 2,000,000 to 268,435,456 AST nodes.
- `plan` now refuses the same oversized or unparsable declared sources as `run`.
- A scoped test sees its project at one path, `/project`, wherever the checkout lives.
  Editable installs and an environment inside the project work there. A scoped test can no
  longer tell where its checkout lives, with the two exceptions under
  "Known limitations".
- Results cached by earlier versions are not reused, because the sandbox view policy
  changed. The first run after the upgrade executes every scoped test again.
- Most projects need no change. One kind of test does: a test that reads the source
  directories in the mount table, or the raw `.pyc` bytes of an environment inside the
  project, should stay ordinary or use `cache=False`.
- In an interleaved benchmark of fresh execution against 0.1.0, no series had a slower
  median. See "Performance".

### Static parse bounds

- Static discovery parses test files and declared Python sources without importing them.
  Its totals are now 2 GiB of Python source (was 64 MiB) and 268,435,456 AST nodes (was
  2,000,000). The source total equals the existing 2 GiB bound on a project's frozen
  bytes, and the node total allows one AST node per eight bytes of it. The per-file limits
  of 4 MiB, 200,000 nodes and depth 128 are unchanged. So is the total of 100,000 literal
  marker calls.
- Each distinct file is read and parsed once per invocation, and its bytes and nodes count
  once. A symlink alias of a file is not parsed again: its marker records are relabeled
  from that one parse. Marker calls count once per import origin, so the marker total also
  bounds the records that aliases add.
- Parsing is single-threaded. Measured with Python 3.12 at the node total, installed
  library source took 404 s at 0.66 million nodes per second, with a peak resident set of
  110 MB. Syntax at the per-file node bound took 570 s at 0.47 million nodes per second,
  with a peak of 159 MB. A project near the totals therefore spends several minutes in
  static discovery for each `plan` or `run`. Memory is bounded per file. See
  [bounds and performance](https://pytest-telescope.readthedocs.io/en/latest/execution-contract/#bounds-and-performance).

### `plan` and `run` apply the same bounds

- `plan` and `run` enforce the parse bounds in the same static pass, before any sandbox is
  prepared and before any case runs. A project over a total refuses with a declaration
  error that names the exceeded total. A test file or declared source with a syntax
  error, a per-file size, node or depth violation, or an unsupported literal marker call
  refuses with a declaration error that names the cause and the file. Previously `plan`
  accepted such a declared source and only `run` refused it.
- A run then freezes the project. Freezing refuses a declared source that changed after
  it was parsed.
- A run verifies the content of each distinct frozen declared Python file once per
  invocation. It reads and hashes the file when the first scoped view selects it. The
  file is the run's own private frozen copy, not the checkout.
- Each later view checks only that the file still has the same inode, mode, size and
  modification time, and reads each selected link's text again. A file or link that is
  changed or replaced afterwards so that any of these differs refuses. An ordinary write
  or replacement does that.
- A process of the same user that deliberately rewrites the private frozen copy during a
  run and leaves all four unchanged is not detected. Earlier versions hashed each file
  again for every view; this release trades that for speed on large declared trees.
  Measured with Python 3.12, one 4 MiB module with 800 symlink aliases took 17 ms for
  its first view and about 11 ms for each later view.

### One project path: `/project`

- The sandbox shows the frozen view at one path, `/project`. Nothing is mounted at the
  checkout's host path, and the live checkout is never mounted. The working directory,
  pytest's `-c` and `--rootdir`, the test files, `Path.cwd()`, `__file__` and tracebacks
  all name `/project`. Version 0.1.0 also showed the frozen view at the checkout's host
  path.
- Environment files that name the checkout are shown as read-only rewritten copies, in
  which the checkout's directory reads `/project`. These files are:
  - the `.pth` files of the interpreter's site directories;
  - the modules those files load at startup, such as an editable install's finder;
  - the installed metadata of a distribution installed from a location: every file of a
    `*.dist-info` directory that has a `direct_url.json`, and `.egg-link` files;
  - for an environment inside the project, its `pyvenv.cfg` and the text files of its
    scripts directory.

  The host files are never modified. The copies live in the invocation's private scratch
  and are bound over the originals for that sandbox only.
- Stale bytecode of a rewritten module is not loaded. Each cache file that exists when
  the run identifies the environment is shown as an empty file, and the rewritten source
  carries a different modification time from the original, so the interpreter compiles
  the rewritten source. One case is not covered: an unchecked hash-based cache file
  written after that point would be loaded.
- Editable-installed modules and test modules share the `/project` root. A test that
  checks that an imported package module lies below its own project root passes, as it
  does in ordinary pytest. A child started with `python -I` imports declared source
  through the rewritten install.
- A scoped test cannot observe where its checkout lives, apart from the exceptions under
  "Known limitations". The sandbox environment is cleared: `PWD` is the working directory
  below `/project`, and `OLDPWD`, `VIRTUAL_ENV` and `PYTHONPATH` are unset unless
  declared. `/proc/self/mounts`, and the mount points and options in
  `/proc/self/mountinfo`, name only sandbox paths. See
  [a scoped test cannot tell where its checkout lives](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap/#a-scoped-test-cannot-tell-where-its-checkout-lives).
- Identical checkouts at different paths share results. No cache key or record contains
  the project's path, and fixture-guard identities name project files by their `/project`
  paths.
- An environment inside the project, such as `.venv`, runs at the same relative path
  below `/project`, for example `/project/.venv`. The session starts
  `/project/.venv/bin/python`, and `sys.prefix`, `sys.executable`, `sys.path` and the
  first `PATH` entry name that path. A console script whose first line names the
  environment's interpreter starts it there. Version 0.1.0 kept such an environment at
  its host path. See
  [an environment inside the project](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap/#an-environment-inside-the-project).
- The sandbox view policy is `bubblewrap-view-v3`. Version 0.1.0 used
  `bubblewrap-view-v2`. The policy is part of every scoped cache key, so results recorded
  by earlier versions are not reused.
- Refusals removed: the project may now live anywhere outside the system directories that
  the sandbox shares with the host. Version 0.1.0 refused a project that was exactly
  `/tmp` or `/dev/shm`, a subdirectory of `/project`, and a project inside `/metadata`,
  `/telescope`, `/proc` or `/dev`. A project below `/tmp` or `/dev/shm`, such as
  `/tmp/app`, was already accepted, and so was a project at `/project` itself.
- Refusals added: an environment file that names the project directory and is too large
  to show rewritten refuses with `sandbox:environment`. The rewritten copies of one
  environment may total at most 1 MiB. A frozen-tool source or original-path alias inside
  `/project` refuses with `tool:layout`.
- Refusals kept: a project that is `/` or lies inside `/usr` or `/etc` still refuses with
  `sandbox:environment`, because the sandbox would show its live files. So does an
  environment that contains the project, and an environment directory outside the project
  that overlaps `/project`, `/metadata`, `/telescope`, `/proc` or `/dev`, or contains
  `/tmp`. Relative link text of a frozen tool's project link must still reach its declared
  target both from the checkout and from `/project`.
- pytest-telescope identifies an environment by starting its interpreter once on the
  host. That start is isolated and never writes bytecode, so identifying an environment
  never writes into it, whatever the calling job's own bytecode setting.
- Failure evidence names a frame in a project module by its project-relative file however
  the module was imported, including through an editable install. Such a frame was
  labeled `<runtime>` before.

### Known limitations

Two things a scoped test can read still name its checkout's location. See
[what a scoped test can still see](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap/#what-a-scoped-test-can-still-see).

- A test that reads the source directories in the mount table (field 4 of
  `/proc/*/mountinfo`) can observe its checkout's location, which its cache key does not
  contain. Leave such a test ordinary, or add `cache=False` to its scope.

  Why: the frozen view lives below the checkout's `.pytest-telescope/scratch` directory,
  so the source directory of each bind mount names the checkout. The mount points
  (field 5), the mount options and every line of `/proc/self/mounts` name only sandbox
  paths.
- A test that reads the raw bytes of cached bytecode files (`__pycache__/*.pyc`) of an
  environment inside the project can observe its checkout's location, which its cache key
  does not contain. Leave such a test ordinary, or add `cache=False` to its scope.

  Why: each cache file records the path its source had when it was compiled, which lies
  below the checkout. A test that scans the environment's files for absolute paths, or
  reads a cache file's bytes itself instead of importing the module, sees that path.
  Imported modules, `__file__`, `co_filename` and tracebacks report sandbox paths. An
  environment outside the project is not affected. See
  [an environment inside the project](https://pytest-telescope.readthedocs.io/en/latest/bubblewrap/#an-environment-inside-the-project).

In either case a result cached in one checkout could be reused in another checkout, where
the test would have seen a different value.

### Performance

Both benchmarks below ran on one workstation under the same outer limits as the
2026-10-21 campaign: 4 CPUs, 8 GiB and five interleaved repetitions. Every measurement
runs the 16-case workload fresh, so the numbers show execution overhead, not the benefit
of reuse. The campaign measured 0.1.1 with pytest 9.2.0 and pluggy 1.7.0. All 50
measurements were valid (`benchmarks/results/2026-11-11-bubblewrap.json`). Against
2026-10-21, plain pytest was slower: its median wall time rose from 0.854 to 0.889 s, a
factor of 1.04. Every pytest-telescope series was faster. By median wall time, the
ordinary route fell from 1.07, 1.07 and 1.08 s to 0.98 s (factors of 0.90 to 0.92).
Scoped cases on 1, 2 and 4 workers fell from 3.92, 2.22 and 1.36 s to 3.80, 2.16 and
1.32 s (0.97). Scoped batches fell from 1.62, 1.26 and 1.28 s to 1.59, 1.23 and 1.23 s
(0.96 to 0.98). Median CPU time changed by similar factors: 1.04 for plain pytest and
0.90 to 0.98 for the others. Relative to plain pytest, scoped cases took 4.27, 2.43 and
1.49 times its wall time (was 4.59, 2.60 and 1.59). Scoped batches took 1.79, 1.39 and
1.38 times (was 1.90, 1.48 and 1.49). The ordinary route took 1.10 to 1.11 times (was
1.25 to 1.26). The two campaigns differ in more than this release: the earlier one
measured 0.1.0a4 with pytest 8.3.5 and pluggy 1.5.0.

A second, interleaved benchmark therefore isolates this release. It measured 0.1.0 and
0.1.1 on pytest 9.2.0 with otherwise identical packages. The two versions alternated
measurement by measurement, for seven configurations. All 70 measurements were valid.
Its record is not checked in. No series had a slower median in 0.1.1. By median wall
time, scoped cases fell from 4.43 to 3.86 s on 1 worker (0.87) and from 1.70 to 1.38 s
on 4 workers (0.81). Scoped batches on 1 and 4 workers fell from 1.92 to 1.74 s (0.91)
and from 1.45 to 1.37 s (0.95). The ordinary route fell from 1.10 to 1.05 s (0.95) and
from 1.08 to 1.02 s (0.94). Median CPU time fell by factors of 0.77 to 0.95. This
comparison was noisier than the campaign. Plain pytest, which runs no pytest-telescope
code, measured 0.923 and 0.901 s (0.98) beside the two versions. The sample ranges of
the two versions overlapped in every series. Of the 30 paired pytest-telescope
measurements, 0.1.1 was faster in 28 and slower in two. Read the smaller differences,
for the ordinary route and scoped batches, with that noise in mind.

### Qualification

- `scripts/check.py --functional-workers N` runs the functional stage on `N` pytest-xdist
  worker processes. The default stays one serial process. With more than one worker, the
  gate first records the functional membership from one serial collection. Every worker
  must reproduce that one membership record exactly; otherwise the gate refuses before it
  schedules any test. A crashed worker is not replaced.
- The `dev` extra pins the worker runner exactly: `pytest-xdist==3.8.0` with its transport
  `execnet==2.1.2`.
- The CI and release workflows use four workers. In local runs limited to 4 CPUs, the
  serial functional stage took 13 to 16 minutes, close to its twenty-minute deadline, and
  four workers took 4 to 5 minutes.
- The CI and release workflows try the Bubblewrap installation at most 3 times, 30
  seconds apart, and then fail the job with an error.

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
