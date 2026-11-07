"""Private HTTP transport from the platform gateway to one session owner."""

from __future__ import annotations

import re
import ssl
from urllib.parse import urlsplit

import httpx2
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool

from platform_sessions.proxy import ProxyRequest, ProxyResponse, _origin
from platform_sessions.registry import _upstream_url
from platform_sessions.routes import _encoded_path, _segments

# The provider adapter adds this only after authenticating the gateway and
# dispatching the request. Its absence distinguishes an internal-hop failure
# from an upstream response, including an upstream 401 or 403.
RESULT_HEADER = b"x-platform-session-proxy-result"
RESULT_VERSION = b"1"


class ProviderProtocolError(ValueError):
    """The private provider did not return a valid proxy response."""


class RemoteSessionTransport:
    """Forward exactly once to a fixed provider endpoint without credentials in URLs.

    The caller must set request.owner only from its authenticated principal.
    Separate gateway-only internal credentials preserve that provenance.
    """

    def __init__(
        self,
        *,
        instance_id: str,
        upstream_base_url: str,
        provider_base_url: str,
        owner_token: SecretStr,
        route_token: SecretStr,
        tls_context: ssl.SSLContext | None = None,
        transport=None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", instance_id):
            raise ValueError("Invalid provider instance")
        self.instance_id = instance_id
        self.upstream_base_url = _upstream_url(upstream_base_url)
        self.provider_base_url = _upstream_url(provider_base_url)
        if urlsplit(self.provider_base_url).scheme != "https":
            raise ValueError("Provider base URL must use HTTPS")
        if urlsplit(self.provider_base_url).path not in {"", "/"}:
            raise ValueError("Provider base URL must be an origin")
        self._upstream_prefix = _segments(urlsplit(self.upstream_base_url).path or "/")
        self._owner_token = owner_token
        self._route_token = route_token
        for token in (owner_token, route_token):
            value = token.get_secret_value()
            if not value or not value.isascii() or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
                raise ValueError("Invalid internal credential")
        if owner_token.get_secret_value() == route_token.get_secret_value():
            raise ValueError("Internal credentials must be distinct")
        if tls_context is not None and (tls_context.verify_mode != ssl.CERT_REQUIRED or not tls_context.check_hostname):
            raise ValueError("Provider TLS must verify certificates and hostnames")
        self.tls_context = tls_context
        self.transport = transport

    async def request(self, request: ProxyRequest) -> ProxyResponse:
        return await run_in_threadpool(self._request, request)

    def _request(self, request: ProxyRequest) -> ProxyResponse:
        # Validate against the configured origin and segment prefix; an upstream
        # URL supplied to this adapter is never forwarded to the remote server.
        url = _upstream_url(request.url)
        if _origin(url) != _origin(self.upstream_base_url):
            raise ValueError("Request does not match the configured upstream")
        segments = _segments(urlsplit(url).path or "/")
        prefix = self._upstream_prefix
        if segments[: len(prefix)] != prefix:
            raise ValueError("Request is outside the configured upstream base")
        relative_path = _encoded_path(segments[len(prefix) :])
        target = self.provider_base_url.rstrip("/") + "/internal/proxy/" + self.instance_id + relative_path
        if request.query:
            target += "?" + request.query.decode("ascii")
        # Reject caller-supplied credential or internal-marker headers even if
        # a future caller bypasses the public proxy's normal header filtering.
        excluded = {b"authorization", b"cookie", b"host", RESULT_HEADER}
        headers = [(key, value) for key, value in request.headers if key.lower() not in excluded]
        token = self._owner_token if request.owner else self._route_token
        headers.append((b"authorization", b"Bearer " + token.get_secret_value().encode("ascii")))
        with (
            httpx2.Client(
                transport=self.transport,
                verify=self.tls_context if self.tls_context is not None else True,
                trust_env=False,
                follow_redirects=False,
                timeout=20,
            ) as client,
            client.stream(request.method, target, headers=headers, content=request.body) as response,
        ):
            markers = response.headers.get_list(RESULT_HEADER.decode("ascii"))
            if markers != [RESULT_VERSION.decode("ascii")]:
                raise ProviderProtocolError("Invalid provider proxy response")
            body = b"".join(response.iter_raw())
            response_headers = tuple((k, v) for k, v in response.headers.raw if k.lower() != RESULT_HEADER)
            return ProxyResponse(response.status_code, response_headers, body)
