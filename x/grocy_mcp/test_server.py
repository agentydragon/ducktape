"""Smoke test: `build_mcp` parses the fixed Grocy spec without raising."""

from __future__ import annotations

import httpx
import pytest_bazel

from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.server import build_mcp


def test_build_mcp_accepts_grocy_spec() -> None:
    settings = ServerSettings(grocy_url="https://grocy.example.com")
    client = httpx.AsyncClient(base_url=f"{settings.grocy_url}/api")
    build_mcp(settings, client=client)


if __name__ == "__main__":
    pytest_bazel.main()
