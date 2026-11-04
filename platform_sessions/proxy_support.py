"""Reusable configuration and HTTP transport for the session proxy."""

from collections.abc import Callable, Set

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from platform_sessions.auth import BearerAuth
from platform_sessions.http_types import HttpToken
from platform_sessions.proxy import HeaderPolicy, ProxyInstance, ProxyResponse


class HeaderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    request: Set[HttpToken] = Field(default_factory=set)
    response: Set[HttpToken] = Field(default_factory=set)


class HttpSessionTransport:
    def __init__(self, store, session_headers: Callable, transport=None):
        self.store = store
        self.session_headers = session_headers
        self.transport = transport

    async def request(self, request):
        return await run_in_threadpool(self._request, request)

    def _request(self, request):
        session = self.store.load()
        if session is None:
            return ProxyResponse(503, (), b"Session unavailable")
        headers = list(request.headers) + list(self.session_headers(session))
        url = request.url + ("?" + request.query.decode("ascii") if request.query else "")
        with (
            httpx2.Client(transport=self.transport, trust_env=False, follow_redirects=False, timeout=20) as client,
            client.stream(request.method, url, headers=headers, content=request.body) as response,
        ):
            # Preserve raw compressed bytes alongside the upstream encoding header.
            body = b"".join(response.iter_raw())
            return ProxyResponse(response.status_code, tuple(response.headers.raw), body)


def create_proxy(runtimes, configs, header_configs, adapter_factory):
    instances = {}
    for runtime in runtimes:
        adapter, policy = adapter_factory(runtime)
        extra = header_configs.get(runtime.config.id, HeaderConfig())
        policy = HeaderPolicy(request=policy.request | extra.request, response=policy.response | extra.response)
        instances[runtime.config.id] = ProxyInstance(runtime.config, adapter, policy)
    if set(header_configs) - instances.keys():
        raise ValueError("Proxy headers reference an unknown or disabled integration")
    return instances, BearerAuth.from_config(configs, instances=instances.keys())
