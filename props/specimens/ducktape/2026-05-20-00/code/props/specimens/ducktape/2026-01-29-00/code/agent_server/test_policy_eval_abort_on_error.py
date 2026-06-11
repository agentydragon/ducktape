from __future__ import annotations

import pytest
import pytest_bazel

from agent_server.mcp.approval_policy.engine import POLICY_EVALUATOR_ERROR_MSG
from agent_server.testing.approval_policy_testdata import fetch_policy
from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix

## Removed: template-based seatbelt tests. Seatbelt now accepts only explicit policy.


@pytest.mark.requires_docker
async def test_container_timeout_causes_deny_abort(
    monkeypatch: pytest.MonkeyPatch, make_policy_gateway_client, make_approval_policy_server, make_simple_mcp
):
    # Force short timeout to trigger evaluator timeout
    monkeypatch.setenv("ADGN_POLICY_EVAL_TIMEOUT_SECS", "0.1")
    # Policy that sleeps (exceeds timeout)
    sleepy_policy = fetch_policy("sleepy_timeout")
    engine = await make_approval_policy_server(sleepy_policy)

    # Reader server
    reader = engine.reader

    async with make_policy_gateway_client({"backend": make_simple_mcp, "approval_policy": reader}) as sess:
        # High-level client surfaces ToolError with message only; assert the canonical message
        with pytest.raises(Exception, match=POLICY_EVALUATOR_ERROR_MSG) as ei:
            await sess.call_tool(build_mcp_function(MCPMountPrefix("backend"), "echo"), {"text": "timeout"})
        assert POLICY_EVALUATOR_ERROR_MSG in str(ei.value)


if __name__ == "__main__":
    pytest_bazel.main()
