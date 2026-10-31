# Per-case example

Two tests in this example read different JSON files. A third has no input declaration
and runs every time. You need pytest-telescope 0.1.0a6 and Bubblewrap (`bwrap`) on Linux.
To set up your own test suite, follow the
[existing-project quickstart](https://pytest-telescope.readthedocs.io/en/latest/).

From this directory, with pytest-telescope installed:

```sh
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

With an empty cache, the first run executes all three cases. The second runs the note
case again and reuses the red and blue results. Change the red JSON file and run again:
the red test reruns, while the blue test can reuse its result. Both depend on the
`pricing` bundle, so a change to a file in that bundle reruns both tests.

Every scoped case starts a fresh sandbox and pytest session. The
[marker reference](https://pytest-telescope.readthedocs.io/en/latest/specification/)
also covers parameter-specific inputs, shared declarations and absent files.
