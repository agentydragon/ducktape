from __future__ import annotations

import pytest_bazel
from fastmcp import FastMCP

from airlock.conftest import as_remote_server
from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from mcp_infra.oauth_facade.config import FacadeSettings, HttpUpstream
from mcp_infra.oauth_facade.upstream_probe import ProbeState, _probe_once


def _settings(downstream_url: str) -> FacadeSettings:
    return FacadeSettings(
        auth=AuthentikAuthConfig(
            oidc_issuer="https://auth.example.com/application/o/test/",
            oidc_client_id="id",
            oidc_client_secret="secret",
            public_base_url="https://test.example.com",
        ),
        upstream=HttpUpstream(url=downstream_url),
        facade_name="Test Facade",
    )


async def test_probe_counts_upstream_tools() -> None:
    downstream = FastMCP("downstream")

    @downstream.tool
    async def echo(text: str) -> str:
        return text

    @downstream.tool
    async def ping() -> str:
        return "pong"

    async with as_remote_server(downstream) as remote:
        assert await _probe_once(_settings(remote.url)) == 2


def test_ready_requires_recent_success_with_tools() -> None:
    state = ProbeState(facade_name="Test Facade", max_staleness_seconds=195.0)
    # No probe has run yet.
    assert state.ready() is False

    # A success with tools makes it ready.
    state.record_success(3)
    assert state.ready() is True
    assert state.last_success_tools == 3

    # A success with zero tools (upstream reachable but exposing nothing) is not ready.
    state.record_success(0)
    assert state.ready() is False


def test_failure_does_not_immediately_flip_ready() -> None:
    # Readiness is staleness-based: one transient probe failure right after a
    # success must not flap the pod NotReady (the staleness window debounces it).
    state = ProbeState(facade_name="Test Facade", max_staleness_seconds=195.0)
    state.record_success(2)
    state.record_failure()
    assert state.ready() is True


def test_ready_expires_when_probes_go_stale() -> None:
    # If probes stop succeeding, readiness lapses once the last success ages out.
    state = ProbeState(facade_name="Test Facade", max_staleness_seconds=0.0)
    state.record_success(2)
    assert state.ready() is False


if __name__ == "__main__":
    pytest_bazel.main()
