from __future__ import annotations

from pydantic import TypeAdapter
import pytest

from adgn.mcp._shared.constants import COMPOSITOR_META_STATE_URI_FMT
from adgn.mcp._shared.resources import extract_single_text_content
from adgn.mcp.snapshots import RunningServerEntry, ServerEntry


@pytest.mark.requires_docker
async def test_compositor_meta_resources_available(
    make_pg_compositor, approval_policy_reader_allow_all, backend_server
):
    async with make_pg_compositor({"backend": backend_server, "approval_policy": approval_policy_reader_allow_all}) as (
        sess,
        _comp,
    ):
        # Read per-mount state from the compositor_meta server
        # Expect running or initializing depending on initialization timing
        rr = await sess.session.read_resource(COMPOSITOR_META_STATE_URI_FMT.format(server="backend"))
        s = extract_single_text_content(rr)
        entry: ServerEntry = TypeAdapter(ServerEntry).validate_json(s)
        assert isinstance(entry, RunningServerEntry)
