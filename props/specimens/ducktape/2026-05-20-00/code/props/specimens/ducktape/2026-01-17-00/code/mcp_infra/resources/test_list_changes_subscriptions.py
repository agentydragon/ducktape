from __future__ import annotations

from fastmcp.server import FastMCP
from hamcrest import assert_that, contains, contains_inanyorder, empty, has_item, has_properties

from mcp_infra.compositor.resources_server import ListSubscribeArgs
from mcp_infra.prefix import MCPMountPrefix


async def test_list_changes_subscriptions_visible_and_cleared_on_unmount(compositor, typed_resources_client):
    origin_prefix = MCPMountPrefix("origin")
    origin = FastMCP("origin")
    await compositor.mount_inproc(origin_prefix, origin)

    # Subscribe to list changes for the origin server
    await typed_resources_client.subscribe_list_changes(ListSubscribeArgs(server=origin_prefix))
    idx = await typed_resources_client.list_subscriptions()
    assert_that(idx.list_subscriptions, has_item(has_properties(server=origin_prefix, present=True, active=True)))

    # Unmount origin; selection should be cleared from the index
    await compositor.unmount_server(origin_prefix)
    idx2 = await typed_resources_client.list_subscriptions()
    assert_that(idx2.list_subscriptions, empty())


async def test_list_changes_multiple_subscriptions_and_unsubscribe(compositor, typed_resources_client):
    a_prefix = MCPMountPrefix("a")
    b_prefix = MCPMountPrefix("b")
    a = FastMCP("a")
    b = FastMCP("b")
    await compositor.mount_inproc(a_prefix, a)
    await compositor.mount_inproc(b_prefix, b)

    # Subscribe to both origins
    await typed_resources_client.subscribe_list_changes(ListSubscribeArgs(server=a_prefix))
    await typed_resources_client.subscribe_list_changes(ListSubscribeArgs(server=b_prefix))
    idx = await typed_resources_client.list_subscriptions()
    assert_that([x.server for x in idx.list_subscriptions], contains_inanyorder(a_prefix, b_prefix))

    # Unsubscribe one
    await typed_resources_client.unsubscribe_list_changes(ListSubscribeArgs(server=a_prefix))
    idx2 = await typed_resources_client.list_subscriptions()
    assert_that([x.server for x in idx2.list_subscriptions], contains(b_prefix))
