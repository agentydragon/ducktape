# Haku Google access: console mediation and Airlock decoupling

How Haku reaches Google (Gmail, Calendar, Drive, Tasks) and how that access moves off Airlock
onto haku-console. This is a Haku credential-architecture plan; the cross-cutting OAuth/identity
program that contains it lives in `plans/oauth_architecture.md`.

Sequenced later than the common Agent lifecycle (H1–H3 there).

## Progress

1. **G1 (done):** the console owns the per-Operator Google connection —
   `haku/console/provider_connection.py` (Postgres per-Operator refresh storage + in-process
   self-refresh), the `/api/operator-connections/*` connect/status/disconnect flow, deploy-named
   connection bindings with execution-time Operator selection, and the Settings → Connected
   accounts UI. Gmail and Calendar have separate least-privilege grants and separate provider
   clients so Calendar can complete sensitive-scope verification independently of restricted Gmail.
   The existing `haku-console-google-client-credentials` Secret remains Gmail's client; Calendar has
   its own optional credential slot. These are downstream-provider relationships, not Agent
   enrollment or Agent-held credentials.
2. **G2 (done):** removed `haku_console_google`, its Secret publication/External Secrets mirror, and
   its airlock-side producer (#3364). The console-owned token never reaches an Agent — it lives only
   in the `haku-console` Postgres, and Agents reach Gmail/Calendar solely through the console's
   approval-gated MCP tools.
3. **G3 (later — not scheduled):** retire Haku's _last_ Airlock dependency, the read-only
   `google-access-token` (`$TOK`) that the `google` Airlock grant reflects into `haku-sandbox`.
   Today the agent holds it directly for Drive/Tasks and as the Gmail/Calendar REST fallback
   (`haku/base/sources/`). What replaces it is the target below.

Do not couple G1/G2/G3 to Airlock's unrelated Oura, BSC, or remaining credential consumers.

## Target: console mediates all Google access, agent holds no standing token

The clean end state: **haku-console holds the Google client(s) and mediates every Google operation;
no Google token with standing capability ever reaches the agent.**

- **High-risk operations — invariant, not a preference.** Anything the operator does not want Haku
  to execute autonomously (sending mail, deleting/modifying Drive files, mutating calendars, …) runs
  only through haku-console tools behind its approval policy. A token carrying those permissions must
  never be handed to the agent. This already holds for Gmail/Calendar writes.
- **Low-risk (read-only) — a genuine tradeoff, currently unresolved:**
  - _Direct token (status quo):_ the agent holds the read-only `$TOK`. Simpler, but it is a standing
    bearer secret in agent context — if it leaks through the LLM provider, whoever reads it can read
    all of the operator's mail/Drive going forward, bounded only by rotation cadence.
  - _Console-mediated (cleaner, more secure):_ route reads through console MCP tools too, so the
    agent holds no Google token at all. Cost: implementing a potentially large read tool surface
    (Drive, Tasks, remaining Gmail/Calendar read affordances) — which may be worth doing anyway, and
    would let G3 drop the `google` Airlock grant and the `haku-sandbox` reflection entirely.

  Leaning console-mediated for the security win; decision deferred. Until then the read-only token
  stays (least-privilege by construction — all `.readonly` scopes).

## Console read-tool surface to build (priority list — what G3 must replace)

Everything Haku might want to do with the read-only `$TOK`, as an implementation priority list. To
retire `$TOK`, the console must expose console-mediated read tools covering all of it, then the
`google` Airlock grant's read scopes can be dropped scope-by-scope as coverage lands (`gmail.readonly`,
`drive.readonly`, `drive.activity.readonly`, `calendar.readonly`, `tasks.readonly`, `contacts.readonly`,
`documents.readonly`, `spreadsheets.readonly`, `presentations.readonly`, `youtube.readonly`). Ordering
is by current use, then held-scope value, then long tail. Each tool maps to a Google REST method so it
is directly implementable.

Writes are **not** on this token — high-risk mutations (send mail, mutate calendar/tasks/Drive) are a
separate approval-gated console surface (Gmail drafts/labels + Calendar create already exist); they are
never added here.

### P1 — active today; direct blockers to dropping `$TOK`

- **Drive — recency + activity** (`drive.readonly`, `drive.activity.readonly`): Haku's "what is the
  operator working on right now" window (`haku/base/sources/drive.md`).
  - `drive_files_list` — recent files by `modifiedTime desc` (`files.list`; id, name, modifiedTime,
    owners, shared, webViewLink; paged). The core recency scan.
  - `drive_activity_query` — change feed since a bookmark (`driveactivity.activity.query`): edits,
    shares, comments, moves.
  - `drive_file_get` — single file metadata (`files.get`) to enrich a referenced file.
