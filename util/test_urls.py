import pytest
import pytest_bazel
from pydantic import TypeAdapter, ValidationError

from util.urls import HttpAllowance, HttpsOrLocalhostHttpUrlString, HttpsUrlString, parse_https_url


@pytest.mark.parametrize("http_allowance", list(HttpAllowance))
def test_https_is_accepted_under_every_http_allowance(http_allowance: HttpAllowance) -> None:
    assert parse_https_url("https://example.test/path", http_allowance=http_allowance).scheme == "https"


@pytest.mark.parametrize(
    ("http_allowance", "value", "accepted"),
    [
        pytest.param(HttpAllowance.NONE, "http://localhost/path", False, id="https-only"),
        pytest.param(HttpAllowance.NONE, "http://127.0.0.1/path", False, id="https-only-ipv4-loopback"),
        pytest.param(HttpAllowance.NONE, "http://remote.example.test/path", False, id="https-only-remote"),
        pytest.param(HttpAllowance.LOCALHOST, "http://localhost/path", True, id="app-localhost"),
        pytest.param(HttpAllowance.LOCALHOST, "http://127.0.0.1/path", True, id="app-ipv4-loopback"),
        pytest.param(HttpAllowance.LOCALHOST, "http://service.localhost/path", False, id="app-subdomain-localhost"),
        pytest.param(HttpAllowance.LOCALHOST, "http://[::1]/path", False, id="app-ipv6-loopback"),
        pytest.param(HttpAllowance.LOCALHOST, "http://remote.example.test/path", False, id="app-remote"),
        pytest.param(HttpAllowance.LOOPBACK, "http://localhost/path", True, id="oidc-localhost"),
        pytest.param(HttpAllowance.LOOPBACK, "http://127.0.0.1/path", True, id="oidc-ipv4-loopback"),
        pytest.param(HttpAllowance.LOOPBACK, "http://service.localhost/path", True, id="oidc-subdomain-localhost"),
        pytest.param(HttpAllowance.LOOPBACK, "http://[::1]/path", True, id="oidc-ipv6-loopback"),
        pytest.param(HttpAllowance.LOOPBACK, "http://remote.example.test/path", False, id="oidc-remote"),
    ],
)
def test_http_allowance_preserves_service_scope(http_allowance: HttpAllowance, value: str, accepted: bool) -> None:
    if accepted:
        assert parse_https_url(value, http_allowance=http_allowance).scheme == "http"
    else:
        with pytest.raises(ValueError, match="URL must use HTTPS or explicitly allowed HTTP"):
            parse_https_url(value, http_allowance=http_allowance)


@pytest.mark.parametrize("http_allowance", list(HttpAllowance))
@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@example.test/path",
        "https://example.test/path?token=secret",
        "https://example.test/path#fragment",
    ],
)
def test_credentials_query_and_fragment_are_rejected_by_default(http_allowance: HttpAllowance, value: str) -> None:
    with pytest.raises(ValueError, match="URL must use HTTPS or explicitly allowed HTTP"):
        parse_https_url(value, http_allowance=http_allowance)


@pytest.mark.parametrize("value", ["https://example.test:invalid/path", "https://example.test:65536/path"])
def test_invalid_ports_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="invalid port number"):
        parse_https_url(value)


@pytest.mark.parametrize("value", ["/relative/path", "https://", "file:///etc/passwd"])
def test_invalid_http_urls_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError, match="URL"):
        parse_https_url(value)


def test_parser_returns_pydantic_normalized_url() -> None:
    assert str(parse_https_url("https://EXAMPLE.test:443")) == "https://example.test/"


def test_query_can_be_allowed_without_allowing_fragments() -> None:
    assert parse_https_url("https://example.test/jwks?tenant=one", allow_query=True).query == "tenant=one"
    assert (
        parse_https_url(
            "http://127.0.0.1/jwks?tenant=one", http_allowance=HttpAllowance.LOOPBACK, allow_query=True
        ).query
        == "tenant=one"
    )
    with pytest.raises(ValueError, match="URL must use HTTPS or explicitly allowed HTTP"):
        parse_https_url("https://example.test/jwks?tenant=one#fragment", allow_query=True)


def test_https_string_alias_validates_and_preserves_raw_value() -> None:
    value = "https://Example.test:443/a%2fb"
    assert TypeAdapter(HttpsUrlString).validate_python(value) is value
    with pytest.raises(ValidationError):
        TypeAdapter(HttpsUrlString).validate_python("http://localhost/path")


@pytest.mark.parametrize("value", ["http://localhost/path", "http://127.0.0.1/path"])
def test_localhost_http_string_alias_validates_and_preserves_raw_value(value: str) -> None:
    assert TypeAdapter(HttpsOrLocalhostHttpUrlString).validate_python(value) is value


@pytest.mark.parametrize(
    "value", ["http://remote.example.test/path", "http://service.localhost/path", "http://[::1]/path"]
)
def test_localhost_http_string_alias_rejects_other_http_hosts(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(HttpsOrLocalhostHttpUrlString).validate_python(value)


if __name__ == "__main__":
    pytest_bazel.main()
