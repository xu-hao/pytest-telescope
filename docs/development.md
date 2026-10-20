# Local development

Use a dedicated Python 3.11+ virtual environment. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]'
python scripts/check.py --artifacts /tmp/telescope-check
```

The check command runs Ruff lint and formatting checks, builds the sdist and a wheel
from it, validates package metadata, builds documentation with warnings as errors,
installs the built wheel without dependencies, and runs behavioral tests. It requires a
virtual environment and uses its interpreter. The artifact directory must not already
exist; use a new directory for another run. Build dependencies may need network access.
No package is uploaded by this command.
Each stage has a five-minute deadline, except the complete real-Docker behavioral suite,
which has a fifteen-minute deadline. A timeout fails the gate.

Linux and a usable Docker daemon are required for real enforcement qualification. The
runtime build uses the pinned public Python base described in the
[specification](specification.md), not an existing application image. Do not treat
skipped integration tests or mocked Docker responses as enforcement evidence. Ordinary
pytest behavior, disabled-plugin behavior, malformed declarations, and static planning
can be tested without running the backend.

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

The [benchmark](benchmark.md) uses a copied synthetic project and external clocks. It
must preserve per-phase command results and distinguish host controller CPU from
container worker CPU. Benchmark output is qualification evidence, not a universal
speedup guarantee.

The CI matrix covers supported host Python/pytest versions. Backend qualification must
additionally execute on a supported Docker host. Keep build/documentation tools in
development dependencies and keep runtime contents explicit. Never use an editable
application install as evidence of a clean isolated runtime.

The [candidate qualification status](scoped-execution.md#candidate-qualification-status)
records focused evidence and remaining release gates separately. A corrected regression
does not turn its earlier failed full-run receipt into a passing one.
