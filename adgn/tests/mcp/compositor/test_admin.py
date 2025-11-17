from __future__ import annotations

import pytest

from adgn.mcp._shared.constants import COMPOSITOR_META_SERVER_NAME


async def test_compositor_admin_attach_detach(admin_env, stdio_echo_spec):
    admin, comp = admin_env
    # Create a stdio child spec and attach
    await admin.attach_server(name="backend", spec=stdio_echo_spec)
    specs = await comp.mount_specs()
    assert "backend" in specs

    # Detach should remove the server
    await admin.detach_server(name="backend")
    specs_after = await comp.mount_specs()
    assert "backend" not in specs_after


async def test_compositor_admin_attach_twice_errors(admin_env, stdio_echo_spec):
    admin, _comp = admin_env
    await admin.attach_server(name="backend2", spec=stdio_echo_spec)
    with pytest.raises(Exception, match="backend2.*already.*attached|name.*already.*exists"):
        await admin.attach_server(name="backend2", spec=stdio_echo_spec)


async def test_compositor_admin_detach_pinned_server_fails(admin_env):
    admin, _comp = admin_env
    # Attempt to detach a pinned server should raise
    with pytest.raises(Exception, match="pinned|cannot.*detach"):
        await admin.detach_server(name=COMPOSITOR_META_SERVER_NAME)


async def test_compositor_admin_attach_invalid_name_errors(admin_env, stdio_echo_spec):
    admin, _comp = admin_env
    # Invalid name containing double underscore should fail
    with pytest.raises(Exception, match="invalid.*name|double.*underscore"):
        await admin.attach_server(name="bad__name", spec=stdio_echo_spec)
