from __future__ import annotations

from typing import Any

import pytest_bazel
from mcp import types as mcp_types
from pydantic import TypeAdapter
from pydantic.networks import AnyUrl

from mcp_infra.compositor.meta_server import _SERVER_STATE_URI_TEMPLATE
from mcp_infra.snapshots import RunningServerEntry, ServerEntry

_BACKEND = "backend"
_SERVERS_LIST_URI = "compositor://servers"


async def _read_text_json(client: Any, uri: AnyUrl | str, model: Any) -> Any:
    uri_obj = AnyUrl(uri) if isinstance(uri, str) else uri
    contents = await client.read_resource(uri_obj)
    if len(contents) != 1:
        raise AssertionError(f"expected one resource part, got {len(contents)}")
    content = contents[0]
    if not isinstance(content, mcp_types.TextResourceContents):
        raise AssertionError(f"expected TextResourceContents, got {type(content).__name__}")
    return TypeAdapter(model).validate_json(content.text)


async def test_meta_discovery_lists_mounted_servers(make_compositor, make_simple_mcp):
    """Test compositor_meta discovery resource lists mounted servers."""
    async with make_compositor({_BACKEND: make_simple_mcp}) as (sess, comp):
        discovery_uri = comp.compositor_meta.add_resource_prefix(_SERVERS_LIST_URI)

        servers: list[str] = await _read_text_json(sess, discovery_uri, list[str])

        assert isinstance(servers, list)
        assert _BACKEND in servers


async def test_meta_presents_inproc_mount_state(make_compositor, make_simple_mcp):
    """Test compositor_meta per-server state for in-process mounts."""
    async with make_compositor({_BACKEND: make_simple_mcp}) as (sess, comp):
        backend_state_uri = comp.compositor_meta.add_resource_prefix(_SERVER_STATE_URI_TEMPLATE.format(server=_BACKEND))

        entry: ServerEntry = await _read_text_json(sess, backend_state_uri, ServerEntry)

        assert isinstance(entry, RunningServerEntry)
        assert entry.initialize is not None
        assert isinstance(entry.tools, list)


if __name__ == "__main__":
    pytest_bazel.main()
