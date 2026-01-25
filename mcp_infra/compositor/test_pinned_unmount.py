from __future__ import annotations

import pytest
import pytest_bazel

from mcp_infra.prefix import MCPMountPrefix


async def test_unmount_pinned_server_errors_and_kept(compositor, make_simple_mcp):
    backend_prefix = MCPMountPrefix("backend")
    # Mount and then pin
    await compositor.mount_inproc(backend_prefix, make_simple_mcp, pinned=True)

    # Attempt to unmount should raise and keep the server
    with pytest.raises(RuntimeError, match="Cannot unmount pinned server"):
        await compositor.unmount_server(backend_prefix)

    # Verify server is still mounted by checking server entries
    entries = await compositor.server_entries()
    assert backend_prefix in entries, "pinned server should remain mounted after failed unmount"


if __name__ == "__main__":
    pytest_bazel.main()
