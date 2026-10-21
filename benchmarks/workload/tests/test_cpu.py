import pytest
from app.compute import chain, longest_collatz

# telescope-scope: bundles=["cpu"]


@pytest.mark.parametrize(
    ("seed", "digest"),
    [
        ("alpha", "21ee397784760430d727847d5b7bae4df91dae69b5d8f2e529a55f3bc98275d3"),
        ("beta", "53672146f35d52d37551d63ee34ac019cc2bec8d9fcf07fabe1bb5021208f098"),
    ],
)
def test_hash_chain(seed, digest):
    assert chain(seed, 600_000) == digest


@pytest.mark.parametrize(("limit", "expected"), [(30_000, (26623, 307)), (40_000, (35655, 323))])
def test_longest_collatz(limit, expected):
    assert longest_collatz(limit) == expected
