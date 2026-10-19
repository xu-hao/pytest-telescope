# Local development

Use a dedicated Python 3.11+ virtual environment. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[dev]'
python scripts/check.py --artifacts /tmp/telescope-check
```

The check command runs Ruff lint and formatting checks, builds the sdist and a
wheel from it, validates package metadata, builds documentation with warnings as
errors, installs the built wheel without dependencies, and runs behavioral tests.
It requires a virtual environment and uses its interpreter. The artifact directory
must not already exist; use a new directory for another run. Build dependencies
may need network access. No package is uploaded by this command.

Tests launch subprocesses using the installed package, a minimal synthetic
environment, and temporary projects. They verify entry-point discovery, normal
passing/failing behavior, fixture teardown, explicit plugin disablement, and the
unfinished execution mode's refusal before conftest or test imports. They do not
prove sandbox enforcement, which is not implemented. Subprocess calls have a
finite timeout.

For documentation alone:

```sh
python -m pip install -r docs/requirements.txt
python -m mkdocs build --strict
```

CI checks Python 3.11 and 3.13 with pytest 8.3.5 and the latest supported 9.x.
Keep the runtime dependency list small; build and documentation tools are optional
development dependencies. Do not use editable application installs as evidence of
a future isolated runtime's contents.

Implement in the order recorded by the
[implementation epic](https://github.com/xu-hao/pytest-telescope/issues/3).
First define the pre-import manifest and unit inventory, then prove real OS
filesystem enforcement and correct invalidation in a small end-to-end slice.
Required sandbox tests must run on a supported host; skipping them is not a passing
enforcement verdict. Extend failure recovery and qualification alongside behavior.
