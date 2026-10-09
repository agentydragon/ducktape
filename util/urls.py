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
    LOCALHOST = "localhost"
    LOOPBACK = "loopback"


def _is_loopback_url(parsed: AnyUrl) -> bool:
    hostname = parsed.host
    if hostname is None:
        return False
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname.removeprefix("[").removesuffix("]")).is_loopback
    except ValueError:
        return False


def _allows_http(parsed: AnyUrl, allowance: HttpAllowance) -> bool:
    if allowance is HttpAllowance.LOCALHOST:
        return parsed.host in {"localhost", "127.0.0.1"}
    if allowance is HttpAllowance.LOOPBACK:
        return _is_loopback_url(parsed)
    return False


def parse_https_url(
    value: str, *, http_allowance: HttpAllowance = HttpAllowance.NONE, allow_query: bool = False
) -> AnyUrl:
    """Parse an absolute HTTPS URL, optionally allowing HTTP within a named scope."""
    return _validate_https_url(_HTTP_URL.validate_python(value), http_allowance=http_allowance, allow_query=allow_query)


def _validate_https_url(
    parsed: AnyUrl, *, http_allowance: HttpAllowance = HttpAllowance.NONE, allow_query: bool = False
) -> AnyUrl:
    if (
        (parsed.scheme == "http" and not _allows_http(parsed, http_allowance))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError("URL must use HTTPS or explicitly allowed HTTP")
    return parsed


def _validate_https_or_localhost_http_url(value: AnyUrl) -> AnyUrl:
    return _validate_https_url(value, http_allowance=HttpAllowance.LOCALHOST)


HttpsUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(allowed_schemes=["https"], host_required=True, preserve_empty_path=True),
    AfterValidator(_validate_https_url),
]
HttpsOrLocalhostHttpUrl = Annotated[
    AnyHttpUrl,
    UrlConstraints(host_required=True, preserve_empty_path=True),
    AfterValidator(_validate_https_or_localhost_http_url),
]
