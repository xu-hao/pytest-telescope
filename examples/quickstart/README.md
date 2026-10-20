# Ordinary and scoped quickstart

These are the base files for the [tutorial](../../docs/index.md), describing the accepted
ordinary/Bubblewrap replacement. The backend remains **UNQUALIFIED**; these are expected
semantics, not a published installation or execution receipt.

After candidate installation and explicit scoped preparation, the intended commands are:

```sh
python -m pytest_telescope runtime
python -m pytest_telescope run --explain -- -q
python -m pytest_telescope run --explain -- -q
```

The first run executes three cases. An unchanged second run executes the ordinary note
case and reuses two scoped successes. Red and blue share a file but declare separate JSON
inputs and a pricing bundle. Defaults do not opt the unmarked note case into scope.
Ordinary collection remains fresh; scoped discovery independently validates membership.

The tutorial adds parameter-specific declarations and demonstrates individual/shared
input changes and a denied read. Scoped case mode starts a fresh session per actual
case. Ordinary cases use one suite session with normal fixture lifetimes; mixed-module
imports may run in ordinary context before scoped items leave pytest dispatch.
