# Grocy

The operator's **Grocy** tracks their household stock — what food and supplies are on
hand, where, how much, and what's about to expire or run low. It's the seam for
quality-of-life suggestions: "the eggs expire in two days → here's an omelette," "you're
down to one roll of paper towels," "you're already out of the wall filler your project
needs." You read it **read-only**: your Grocy user has empty permissions, so the Grocy
API serves every read (200) and rejects every write (403) server-side — never try to
add, consume, or edit stock.

## Reaching it

Grocy is exposed as an MCP server at `https://grocy-mcp-sf.allegedly.works/mcp`,
authenticated by a bearer JWT (the MCP validates it and runs the auth handshake into
Grocy itself, injecting your read-only `haku` user). Your home has the `fastmcp` CLI
(baked into the agent-haku closure — see `haku/runtime/claude_web_env/`), so talk to it
**directly** — no pod. Read the bearer from the reflected `haku-cloud-grocy-sf-token`
secret (the same rotated grocy JWT the cloud agent uses, mirrored into `haku-sandbox`)
into a shell variable — reference `"$TOKEN"`, never the literal, so the secret stays out
of your transcript:

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-cloud-grocy-sf-token \
  -o jsonpath='{.data.jwt}' | base64 -d)
URL=https://grocy-mcp-sf.allegedly.works/mcp

# What's exposed, with each tool's argument schema:
fastmcp list "$URL" --auth "$TOKEN" --transport http --input-schema

# Call a read tool (--json for machine-readable output):
fastmcp call "$URL" stock_get --auth "$TOKEN" --transport http --json
```

### Fallback: `curl` when `fastmcp` is missing

`fastmcp` is just a convenience wrapper (see `tana.md` for the same situation). If it
isn't on your `PATH`, drive the facade with `curl` over the standard streamable-HTTP MCP
handshake (`initialize` → capture `Mcp-Session-Id` → `notifications/initialized` →
`tools/call`, threading the header; responses may arrive SSE-framed on `data:` lines).
**Surface the `fastmcp` gap as an item** so the operator can fix the closure.

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
else is going on (see `../recipes.md` → _Generate, don't just detect_). Never surface a
suggestion to buy something already on a shopping list. If `list` is empty or a call
401s, note the gap in your log and move on (the bearer must be the reflected one).
