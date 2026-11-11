# Contributing

See [development](docs/development.md) for installation and checks, the
[execution contract](docs/execution-contract.md) for declarations, inventory and reuse,
and the [sandbox page](docs/bubblewrap.md) for routing and the sandbox view.

Include behavioral regression tests and documentation with behavior changes. Sandbox
tests must run real Bubblewrap and check which files a test can see; skipped or
expected-failure tests do not count as passing. Sandbox changes must keep the
[one-path rule](docs/bubblewrap.md#one-path-for-every-file): a scoped session shows every
file at exactly one path. Report benchmark overhead and slower
results along with any improvements.

Documentation is Markdown rendered by Sphinx with Furo and MyST. Use the strict
documentation command in the development guide and inspect desktop/mobile navigation
for layout changes.

Contributions are licensed under the project's MIT License.
