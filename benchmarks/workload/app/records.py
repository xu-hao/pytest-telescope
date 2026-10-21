"""IO-light work: read one small JSON record and summarize it."""

import json
from pathlib import Path


def total(name):
    record = json.loads(Path("data", name + ".json").read_text())
    return sum(item["quantity"] * item["price"] for item in record["items"])