- **Tasks** (`tasks.readonly`) — overdue/stale to-do scan (`haku/base/sources/tasks.md`).
  - `tasks_lists_list` (`tasklists.list`) and `tasks_list` (`tasks.list`, `showCompleted=false`, with
    `due`/`updated`).
- **Gmail / Calendar** — already console-mediated (reads + bounded writes). Remaining REST-fallback
  parity so the fallback path can be dropped is tracked in `haku/console/TODO.md`; no new work here is
  needed to stop using `$TOK` for these two.

### P2 — held scope, high value, not yet wired

- **Drive content & collaboration** (`drive.readonly`):
  - `drive_file_export` / `drive_file_download` — export a Doc/Sheet/Slide to text/PDF or download a
    binary (`files.export` / `files.get?alt=media`), so Haku can actually _read_ a file it flagged
    (summarize before a meeting, extract an implied task).
  - `drive_comments_list` — comments + @-mentions on a file directed at the operator (`comments.list`);
    a finding source called out in `drive.md`.
- **Google Docs** (`documents.readonly`):
  - `docs_get` — structured document content (`documents.get`) for summarization / task extraction.
- **Google Sheets** (`spreadsheets.readonly`):
  - `sheets_values_get` — a range/tab (`spreadsheets.values.get` / `batchGet`) for trackers, budgets,
    lists the operator keeps in Sheets.
  - `sheets_get` — spreadsheet metadata (`spreadsheets.get`: tab names, structure).
- **Contacts / People** (`contacts.readonly`):
  - `people_search` — resolve a name/email to a person + relationship context
    (`people.searchContacts` / `people.get`), so attendees/senders in other findings are legible.

### P3 — long tail / opportunistic

- **Google Slides** (`presentations.readonly`): `slides_get` (`presentations.get`) — presentation text,
  read on demand when a deck is flagged. Rarely the operator's own working surface.
- **Drive sharing detail** (`drive.readonly`): `drive_permissions_list` (`permissions.list`) — who a
  file is shared with, only when a finding needs it.
- **YouTube** (`youtube.readonly`): weak interest/attention signal, privacy-heavy, no current source.
  Prefer **dropping the scope** over building a tool unless a concrete use appears.

### Retirement mechanic

As each product reaches full console read coverage, drop its scope from the `google` Airlock grant.
When every scope above is covered (or dropped), remove the `google` grant, its ESO, and the
`haku-sandbox` reflection — that completes G3 and ends Haku's Airlock dependency.

## Implementation: tiered, discovery-generated tools

Don't hand-code the schemas. Google API **Discovery Documents** already carry every method's
parameters, request/response schemas, and required scopes; the shipped factory
(`haku/console/tools/google_discovery.py`, first used by the gmail reads) generates clean MCP
`inputSchema`s for reads near-turnkey (writes balloon into deep recursive bodies, so they stay
hand-written). Build the surface as **one hand-written spine plus three tiers of tool specs**.

**Spine (written once):** per-Operator token resolution (`provider_connection.py`), a generic
`googleapiclient` executor that runs any method by id, and the approval envelope. Fixed cost
regardless of tool count.

**Three tiers:**

1. **Fully generated** — simple reads, few params (`tasks.tasklists.list`, `labels.get`,
   `files.get`, `drafts.get`). Point the spine at the method; the generated schema is fine as-is.
2. **Generated + slimmed** (most tools) — big-param reads (`drive.files.list` ~25 params,
   `calendar.events.list` ~19, `messages.list`). Generate the schema, then apply a **subtractive
   overlay**: allowlist the params worth exposing, pin constants (`userId=me`), drop noise
   (`showDeleted`, `corpora`).
3. **Hand-written thin** — writes / shaped ops (`send`, `events.insert`). Author a small
   purpose-built schema (`{to, subject, body}`) and map it to the Google body in code (as the gmail
   drafts tool already does). Discovery is reference, not the tool. Stay approval-gated.

**Overlay, never fork.** A tool spec references `method_id` + an allowlist/pin set; regeneration
re-derives the schema and re-applies the overlay, and **fails loudly if the overlay names a param
Google removed**. That keeps a ~40-tool surface maintainable against Google's API drift — small
specs, not forked schemas.

