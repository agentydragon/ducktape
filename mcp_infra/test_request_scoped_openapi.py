"""Compatibility tests for request-scoped FastMCP OpenAPI clients."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import Any

import httpx
import pytest
import pytest_bazel
from fastmcp.server.providers.openapi import OpenAPIProvider, OpenAPITool

from mcp_infra.request_scoped_openapi import RequestScopedOpenAPIClients

_SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "request-scoped-client-test", "version": "1"},
    "servers": [{"url": "https://placeholder.invalid"}],
    "paths": {
        "/echo": {
            "get": {
                "operationId": "echo",
                "parameters": [
                    {
                        "name": "value",
                        "in": "query",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Value returned by the test backend.",
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Echo response",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"value": {"type": "string"}},
                                    "required": ["value"],
                                }
                            }
                        },
                    }
                },
            }
        }
    },
}


@pytest.fixture
async def placeholder_client() -> AsyncIterator[httpx.AsyncClient]:
    async def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"provider-level client was used for {request.url}")

    async with httpx.AsyncClient(
        base_url="https://placeholder.invalid", transport=httpx.MockTransport(unexpected_request)
    ) as client:
        yield client


@pytest.fixture
def echo_provider(placeholder_client: httpx.AsyncClient) -> OpenAPIProvider:
    return OpenAPIProvider(_SPEC, client=placeholder_client)


async def test_preserves_schema_filters_injected_argument_and_closes_each_client(
    echo_provider: OpenAPIProvider, placeholder_client: httpx.AsyncClient
) -> None:
    requests: list[tuple[str, str | None]] = []
    entered: list[httpx.AsyncClient] = []
    exited: list[httpx.AsyncClient] = []

    async def backend(request: httpx.Request) -> httpx.Response:
        value = request.url.params["value"]
        requests.append((value, request.headers.get("authorization")))
        return httpx.Response(200, json={"value": value})

    @asynccontextmanager
    async def trusted_client() -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            base_url="https://backend.invalid",
            headers={"Authorization": "Bearer trusted"},
            transport=httpx.MockTransport(backend),
        ) as client:
            entered.append(client)
            try:
                yield client
            finally:
                exited.append(client)

    parent = await echo_provider.get_tool("echo")
    assert isinstance(parent, OpenAPITool)
    original_schema = parent.parameters

    echo_provider.add_transform(RequestScopedOpenAPIClients(trusted_client))
    listed = await echo_provider.list_tools()
    wrapped = await echo_provider.get_tool("echo")
    assert wrapped is listed[0]
    assert wrapped is not None
    assert wrapped.parameters == original_schema
    assert wrapped.parameters is not original_schema

    first = await wrapped.run({"value": "one", "_fastmcp_request_scoped_http_client": "attacker-controlled"})
    second = await wrapped.run({"value": "two"})

    assert first.structured_content == {"value": "one"}
    assert second.structured_content == {"value": "two"}
    assert requests == [("one", "Bearer trusted"), ("two", "Bearer trusted")]
    assert len(entered) == 2
    assert exited == entered
    assert entered[0] is not entered[1]
    assert all(client.is_closed for client in exited)
    assert parent._client is placeholder_client


async def test_rejects_openapi_parameter_that_collides_with_injected_client(
    placeholder_client: httpx.AsyncClient,
) -> None:
    spec = deepcopy(_SPEC)
    spec["paths"]["/echo"]["get"]["parameters"].append(
        {"name": "_fastmcp_request_scoped_http_client", "in": "query", "schema": {"type": "string"}}
    )
    provider = OpenAPIProvider(spec, client=placeholder_client)

    @asynccontextmanager
    async def client_provider() -> AsyncIterator[httpx.AsyncClient]:
        yield placeholder_client

    provider.add_transform(RequestScopedOpenAPIClients(client_provider))
    with pytest.raises(ValueError, match="uses reserved parameter"):
        await provider.get_tool("echo")


async def test_concurrent_calls_keep_request_clients_isolated(
    echo_provider: OpenAPIProvider, placeholder_client: httpx.AsyncClient
) -> None:
    current_bearer: ContextVar[str] = ContextVar("current_bearer")
    both_requests_started = asyncio.Event()
    observed: list[tuple[str, str | None]] = []
    entered: list[str] = []
    exited: list[str] = []

    async def backend(request: httpx.Request) -> httpx.Response:
        value = request.url.params["value"]
        authorization = request.headers.get("authorization")
        observed.append((value, authorization))
        if len(observed) == 2:
            both_requests_started.set()
        await both_requests_started.wait()
        return httpx.Response(200, json={"value": value})

    @asynccontextmanager
    async def per_call_client() -> AsyncIterator[httpx.AsyncClient]:
        bearer = current_bearer.get()
        entered.append(bearer)
        async with httpx.AsyncClient(
            base_url="https://backend.invalid",
            headers={"Authorization": f"Bearer {bearer}"},
            transport=httpx.MockTransport(backend),
        ) as client:
            try:
                yield client
            finally:
                exited.append(bearer)

    parent = await echo_provider.get_tool("echo")
    assert isinstance(parent, OpenAPITool)
    echo_provider.add_transform(RequestScopedOpenAPIClients(per_call_client))
    wrapped = await echo_provider.get_tool("echo")
    assert wrapped is not None

    async def call(value: str, bearer: str) -> dict[str, Any] | None:
        token = current_bearer.set(bearer)
        try:
            return (await wrapped.run({"value": value})).structured_content
        finally:
            current_bearer.reset(token)

    results = await asyncio.gather(call("alpha", "operator-alpha"), call("beta", "operator-beta"))

    # typeshed models a two-argument gather as returning a tuple so it can type the elements; the
    # runtime value is a list, and comparing the two shapes is what strict_equality objects to.
    assert list(results) == [{"value": "alpha"}, {"value": "beta"}]
    assert sorted(observed) == [("alpha", "Bearer operator-alpha"), ("beta", "Bearer operator-beta")]
    assert sorted(entered) == ["operator-alpha", "operator-beta"]
    assert sorted(exited) == ["operator-alpha", "operator-beta"]
    assert parent._client is placeholder_client


if __name__ == "__main__":
    pytest_bazel.main()
