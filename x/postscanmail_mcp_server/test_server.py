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


def _items_body(items: list[dict]) -> dict:
    """A realistic Laravel `LengthAwarePaginator` `/items` envelope."""
    return {
        "status": 1,
        "data": {
            "current_page": 1,
            "last_page": 1,
            "per_page": 10,
            "total": len(items),
            "next_page_url": None,
            "prev_page_url": None,
            "data": items,
        },
    }


async def test_list_items_forwards_query_params_and_api_key(mcp_client: Client, respx_router: respx.Router) -> None:
    route = respx_router.get("/items").mock(return_value=httpx.Response(200, json=_items_body([])))
    result = await mcp_client.call_tool("list_items", {"sort_order": "asc", "page": 3})
    # query params + api key are forwarded unchanged
    assert dict(route.calls.last.request.url.params) == {"sort_order": "asc", "page": "3"}
    assert route.calls.last.request.headers["x-api-key"] == TEST_API_KEY
    assert result.structured_content is not None
    page = result.structured_content
    assert page["items"] == []
    assert page["total"] == 0


async def test_list_items_parses_typed_page(mcp_client: Client, respx_router: respx.Router) -> None:
    item = {
        "mail_id": "217547",
        "sender_name": "Charles Schwab",
        "address_id": 1730,
        "ai_summary": ["Sender Name: Schwab", "Overall Subject: Fee notice"],
        "ai_summary_version": "v1",
        "cover_image": "https://psm.example/cover.png?signature=x",
        "pdf_content": "https://psm.example/mail.pdf?signature=y",
        "pdf_metadata": {
            "received_at": "2026-06-20 01:05:18",
            "current_status": "Inbox",
            "uploaded_from_address": {"city": "San Francisco", "state": "CA", "postal_code": "94108"},
        },
    }
    respx_router.get("/items").mock(return_value=httpx.Response(200, json=_items_body([item, item])))
    result = await mcp_client.call_tool("list_items", {})
    assert result.structured_content is not None
    page = result.structured_content
    assert page["total"] == 2
    assert len(page["items"]) == 2
    got = page["items"][0]
    assert got["mail_id"] == "217547"
    assert got["sender_name"] == "Charles Schwab"
    assert got["ai_summary"] == ["Sender Name: Schwab", "Overall Subject: Fee notice"]
    assert got["pdf_content"].startswith("https://psm.example/mail.pdf")
    assert got["pdf_metadata"]["received_at"] == "2026-06-20 01:05:18"
    assert got["pdf_metadata"]["uploaded_from_address"]["city"] == "San Francisco"


async def test_list_automation_rules_parses(mcp_client: Client, respx_router: respx.Router) -> None:
    body = {
        "status": 1,
        "data": {
            "current_page": 1,
            "last_page": 1,
            "per_page": 10,
            "total": 1,
            "next_page_url": None,
            "prev_page_url": None,
            "data": [
                {
                    "user_full_name": "M Pokorny",
                    "auto_scan": True,
                    "auto_shred": False,
                    "auto_discard": False,
                    "auto_ai_summary": True,
                    "last_changed_at": "2026-05-29 18:33:09",
                }
            ],
        },
    }
    respx_router.get("/user-defined-rules/system-user-defined-rules").mock(return_value=httpx.Response(200, json=body))
    result = await mcp_client.call_tool("list_automation_rules", {})
    assert result.structured_content is not None
    page = result.structured_content
    assert page["total"] == 1
    assert page["rules"][0]["auto_scan"] is True
    assert page["rules"][0]["auto_shred"] is False
    assert page["rules"][0]["auto_ai_summary"] is True


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
