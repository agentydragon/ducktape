"""FastMCP server exposing the PostScan Mail Developer API.

Hand-authored thin wrapper around the eleven endpoints documented at
<https://github.com/PostScanMail/api-docs>. PostScan Mail publishes no OpenAPI
spec (verified: the docs repo is markdown-only and the live API serves no
`/openapi.json`/`/swagger.json`), so `FastMCP.from_openapi` is not an option;
the read responses are modeled as typed Pydantic schemas built from the
observed payload shapes instead. Mutating/action response shapes are
undocumented, so those tools return the upstream JSON verbatim (`object`).

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
from collections.abc import Mapping
from typing import Any, Literal, cast

import httpx
import uvicorn
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BASE_URL = "https://api.postscanmail.com/api/account-docs/v2"
DOCS = "https://github.com/PostScanMail/api-docs"
SortOrder = Literal["asc", "desc"]
AutomationName = Literal["auto_scan", "auto_shred", "auto_discard"]
ActionKind = Literal["open", "discard", "rescan", "shred"]

# MCP tool annotations advertise each tool's kind so clients (claude.ai / Claude Code) group
# them and relax approval prompts. All fields are hints (advisory, not a security boundary);
# haku-console enforces its own approval policy server-side regardless. See
# <mcp_infra/docs/tool_annotations.md>. openWorldHint is left at its default (true) for every
# tool — PostScan Mail is an external, changing world, not the tool's own state.
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Account-wide automation toggle via PUT: setting the same value twice leaves the same state
# (idempotent) and the change is reversible (toggle back) — non-destructive.
_AUTOMATION_TOGGLE = ToolAnnotations(idempotentHint=True, destructiveHint=False)
# Cancels a pending action request: repeating the call once nothing is pending is a no-op
# (idempotent), and cancelling prevents — never causes — an effect (non-destructive).
_CANCEL_ACTION = ToolAnnotations(idempotentHint=True, destructiveHint=False)
# Paid, state-changing scan request (opens envelopes / re-scans): irreversible once completed
# and incurs a per-piece fee, but additive (scans content) — non-destructive. destructiveHint
# defaults to true, so set it false explicitly.
_PAID_SCAN = ToolAnnotations(destructiveHint=False)
# Permanently removes/destroys mail: discard moves it out of the mailbox (trash), shred is
# secure irreversible destruction.
_DESTRUCTIVE = ToolAnnotations(destructiveHint=True)


# --- Response models (observed shapes; PostScan Mail documents no schema) ---


class _Pagination(BaseModel):
    """Pagination metadata from the Laravel ``LengthAwarePaginator`` envelope wrapping every list response."""

    current_page: int
    last_page: int
    per_page: int
    total: int
    next_page_url: str | None = None
    prev_page_url: str | None = None


class Address(BaseModel):
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None


class PdfMetadata(BaseModel):
    received_at: str | None = None  # "YYYY-MM-DD HH:MM:SS"
    current_status: str | None = None
    current_folder_name: str | None = None
    assigned_user: str | None = None
    uploaded_from_address: Address | None = None


class MailItem(BaseModel):
    mail_id: str
    sender_name: str | None = None
    address_id: int | None = None
    # PostScan Mail's own per-piece AI summary lines (sender, subject, ...).
    ai_summary: list[str] = Field(default_factory=list)
    ai_summary_version: str | None = None
    # Signed URLs to the scan; absent until the piece is opened/scanned. This tool lists them;
    # it does not download the content.
    cover_image: str | None = None
    pdf_content: str | None = None
    pdf_metadata: PdfMetadata | None = None


class MailItemsPage(_Pagination):
    items: list[MailItem]


class AutomationRule(BaseModel):
    user_full_name: str | None = None
    auto_scan: bool
    auto_shred: bool
    auto_discard: bool
    auto_ai_summary: bool | None = None
    last_changed_at: str | None = None


class AutomationRulesPage(_Pagination):
    rules: list[AutomationRule]


def _pagination(inner: Mapping[str, Any]) -> dict[str, Any]:
    return {f: inner[f] for f in _Pagination.model_fields if f in inner}


def _items_page(raw: Mapping[str, Any]) -> MailItemsPage:
    inner = raw["data"]
    return MailItemsPage(items=[MailItem.model_validate(r) for r in inner["data"]], **_pagination(inner))


def _rules_page(raw: Mapping[str, Any]) -> AutomationRulesPage:
    inner = raw["data"]
    return AutomationRulesPage(rules=[AutomationRule.model_validate(r) for r in inner["data"]], **_pagination(inner))


def _json(response: httpx.Response) -> object:
    """Read JSON from a 2xx response as opaque ``object``.

    Used only for mutating/action endpoints, whose response shapes PostScan Mail does not
    document; reads return typed models.
    """
    response.raise_for_status()
    return cast("object", response.json())


def build_mcp(client: httpx.AsyncClient) -> FastMCP:
    mcp: FastMCP = FastMCP("postscanmail")

    @mcp.tool(annotations=_READ_ONLY)
    async def list_items(sort_order: SortOrder = "desc", page: int = 1) -> MailItemsPage:
        """List mail items received in the account, paginated (newest first by default).

        Each item carries PostScan Mail's own ``ai_summary`` and, once opened/scanned, signed
        ``cover_image``/``pdf_content`` URLs. This tool lists metadata; it does not download
        the scan content itself.

        Upstream: <https://github.com/PostScanMail/api-docs/blob/main/docs/endpoints/items.md>
        """
        resp = await client.get("/items", params={"sort_order": sort_order, "page": page})
        resp.raise_for_status()
        return _items_page(resp.json())

    @mcp.tool(annotations=_READ_ONLY)
    async def list_automation_rules(sort_order: SortOrder = "desc", page: int = 1) -> AutomationRulesPage:
        """List system automation rules (Auto Scan / Auto Shred / Auto Discard / Auto AI Summary) and their per-user on/off state.

        Upstream: <https://github.com/PostScanMail/api-docs/blob/main/docs/endpoints/system-user-defined-rules.md>
        """
        resp = await client.get(
            "/user-defined-rules/system-user-defined-rules", params={"sort_order": sort_order, "page": page}
        )
        resp.raise_for_status()
        return _rules_page(resp.json())

    @mcp.tool(annotations=_AUTOMATION_TOGGLE)
    async def set_automation_rule(automation_name: AutomationName, is_active: bool) -> object:
        """Enable or disable a system automation rule account-wide.

        ``is_active=True`` enables the rule for all mailbox users; ``False`` disables it.
        Affects future incoming mail only.

        Upstream: <https://github.com/PostScanMail/api-docs/blob/main/docs/endpoints/update-system-user-defined-rule.md>
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

    @mcp.tool(annotations=_PAID_SCAN)
    async def request_open(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail open the named envelopes and scan their contents.

        Paid action (per-piece scan fee). Use ``cancel_open`` to undo while the request is
        still pending.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "open", cancel=False, mail_ids=mail_ids)

    @mcp.tool(annotations=_CANCEL_ACTION)
    async def cancel_open(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending ``request_open`` for the named items.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "open", cancel=True, mail_ids=mail_ids)

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def request_discard(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail discard (trash) the named items. Use ``cancel_discard`` to undo while pending.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "discard", cancel=False, mail_ids=mail_ids)

    @mcp.tool(annotations=_CANCEL_ACTION)
    async def cancel_discard(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending ``request_discard`` for the named items.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "discard", cancel=True, mail_ids=mail_ids)

    @mcp.tool(annotations=_PAID_SCAN)
    async def request_rescan(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail rescan the named items. Paid action.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "rescan", cancel=False, mail_ids=mail_ids)

    @mcp.tool(annotations=_CANCEL_ACTION)
    async def cancel_rescan(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending ``request_rescan`` for the named items.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "rescan", cancel=True, mail_ids=mail_ids)

    @mcp.tool(annotations=_DESTRUCTIVE)
    async def request_shred(address_id: str, mail_ids: list[str]) -> object:
        """Request that PostScan Mail shred the named items (secure destruction). Use ``cancel_shred`` to undo while pending.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
        return await _action(address_id, "shred", cancel=False, mail_ids=mail_ids)

    @mcp.tool(annotations=_CANCEL_ACTION)
    async def cancel_shred(address_id: str, mail_ids: list[str]) -> object:
        """Cancel a pending ``request_shred`` for the named items.

        Upstream: <https://github.com/PostScanMail/api-docs#item-actions-address-scoped>
        """
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
