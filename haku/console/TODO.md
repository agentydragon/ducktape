# haku/console TODO

Project-level TODOs for the console. Design rationale lives in `README.md`; this is the
actionable checklist. Remove entries once done.

## Extend the kubectl-passthrough redundancy check past public-coder

`kubectl_passthrough_redundancy_check` (`auto_approval_policies`, `type: kubernetes_passthrough`)
auto-denies a `kubectl-passthrough-mcp` call when the caller's own Kubernetes SAR identity already
covers it, redirecting the Agent to its direct path instead of the operator's broader passthrough
credential. It's scoped to `public_coder_safe_reads` only. A hard auto-deny is only safe when the
redirect target is reliably reachable — otherwise it's a denial with nowhere to go.

`haku_v1` spans two contexts, and their _local_ kubectl setups are not equivalent:

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

But the redirect target doesn't have to be each context's own local kubectl. Both already
share `sandbox_mcp.exec_sandbox` (policy `haku_sandbox_control`, unconditionally in `haku_v1`'s
`any_of` — not something to add, already live). It runs bash inside a pod that uses the real
`haku` ServiceAccount (`sandboxtemplate-haku.yaml`: `serviceAccountName: haku`, bound to
`haku-sandbox-admin` — the same identity `haku-sandbox`'s own pod runs as), reached as an MCP call
through the same `/mcp` connection every context already needs for anything else — so it doesn't
depend on the caller's own local kubectl or JWT setup.

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

## Audit and curate generated Google tool descriptions

The Google Discovery-generated schemas currently carry Google's copied descriptions verbatim. Do
not hand-edit them as part of unrelated MCP guidance work; audit and curate their live-schema
verbosity in a dedicated follow-up, with client-facing token budgets and semantic tests.

## Recall indexing is disabled

The deployed console intentionally leaves Recall index configuration, the `haku_index` MCP server,
and index-maintenance workers unwired. The `recall_index` database schema and data remain for a
future re-enable; restore source/embedding workers and review catalog/access-profile exposure
together when that work resumes.

## Small cleanups

- The comment above `_operator_auth_requires_canonical_public_origin` (`config.py`) describes an
  optional standing Kubernetes authorization policy field that is not on `Settings` —
  `kubernetes_authorization` is on `ConsoleConfigFile` in `mcp/config.py`. Delete the comment.
- `approval_mode` (`ApprovalMode` in `haku/shared/haku/console/tool_calls.py`, mirrored on
  `mcp_approval.ToolMetadata`) conflates "which input-schema shape does the proxy tool advertise"
  (enveloped vs raw) with "does a call auto-approve". They happen to map roughly 1-1 today, but
  the interface should not encode that coupling — split the schema-shape signal from the
  approval-policy signal.
