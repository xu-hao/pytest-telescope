# Contributing

See [development](docs/development.md) for installation and checks, and the
[execution contract](docs/bubblewrap.md) for routing and the sandbox view.

Add behavioral regression tests and relevant documentation together. Tests that launch
Bubblewrap must establish the filesystem view before it is claimed; skipped or
expected-failure sandbox tests are not a qualifying result. Publish observed benchmark
measurements, including overhead and unfavorable results.

Documentation is Markdown rendered by Sphinx with Furo and MyST. Use the strict
documentation command in the development guide and inspect desktop/mobile navigation
for layout changes.

Contributions are licensed under the project's MIT License.
