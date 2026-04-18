# Grocy MCP: Agent Interaction Design

De novo design for how an AI agent should interact with a grocery inventory
system via MCP. Not constrained by Grocy's existing API shapes or OpenAPI
spec — this is the ideal interface, from which we derive implementation.

## Design Principles

1. **Hard to use incorrectly.** If the agent omits something important
   (like a location or unit), that's an error — not a silent default that
   puts stock in the wrong place. The schema and validation should make
   silent mistakes impossible.

2. **No surprising footguns.** An "edit" should not null out fields the
   agent didn't mention. A "create" should not silently pick defaults for
   things the agent should decide. Operations should do what the name implies
   and nothing more.

3. **Common workflows should be natural.** Adding a product to stock
   shouldn't require 6 round-trips. But consolidation shouldn't come at the
   cost of safety — a single call that does too much implicitly is worse than
   two explicit calls.

4. **Descriptions are the #1 lever.** Anthropic's research: "Provide
   extremely detailed descriptions. This is by far the most important factor
   in tool performance." Each tool description must cover: when to use it,
   what to provide, what comes back, and what can go wrong. 3-4 sentences
   minimum.

5. **Return only what's needed for the next step.** Responses should be
   compact and human-readable. Always include names alongside IDs so the
   agent can present results to the user without extra lookups. Don't return
   30-field product dicts when the agent needs a name and an amount.

6. **Names are first-class.** The user says "cucumbers", not "product 42".
   Every tool that accepts an ID should also accept a name. Ambiguity and
   not-found produce clear errors listing the alternatives.

7. **Units are always explicit.** Every request and response that involves
   a quantity must name the unit. This prevents the agent from silently
   adding 5 kilograms when the user meant 5 pieces.

## Workflow Scenarios

### Scenario 1: "Add 5 kilos of cucumbers to the fridge"

The user mentions a product, a quantity with a unit, and a location.
The agent doesn't know whether "cucumbers" exists in inventory yet.

```
Agent: stock_get()
  Response:
    [{product_name: "Rice", amount: 2.0, qu_name: "Kilogram",
      location_name: "Pantry", best_before_date: "2026-12-01"},
     {product_name: "Milk", amount: 3.0, qu_name: "Liter",
      location_name: "Fridge", best_before_date: "2026-04-25"}]

  Agent sees: no cucumbers in stock. Needs to check if the product
  exists but is just unstocked, and check that the location and unit
  exist.

Agent: products_list()
  Response:
    [{id: 1, name: "Rice"}, {id: 10, name: "Milk"}, ...]

  No "Cucumbers" in the list. Agent needs to create it.
  But first: does the unit "Kilogram" and location "Fridge" exist?
  The agent already saw "Kilogram" and "Fridge" in the stock response
  above — but if stock were empty, it would need:

Agent: locations_list()
  Response:
    [{id: 1, name: "Pantry"}, {id: 7, name: "Fridge"}, {id: 12, name: "Freezer"}]

Agent: quantity_units_list()
  Response:
    [{id: 1, name: "Piece"}, {id: 3, name: "Kilogram"}, {id: 5, name: "Liter"}]

  Now the agent has everything it needs to create the product.

Agent: create_product(
    name: "Cucumbers",
    stock_qu: "Kilogram",
    location: "Fridge"
  )
  Response:
    {product_id: 42, name: "Cucumbers",
     stock_qu: "Kilogram", default_location: "Fridge"}

  Notes on this tool:
  - `stock_qu` accepts a name (or ID). It's required — no default.
  - `location` is the default storage location. Required — no default.
  - purchase_qu defaults to stock_qu (90% of the time they're the same).
    Agent can override: `purchase_qu: "Crate"` if buying in bulk units.
  - Optional fields: `min_stock_amount` (for low-stock alerts),
    `default_best_before_days` (auto-calculates expiry on add),
    `product_group` (int|str), `description`.
  - Response confirms what was created with names, not just IDs.

Agent: stock_add(
    product: "Cucumbers",
    amount: 5,
    qu: "Kilogram",
    location: "Fridge"
  )
  Response:
    {kind: "ok", product_name: "Cucumbers", amount_added: 5.0,
     new_amount: 5.0, qu_name: "Kilogram", location_name: "Fridge",
     transaction_id: "abc123"}

  Notes:
  - `product` accepts name or ID. Errors on ambiguity/not-found.
  - `qu` is required. Must match the product's stock QU.
  - `location` is required. No silent default to product's default location.
    The agent explicitly chose "Fridge" because the user said "fridge".
  - Response includes names for everything. Agent can immediately tell
    the user: "Added 5 Kilogram of Cucumbers to Fridge. New total: 5.0 kg."
  - `transaction_id` enables undo if the user says "actually, undo that".
```

