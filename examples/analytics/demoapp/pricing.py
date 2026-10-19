"""Calculate a synthetic schedule's weighted price exposure."""

from demoapp.common import weighted


def exposure(products: list[dict], rounds: int) -> float:
    total = 0.0
    for period in range(rounds):
        discount = 1.0 - (period % 13) / 100
        for product in products:
            total += weighted(product["price"], product["quantity"]) * discount
    return total
