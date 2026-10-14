import pytest

from src.parser import parse_integers


def test_numbers_with_whitespace():
    assert parse_integers(" 1, -2, 3 ") == [1, -2, 3]


def test_blank_input():
    assert parse_integers("   ") == []


def test_invalid_number_is_rejected():
    with pytest.raises(ValueError):
        parse_integers("1, nope")
