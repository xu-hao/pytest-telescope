# Per-case quickstart

These are the base files for the [runnable tutorial](https://pytest-telescope.readthedocs.io/en/latest/).
They use pytest-telescope 0.1.0a3 and a working local Linux Docker daemon.

From this directory, with pytest-telescope installed:

```sh
python -m pytest_telescope runtime
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The first run executes three cases. The second reuses three successes. The red and blue
cases are in the same file but declare separate JSON resources. Both use the pricing
bundle; the unmarked note case sees and hashes the whole visible project snapshot.

The tutorial adds parameter-specific inputs and demonstrates narrow/shared/broad changes
and a denied read. Every actual case starts a fresh pytest session, including each
parametrized variant.
