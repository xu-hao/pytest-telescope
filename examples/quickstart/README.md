# Per-case quickstart

These are the base files for the [runnable tutorial](https://pytest-telescope.readthedocs.io/en/latest/).
They use pytest-telescope 0.1.0a4 with Bubblewrap (`bwrap`) on Linux.

From this directory, with pytest-telescope installed:

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The first run executes three cases. The second runs the ordinary note case again and
reuses the two scoped successes. The red and blue cases are in the same file but declare
separate JSON resources; both use the pricing bundle. The unmarked note case is ordinary:
it runs in an ordinary pytest session every time and is never cached.

The tutorial adds parameter-specific inputs and demonstrates narrow and shared changes
and an undeclared file. Every scoped case starts a fresh sandbox and pytest session,
including each parametrized variant.
