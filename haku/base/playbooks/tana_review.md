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
see `haku/runtime/claude_web_env/`), so talk to it **directly** — no pod, no JSON-RPC
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

### Fallback: drive the facade with `curl` when `fastmcp` is missing

`fastmcp` is the convenience wrapper, **not** the only way in. If it isn't on your
`PATH` (e.g. the web home came up with the lean `.#devtools` instead of
`.#agent-haku`), **do not skip Tana** — it's the operator's most important source.
The facade is plain MCP-over-HTTP behind the same bearer, so reach it with `curl`:
do an `initialize` (capture the `Mcp-Session-Id` response header), send
`notifications/initialized`, then `tools/call`. Responses come back as SSE
(`data: {…}` lines). A minimal one-call-per-invocation helper (fresh handshake each
time, stateless-friendly):

```bash
TOK=$(kubectl -n haku-sandbox get secret haku-tana-ro-token -o jsonpath='{.data.token}' | base64 -d)
URL=https://tana-mcp-ro.allegedly.works/mcp
H=(-H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream")
SID=$(curl -sS -D - -o /dev/null "${H[@]}" -X POST "$URL" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"haku","version":"0"}}}' \
  | awk 'tolower($1)=="mcp-session-id:"{print $2}' | tr -d '\r')
curl -sS "${H[@]}" -H "Mcp-Session-Id: $SID" -X POST "$URL" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' >/dev/null
curl -sS "${H[@]}" -H "Mcp-Session-Id: $SID" -X POST "$URL" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"search_nodes","arguments":{"query":{"and":[{"hasType":"<taskTagId>"},{"or":[{"field":{"fieldId":"<statusFieldId>","nodeId":"<backlogId>"}},{"field":{"fieldId":"<statusFieldId>","nodeId":"<inProgressId>"}}]}]},"limit":100}}}' \
  | sed 's/^data: //' | grep -E '^\{' | tail -1
```

`search_nodes` takes a top-level `query` (and `limit`, max ~100) — **no
`workspaceId`**; the structured query DSL is in the tool's own `description`. Link
Tana nodes in items as `https://app.tana.inc?nodeid=<nodeId>`. **Surface the
`fastmcp` gap itself as an item** (see _Environment self-check_) so the operator can
fix the closure — curl keeps you working meanwhile, but the root cause should reach
the dashboard.

## Determining whether a task is actually open — use the `Task status` field, NOT the checkbox or `is:todo`

**Both `is:'todo'/'done'` and the markdown `[ ]/[X]` checkbox are unreliable
truth sources for a `#Task`** — they cost real reconciliation errors (filing/keeping
items for work the operator had already finished). Measured on this graph
(2026-06-25): `is:todo` returned 27 nodes of which **26 were actually done**, and it
**missed** genuinely-open tasks entirely; the checkbox is binary so it conflates
**Done** and **Cancelled** (and any other terminal state) into one `[X]`.

The real source of truth is the `#Task` supertag's **`Task status`** field, an enum.
Discover its field id and option ids with `get_tag_schema` on the task tag (ids are
**workspace-specific** — never hardcode; re-read the schema). On this graph today:

- Field **`Task status`** = `1McjTPZczhYk`, options: **Backlog** `Prpt16bEROhy`
  (default), **In progress** `nwX-Fo55la66`, **Done** `BWHY3xGsWmOJ`, **Cancelled**
  `6ruvnDPXXUBF`.

**Actionable = `Task status` ∈ {Backlog, In progress}. Treat Done AND Cancelled as
closed** — never surface them, and reconcile any existing item against them: a Tana
**Done** → mark the item `done`; a Tana **Cancelled** → mark it `rejected` (the
operator decided against it), not `done`. Query the actionable set directly with the
`field` operator (the curl example above): `hasType:<taskTag>` AND (`field` status =
Backlog **or** In progress). When you read an individual node, take its
`**Task status**:` line as authoritative — ignore the checkbox glyph.

### Always exclude trashed nodes

Every node in a `search_nodes` result carries an **`inTrash`** boolean. There is **no
trash flag in the query DSL**, so `search_nodes` returns trashed nodes mixed in with
live ones — **filter `inTrash === true` out client-side** before surfacing or filing
anything. A trashed task the operator deleted is not relevant; surfacing it is the
same class of error as surfacing a Done one. (Measured 2026-06-25: trashed `#Task`
nodes still come back from the status-field query.)

## Incremental scan — read EVERY change since the bookmark, not just tasks

Tana is the operator's primary brain; **a sweep must see all of it that changed since
last time, across all node types** — not only `#Task`. Edits to meetings, projects,
daily notes, company/person nodes, and free notes carry context that implies new tasks,
problems worth solving, status updates on things you already track, and patterns worth
suggesting. Missing edits = missing the operator's current reality.

Keep an **exact millisecond bookmark** in `memory/` (e.g. `tana: through
2026-06-25T20:30:00Z` → `1750883400000`); `edited.since` takes ms. Each sweep, pull
everything edited since it and **advance the bookmark to now** when done.

**The hard constraint: `search_nodes` has NO pagination** (only `query`,
`workspaceIds`, `limit`; no offset/cursor) **and `edited` has no upper bound** (only
`since`/`last`, no range). So a single `{edited:{since}}` silently **truncates at the
`limit` (~100)** — and Tana syncs Google Calendar as a stream of `#Meeting` nodes, so
churn blows past 100 fast. To read the change set **completely**:

1. Scope to the operator's workspace(s) with `workspaceIds`.
2. Run `{edited:{since:<ms>}}` (+ `created:{last:N}` for brand-new nodes — `created`
   has no `since`). If it returns **fewer than `limit`**, that's the complete set.
3. If it **hits the cap**, it's truncated — **narrow and re-run per supertag**:
   `and:[{hasType:<tag>},{edited:{since}}]` for each tag from `list_tags`, since a
   single tag rarely exceeds 100. Cover untagged/daily churn via the date nodes
   directly (`get_children` on the recent `#Day`/Week nodes).
4. Keep the **bookmark cadence tight enough** that a window never exceeds the cap; if
   one tag ever caps anyway, sweep more often (smaller windows). Log if you suspect
   truncation so a gap is visible rather than silent.

Always drop `inTrash` nodes (above). `#Meeting`/`#Day` nodes are mostly routine
calendar syncs — skim, don't dwell, unless one implies something (a note like "…for
insurance fighting" on a meeting is a real signal).

## What to mine — reason beyond tasks

- **Actionable `#Task`s** (Backlog/In-progress, not trashed): freshly-edited = current
  focus; stale = easy wins or things slipping.
- **Everything else that changed** — read it for: (a) **status updates** on things you
  already track (close/advance the matching item); (b) **latent tasks** jotted in
  daily notes/projects but never made `#Task`s ("follow up on X", "ask Y"); (c)
  **problems** implied by a note that an agent could solve; (d) **patterns in how the
  operator uses Tana** worth suggesting (e.g. recurring manual steps to automate, a
  messy area to restructure) — surface those as `suggestion` items too.

Turn the worthwhile ones into items: `suggestion` for "do this / here's an idea",
`prepared_prompt` where a full-access agent could carry it out (embed the node title +
date + desired outcome). Evidence in `body`: node title, date, a short quote — **never**
dump raw node bodies. Skip anything trashed, anything whose `Task status` is
Done/Cancelled, anything already tracked, and anything you've filed before.
