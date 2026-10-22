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
installs the built wheel without dependencies, and runs the behavioral tests. It requires
a virtual environment and uses its interpreter. The artifact directory must not already
exist; use a new directory for another run. Build dependencies may need network access.
No package is uploaded by this command. Each stage has a five-minute deadline, except the
behavioral suite, which has fifteen minutes. A timeout fails the gate.

The behavioral suite needs Linux and a `bwrap` executable that can create user, mount and
PID namespaces. Tests that launch Bubblewrap are required controls: they are never
skipped or marked as expected failures. On a host whose security policy denies those
namespaces, they fail with the `sandbox:namespaces` refusal, and the gate fails. Do not
weaken the host policy to work around that; run the gate on a host that permits
Bubblewrap. Ordinary routing, declarations, planning, the interpreter probe and refusal
classification are tested without launching Bubblewrap and pass on any Linux host.

Behavioral tests cover what a scoped test can observe: declared files present at
`/project` and at the project's host path, undeclared project files absent at both, the
host interpreter and installed packages usable but read-only, isolated child interpreters
importing declared source through editable installs (including an environment inside the
project, without modifying the frozen copy), writes outside private scratch refused,
fresh scratch per sandbox, and descendants. They also cover ordinary routing staying fresh
and unsandboxed, case and batch modes, parallel workers, exact cache invalidation
(declared inputs, interpreter, installed distributions and the environment's references
into the project), reuse across identical checkouts, new test inventory,
corrupt/concurrent records, deadlines, retry exhaustion and cleanup. Every test owns and cleans up its projects. A test that
simulates a host refusal with a stand-in `bwrap` says so in its name. Avoid tests that
assert source code or documentation text instead of observable behavior.

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

The CI matrix covers supported host Python/pytest versions on Linux runners with
Bubblewrap installed. Keep build/documentation tools in development dependencies. The
[benchmark](benchmark.md) defines the plain-versus-Bubblewrap measurement; its output is
qualification evidence, not a universal speedup guarantee.

## Qualification status

The Bubblewrap replacement has not yet passed a complete gate on a host that permits
Bubblewrap namespaces. Results of the 0.1.0a3 Docker gate do not
transfer to it. A corrected regression does not turn its earlier failed full-run receipt
into a passing one.
