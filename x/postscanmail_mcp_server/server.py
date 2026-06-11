"""FastMCP server exposing the PostScan Mail Developer API.

Hand-authored thin wrapper around the eleven endpoints documented at
<https://github.com/PostScanMail/api-docs>. PostScan Mail does not publish
an OpenAPI spec, so `FastMCP.from_openapi` is not an option.

This server is **not** auth-aware — it speaks to PostScan Mail with a single
static `x-api-key` for the account-wide developer key. Per-caller auth and
the agentydragon-only ACL are enforced upstream by the `mcp-oauth-facade`
sidecar in the same Kubernetes pod (see
<cluster/k8s/agents/postscanmail-mcp/app/deployment.yaml>).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Literal, cast

import httpx
import uvicorn
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

BASE_URL = "https://api.postscanmail.com/api/account-docs/v2"
SortOrder = Literal["asc", "desc"]
AutomationName = Literal["auto_scan", "auto_shred", "auto_discard"]
ActionKind = Literal["open", "discard", "rescan", "shred"]


def _json(response: httpx.Response) -> object:
    """Read JSON from a 2xx response. Returns opaque `object` — PostScan Mail's response shapes are not documented."""
    response.raise_for_status()
    return cast("object", response.json())


def build_mcp(client: httpx.AsyncClient) -> FastMCP:
    mcp: FastMCP = FastMCP("postscanmail")

    @mcp.tool
    async def list_items(sort_order: SortOrder = "desc", page: int = 1) -> object:
        """List mail items received in the account, paginated.

        `sort_order` is by received-date; `page` is 1-indexed. Response
        shape is set by PostScan Mail and varies by account; expect a list
        of items plus paging metadata.
        """
        return _json(await client.get("/items", params={"sort_order": sort_order, "page": page}))

    @mcp.tool
    async def list_automation_rules(sort_order: SortOrder = "desc", page: int = 1) -> object:
        """List system automation rules (Auto Scan / Auto Shred / Auto Discard) and their on/off state."""
        return _json(
            await client.get(
                "/user-defined-rules/system-user-defined-rules", params={"sort_order": sort_order, "page": page}
            )
        )

    @mcp.tool
    async def set_automation_rule(automation_name: AutomationName, is_active: bool) -> object:
        """Enable or disable a system automation rule account-wide.

        `is_active=True` enables the rule for all mailbox users; `False`
        disables it. Affects future incoming mail only.
        """
        return _json(
            await client.put(
                "/user-defined-rules/update-system-user-defined-rule",
                json={"automation_name": automation_name, "is_active": 1 if is_active else 0},
            )
        )

    async def _action(address_id: str, kind: ActionKind, *, cancel: bool, mail_ids: list[str]) -> object:
        suffix = "/cancel" if cancel else ""
        return _json(
            await client.post(f"/addresses/{address_id}/items/actions/{kind}{suffix}", json={"mail_ids": mail_ids})
        )

    @mcp.tool
    async def request_open(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail open the named envelopes and scan their contents.

        Paid action (per-piece scan fee). Use `cancel_open` to undo while
        the request is still pending.
        """
        return await _action(address_id, "open", cancel=False, mail_ids=mail_ids)

    @mcp.tool
    async def cancel_open(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending `request_open` for the named items."""
        return await _action(address_id, "open", cancel=True, mail_ids=mail_ids)

    @mcp.tool
    async def request_discard(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail discard (trash) the named items. Use `cancel_discard` to undo while pending."""
        return await _action(address_id, "discard", cancel=False, mail_ids=mail_ids)

    @mcp.tool
    async def cancel_discard(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending `request_discard` for the named items."""
        return await _action(address_id, "discard", cancel=True, mail_ids=mail_ids)

    @mcp.tool
    async def request_rescan(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail rescan the named items. Paid action."""
        return await _action(address_id, "rescan", cancel=False, mail_ids=mail_ids)

    @mcp.tool
    async def cancel_rescan(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending `request_rescan` for the named items."""
        return await _action(address_id, "rescan", cancel=True, mail_ids=mail_ids)

    @mcp.tool
    async def request_shred(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail shred the named items (secure destruction). Use `cancel_shred` to undo while pending."""
        return await _action(address_id, "shred", cancel=False, mail_ids=mail_ids)

    @mcp.tool
    async def cancel_shred(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending `request_shred` for the named items."""
        return await _action(address_id, "shred", cancel=True, mail_ids=mail_ids)

    return mcp


def build_client(api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=BASE_URL, headers={"x-api-key": api_key, "Content-Type": "application/json"}, timeout=30.0
    )


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    api_key = os.environ["POSTSCANMAIL_API_KEY"]
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    mcp = build_mcp(build_client(api_key))
    app = mcp.http_app(path="/mcp")
    logger.info("postscanmail-mcp listening on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
