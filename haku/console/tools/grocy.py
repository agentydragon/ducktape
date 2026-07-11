"""haku-console's grocy-sf reference-data lookup: resolves the `grocy-sf` MCP server's
product/location/quantity-unit/product-group IDs to names for rendering pending
`stock_add` / `stock_consume` / `products_create` tool-call previews (see
`haku/console/frontend/grocy_tool_previews.tsx`), whose arguments accept either a name
or a numeric ID.

Unlike `gmail.py` / `google_calendar.py`, `grocy-sf` is a remote MCP server, not an
in-process one — there is
no FastMCP instance to build here, only this narrow read-only router. It reaches the
server via `mcp_approval.operator_authenticated_client`, the one public seam that module
exposes for this: authenticated exactly as an approved tool call for that server would be
(the requesting operator's own `operator_oauth` token), calling only grocy-sf's own
`*_list` read tools — never a generic "call any tool" escape hatch that would bypass the
approval queue.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from haku.console.deps import SettingsDep
from haku.console.mcp_approval import OAuthStoreDep, operator_authenticated_client

GROCY_SF_SERVER_ID = "grocy-sf"

router = APIRouter(prefix="/api/grocy-sf", tags=["grocy"])


class GrocyReferenceItem(BaseModel):
    id: int
    name: str


class GrocyReferenceResponse(BaseModel):
    products: list[GrocyReferenceItem]
    locations: list[GrocyReferenceItem]
    quantity_units: list[GrocyReferenceItem]
    product_groups: list[GrocyReferenceItem]


def _grocy_reference_items(structured_content: dict[str, Any] | None) -> list[GrocyReferenceItem]:
    """Extract `{id, name}` rows from a grocy-sf `*_list` tool's structured result.

    Reads `structured_content` (the raw, MCP-wire-shape result — FastMCP wraps a bare-list
    tool return value as `{"result": [...]}`) rather than the convenience `.data` property.
    `fastmcp==3.1.0` fails to reconstruct `.data` for loosely-typed list results (returns
    opaque placeholder objects instead of the underlying dicts) — `structured_content` has
    no such version-dependent reconstruction step, so it's the reliable source here.
    """
    assert structured_content is not None
    rows = structured_content["result"]
    return [GrocyReferenceItem(id=int(row["id"]), name=str(row["name"])) for row in rows]


@router.get("/reference")
async def grocy_sf_reference(
    request: Request, settings: SettingsDep, oauth_store: OAuthStoreDep
) -> GrocyReferenceResponse:
    """Read-only product/location/quantity-unit `{id, name}` lookups for rendering pending
    grocy-sf tool-call previews (`stock_add` / `stock_consume` / `products_create` accept
    either a name or a numeric ID) — deliberately narrow: calls only grocy-sf's own
    `products_list` / `locations_list` / `quantity_units_list` / `product_groups_list`
    read tools. Uses the requesting operator's own linked token — the same one their
    approvals execute with.
    """
    async with await operator_authenticated_client(GROCY_SF_SERVER_ID, request, settings, oauth_store) as client:
        products = await client.call_tool("products_list", {})
        locations = await client.call_tool("locations_list", {})
        quantity_units = await client.call_tool("quantity_units_list", {})
        product_groups = await client.call_tool("product_groups_list", {})
    return GrocyReferenceResponse(
        products=_grocy_reference_items(products.structured_content),
        locations=_grocy_reference_items(locations.structured_content),
        quantity_units=_grocy_reference_items(quantity_units.structured_content),
        product_groups=_grocy_reference_items(product_groups.structured_content),
    )
