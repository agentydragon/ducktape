from __future__ import annotations

from collections.abc import Callable

import pytest

from adgn.mcp._shared.constants import POLICY_EVALUATOR_ERROR_MSG
from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.approval_policy.server import ApprovalPolicyServer

BAD_POLICY_SRC: str


## Removed: template-based seatbelt tests. Seatbelt now accepts only explicit policy.


@pytest.mark.requires_docker
async def test_container_timeout_causes_deny_abort(
    monkeypatch: pytest.MonkeyPatch,
    policy_fetch: Callable[[str], str],
    make_pg_compositor,
    make_policy_engine,
    backend_server,
):
    # Force short timeout to trigger evaluator timeout
    monkeypatch.setenv("ADGN_POLICY_EVAL_TIMEOUT_SECS", "0.1")
    # Policy that sleeps (exceeds timeout)
    sleepy_policy = policy_fetch("sleepy_timeout")
    engine = make_policy_engine(sleepy_policy)

    # Reader server
    reader = ApprovalPolicyServer(engine)

    async with make_pg_compositor({"backend": backend_server, "approval_policy": reader}) as (sess, _):
        # High-level client surfaces ToolError with message only; assert the canonical message
        with pytest.raises(Exception, match=POLICY_EVALUATOR_ERROR_MSG) as ei:
            await sess.call_tool(build_mcp_function("backend", "echo"), {"text": "timeout"})
        assert POLICY_EVALUATOR_ERROR_MSG in str(ei.value)
