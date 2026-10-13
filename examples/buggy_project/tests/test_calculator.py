from src.calculator import average


def test_average_non_empty():
    assert average([2, 4, 6]) == 4


def test_average_empty():
    assert average([]) == 0
