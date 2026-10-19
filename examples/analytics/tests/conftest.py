from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def data_dir():
    return Path(__file__).resolve().parents[1] / "data"
