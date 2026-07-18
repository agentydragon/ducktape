# Grocy

The operator's **Grocy** tracks their household stock — what food and supplies are on
hand, where, how much, and what's about to expire or run low. It's the seam for
quality-of-life suggestions: "the eggs expire in two days → here's an omelette," "you're
down to one roll of paper towels," "you're already out of the wall filler your project
needs." Reads (`stock_get`, `products_list`, …) auto-approve under the console's
reviewed policy; every write (add/consume/edit stock, shopping-list edits, …) routes
through the console's operator-approval queue instead of executing directly — never
expect a write to complete without a human clicking approve.

## Reaching it

haku-console proxies the grocy-sf MCP server (tools get a `grocy_sf_` prefix). Reach
it however your runtime wires it: managed sessions expose the tools directly as
in-session MCP tools; otherwise call `https://haku.allegedly.works/mcp` over MCP-HTTP
(<mcp_over_http.md>) with the `haku-console-agent-api` bearer from `haku-sandbox`,
same as the Gmail/Calendar/osm/Tana console tools.

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-console-agent-api -o jsonpath='{.data.token}' | base64 -d)
fastmcp call https://haku.allegedly.works/mcp grocy_sf_stock_get \
  --auth "$TOKEN" --transport http --json
```

## What to read

Read tools (auto-approve; everything else becomes a pending approval — never assume a
write executed):

- **`stock_get`** — current stock overview: product, amount, location, and best-before
  dates. The primary read; scan it for items expiring soon or below their minimum.
- **`stock_entries_list`** — individual stock entries (per-lot best-before / opened
  state) when you need finer detail than the aggregated overview.
- **`products_list`**, **`product_groups_list`** — the product catalog and its grouping
  (min-stock thresholds, quantity units, default locations).
- **`shopping_lists_list`**, **`shopping_list_get`** — what the operator has already
  queued to buy (so you don't re-suggest it).

Mine these for: items **expiring** (propose using them up — recipes, "eat this first"),
items **below minimum** or absent that the operator relies on (a shopping nudge), and
**opportunistic** suggestions that combine stock with where the operator is and what
else is going on (see your procedures, `procedures/maintenance_and_synthesis.md` →
_Generate, don't just detect_, in your state). Never surface a
suggestion to buy something already on a shopping list. If `list` is empty or a call
401s, note the gap in your log and move on.
