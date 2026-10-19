import json

import pytest
from demoapp.scoring import score

pytestmark = pytest.mark.telescope(bundles=["scoring"])


def test_empty():
    assert score([], 20) == 0


@pytest.mark.telescope(inputs=["data/scoring-check.json"])
def test_customer_scores(data_dir):
    customers = json.loads((data_dir / "customers.json").read_text())
    total = score(customers, 250_000)
    check = json.loads((data_dir / "scoring-check.json").read_text())
    assert check["minimum"] < total < 100_000_000
