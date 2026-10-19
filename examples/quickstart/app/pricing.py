import json
from pathlib import Path


def quote(product, quantity):
    price = json.loads(Path("data", f"{product}.json").read_text())["unit_price"]
    currency = json.loads(Path("data/currency.json").read_text())["code"]
    return {"amount": quantity * price, "currency": currency}