**Total: 3-5 calls** depending on whether the agent already knows the
product/location/QU exist. The critical path (product exists, agent knows
IDs from prior context) is **1 call**.

### Scenario 2: "What's in stock?"

```
Agent: stock_get()
  Response:
    [{product_id: 42, product_name: "Cucumbers", amount: 5.0,
      qu_name: "Kilogram", location_name: "Fridge",
      best_before_date: "2026-05-01"},
     {product_id: 10, product_name: "Rice", amount: 2.0,
      qu_name: "Kilogram", location_name: "Pantry",
      best_before_date: null},
     {product_id: 15, product_name: "Red Wine", amount: 6.0,
      qu_name: "Bottle", location_name: "Cellar",
      best_before_date: null}]
```

Notes:

- Compact by default: name, amount, unit, location, expiry. That's what
  the user cares about.
- No 30-field product dicts. No nested `product.name` extraction.
- Agent can immediately format this as a table for the user.
- If the agent needs full product details (for editing, cross-referencing),
  it uses `get_product(product: "Rice")` instead.

### Scenario 3: "What's about to expire?"

```
Agent: get_expiring_stock(days_ahead: 7)
  Response:
    [{product_name: "Milk", amount: 1.5, qu_name: "Liter",
      location_name: "Fridge", best_before_date: "2026-04-20",
      days_until_expiry: 3},
     {product_name: "Yogurt", amount: 4.0, qu_name: "Piece",
      location_name: "Fridge", best_before_date: "2026-04-22",
      days_until_expiry: 5}]
```

Notes:

- Dedicated tool for a common query. Grocy has `GET /stock/volatile` which
  returns overdue/expiring/expired/below-min — but it bundles 4 different
  concerns. Better to expose the most common one (expiring soon) as a
  focused tool.
- `days_until_expiry` is pre-computed — the agent doesn't need to do date
  math.

### Scenario 4: "We used 2 kilograms of rice from the pantry"

```
Agent: stock_consume(
    product: "Rice",
    amount: 2,
    qu: "Kilogram",
    location: "Pantry"
  )
  Response:
    {kind: "ok", product_name: "Rice", amount_consumed: 2.0,
     new_amount: 0.0, qu_name: "Kilogram",
     location_name: "Pantry",
     transaction_id: "def456"}
```

Notes:

- `location` is required (same as `stock_add`). This prevents accidentally
  consuming from the wrong location — important when one Grocy instance
  manages multiple households/locations.
- The agent must know where the stock is. If unsure, `stock_get` shows
  which locations have stock of a given product.

### Scenario 5: "Change the price of that rice entry to 9.99"

```
Agent: stock_entry_edit(entry_id: 42, price: 9.99)
  Response:
    {kind: "ok", entry_id: 42, product_name: "Rice",
     qu_name: "Kilogram",
     changes: {price: {old: 4.50, new: 9.99}},
     current: {amount: 5.0, price: 9.99, best_before_date: "2026-05-01",
               location_name: "Pantry", open: false}}
```

Notes:

- **Partial update.** Only `price` was specified, only `price` changed.
  The server reads the current entry, merges the change, and writes back.
  No risk of the agent accidentally zeroing `amount` or `best_before_date`.
- Response shows `changes` (old→new diff) so the agent can confirm to the
  user exactly what changed.
- Response shows `current` (full state after edit) so the agent has the
  updated view without a follow-up read.

### Scenario 6: "Move 3 bottles of wine from the cellar to the fridge"

```
Agent: stock_transfer(
    product: "Red Wine",
    amount: 3,
    qu: "Bottle",
    from_location: "Cellar",
    to_location: "Fridge"
  )
  Response:
    {kind: "ok", product_name: "Red Wine",
     amount_transferred: 3.0, qu_name: "Bottle",
     from_location: "Cellar", to_location: "Fridge",
     transaction_id: "ghi789"}
```

Notes:

- Both locations required. No defaults.
- Unit required and validated against product's stock QU.
- Names accepted for product and both locations.

### Scenario 7: "Add things we're running low on to the shopping list, and a reminder to check paper towels"

