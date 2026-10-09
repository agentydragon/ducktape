from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Annotated

from pydantic import AfterValidator, AnyHttpUrl, AnyUrl, TypeAdapter, UrlConstraints

_HTTP_URL: TypeAdapter[AnyUrl] = TypeAdapter(
    Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https", "http"], host_required=True)]
)


class HttpAllowance(StrEnum):
    NONE = "none"
    LOOPBACK = "loopback"


def no_credentials(url: AnyUrl) -> AnyUrl:
    if url.username is not None or url.password is not None:
        raise ValueError("URL must not contain credentials")
    return url


def no_query(url: AnyUrl) -> AnyUrl:
    if url.query is not None:
        raise ValueError("URL must not contain a query")
    return url


def no_fragment(url: AnyUrl) -> AnyUrl:
    if url.fragment is not None:
        raise ValueError("URL must not contain a fragment")
    return url


def _is_loopback_url(url: AnyUrl) -> bool:
    hostname = url.host
    if hostname is None:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


def _https_or_loopback_http(url: AnyUrl) -> AnyUrl:
    if url.scheme == "http" and not _is_loopback_url(url):
        raise ValueError("HTTP URLs must use a loopback host")
    return url


HttpEndpointUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(host_required=True),
    AfterValidator(no_credentials),
    AfterValidator(no_query),
    AfterValidator(no_fragment),
]
HttpsEndpointUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(allowed_schemes=["https"], host_required=True),
    AfterValidator(no_credentials),
    AfterValidator(no_query),
    AfterValidator(no_fragment),
]
HttpsOrLoopbackHttpEndpointUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(host_required=True),
    AfterValidator(no_credentials),
    AfterValidator(no_query),
    AfterValidator(no_fragment),
    AfterValidator(_https_or_loopback_http),
]


def parse_https_url(
    value: str, *, http_allowance: HttpAllowance = HttpAllowance.NONE, allow_query: bool = False
) -> AnyUrl:
    """Parse an HTTPS URL, optionally allowing HTTP for loopback OIDC discovery."""
    url = _HTTP_URL.validate_python(value)
    if url.scheme == "http" and http_allowance is not HttpAllowance.LOOPBACK:
        raise ValueError("URL must use HTTPS or explicitly allowed HTTP")
    if url.scheme == "http":
        _https_or_loopback_http(url)
    no_credentials(url)
    no_fragment(url)
    if not allow_query:
        no_query(url)
    return url
