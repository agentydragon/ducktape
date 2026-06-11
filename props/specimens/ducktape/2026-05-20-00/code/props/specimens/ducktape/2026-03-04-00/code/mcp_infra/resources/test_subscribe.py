import pytest
import pytest_bazel

from mcp_infra.compositor.resources_server import ResourcesSubscribeArgs
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.notifications import enable_resources_caps, install_subscription_recorder


@pytest.mark.asyncio
async def test_client_resource_subscribe_and_unsubscribe(compositor, typed_resources_client):
    """Subscribe/unsubscribe to a server resource via the Compositor client.

    Uses an origin that exposes a dummy resource and advertises subscribe capability.
    """
    # Compositor with a simple origin that exposes the resource to subscribe to
    origin = EnhancedFastMCP("origin")
    recorder = install_subscription_recorder(origin)
    enable_resources_caps(origin, subscribe=True)

    @origin.resource("resource://foo/bar", name="dummy", mime_type="text/plain")
    async def _foo_bar() -> str:
        return "ok"

    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # Subscribe to the resource and then unsubscribe
    await typed_resources_client.subscribe(
        ResourcesSubscribeArgs(server=MCPMountPrefix("origin"), uri="resource://foo/bar")
    )
    await typed_resources_client.unsubscribe(
        ResourcesSubscribeArgs(server=MCPMountPrefix("origin"), uri="resource://foo/bar")
    )
    assert recorder.subscribed, "expected origin to receive subscribe"
    assert recorder.unsubscribed, "expected origin unsubscribe call"


if __name__ == "__main__":
    pytest_bazel.main()
