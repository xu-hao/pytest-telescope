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
python -m mkdocs build --strict
```

The [benchmark](benchmark.md) uses a copied synthetic project and external clocks. It
must preserve per-phase command results and distinguish host controller CPU from
container worker CPU. Benchmark output is qualification evidence, not a universal
speedup guarantee.

The CI matrix covers supported host Python/pytest versions. Backend qualification must
additionally execute on a supported Docker host. Keep build/documentation tools in
development dependencies and keep runtime contents explicit. Never use an editable
application install as evidence of a clean isolated runtime.
