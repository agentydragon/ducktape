# Eval scenarios — backlog

Scenarios we want to exercise but haven't built yet, in rough priority.
Currently-registered cases live in `cases.py` — don't re-list them here.

### Consumption with location disambiguation

> "I just finished the rice." With the same product stocked in both Pantry
> and Fridge, does the agent run `stock_get` first or silently consume
> from whichever location Grocy picks?

Tools: `stock_get` (preflight), `stock_consume`, `transaction_undo` (if the
agent notices the mistake). Specifically targets the silent-wrong-location
footgun the `stock_consume` docs now warn about.

### Expiry cleanup

> "Throw out anything that expired more than a week ago, and add
> replacements to my shopping list."

Tools: `get_expired_stock` → decide discard vs. replace → `stock_consume`
(with `note="spoiled"`) → `shopping_list_items_add`. Multi-tool chain driven
by per-row `days_overdue`.

### Shopping-list round-trip

> "Here's what I bought today: [receipt items]. Mark those off my list and
> add them to stock."

Tools: `shopping_list_get` → `shopping_list_item_edit` (mark done) or
`shopping_list_items_remove` → `stock_add`. Tests the full shopping → stock
transition.

### Pantry audit / absolute correction

> "I just counted the pantry: rice 1.2kg, pasta 500g, …"

Tools: `stock_get` (preflight for comparison) → bulk `stock_set` with
absolute amounts. Tests whether the agent prefers `stock_set` over
computing deltas manually.

### Costco receipt bulk-add

> "I just got back from Costco, log everything I bought." Receipt contains
> 20+ items, some already in the system, some new.

Tools: `products_list` + fuzzy match → `products_create` for the new ones
→ batched `stock_add`. Tests whether the agent handles mixed existing +
new-product flows without duplicating.

### QU conversion stress

> "I bought 2 cases of beer (24 bottles each). Log it."

Seed a product whose stock QU is "Bottle" with a `quantity_unit_conversions`
row saying 1 Case = 24 Bottles. Test whether the agent uses the
`Case` QU directly (relying on the conversion) or pre-multiplies to
Bottles. Also tests whether the agent handles the case where the product
doesn't yet have a conversion defined.

### Multi-location transfer

> "We swapped freezers — move everything from the old chest to the new
> upright."

Tools: `list_location_stock` → bulk `stock_transfer`. Currently
untested by eval.

### Edit + `clear_fields` semantics

> "Remove the best-before date from these three stock entries — they're
> pantry staples that don't expire."

Tools: `stock_entries_list` → `stock_entry_edit` with
`clear_fields=["best_before_date"]`. Tests whether the agent distinguishes
"set to null" from "set to today" and respects the enum of nullable fields.

### Ambiguous name resolution

> "Consume 100g of Rice." With two products named `Rice` and `Brown rice`
> in the system.

Tests whether the agent asks for clarification, picks the exact-match, or
falls back to fuzzy matching and exposes that choice.

### Purchase-QU bulk stocking with auto-created conversions

> "Product X comes in boxes of 30 pieces and product Y comes in boxes of
> 12 pieces. I bought 3 boxes of X and 5 boxes of Y, stock them all."

Seed products with `stock_qu=Piece` and `purchase_qu=Box`. On creation,
Grocy auto-creates a product-specific factor=1 Box→Piece conversion.
The agent must notice the factor is wrong (or create the correct one
up front), update each product's conversion to the real factor (30, 12),
then `stock_add` using the Box QU. Validates: (1) awareness of the
auto-created factor=1 footgun, (2) correcting conversions before
stocking, (3) using purchase QU directly instead of pre-multiplying.

Tools: `products_create` → `entities_list` (`quantity_unit_conversions`)
→ `entity_update` (fix factors) → `stock_add` (in Box QU).

### Bulk stock take with non-expiring products

> "I just moved in. Set up these pantry staples: salt, sugar, vinegar,
> rice, olive oil. I have 1 of each. None of them expire."