```
Agent: get_below_minimum_stock()
  Response:
    [{product_name: "Milk", amount: 0.5, min_amount: 2.0,
      qu_name: "Liter", deficit: 1.5},
     {product_name: "Eggs", amount: 2.0, min_amount: 12.0,
      qu_name: "Piece", deficit: 10.0}]

Agent: shopping_list_items_add(items: [
    {product: "Milk", amount: 1.5},
    {product: "Eggs", amount: 10},
    {note: "Check if we need paper towels"}
  ])
  Response:
    [{kind: "ok", item_id: 50, product_name: "Milk",
      amount: 1.5, qu_name: "Liter"},
     {kind: "ok", item_id: 51, product_name: "Eggs",
      amount: 12.0, qu_name: "Piece"},
     {kind: "ok", item_id: 52, product_name: null,
      amount: 1.0, qu_name: null}]
```

Notes:

- No QU needed on add — shopping list uses the product's purchase QU.
- Note-only items (no product) are first-class.
- Each item gets an `item_id` back — needed for edit/remove since the
  same product can appear multiple times on a list.
- `shopping_list` is required — no default. The agent must name the list.

### Scenario 7b: "What's on the shopping list? Remove the eggs."

```
Agent: shopping_list_get()
  Response:
    {name: "Shopping list", description: null,
     items: [
       {item_id: 50, product_name: "Milk", product_id: 10,
        amount: 1.5, qu_name: "Liter", note: null, done: false},
       {item_id: 51, product_name: "Eggs", product_id: 22,
        amount: 12.0, qu_name: "Piece", note: null, done: false},
       {item_id: 52, product_name: null, product_id: null,
        amount: 1.0, qu_name: null,
        note: "Check if we need paper towels"}]}

Agent: shopping_list_items_remove(item_ids: [51])
  Response:
    [{kind: "ok", item_id: 51,
      product_name: "Eggs", amount: 12.0, qu_name: "Piece"}]
```

### Scenario 8: Agent creates a product but forgets a required field

```
Agent: create_product(name: "Butter", location: "Fridge")
  Response:
    {kind: "error",
     error: "Missing required field: stock_qu (the quantity unit for
             tracking stock). Available units: Piece, Kilogram, Liter, Pack.
             Example: stock_qu: \"Piece\""}
```

Notes:

- The error is actionable: names the missing field, explains what it's for,
  lists available values, gives an example.
- The agent doesn't need to guess or consult training data. The error
  teaches the correct usage.

### Scenario 9: Agent uses wrong unit (no conversion exists)

```
Agent: stock_add(product: "Rice", amount: 5, qu: "Liter", location: "Pantry")
  Response:
    {kind: "error",
     error: "No conversion from 'Liter' to stock QU 'Kilogram' for 'Rice'.
             Use qu: \"Kilogram\" directly, or create a QU conversion."}
```

The server accepts any QU that either matches the stock QU or has a valid
conversion path. If neither, the error names the stock QU and suggests
the fix.

### Scenario 9b: Agent uses purchase QU (conversion exists)

```
Agent: stock_add(product: "Beer", amount: 2, qu: "Crate", location: "Cellar")
  Response:
    {kind: "ok", product_name: "Beer",
     amount_added: 2.0, qu_name: "Crate",
     stock_amount_added: 48.0, stock_qu_name: "Bottle",
     new_amount: 72.0, location_name: "Cellar",
     transaction_id: "xyz789"}
```

