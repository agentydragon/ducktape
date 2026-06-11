"""Shared fixtures for postscanmail_mcp_server tests.

`mcp_client` yields a FastMCP in-process Client wired to a freshly built
server, so each test gets isolated state and a real OAuth-free call path.

`respx_router` intercepts every outbound HTTP call to PostScan Mail and
asserts that all mocked routes were exercised, surfacing tests that mock
URLs they never hit.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import respx
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from x.postscanmail_mcp_server.server import BASE_URL, build_client, build_mcp

TEST_API_KEY = "test-key"


@pytest.fixture
async def mcp_client() -> AsyncGenerator[Client]:
    mcp = build_mcp(build_client(TEST_API_KEY))
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def respx_router() -> AsyncGenerator[respx.Router]:
    async with respx.mock(base_url=BASE_URL, assert_all_called=True) as router:
        yield router
