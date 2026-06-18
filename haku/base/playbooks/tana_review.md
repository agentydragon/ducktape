# tana_review (example)

The operator's Tana workspace — daily notes, captured tasks, project nodes — is
full of things they meant to do and never closed out. You can read it (read-only)
through the `tana-mcp-ro` facade, which exposes only Tana's read tools
(`search_nodes`, `read_node`, `get_children`, `open_node`, `list_tags`,
`list_workspaces`, `get_tag_schema`); every write tool is hidden and rejected, and
the Tana PAT stays server-side, so you never see it.

## Reaching it

`tana-mcp-ro` is cluster-internal
(`tana-mcp-ro.tana-mcp.svc.cluster.local:8765/mcp`), so your home can't reach it —
drive it from a `haku-sandbox` pod the way you query Plaid: bake the work into the
pod's **command** and read results from `kubectl logs` (`exec`/`port-forward` don't
work through the API gateway). Mount the bearer from the `haku-tana-ro-token` secret
as an env var (`secretKeyRef`, so it never lands on a command line) and have the pod
speak MCP over Streamable HTTP with `Authorization: Bearer $TOKEN` — e.g. a `python`
pod running a small `fastmcp` client, or a script doing the JSON-RPC `initialize` →
`tools/call` handshake.

This connection isn't paved yet — `tana-mcp-ro` is newly deployed. On first use,
confirm it's on your wire (the secret exists, the tools list is non-empty); if not,
note the gap in your log and move on. Once you find a pod recipe that works, record
it in `memory/` so later runs reuse it.

## What to mine

Resume from a bookmark in `memory/` (e.g. "tana: through 2026-06-18") and look at
what's recent in the operator's graph:

- **Recent daily notes** — walk the last ~1–2 weeks of daily/calendar nodes (find
  them via `search_nodes`, then `get_children` to read them) for tasks the operator
  jotted but never actioned: "follow up on X", "ask Y", half-captured ideas, items
  with no owner or date.
- **Recently-touched nodes** — `search_nodes` for what the operator edited lately; a
  node they left open often implies intended next work, and is a window into what
  they're focused on right now.
- **Stale open tasks** — unchecked task nodes that have sat untouched, especially
  ones implying a deadline or an easy win.

Turn the worthwhile ones into items: `suggestion` for "do this", `prepared_prompt`
where a full-access agent could carry it out (embed the node title + date + the
desired outcome). Evidence in `body`: node title, date, a short quote — **never**
dump raw node bodies. Skip anything already done elsewhere and anything you've filed
before.