The agent said "2 crates", the server converted to 48 bottles (using the
product's QU conversion: 1 crate = 24 bottles) and sent that to Grocy.
The response reports both the input QU and the stock QU amounts.

### Scenario 10: Agent references non-existent product name

Grocy enforces unique product names, so there's no ambiguity — but the
agent might use an inexact name ("Wine" when the product is "Red Wine").

```
Agent: stock_consume(product: "Wine", amount: 1, qu: "Bottle")
  Response:
    {kind: "error",
     error: "No product named 'Wine'. Similar products:
             - 'Red Wine' (id: 15)
             - 'White Wine' (id: 16)
             Use the exact name or product ID."}
```

Notes:

- Name resolution is exact (case-insensitive). No fuzzy matching in the
  resolution itself — that would be a source of silent mistakes.
- But the error message suggests close matches to help the agent recover
  in one retry instead of needing a separate `search_products` call.

### Scenario 11: "Undo that last stock change"

```
Agent: transaction_undo(transaction_id: "abc123")
  Response:
    {kind: "ok", undone: "stock_add",
     product_name: "Cucumbers", amount_reversed: 5.0,
     qu_name: "Kilogram"}
```

Every mutating stock operation returns a `transaction_id`. Undo is
a single call. The response confirms what was undone.

### Scenario 12: "Show me the stock entries for rice"

```
Agent: stock_entries_list(product: "Rice")
  Response:
    [{entry_id: 100, amount: 2.0, qu_name: "Kilogram",
      location_name: "Pantry", best_before_date: "2026-12-01",
      purchased_date: "2026-03-15", price: 3.50, open: false},
     {entry_id: 101, amount: 1.0, qu_name: "Kilogram",
      location_name: "Fridge", best_before_date: "2026-06-01",
      purchased_date: "2026-04-01", price: 4.00, open: true}]
```

Notes:

- Lookup by product name (or ID), not by entry ID. The common question is
  "what entries does this product have?" not "show me entry 42".
- Entry IDs are returned for use with `stock_entry_edit`.
- Compact: names, not nested dicts.

## Proposed Tool Inventory

### Stock Operations (the core)

| Tool               | Purpose                    | Required Params                                 | Notes                                                                     |
| ------------------ | -------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------- |
| `stock_get`        | Current stock overview     | (none; optional product/location filters)       | Compact. Filters: `products: list[int\|str]`, `locations: list[int\|str]` |
| `stock_add`        | Add stock                  | product, amount, qu, location                   | All `int\|str`. All required, no defaults. Batch.                         |
| `stock_consume`    | Consume stock              | product, amount, qu, location                   | All required — prevents cross-household mistakes.                         |
| `stock_transfer`   | Move between locations     | product, amount, qu, from_location, to_location | Both locations required.                                                  |
| `stock_set`        | Set absolute amount        | product, new_amount, qu, location               | All required — same rationale as consume.                                 |
| `open_stock`       | Mark stock entry as opened | product, amount, qu                             |                                                                           |
| `transaction_undo` | Undo a stock operation     | transaction_id                                  |                                                                           |

### Stock Entry Operations

| Tool                 | Purpose                    | Required Params                | Notes                                                                                             |
| -------------------- | -------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------- |
| `stock_entries_list` | List entries for a product | product or entry_ids           | By product (common) or by entry ID (editing)                                                      |
| `stock_entry_edit`   | Partial-update an entry    | entry_id + changed fields only | Fields: amount, price, best_before_date, purchased_date, location, open, note. Server-side merge. |

### Product Management

| Tool             | Purpose                  | Required Params          | Notes                                                                            |
| ---------------- | ------------------------ | ------------------------ | -------------------------------------------------------------------------------- |
| `products_list`  | All products             | (none)                   | `detail: "brief"\|"full"`, default brief (id + name)                             |
| `create_product` | Create a new product     | name, stock_qu, location | Optional: min_stock_amount, default_best_before_days, product_group, description |
| `product_edit`   | Partial-update a product | product + changed fields |                                                                                  |
| `product_delete` | Delete a product         | product                  |                                                                                  |

### Reference Data

| Tool                  | Purpose               | Notes                                                              |
| --------------------- | --------------------- | ------------------------------------------------------------------ |
| `locations_list`      | All storage locations | `detail: "brief"\|"full"`, default brief (id + name)               |
| `quantity_units_list` | All quantity units    | `detail: "brief"\|"full"`, default brief (id + name + name_plural) |
| `product_groups_list` | All product groups    | `detail: "brief"\|"full"`, default brief (id + name)               |

### Queries

| Tool                      | Purpose                      | Notes                            |
| ------------------------- | ---------------------------- | -------------------------------- |
| `get_expiring_stock`      | Items expiring within N days | Pre-computed `days_until_expiry` |
| `get_below_minimum_stock` | Items below min stock        | Shows deficit                    |
| `get_expired_stock`       | Already expired items        |                                  |

### Shopping List

Grocy supports multiple named shopping lists (`shopping_lists` table: id,
name, description; names are unique). A default list named "Shopping list"
(id=1) always exists. Items (`shopping_list` table) link to a list via
`shopping_list_id`. Items can be product-linked or note-only (`product_id`
is nullable). The same product can appear multiple times on a list (no
uniqueness constraint), so the item's own `id` is the real key.

| Tool                         | Purpose                        | Notes                                                                                |
| ---------------------------- | ------------------------------ | ------------------------------------------------------------------------------------ |
| `shopping_list_get`          | Get items on a shopping list   | `shopping_list: int\|str  (required)`. Returns list metadata + items.                |
| `shopping_list_items_add`    | Add items to a shopping list   | Batch. Product-linked or note-only. `shopping_list` per item (required, no default). |
| `shopping_list_item_edit`    | Partial-update a shopping item | By `item_id`. Can change amount, note, done. Not product (delete + re-add instead).  |
| `shopping_list_items_remove` | Remove items by item ID        | `item_ids: list[int]`. Item ID is the key (product is not unique).                   |
| `shopping_list_clear`        | Clear all items from a list    | `shopping_list: int\|str  (required)`.                                               |

To discover what shopping lists exist, use the generic
`entities_list(["shopping_lists"])` — it's a rare operation.

### Generic Entity CRUD (escape hatch)

For entities without dedicated tools (barcodes, shopping list metadata,
QU conversions, recipes, etc.). Covered by e2e tests (create location +
QU + product, list, get by ID, update, delete).

| Tool              | Purpose              | Notes                                                                   |
| ----------------- | -------------------- | ----------------------------------------------------------------------- |
| `entities_create` | Batch create         | `[{entity_type, body: dict}]`. Body is opaque — agent must know fields. |
| `entities_list`   | Batch list by type   | `[entity_type]` → `{type: [dicts]}`. `detail: "brief"\|"full"`.         |
| `entities_get`    | Batch get by ID      | `entity_type, [ids]` → `[ok\|error]`.                                   |
| `entity_update`   | Update single entity | **WARNING: full replace, not partial update.** See note below.          |
| `entity_delete`   | Delete single entity | By entity type + ID.                                                    |

**`entity_update` is a full replace.** The tool description must warn the
agent prominently: "This replaces the entire entity. You must include ALL
fields, not just the ones you want to change. Fields you omit will be set
to null. First use `entities_get` to read the current state, then send the
complete object with your changes applied." This is acceptable for simple
entities (locations, QUs — 1-3 fields) but dangerous for complex ones.
Products have a dedicated `product_edit` with partial-update semantics
specifically to avoid this.

### System

| Tool              | Purpose            | Notes         |
| ----------------- | ------------------ | ------------- |
| `get_system_info` | Grocy version etc. | Rarely needed |

## Future Work

Features not covered by dedicated tools, roughly ordered by usefulness:

1. **Product barcodes** (`product_barcodes`) — scan-to-lookup/add workflows.
   Currently reachable via generic entity CRUD.
2. **QU conversions** (`quantity_unit_conversions`) — "1 bottle = 0.75 L".
   Needed when purchase QU differs from stock QU. Generic CRUD works.
3. **Recipes** (`recipes`, `recipes_pos`, `recipes_nestings`) — "what can
   I make?", recipe fulfillment, consume-recipe. Currently disabled.
4. **Meal plan** (`meal_plan`, `meal_plan_sections`) — weekly planning.
   Currently not exposed.
5. **Tasks** (`tasks`, `task_categories`) — generic todo. May overlap
   with other agent tools.
6. **Stock log** (`stock_log`) — history view, useful for debugging.

## Key Design Decisions

### Names vs IDs — single `int | str` fields

Every tool that accepts a reference to a product, location, or quantity unit
uses a **single field** typed `int | str`. If the agent passes an int, it's
treated as an ID. If a string, it's resolved by name. No separate
`product_id` / `product_name` pair — just `product: int | str`.

```
stock_add(product: "Rice", ...)      # resolved by name
stock_add(product: 42, ...)          # used as ID directly
create_product(location: "Fridge", stock_qu: "Kilogram")  # both by name
create_product(location: 7, stock_qu: 3)                   # both by ID
```

Resolution rules for string values:

- Exact case-insensitive match → use it
- No match → error listing similar/available names
- ID (int) always works as a fallback

**Grocy enforces unique names** at the database level (`name TEXT NOT NULL
UNIQUE`) for products, locations, and quantity units. This means name-based
resolution is unambiguous by construction — a name maps to exactly 0 or 1
entity, never multiple. Our server-side resolver should still defensively
check for duplicates (in case Grocy changes this constraint), but the
ambiguity case should not arise in practice.

### Server-Side QU Conversion

Grocy's stock API (`add`, `consume`, `inventory`) does **not** accept a
`qu_id` parameter — all amounts must be in the product's stock QU. But
users naturally say "add 2 crates of beer" when the stock QU is bottles.

The MCP server handles this transparently:

1. The agent passes `qu: "Crate"` (or any QU with a valid conversion).
2. The server looks up the conversion factor from Grocy's
   `quantity_unit_conversions_resolved` table (product-specific or global).
3. The server converts the amount to stock QU and sends that to Grocy.
4. The response reports both the original amount/QU and the converted
   stock amount: `{amount_added: 2.0, qu_name: "Crate",
stock_amount_added: 48.0, stock_qu_name: "Bottle", new_amount: 72.0}`.

If no conversion path exists, the error names the stock QU and lists
available conversions for the product.

Validation flow: resolve QU by name/ID → check if it matches stock QU
(pass through) or if a conversion exists (convert) → reject if neither.

### Required Locations on All Mutations

Location is **required** on `stock_add`, `stock_consume`, `stock_set`,
and `stock_transfer` (both from and to). No silent defaults.

Rationale: one Grocy instance may manage multiple households/locations.
Omitting location and relying on Grocy's default (product's `location_id`
or FIFO) risks putting stock in the wrong household. The agent should
always confirm where stock is going or coming from.

