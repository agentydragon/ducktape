from __future__ import annotations

import ipaddress
from typing import Annotated

from pydantic import AfterValidator, AnyHttpUrl, AnyUrl, UrlConstraints


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


def https_or_loopback_http(url: AnyHttpUrl) -> AnyHttpUrl:
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
    AfterValidator(https_or_loopback_http),
]
