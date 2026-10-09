from typing import Annotated

import pytest
import pytest_bazel
from pydantic import AfterValidator, AnyHttpUrl, TypeAdapter, ValidationError

from util.urls import (
    HttpEndpointUrl,
    HttpsEndpointUrl,
    HttpsOrLoopbackHttpEndpointUrl,
    no_credentials,
    no_fragment,
    no_query,
)

_HTTP_ENDPOINT: TypeAdapter[AnyHttpUrl] = TypeAdapter(HttpEndpointUrl)
_HTTPS_ENDPOINT: TypeAdapter[AnyHttpUrl] = TypeAdapter(HttpsEndpointUrl)
_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT: TypeAdapter[AnyHttpUrl] = TypeAdapter(HttpsOrLoopbackHttpEndpointUrl)


@pytest.mark.parametrize(
    ("adapter", "value"),
    [
        pytest.param(_HTTP_ENDPOINT, "http://example.test/path", id="http-endpoint"),
        pytest.param(_HTTP_ENDPOINT, "https://example.test/path", id="https-endpoint"),
        pytest.param(_HTTPS_ENDPOINT, "https://example.test/path", id="https-only-endpoint"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "https://example.test/path", id="https-or-loopback-endpoint"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://localhost/path", id="localhost-http-endpoint"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://service.localhost/path", id="subdomain-localhost"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://127.0.0.2/path", id="ipv4-loopback"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://[::1]/path", id="ipv6-loopback"),
    ],
)
def test_endpoint_types_accept_their_url_schemes_and_scopes(adapter: TypeAdapter[AnyHttpUrl], value: str) -> None:
    assert str(adapter.validate_python(value)) == value


@pytest.mark.parametrize(
    ("adapter", "value"),
    [
        pytest.param(_HTTP_ENDPOINT, "https://user:secret@example.test/path", id="http-credentials"),
        pytest.param(_HTTP_ENDPOINT, "https://example.test/path?token=secret", id="http-query"),
        pytest.param(_HTTP_ENDPOINT, "https://example.test/path?", id="http-empty-query"),
        pytest.param(_HTTP_ENDPOINT, "https://example.test/path#fragment", id="http-fragment"),
        pytest.param(_HTTP_ENDPOINT, "https://example.test/path#", id="http-empty-fragment"),
        pytest.param(_HTTPS_ENDPOINT, "http://localhost/path", id="https-only-scheme"),
        pytest.param(_HTTPS_ENDPOINT, "https://user:secret@example.test/path", id="https-credentials"),
        pytest.param(_HTTPS_ENDPOINT, "https://example.test/path?token=secret", id="https-query"),
        pytest.param(_HTTPS_ENDPOINT, "https://example.test/path?", id="https-empty-query"),
        pytest.param(_HTTPS_ENDPOINT, "https://example.test/path#fragment", id="https-fragment"),
        pytest.param(_HTTPS_ENDPOINT, "https://example.test/path#", id="https-empty-fragment"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://remote.example.test/path", id="remote-http"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://user:secret@localhost/path", id="loopback-credentials"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://localhost/path?token=secret", id="loopback-query"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://localhost/path?", id="loopback-empty-query"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://localhost/path#fragment", id="loopback-fragment"),
        pytest.param(_HTTPS_OR_LOOPBACK_HTTP_ENDPOINT, "http://localhost/path#", id="loopback-empty-fragment"),
    ],
)
def test_endpoint_types_reject_disallowed_scheme_or_components(adapter: TypeAdapter[AnyHttpUrl], value: str) -> None:
    with pytest.raises(ValidationError):
        adapter.validate_python(value)


@pytest.mark.parametrize(
    "value",
    [
        "/relative/path",
        "https://",
        "file:///etc/passwd",
        "https://example.test:invalid/path",
        "https://example.test:65536/path",
    ],
)
def test_endpoint_types_reject_invalid_http_urls(value: str) -> None:
    for adapter in (_HTTP_ENDPOINT, _HTTPS_ENDPOINT, _HTTPS_OR_LOOPBACK_HTTP_ENDPOINT):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_endpoint_types_use_pydantic_url_normalization() -> None:
    url = _HTTP_ENDPOINT.validate_python("http://EXAMPLE.test:80")
    assert isinstance(url, AnyHttpUrl)
    assert str(url) == "http://example.test/"
    assert _HTTP_ENDPOINT.dump_python(url, mode="json") == "http://example.test/"


def test_public_url_validators_compose_with_annotated_types() -> None:
    without_query = Annotated[AnyHttpUrl, AfterValidator(no_query)]
    without_fragment = Annotated[AnyHttpUrl, AfterValidator(no_fragment)]
    without_credentials = Annotated[AnyHttpUrl, AfterValidator(no_credentials)]

    assert TypeAdapter(without_query).validate_python("https://example.test/path#fragment")
    assert TypeAdapter(without_fragment).validate_python("https://example.test/path?query=one")
    assert TypeAdapter(without_credentials).validate_python("https://example.test/path")
    with pytest.raises(ValidationError):
        TypeAdapter(without_query).validate_python("https://example.test/path?query=one")
    with pytest.raises(ValidationError):
        TypeAdapter(without_fragment).validate_python("https://example.test/path#fragment")
    with pytest.raises(ValidationError):
        TypeAdapter(without_credentials).validate_python("https://user:secret@example.test/path")


if __name__ == "__main__":
    pytest_bazel.main()
