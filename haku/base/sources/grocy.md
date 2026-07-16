# Grocy

The operator's **Grocy** tracks their household stock — what food and supplies are on
hand, where, how much, and what's about to expire or run low. It's the seam for
quality-of-life suggestions: "the eggs expire in two days → here's an omelette," "you're
down to one roll of paper towels," "you're already out of the wall filler your project
needs." You read it **read-only**: your Grocy user has empty permissions, so the Grocy
API serves every read (200) and rejects every write (403) server-side — never try to
add, consume, or edit stock through this path. (Grocy **mutations** go through
haku-console's `grocy-sf` proxy as approval-gated tool calls; the console also
auto-approves its read-only `grocy_sf_*` subset, an equivalent read path when the
console tools are on your wire.)

## Reaching it

Grocy is an MCP server at `https://grocy-mcp-sf.allegedly.works/mcp`. The transport
mechanics — `fastmcp`, the `curl` fallback, reading the bearer into `"$TOKEN"` — are
shared across MCP sources; see [`mcp_over_http.md`](mcp_over_http.md). Grocy specifics:

- **Bearer:** the `haku-cloud-grocy-sf-token` secret, key `jwt` (the same rotated grocy
  JWT the cloud agent uses, mirrored into `haku-sandbox` by ESO). The MCP validates it
  and runs the auth handshake into Grocy, injecting your read-only `haku` user — so
  reads return 200 and **every write 403s** server-side.

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-cloud-grocy-sf-token \
  -o jsonpath='{.data.jwt}' | base64 -d)
fastmcp call https://grocy-mcp-sf.allegedly.works/mcp stock_get \
  --auth "$TOKEN" --transport http --json
```

## What to read

Read tools (writes exist but 403 for you — ignore them):

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
401s, note the gap in your log and move on (the bearer must be the one in `haku-sandbox`).
