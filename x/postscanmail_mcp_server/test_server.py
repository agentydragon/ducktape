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
from mcp.types import ToolAnnotations

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


async def test_tools_carry_correct_annotations(mcp_client: Client) -> None:
    tools = {t.name: t for t in await mcp_client.list_tools()}

    def ann(name: str) -> ToolAnnotations:
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} missing annotations"
        return annotations

    # Reads: read-only (openWorldHint stays default true — external mailbox, not the tool's
    # own state), so clients auto-run them without a per-call approval prompt.
    for name in ("list_items", "list_automation_rules"):
        assert ann(name).readOnlyHint is True
    # Account-wide automation toggle: idempotent PUT of a boolean, reversible — non-destructive.
    toggle = ann("set_automation_rule")
    assert toggle.idempotentHint is True
    assert toggle.destructiveHint is False
    # Cancels: a repeat with nothing pending is a no-op (idempotent); cancelling prevents,
    # never causes, an effect — non-destructive.
    for name in ("cancel_open", "cancel_discard", "cancel_rescan", "cancel_shred"):
        cancel = ann(name)
        assert cancel.idempotentHint is True
        assert cancel.destructiveHint is False
    # Paid scans (open/rescan): state-changing and irreversible once done, but additive —
    # non-destructive.
    for name in ("request_open", "request_rescan"):
        assert ann(name).destructiveHint is False
    # Mail removal/destruction: discard trashes, shred destroys securely — destructive.
    for name in ("request_discard", "request_shred"):
        assert ann(name).destructiveHint is True


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
