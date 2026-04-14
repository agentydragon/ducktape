"""E2E test: Grocy container + MCP server → inventory bootstrap workflow.

Starts a real Grocy container (LinuxServer image, auth disabled, demo mode),
wires the MCP server to it, and exercises a realistic sequence of tool calls
that mirrors how an LLM would bootstrap a Grocy inventory from scratch.

All tool calls go through the full MCP protocol via fastmcp.Client with
FastMCPTransport. This ensures output schema validation and error propagation
match what real MCP clients (e.g. Claude) experience.
"""

from __future__ import annotations

import logging
import tempfile
import time
import uuid
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

import httpx
import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.client import Client
from fastmcp.client.transports import FastMCPTransport

from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.server import build_mcp
from x.grocy_mcp.tool_metadata import TOOL_OVERRIDES

logger = logging.getLogger(__name__)

# Names of the custom batch tools registered by register_batch_tools.
CUSTOM_TOOL_NAMES = {
    "create_entities",
    "list_entities",
    "get_entities",
    "get_stock",
    "add_stock",
    "consume_stock",
    "inventory_products",
}


# ── Fixtures ─────────────────────────────────────────────────────────────


def _settings(grocy_url: str) -> ServerSettings:
    return ServerSettings(
        oidc_issuer="https://auth.example.com/application/o/grocy-mcp/",
        oidc_client_id="unused",
        oidc_client_secret="unused",
        public_base_url="https://grocy-mcp.example.com",
        grocy_url=grocy_url,
        grocy_proxy_client_id="unused",
    )


def _prepare_custom_init_dir() -> str:
    """Create a dir with a script that strips IPv6 listen directives from nginx config.

    LinuxServer s6-overlay runs scripts in /custom-cont-init.d/ after
    migrations (which generate the nginx config) but before services start.
    """
    init_dir = tempfile.mkdtemp(prefix="grocy-custom-init-")
    script = Path(init_dir) / "disable-ipv6.sh"
    script.write_text(
        "#!/bin/bash\n"
        "echo 'disable-ipv6: patching nginx configs'\n"
        "sed -i '/listen \\[/d' /config/nginx/site-confs/*.conf\n"
        "echo 'disable-ipv6: done, resulting config:'\n"
        "cat /config/nginx/site-confs/default.conf\n"
    )
    script.chmod(0o755)
    return init_dir


@pytest.fixture(scope="session", autouse=True)
def _preload_grocy() -> None:
    load_oci_image(GROCY)


@pytest.fixture(scope="session")
def grocy_container() -> Generator[LoggedContainer]:
    """Session-scoped Grocy container with auth disabled."""
    container = LoggedContainer(GROCY.tag, test_name="grocy")
    container.with_exposed_ports(80)
    container.with_env("PUID", "1000")
    container.with_env("PGID", "1000")
    container.with_env("TZ", "UTC")
    container.with_env("GROCY_MODE", "production")
    container.with_env("GROCY_DISABLE_AUTH", "true")
    # Mount custom-init script that strips IPv6 — RBE workers lack IPv6.
    container.with_volume_mapping(_prepare_custom_init_dir(), "/custom-cont-init.d", "ro")

    with container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(80)
        base_url = f"http://{host}:{port}"

        deadline = time.monotonic() + 90
        last_err = ""
        while time.monotonic() < deadline:
            try:
                # Hit the web UI first — Grocy runs DB migrations on first page load.
                # The first load can be slow (demo data generation).
                httpx.get(f"{base_url}/", timeout=10)
                r = httpx.get(f"{base_url}/api/system/info", timeout=10)
                if r.status_code == 200:
                    logger.info("Grocy ready at %s", base_url)
                    break
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
                last_err = f"{type(e).__name__}: {e}"
            time.sleep(2)
        else:
            raise TimeoutError(f"Grocy did not become ready at {base_url} within 90s. Last: {last_err}")

        yield container


@pytest.fixture(scope="session")
def grocy_base_url(grocy_container: LoggedContainer) -> str:
    host = grocy_container.get_container_host_ip()
    port = grocy_container.get_exposed_port(80)
    return f"http://{host}:{port}"


@pytest.fixture(scope="session")
def grocy_mcp(grocy_base_url: str) -> FastMCP:
    client = httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)
    return build_mcp(_settings(grocy_base_url), client=client)


@pytest.fixture
async def mcp_client(grocy_mcp: FastMCP) -> AsyncGenerator[Client]:
    """Function-scoped MCP client exercising the full MCP protocol in-process.

    Function scope (not session) ensures the client and its transport share the same
    event loop as the test function. pytest-asyncio creates a new event loop per test
    function; a session-scoped async fixture would run in a different loop and the
    client's internal asyncio primitives would deadlock when used from the test loop.
    """
    async with Client(FastMCPTransport(grocy_mcp)) as client:
        yield client


# ── Tool name coverage ───────────────────────────────────────────────────


