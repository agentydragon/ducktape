from __future__ import annotations

from fastmcp.server import FastMCP
import pytest

from adgn.mcp.compositor.server import Compositor


def _backend(name: str = "backend") -> FastMCP:
    m = FastMCP(name)

    @m.tool(name="ping")
    def ping() -> str:
        return "pong"

    return m


async def test_unmount_pinned_server_errors_and_kept():
    comp = Compositor("comp")
    srv = _backend()
    # Mount and then pin
    await comp.mount_inproc("backend", srv, pinned=True)

    # Attempt to unmount should raise and keep the server
    with pytest.raises(RuntimeError):
        await comp.unmount_server("backend")

    specs = await comp.mount_specs()
    assert "backend" in specs, "pinned server should remain mounted after failed unmount"