If the agent doesn't know the location, it should call `stock_get()` first
to see where existing stock is, or `locations_list()` to see what's
available.

### Partial Updates with `clear_fields`

All "edit" operations are **partial updates**. The agent sends only the
fields it wants to change. The server reads the current state, merges, and
writes back. The response shows both the changes (diff) and the full
current state.

This is critical for safety: the agent should never need to copy-paste
fields it doesn't understand. An agent editing a price should not need to
know what `purchased_date` is.

**Nullable fields need a "set to null" mechanism** distinct from "leave
unchanged" (both would be absent from the request). Each edit tool has a
`clear_fields: set[ClearableField]` parameter typed as
`set[Literal["field1", "field2", ...]]` — a JSON Schema array with
`uniqueItems: true` and an enum of allowed values.

- `stock_entry_edit`: clearable fields are `price`, `best_before_date`,
  `purchased_date`, `note` (all nullable in the `stock` table).
- `product_edit`: clearable fields are `description`, `product_group`,
  `parent_product`, `calories` (nullable in the `products` table).
- `shopping_list_item_edit`: clearable field is `note`.

### Compact Responses by Default

Stock overview returns: product name, amount, unit name, location name,
expiry date. That's it. Full product/QU/location objects are available
through dedicated detail tools (`get_product`, `stock_entries_list`) when
needed.