async def test_all_tool_names_are_customized(mcp_client: Client) -> None:
    """Every tool exposed by the MCP server has a name from TOOL_OVERRIDES or CUSTOM_TOOL_NAMES."""
    tools = await mcp_client.list_tools()
    expected_names = {o.name for o in TOOL_OVERRIDES.values() if o.enabled and not o.resource}
    expected_names |= CUSTOM_TOOL_NAMES
    actual_names = {t.name for t in tools}
    assert actual_names == expected_names, (
        f"Mismatch: extra={actual_names - expected_names}, missing={expected_names - actual_names}"
    )


# ── MCP resource ─────────────────────────────────────────────────────────


async def test_system_info_resource(mcp_client: Client) -> None:
    """GET /system/info is exposed as an MCP resource, not a tool."""
    tools = await mcp_client.list_tools()
    tool_names = {t.name for t in tools}
    assert "get_system_info" not in tool_names, "system_info should be a resource, not a tool"

    resources = await mcp_client.list_resources()
    resource_uris = {str(r.uri): r for r in resources}
    matching = [uri for uri in resource_uris if "system_info" in uri]
    assert matching, f"system_info resource not found in {list(resource_uris.keys())}"

    resource_uri = matching[0]
    content = await mcp_client.read_resource(resource_uri)
    assert content, "system_info resource returned empty content"
    text = str(content)
    assert "grocy_version" in text.lower() or "Grocy" in text, f"Unexpected resource content: {text[:200]}"


# ── Inventory bootstrap workflow ─────────────────────────────────────────


async def test_inventory_bootstrap(mcp_client: Client) -> None:
    """Full batch inventory workflow: create entities → add → get enriched → consume → inventory."""
    suffix = uuid.uuid4().hex[:6]

    # 1. Create location and quantity unit in one batch call
    result = await mcp_client.call_tool(
        "create_entities",
        {
            "items": [
                {"entity_type": "locations", "body": {"name": f"TestLoc-{suffix}"}},
                {
                    "entity_type": "quantity_units",
                    "body": {"name": f"TestUnit-{suffix}", "name_plural": f"TestUnits-{suffix}"},
                },
            ]
        },
    )
    sc = result.structured_content
    assert sc is not None
    created = sc["result"]
    assert created[0]["ok"], f"location create failed: {created[0].get('error')}"
    assert created[1]["ok"], f"quantity_unit create failed: {created[1].get('error')}"
    loc_id = created[0]["created_object_id"]
    qu_id = created[1]["created_object_id"]

    # 2. Create a product
    result = await mcp_client.call_tool(
        "create_entities",
        {
            "items": [
                {
                    "entity_type": "products",
                    "body": {
                        "name": f"TestRice-{suffix}",
                        "location_id": loc_id,
                        "qu_id_purchase": qu_id,
                        "qu_id_stock": qu_id,
                    },
                }
            ]
        },
    )
    sc = result.structured_content
    assert sc is not None
    product_id = sc["result"][0]["created_object_id"]
    assert product_id is not None

    # 3. Add stock
    result = await mcp_client.call_tool(
        "add_stock", {"items": [{"product_id": product_id, "amount": 5, "best_before_date": "2030-01-01"}]}
    )
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["ok"], f"add_stock failed: {op.get('error')}"
    assert op["new_amount"] == 5.0

    # 4. Get enriched stock — verify product + QU + location attached
    result = await mcp_client.call_tool("get_stock", {"include_quantity_unit": True, "include_location": True})
    sc = result.structured_content
    assert sc is not None
    stock = sc["result"]
    product_stock = [s for s in stock if str(s["product_id"]) == str(product_id)]
    assert len(product_stock) == 1, f"product {product_id} not found in stock"
    assert float(product_stock[0]["amount"]) == 5.0
    assert product_stock[0]["quantity_unit"] is not None, "quantity_unit not enriched"
    assert product_stock[0]["location"] is not None, "location not enriched"

    # 5. Consume some stock
    result = await mcp_client.call_tool("consume_stock", {"items": [{"product_id": product_id, "amount": 2}]})
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["ok"], f"consume_stock failed: {op.get('error')}"
    assert op["new_amount"] == 3.0

    # 6. Inventory — set absolute amount
    result = await mcp_client.call_tool("inventory_products", {"items": [{"product_id": product_id, "new_amount": 10}]})
    sc = result.structured_content
    assert sc is not None
    op = sc["result"][0]
    assert op["ok"], f"inventory_products failed: {op.get('error')}"
    assert op["new_amount"] == 10.0

    # 7. List entities — fetch products, locations, quantity_units in one call
    result = await mcp_client.call_tool("list_entities", {"entity_types": ["products", "locations", "quantity_units"]})
    data = result.structured_content
    assert data is not None
    assert "products" in data
    assert "locations" in data
    assert "quantity_units" in data
    product_names = [p["name"] for p in data["products"]]
    assert f"TestRice-{suffix}" in product_names

    # 8. Get specific entity by ID
    result = await mcp_client.call_tool("get_entities", {"entity_type": "products", "object_ids": [product_id]})
    sc = result.structured_content
    assert sc is not None
    entities = sc["result"]
    assert entities[0]["ok"], f"get_entities failed: {entities[0].get('error')}"
    assert entities[0]["data"]["name"] == f"TestRice-{suffix}"


if __name__ == "__main__":
    pytest_bazel.main()
