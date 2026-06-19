# tana_review (example)

The operator's Tana workspace — daily notes, captured tasks, project nodes — is
full of things they meant to do and never closed out. You can read it (read-only)
through the `tana-mcp-ro` facade, which exposes only Tana's read tools
(`search_nodes`, `read_node`, `get_children`, `open_node`, `list_tags`,
`list_workspaces`, `get_tag_schema`); every write tool is hidden and rejected, and
the Tana PAT stays server-side, so you never see it.

## Reaching it

`tana-mcp-ro` is exposed at `https://tana-mcp-ro.allegedly.works/mcp`, gated by a
static bearer. Your home has the `fastmcp` CLI (baked into the agent-haku closure —
see `haku/claude_web_env/`), so talk to it **directly** — no pod, no JSON-RPC
handshake. Read the bearer from the reflected `haku-tana-ro-token` secret into a
shell variable (reference `"$TOKEN"`, never the literal, so the secret stays out of
your transcript), then list and call:

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-tana-ro-token \
  -o jsonpath='{.data.token}' | base64 -d)
URL=https://tana-mcp-ro.allegedly.works/mcp

# What's exposed, with each tool's argument schema:
fastmcp list "$URL" --auth "$TOKEN" --transport http --input-schema

# Call a tool — args are key=value pairs per the schema above (or
# --input-json '{…}' for nested); add --json for machine-readable output:
fastmcp call "$URL" search_nodes query="follow up" --auth "$TOKEN" --transport http
fastmcp call "$URL" read_node nodeId="<id>" --auth "$TOKEN" --transport http
```

Read tools only — `search_nodes`, `read_node`, `get_children`, `open_node`,
`list_tags`, `list_workspaces`, `get_tag_schema`; writes are hidden and rejected. If
`list` is empty or a call 401s, note the gap in your log and move on (the facade
flips NotReady on a bad upstream, and the bearer must be the reflected one).

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
