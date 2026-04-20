## Fold `server_instructions.md` conventions into tool descriptions

claude.ai does not expose MCP `initialize.instructions` to the LLM
(verified 2026-04-19). The eval harness compensates by prepending the
markdown to the system prompt manually; claude.ai has no equivalent
injection point.

Make tool descriptions more self-contained so they work well without
server-level instructions. Key conventions that currently live only in
`server_instructions.md` (e.g. `amount_opened` semantics, when to use
`stock_set` vs `stock_add`, unit handling) should be folded into the
relevant tool descriptions themselves.

## Finish the `<entity>_<verb>` rename

The first pass renamed the CRUD / stock / shopping-list families. The
remainder needs a bit more care:

- **Stock views** — `get_expiring_stock`, `get_below_minimum_stock`,
  `get_expired_stock`, `list_volatile_stock`. `stock_expiring_list` etc.
  works mechanically but "stock views keyed on a query" doesn't feel like
  a true entity; a separate "queries" namespace
  (`stock_query_expiring` / `stock_query_expired` / …) or grouping under
  `volatile_stock_*` might read better. Decide before renaming.
- **Singletons** — `get_system_info`, `get_db_changed_time`,
  `get_current_user`. These are zero-argument reads on singleton
  resources; dropping the verb (`system_info`, `db_changed_time`,
  `current_user`) reads more naturally as MCP resources than as tools.
- **Product stock helpers** (OpenAPI) — `get_product_stock`,
  `open_product_stock`, `transfer_product_stock` (already disabled). The
  `<entity>_<verb>` rename is mechanical (`product_stock_get`,
  `product_stock_open`) but the `product_stock` name implies it's a
  standalone entity, which it isn't — it's a derived view over
  `stock_entries` filtered by product. Worth thinking about whether to
  promote to a batch tool that groups with `stock_*`.

## Echo server-computed values in mutation responses

Stock mutations (e.g. `stock_add`) let Grocy compute derived values
like `best_before_date` (from `default_best_before_days`). The current
response only returns `new_amount` — the agent can't see what
best-before date Grocy assigned without a follow-up `stock_entries_list`
call. Echoing computed values back (best-before date, resolved location,
etc.) would let the agent catch surprises (e.g. "expires today" when it
expected no expiry) without extra round-trips.

Needs design thought: Grocy's `POST /stock/products/{id}/add` response
includes a list of created stock entries with their IDs but not the full
entry bodies. We'd need to either fetch entries by ID after the mutation
or parse the transaction log. Figure out the cleanest approach.

## Consider per-test container isolation for e2e tests

Tests currently share a session-scoped Grocy container and use uuid
suffixes to avoid name collisions. Container startup is ~25-30s
(LinuxServer image runs s6-overlay + nginx + PHP + SQLite migrations),
so function-scoped containers would make the suite too slow (~4-5 min
for 9 tests).

Options to explore:

- Lighter Grocy container image (skip nginx/s6, run PHP built-in server
  directly against a fresh SQLite DB).
- Grocy's built-in demo-mode reset endpoint (if one exists).
- Per-test database reset via direct SQLite file swap.

## Per-turn eval logging

`agent.run()` is a black box — no per-turn callbacks in
agent_framework. Replace with a manual conversation loop (like
`skills/info_gathering/evals/function_learning/function_learning.py`)
to log timestamps, token counts, and tool call names on every LLM
turn. This would let us see which tool calls are slow and where time
is spent during eval runs.

## `shopping_list_clear` checks each DELETE (landed in #1345)

Tombstone — keep until the next design pass so we remember it was an
intentional fix rather than an oversight.

## File upstream Grocy PR for missing entity-schema properties

`fix_openapi_spec.py::patch_product_schema` downstream-patches 12 writable
product columns that Grocy's hand-maintained `grocy.openapi.json` has never
picked up since migrations 0207/0210/0219 added them (2022–2023). Upstream
spec has similar gaps on `Location` (no `active`, no `is_freezer`),
`QuantityUnit` (no `active`), `ShoppingListItem` (no `qu_id`, no `done`),
and is missing `ShoppingList`, `ProductGroup`, `Recipe` entirely.

Action: file an issue + PR against https://github.com/grocy/grocy adding
the missing properties and the three missing schemas. Maintainer (berrnd)
has accepted similar drift fixes (#1451, #1967, #2198, #2694). Once a
Grocy release shipping the upstream fix is pinned in `MODULE.bazel`,
remove the corresponding downstream patches from `fix_openapi_spec.py`.
