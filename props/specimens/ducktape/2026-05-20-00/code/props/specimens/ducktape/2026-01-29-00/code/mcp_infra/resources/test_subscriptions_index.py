from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import assert_that, empty, has_item, has_properties

from mcp_infra.compositor.resources_server import ResourcesSubscribeArgs
from mcp_infra.prefix import MCPMountPrefix


@pytest.mark.asyncio
async def test_subscriptions_index_updates_on_unmount(compositor, origin_with_recorder, typed_resources_client):
    # Compositor with one origin server mounted
    origin, hooks = origin_with_recorder
    origin_prefix = MCPMountPrefix("origin")
    await compositor.mount_inproc(origin_prefix, origin)

    # Subscribe to an origin resource via the resources server tool
    await typed_resources_client.subscribe(ResourcesSubscribeArgs(server=origin_prefix, uri="resource://foo/bar"))

    # Verify origin subscribe handler ran and index reflects the subscription
    assert hooks.subscribed, "expected origin to receive subscribe"
    # Read the subscriptions index resource
    idx = await typed_resources_client.list_subscriptions()
    assert_that(idx.subscriptions, has_item(has_properties(server=origin_prefix, uri="resource://foo/bar")))

    # Unmount the origin server; the resources server should not attempt remote
    # unsubscription. It should drop the non-pinned record from the index.
    await compositor.unmount_server(origin_prefix)

    # Ensure no unsubscribe call happened as a result of unmount
    assert not hooks.unsubscribed, "unexpected origin unsubscribe on unmount"

    # Subscriptions index should be empty now
    idx2 = await typed_resources_client.list_subscriptions()
    assert_that(idx2.subscriptions, empty())


if __name__ == "__main__":
    pytest_bazel.main()
