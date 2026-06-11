"""Smoke + behavior tests for the PostScan Mail FastMCP server.

Uses respx to mock the upstream REST API and FastMCP's in-process Client
to exercise the registered tools end-to-end without spinning up uvicorn.
Shared setup lives in <conftest.py>.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel
import respx
from fastmcp.client import Client

from x.postscanmail_mcp_server.conftest import TEST_API_KEY

EXPECTED_TOOLS = {
    "list_items",
    "list_automation_rules",
    "set_automation_rule",
    "request_open",
    "cancel_open",
    "request_discard",
    "cancel_discard",
    "request_rescan",
    "cancel_rescan",
    "request_shred",
    "cancel_shred",
}


async def test_all_tools_registered(mcp_client: Client) -> None:
    tools = await mcp_client.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_list_items_forwards_query_params_and_api_key(mcp_client: Client, respx_router: respx.Router) -> None:
    route = respx_router.get("/items").mock(return_value=httpx.Response(200, json={"items": []}))
    result = await mcp_client.call_tool("list_items", {"sort_order": "asc", "page": 3})
    assert result.structured_content == {"result": {"items": []}}
    assert dict(route.calls.last.request.url.params) == {"sort_order": "asc", "page": "3"}
    assert route.calls.last.request.headers["x-api-key"] == TEST_API_KEY


async def test_set_automation_rule_encodes_bool_as_int(mcp_client: Client, respx_router: respx.Router) -> None:
    route = respx_router.put("/user-defined-rules/update-system-user-defined-rule").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await mcp_client.call_tool("set_automation_rule", {"automation_name": "auto_scan", "is_active": False})
    assert route.calls.last.request.read() == b'{"automation_name":"auto_scan","is_active":0}'


@pytest.mark.parametrize(
    ("tool", "url_suffix"),
    [
        ("request_open", "open"),
        ("cancel_open", "open/cancel"),
        ("request_discard", "discard"),
        ("cancel_discard", "discard/cancel"),
        ("request_rescan", "rescan"),
        ("cancel_rescan", "rescan/cancel"),
        ("request_shred", "shred"),
        ("cancel_shred", "shred/cancel"),
    ],
)
async def test_address_actions_dispatch_to_right_url(
    tool: str, url_suffix: str, mcp_client: Client, respx_router: respx.Router
) -> None:
    route = respx_router.post(f"/addresses/addr-1/items/actions/{url_suffix}").mock(
        return_value=httpx.Response(200, json={"queued": 2})
    )
    await mcp_client.call_tool(tool, {"address_id": "addr-1", "mail_ids": ["m1", "m2"]})
    assert route.calls.last.request.read() == b'{"mail_ids":["m1","m2"]}'


if __name__ == "__main__":
    pytest_bazel.main()
