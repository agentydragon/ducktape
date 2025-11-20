from __future__ import annotations

import pytest
from hamcrest import all_of, assert_that, has_properties, instance_of

from adgn.mcp._shared.naming import build_mcp_function
from adgn.mcp.exec.docker.server import make_container_exec_server
from adgn.mcp.exec.models import BaseExecResult, ExecInput, Exited, TimedOut
from adgn.mcp.stubs.typed_stubs import ToolStub
from tests.conftest import make_container_opts


def _runtime_spec_persession(image: str = "alpine:3.19"):
    return make_container_exec_server(
        make_container_opts(image, ephemeral=False)  # per-session container
    )


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
        stub = ToolStub(sess, build_mcp_function("runtime", "exec"), BaseExecResult)

        res_timeout = await stub(ExecInput(cmd=["sh", "-lc", "sleep 3"], timeout_ms=500, shell=True))
        assert_that(res_timeout.exit, instance_of(TimedOut))

        # Next call should work; container should have been restarted
        res_ok = await stub(ExecInput(cmd=["/bin/echo", "-n", "ok"], timeout_ms=5000, shell=False))
        assert_that(
            res_ok,
            has_properties(exit=all_of(instance_of(Exited), has_properties(exit_code=0)), stdout="ok"),
        )
