import json

from demoapp.scoring import score


def test_empty():
    assert score([], 20) == 0


def test_customer_scores(data_dir):
    customers = json.loads((data_dir / "customers.json").read_text())
    total = score(customers, 250_000)
    assert 1_000_000 < total < 100_000_000
