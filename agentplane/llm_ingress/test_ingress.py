"""End-to-end ingress proof: real HTTP, fake Kubernetes, and a byte-streaming fake LiteLLM."""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from uuid import uuid4

import httpx
import pytest
import pytest_bazel
import yaml
from aiohttp import web
from more_itertools import one

from agentplane.llm_ingress.models import ModelConfig
from agentplane.sandbox_resource import SANDBOXES_PLURAL
from agentplane.testing.fake_apiserver import (
    SANDBOX_NAMESPACE,
    FakeApiServer,
    TokenVerdict,
    fake_apiserver,
    pod_for,
    sandbox,
)
from util.bazel.runfiles import get_required_path, own_repo_rlocation
from util.net import async_wait_for_port, pick_free_port
from util.testing.undeclared_outputs import undeclared_outputs_dir

AUDIENCE = "llm-ingress-test-audience"
SUBJECT = f"system:serviceaccount:{SANDBOX_NAMESPACE}:sandbox-a"
TOKEN_A = "opaque-workload-token-a"
TOKEN_B = "opaque-workload-token-b"
WRONG_AUDIENCE_TOKEN = "opaque-wrong-audience-token"
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
    stream_started: asyncio.Event = field(default_factory=asyncio.Event)
    continue_stream: asyncio.Event = field(default_factory=asyncio.Event)
    stream_cancelled: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.requests.append(
            BackendRequest(
                method=request.method,
                path_qs=request.path_qs,
                headers={name.lower(): value for name, value in request.headers.items()},
                body=await request.read(),
            )
        )
        if request.path == "/compressed":
            return web.Response(
                body=gzip.compress(TOOL_STREAM),
                headers=[
                    ("Content-Encoding", "gzip"),
                    ("Set-Cookie", "first=1"),
                    ("Set-Cookie", "second=2"),
                    ("Connection", "x-drop"),
                    ("x-drop", "hop-only"),
                    ("Authorization", f"Bearer {LITELLM_KEY}"),
                ],
            )
        if request.path == "/gated-stream":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"first\n")
            self.stream_started.set()
            try:
                await self.continue_stream.wait()
                await response.write(b"second\n")
                await response.write_eof()
            except asyncio.CancelledError:
                self.stream_cancelled.set()
                raise
            return response
        if request.path == "/truncated":
            response = web.StreamResponse(headers={"Content-Length": "100"})
            response.force_close()
            await response.prepare(request)
            await response.write(b"partial")
            await response.write_eof()
            return response
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


@asynccontextmanager
async def ingress_clients(
    kubernetes: FakeApiServer, backend: FakeLiteLLM, *, models: tuple[ModelConfig, ...] = ()
) -> AsyncIterator[httpx.AsyncClient]:
    binary = get_required_path(own_repo_rlocation("agentplane/llm_ingress/go/server_/server"))
    port = pick_free_port()
    log_path = undeclared_outputs_dir() / f"ingress-{uuid4().hex}.log"
    with TemporaryDirectory() as directory, log_path.open("wb") as logs:
        config_dir = Path(directory)
        kubeconfig = config_dir / "kubeconfig.yaml"
        kubeconfig.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "v1",
                    "kind": "Config",
                    "clusters": [{"name": "test", "cluster": {"server": f"http://127.0.0.1:{kubernetes.port}"}}],
                    "contexts": [{"name": "test", "context": {"cluster": "test", "user": "test"}}],
                    "current-context": "test",
                    "users": [{"name": "test", "user": {}}],
                }
            )
        )
        settings = config_dir / "settings.yaml"
        settings.write_text(
            yaml.safe_dump(
                {
                    "allowed_service_account_namespaces": [SANDBOX_NAMESPACE],
                    "models": [model.model_dump(mode="json") for model in models],
                    "log_levels": {"root": "INFO"},
                }
            )
        )
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "--host=127.0.0.1",
            f"--port={port}",
            f"--kubeconfig={kubeconfig}",
            f"--token-audience={AUDIENCE}",
            f"--litellm-url=http://127.0.0.1:{backend.port}",
            env={
                "AGENTPLANE_LLM_INGRESS_CONFIG_FILE": str(settings),
                "AGENTPLANE_LLM_INGRESS_LITELLM_KEY": LITELLM_KEY,
            },
            stdout=logs,
            stderr=logs,
        )
        try:
            await async_wait_for_port("127.0.0.1", port)
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False) as client:
                health = await client.get("/healthz")
                assert health.status_code == 200
                yield client
        finally:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            assert process.returncode == 0, f"Ingress exited with {process.returncode}; see {log_path}"
    transcript = log_path.read_text()
    for credential in (TOKEN_A, TOKEN_B, WRONG_AUDIENCE_TOKEN, LITELLM_KEY):
        assert credential not in transcript, f"Credential leaked into ingress logs; see {log_path}"


