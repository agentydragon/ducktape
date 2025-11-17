from __future__ import annotations

from fastmcp.exceptions import ToolError
import pytest

from adgn.mcp._shared.constants import COMPOSITOR_META_SERVER_NAME
from adgn.mcp.compositor.clients import CompositorAdminClient, CompositorMetaClient


async def test_admin_cannot_detach_pinned_server(make_pg_compositor, approval_policy_reader_stub):
    # make_pg_compositor mounts compositor_meta and compositor_admin pinned by default
    async with make_pg_compositor({"approval_policy": approval_policy_reader_stub}) as (sess, _comp):
        admin = CompositorAdminClient(sess)
        meta = CompositorMetaClient(sess)

        # Ensure compositor_meta is visible among mounts (via meta state resources)
        states_before = await meta.list_states()
        assert COMPOSITOR_META_SERVER_NAME in states_before

        # Attempt to detach the pinned meta server via the admin tool should error
        with pytest.raises(ToolError):
            await admin.detach_server(name=COMPOSITOR_META_SERVER_NAME)

        # Still present after failed detach
        states_after = await meta.list_states()
        assert COMPOSITOR_META_SERVER_NAME in states_after