```python
# tier 1/2 — generated schema, curated surface; policy defaults from httpMethod
GenTool("gmail.users.messages.list", expose=["q", "maxResults", "pageToken", "labelIds"], pin={"userId": "me"})
GenTool("drive.files.list",          expose=["q", "orderBy", "pageSize", "pageToken", "fields"])
GenTool("tasks.tasklists.list")      # nothing to slim

# tier 3 — custom schema + mapper, never the discovery body
ShapedTool("gmail_send", input=SendMail, method="gmail.users.messages.send",
           build=lambda a: {"userId": "me", "body": {"raw": mime(a)}}, approve="operator")
```

**Policy is mostly derived, not configured:** default `GET → auto-approve for authenticated
agents`, non-GET → operator-gated, with a few overrides (the existing `haku/`-label carve-out). The
same discovery `schemas` also generate **response types**, so Python/TS typing rides along at every
tier regardless of input slimming.

**Discovery-doc source (versioning + typing).** Read the docs from the pinned
`google-api-python-client` wheel's bundled static cache (`@pypi//google_api_python_client`) — no
giant schema JSON committed to the repo; the docs are a Bazel dep, pinned with the wheel. If the
wheel's snapshot lag ever matters, git-pin Google's `googleapis/discovery-artifact-manager` instead
(versioned, fresher, still not vendored). Do **not** vendor the raw discovery JSON in-repo (~1.3 MB;
rejected for bloat), and don't read the live Discovery endpoint (latest-not-immutable → not a
reproducible build input). The same `schemas` generate response types, so typing shares the source.
Implemented in `haku/console/tools/google_discovery.py`.

**Build vs. reuse.** Own the discovery→JSON-Schema converter — ~90 lines of stdlib, the mapping is
small and frozen (classic Workspace APIs use no `variant`/`oneOf`), and the value-add (curation
overlay, approval envelope, per-Operator auth) is ours regardless. Make it **fail loud** on any
Discovery construct it doesn't handle rather than mis-convert. Do **not** own execution or type
codegen: reuse `googleapiclient`'s dynamic client for calls, and `datamodel-code-generator` (JSON
Schema → Pydantic) for response types. Avoid discovery→OpenAPI→`FastMCP.from_openapi` — a bigger
dependency that still doesn't yield approval-gated per-Operator tools.

**Frontend types (Zod) — no new pipeline.** The frontend already derives runtime Zod validators + TS
types from the live MCP `tools/list` via `export_mcp_tool_schemas` → `js_json_schema` →
`z.fromJSONSchema` (`frontend/mcp_tool_schema.ts`). A generated tool appears in `tools/list` like any
other, so it flows through unchanged — register it and add it to the exporter allowlist. The one
constraint is on the converter: emit only the JSON-Schema subset `z.fromJSONSchema` accepts (standard
keywords; `enumDescriptions` folded into `description`; `int64→string`; recursion collapsed) — the
Zod-import build step is the gate that catches a violation. **Advertise full generated result schemas
too — do not trim until a large/recursive output actually forces it;** the same `--results` path
generates their validators. (`z.fromJSONSchema` is Zod-experimental — a pre-existing bet, one seam a
Zod bump could churn.)

## Migration order (existing gmail/calendar tools)

Switch the existing hand-written tools in this order, using them as the factory's validation baseline
before building the new surface:

1. **gmail reads** (`labels`/`filters`/`drafts` `list`+`get`, then `messages_get`, `threads_get`,
   `threads_list`) — they already return the REST shape **verbatim**, so a generated tool is
   behavior-identical and can be **diffed** against the hand-written one to prove the factory. Pure
   GET → matches derived auto-approve; lowest blast radius. Surfaces the overlay's expose/pin and any
   param-rename need (friendly `query` vs Google `q`).
2. **calendar reads** (`get_event`, `list_events`, `list_event_instances`) — deliberate, not free:
   they currently return **shaped** recurrence-aware models the frontend/Haku consume, so migrating
   means dropping the shaping (a response-contract change) or keeping a thin shaping layer. Decide the
   shaping when you get here.

**Hold (tier 3, stay hand-written):** gmail writes that build bodies or carry policy — `drafts_create`/
`drafts_update`, `threads_modify_labels`, `labels_patch`/`labels_delete`, `filters_create` (the
`haku/`-label carve-out) — and `create_event` (RRULE building). New Drive/Tasks/Docs/Sheets/People
tools are **born generated** — no migration.