@pytest.fixture
async def kubernetes() -> AsyncIterator[FakeApiServer]:
    async with fake_apiserver() as fake:
        yield fake


@pytest.fixture
async def backend() -> AsyncIterator[FakeLiteLLM]:
    async with fake_litellm() as fake:
        yield fake


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def verified_metadata(request: BackendRequest) -> dict[str, str]:
    return cast(dict[str, str], json.loads(request.headers["x-litellm-spend-logs-metadata"]))


async def test_two_workloads_share_one_backend_key_and_keep_distinct_verified_metadata(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    body = b'{"model":"chatgpt/oai-responses/gpt-5.6-luna","stream":true,"metadata":{"sandbox_name":"forged"}}'
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    add_sandbox(kubernetes, "sandbox-b", TOKEN_B, pod_uid="pod-b-uid")
    async with ingress_clients(kubernetes, backend) as client:
        first = await client.post(
            "/v1/responses?trace=kept",
            content=body,
            headers={
                **bearer(TOKEN_A),
                "content-type": "application/json",
                "x-litellm-spend-logs-metadata": '{"agentplane.sandbox_name":"forged"}',
                "x-litellm-customer-id": "forged",
                "x-api-key": "forged",
                "x-litellm-api-key": "forged",
                "x-agentplane-pod-uid": "forged",
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
        "agentplane.service_account": "sandbox-a",
    }
    assert verified_metadata(backend.requests[1])["agentplane.pod_name"] == "sandbox-b"
    assert (
        verified_metadata(backend.requests[1])["agentplane.pod_uid"]
        != verified_metadata(backend.requests[0])["agentplane.pod_uid"]
    )
    for forged_header in (
        "x-litellm-customer-id",
        "x-api-key",
        "x-litellm-api-key",
        "x-agentplane-pod-uid",
        "x-sandbox-name",
        "x-pod-uid",
        "x-agent-id",
        "x-thread-id",
    ):
        assert forged_header not in backend.requests[0].headers
    transcript = first.text + second.text
    assert TOKEN_A not in transcript
    assert TOKEN_B not in transcript
    assert LITELLM_KEY not in transcript


async def test_anthropic_status_error_body_and_request_shape_pass_unchanged(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    body = b'{"model":"anthropic-api/ant-messages/claude-haiku-4-5-20251001","max_tokens":32,"messages":[]}'
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    async with ingress_clients(kubernetes, backend) as client:
        response = await client.post("/v1/messages", content=body, headers=bearer(TOKEN_A))

    assert response.status_code == 529
    assert response.content == ANTHROPIC_ERROR
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-backend-shape"] == "messages"
    assert backend.requests[0].body == body
    assert backend.requests[0].headers["authorization"] == f"Bearer {LITELLM_KEY}"


async def test_missing_malformed_unknown_and_wrong_audience_bearers_fail_before_backend(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    """Everything the TokenReview itself settles. A Pod that is gone or replaced is settled there
    too -- the API server validates the object the token is bound to -- and is not re-checked here."""
    add_sandbox(kubernetes, "live", TOKEN_A, pod_uid="live-pod-uid")
    kubernetes.tokens[WRONG_AUDIENCE_TOKEN] = TokenVerdict(
        username=SUBJECT, pod_name="live", pod_uid="live-pod-uid", audiences=("somewhere-else",)
    )
    async with ingress_clients(kubernetes, backend) as client:
        attempts = [
            await client.post("/v1/messages", content=b"{}"),
            await client.post("/v1/messages", content=b"{}", headers={"Authorization": "not-a-bearer"}),
            await client.post("/v1/messages", content=b"{}", headers=bearer("unknown-token")),
            await client.post("/v1/messages", content=b"{}", headers=bearer(WRONG_AUDIENCE_TOKEN)),
        ]

    assert not backend.requests
    assert {(response.status_code, response.json()["detail"]) for response in attempts} == {
        (401, "invalid workload bearer")
    }
    transcript = "".join(response.text for response in attempts)
    for credential in (TOKEN_A, WRONG_AUDIENCE_TOKEN, LITELLM_KEY):
        assert credential not in transcript


async def test_a_workload_no_sandbox_owns_is_served_as_its_service_account(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    """The deliberate widening: spend is attributed to the account, and nothing follows an owner."""
    add_sandbox(kubernetes, "unowned", UNOWNED_TOKEN, pod_uid="unowned-pod-uid")
    kubernetes.pods["unowned"]["metadata"]["ownerReferences"] = []
    async with ingress_clients(kubernetes, backend) as client:
        served = await client.post(
            "/v1/responses",
            content=b'{"model":"chatgpt/oai-responses/gpt-5.6-luna","stream":true}',
            headers={**bearer(UNOWNED_TOKEN), "content-type": "application/json"},
        )

    assert served.status_code == 200, served.text
    assert verified_metadata(backend.requests[0])["agentplane.pod_name"] == "unowned"


async def test_model_config_lookup_answers_from_configuration_for_authenticated_workloads(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    model = "ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k"
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    async with ingress_clients(
        kubernetes, backend, models=(ModelConfig(model=model, total_context_budget_tokens=256 * 1024),)
    ) as client:
        found = await client.get("/agentplane/model-config", params={"model": model}, headers=bearer(TOKEN_A))
        missing = await client.get("/agentplane/model-config", params={"model": "unlisted"}, headers=bearer(TOKEN_A))
        unauthenticated = await client.get("/agentplane/model-config", params={"model": model})
        missing_parameter = await client.get("/agentplane/model-config", headers=bearer(TOKEN_A))

    assert found.status_code == 200
    assert found.json() == {"model": model, "total_context_budget_tokens": 256 * 1024}
    assert missing.status_code == 200
    assert missing.json() is None
    assert unauthenticated.status_code == 401
    assert missing_parameter.status_code == 422
    assert not backend.requests, "metadata is answered from generated ingress configuration, not LiteLLM"


async def test_stream_is_incremental_and_client_disconnect_cancels_backend(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    async with ingress_clients(kubernetes, backend) as client:
        async with client.stream("POST", "/gated-stream", headers=bearer(TOKEN_A)) as response:
            assert response.status_code == 200
            chunks = response.aiter_raw()
            assert await anext(chunks) == b"first\n"
            assert backend.stream_started.is_set()
            assert not backend.continue_stream.is_set()
        await asyncio.wait_for(backend.stream_cancelled.wait(), timeout=5)


async def test_compressed_bytes_duplicate_headers_and_hop_header_removal(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    async with (
        ingress_clients(kubernetes, backend) as client,
        client.stream(
            "POST", "/compressed", headers={**bearer(TOKEN_A), "Connection": "x-drop", "x-drop": "hop-only"}
        ) as response,
    ):
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
        assert response.status_code == 200
        assert response.headers["content-encoding"] == "gzip"
        assert gzip.decompress(raw) == TOOL_STREAM
        assert response.headers.get_list("set-cookie") == ["first=1", "second=2"]
        assert "authorization" not in response.headers
        assert "x-drop" not in response.headers
    assert "x-drop" not in backend.requests[0].headers


async def test_truncated_backend_response_does_not_look_like_successful_end_of_stream(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    async with ingress_clients(kubernetes, backend) as client:
        with pytest.raises(httpx.RemoteProtocolError):
            await client.post("/truncated", headers=bearer(TOKEN_A))


async def test_revoked_and_disallowed_namespace_tokens_fail_on_the_next_request(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    kubernetes.tokens[TOKEN_B] = TokenVerdict(
        username="system:serviceaccount:other:sandbox-b", pod_name="sandbox-b", pod_uid="pod-b", audiences=(AUDIENCE,)
    )
    async with ingress_clients(kubernetes, backend) as client:
        assert (await client.post("/v1/messages", headers=bearer(TOKEN_A))).status_code == 529
        del kubernetes.tokens[TOKEN_A]
        for headers in (
            list(bearer(TOKEN_A).items()),
            list(bearer(TOKEN_B).items()),
            [("Authorization", f"Bearer {TOKEN_B}"), ("Authorization", f"Bearer {TOKEN_A}")],
        ):
            refused = await client.post("/v1/messages", headers=headers)
            assert refused.status_code == 401
            assert refused.headers["www-authenticate"] == "Bearer"
    assert len(backend.requests) == 1


async def test_bearer_scheme_is_case_insensitive_and_accepts_multiple_spaces(
    kubernetes: FakeApiServer, backend: FakeLiteLLM
) -> None:
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    async with ingress_clients(kubernetes, backend) as client:
        response = await client.post("/v1/messages", headers={"Authorization": f"bearer   {TOKEN_A}"})
    assert response.status_code == 529
    assert backend.requests[0].headers["authorization"] == f"Bearer {LITELLM_KEY}"


@pytest.mark.parametrize("unavailable", ["kubernetes", "backend"])
async def test_dependency_outage_is_sanitized_and_does_not_forward_workload_credentials(
    kubernetes: FakeApiServer, backend: FakeLiteLLM, unavailable: str
) -> None:
    add_sandbox(kubernetes, "sandbox-a", TOKEN_A, pod_uid="pod-a-uid")
    if unavailable == "kubernetes":
        kubernetes.port = pick_free_port()
    else:
        backend.port = pick_free_port()
    async with ingress_clients(kubernetes, backend) as client:
        response = await client.post("/v1/messages", headers=bearer(TOKEN_A))
    assert response.status_code == (500 if unavailable == "kubernetes" else 502)
    assert not backend.requests
    for credential in (TOKEN_A, LITELLM_KEY):
        assert credential not in response.text


if __name__ == "__main__":
    pytest_bazel.main()
