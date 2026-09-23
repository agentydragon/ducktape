# Haku Google access: mediation and Airlock decoupling

How Haku reaches Google (Gmail, Calendar, Drive, Tasks): first moved off Airlock onto
haku-console, and since G4 mediated by agentplane-staging. This is a Haku credential-architecture
plan; the cross-cutting OAuth/identity program that contains it lives in
`plans/oauth_architecture.md`.

Sequenced later than the common Agent lifecycle (H1–H3 there).

## Progress

1. **G1 (done):** the console owns the per-Operator Google connection —
   `haku/console/oauth/provider_connection.py` (Postgres per-Operator refresh storage + in-process
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
3. **G3 (done):** stopped mirroring the read-only `google-access-token` (`$TOK`) into
   `haku-sandbox`, ending Haku's Airlock dependency. Haku's direct Drive/Tasks reads and its
   Gmail/Calendar REST fallback (per-source docs in the `haku-state` repo) went with it; G4
   restored them.
4. **G4 (done):** removed the console's `gmail`/`google_calendar` servers from the deployment
   (#7671). Haku reaches Google through agentplane-staging as its `claude-ai` principal. Reads go
   out from its sandboxes by egress substitution: the `google-readonly` credential, a token from
   Airlock's `google` grant held by the egress proxy, never by the sandbox. Writes are the
   `gmail`/`google_calendar` ActionGroups served by `google-mcp`, which alone holds Airlock's
   separate `google-write` grant: their reads auto-approve, every write waits for the operator.
   The `google-write` consent is still pending, so both groups read `disconnected`. Airlock is back
   in the chain as the broker for both grants, but its tokens reach only the proxy and
   `google-mcp`: the decoupling G3 was for — no Google token in the agent — still holds.

Do not couple G1/G2/G3 to Airlock's unrelated Oura, BSC, or remaining credential consumers.

## Target: a mediator holds every Google grant, the agent holds no standing token

The end state: **a mediator outside the agent — agentplane-staging since G4 — holds the Google
grants and mediates every Google operation; no Google token with standing capability ever reaches
the agent.**

- **High-risk operations — invariant, not a preference.** Anything the operator does not want Haku
  to execute autonomously (sending mail, deleting/modifying Drive files, mutating calendars, …) runs
  only through mediated tools behind an approval policy. A token carrying those permissions must
  never be handed to the agent. This already holds for Gmail/Calendar writes.
- **Low-risk (read-only) — no standing token either.** A read-only token in agent context is still
  a standing bearer secret: leaked through the LLM provider, it reads all of the operator's
  mail/Drive until rotation. Reads go through mediated tools instead; the cost is the read-tool
  surface below.

## Read surface: egress, not tools

G4's `google-readonly` egress route admits GET on `gmail`, `tasks`, `people`, `docs`, `sheets`,
`slides` and `youtube.googleapis.com`, on `www.googleapis.com/{calendar,drive,youtube}/v3/**`, and
Drive Activity's `activity:query` POST. That covers every read this plan used to rank as console
tools to build (Drive recency and activity, Tasks, Docs, Sheets, Slides, Contacts). Build a read
tool only for a need a sandbox request cannot serve.

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
types from the live MCP `tools/list` via `export_tool_schemas` → `js_json_schema` →
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
