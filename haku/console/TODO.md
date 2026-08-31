# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

## Extend the kubectl-passthrough redundancy check past public-coder

`kubectl_passthrough_redundancy_check` (`auto_approval_policies`, `type: kubernetes_passthrough`)
auto-denies a `kubectl-passthrough-mcp` call when the caller's own Kubernetes SAR identity already
covers it, redirecting the Agent to its direct path instead of the operator's broader passthrough
credential. It's scoped to `public_coder_agent` only. A hard auto-deny is only safe when the
redirect target is reliably reachable — otherwise it's a denial with nowhere to go.

`haku_v1` spans three harness contexts, and their _local_ kubectl setups are not equivalent:

- **`haku-sandbox`** (Haku's own pod): kubelet-projected ServiceAccount token, talks straight to
  `kubernetes.default.svc` in-cluster. No proxy, no OIDC round-trip. Robust.
- **Claude Code web sessions enrolled as Haku** (e.g. "Claude 2"): a _different_ mechanism, not a
  weaker copy of the sandbox's. `devinfra/k8s/kubeconfig.py` decrypts `secrets/haku-k8s-jwt.yaml`
  (SOPS) — a JWT the `authentik-jwt-rotation` CronJob's `haku-k8s` entry mints biweekly via
  Authentik `kubectl-sandbox-client-credentials` (`expected_group: haku`) — into a bearer-token
  kubeconfig against `https://kubeapi.allegedly.works`. That route exists specifically because
  Claude Code web's egress goes through Anthropic's L7 TLS-terminating MITM proxy, which kills
  client-cert auth (see `cluster/k8s/kube-api-proxy/README.md`). So this path carries real
  dependencies `haku-sandbox` doesn't: the Gateway/HTTPRoute, the Anthropic proxy round-tripping
  cleanly, and a JWT that's only as fresh as the last biweekly mint. It authenticates as the OIDC
  group `oidc-ksbx-groups:haku`, co-subjected onto the same RoleBindings as the sandbox's SA
  (permissions match), but the transport can degrade independently. These sessions also pick
  `kubectl` vs `kubectl-passthrough-mcp` per call at will — a passthrough call is not itself
  evidence the direct path is down.
- **Console-launched `haku-harness-sandbox` chat sessions**: per its namespace annotation, "no
  ServiceAccount identity" — no local path at all.

But the redirect target doesn't have to be each harness registration's own local kubectl. All three already
share `sandbox_mcp.exec_sandbox` (policy `haku_sandbox_control`, unconditionally in `haku_v1`'s
`any_of` — not something to add, already live). It runs bash inside a pod that uses the real
`haku` ServiceAccount (`sandboxtemplate-haku.yaml`: `serviceAccountName: haku`, bound to
`haku-sandbox-admin` — the same identity `haku-sandbox`'s own pod runs as), reached as an MCP call
through the same `/mcp` connection every context already needs for anything else — so it doesn't
depend on the caller's own local kubectl or JWT setup, and it's just as reachable for the
harness-sandbox context (no local path) as for the other two.

What that path isn't is free. The first `exec_sandbox` call in a session provisions/adopts a
`SandboxClaim` (`provisioning_timeout_seconds: 600` in the `haku-sandbox-mcp` app config), which
can be slow or fail if the warm pool is exhausted — a heavier failure mode than "the redirect
target is unreachable." And it grants arbitrary bash, not a kubectl-scoped
surface: already reviewed and auto-approved for `haku_v1` as "≈ the direct `kubectl exec` Haku's
SA can already run" (`config.yaml`, `sandbox-mcp` server comment), but the redirect trades a
narrow SAR-scoped request for a broad one.

Before extending the check to `haku_v1`: point its denial message at `exec_sandbox`, not "your own
kubectl" (untrue for two of three contexts); and decide whether provisioning latency/failure is an
acceptable cost for a hard auto-deny, or whether the check should confirm a live claim (or that one
can be provisioned) before denying, rather than assuming reachability the way it can for a
same-cluster ServiceAccount.

## The console as a channel, not a viewer

Direction set 2026-08-15: Matrix and the console frontend should be two **messaging channels**
onto one conversation, each able to do broadly what the other can. The lifecycle facts are in the
conversation record; projecting that record consistently onto both channels is what remains.
Design, the parity gaps it closes, and the traps in each:
<plans/conversation_layers.md>.

The channel-neutral allocator and conversation-harness supervision are complete. Web and Matrix
offer prompts by conversation; Matrix no longer creates, replaces or tends sessions. Delivery is
attachment-scoped: each bound room's subscriber reads its own conversation for replies, sealed
notices — relayed prompts and silent turns included — and the two editable span lines, all off its
own cursor that advances only after the homeserver accepts what it is owed; the room's own copy
suppresses replays, and a second invite binds and serves a second room beside the first. What
remains, in dependency order:

1. **Add Matrix commands**, beginning with abort, as ingress interception rather than an agent tool.
   Prefer a prefix Element does not consume (for example `!haku stop`) over an assumed-free slash
   command.
2. **Interlink the channels**: durable console-session links in Matrix, `matrix.to` links in the
   console, and session ↔ tool-call navigation. A posted Matrix event is permanent and federated, so
   mint only routes intended to survive.

## Notification text per tool kind

A push notification is titled with the tool's shared action description
(`frontend/tool_rendering/<server>/actions.ts`) — the same one-line summary the approvals card's
identity line shows. That is the right default, but a notification is a different surface: no
arguments visible, no expand affordance, read on a lock screen, and it is the one place a call
can be approved without seeing its arguments at all. Some tools would be better served by
notification-specific wording — naming the actual target ("Delete Pod haku-console-7f9 in
haku-console") where the card can rely on the widget below it to show that.

Add an optional per-tool notification override alongside the action description, falling back to
it when absent. Deliberately not done in the change that introduced push: the shared description
is the honest starting point, and which tools actually warrant divergence is worth learning from
real notifications rather than guessing up front.

## `gmail` MCP server — Gmail API affordances not yet exposed

The in-process `gmail` server (`tools/gmail.py`) currently mirrors a slice of Gmail's REST
API (thread/message/label/filter reads, draft CRUD, thread-label changes, label CRUD, filter
create/delete). Add the rest as approval-gated tools when a workflow needs them — each maps
to a Gmail API method:

- **Send / reply** — `users.messages.send`, `users.drafts.send`. High blast radius (mail
  leaves the account); keep firmly approval-gated, never a candidate for auto-approve.
- **Delete / trash** — `users.messages.{trash,untrash,delete}`,
  `users.threads.{trash,untrash,delete}`. `delete` is permanent; `trash` is recoverable.
- **Message-level label changes** — `users.messages.modify`, `users.messages.batchModify`
  (today only whole-thread label changes are exposed, via `threads_modify_labels`).
- **Attachments** — `users.messages.attachments.get` (fetch attachment bytes).
- **Raw import/insert** — `users.messages.{import,insert}`.
- **History** — `users.history.list` (incremental sync since a `historyId`).
- **Settings** — `users.settings.*`: forwarding, vacation responder, send-as, delegates,
  language, IMAP/POP (filters are already exposed).
- **Watch / stop** — `users.{watch,stop}` (push notifications; needs a Pub/Sub topic).

**Draft message shape is flat.** `drafts_create`/`drafts_update` take plain-text
`to`/`cc`/`bcc`/`subject`/`body` and build the MIME server-side, so they leave a lot on the
table — attachments, an HTML alternative part, arbitrary headers, or a raw RFC 2822 message.
If a workflow needs more than the flat fields, accept a richer message representation from the
client (e.g. an `html_body`, an attachments list, or a raw passthrough) rather than growing the
flat parameter list one field at a time.

## `google_calendar` MCP server — Calendar API affordances not yet exposed

Audited against the Google Calendar API v3 reference on 2026-07-14:
<https://developers.google.com/workspace/calendar/api/v3/reference>. The current server exposes
`create_event`, `get_event`, `list_events`, and `list_event_instances`; authenticated-agent reads
auto-approve, while creation stays operator-approved. The remaining public API is intentionally
deferred:

- **Event recurrence and mutation** — accept Google-supported `RDATE`, `EXDATE`, and `EXRULE`
  content lines; update or delete a whole series or one instance; and implement "this and
  following" as the documented trim-old-series + insert-new-series operation. Before exposing
  these, specify exception preservation, optimistic concurrency, attendee notifications, and
  partial-failure recovery.
- **Remaining Events methods** — `events.delete`, `import`, `move`, `patch`/`update`, `quickAdd`,
  and `watch`. Deletes/moves/updates need explicit approval scope and etag behavior; import and
  quick-add need clear reasons to coexist with typed creation; watch needs durable callback and
  renewal infrastructure.
- **Remaining Events list/sync controls** — incremental `syncToken`/`nextSyncToken`, `updatedMin`,
  `showDeleted`, `showHiddenInvitations`, `iCalUID`, `eventTypes`, private/shared extended-property
  filters, `maxAttendees`, `orderBy`, and response `timeZone`. Add these as real workflows emerge,
  preserving Google's incompatible-parameter rules in the MCP schema.
- **Remaining Event fields** — attachments, Meet `conferenceData`, attendee `sendUpdates`, custom
  event ids, colors/event labels, visibility/transparency, guest permissions, source, extended
  properties, reminders using calendar defaults, and specialized birthday/focus-time/
  out-of-office/working-location event types. Each addition needs typed arguments, an approval
  preview, and tests against that event type's Google restrictions.
- **Calendar discovery and availability** — `calendarList.get/list/insert/patch/update/delete/watch`,
  `calendars.get`, `colors.get`, `freebusy.query`, and `settings.get/list/watch`. Read-only discovery,
  colors, free/busy, and settings may be candidates for standing read approval; calendar-list
  mutations remain manual. Watch methods share the push-infrastructure prerequisite below.
- **Calendar administration and sharing** — `calendars.insert/patch/update/delete/clear`,
  and every `acl.get/list/insert/patch/update/delete/watch` method. These need separate
  administrative intent, destructive confirmation, and any additional Google or Workspace-admin
  scopes; `clear` and ACL writes must never auto-approve.
- **Push channels** — resource watches plus `channels.stop`. Do not expose until haku-console owns
  authenticated webhook delivery, durable channel metadata, expiration renewal, deduplication,
  replay/catch-up, and cleanup on disconnect.

## MCP server (`/mcp`) — deferred follow-ups

The `/mcp` server (`mcp/server.py`) now resolves canonical Operators, Agents, grants, and
credential bindings through one authority, and derives each request's tool surface from that
Agent's Operator connections. Settings lists the Operator's Agents and lets an OAuth Agent's
auto-approval policy be reassigned among the roots `config.yaml` defines. The architecture is
specified in <../../plans/oauth_architecture.md>. The next product slices are:

- **Fuller Agent detail** — `AgentView` carries name, status, credential kind/status, and the
  creation/activation/last-seen times. Client software, granted scopes, and reconnect history are
  in the durable graph and are not yet surfaced.
- **Agent-filtered history** — filter past tool calls by Agent only after applying the
  authenticated Operator predicate. Resolve display names through canonical joins; never copy
  them into tool-call rows or use them as authority.
- **Agent lifecycle controls** — expose revoke/disable, rename/history, and tombstone/reconnect
  operations as vertical API + UI + audit-event slices.
- **Author a policy in the UI** — reassignment picks among deploy-defined roots; composing a typed
  structured policy in the console is what remains.
- **Per-Agent tool surface** — derive request-time `list_tools` from the verified binding and
  policy, with `tools/list_changed` on policy edits. Do not key authorization directly on an
  unverified DCR `client_id`.

## Serve a last-known tool catalog for a degraded server

A degraded server reports no tools at all, so an agent can see that `home-assistant` exists, see
exactly why it is unreachable, and still not learn a single tool name — even though the console
reflected that catalog successfully minutes earlier. Connection state and catalog knowledge are
orthogonal: a tool list is _what this server has_, not _may this caller reach it right now_.
Operator decisions already taken (2026-08-10):

- **Status reads only.** `get_mcp_server_status` may serve a stale catalog, explicitly marked with
  when it was reflected. `tools/list` must keep contributing nothing for a degraded server —
  discovery deliberately fails closed once an Operator disconnects, and handing back
  callable-looking proxy tools would reverse that. Knowing a name is not authorization: execution
  re-resolves credentials and still fails.
- **Persisted in Postgres**, not in the reflection cache. Two reasons, both load-bearing:
  - The cache key is `(server_id, config_fingerprint, credential_fingerprint)` and that third
    component _is_ the fail-closed property (see `mcp_reflection_cache`'s module docstring). A
    last-known lookup cannot use it, so this needs its own key — scope it per
    `(operator_id, server_id, config_fingerprint)` so one Operator's tool list never surfaces for
    another, since upstreams may vary tools by account.
  - The cache is per-replica, in-memory, and `_prune` drops entries at expiry (60s default), so
    there is no long-term memory to serve and a rollout would empty it anyway. The outage that
    motivated this ran three days.

Two traps for whoever picks this up:

- **The failure that motivated this never reaches the cache.** `home-assistant` was
  `failure_stage: credential_resolution`, and `metadata_for_operator` returns `DegradedReflection`
  before it ever calls the dispatcher. Only `tool_discovery` failures get that far, so the
  last-known lookup belongs in `get_mcp_server_status`, above the dispatcher — not inside
  `McpServerDispatcher.metadata`.
- **`_exposed_metadata` early-returns on `DegradedServerState`.** Stale tools must go through the
  same projection, or a caller gets raw upstream schemas with no `approval_mode` and sends the
  wrong payload shape to `call_mcp_tool` — the exact failure the exposed reflection exists to
  prevent.

## Operator browser auth — parked remainders

The browser login flow is fixed in #3516/#3519 except for:

- **A background 401 still navigates the tab** (audit F3). Expiry is now announced beforehand and
  re-authentication returns to the same page, but the redirect itself is still fired by whichever
  poll happens to fail first, and the top-level navigation discards whatever is unsaved in the
  framed haku-ui. The alternative is an explicit "session expired — sign in" state the operator
  clicks, so the frame survives until they choose. Superseded entirely if session renewal lands
  (<plans/operator_session_renewal.md>).
- **No sign-out affordance** (audit F6). `/auth/logout` exists and is exact-Origin gated, but
  nothing in the SPA calls it, and it clears only the console session — not Authentik's — so a
  manual logout silently re-logs-in on the next 401. Needs RP-initiated logout to be meaningful.

## The chat harness registration's timings are module constants, not configuration

`HarnessRegistrationConfig` carries the deploy wiring (namespace, warm pool, proxy, MCP URL) and
exactly one timing — `session_ttl_seconds`. Every other number the harness registration's behaviour depends on is a
module-level constant, so changing one is a code edit, a CI build and a roll. The ones that are
genuinely operational knobs should move onto the config model:

- `channels/matrix/spans.py` — `STATUS_AFTER` (8s before a turn says anything, R6.2),
  `STATUS_EDIT_INTERVAL` (5s edit floor, R6.5), `TYPING_REFRESH`.
- `session/store.py` — `LEASE_TTL` / `LEASE_RENEW_INTERVAL`, `PROVISION_LEASE`, `ADOPTION_GRACE`.
- `channels/matrix/pacer.py` — `SENDS_PER_SECOND`, `SEND_BURST`, `MAX_QUEUED_SENDS`, `FLUSH_SECONDS`.
- `channels/matrix/conversation.py` — `SUPERVISE_INTERVAL`, `PROVISION_BACKOFF`,
  `RE_AWAKENING_MESSAGES` (the N of R3.3a).
- `channels/matrix/conversation_subscriber.py` — `POLL_INTERVAL`, `ERROR_BACKOFF`; and
  `MAX_BACKFILL_PAGES` / `TIMELINE_LIMIT` from `channels/matrix/client.py`.
- `runner/runner.py` — `MAX_DISCONNECTED_SECONDS`, `REPLAY_WINDOW`,
  `RECONNECT_{BASE,MAX}_DELAY`. **These live in the runner**, whose image is pinned at claim
  creation, so they are not console config at all: they reach a running sandbox only through the
  launch, or not until it is replaced.

**Not everything here is a knob, and the split is the point.** `TYPING_TIMEOUT_MS` and
`SYNC_TIMEOUT_MS` are the homeserver's own semantics, `MAX_RATE_LIMIT_RETRIES` exists to bound a
nio behaviour (<docs/conversation_runtime_facts.md>), and the `*_FRAME_KIND` strings are wire vocabulary.
Making those configurable would invite a deploy that contradicts a protocol. Move the timings;
leave the facts where the code that depends on them can be read beside them.

Two things worth settling in the same change, since they are the same question: the three ingress
values nobody has chosen — the **batch size cap**, the **debounce window** and the **age fence**
that makes a very old message context rather than work — should arrive as config with a default
rather than as
another constant, because the whole reason they are unchosen is that the right value is an
operational finding. And a value read per use rather than at startup is what makes tuning a
ConfigMap edit instead of a roll.

## Finish the `claude_chat` → `session` rename

The tables, the wake channel, the Python, the operator routes and the SPA moved; one thing is
deliberately still holding the old name.

- **`/internal/claude/runner/{session_id}`.** Left alone on purpose: the runner image dials it, so
  renaming it is a coordinated two-sided roll, not part of a console-only change.

## Give `system/compact_boundary` a real branch in the projection

Both are now captured — `haku/cli_protocol/probes/compaction.py` drove a session with hooks, an
in-process tool and a forced compaction; see <../cli_protocol/protocol.md> § Compaction. What that
leaves is work here rather than a question.

The cursor is safe: nothing is retracted, so `project` from an old cursor replays exactly what it
replayed before. What is not safe is the default branch. A compaction emits `system/compact_boundary`
and then an inbound `user` frame carrying the summary, marked only by `isSynthetic: true` — so a
projection that ignores the boundary and renders `user` frames produces the pre-compaction turns
**plus** a summary of them, attributed to a user who never sent it. Both frames land in
`unprojected` today.

The rule to implement is positional (everything before the boundary's offset is out of the model's
context); `logical_parent_uuid` looks like the relink for it and names a message the wire never
carries. A **partial** compaction — `compact_metadata.preserved_segment`, documented by the CLI's
schema — is still unobserved, so leave it unimplemented rather than guessed.

## Audit and curate generated Google tool descriptions

The Google Discovery-generated schemas currently carry Google's copied descriptions verbatim. Do
not hand-edit them as part of unrelated MCP guidance work; audit and curate their live-schema
verbosity in a dedicated follow-up, with client-facing token budgets and semantic tests.

## Scope conversation reads to the reader's trust tier

**The policy is decided** (operator, 2026-08-15): an agent reads the transcripts and
indexes its tier grants. The fence is the tier, not the room, so cross-room and cross-session
reads remain open within a tier.

Most of the boundary is built: named logical indexes with per-profile `recall_index_ids` grants
enforced server-side, and one profile-DAG read authorizer (`conversation_read_access.py`, #4431
stage 5) that fences both `haku_conversations` drilldowns and `haku_index` chat search on the
conversation's pinned `access_profile_id` — semantic discovery and direct drilldown share one
boundary, and unknown/unpinned data fails closed for agents.

What remains is the tier generalization, when several agent kinds and shared rooms arrive:

1. Add a tier to agent kinds and Matrix rooms, derive each conversation's label from them (room
   tier authoritative where both exist) instead of equating label with the launch profile; data
   predating any label keeps reading as highest trust.
2. Decide whether tier-specific chat indexes replace the per-conversation profile join, keeping
   `chunks` as the shared embedding cache either way.

Full trust design: <../plans/information_trust_tiers.md>. Current index operation and the RLS
alternative: <../recall_index/README.md> § Read scoping.

## Recall/server access should ride on access tier, not be hand-declared per agent

`recall_index_ids` and `in_process_server_ids` are each spelled out independently per
`access_profiles` entry in `config.yaml`. That let `public-coder` carry
`recall_index_ids: [ducktape-public]` with no `haku_index` in its `in_process_server_ids` —
the grant was structurally unreachable and nobody noticed until an approval-ledger audit
(fixed in #4696). Distinct from "Scope conversation reads to the reader's trust tier" above
(that's the room/session fence _within_ an index; this is _which_ indexes and servers a
profile can reach at all) but the same shape of problem: two lists that are supposed to move
together but can silently drift apart because nothing ties them to one tier concept.

## A second runner, and binding an agent to one

Two wants on one axis: the console launches a runner that is not Claude Code, and it holds several
agent identities that differ in which runner each gets. They meet at the session row — which Agent a
session runs as is also what says which runner implementation to launch for it.

### A runner that talks to OpenAI models

Through the Codex app server, or directly against the Responses API, either way routed at the
in-cluster LiteLLM — <../../cluster/k8s/litellm/app/proxy-config.yaml> already carries
`chatgpt/oai-responses/*` models on the `openai/` provider. Today `x/claude_code/` is the only runner, reached over the
runner protocol; the frame log and its adapter are what keep a runner's shape below the conversation layer
(<docs/conversation_layers.md>).

**This is the first real test of the neutrality the conversation layer claims.**
`ConversationEventKind` and the frame adapter exist so that a second backend is possible, and
nothing has ever exercised that — every claim in this repo about one is read from the sketch in
<../runner/docs/second_backend.md> rather than measured.

**The seam is the frame protocol, not `CliBackend`.** A runner is anything that dials the console
with the session token and speaks frames over the runner protocol, which is why <../runner/backend.py> can say
almost nothing below the envelope is Claude-specific. `CliBackend` answers one question — how to get
frames out of a **child process** — so it is what a runner uses when it happens to wrap a CLI, and
`second_backend.md`'s subprocess assumptions (a binary to resolve, argv, `replayable` over a child's
stdio) are about that case rather than about runners. A runner implementing the Responses API loop
itself is therefore a peer of the CLI-wrapping runner, producing frames directly instead of pumping
a child's stdio. Codex is interesting because its embedded app server may let it fit that same
shape — a process we speak a protocol to and translate into frames — rather than being a third kind
of thing.

What the sketch still names correctly is what the seam does not cover: selecting an adapter, the
control channel's `control_request` spelling, and choosing a backend per session.

### More than one agent

Several agent identities, each with its own permissions and its own session runner — an
OpenAI-driven agent with one permission set on one runner, Haku with another on Claude Code.

**A runner runs _as_ an Agent** (operator, 2026-08-18), and talks to `/mcp` as that Agent, so the
two are one identity and the binding belongs on the session: a session names the Agent it runs as,
beside the operator and the conversation it already names.

**Enrollment is unaffected.** An agent in an external harness still reaches `/mcp` by static token
or dynamic client registration exactly as now. This adds a way to _be_ an Agent — one the console
launches — beside the way an Agent arrives from outside, and replaces neither.

**The permission machinery is now wired through the session.** `agents/` holds the canonical Agent
domain and enrollment selects the Agent's access profile. A conversation pins Agent/profile/harness;
each session pins the credential binding that authorized that sandbox. Allocation mints one
session token for the runner websocket and direct `/mcp` calls. It arrives through the
SandboxClaim environment and is intentionally available to the provider CLI and its child commands;
Console resolves it back to that specific session and pinned identity. Harness configuration
therefore holds an MCP endpoint, not a static Agent credential. Each configured harness pins the
Agent/profile whose sandbox pool, prompt, environment, and MCP endpoint it owns.

The claim carries that same bearer under the runner variable and an MCP alias. This is rollout
compatibility, not a second credential: the previous runner strips the runner-named variable from
Claude but passes the unknown alias, while the new runner preserves both.

**The remaining runner half is independently extensible.** A new frame-speaking implementation still
needs its own adapter and deploy configuration. Each implementation remains singular in namespace,
warm pool, provider placeholder, system-prompt template and MCP URL; a concrete need for multiple
instances of one implementation kind would replace that with keyed harness instances without
changing session Agent identity.

## Small cleanups

- The comment above `_operator_auth_requires_canonical_public_origin` (`config.py`) describes an
  optional standing Kubernetes authorization policy field that is not on `Settings` —
  `kubernetes_authorization` is on `ConsoleConfigFile` in `mcp/config.py`. Delete the comment.
- `HistorySender.ASSISTANT` (`session/system_prompt.py`) reads like the provider-LLM-API `assistant`
  role; it records harness-side provenance of a recorded message. Rename to say so (e.g.
  `HARNESS`) once the in-flight StrEnum PRs land.
- `approval_mode` (`ApprovalMode` in `haku/shared/haku/console/tool_calls.py`, mirrored on
  `mcp_approval.ToolMetadata`) conflates "which input-schema shape does the proxy tool advertise"
  (enveloped vs raw) with "does a call auto-approve". They happen to map roughly 1-1 today, but
  the interface should not encode that coupling — split the schema-shape signal from the
  approval-policy signal.
