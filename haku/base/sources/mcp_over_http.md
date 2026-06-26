# Calling an MCP-server source (`fastmcp`, with a `curl` fallback)

Several sources — Tana, Grocy, … — are **MCP servers reached over HTTP behind a
bearer token**. The transport is identical across them; only the URL, the bearer
secret, and the exposed tools differ (those live in each source's own file). This is
the shared how-to; the per-channel files just point here.

## With `fastmcp` (the normal path)

Your home has the `fastmcp` CLI (baked into the agent-haku closure — see
`haku/runtime/claude_web_env/`), so talk to the server **directly** — no pod, no
hand-rolled JSON-RPC. Read the bearer from the source's reflected `haku-sandbox`
secret into a shell variable, and **always reference `"$TOKEN"`, never the literal
value**, so the secret stays out of your transcript:

```bash
TOKEN=$(kubectl -n haku-sandbox get secret <secret-name> \
  -o jsonpath='{.data.<key>}' | base64 -d)
URL=<the source's …/mcp URL>

# Discover the exposed tools with their argument schemas:
fastmcp list "$URL" --auth "$TOKEN" --transport http --input-schema

# Call a tool — args are key=value per the schema above (or --input-json '{…}' for
# nested); add --json for machine-readable output:
fastmcp call "$URL" <tool> key=value --auth "$TOKEN" --transport http --json
```

## Fallback: `curl` when `fastmcp` is missing

`fastmcp` is just a convenience wrapper. If it isn't on your `PATH` (e.g. the web home
came up with the lean `.#devtools` instead of `.#agent-haku`), **do not skip the
source** — drive it with `curl` over the standard streamable-HTTP MCP handshake you
already know:

1. `initialize` → capture the `Mcp-Session-Id` **response header**.
2. `notifications/initialized` (threading that header).
3. `tools/list` / `tools/call` (still threading the header).

Read `"$TOKEN"` from the secret as above and send it as `Authorization: Bearer
$TOKEN`; curl goes through the egress proxy transparently. **Gotcha:** responses often
come back **SSE-framed** — the JSON-RPC payload is on `data: {…}` lines, not a bare
body, so strip the `data: ` prefix before parsing.

**Surface the `fastmcp` gap itself as an item** (environment self-check) so the
operator can fix the closure — `curl` keeps you working meanwhile, but the root cause
should reach the dashboard.
