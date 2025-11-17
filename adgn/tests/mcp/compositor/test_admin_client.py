from __future__ import annotations

from fastmcp.server import FastMCP

from adgn.mcp.compositor.clients import CompositorAdminClient, CompositorMetaClient


def _make_backend(name: str = "backend") -> FastMCP:
    m = FastMCP(name)

    @m.tool(name="ping")
    def ping() -> str:
        return "pong"

    return m


async def test_admin_client_list_and_detach(make_pg_compositor, approval_policy_reader_allow_all):
    backend = _make_backend()
    async with make_pg_compositor({"backend": backend, "approval_policy": approval_policy_reader_allow_all}) as (
        sess,
        comp,
    ):
        admin = CompositorAdminClient(sess)
        meta = CompositorMetaClient(sess)
        states = await meta.list_states()
        assert "backend" in states
        # Detach via admin client (allowed for in-proc proxies)
        await admin.detach_server(name="backend")
        states_after = await meta.list_states()
        assert "backend" not in states_after
        # Re-attach in-proc directly to leave compositor in a sane state for any following assertions
        await comp.mount_inproc("backend", backend)
