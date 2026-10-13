"""Intentionally buggy fixture for the agent's future repair workflow."""


def average(values: list[float]) -> float:
    """Return the mean; the empty-list bug is intentionally left for repair."""
    return sum(values) / len(values)
