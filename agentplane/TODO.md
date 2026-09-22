# agentplane TODO

Entries are removed once landed — this is a burn-down, not a changelog.

## Give Grocy SF its own EgressPolicy instead of an MCP ActionGroup

Grocy's own REST API conventionally authenticates with a static `GROCY-API-KEY` header, and
`cluster/k8s/grocy/sf/mcp/config.yaml` shows the MCP server's own OIDC/proxy wrapping is a
separate concern layered on top of it. **Unconfirmed** — check the `grocy-mcp-oidc-sf`
Secret's fields for the actual Grocy API key before assuming this is a drop-in credential
substitution; the grocy-sf MCP server may do more than pass a bare key through (rate limiting,
response shaping) that would need to move somewhere else first.

## Generate the frontend's server types from the Pydantic models, not only the OpenAPI document

`js_openapi_schema` renders `agentplane/app`'s OpenAPI into `components["schemas"][...]`, which the
frontend re-exports one alias at a time in `app/frontend/client.ts`. It works, but the aliasing is
hand-maintained and a union has to be spelled out on the TypeScript side (`ConversationPayloadBody`
is `ConversationPayloadPresent | ConversationPayloadUnavailable` there because the document carries
the members, not the union). Worth checking whether a generation rule can emit the model types
directly, so a new response model reaches the browser without an alias line.

Related and now fixed for the conversation routes, but not elsewhere: a 64-bit cursor declared
`int` in FastAPI puts `integer` in the schema, which forced `as unknown as number` at every call
site because a JavaScript number cannot hold one. `api.py`'s `DecimalCursor` is the shape that
avoids it. Other routes that take 64-bit identifiers have not been checked.

## One name for the payload owner across representations

A `PayloadRef` calls it `owner_item_id`; the manifest and chunk tables, the sync query parameters
and the payload route all call it `owner_id`. Every caller that turns a reference into a request
spells the translation out (`owner_id: reference.owner_item_id`), in Python and TypeScript alike.
STYLE § General wants one concept under one name across representations. The rename touches the
projected JSONB, so it wants an epoch bump — worth doing on the back of W5's, which needs one
anyway.

## Simplify the conversation collection's rotation machinery

`ConversationCollection` carries two `ActiveConversation` instances (one hidden, pre-warming the
next interest), three refs mirroring state so error callbacks do not close over a stale selection,
and a generation counter. It passes its acceptance gates — the 105-item rotation proof and the
disposal check — so this is a readability item, not a defect. Whether the hidden pre-warm still
earns its place depends on what a shape creation costs, which
`agentplane/plans/conversation_sync_latency.md` is measuring.

`ElectricProxy` taking three resolver callables rather than the `TrajectoryStore` they all close
over looks like indirection over one concrete collaborator, and there is no import cycle forcing
it. It was tried and reverted: those callables are what lets `test_electric.py` run 22 proxy cases
with no database, and taking the store would either make them container-backed or need a faked
concrete class. The seam earns its place; leave it unless that test grows a real store for other
reasons.

`conversation_store.tsx` also writes collection lifecycle events to a
`window.__agentplaneConversationCollectionTrace` global that only the browser test reads. It is
there because the sync design requires proving release in the TanStack and Electric caches rather
than in the DOM, so it stays until there is another way to observe that; it is worth revisiting if
the adapter grows one.
