"""E2E test: Grocy container + MCP server → inventory bootstrap workflow.

Starts a real Grocy container (LinuxServer image, auth disabled, demo mode),
wires the MCP server to it, and exercises a realistic sequence of tool calls
that mirrors how an LLM would bootstrap a Grocy inventory from scratch.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import jsonschema
import pytest
import pytest_bazel
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import TextContent

from third_party.containers.rlocations import GROCY
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.server import build_mcp
from x.grocy_mcp.tool_metadata import TOOL_OVERRIDES

logger = logging.getLogger(__name__)


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


def _make_grocy_client(grocy_base_url: str) -> httpx.AsyncClient:
    """Create a fresh async httpx client for the Grocy API."""
    return httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)


# ── Helpers ──────────────────────────────────────────────────────────────


def _result_json(result: ToolResult) -> Any:
    """Extract parsed JSON from a FastMCP ToolResult."""
    if result.structured_content is not None:
        return result.structured_content
    if result.content:
        first = result.content[0]
        assert isinstance(first, TextContent), f"Expected TextContent, got {type(first)}"
        return json.loads(first.text)
    raise ValueError(f"Empty tool result: {result!r}")


async def _call(mcp: FastMCP, tool_name: str, args: dict | None = None) -> Any:
    """Call a tool and validate the result against the tool's declared output schema.

    This mirrors what the MCP client (e.g. Claude) does: after receiving a
    CallToolResult with structuredContent, it validates the content against the
    tool's declared outputSchema. Calling mcp.call_tool() directly skips that
    client-side validation, so we replicate it here to catch schema mismatches
    (e.g. Grocy returning "9" where the spec says integer).
    """
    result = await mcp.call_tool(tool_name, args or {})
    data = _result_json(result)

    tool = await mcp.get_tool(tool_name)
    if tool is not None and tool.output_schema is not None and result.structured_content is not None:
        try:
            jsonschema.validate(result.structured_content, tool.output_schema)
        except jsonschema.ValidationError as e:
            pytest.fail(f"Output schema validation failed for {tool_name!r}: {e.message}")

    return data


# ── Tool name coverage ───────────────────────────────────────────────────


async def test_all_tool_names_are_customized(grocy_base_url: str) -> None:
    """Every tool exposed by the MCP server has a name from TOOL_OVERRIDES."""
    mcp = build_mcp(_settings(grocy_base_url), client=_make_grocy_client(grocy_base_url))
    tools = await mcp.list_tools()
    expected_names = {o.name for o in TOOL_OVERRIDES.values() if o.enabled and not o.resource}
    actual_names = {t.name for t in tools}
    assert actual_names == expected_names, (
        f"Mismatch: extra={actual_names - expected_names}, missing={expected_names - actual_names}"
    )


# ── MCP resource ─────────────────────────────────────────────────────────


async def test_system_info_resource(grocy_base_url: str) -> None:
    """GET /system/info is exposed as an MCP resource, not a tool."""
    mcp = build_mcp(_settings(grocy_base_url), client=_make_grocy_client(grocy_base_url))

    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert "get_system_info" not in tool_names, "system_info should be a resource, not a tool"

    resources = await mcp.list_resources()
    resource_uris = {str(r.uri): r for r in resources}
    matching = [uri for uri in resource_uris if "system_info" in uri]
    assert matching, f"system_info resource not found in {list(resource_uris.keys())}"

    # Read the resource — should return Grocy version info.
    resource_uri = matching[0]
    content = await mcp.read_resource(resource_uri)
    assert content, "system_info resource returned empty content"
    text = str(content)
    assert "grocy_version" in text.lower() or "Grocy" in text, f"Unexpected resource content: {text[:200]}"


# ── Inventory bootstrap workflow ─────────────────────────────────────────


async def test_inventory_bootstrap(grocy_base_url: str) -> None:
    """Full inventory workflow: create entities → add stock → query → consume."""
    mcp = build_mcp(_settings(grocy_base_url), client=_make_grocy_client(grocy_base_url))

    # Use unique names to avoid collision with Grocy's seed data.
    suffix = uuid.uuid4().hex[:6]

    # 1. Create a location
    loc = await _call(mcp, "create_entity", {"entity": "locations", "body": {"name": f"TestLoc-{suffix}"}})
    loc_id = loc["created_object_id"]

    # 2. Create a quantity unit
    qu = await _call(
        mcp,
        "create_entity",
        {"entity": "quantity_units", "body": {"name": f"TestUnit-{suffix}", "name_plural": f"TestUnits-{suffix}"}},
    )
    qu_id = qu["created_object_id"]

    # 3. Create a product
    product = await _call(
        mcp,
        "create_entity",
        {
            "entity": "products",
            "body": {
                "name": f"TestRice-{suffix}",
                "location_id": loc_id,
                "qu_id_purchase": qu_id,
                "qu_id_stock": qu_id,
            },
        },
    )
    product_id = product["created_object_id"]

    # 4. Add stock
    add_result = await _call(
        mcp, "add_product_stock", {"productId": product_id, "amount": 5, "best_before_date": "2030-01-01"}
    )
    # FastMCP wraps the response in {"result": [...]}.
    transactions = add_result["result"] if isinstance(add_result, dict) and "result" in add_result else add_result
    assert isinstance(transactions, list), f"Expected transaction list, got {add_result!r}"

    # 5. Verify stock shows up
    stock_raw = await _call(mcp, "list_stock")
    stock = stock_raw["result"] if isinstance(stock_raw, dict) and "result" in stock_raw else stock_raw
    assert isinstance(stock, list)
    # product_id may be str or int depending on Grocy version — compare loosely.
    product_stock = [s for s in stock if str(s.get("product_id")) == str(product_id)]
    assert len(product_stock) == 1
    assert float(product_stock[0]["amount"]) == 5.0

    # 6. Get product details
    details = await _call(mcp, "get_product_stock", {"productId": product_id})
    assert float(details["stock_amount"]) == 5.0

    # 7. Consume some stock
    await _call(mcp, "consume_product_stock", {"productId": product_id, "amount": 2})

    # 8. Verify reduced stock
    stock_after_raw = await _call(mcp, "list_stock")
    stock_after = (
        stock_after_raw["result"]
        if isinstance(stock_after_raw, dict) and "result" in stock_after_raw
        else stock_after_raw
    )
    product_stock_after = [s for s in stock_after if str(s.get("product_id")) == str(product_id)]
    assert float(product_stock_after[0]["amount"]) == 3.0

    # 9. Verify entity CRUD — read back the product
    product_obj = await _call(mcp, "get_entity", {"entity": "products", "objectId": product_id})
    assert product_obj["name"] == f"TestRice-{suffix}"


if __name__ == "__main__":
    pytest_bazel.main()
