from __future__ import annotations

import pytest_bazel

from mcp_infra.resource_utils import read_text_json_typed
from mcp_infra.snapshots import RunningServerEntry, ServerEntry

_BACKEND = "backend"


async def test_meta_presents_inproc_mounts(make_compositor, make_simple_mcp):
    """Test compositor_meta server presents inproc mounts."""
    async with make_compositor({_BACKEND: make_simple_mcp}) as (sess, comp):
        # Get meta server from compositor
        meta_server = comp.compositor_meta.server
        # First, check that the discovery resource exists and lists mounted servers
        # Note: The URI gets prefixed with the mount name (compositor_meta)
        discovery_uri = comp.compositor_meta.add_resource_prefix(meta_server.servers_list_resource.uri)

        # Read the discovery resource using the typed helper
        servers: list[str] = await read_text_json_typed(sess, discovery_uri, list[str])

        # Should list at least the backend server we mounted
        assert isinstance(servers, list)
        assert _BACKEND in servers

        # Now read an individual server's state using the template pattern
        # Note: The URI gets prefixed with the mount name (compositor_meta)
        backend_state_uri = comp.compositor_meta.add_resource_prefix(
            meta_server.server_state_resource.uri_template.format(server=_BACKEND)
        )

        # Read state using the typed helper
        entry: ServerEntry = await read_text_json_typed(sess, backend_state_uri, ServerEntry)

        # In-proc mounts should be running when read via compositor_meta
        assert isinstance(entry, RunningServerEntry)
        assert entry.initialize is not None
        assert isinstance(entry.tools, list)


if __name__ == "__main__":
    pytest_bazel.main()
