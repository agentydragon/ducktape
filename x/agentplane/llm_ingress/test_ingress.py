"""End-to-end ingress proof: real HTTP, fake Kubernetes, and a byte-streaming fake LiteLLM."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import cast

import httpx
import pytest
import pytest_bazel
from aiohttp import web
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import ApiClient, AuthenticationV1Api, CoreV1Api
from more_itertools import one

from x.agentplane.egress.resources import SANDBOXES_PLURAL
from x.agentplane.egress.testing.fake_apiserver import (
    SANDBOX_NAMESPACE,
    FakeApiServer,
    TokenVerdict,
    fake_apiserver,
    pod_for,
    sandbox,
)
from x.agentplane.llm_ingress.app import IngressResources, create_app
from x.agentplane.sandbox_auth.http import SandboxPrincipalAuthenticator
from x.agentplane.sandbox_auth.principal import SandboxPrincipalResolver

AUDIENCE = "agentplane-egress"
SUBJECT = f"system:serviceaccount:{SANDBOX_NAMESPACE}:agentplane-runner"
TOKEN_A = "opaque-workload-token-a"
TOKEN_B = "opaque-workload-token-b"
WRONG_AUDIENCE_TOKEN = "opaque-wrong-audience-token"
STALE_TOKEN = "opaque-stale-token"
REPLACED_TOKEN = "opaque-replaced-token"
UNOWNED_TOKEN = "opaque-unowned-token"
LITELLM_KEY = "sk-server-held-test-key"

TOOL_STREAM = b"".join(
    [
        b'event: response.output_item.added\ndata: {"type":"response.output_item.added","item":{"type":"function_call","name":"shell","call_id":"call_1"}}\n\n',
        b'event: response.function_call_arguments.delta\ndata: {"type":"response.function_call_arguments.delta","delta":"{\\"command\\":\\"pwd\\"}"}\n\n',
        b'event: response.completed\ndata: {"type":"response.completed","response":{"status":"completed"}}\n\n',
    ]
)
ANTHROPIC_ERROR = b'{"type":"error","error":{"type":"overloaded_error","message":"backend busy"}}'


@dataclass(frozen=True)
class BackendRequest:
    method: str
    path_qs: str
    headers: dict[str, str]
    body: bytes


@dataclass
class FakeLiteLLM:
    requests: list[BackendRequest] = field(default_factory=list)
    port: int = 0

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            BackendRequest(
                method=request.method,
                path_qs=request.path_qs,
                headers={name.lower(): value for name, value in request.headers.items()},
                body=await request.read(),
            )
        )
        if request.path == "/v1/messages":
            return web.Response(
                status=529,
                body=ANTHROPIC_ERROR,
                headers={"Content-Type": "application/json", "x-backend-shape": "messages"},
            )
        response = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream", "x-backend-shape": "responses"}
        )
        await response.prepare(request)
        for chunk in (TOOL_STREAM[:97], TOOL_STREAM[97:241], TOOL_STREAM[241:]):
            await response.write(chunk)
        await response.write_eof()
        return response


@asynccontextmanager
async def fake_litellm() -> AsyncIterator[FakeLiteLLM]:
    fake = FakeLiteLLM()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", fake.handle)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.port = one(runner.addresses)[1]
    try:
        yield fake
    finally:
        await runner.cleanup()


def add_sandbox(fake: FakeApiServer, name: str, token: str, *, pod_uid: str) -> None:
    fake.put(SANDBOXES_PLURAL, sandbox(name))
    fake.pods[name] = pod_for(fake, name, pod_uid=pod_uid, ip=f"10.0.0.{len(fake.pods) + 10}")
    fake.tokens[token] = TokenVerdict(username=SUBJECT, pod_name=name, pod_uid=pod_uid, audiences=(AUDIENCE,))


def configuration(fake: FakeApiServer) -> k8s_client.Configuration:
    config = k8s_client.Configuration(host=f"http://127.0.0.1:{fake.port}")
    config.api_key = {}
    return config


@asynccontextmanager
async def ingress_clients(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> AsyncIterator[tuple[httpx.AsyncClient, ApiClient]]:
    async with (
        ApiClient(configuration=configuration(kubernetes)) as api,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{backend.port}", timeout=5) as backend_http,
    ):
        resolver = SandboxPrincipalResolver(
            authentication=AuthenticationV1Api(api),
            core_v1=CoreV1Api(api),
            audience=AUDIENCE,
            namespaces=frozenset({SANDBOX_NAMESPACE}),
        )
        app = create_app(
            IngressResources(
                authenticate=SandboxPrincipalAuthenticator(resolver), backend=backend_http, litellm_key=LITELLM_KEY
            )
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ingress.test") as client:
            yield client, api


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def verified_metadata(request: BackendRequest) -> dict[str, str]:
    return cast(dict[str, str], json.loads(request.headers["x-litellm-spend-logs-metadata"]))


@pytest.mark.asyncio
async def test_two_workloads_share_one_backend_key_and_keep_distinct_verified_metadata(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    body = b'{"model":"chatgpt/oai-responses/gpt-5.6-luna","stream":true,"metadata":{"sandbox_name":"forged"}}'
    async with fake_apiserver() as kubernetes, fake_litellm() as backend:
        add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
        add_sandbox(kubernetes, "sandbox-b", TOKEN_B, pod_uid="pod-b-uid")
        async with ingress_clients(kubernetes, backend) as (client, _):
            first = await client.post(
                "/v1/responses?trace=kept",
                content=body,
                headers={
                    **bearer(TOKEN_A),
                    "content-type": "application/json",
                    "x-litellm-spend-logs-metadata": '{"agentplane.sandbox_name":"forged"}',
                    "x-litellm-customer-id": "forged",
                    "x-sandbox-name": "forged",
                    "x-pod-uid": "forged",
                    "x-agent-id": "forged",
                    "x-thread-id": "forged",
                },
            )
            second = await client.post("/v1/responses?trace=kept", content=body, headers=bearer(TOKEN_B))

    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert first.content == second.content == TOOL_STREAM
    assert first.headers["content-type"] == "text/event-stream"
    assert first.headers["x-backend-shape"] == "responses"
    assert len(backend.requests) == 2
    assert {request.headers["authorization"] for request in backend.requests} == {f"Bearer {LITELLM_KEY}"}
    assert all(request.path_qs == "/v1/responses?trace=kept" for request in backend.requests)
    assert all(request.body == body for request in backend.requests), "provider-native request bodies pass unchanged"
    assert verified_metadata(backend.requests[0]) == {
        "agentplane.namespace": SANDBOX_NAMESPACE,
        "agentplane.pod_name": "sandbox-a",
        "agentplane.pod_uid": "pod-a-uid",
        "agentplane.sandbox_name": "sandbox-a",
        "agentplane.sandbox_uid": kubernetes.objects[SANDBOXES_PLURAL]["sandbox-a"]["metadata"]["uid"],
        "agentplane.service_account": "agentplane-runner",
        "agentplane.service_account_subject": SUBJECT,
    }
    assert verified_metadata(backend.requests[1])["agentplane.sandbox_name"] == "sandbox-b"
    assert (
        verified_metadata(backend.requests[1])["agentplane.sandbox_uid"]
        != verified_metadata(backend.requests[0])["agentplane.sandbox_uid"]
    )
    for forged_header in ("x-litellm-customer-id", "x-sandbox-name", "x-pod-uid", "x-agent-id", "x-thread-id"):
        assert forged_header not in backend.requests[0].headers
    transcript = caplog.text + first.text + second.text
    assert TOKEN_A not in transcript
    assert TOKEN_B not in transcript
    assert LITELLM_KEY not in transcript


@pytest.mark.asyncio
async def test_anthropic_status_error_body_and_request_shape_pass_unchanged() -> None:
    body = b'{"model":"anthropic-api/ant-messages/claude-haiku-4-5-20251001","max_tokens":32,"messages":[]}'
    async with fake_apiserver() as kubernetes, fake_litellm() as backend:
        add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
        async with ingress_clients(kubernetes, backend) as (client, _):
            response = await client.post("/v1/messages", content=body, headers=bearer(TOKEN_A))

    assert response.status_code == 529
    assert response.content == ANTHROPIC_ERROR
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-backend-shape"] == "messages"
    assert backend.requests[0].body == body
    assert backend.requests[0].headers["authorization"] == f"Bearer {LITELLM_KEY}"


@pytest.mark.asyncio
async def test_invalid_stale_replaced_and_unowned_bearers_fail_before_backend(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    async with fake_apiserver() as kubernetes, fake_litellm() as backend:
        add_sandbox(kubernetes, "live", TOKEN_A, pod_uid="live-pod-uid")
        kubernetes.tokens[WRONG_AUDIENCE_TOKEN] = TokenVerdict(
            username=SUBJECT, pod_name="live", pod_uid="live-pod-uid", audiences=("somewhere-else",)
        )
        kubernetes.tokens[STALE_TOKEN] = TokenVerdict(
            username=SUBJECT, pod_name="deleted", pod_uid="deleted-pod-uid", audiences=(AUDIENCE,)
        )
        kubernetes.tokens[REPLACED_TOKEN] = TokenVerdict(
            username=SUBJECT, pod_name="live", pod_uid="old-pod-uid", audiences=(AUDIENCE,)
        )
        add_sandbox(kubernetes, "unowned", UNOWNED_TOKEN, pod_uid="unowned-pod-uid")
        kubernetes.pods["unowned"]["metadata"]["ownerReferences"] = []
        async with ingress_clients(kubernetes, backend) as (client, _):
            attempts = [
                await client.post("/v1/messages", content=b"{}"),
                await client.post("/v1/messages", content=b"{}", headers={"Authorization": "not-a-bearer"}),
                await client.post("/v1/messages", content=b"{}", headers=bearer("unknown-token")),
                await client.post("/v1/messages", content=b"{}", headers=bearer(WRONG_AUDIENCE_TOKEN)),
                await client.post("/v1/messages", content=b"{}", headers=bearer(STALE_TOKEN)),
                await client.post("/v1/messages", content=b"{}", headers=bearer(REPLACED_TOKEN)),
                await client.post("/v1/messages", content=b"{}", headers=bearer(UNOWNED_TOKEN)),
            ]

    assert not backend.requests
    assert {(response.status_code, response.json()["detail"]) for response in attempts} == {
        (401, "invalid workload bearer")
    }
    transcript = caplog.text + "".join(response.text for response in attempts)
    for credential in (TOKEN_A, WRONG_AUDIENCE_TOKEN, STALE_TOKEN, REPLACED_TOKEN, UNOWNED_TOKEN, LITELLM_KEY):
        assert credential not in transcript


if __name__ == "__main__":
    pytest_bazel.main()
