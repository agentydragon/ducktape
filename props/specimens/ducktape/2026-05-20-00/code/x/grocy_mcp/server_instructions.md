# Grocy MCP — cross-cutting conventions

This MCP server wraps [Grocy's](https://github.com/grocy/grocy) REST API.

## Data model

Every Grocy entity is accessible via the generic `entities_{create,list,get,update,delete}`
tools — including the ones the prose below doesn't expand on (recipes, chores, tasks,
batteries, equipment, meal plans, userfields, etc.). The typed tools below cover
the entities most day-to-day household ops touch; everything else is reachable through
the generic path.

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
quantity_units, product_groups, shopping_lists) a unique `name`.
For convenience, every tool parameter that targets an entity accepts either an `int` ID or
a `str` name. The MCP server looks up the corresponding ID by case-insensitively lookup.

## Typed tools vs generic `entities_*`

The typed wrappers resolve names for you, enforce per-operation shape, and
return typed results. Fall back to generic `entities_*` tools only when no
typed wrapper covers the entity.

| Entity              | Typed tools                                                                                  | Notes                                                                                         |
| ------------------- | -------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------- |
| products            | `products_create` / `products_list` / `products_edit` / `product_delete` / `products_merge`  | Full typed CRUD                                                                               |
| locations           | `locations_create` / `locations_list`                                                        | Create + list; edit/delete via generic                                                        |
| quantity_units      | `quantity_units_create` / `quantity_units_list`                                              | Grocy ships only `Piece`                                                                      |
| product_groups      | `product_groups_create` / `product_groups_list`                                              | Optional category on products                                                                 |
| shopping_lists      | `shopping_lists_{create,list}`                                                               | List **metadata**, not items                                                                  |
| shopping_list items | `shopping_list_{get,clear}` / `shopping_list_items_{add,remove}` / `shopping_list_item_edit` | Items live on their own table; typed tools hide the `shopping_lists` vs `shopping_list` split |
| stock               | `stock_{get,add,consume,set,transfer}`                                                       | Product-level totals and mutations                                                            |
| stock entries       | `stock_entries_list` / `stock_entry_edit`                                                    | Per-purchase line items                                                                       |
| transactions        | `transaction_undo`                                                                           | Undo a single stock mutation                                                                  |
| files               | `file_get` / `file_upload`                                                                   | Product pictures, attachments                                                                 |
| Everything else     | `entities_create` / `entities_list` / `entities_get` / `entity_update` / `entity_delete`     | Recipes, chores, tasks, batteries, barcodes, etc.                                             |

## Amount / `amount_opened` / `open`

- `amount` is the total on-hand in the product's stock QU. It is the only
  number you ever need to plan a recipe around.
- `amount_opened` is the subset of `amount` already marked opened (via
  `open_product_stock` or Grocy's auto-open behaviour). The opened subset
  is **still counted in `amount`** — it's not a separate bucket.
- On individual stock entries (returned by `stock_entries_list`),
  `open: true` flags that entry as opened. `stock_consume` and `stock_set`
  touch opened entries before unopened ones by default.

### Example

Four bottles on hand, one of them already opened: `amount = 4`, `amount_opened = 1`.
Calling `stock_consume(amount=1)` targets the opened one first, leaving `amount = 3`, `amount_opened = 0`.
A second `stock_consume(amount=1)` dips into an unopened bottle and (depending on product config)
may mark it opened, yielding `amount = 2`, `amount_opened = 1`.

## Quantity units and conversions

Stock mutations take a `qu` that must either match the product's stock QU
outright or have a defined conversion. See the `quantity_unit_conversions`
entity for what's defined. Product-specific rows win over global rows. When
no valid conversion exists, the mutation fails with an error naming the
stock QU and any available substitutes; create a
`quantity_unit_conversions` row via `entities_create` to unblock.

When `products_create` sets a `purchase_qu` different from `stock_qu`, Grocy
auto-creates a product-specific factor=1 `quantity_unit_conversions` row
(unless a matching conversion already exists, e.g. from a global default).
If the real factor is not 1 (e.g. 1 Bag = 500 g), update the auto-created
conversion via `entity_update` on `quantity_unit_conversions`.

## Absolute vs delta stock ops

- `stock_add` / `stock_consume` / `stock_transfer` take **deltas** — the
  amount added, consumed, or moved.
- `stock_set` takes **absolute** amounts — the target on-hand after the
  operation. Grocy figures out whether to add or remove to reach it. Use
  this for corrections / stock-takes. When `new_amount=0` the unit is
  irrelevant, so `qu` can be omitted (falls back to the product's stock
  QU); for any nonzero amount, `qu` is required.

## Parent products (variants)

A product can have a `parent_product` — this marks it as a **variant**
of the parent. Common example: a generic `Milk` parent with
`Alpura 1%` and `Lala Entera` as variants. Each variant keeps its own
barcodes, prices, and expiry dates; the parent is typically a pure
umbrella.

What the relationship unlocks:

- **Stock aggregation.** `stock_get` and the UI's stock overview show the
  parent's row with `amount_aggregated` summed from every variant (with
  per-variant QU conversion). Variants still show with their own counts.
