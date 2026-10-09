from __future__ import annotations

import ipaddress
from enum import StrEnum
from typing import Annotated
from urllib.parse import SplitResult, urlsplit

from pydantic import AfterValidator, AnyUrl, TypeAdapter

# Single shared AnyUrl adapter for fast validation/coercion across modules
ANY_URL: TypeAdapter[AnyUrl] = TypeAdapter(AnyUrl)


class HttpAllowance(StrEnum):
    NONE = "none"
    LOCALHOST = "localhost"
    LOOPBACK = "loopback"


def _is_loopback_url(parsed: SplitResult) -> bool:
    hostname = parsed.hostname
    if hostname is None:
        return False
    hostname = hostname.casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _allows_http(parsed: SplitResult, allowance: HttpAllowance) -> bool:
    if allowance is HttpAllowance.LOCALHOST:
        return parsed.hostname in {"localhost", "127.0.0.1"}
    if allowance is HttpAllowance.LOOPBACK:
        return _is_loopback_url(parsed)
    return False


def parse_https_url(
    value: str, *, http_allowance: HttpAllowance = HttpAllowance.NONE, allow_query: bool = False
) -> SplitResult:
    """Parse an absolute HTTPS URL, optionally allowing HTTP within a named scope."""
    parsed = urlsplit(value)
    if (
        (parsed.scheme != "https" and (parsed.scheme != "http" or not _allows_http(parsed, http_allowance)))
        or not parsed.netloc
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError("URL must use HTTPS or explicitly allowed HTTP")
    return parsed


def _validate_https_url_string(value: str) -> str:
    parse_https_url(value)
    return value


def _validate_https_or_localhost_http_url_string(value: str) -> str:
    parse_https_url(value, http_allowance=HttpAllowance.LOCALHOST)
    return value


HttpsUrlString = Annotated[str, AfterValidator(_validate_https_url_string)]
HttpsOrLocalhostHttpUrlString = Annotated[str, AfterValidator(_validate_https_or_localhost_http_url_string)]


# Internal module; keep imports explicit rather than curating a public API
