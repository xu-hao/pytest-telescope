# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

**Pre-alpha: static declaration planning only; no sandbox or result cache.**
The completed plugin will use one declaration of test inputs both to enforce file
access and to decide which successful results can be reused.

## Try the scaffold

Use Python 3.11 or newer in a dedicated virtual environment. From a checkout:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
python -m pytest
```

The installed `pytest11` entry point is inactive unless requested. Ordinary pytest
runs normally, with its usual filesystem access and outcomes. To prevent plugin
loading entirely, use `python -m pytest -p no:telescope`.

```sh
python -m pytest --telescope
```

This currently exits with pytest usage status **4**, before initial conftest and
test-module imports. It does not execute an unrestricted substitute or claim a
cache hit. Other installed pytest plugins still participate in normal pytest
startup; this scaffold is not a security boundary.

## What comes next

The [specification](https://github.com/xu-hao/pytest-telescope/blob/main/docs/specification.md) describes the intended Linux sandbox,
pre-import declarations, fresh workers, exact-snapshot reuse, and qualification
requirements. These are requirements, not current guarantees. No sandbox backend
has been selected or validated and no performance results are available.

Start with [contributor instructions](https://github.com/xu-hao/pytest-telescope/blob/main/CONTRIBUTING.md). The
[implementation epic](https://github.com/xu-hao/pytest-telescope/issues/3) tracks
manifests, the first enforcement slice, selection/cache, lifecycle, and qualification
in dependency order. [Publishing](https://github.com/xu-hao/pytest-telescope/blob/main/docs/publishing.md) covers documentation hosting
and the separate package release workflow.

Licensed under the [MIT License](https://github.com/xu-hao/pytest-telescope/blob/main/LICENSE).

## Plan declared inputs

Add the target/bundle tables shown in the [planner specification](https://github.com/xu-hao/pytest-telescope/blob/main/docs/specification.md)
to your project's `pyproject.toml`, then run from that project's root:

```sh
python -m pytest --telescope-plan
```

This validates explicit existing file paths and dependency bundles, prints a
sorted JSON plan, and exits before importing conftest or test modules. It does not
execute tests, enforce file access, freeze inputs, or reuse previous results.
Unknown references, cycles, malformed declarations, and paths escaping the project
root fail clearly. Globs and absent-input declarations are not supported yet.