- **Consume substitution.** `stock_consume` on the parent with
  `allow_subproduct_substitution=true` falls back to any variant when the
  parent itself is short (FIFO across variants). This is how "recipe
  needs milk, use whichever brand is in the fridge" works.
- **Shared low-stock rule.** Setting
  `cumulate_min_stock_amount_of_sub_products=1` on the parent means the
  parent's minimum is compared against the aggregated total — only the
  parent is flagged, not individual variants.
- **Virtual parent.** `no_own_stock=1` on the parent makes it an umbrella
  with no stock of its own; stock only lives on variants.

Constraints:

- Single-level nesting only — variants can't have their own variants.
- A product that is already a parent cannot itself be made a variant.

Typical `Milk` setup: create `Milk` with
`no_own_stock=1, cumulate_min_stock_amount_of_sub_products=1, min_stock_amount=2`,
then create `Alpura 1%` and `Lala Entera` with `parent_product="Milk"`
and their own barcodes and purchase QUs.

## Undo

Every stock mutation returns a `transaction_id`. Pass it to
`transaction_undo` to revert that operation. There is no cross-tool
atomicity; undo covers exactly the one mutation whose id you pass.

## Dates and expiry

All date parameters and returns use `YYYY-MM-DD`. Grocy's "never expires"
sentinel is `2999-12-31`.

**`default_best_before_days`** on a product controls what happens when
`stock_add` omits `best_before_date`:

- `-1` → never expires (`2999-12-31`)
- `0` (default) → **today** (not "disabled" — stock appears due immediately)
- `N > 0` → today + N days

For products that don't expire (salt, vinegar, etc.), set
`default_best_before_days = -1` at creation time, or pass
`best_before_date = "2999-12-31"` on every `stock_add`.

**`due_type`** on a product (settable via `products_edit` or
`entity_update`) controls how Grocy treats the best-before date:

- `1` (default) = "Best before date" — item is _possibly_ still safe
  after the date. Shows as warning. `get_expired_stock` does **not**
  return these items.
- `2` = "Expiration date" — item is _unsafe_ after the date. Shows as
  danger. `get_expired_stock` **only** returns `due_type=2` items.

Set `due_type=2` for perishables (meat, dairy, medicine) where past-date
means discard.

**Freezer transfers** silently change the best-before date using the
product's `default_best_before_days_after_freezing` (to freezer) or
`default_best_before_days_after_thawing` (from freezer). Both default
to `0` (= today). Set these to `-1` on products that don't expire when
frozen/thawed.

**Never set `best_before_date` to NULL.** A NULL date makes the product
invisible in Grocy's stock overview (the UI view filters out entries
where `best_before_date IS NULL`). Always use `2999-12-31` for
never-expires. The `stock_entry_edit` tool blocks clearing
`best_before_date` for this reason.
