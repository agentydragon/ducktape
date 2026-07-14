"""haku-console's grocy-sf reference-data lookup: resolves the `grocy-sf` MCP server's
product/location/quantity-unit/product-group/shopping-list IDs to names for rendering
pending `stock_add` / `stock_consume` / `products_create` / `shopping_list_*` tool-call
previews, whose arguments accept either a name or a numeric ID. Shopping-list
*item* IDs (`shopping_list_item_edit`, `shopping_list_items_remove`) resolve to their
product name / note through the per-item rows carried here.

Products carry more than `{id, name}`: `products_edit` renders an old→new diff, so the
preview needs each product's *current* field values (stock/purchase/consume unit, default
location, group, parent, min stock, shelf life, …). Those come back as raw Grocy IDs the
frontend resolves through the sibling reference maps — the same `resolveName` path the
other previews use. Shopping-list items carry their current amount/note/done for the same
reason: `shopping_list_item_edit` renders an old→new diff.

Unlike `gmail.py` / `google_calendar.py`, `grocy-sf` is a remote MCP server, not an
in-process one — there is
no FastMCP instance to build here, only this narrow read-only router. It reaches the
server via `mcp_approval.operator_authenticated_client`, the one public seam that module
exposes for this: authenticated exactly as an approved tool call for that server would be
(the requesting operator's own `operator_oauth` token), calling only grocy-sf's own
read tools (`*_list`, plus `shopping_list_get` per list for item detail) — never a generic
"call any tool" escape hatch that would bypass the approval queue.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from grocy_mcp.grocy_types import ProductRow
from haku.console.deps import SettingsDep
from haku.console.mcp_approval import operator_authenticated_client
from haku.console.mcp_operator_oauth import OAuthStoreDep
from haku.console.operator_auth import OperatorActorDep

GROCY_SF_SERVER_ID = "grocy-sf"

router = APIRouter(prefix="/api/grocy-sf", tags=["grocy"])


class GrocyReferenceItem(BaseModel):
    id: int
    name: str


class GrocyShoppingListItem(BaseModel):
    """One shopping-list item — `shopping_list_get`'s per-item shape, used to resolve the
    bare `item_id` arg of `shopping_list_item_edit` / `shopping_list_items_remove` to a
    product name or note, and to render the edit preview's old→new diff."""

    item_id: int
    product_name: str | None
    note: str | None
    amount: float
    qu_name: str | None
    done: bool


class GrocyReferenceResponse(BaseModel):
    # `products` carry each product's current field values (grocy_mcp's `ProductRow`) so the
    # `products_edit` preview can render an old→new diff; `shopping_list_items` likewise carry
    # each item's current amount/note/done for `shopping_list_item_edit`. The rest are
    # `{id, name}` name lookups.
    products: list[ProductRow]
    locations: list[GrocyReferenceItem]
    quantity_units: list[GrocyReferenceItem]
    product_groups: list[GrocyReferenceItem]
    shopping_lists: list[GrocyReferenceItem]
    shopping_list_items: list[GrocyShoppingListItem]


def _parse_rows[T: BaseModel](model: type[T], structured_content: dict[str, Any] | None) -> list[T]:
    """Validate a grocy-sf `*_list` tool's structured result into `model` rows.

    Reads `structured_content` (the raw, MCP-wire-shape result — FastMCP wraps a bare-list tool
    return value as `{"result": [...]}`) rather than the convenience `.data` property, keeping
    this adapter independent of FastMCP's reconstruction rules for loosely typed list results.
    Each row is a raw Grocy object; `model` selects and coerces the fields it declares.
    """
    assert structured_content is not None
    return [model.model_validate(row) for row in structured_content["result"]]


@router.get("/reference")
async def grocy_sf_reference(
    settings: SettingsDep, oauth_store: OAuthStoreDep, actor: OperatorActorDep
) -> GrocyReferenceResponse:
    """Read-only reference lookups for rendering pending grocy-sf tool-call previews whose
    arguments accept either a name or a numeric ID — deliberately narrow: calls only
    grocy-sf's own read tools (`products_list` / `locations_list` / `quantity_units_list` /
    `product_groups_list` / `shopping_lists_list`, plus `shopping_list_get` per list to pull
    item detail). Products come back in `full` detail so `products_edit` can render an
    old→new diff; the per-list `shopping_list_get` calls flatten every shopping-list item so
    `shopping_list_item_edit` / `shopping_list_items_remove` can resolve bare `item_id`s to
    product names/notes. Uses the requesting operator's own linked token — the same one their
    approvals execute with.
    """
    async with await operator_authenticated_client(GROCY_SF_SERVER_ID, actor, settings, oauth_store) as client:
        products = await client.call_tool("products_list", {"detail": "full"})
        locations = await client.call_tool("locations_list", {})
        quantity_units = await client.call_tool("quantity_units_list", {})
        product_groups = await client.call_tool("product_groups_list", {})
        shopping_lists = await client.call_tool("shopping_lists_list", {})
        shopping_list_rows = _parse_rows(GrocyReferenceItem, shopping_lists.structured_content)
        # `shopping_list_get` returns a dict, which FastMCP inlines into structured content
        # (only non-dict returns get the {"result": …} wrapper), so each list's items sit at
        # the top level under "items" — not under "result" like the *_list tools above.
        shopping_list_items: list[GrocyShoppingListItem] = []
        for sl in shopping_list_rows:
            list_result = await client.call_tool("shopping_list_get", {"shopping_list": sl.id})
            shopping_list_items.extend(
                GrocyShoppingListItem.model_validate(row) for row in list_result.structured_content["items"]
            )
    return GrocyReferenceResponse(
        products=_parse_rows(ProductRow, products.structured_content),
        locations=_parse_rows(GrocyReferenceItem, locations.structured_content),
        quantity_units=_parse_rows(GrocyReferenceItem, quantity_units.structured_content),
        product_groups=_parse_rows(GrocyReferenceItem, product_groups.structured_content),
        shopping_lists=shopping_list_rows,
        shopping_list_items=shopping_list_items,
    )
