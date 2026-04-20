"""MCP tool I/O types and server settings.

Pydantic models for what the MCP tools accept (input types) and return
(output types), plus ``ServerSettings``. These are *our* types — for types
modelling Grocy's API surface, see ``grocy_types.py``.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_infra.authentik_auth.auth import AuthentikAuthConfig
from x.grocy_mcp.grocy_types import ReadableEntityType, WriteableEntityType

MAX_BATCH_SIZE = 100

# ── Server settings ─────────────────────────────────────────────────────────


class ServerSettings(BaseSettings):
    """Config for the Grocy MCP server."""

    model_config = SettingsConfigDict(env_prefix="GROCY_MCP_", env_nested_delimiter="__")

    auth: AuthentikAuthConfig | None = Field(
        default=None, description="Authentik auth config. None means no auth — MCP server and Grocy both unprotected."
    )

    grocy_url: str = Field(description="URL of the Grocy instance. For production this is the outpost-protected URL.")
    host: str = "0.0.0.0"
    port: int = 8765

    grocy_timeout: float = Field(default=30.0, description="Timeout (seconds) for Grocy API requests.")
    max_batch_size: int = Field(default=MAX_BATCH_SIZE, description="Maximum items per batch tool call.")
    max_concurrent_requests: int = Field(default=4, description="Maximum parallel Grocy API requests within a batch.")
    max_retries: int = Field(default=2, description="Retry count for transient errors (timeouts, 5xx).")
    retry_base_delay: float = Field(default=0.5, description="Initial retry delay in seconds; doubles each attempt.")


# ── Shared field descriptions ───────────────────────────────────────────────

# Reused field descriptions for stock-mutation item fields. Stating the QU
# constraint in one place keeps `stock_add` / `stock_consume` /
# `stock_set` / `stock_transfer` honest about it.
PRODUCT_DESC = "Product. Name or ID."
QU_DESC = (
    "Quantity unit. Name or ID. Must match the product's stock QU or have a defined conversion "
    "(see the `quantity_unit_conversions` entity)."
)
BEST_BEFORE_DESC = (
    "Best-before / expiration date in `YYYY-MM-DD` format. "
    "Omit to use the product's `default_best_before_days`. "
    "Use `2999-12-31` for never-expires."
)
PRICE_DESC = "Per-unit price. Omit to use the last recorded price."
DETAIL_DESC = "`brief` returns only `id` + `name`. `full` returns every column."
DEFAULT_BBD_DESC = (
    "Auto-fill best-before date when `stock_add` omits it. "
    "-1 = never expires, 0 (default) = today, N > 0 = today + N days."
)
DUE_TYPE_DESC = (
    "How to treat the best-before date. "
    "1 (default) = 'best before' (possibly still safe after date, "
    "`get_expired_stock` ignores these). "
    "2 = 'expiration' (unsafe after date, `get_expired_stock` returns these). "
    "Use 2 for perishables (meat, dairy, medicine)."
)


# ── Shared input/output types ──────────────────────────────────────────────


class CreateItem(BaseModel):
    entity_type: WriteableEntityType
    body: dict[str, Any]


class CreateOk(BaseModel):
    kind: Literal["ok"] = "ok"
    created_object_id: int | None = None


class EditOk(BaseModel):
    kind: Literal["ok"] = "ok"
    object_id: int


class CreateError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class GetOk(BaseModel):
    kind: Literal["ok"] = "ok"
    entity_type: ReadableEntityType
    object_id: int
    data: dict[str, Any]


class GetError(BaseModel):
    kind: Literal["error"] = "error"
    entity_type: ReadableEntityType
    object_id: int
    error: str


# ── Stock overview response ────────────────────────────────────────────────


class StockEntry(BaseModel):
    """Compact stock overview entry. Names always included, no nested dicts."""

    product_id: int
    product_name: str
    amount: float
    amount_opened: float
    qu_name: str
    location_name: str
    best_before_date: date | None = None


# ── Stock mutation input/output ────────────────────────────────────────────


class AddItem(BaseModel):
    """One stock addition for ``stock_add``."""

    product: int | str = Field(description=PRODUCT_DESC)
    amount: float = Field(description="Amount to add, in `qu` units.")
    qu: int | str = Field(description=QU_DESC)
    location: int | str = Field(description="Storage location to add to. Name or ID.")
    best_before_date: date | None = Field(default=None, description=BEST_BEFORE_DESC)
    price: float | None = Field(default=None, description=PRICE_DESC)
    note: str | None = Field(default=None, description="Free-text note attached to the resulting stock entry.")


class ConsumeItem(BaseModel):
    """One stock consumption for ``stock_consume``."""

    product: int | str = Field(description=PRODUCT_DESC)
    amount: float = Field(description="Amount to consume, in `qu` units.")
    qu: int | str = Field(description=QU_DESC)
    location: int | str = Field(
        description="Location to consume from. Name or ID. Use `stock_get` first if you don't know where the product is."
    )
    spoiled: bool = Field(default=False, description="Mark the consumption as spoilage rather than normal use.")
    allow_subproduct_substitution: bool = Field(
        default=False,
        description="If the product has sub-products configured, allow Grocy to consume from sub-product stock when the product itself is short.",
    )


class SetItem(BaseModel):
    """One absolute-amount correction for ``stock_set``."""

    product: int | str = Field(description=PRODUCT_DESC)
    new_amount: float = Field(
        description="Absolute target stock amount in `qu` units. Grocy computes the delta and adds or removes to reach it."
    )
    qu: int | str = Field(description=QU_DESC)
    location: int | str = Field(description="Location for any units being added by the correction. Name or ID.")
    best_before_date: date | None = Field(
        default=None, description=f"Applies to units being added: {BEST_BEFORE_DESC.lower()}"
    )
    price: float | None = Field(
        default=None, description="Per-unit price for units being added. Omit to use the last recorded price."
    )


class StockOpOk(BaseModel):
    """Per-item success returned by ``stock_add`` / ``stock_consume`` / ``stock_set`` / ``stock_transfer``."""

    kind: Literal["ok"] = "ok"
    product_name: str
    transaction_id: str | None = Field(
        default=None, description="Grocy transaction ID. Pass to `transaction_undo` to revert this single op."
    )
    amount_delta: float | None = Field(
        default=None, description="Net stock change in stock QU. Negative for consume; null for transfer."
    )
    new_amount: float | None = Field(
        default=None,
        description="Resulting total stock for this product, in stock QU. Best-effort: null if the follow-up read fails.",
    )
    qu_name: str = Field(description="Name of the quantity unit the input `amount` was specified in.")
    stock_qu_name: str | None = Field(
        default=None,
        description=(
            "Name of the product's stock QU when a conversion was applied. Null on a normal success "
            "where the input `qu` already matched the product's stock QU (no conversion needed)."
        ),
    )
    location_name: str = Field(
        description="Name of the storage location. For `stock_transfer`, formatted as `from -> to`."
    )


class StockOpError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


# ── Stock entry input/output ──────────────────────────────────────────────


class StockEntryDetail(BaseModel):
    """Detailed stock entry with names."""

    entry_id: int
    product_id: int
    product_name: str
    amount: float
    qu_name: str
    location_name: str
    best_before_date: date | None = None
    purchased_date: date | None = None
    price: float | None = None
    open: bool = False
    note: str | None = None


class StockEntryOk(BaseModel):
    kind: Literal["ok"] = "ok"
    entry: StockEntryDetail


class StockEntryEditOk(StockEntryOk):
    changes: dict[str, dict[str, Any]] | None = Field(
        default=None, description="For edits: {field: {old: ..., new: ...}} diff of changed fields."
    )


class StockEntryError(BaseModel):
    kind: Literal["error"] = "error"
    entry_id: int | None = None
    error: str


class EditStockEntryField(StrEnum):
    """Nullable stock-entry fields that ``stock_entry_edit.clear_fields`` can null out.

    Other stock-entry fields (``amount``, ``location_id``, ``open``) are NOT
    NULL in Grocy's schema and must be assigned a value if changed.

    ``best_before_date`` is intentionally excluded: setting it to NULL makes
    the product invisible in Grocy's stock overview. Use
    ``best_before_date="2999-12-31"`` for never-expires instead.
    """

    PRICE = "price"
    PURCHASED_DATE = "purchased_date"
    NOTE = "note"


class EditStockEntryItem(BaseModel):
    """One stock-entry edit for ``stock_entry_edit``."""

    entry_id: int = Field(description="Stock-entry ID (from `stock_entries_list`).")
    amount: float | None = Field(default=None, description="New amount, in the entry's stock QU.")
    best_before_date: date | None = Field(
        default=None,
        description=(
            "New best-before / expiration date in `YYYY-MM-DD` format. "
            "Omit to keep the current value. Use `2999-12-31` for never-expires."
        ),
    )
    purchased_date: date | None = Field(default=None, description="New purchase date in `YYYY-MM-DD` format.")
    price: float | None = Field(default=None, description="New per-unit price.")
    location: int | str | None = Field(default=None, description="New storage location. Name or ID.")
    open: bool | None = Field(default=None, description="Mark the entry as opened or unopened.")
    note: str | None = Field(default=None, description="New free-text note.")
    clear_fields: set[EditStockEntryField] | None = Field(
        default=None,
        description=(
            "Fields to explicitly null out. Only the values in `EditStockEntryField` are nullable in "
            "Grocy's schema; everything else is NOT NULL and must be assigned a value if changed."
        ),
    )


# ── Reference data create types ────────────────────────────────────────────


class CreateProductItem(BaseModel):
    """One product to create via ``products_create``."""

    name: str = Field(description="Product name. Must be unique across all products.")
    stock_qu: int | str = Field(description="Stock quantity unit. Name or ID. The unit Grocy stores stock totals in.")
    location: int | str = Field(description="Default storage location. Name or ID.")
    purchase_qu: int | str | None = Field(
        default=None,
        description=(
            "Purchase quantity unit. Name or ID. Defaults to `stock_qu`. "
            "When different from `stock_qu`, Grocy auto-creates a "
            "product-specific factor=1 conversion (unless one already "
            "exists); update it afterwards if the real factor is not 1."
        ),
    )
    min_stock_amount: float = Field(
        default=0,
        description="Threshold (in stock QU) below which `get_below_minimum_stock` flags this product. 0 disables.",
    )
    consume_qu: int | str | None = Field(
        default=None, description="Consume quantity unit. Name or ID. Defaults to `stock_qu`."
    )
    default_best_before_days: int = Field(default=0, description=DEFAULT_BBD_DESC)
    due_type: Literal[1, 2] = Field(default=1, description=DUE_TYPE_DESC)
    parent_product: int | str | None = Field(default=None, description="Parent product for grouping. Name or ID.")
    product_group: int | str | None = Field(default=None, description="Product group / category. Name or ID.")
    description: str | None = Field(default=None, description="Free-text description.")


class CreateLocationItem(BaseModel):
    """One storage location to create via ``locations_create``."""

    name: str = Field(description="Location name (e.g. Pantry, Fridge, Freezer). Must be unique across all locations.")
    description: str | None = Field(default=None, description="Free-text description.")
    is_freezer: bool = Field(
        default=False,
        description=(
            "True if this is a freezer. Grocy applies frozen/thawed best-before-days adjustments to "
            "stock entries moved into or out of freezer locations."
        ),
    )


class CreateQuantityUnitItem(BaseModel):
    """One quantity unit to create via ``quantity_units_create``."""

    name: str = Field(description="Singular form (e.g. Liter, Bag, Piece). Must be unique across all units.")
    name_plural: str = Field(description="Plural form (e.g. Liters, Bags, Pieces).")
    description: str | None = Field(default=None, description="Free-text description.")
    plural_forms: str | None = Field(
        default=None,
        description="Optional Gettext-style plural rules for non-English locales (e.g. `nplurals=3; plural=…;`).",
    )


class CreateShoppingListItem(BaseModel):
    """One shopping-list metadata row to create via ``shopping_lists_create``.

    The list itself, not an item on a list — individual items go through
    ``shopping_list_items_add``.
    """

    name: str = Field(description="List name (e.g. Weekly, Costco run). Must be unique across all shopping lists.")
    description: str | None = Field(default=None, description="Free-text description.")


class CreateProductGroupItem(BaseModel):
    """One product group (category) to create via ``product_groups_create``."""

    name: str = Field(description="Group name (e.g. Dairy, Produce). Must be unique across all product groups.")
    description: str | None = Field(default=None, description="Free-text description.")


# ── Product edit types ─────────────────────────────────────────────────────


class EditProductField(StrEnum):
    """Nullable product fields that ``products_edit.clear_fields`` can null out.

    Other product fields are NOT NULL in Grocy's schema and cannot be cleared
    — to remove a product entirely, use ``product_delete``.
    """

    DESCRIPTION = "description"
    PRODUCT_GROUP = "product_group"
    PARENT_PRODUCT = "parent_product"
    CALORIES = "calories"


class EditProductItem(BaseModel):
    """One product edit operation for ``products_edit``."""

    product: int | str = Field(description="Product to edit. Name or ID.")
    name: str | None = Field(default=None, description="New name.")
    stock_qu: int | str | None = Field(default=None, description="New stock quantity unit. Name or ID.")
    location: int | str | None = Field(default=None, description="New default storage location. Name or ID.")
    purchase_qu: int | str | None = Field(default=None, description="New purchase quantity unit. Name or ID.")
    consume_qu: int | str | None = Field(default=None, description="New consume quantity unit. Name or ID.")
    min_stock_amount: float | None = Field(
        default=None, description="New low-stock threshold (in stock QU). 0 disables."
    )
    default_best_before_days: int | None = Field(default=None, description=DEFAULT_BBD_DESC)
    due_type: Literal[1, 2] | None = Field(default=None, description=DUE_TYPE_DESC)
    parent_product: int | str | None = Field(default=None, description="New parent product. Name or ID.")
    product_group: int | str | None = Field(default=None, description="New product group. Name or ID.")
    description: str | None = Field(default=None, description="New free-text description.")
    clear_fields: set[EditProductField] | None = Field(
        default=None,
        description=(
            "Fields to explicitly null out. Only the values in `EditProductField` are nullable in "
            "Grocy's schema; everything else is NOT NULL."
        ),
    )


# ── Shopping list types ────────────────────────────────────────────────────


class ShoppingItem(BaseModel):
    """One shopping-list item to add via ``shopping_list_items_add``.

    Three valid shapes: product-only (``product`` set), note-only (``note``
    set), or product + note (e.g. "Milk — buy the organic brand"). At
    least one of the two must be provided.
    """

    shopping_list: int | str = Field(description="Target shopping list. Name or ID.")
    product: int | str | None = Field(default=None, description="Product to add. Name or ID. Omit for note-only items.")
    amount: float = Field(default=1, description="Quantity to buy, in the product's stock QU.")
    note: str | None = Field(
        default=None,
        description="Free-text note. May accompany a `product` to qualify it, or stand alone for note-only items.",
    )

    @model_validator(mode="after")
    def _at_least_one_of_product_or_note(self) -> ShoppingItem:
        if self.product is None and self.note is None:
            raise ValueError("ShoppingItem requires at least one of `product` or `note`.")
        return self


class ShoppingListItemOk(BaseModel):
    """Per-item success returned by every shopping-list mutation tool."""

    kind: Literal["ok"] = "ok"
    item_id: int = Field(description="Grocy shopping-list item ID.")
    product_name: str | None = Field(default=None, description="Product name; null for note-only items.")
    amount: float
    qu_name: str | None = Field(default=None, description="Stock QU name; null for note-only items.")


class ShoppingListItemError(BaseModel):
    kind: Literal["error"] = "error"
    error: str


class EditShoppingListField(StrEnum):
    """Nullable shopping-list-item fields that ``shopping_list_item_edit.clear_fields`` can null out.

    Other fields (``amount``, ``done``, ``shopping_list_id``) are NOT NULL in
    Grocy's schema. To re-point an item at a different product, remove
    it via ``shopping_list_items_remove`` and re-add via
    ``shopping_list_items_add``.
    """

    NOTE = "note"


# ── Reference-data list result shapes ──────────────────────────────────────


class BriefListItem(BaseModel):
    """Minimal ``list_*`` row returned when ``detail="brief"``: just ``id`` + ``name``."""

    id: int
    name: str


class BriefQuantityUnit(BriefListItem):
    """Brief QU entry; adds ``name_plural`` since it's frequently needed at call sites."""

    name_plural: str | None = None


class FullProduct(BaseModel):
    """Full product row — Grocy's schema varies by version, so unlisted columns are passed through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


class FullLocation(BaseModel):
    """Full location row — ``is_freezer``, description, and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


class FullQuantityUnit(BaseModel):
    """Full QU row — ``name_plural``, description, and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str
    name_plural: str | None = None


class FullProductGroup(BaseModel):
    """Full product-group row — description and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str


class FullShoppingList(BaseModel):
    """Full shopping-list metadata row — description and any Grocy extras pass through."""

    model_config = ConfigDict(extra="allow")
    id: int
    name: str
