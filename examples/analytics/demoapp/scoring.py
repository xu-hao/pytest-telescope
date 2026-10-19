"""Aggregate a synthetic customer score across seasonal periods."""

from demoapp.common import weighted


def score(customers: list[dict], rounds: int) -> float:
    total = 0.0
    for period in range(rounds):
        season = 1.0 / (1 + period % 17)
        for customer in customers:
            total += weighted(customer["value"], customer["quality"]) * season
    return round(total, 8)
