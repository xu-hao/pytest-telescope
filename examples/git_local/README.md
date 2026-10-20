# Private local Git example

This example shows the proposed `git-local-v2` migration. The replacement runtime,
Git loader and lifecycle are **UNQUALIFIED**; it is not a published 0.1.0a4 recipe.
After the production candidate and explicit runtime path are qualified, the intended
commands from this directory are:

```sh
python -m pytest_telescope runtime
python -m pytest_telescope plan
python -m pytest_telescope run
python -m pytest_telescope run
```

The expected first run executes one scoped case; the next reuses it. The test declares its input beside
the test, copies that read-only file into private scratch, commits with explicit author
identity and dates, pushes to a private local-file bare origin and clones it. No external
network, host repository, writable project source or executable scratch is required.

Changing `data/message.txt` reruns the case. The profile does not make arbitrary Git
commands deterministic: tests must control their own inputs, dates and configuration.
`cache=False` means execute fresh without reusable recording, not wider capabilities.

See the [runtime contract](../../docs/git-runtime.md) for pinned provenance, preparation
bounds and explicitly unsupported capabilities.
