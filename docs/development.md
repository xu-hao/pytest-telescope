# Local development

The accepted [ordinary/Bubblewrap contract](bubblewrap.md) is **UNQUALIFIED** as a complete
backend. The [API/runtime seam](bubblewrap-api.md) defines the required production work.
Only the bounded native resource mechanism passed
[run 39503556501](https://github.com/xu-hao/pytest-telescope/actions/runs/39503556501).
Runtime preparation/root closure, Git ABI, isolated children, ordinary/scoped routing,
continuous descendant supervision and final cgroup CPU accounting remain required.

Use a dedicated Python 3.11+ virtual environment and install development dependencies:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]'
```

The existing `python scripts/check.py --artifacts <new-directory>` command builds an
sdist and wheel, validates metadata, runs behavioral controls and strict documentation.
Its Docker-era native controls and gate recipe do not qualify the replacement. The
replacement gate must build/install the exact submitted candidate and execute every
required real native control without skips. Concrete preparation, administrator
preflight and complete gate commands must be verified against that production path
before this guide can claim a runnable qualification recipe.

Local unsupported namespace/controller prerequisites must fail explicitly. Run entry
preflight in the invoking admission context before any external user service. A denied
context cannot be bypassed by a more permissive service. Administrator delegation setup
is separate from normal launch; the package must never provision or stop/reconfigure the
user manager itself. See [the prerequisites](bubblewrap-api.md#bubblewrap-provenance-and-failure-ownership).

The proposed hosted executor runs inside one guarded landing row, binding exact source,
workflow/job identity, artifact hashes, enforced resources, terminal verdict and exact
owned cleanup. It has bounded retries/deadlines and cancels only its own run. A stale
head or incomplete/failed receipt cannot authorize publication. Strict documentation
validation alone is proportionate for documentation changes and proves no runtime behavior.

Behavioral tests should cover collection/import/fixture/teardown isolation, undeclared
reads from Python/native code/subprocesses, immutable inputs, network restrictions,
exact-snapshot invalidation, new test inventory, corrupt/concurrent records, deadlines,
descendants, retry exhaustion, and cleanup. Every test owns and cleans up its projects
and backend resources. Avoid tests that assert source code or documentation text instead
of observable behavior.

For documentation alone:

```sh
python -m pip install -r docs/requirements.txt
python -m sphinx -n -W --keep-going -b dirhtml docs /tmp/telescope-docs
```

The documentation uses [Sphinx](https://www.sphinx-doc.org/),
[Furo](https://pradyunsg.me/furo/), and [MyST Markdown](https://myst-parser.readthedocs.io/).
The pinned versions in `docs/requirements.txt` also appear in the development extra.
Sphinx 8.2 supports the project's Python 3.11 development baseline. `-n -W` makes
unresolved references and other warnings fail the build. The directory-HTML builder
preserves page URLs such as `/benchmark/`. Serve the generated directory to inspect it:

```sh
python -m http.server --directory /tmp/telescope-docs 8000
```

Check the quickstart, sidebar links, search, source links, and mobile navigation in a
real browser after layout changes. Stop the local server when finished. Edit navigation
in the hidden MyST toctree at the end of `docs/index.md`; keep runnable examples first.

The [benchmark](benchmark.md) retains historical Docker measurements. New fresh trials
must match selected cases, session counts, scopes and admitted resources at 1/2/4 workers
with caching disabled. Separate launch-only costs from end-to-end verification/freezing,
planning, materialization and cleanup. Record actual overlap, isolated views, outcomes,
sample order/dispersion and complete CPU accounting. Unknown ordinary CPU stays null;
RUSAGE_CHILDREN is not complete sandbox CPU. A fixture-lifetime change is not pure
sandbox overhead or a universal speedup claim.

The CI matrix must cover supported host Python/pytest versions. Replacement qualification
must additionally exercise both distro and dedicated CPython layouts on supported native
hosts, plus pinned Git and isolated-source child profiles. Keep build/documentation tools in
development dependencies and keep runtime contents explicit. Never use an editable
application install as evidence of a clean isolated runtime.

The [candidate qualification status](scoped-execution.md#candidate-qualification-status)
records focused evidence and remaining release gates separately. A corrected regression
does not turn its earlier failed full-run receipt into a passing one.
