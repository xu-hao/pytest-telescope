import pytest
from app.records import total

# telescope-scope: bundles=["io"]

TOTALS = [40, 55, 70, 85, 100, 115, 130, 145]


@pytest.mark.parametrize("number", range(8))
def test_record_total(number):
    assert total(f"record-{number}") == TOTALS[number]
