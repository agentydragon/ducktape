from __future__ import annotations

from fastmcp.client import Client
from fastmcp.server import FastMCP
from hamcrest import assert_that, empty, has_item, has_properties
from tests.util.notifications import SubscriptionRecorder, enable_resources_caps, install_subscription_recorder

from adgn.mcp.compositor.server import Compositor
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP
from adgn.mcp.resources.clients import ResourcesClient
from adgn.mcp.resources.server import make_resources_server


class _StubGatewaySession:
    def __init__(self) -> None:
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []

    async def subscribe_resource(self, uri):  # type: ignore[no-untyped-def]
        self.subscribed.append(str(uri))

    async def unsubscribe_resource(self, uri):  # type: ignore[no-untyped-def]
        self.unsubscribed.append(str(uri))


class _StubGatewayClient:
    def __init__(self) -> None:
        self.session = _StubGatewaySession()


def _make_origin() -> tuple[FastMCP, SubscriptionRecorder]:
    m = NotifyingFastMCP("origin")
    recorder = install_subscription_recorder(m)

    @m.resource("resource://foo/bar", name="dummy", mime_type="text/plain", description="dummy")
    async def foo_bar() -> str:
        return "ok"

    # Ensure this origin advertises resources.subscribe for gating and
    # registers explicit handlers so subscribe/unsubscribe calls succeed.
    enable_resources_caps(m, subscribe=True)
    return m, recorder


async def test_subscriptions_index_updates_on_unmount():
    # Compositor with one origin server mounted
    comp = Compositor("comp")
    origin, hooks = _make_origin()
    await comp.mount_inproc("origin", origin)

    # Resources server with a real gateway client
    async with Client(comp) as gw:
        res_server = make_resources_server(name="resources", gateway_client=gw, compositor=comp)
        async with Client(res_server) as client:
            # Subscribe to an origin resource via the resources server tool
            rc = ResourcesClient(client)
            await rc.subscribe(server="origin", uri="resource://foo/bar")
            assert hooks.subscribed, "expected origin to receive subscribe"
            # Index reflects the subscription
            idx = await rc.list_subscriptions()
            assert_that(idx.subscriptions, has_item(has_properties(server="origin", uri="resource://foo/bar")))

            # Unmount the origin server; subscription should be dropped from index
            await comp.unmount_server("origin")
            assert not hooks.unsubscribed, "unexpected origin unsubscribe on unmount"
            idx2 = await rc.list_subscriptions()
            assert_that(idx2.subscriptions, empty())
