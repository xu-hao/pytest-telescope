# Per-case example

This standalone example shows per-case declarations with pytest-telescope 0.1.0a6 and
Bubblewrap (`bwrap`) on Linux. To adopt it in your own test suite, follow the
[existing-project quickstart](https://pytest-telescope.readthedocs.io/en/latest/).

From this directory, with pytest-telescope installed:

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The first run executes three cases. The second runs the ordinary note case again and
reuses the two scoped successes. The red and blue cases are in the same file but declare
separate JSON resources; both use the pricing bundle. The unmarked note case is ordinary:
it runs in an ordinary pytest session every time and is never cached.

Every scoped case starts a fresh sandbox and pytest session. The
[marker reference](https://pytest-telescope.readthedocs.io/en/latest/specification/)
also covers parameter-specific inputs, shared declarations and absent files.
