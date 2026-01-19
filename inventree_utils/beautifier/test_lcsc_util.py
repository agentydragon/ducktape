import pytest
import pytest_bazel

from inventree_utils.beautifier.lcsc_util import parse_url_for_lcsc_id


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("https://www.lcsc.com/product-detail/C123456.html", id="normalized"),
        pytest.param("https://www.lcsc.com/product-detail/ABC_def_C999_C123456.html", id="without_query"),
        pytest.param("https://www.lcsc.com/product-detail/ABC_def-123_C123456.html?foobar", id="with_query"),
        pytest.param(
            "https://www.lcsc.com/product-detail/RGB-LEDs-Built-in-IC_Worldsemi-WS2812B-B-W_C123456.html?s_z=n_C123456",
            id="ws2812b",
        ),
    ],
)
def test_parse_url_for_lcsc_id_valid_forms(url: str):
    parsed_id = parse_url_for_lcsc_id(url)
    expected_id = "C123456"
    if parsed_id != expected_id:
        assert parsed_id == expected_id


if __name__ == "__main__":
    pytest_bazel.main()
