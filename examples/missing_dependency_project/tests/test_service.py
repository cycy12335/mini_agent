from src.service import render


def test_render():
    assert render("hello") == "hello"
