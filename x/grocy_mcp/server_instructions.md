# Grocy MCP — cross-cutting conventions

This server wraps [Grocy's](https://github.com/grocy/grocy) REST API as an
MCP tool surface. Conventions that apply across tools live here so individual
tool docstrings can stay focused on what each tool does.

## Data model

Every Grocy entity is accessible via the generic `entities_create` /
`entities_list` / `entities_get` / `entity_update` / `entity_delete` tools
— including the ones the prose below doesn't expand on (recipes, chores,
tasks, batteries, equipment, meal plans, userfields, etc.). The typed tools
below cover the entities most day-to-day household ops touch; everything
else is reachable through the generic path.

Core entities and how they relate:

```
product ─┐
         ├── qu_id_stock       → quantity_unit   (the unit Grocy stores stock in)
         ├── qu_id_purchase    → quantity_unit
         ├── location_id       → location        (default location for new stock)
         ├── product_group_id  → product_group   (optional category)
         └── 0..n stock_entry  → (amount, best_before_date, price, open, …)
                                 stock_entry.location_id → location (may differ from default)

quantity_unit_conversion
  from_qu_id  → quantity_unit
  to_qu_id    → quantity_unit
  product_id  → product | null    (null = global; non-null = product-specific, wins over global)
  factor: float                   (multiply input-qu amount by factor to get to-qu amount)

shopping_lists (metadata)        ← one row per named list ("Weekly", "Costco run", …)
  0..n shopping_list (items)     ← item rows; each row points back at a shopping_list
    shopping_list_id → shopping_lists
    product_id       → product | null    (note-only items have product_id = null)
    amount, note, done
```

Entity names have a confusing quirk: **`shopping_lists`** is the
list-metadata table, **`shopping_list`** is the item table. The typed
shopping-list tools all take a list by name or ID and hide this split;
`shopping_list_get` returns both the list metadata and every item on it.

Every entity is identified by an integer `id` and (for products, locations,
quantity_units, product_groups, shopping_lists) a `name` that Grocy enforces
`UNIQUE` on at the database layer. Name lookups here are case-insensitive,
but the stored casing is preserved when names come back in tool responses.

## Typed tools vs generic `entities_*`

The typed wrappers resolve names for you, enforce per-operation shape, and
return typed results. Reach for the generic `entities_*` path only when no
typed wrapper covers the entity.

| Entity              | Typed tools                                                                                                                        | Notes                                                                                         |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| products            | `products_create` / `products_list` / `product_edit` / `product_delete` / `products_merge`                                         | Full typed CRUD                                                                               |
| locations           | `locations_create` / `locations_list`                                                                                              | Create + list; edit/delete via generic                                                        |
| quantity_units      | `quantity_units_create` / `quantity_units_list`                                                                                    | Grocy ships only `Piece`                                                                      |
| product_groups      | `product_groups_create` / `product_groups_list`                                                                                    | Optional category on products                                                                 |
| shopping_lists      | `shopping_lists_create` / `shopping_lists_list`                                                                                    | List **metadata**, not items                                                                  |
| shopping_list items | `shopping_list_get` / `shopping_list_items_add` / `shopping_list_item_edit` / `shopping_list_items_remove` / `shopping_list_clear` | Items live on their own table; typed tools hide the `shopping_lists` vs `shopping_list` split |
| stock               | `stock_get` / `stock_add` / `stock_consume` / `stock_set` / `stock_transfer`                                                       | Product-level totals and mutations                                                            |
| stock entries       | `stock_entries_list` / `stock_entry_edit`                                                                                          | Per-purchase line items                                                                       |
| transactions        | `transaction_undo`                                                                                                                 | Undo a single stock mutation                                                                  |
| files               | `file_get` / `file_upload`                                                                                                         | Product pictures, attachments                                                                 |
| Everything else     | `entities_create` / `entities_list` / `entities_get` / `entity_update` / `entity_delete`                                           | Recipes, chores, tasks, batteries, barcodes, etc.                                             |

## Passing references

Every tool parameter that targets an entity accepts either an integer ID or
a name string — fields typed `int | str`. The server resolves names to IDs
via the `/objects/<entity>` tables on every call (no caching between tool
invocations, since other clients can mutate Grocy behind us). Prefer names
for readability; fall back to IDs when you've just created something and
want to avoid a name round-trip.

## Amount / `amount_opened` / `open`

- `amount` is the total on-hand in the product's stock QU. It is the only
  number you ever need to plan a recipe around.
- `amount_opened` is the subset of `amount` already marked opened (via
  `open_product_stock` or Grocy's auto-open behaviour). The opened subset
  is **still counted in `amount`** — it's not a separate bucket.
- On individual stock entries (returned by `stock_entries_list`),
  `open: true` flags that entry as opened. `stock_consume` and `stock_set`
  touch opened entries before unopened ones by default.

**Worked example.** Four bottles on hand, one of them already opened:
`amount = 4`, `amount_opened = 1`. Calling `stock_consume(amount=1)`
targets the opened bottle first, leaving `amount = 3`, `amount_opened = 0`.
A second `stock_consume(amount=1)` dips into an unopened bottle and
(depending on product config) may mark it opened, yielding
`amount = 2`, `amount_opened = 1`.

## Quantity units and conversions

Stock mutations take a `qu` that must either match the product's stock QU
outright or have a defined conversion. See the `quantity_unit_conversions`
entity for what's defined. Product-specific rows win over global rows. When
no valid conversion exists, the mutation fails with an error naming the
stock QU and any available substitutes; create a
`quantity_unit_conversions` row via `entities_create` to unblock.

## Absolute vs delta stock ops

- `stock_add` / `stock_consume` / `stock_transfer` take **deltas** — the
  amount added, consumed, or moved.
- `stock_set` takes **absolute** amounts — the target on-hand after the
  operation. Grocy figures out whether to add or remove to reach it. Use
  this for corrections ("I just counted the pantry, actually have X") to
  avoid computing deltas yourself.

## Undo

Every stock mutation returns a `transaction_id`. Pass it to
`transaction_undo` to revert that one operation. There is no cross-tool
atomicity; undo covers exactly the one mutation whose id you hand back.

## Dates

All date parameters and returns use `YYYY-MM-DD`. `best_before_date` is
nullable on stock entries and on product defaults; omit it when there's no
meaningful expiry (salt, etc.).
