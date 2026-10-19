# Product specification

Alpha 0.1.0a1 implements declared-input isolation and result reuse on Linux Docker. The
[execution and reuse contract](execution-contract.md) is authoritative: declarations
before imports, one fresh pytest session per target, static test-file inventory,
immutable input snapshots, enforced worker restrictions, explicit runtime contents,
exact cache identity, and bounded failure recovery.

Qualification includes real backend integration tests and [measured benchmark
results](benchmark.md). The alpha does not claim universal determinism: tests depending
on uncontrolled clocks, randomness, native state, or external services must be
explicitly non-cacheable or satisfy the reuse assumptions themselves.
