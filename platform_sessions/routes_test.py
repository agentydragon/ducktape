"""Authorization and upstream path mapping share one path interpretation."""

import pytest
import pytest_bazel

from platform_sessions.routes import Route, authorize_route


def test_exact_methods_and_path_relative_to_base():
    rules = (Route("GET", "/orders"),)
    route = authorize_route("GET", "/orders", "https://example.test/api/", rules)
    assert route is not None
    assert route.relative_path == "/orders"
    assert route.upstream_url == "https://example.test/api/orders"
    assert authorize_route("HEAD", "/orders", "https://example.test/api", rules) is None
    assert authorize_route("GET", "/api/orders", "https://example.test/api", rules) is None


def test_wildcard_is_one_segment_and_decoding_is_consistent():
    rules = (Route("GET", "/items/*"),)
    match = authorize_route("GET", "/items/caf%C3%A9", "https://example.test", rules)
    assert match is not None
    assert match.relative_path == "/items/caf%C3%A9"
    assert match.upstream_url == "https://example.test/items/caf%C3%A9"
    assert authorize_route("GET", "/items/a/b", "https://example.test", rules) is None
    assert authorize_route("GET", "/items/", "https://example.test", rules) is None
    slash = authorize_route("GET", "/items/", "https://example.test", (Route("GET", "/items/"),))
    assert slash is not None
    assert slash.upstream_url == "https://example.test/items/"


@pytest.mark.parametrize(
    "path", ["//evil.test/x", "/%2fexample", "/a%5cb", "/%2e%2e/x", "/%252f", "/x%", "/x?y", "/x#y", "/%00", "/%FF"]
)
def test_reject_ambiguous_paths(path):
    with pytest.raises(ValueError, match="path"):
        authorize_route("GET", path, "https://example.test/base", (Route("GET", "/*"),))


def test_invalid_rules_and_base_paths():
    with pytest.raises(ValueError, match="method"):
        Route("get", "/items")
    with pytest.raises(ValueError, match="wildcard"):
        Route("GET", "/item*")
    with pytest.raises(ValueError, match="path"):
        authorize_route("GET", "/items", "https://example.test/%2e%2e", (Route("GET", "/items"),))


if __name__ == "__main__":
    pytest_bazel.main()