### Batch Operations

`stock_add`, `stock_consume`, and `shopping_list_items_add` accept lists of
items for multi-product operations. Each item succeeds or fails
independently — failures don't abort the batch. Results are ordered to
match inputs.

Single-item operations are just batches of size 1. No separate
single-item tools.

### Error Messages as Teaching

Every error message should be **actionable**: name what's wrong, suggest
the fix, list available options. The agent shouldn't need to make a separate
lookup call to recover from an error.

## Delta from Current Implementation

This design implies several changes from the current Grocy MCP server:

1. **Name-based resolution** for products and locations (currently only QUs)
2. **Partial update** for `stock_entry_edit` (currently full-replace), adding `note` field
3. **Compact `stock_get` response** with product/location filters (currently returns full product dicts, no filtering)
4. **Location required on all mutations** — `stock_add`, `stock_consume`, `stock_set` (currently optional with silent defaults)
5. **Typed `create_product`** with optional `min_stock_amount`, `default_best_before_days`, `product_group`, `description` (currently `dict[str, Any]` body)
6. **`products_list` with `detail` param** replacing `entities_list` + `search_products` (brief/full output)
7. **`stock_entries_list` by product** (currently by entry ID list only)
8. **`get_expiring_stock` / `get_below_minimum_stock`** as focused tools
   (currently bundled in `list_volatile_stock`)
9. **Rewritten tool descriptions** from the agent's perspective
10. **`stock_transfer` as custom tool** with name resolution and QU validation
    (currently OpenAPI-generated `transfer_product_stock` with opaque body)
11. **`detail: "brief" | "full"` on reference data tools** (currently always return full Grocy objects)
12. **Server-side QU conversion** — accept any QU with a valid conversion to stock QU, convert server-side (Grocy's API only accepts stock QU)
13. **Enable `transaction_undo`** (currently disabled in tool_metadata.py)
14. **Shopping list `done` field** in responses and `shopping_list_item_edit`
15. **Shopping list `shopping_list_get`** returns list metadata + items, referenced by name or ID
