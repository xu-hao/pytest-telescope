from pathlib import Path

import pytest
from app.pricing import quote


@pytest.mark.telescope(inputs=["data/red.json"], bundles=["pricing"])
def test_red():
    assert quote("red", 0)["amount"] == 0
    assert quote("red", 2)["amount"] == 2 * quote("red", 1)["amount"] > 0
    assert quote("red", 1)["currency"] in {"USD", "EUR"}


@pytest.mark.telescope(inputs=["data/blue.json"], bundles=["pricing"])
def test_blue():
    assert quote("blue", 0)["amount"] == 0
    assert quote("blue", 2)["amount"] == 2 * quote("blue", 1)["amount"] > 0
    assert quote("blue", 1)["currency"] in {"USD", "EUR"}


def test_project_note():
    assert Path("notes.txt").read_text().strip()
