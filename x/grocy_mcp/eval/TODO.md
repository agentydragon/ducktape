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
