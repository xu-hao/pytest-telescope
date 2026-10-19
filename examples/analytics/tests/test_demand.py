import json

from demoapp.demand import project


def test_empty():
    assert project([], 20) == 0


def test_demand_projection(data_dir):
    items = json.loads((data_dir / "demand.json").read_text())
    total = project(items, 250_000)
    assert 100_000_000 < total < 1_000_000_000
