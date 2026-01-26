"""Shared test fixtures for claude_hooks tests.

Import and use in test files - Bazel doesn't do conftest.py auto-discovery.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools.claude_hooks.testing.mock_anthropic_proxy import MockAnthropicProxy, UpstreamProxyConfig


@dataclass
class MockProxyFixture:
    """Container for mock proxy and its associated log file."""

    proxy: MockAnthropicProxy
    log_file: Path


@pytest.fixture(scope="module")
def mock_anthropic_proxy() -> Generator[MockProxyFixture]:
    """Mock of Anthropic's TLS-inspecting proxy that chains through upstream if available.

    Works in gVisor environments by detecting HTTPS_PROXY and chaining through it.
    Configures file logging for debugging proxy behavior in CI.

    Yields a MockProxyFixture with both the proxy and its log file path.
    """
    outputs_dir = Path(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR", "/tmp/test-outputs"))
    outputs_dir.mkdir(parents=True, exist_ok=True)
    log_file = outputs_dir / "mock-anthropic-proxy.log"

    proxy_logger = logging.getLogger("tools.claude_hooks.testing.mock_anthropic_proxy")
    proxy_logger.setLevel(logging.DEBUG)

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    handler.setLevel(logging.DEBUG)
    proxy_logger.addHandler(handler)

    try:
        with MockAnthropicProxy(
            listen_port=0,
            require_auth=True,
            username="proxy_user",
            password="test_jwt_token",
            upstream_proxy=UpstreamProxyConfig.from_env(),
        ) as proxy:
            yield MockProxyFixture(proxy=proxy, log_file=log_file)
    finally:
        handler.close()
        proxy_logger.removeHandler(handler)
