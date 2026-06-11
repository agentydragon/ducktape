"""Test list_resources with null/None filter parameters.

Tests that optional filter parameters (server, uri_prefix) properly handle
null/None values and list all resources when filters are not provided.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from hamcrest import assert_that, contains_inanyorder, has_length

from mcp_infra.compositor.resources_server import ResourcesListArgs
from mcp_infra.enhanced.server import EnhancedFastMCP
from mcp_infra.prefix import MCPMountPrefix
from mcp_infra.testing.notifications import enable_resources_caps


@pytest.mark.asyncio
async def test_list_resources_with_null_server_filter(compositor, origin_with_recorder, typed_resources_client):
    """Test that server=None lists resources from all servers."""
    origin, _ = origin_with_recorder
    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # List resources with explicit server=None (should list all)
    result = await typed_resources_client.list_resources(ResourcesListArgs(server=None, uri_prefix=None))

    # Should find resources from all mounted servers (resources, compositor_meta, origin)
    assert_that(result.resources, has_length(3))
    server_names: set[str] = {r.server for r in result.resources}
    assert_that(list(server_names), contains_inanyorder("resources", "compositor_meta", "origin"))

    # Verify the origin resource is present with unprefixed URI
    origin_resources = [r for r in result.resources if r.server == "origin"]
    assert len(origin_resources) == 1
    assert str(origin_resources[0].resource.uri) == "resource://foo/bar"


@pytest.mark.asyncio
async def test_list_resources_with_no_args_uses_defaults(compositor, origin_with_recorder, typed_resources_client):
    """Test that passing explicit None values works (fields are required but accept None)."""
    origin, _ = origin_with_recorder
    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # List resources with explicit None for both filters
    result = await typed_resources_client.list_resources(ResourcesListArgs(server=None, uri_prefix=None))

    # Should find resources from all servers (same as explicit server=None)
    assert_that(result.resources, has_length(3))
    server_names: set[str] = {r.server for r in result.resources}
    assert_that(list(server_names), contains_inanyorder("resources", "compositor_meta", "origin"))


@pytest.mark.asyncio
async def test_list_resources_filters_by_server_when_provided(compositor, origin_with_recorder, typed_resources_client):
    """Test that server filter works when a specific server name is provided."""
    origin, _ = origin_with_recorder
    await compositor.mount_inproc(MCPMountPrefix("origin"), origin)

    # Create a second server without resources

    other = EnhancedFastMCP("other")
    await compositor.mount_inproc(MCPMountPrefix("other"), other)

    # Filter by specific server
    result_origin = await typed_resources_client.list_resources(
        ResourcesListArgs(server=MCPMountPrefix("origin"), uri_prefix=None)
    )
    result_other = await typed_resources_client.list_resources(
        ResourcesListArgs(server=MCPMountPrefix("other"), uri_prefix=None)
    )

    # Origin should have resources, other should not
    assert_that(result_origin.resources, has_length(1))
    assert_that(result_other.resources, has_length(0))


@pytest.mark.asyncio
async def test_list_resources_with_uri_prefix_filter(compositor, typed_resources_client):
    """Test that uri_prefix filter works correctly."""

    # Create server with multiple resources
    server = EnhancedFastMCP("test")

    @server.resource("resource://test/foo", name="foo", mime_type="text/plain")
    async def foo() -> str:
        return "foo"

    @server.resource("resource://test/bar", name="bar", mime_type="text/plain")
    async def bar() -> str:
        return "bar"

    @server.resource("resource://other/baz", name="baz", mime_type="text/plain")
    async def baz() -> str:
        return "baz"

    enable_resources_caps(server, subscribe=False)
    await compositor.mount_inproc(MCPMountPrefix("test"), server)

    # List all resources (no filter)
    all_resources = await typed_resources_client.list_resources(
        ResourcesListArgs(server=MCPMountPrefix("test"), uri_prefix=None)
    )
    assert_that(all_resources.resources, has_length(3))

    # Filter by uri_prefix
    test_resources = await typed_resources_client.list_resources(
        ResourcesListArgs(server=MCPMountPrefix("test"), uri_prefix="resource://test/")
    )
    other_resources = await typed_resources_client.list_resources(
        ResourcesListArgs(server=MCPMountPrefix("test"), uri_prefix="resource://other/")
    )

    # Should find 2 resources with test prefix, 1 with other prefix
    assert_that(test_resources.resources, has_length(2))
    assert_that(other_resources.resources, has_length(1))

    # Verify URIs (unprefixed - the server name is in ResourceEntry.server)
    test_uris = [str(r.resource.uri) for r in test_resources.resources]
    assert_that(test_uris, contains_inanyorder("resource://test/foo", "resource://test/bar"))


if __name__ == "__main__":
    pytest_bazel.main()
