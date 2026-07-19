# Tana

The operator's Tana workspace — daily notes, captured tasks, project nodes — is
full of things they meant to do and never closed out. You can read it through
haku-console's `tana-rw` MCP entry: the read tools (`search_nodes`, `read_node`,
`get_children`, `open_node`, `list_tags`, `list_workspaces`, `get_tag_schema`) plus
the idempotent `get_or_create_calendar_node` auto-approve under the console's
reviewed policy; every other tool (node edits, moves, deletes, tag creation, …)
routes through the console's operator-approval queue instead of executing directly.

## Reaching it

haku-console proxies the tana-rw MCP server (tools get a `tana_rw_` prefix). Reach
it however your runtime wires it: managed sessions expose the tools directly as
in-session MCP tools; otherwise call `https://haku.allegedly.works/mcp` over MCP-HTTP
(<mcp_over_http.md>) with the `haku-console-agent-api` bearer from `haku-sandbox`,
same as the Gmail/Calendar/osm console tools. Tana is the operator's most important
source — if `fastmcp` is missing, fall back to `curl`, never skip it.

```bash
TOKEN=$(kubectl -n haku-sandbox get secret haku-console-agent-api -o jsonpath='{.data.token}' | base64 -d)
fastmcp call https://haku.allegedly.works/mcp \
  tana_rw_search_nodes query="follow up" --auth "$TOKEN" --transport http --json
```

Read tools only auto-approve — `search_nodes`, `read_node`, `get_children`, `open_node`,
`list_tags`, `list_workspaces`, `get_tag_schema`, `get_or_create_calendar_node`; every
other tool becomes a pending approval instead of executing. If `list` is empty or a
call 401s, note the gap in your log and move on.

`search_nodes(query, workspaceIds=[…], limit=50)` — `limit` **max 100** (101 → `too_big`),
**no offset/cursor** (it cannot paginate; a result of exactly 100 means truncated). The
structured query DSL is in the tool's own `description`. Link Tana nodes as
`https://app.tana.inc?nodeid=<nodeId>`.

## Structure — read subtrees, not just names (read this first)

Tana is a **nested outliner**: every node owns a subtree of child bullets, and the substance
of the operator's thinking lives in those **subtrees**. A flat `search_nodes` result gives you
only node **names**. So the scan is **time-sweep to find what changed, then `read_node` its
subtree** for the actual content. Verified facts that shape the method:

- **Most of his nodes are UNTAGGED** free-text bullets. Anything built on `hasType`/supertags
  is structurally **blind to the bulk of his content** — never scan by tag for completeness.
- **Task updates accrete as child bullets INSIDE the task node** (the call log, the next step,
  the dollar estimate), not under a daily node. **Notes** often go under the daily (`#Day`)
  node — but **an edit on a given day does not necessarily live under that day's node.**
- `read_node(nodeId, maxDepth)` (maxDepth ≤ 10) renders the subtree as markdown with
  `<!-- node-id -->` markers — **the content tool.** `get_children(nodeId, limit≤100, offset)`
  is the one **paginated** read for walking a large subtree.
- Returned nodes carry `inTrash` (filter `true` out client-side — no trash flag in the DSL)
  but **no `edited` timestamp** (can't sort by edit time).

The operator maintains the concrete runnable sweep recipe in its own state procedures; keep
this guide and that recipe in sync.

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
`field` operator: `hasType:<taskTag>` AND (`field` status = Backlog **or** In
progress). When you read an individual node, take its
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

Keep an **exact millisecond bookmark**; `edited.since` takes ms. In haku-state it lives
as the typed `epoch_millis` entry in `memory/bookmarks.md`'s frontmatter ledger — advance
it **after triaging the sweep** via `tools/bookmark.py advance tana --to <ms>`
(tool-written, monotonic), never by hand-editing the ledger.

**`edited.since` alone is not enough — you also need `created.since`.** Typing a _new_
bullet **creates** a node; it does not "edit" an existing one. So `{edited:{since}}` **misses
most new daily content** (verified: a 2-day window returned 29 edited nodes but 100+ _created_,
70 of them untagged and absent from the edited set). Run **both** every sweep:

1. Scope to the operator's workspace(s) with `workspaceIds`.
2. **Edited:** `{edited:{since:<ms>}}`. **Created:** `{created:{last:N}}` (`created` has only
   `last:<days>`, **no `since`**) → keep nodes whose `created` ISO ≥ your bookmark.
3. **Read subtrees of what's substantive** (`read_node`) — the change's content is in the
   subtree, not the name.
4. **Cap = 100, no pagination.** A result of exactly 100 is truncated. Narrowing per supertag
   (`and:[{hasType:<tag>},{edited:{since}}]`) helps for _tagged_ churn (Tana streams Google
   Calendar as `#Meeting` nodes), **but cannot recover untagged nodes — the bulk.** So also
   read recent `#Day` nodes' subtrees directly (`read_node`/paginated `get_children`) and keep
   the **bookmark cadence tight** so windows rarely cap. If real content risks truncation,
   **log it as a visible gap**, don't pretend completeness.

Always drop `inTrash` nodes (above). `#Meeting`/`#Day` calendar-sync nodes are mostly routine
— skim, don't dwell, unless one implies something (a note like "…for insurance fighting" on a
meeting is a real signal).

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
