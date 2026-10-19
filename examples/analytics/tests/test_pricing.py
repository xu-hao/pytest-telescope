import json

from demoapp.pricing import exposure


def test_empty():
    assert exposure([], 20) == 0


def test_price_schedule(data_dir):
    products = json.loads((data_dir / "prices.json").read_text())
    total = exposure(products, 250_000)
    assert 100_000_000 < total < 1_000_000_000
