"""Authenticate a Sandbox workload bearer and transparently stream requests to LiteLLM."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.responses import Response, StreamingResponse

from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipal

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "x-litellm-api-key"})
_UNTRUSTED_METADATA_HEADERS = frozenset(
    {"x-litellm-agent-id", "x-litellm-customer-id", "x-litellm-spend-logs-metadata"}
)
_IDENTITY_HEADER_PREFIXES = ("x-agentplane-", "x-sandbox-", "x-pod-", "x-agent-", "x-thread-")
_REQUEST_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]


@dataclass(frozen=True)
class IngressResources:
    """The only authorities the ingress holds: workload auth, one backend, and one key."""

    authenticate: SandboxPrincipalAuthenticator
    backend: httpx.AsyncClient
    litellm_key: str


def _verified_metadata(principal: SandboxPrincipal) -> str:
    return json.dumps(
        {
            "agentplane.namespace": principal.namespace,
            "agentplane.service_account": principal.service_account_name,
            "agentplane.service_account_subject": principal.service_account_subject,
            "agentplane.pod_name": principal.pod_name,
            "agentplane.pod_uid": principal.pod_uid,
            "agentplane.sandbox_name": principal.sandbox_name,
            "agentplane.sandbox_uid": principal.sandbox_uid,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _forwarded_request_headers(
    request: Request, principal: SandboxPrincipal, litellm_key: str
) -> list[tuple[str, str]]:
    forwarded: list[tuple[str, str]] = []
    connection_tokens = {
        token.strip().lower()
        for value in request.headers.getlist("connection")
        for token in value.split(",")
        if token.strip()
    }
    for name_bytes, value_bytes in request.scope["headers"]:
        name = name_bytes.decode("latin-1")
        lowered = name.lower()
        if (
            lowered in _HOP_BY_HOP
            or lowered in connection_tokens
            or lowered in _CREDENTIAL_HEADERS
            or lowered in _UNTRUSTED_METADATA_HEADERS
            or lowered in {"host", "content-length"}
            or lowered.startswith(_IDENTITY_HEADER_PREFIXES)
        ):
            continue
        forwarded.append((name, value_bytes.decode("latin-1")))
    forwarded.extend(
        [("Authorization", f"Bearer {litellm_key}"), ("x-litellm-spend-logs-metadata", _verified_metadata(principal))]
    )
    return forwarded


def _forwarded_response_headers(response: httpx.Response) -> list[tuple[bytes, bytes]]:
    connection_tokens = {
        token.strip().lower()
        for value in response.headers.get_list("connection")
        for token in value.split(",")
        if token.strip()
    }
    return [
        (name, value)
        for name, value in response.headers.raw
        if name.decode("latin-1").lower()
        not in (_HOP_BY_HOP | connection_tokens | _CREDENTIAL_HEADERS | {"content-length"})
    ]


def create_app(resources: IngressResources) -> FastAPI:
    app = FastAPI(title="agentplane-llm-ingress")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    async def principal(request: Request) -> SandboxPrincipal:
        return await resources.authenticate(request)

    principal_dependency = Depends(principal)

    @app.api_route("/{path:path}", methods=_REQUEST_METHODS)
    async def forward(request: Request, path: str, verified: SandboxPrincipal = principal_dependency) -> Response:
        del path  # The raw ASGI path below is the whole forwarded path; this value is only routing syntax.
        url = request.url.path
        if request.url.query:
            url += f"?{request.url.query}"
        upstream_request = resources.backend.build_request(
            request.method,
            url,
            headers=_forwarded_request_headers(request, verified, resources.litellm_key),
            content=await request.body(),
        )
        try:
            upstream = await resources.backend.send(upstream_request, stream=True)
        except httpx.HTTPError as error:
            # Never include the exception: transport errors can contain request headers.
            raise HTTPException(status_code=502, detail="LiteLLM backend unavailable") from error

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                # Client disconnect/cancellation closes the in-flight internal hop too.
                await upstream.aclose()

        streamed = StreamingResponse(body(), status_code=upstream.status_code, media_type=None)
        streamed.raw_headers = _forwarded_response_headers(upstream)
        return streamed

    return app
