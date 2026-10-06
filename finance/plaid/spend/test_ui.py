"""Smoke-check the assets packaged with the Spend service."""

import pytest_bazel

from finance.plaid.spend.app import _UI_DIR


def test_ui_bundle() -> None:
    assert (_UI_DIR / "index.html").is_file()
    assert (_UI_DIR / "main.js").is_file()
    assert (_UI_DIR / "main.css").is_file()
    html = (_UI_DIR / "index.html").read_text("utf-8")
    assert "/static/main.js" in html
    assert "/static/main.css" in html


if __name__ == "__main__":
    pytest_bazel.main()
