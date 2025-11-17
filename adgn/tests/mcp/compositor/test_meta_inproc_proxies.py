from __future__ import annotations

from fastmcp.server import FastMCP
from pydantic import TypeAdapter

from adgn.mcp._shared.constants import COMPOSITOR_META_STATE_URI_FMT
from adgn.mcp._shared.resources import extract_single_text_content
from adgn.mcp.snapshots import RunningServerEntry, ServerEntry


def _make_backend(name: str = "backend") -> FastMCP:
    m = FastMCP(name)

    @m.tool(name="ping")
    def ping() -> str:
        return "pong"

    # No resources declared; meta should still expose state/instructions/capabilities
    return m


async def test_meta_presents_inproc_mounts(make_pg_compositor, approval_policy_reader_allow_all):
    backend = _make_backend()
    async with make_pg_compositor({"backend": backend, "approval_policy": approval_policy_reader_allow_all}) as (
        sess,
        _comp,
    ):
        # List resources and find compositor_meta state entry for this mount
        resources = await sess.list_resources()
        uris = {str(r.uri) for r in resources}
        # FastMCP prefixes the path with the server name when mounted under a compositor
        assert COMPOSITOR_META_STATE_URI_FMT.format(server="backend") in uris

        # Read state via helper and validate JSON has a discriminator
        rr = await sess.session.read_resource(COMPOSITOR_META_STATE_URI_FMT.format(server="backend"))
        s = extract_single_text_content(rr)
        entry: ServerEntry = TypeAdapter(ServerEntry).validate_json(s)
        # In-proc mounts should be running when read via compositor_meta
        assert isinstance(entry, RunningServerEntry)
        assert entry.initialize is not None
        assert isinstance(entry.tools, list)
