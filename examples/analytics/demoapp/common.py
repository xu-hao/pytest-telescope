"""Shared normalization consumed by every target."""

SCALE = 1.0


def weighted(value: float, weight: float) -> float:
    return max(0.0, value) * weight * SCALE
