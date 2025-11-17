from __future__ import annotations

import pytest

from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.exec.docker.server import make_container_exec_server
from adgn.mcp.exec.models import BaseExecResult, ExecInput, Exited, TimedOut
from adgn.mcp.testing.typed_stubs import call_tool_typed
from tests.conftest import make_container_opts


def _runtime_spec_persession(image: str = "alpine:3.19"):
    return make_container_exec_server(
        make_container_opts(image, ephemeral=False)  # per-session container
    )


async def _run_exec(sess, cmd, timeout_ms: int, shell: bool = True) -> BaseExecResult:
    # Use known Pydantic IO models by name for clarity and stability
    payload = ExecInput(cmd=cmd, timeout_ms=timeout_ms, shell=shell)
    return await call_tool_typed(sess, "exec", payload, BaseExecResult)


@pytest.mark.requires_docker
async def test_runtime_per_session_timeout_then_next_call_ok(
    make_pg_compositor, approval_policy_reader_allow_all
) -> None:
    async with make_pg_compositor(
        {"runtime": _runtime_spec_persession(), "approval_policy": approval_policy_reader_allow_all}
    ) as (mcp_client, _comp):
        # Call via Compositor using namespaced tool
        sess = mcp_client

        # Cause a host-side timeout: sleep longer than timeout_ms
        # Namespaced exec via Compositor
        async def _run_ns(cmd, timeout_ms: int, shell: bool = True):
            return await call_tool_typed(
                sess,
                build_mcp_function("runtime", "exec"),
                ExecInput(cmd=cmd, timeout_ms=timeout_ms, shell=shell),
                BaseExecResult,
            )

        res_timeout = await _run_ns(["sh", "-lc", "sleep 3"], timeout_ms=500, shell=True)
        assert isinstance(res_timeout.exit, TimedOut)

        # Next call should work; container should have been restarted
        res_ok = await _run_ns(["/bin/echo", "-n", "ok"], timeout_ms=5000, shell=False)
        assert isinstance(res_ok.exit, Exited)
        assert res_ok.exit.exit_code == 0
        assert (res_ok.stdout or "") == "ok"