The agent must create products with `default_best_before_days=-1` (or
pass `best_before_date="2999-12-31"` on each `stock_add`). Validates
that the agent doesn't leave the default of 0, which would set all
best-before dates to today — making every item appear as expiring
immediately.

Grader checks: no stock entry has a `best_before_date` equal to today.
All entries should be `2999-12-31`.

### Expired stock check with due_type

> "Create: milk (expires in 7 days), canned beans (best before 30 days),
> chicken breast (expires in 3 days). Add 1 of each to stock. Then tell
> me what's expired or expiring within a week."

The agent should set `due_type=2` for milk and chicken (perishable —
unsafe after date) and `due_type=1` for beans (best-before — possibly
safe after date). It should then use `get_expiring_stock` to find
items due within 7 days (chicken), and `get_expired_stock` to find
truly expired items (none yet). Validates: (1) agent knows `due_type`
exists and sets it correctly, (2) agent understands `get_expired_stock`
only returns `due_type=2` items.

Grader checks: chicken and milk have `due_type=2`, beans have
`due_type=1`.

### Stock correction no-op handling

> "I just counted: we have exactly 5 bags of rice."

Seed a product with exactly 5 bags already in stock. The agent calls
`stock_set(new_amount=5)` and gets an error ("new amount cannot equal
current stock amount"). Validates the agent handles this gracefully
(reports "stock is already correct") rather than retrying or crashing.

### Freezer transfer date side-effect

> "Move the chicken from the fridge to the freezer."

Seed a product with `default_best_before_days_after_freezing=0`
(the default), stock in a Fridge location, best-before date 7 days
from now. After `stock_transfer` to a Freezer location, Grocy silently
resets the best-before date to today. Validates the agent either:
(a) warns the user about the date change, (b) sets
`default_best_before_days_after_freezing=-1` first, or (c) checks
the entry afterwards.

Grader checks: agent acknowledges or handles the date side-effect
rather than silently letting stock appear as expiring today.

### Clearing expiry on a stock entry

> "The expiry date on the canned tomatoes is wrong — they don't expire.
> Fix it."

Seed a product with stock that has a `best_before_date` set. The agent
should set `best_before_date="2999-12-31"` via `stock_entry_edit`.
`best_before_date` is not clearable via `clear_fields` (NULL makes the
product invisible in Grocy's stock overview). Validates the agent knows
the `2999-12-31` never-expires convention.

### Sensible expiry defaults for mixed product types

> "Set up my kitchen: salt, white vinegar, fresh strawberries, sliced
> deli turkey, canned chickpeas, ground coffee. Add 1 of each to stock."

The agent must pick reasonable `default_best_before_days` and `due_type`
for each product based on common knowledge:

- Salt, vinegar: `default_best_before_days=-1` (never expires)
- Strawberries: short expiry (< 14 days), `due_type=2`
- Deli turkey: short expiry (< 14 days), `due_type=2`
- Canned chickpeas: long expiry (months+), `due_type=1`
- Ground coffee: medium expiry (weeks to months), `due_type=1`

Grader checks: (1) salt and vinegar have `default_best_before_days=-1`
or stock entries with `best_before_date=2999-12-31`, (2) strawberries
and turkey have `due_type=2` and best-before date < 4 weeks out,
(3) no product has best-before date set to today (the `0` footgun).

### Product merge / deduplication

> "I just noticed we have both `Rice` and `rice` — merge them."

Tools: `products_merge`. Destructive, rarely used, but worth a single
eval to confirm the agent doesn't misfire.

## How to add one

1. Write a seed function in `seed.py` (`async def seed_<id>(client) -> None`).
2. Add an `EvalCase` entry in `cases.py` with the task prompt and prose
   success criteria.
3. Register it in `CASES`.
4. Run `bb run //x/grocy_mcp/eval:cli -- --case <id> --api anthropic`.
5. Drop the rollout into `sample_rollouts/<id>/` alongside the others.
