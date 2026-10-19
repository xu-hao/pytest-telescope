"""Project demand across recurring weekly adjustments."""

from demoapp.common import weighted


def project(items: list[dict], rounds: int) -> float:
    total = 0.0
    for period in range(rounds):
        adjustment = 1.0 + (period % 7) / 100
        for item in items:
            total += weighted(item["units"], item["growth"]) * adjustment
    return total
