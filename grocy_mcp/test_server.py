"""Smoke test: `build_mcp` parses the fixed Grocy spec without raising."""

from __future__ import annotations

import asyncio

import httpx
import pytest_bazel
from prometheus_client import REGISTRY

from grocy_mcp.mcp_types import ServerSettings
from grocy_mcp.server import build_mcp, record_tool_count


def test_build_mcp_accepts_grocy_spec() -> None:
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    client = httpx.AsyncClient(base_url=f"{settings.grocy_url}/api")
    build_mcp(settings, client=client)


def test_record_tool_count_exports_metric() -> None:
    async def _run() -> int:
        settings = ServerSettings(grocy_url="https://grocy.example.com")
        async with httpx.AsyncClient(base_url=f"{settings.grocy_url}/api") as client:
            mcp = build_mcp(settings, client=client)
            return await record_tool_count(mcp)

    tool_count = asyncio.run(_run())

    assert tool_count > 0
    assert REGISTRY.get_sample_value("grocy_mcp_tools") == tool_count


if __name__ == "__main__":
    pytest_bazel.main()
