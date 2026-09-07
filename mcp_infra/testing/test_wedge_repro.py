"""Regression test for issue #5786: does a mid-flight cancellation still wedge `/mcp`?

Answers the issue's "confirm mcp-sdk v2 actually fixes this" sub-task -- narrowly. The
`Client(spec.url)` call below uses no `mode=` override, so it defaults to `mode="auto"`,
which negotiates the modern 2026-07-28 era against the fastmcp v4 server `wedge_repro`
serves (see mcp.client._probe.negotiate_auto). The modern era's per-request task group
(mcp/server/_streamable_http_modern.py) structurally cannot exhibit the bug this guards
against, so a green run here proves only that: a connection that negotiates the modern
protocol survives a mid-flight cancellation. It says nothing about the legacy path
(mode="legacy", or any client that doesn't negotiate 2026-07-28) -- that path still runs
through the same shared-task-group `StreamableHTTPSessionManager` code as mcp-sdk v1, with
the identical `except Exception` that doesn't catch `CancelledError` (confirmed by reading
mcp/server/streamable_http_manager.py in both SDK versions directly, not from docs), and
remains exposed by design. See wedge_repro.py's own docstring for why this harness isn't
used to attempt reproducing that: the same clean, same-process, loopback cancellation
wasn't reliably reproducible against the legacy handler in ad hoc testing either, so a
timing-based attempt to assert the negative here would only be flaky, not evidence.
"""

from __future__ import annotations

import pytest_bazel

from mcp_infra.testing.wedge_repro import cancel_request_mid_flight_then_retry, make_wedge_repro_server


async def test_follow_up_request_succeeds_after_mid_flight_cancellation() -> None:
    server, slow_call_started = make_wedge_repro_server()
    result = await cancel_request_mid_flight_then_retry(server, slow_call_started)
    assert not result.is_error
    assert result.data == "pong"


if __name__ == "__main__":
    pytest_bazel.main()
