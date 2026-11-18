import pytest

from inventree_utils.beautifier.fix_lcsc_links import normalize_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://example.com", "http://example.com"),
        (
            "https://www.lcsc.com/product-detail/ABC_def_C999_C123456.html",
            "https://www.lcsc.com/product-detail/C123456.html",
        ),
    ],
)
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected
