"""Fixture: parse a comma-separated list of integers."""


def parse_integers(text: str) -> list[int]:
    """Blank input should produce an empty list, but this version raises."""
    return [int(part.strip()) for part in text.split(",")]
