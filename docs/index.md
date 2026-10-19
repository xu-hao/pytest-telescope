# pytest-telescope

Declared inputs. Isolated tests. Focused reruns.

One manifest defines a test target's visible project files and the inputs used to
invalidate successful cached results. A target is one fresh pytest session in a Linux
Docker worker, including collection, imports, fixtures, and teardown.

Alpha 0.1.0a1 implements this contract. Real backend tests qualify the supported Linux
Docker boundary, and the benchmark records measured results for cold, warm, and
selective reruns. The reuse assumptions are explicit; filesystem isolation does not
establish universal determinism.

- [Specification](specification.md): quickstart schema, execution boundary, cache,
  grouping, and failure recovery.
- [Benchmark](benchmark.md): synthetic workload, measurement method, and results.
- [Development](development.md): install, build, and run behavioral checks.
- [Publishing](publishing.md): separate documentation and package publication.

Host tooling supports Python 3.11+ and pytest 8.3 through 9.x. The first worker backend
uses Linux Docker with a pinned Python 3.12 image. A missing or unusable backend is an
infrastructure error; there is no unrestricted fallback.
