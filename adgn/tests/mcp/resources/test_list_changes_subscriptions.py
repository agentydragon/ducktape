from __future__ import annotations

from fastmcp.client import Client
from fastmcp.server import FastMCP
from hamcrest import assert_that, contains, contains_inanyorder, empty, has_item, has_properties

from adgn.mcp.compositor.server import Compositor
from adgn.mcp.resources.clients import ResourcesClient
from adgn.mcp.resources.server import make_resources_server


class _StubGatewaySession:
    async def subscribe_resource(self, uri):  # type: ignore[no-untyped-def]
        return None

    async def unsubscribe_resource(self, uri):  # type: ignore[no-untyped-def]
        return None


class _StubGatewayClient:
    def __init__(self) -> None:
        self.session = _StubGatewaySession()


async def test_list_changes_subscriptions_visible_and_cleared_on_unmount():
    comp = Compositor("comp")
    origin = FastMCP("origin")
    await comp.mount_inproc("origin", origin)

    gw = _StubGatewayClient()
    res_server = make_resources_server(name="resources", gateway_client=gw, compositor=comp)

    async with Client(res_server) as client:
        # Subscribe to list changes for the origin server
        rc = ResourcesClient(client)
        await rc.subscribe_list_changes(server="origin")
        idx = await rc.list_subscriptions()
        assert_that(idx.list_subscriptions, has_item(has_properties(server="origin", present=True, active=True)))

        # Unmount origin; selection should be cleared from the index
        await comp.unmount_server("origin")
        idx2 = await rc.list_subscriptions()
        assert_that(idx2.list_subscriptions, empty())


async def test_list_changes_multiple_subscriptions_and_unsubscribe():
    comp = Compositor("comp2")
    a = FastMCP("a")
    b = FastMCP("b")
    await comp.mount_inproc("a", a)
    await comp.mount_inproc("b", b)

    gw = _StubGatewayClient()
    res_server = make_resources_server(name="resources", gateway_client=gw, compositor=comp)

    async with Client(res_server) as client:
        rc = ResourcesClient(client)
        # Subscribe to both origins
        await rc.subscribe_list_changes(server="a")
        await rc.subscribe_list_changes(server="b")
        idx = await rc.list_subscriptions()
        assert_that([x.server for x in idx.list_subscriptions], contains_inanyorder("a", "b"))

        # Unsubscribe one
        await rc.unsubscribe_list_changes(server="a")
        idx2 = await rc.list_subscriptions()
        assert_that([x.server for x in idx2.list_subscriptions], contains("b"))
