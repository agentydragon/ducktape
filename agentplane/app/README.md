# Agentplane integration app

The browser and agent surface over Agentplane's sandboxes: a FastAPI service that stamps
Sandboxes from a `SandboxTemplate`, dials each runner Pod over the runner protocol,
projects runner events into PostgreSQL thread entity rows, synchronizes selected rows and content
to the browser through authenticated Electric endpoints, and watches Kubernetes inventory.
Raw runner events remain archived for debug access.
The staging instance lives in `cluster/k8s/agentplane-staging/`.

The current bridge's session-scoped runner attachment is implementation state, not
the desired product model. [Thread, runner, and harness layering](../docs/thread_layering.md) is
the authoritative design for submission durability, the app queue decision,
multi-session Thread history, and folded/Raw projections.

```sh
bbr test //agentplane/app/...
```

## Layout

- `main.py`: the entry point; `Settings` names every knob as a flag, an `AGENTPLANE_*`
  variable, and a key of the YAML file `AGENTPLANE_CONFIG_FILE` points at.
- `presets.py`: app-owned `SandboxPreset` and `ThreadPreset` configuration and concrete launch
  resolution. Preset names stop at the app boundary; Kubernetes and the runner receive resolved
  fields.
- `inventory.py`: the sandbox inventory read from and written to Kubernetes (create, suspend,
  resume, delete), with the parsed subset of each CR it needs. It works in
  `--sandbox-namespace`, which is not the app's own: a sandbox is the blast radius, and it shares a
  namespace with neither the app, its database, nor the rules below.
- `egress.py`: the app namespace's `EgressPolicy` and `EgressBinding` resources as the app shows and
  edits them. A binding is desired state, so creating one is the whole grant and deleting it the
  whole revocation; there is no decision recorded on the rule afterwards. A sandbox may be granted
  after it is running, and each grant is a binding of its own so its expiry and revocation are its
  own ([the composition doc](../docs/egress_composition.md)). A binding Flux applied is the
  repository's to remove, so revoking one is refused with 409 rather than deleting an object the
  next reconcile re-creates. `decisions.py` reads the proxy's recent decisions off its admin port,
  and an unreachable proxy leaves the rules readable.
- `action_policy.py`: the sandbox namespace's `ActionPolicyBinding`s as the app writes them, and
  the Action Service's answer for a Sandbox as the app shows it. A preset's `action_policy_sets`
  become one binding per Sandbox the app launches, owner-referenced to it and labelled
  `app.agentplane.allegedly.works/managed-by: integration-app`; the Action Service evaluates
  bindings and reads `spec` only, so no preset name reaches it. The read side asks the service
  (below). Nothing edits a binding at runtime; kubectl does.
- `bridge.py`: runner-first commands, leased batched ingestion per sandbox, and retained-event
  replay; `api.py` is the REST surface and the OpenAPI schema `export_schema.py` emits
  for the frontend's generated client.
- `client.py`: a Python client over the app's HTTP surface, speaking the app's own request and
  response models and the runner protocol's `Event` messages.
- `live.py`: one list-and-watch over Sandboxes, their Pods and the egress objects
  (`../kubernetes_watch.py`), the action policy kinds watched only as a trigger to re-ask the Action
  Service, and the SSE streams that push a snapshot of it to every open tab.
- `changes.py`: the payload-free wake-up a reader of the cluster index or the trajectory store waits
  on; a burst of changes coalesces into one re-read.
- `identity.py`: whether a request proved itself, by whichever credential it carried; `oidc.py` and
  `auth_routes.py` are the browser's half of that (see below).
- `trajectory.py`: the PostgreSQL store of threads, events, feed state, leases, materialized
  thread entities, and immutable content chunks/manifests. Each ingestion transaction
  folds only the batch and its touched entities, then commits all projection writes and checkpoint.
  `trajectory_updates.py` turns committed PostgreSQL notifications into replica-local wakeups.
- `thread_fold.py`: typed deterministic event fold with independent item revisions.
- `electric.py`: authenticated, scope-checked metadata, selected-command, and payload shape proxy.
  The private Electric service reads PostgreSQL logical replication; app replicas do not retain
  per-listener thread copies.
- `action_federation.py`: request-bound operator federation into the canonical Action Service.
- `consent.py`: browser-session-bound enrollment BFF; the Action Service owns consent and grants.
- `operator_sessions.py`: PostgreSQL browser identity and pending OAuth state, shared across replicas.
- `database_migrate.py` and `migrations/`: the Alembic history covering the shared `Base` declared
  in `operator_sessions.py` and reused by `trajectory.py`'s tables. Migrations run separately through
  `:migrate`; the server itself never creates or checks tables at startup. `:image` and
  `:migration_image` are separate OCI targets.
- `frontend/`: the React SPA on the repo's `ts_library` and esbuild toolchain, with the visual
  scenarios under `frontend/visual/`.

## Live views

### Replica-safe runner delivery

Every app replica can send commands through an independent runner attachment. Generated `Command`
payloads go to `POST /threads/{id}/commands` with stable command ids. The runner serializes admission
and deduplicates retries. HTTP success returns the exact archived `CommandAdmitted` entry, only after
the contiguous PostgreSQL prefix includes it; admission does not claim native execution. Identical
retries return that entry before contacting the runner; a reused id with different payload is rejected.
There is no database command queue.

One app replica leases each sandbox's ingestion in PostgreSQL. Each write locks and validates
the lease token against database time, so an expired owner cannot write after takeover. A batch
must extend the contiguous copied prefix with the same runner source and original sequence;
replayed entries must match their complete stored payload. Gaps and conflicts reject the whole
batch. The maximum stored cursor is therefore the copied checkpoint, committed atomically with
the Events and event-derived feed updates without a separate counter.

`Attached` is a runner snapshot at its own `last_cursor`, persisted before replay. During catch-up
it can be ahead of the app's copied Event prefix; its model/turn state is not a fold of only that
prefix. Subsequent Events advance it without rewinding through older replay. Consumers must keep
that snapshot provenance separate from thread-entity and pending-command projections of copied Events.

Reconnect replays the last copied entry too, verifying its identity before extending the prefix.
A conflict or a stream ending before its advertised cursor records a feed error, not a normal
completion; the archived prefix stays readable. A graceful exit releases leases, while a crashed
owner is replaced after its lease expires. This coordinates ownership, not balanced placement.

Sandbox discovery and runner addresses use the existing Kubernetes list/watch cache, including
relist recovery; watch changes wake reconciliation. The timer renews leases and discovers sessions
via runner `ListSessions`, which currently has no watch RPC. Merely observing a stopped session
does not restart its harness. Explicit Open starts/resumes it and waits for ingestion to catch up
before returning, so a resumed browser does not read the previous harness's terminal state.

The retained-event SSE API reads committed PostgreSQL events on whichever replica receives
its request. Transactional `NOTIFY` wakes event/archive and inventory readers; notifications
are hints and the database cursor remains authoritative.

`/#/threads/{id}` loads metadata and a bounded latest-30 entity interest independently
of runner discovery. TanStack DB owns synchronized server rows; Electric supplies snapshot,
live changes and reconnect. Earlier history uses exclusive cursor windows. Text and tool
arguments follow their referenced revisions, while reasoning, tool output and associated debug
frames are selected on demand. A command-ID subscription retains outcomes after the command
leaves the visible history. Local authored intent, unsent drafts and viewport/disclosure state
remain separate from these server collections. See [the sync design](../docs/thread_view_sync.md)
for query, revision and memory contracts and the remaining acceptance gates.

Sidebar entries remain navigable after Sandbox deletion. Availability comes from the separate
live inventory snapshot; suspended/deleted Sandboxes disable runner controls. Unfinished
retained items are labelled incomplete rather than actively streaming. The ingestion coordinator
owns runner attachment; loading a thread never rebuilds its history.

`test_replication_process.py` kills real app processes before and after an ingestion commit,
then verifies replacement lease ownership, the exact PostgreSQL prefix, and HTTP/SSE replay/live
handoff from the browser's last observed cursor. The test advances the dead owner's database lease
expiry explicitly and uses a controlled protocol source; it does not test elapsed lease timing,
native harness recovery, or PostgreSQL host/storage loss.

`test_thread_browser.py` loads the built SPA in hermetic Chromium against real application
processes, PostgreSQL and Electric through a real HTTP/2 ingress. Only the runner source,
Kubernetes and authentication boundaries are controlled. Its cases are split across four Bazel
shards. Traces and screenshots are test artifacts; inspect the images as well as results.

Coverage includes older-item streaming, selective bodies/debug, archived threads,
reload/offline/reconnect, admission responses ahead of projection, lost HTTP replies, exact
same-ID retries, and failed/noop outcomes after history eviction and explicit dismissal.
Fault injection holds actual Electric responses while ingestion continues. Rejected runner
suffixes preserve verified history, report rejected versus verified cursors, and disable controls.
Scrolling checks cover live growth, reader anchors, desktop/phone layouts, and returning to the
bottom before a queued scroll event. Larger history-window, collection-retention and resource
proofs are tracked in [the acceptance matrix](../debug/conversation_acceptance.md).

The new projection schema is incompatible with populated pre-projection staging/testing
archives: reset the disposable trajectory data before applying it. There is no implicit
backfill, tolerant old-row reader, or on-open replay. Migration `0005_conversation_projection`
creates the materialized tables, grants and publication, which `0006_thread_fold_rename` renames
to the `thread_*` family; the managed `electric` database role must already exist. Changing projection epochs requires an explicit reset/rebuild rather than
mixing incompatible state. No instance reset is performed by this implementation work.

Rollout prerequisite: existing sandbox runners must support independent attachments before the new
app bridge is deployed; old runner processes are not upgraded merely by publishing the new image.
Staging runs two app replicas on separate nodes with `RollingUpdate` (`maxUnavailable: 0`,
`maxSurge: 1`) and a PodDisruptionBudget of one available; testing stays at one replica.

### Inventory snapshots

`/live/sandboxes` and `/live/sandboxes/{name}` are SSE, authenticated like every other route. Each
carries a whole `snapshot` of what it covers whenever that changes, and a `health` frame through
the quiet in between. What is pushed: the sandbox rows, one sandbox's egress bindings, its action
policy, and its threads, the last of these from the store rather than a watch, since the app is the
only writer of a thread's name.

Two things stay request-shaped, both because their source offers no stream. The proxy's recent
decisions live in its memory and the egress tab still asks for them on an interval; the runner
answers `ListSessions` per request, so the sandbox page re-reads the session table when the
sandbox's Pod comes or goes and when a session opens (which reaches it as a new thread).
An unavailable runner shows a waiting state and retries until it answers, without requiring a
reload. Session creation displays progress and prevents duplicate clicks while the request runs.

A snapshot is not a delta, and there is no resumable id: a relist replaces a kind wholesale and a
`resourceVersion` expires, so a reconnecting tab is served a fresh snapshot instead. That leaves
one failure to handle honestly -- a watch that has wedged looks exactly like a cluster where
nothing is happening -- so every frame carries how long ago each kind last completed a cycle,
against the same three-cycle bound the egress proxy's `/healthz` uses, and a page whose data has
stopped moving says so rather than showing it as live.

## Authentication and authorization

Every API route needs a caller; only `/healthz`, `/readyz`, the `/auth/*` endpoints, the service
worker at `/sw.js`, and the SPA's static files mounted at `/` answer without one. There are two credentials,
and both are cryptographic:

- **An operator's OIDC session.** `AGENTPLANE_OIDC_ISSUER` and its siblings register the app as an
  Authentik client; `/auth/login` runs an authorization-code flow with PKCE and stores the
  `iss`, stable `sub`, and display `preferred_username` in PostgreSQL behind an opaque signed `__Host-` cookie. Unsafe methods additionally have to be
  same-origin, which is the CSRF defence `SameSite=lax` leaves open. Unset the issuer and there is
  no login at all, which is how the tests and a local run work.
- **A Kubernetes token.** `Authorization: Bearer <token>` goes to TokenReview, which returns the
  username the API server vouches for. The token has to carry the app's audience
  (`--token-audience`, `agentplane`), so a token minted for anything else cannot be replayed here,
  and the username has to be one `--token-subjects` names, or the app answers 403. The audience
  would not be a gate on its own: RBAC decides which ServiceAccount a principal may mint a token
  for, never which audience it asks for, so an unnamed subject is refused however it minted. Empty
  -- the default -- accepts no token caller at all. On staging the list holds one entry, and an
  agent mints for the ServiceAccount that exists only to be that identity:

  ```sh
  TOKEN=$(kubectl -n agentplane-staging create token agentplane-agent --audience=agentplane)
  curl -H "Authorization: Bearer $TOKEN" https://agentplane-staging.allegedly.works/sandboxes
  ```

Nothing is inferred from a request header, and nothing in front of the app authenticates for it:
the gateway routes straight to the Service. The app used to sit behind an Authentik forward-auth
outpost and trust the `x-authentik-username` it set, on the grounds that the outpost was the only
way in. The API server's service proxy was the other way in and it forwards caller-supplied
headers, so anyone with `services/proxy` on the Service could grant themselves egress.
Owning the login also drops the outpost's 15-second stall on every SSE stream, whose response
writer implements no `Flush()`.

## Sidebar inventory updates

The sidebar subscribes to `/live/threads`: whole operational snapshots of all Sandboxes and
Threads, including archived Threads and threadless Sandboxes. Kubernetes watch invalidations and
the existing PostgreSQL trajectory notifications both refresh that snapshot. A listener reconnect
rereads PostgreSQL, including writes whose notifications were missed while disconnected. There
is no sidebar polling loop or new notification service.

The retained snapshot stays navigable during an outage, with separate warnings for the browser
connection, stale Kubernetes watch, and disconnected database listener. A Thread's running dot
requires fresh sources, a running Sandbox, and its last observed harness state; a stale persisted
RUNNING state alone does not make a suspended or deleted Sandbox look live. These are operational
snapshots, not replacements for a Thread's runner Event prefix.

## Shutdown

SIGTERM begins a drain as Uvicorn's shutdown starts: `/readyz` answers 503 (`/healthz` stays a
liveness check), every other new request is refused, and every open SSE stream -- the live views,
a session's events, the Actions stream -- ends where it is waiting rather than at its next frame.
Uvicorn waits at most `--shutdown-timeout` (5 s) for what is still open, then cancels it. Only after
that does the bridge release its ingestion leases and the store close its connections, which is what
the rest of the Deployments' 60-second grace period is for. No preStop delay consumes that budget.

## Shape

```text
browser (OIDC session)  /  agent (Kubernetes token)
   |  REST + Electric sync + inventory SSE, through the cluster gateway
integration app (Deployment, namespace agentplane-staging)
   |  Kubernetes API              |  runner protocol (gRPC, in-cluster)
Sandbox -> Pod, PVC               |
                  runner container <-+
                    runner process, state dir on the PVC
                    claude / codex child per session
                    model traffic to LiteLLM through the egress proxy, which holds the key
```

Kubernetes is the sandbox inventory, including a compact annotation holding its live preset
association plus explicit thread-default edits; the runner holds the live session;
PostgreSQL holds the raw event archive and materialized fold that outlives the sandbox. Preset definitions remain app
configuration, and each launch sends only resolved concrete fields to the runtime.

## External-client consent

The Action Service's OAuth adapter sends a validated authorization request to
`/#/connection-enrollments/{handle}`. The hash route survives the app's existing operator
login. The page shows the client-supplied name, client ID, and validated redirect as text,
asks for a new Connection name or existing Connection and a labeled caller ServiceAccount, and
offers Authorize/Deny. Existing selection shows its UUID, reviewed version and grant history, and
requires explicit authority-replacement confirmation. Changing the ServiceAccount clears that
confirmation. A fresh authorization can reconnect to the same ServiceAccount or choose another; no
policy editing is offered.

Both `/connection-enrollments/{handle}/preview` and `/decision` are operator-only POSTs
with the existing exact-Origin check and per-request federation. The BFF stores a random
browser binding, CSRF token, original version, and decision retry key in the persistent
operator session. The binding never leaves the server-side app/Actions channel. Distinct
interactions have distinct bindings; replicas share them, while logout/re-login does not.
At most 32 unexpired interactions are retained per session. The Actions authority binds
the first preview to that browser and authenticated operator, owns expiry, and consumes
one decision. The BFF preserves the exact attempted decision for retry after a lost
response, including page reload; changing it requires a fresh OAuth authorization.
The existing Connection's reviewed version is part of that exact decision; a 409 never silently
refreshes it or retries replacement. Consent does not revoke old authority: token exchange's
version-checked reservation does, before replacement activation. Failed issuance does not restore
old grants, and old credentials and Action provenance never change ServiceAccount.
The v2 session interaction namespace requires in-flight pre-upgrade consent pages to restart OAuth;
ordinary operator login sessions remain valid.

Authorize resumes only the server-held continuation returned by the authenticated Actions
authority; no client or browser-provided URL is accepted. Deny grants nothing and leaves a
terminal page. Completing consent alone does not activate a grant: the OAuth adapter still
verifies the same upstream operator and completes issuance. Operator credentials never
reach the external client. An external-client Action is auto-approved only by an
ActionPolicyBinding naming its ServiceAccount, otherwise by the operator; clients sharing a
ServiceAccount share receipts while submitted Actions preserve exact connection provenance.

## Action review

`/#/actions` renders canonical Action Service receipts. The app's `GET /actions`,
`GET /actions/{request_id}`, `GET /actions/{request_id}/events?after_sequence=0`,
and `POST /actions/{request_id}/decision` are an
operator-only BFF over `OperatorActionServiceClient`. They use the service's models
unchanged, including expected versions, idempotency keys, and the human-authored `decision_note`.
The same note is visible to the requesting caller and operator; the existing UI displays it.
Provider-authored bounded `reason_code`/`reason_description` are separate outcome evidence, not
another human note. OpenAPI and frontend types are generated from the canonical models.
External receipts display the immutable authenticated ServiceAccount, issuer/client and Connection
from `external_grant`; the grant ID and revision are expandable audit detail. This is
submission-time evidence, not the Connection's current authorization status. Receipts without
that snapshot retain their caller-principal display.
Event reads return canonical `ActionEventView` entries in sequence order, strictly after the
non-negative cursor (default 0).
The service owns persistence, authorization, Decisions, dispatch, and recovery. Workload
submission and owner-scoped reads use the service's `/v1/action-requests` API, not the app.

Staging configures federation: the Deployment reads `AGENTPLANE_ACTION_FEDERATION` from the
Git-owned `agentplane-action-federation` ConfigMap (built from the `action_federation`/
`operator_oidc` dicts in `cluster/cdk8s/generate_manifests.py`), and the app's network
policy admits Authentik by TLS SNI and the Action Service on 8080. The app composes a request-bound
JWT-bearer exchanger; the service independently validates the exchanged operator JWT. No static BFF
bearer or workload-token promotion is used. Missing configuration is specifically
`503 detail.code=operator_federation_not_configured`; token callers get 403.

See [`../docs/operator_federation.md`](../docs/operator_federation.md) for exact settings, PostgreSQL
session storage, opaque session lifecycle/CSRF/invalidation, failure codes, and signed
multi-replica/two-operator test targets. Existing browser sessions must log in again after rollout.
Only operator Action arguments are unredacted; caller arguments and execution result/error
redaction are unchanged. There is no app-owned Action/Decision/Execution authority.

## Settings

The sidebar footer's gear button opens a modal with OAuth clients/MCP servers/Notifications tabs; it
has no dedicated route of its own. The one exception is `/#/mcp-servers`, the MCP-linkage OAuth
callback's redirect target (see below): landing there opens the modal pre-selected to that tab
instead of showing the Threads landing view as if the linkage completed silently.

The OAuth clients tab lists the Action Service's runtime named Connections, each row showing its
most recent grant's OAuth client ID and the ServiceAccount it acts as; superseded grants stay in
the Action Service's own history but are not listed here. The operator can explicitly confirm
unlink, which revokes active/pending authority without deleting history or stopping already claimed
executions. Whether that grant's ServiceAccount is still a labeled caller is displayed separately
from the grant's lifecycle status: an active grant does not imply its ServiceAccount remains
eligible. The tab does not offer renaming a Connection.

The BFF proxies `GET /connections[/{id}]`, `PATCH /connections/{id}`,
`POST /connections/{id}/unbind`, and `GET /connection-service-accounts` through the same request-bound
operator federation as Action review. Unsafe browser requests require exact Origin. Canonical
`ConnectionRename` and `ConnectionVersion` models carry the version the operator reviewed; a 409
refreshes the inventory and asks for review, never automatically retrying a destructive operation.
The app owns no Connection state. Reconnect/rebind begins with fresh authorization from the external
client and selecting this Connection on the consent page; management has no direct retarget action.
Policy editing and deployment are outside this surface.

The MCP servers tab lists the Action Service's OAuth-linked MCP server groups and links or
disconnects each one. The BFF proxies `GET /mcp-servers`, `GET /mcp-servers/{id}/linkage`,
`POST /mcp-servers/{id}/linkage/start`, and `POST /mcp-servers/{id}/linkage/disconnect` through the
same operator federation; the provider's redirect lands on `GET /mcp-linkage/callback`, which
completes the link and returns the browser to `/#/mcp-servers`. What a link authorizes and when a
linked group becomes available is the Action Service's contract
([its README](../action_service/README.md#action-catalog)).

## Launch presets

`GET /presets` publishes configured Sandbox presets and their inherited editable Thread defaults.
`POST /sandboxes` keeps its no-preset shape and additionally accepts an optional preset: omitted
fields inherit, while explicit policies and thread fields replace preset values. The Sandbox
annotation stores the preset name and only explicit thread edits, so later sessions resolve against
the current configured default instead of freezing a copied form. `action_policy_sets` works as
`policies` does: a preset pre-fills the pick, an explicit list replaces it (an empty one binds
nothing), and a launch without a preset may pick sets of its own. The launch writes one
`ActionPolicyBinding` naming the picked sets for the new Sandbox; a set name the namespace does not
hold is refused with 422 before the Sandbox exists, as an unknown egress policy is. The create form
offers the namespace's sets from `GET /action-policy/sets`, each with the Action Service's verdict
on it, and records the picked preset in the URL (`/#/?preset=<name>`) so a launch form can be
linked to.

Before opening a session on a bound Sandbox, the app sends the SandboxPreset's configured bootstrap
content to the runner under a stable preset identity. The runner executes it idempotently on the
persistent state volume; a failure refuses the session open. The existing full `SessionSpec` API is
available when no preset is selected. Every launch prepends the image's `agent_instructions.j2` to
the task or preset instructions, including direct `SessionSpec` API launches. The app renders its
service URLs from `agent_egress_api_url` and `agent_actions_service_url` in deployment
configuration. A configured `agent_instructions` key replaces that image default, including an
explicitly empty value. The
shared block teaches agents the platform's egress and Actions Service protocol; a preset and the
per-turn task remain the place for workload-specific constraints and the requested outcome.

## Action policy

The Sandbox page's "Action policy" tab, carried in the live snapshot frame, shows the Action
Service's own answer for the ServiceAccount the Sandbox runs as, read through the operator
federation (`/v1/operator/action-policy/service-accounts/{namespace}/{name}`, so an operator session
is required as for the Actions page): the unexpired `ActionPolicyBinding`s naming that account, each
with expiry
and the service's `Ready` verdict; every `ActionPolicySet` those name, as present, edited since the
service judged it, refused with the validation report, or missing; the resulting `autoApproveIf`,
`autoDenyIf` and `autoDenyUnless` lists in the order the service walks them, each entry naming the
binding, set and index a Decision's evidence names; and `synced`, false while the service's watch
has not synced and nothing auto-decides. It is the resolution an admission would use now, from the
service that would use it, and says nothing about past Decisions; the Actions page holds those. The
app adds only each binding's provenance (git, this app at launch, or the operator with kubectl),
read from labels the service reports. The tab is read-only; the one place the app writes a binding is a launch, whose pick the create form
draws from `GET /action-policy/sets`, the namespace's sets each with the same verdict. The app's own watch of the two kinds is
a trigger: an event on either re-asks the service for the next frame, and a binding lapsing while
nothing changes leaves the page at the next frame. The two watches are independent, so a frame can
briefly precede the service's informer seeing the same event and show the answer from just before
it. Where the service cannot be asked -- no federation configured, a session to log in again, a
failed exchange or request -- the frame says so in place of the policy rather than showing an empty
one. Which lists the service enforces is its contract
([SPEC § Action policies](../action_service/SPEC.md#action-policies)).

## Decisions

- **Connection direction:** the app dials the runner Pod's address directly, re-resolving on
  reconnect; Pod replacement changes the address and the session log makes the cursor valid across
  it. A Service per sandbox is not needed until something outside the cluster must reach a runner.
- **Model credentials:** staging uses a dedicated OpenAI/Claude subscription key; testing uses
  the `cheap-experiments` LiteLLM key. The Pod holds no
  key or workload token: a harness sends the inert placeholder the `agentplane-workload`
  EgressCredential derives from its name, central substitutes the sidecar-only Pod-bound token,
  and the authenticated LLM ingress replaces it with its one server-held key after resolving the
  `WorkloadPrincipal` the token proves. The model endpoint remains governed by the credentialless egress design
  in [the ADR](../docs/adr_sandbox_proxy_gateway.md) rather than excepted from it.
- **Transport security on the runner port:** Cilium policy between the app namespace and the
  sandbox Pods is the v0 control. Authentication on the port itself waits for the credentialed
  readiness gate.
- **No gRPC-Web or Connect proxy in front of the runners:** browsers cannot carry the
  bidirectional `Attach`, and a standard proxy would still need per-sandbox routing to Pod
  addresses that change on every resume. The app stays the one HTTP surface; the schema is shared
  through proto-JSON and generated types instead. Splitting `Attach` into a server-streaming
  `Open` plus unary commands waits for a second, non-browser client that wants it. Attachments are
  independent; commands share the runner session's serialization and deduplication.
- **Deletion takes only a suspended sandbox:** it removes the Pod and the volume with everything on
  it, and nothing brings that back. The rule lives in the API rather than in the browser, so it also
  binds the agent driving staging with a token; the two clicks it costs an operator are suspend and
  then delete. A running sandbox answers `DELETE` with 409 and a message saying to suspend it.
- **Raw is additive:** both modes use the same chronological thread blocks and disclosure
  state. Compact Event summaries sit between those blocks; expanded details retain the full generated
  envelope, native payload, and causal references. JSON wraps instead of forcing horizontal scrolling.
  Cards are labelled as current aggregates, not historical snapshots at their first cursor. The
  [projection contract](../docs/thread_layering.md#timeline-pending-queue-and-operational-state) separates
  this evidence from the pending-command queue and operational snapshot.
- **Failed model turns:** normal and Raw views show the terminal failure and its plain-text
  diagnostic at the completion position, retaining any partial output. A confirmed input remains
  confirmed; a failed turn neither creates a delivery retry nor resends that input. Later input
  can start another turn while the harness remains available.

## Action live updates and browser notifications

`/#/actions` follows the Action Service's operator SSE stream through `/actions/stream`, replacing
its old two-second timer poll. Streams provide fresh snapshots after reconnect, and an unavailable
stream is shown as disconnected rather than silently presenting stale state as live. The BFF
bounds stream lifetime to 30 seconds so each reconnect checks current browser-session state,
including logout in another replica. These reconnects are authentication checks, not state polling.

The Settings modal's Notifications tab (`/#/notifications`, see [Settings](#settings)) registers the
current browser, lists registered browsers, identifies this one, and forgets registrations. Forgetting the current browser also unsubscribes locally. The stable
`/sw.js` service worker receives background Web Push, offers Approve/Deny, and opens the Actions
page on a body tap or failed decision. Buttons use the existing operator session and an expected
Action version, never authority supplied by the push message. A resolved notification has no
approval buttons. Supported browser notification actions vary; the Actions page remains available
when a platform cannot show buttons. Background subscriptions/sending belong to the Action Service;
the app has no process-local subscription authority.

Deployment must configure the Action Service's VAPID identity and exact push-service hostnames
before enrollment can work. Browser permission is requested only from an explicit registration
click. Real browser/OS push-service delivery still needs operator acceptance; transport and
service-worker tests do not establish that production experience.

### Lazy thread evidence

`GET /threads/{thread_id}/evidence` lists observations associated with
one `entity_kind`/`entity_id`, scoped by the exact `source_id` and `projection_epoch`.
It returns observation cursors and whether native links exist, without reading frame bodies.
`after_cursor` is exclusive; `next_after_cursor` is null at the end of the page sequence.

`GET /threads/{thread_id}/evidence/{observation_cursor}/frames` expands
one association under the same scope and entity. It pages native links by exclusive
`after_sequence`, returning whole raw entries. Missing captured frames are explicitly
`unavailable`; an association with no native links returns an empty page. All returned
cursors/sequences are decimal strings. Both reads default to 30 records and allow up to
200, reject stale source/epoch references with 410, and require the normal app identity.

`GET /threads/{thread_id}/observations` reads the original archive,
including semantic observations, unlinked native frames, stderr, and debug checkpoints.
With no cursor it selects the latest 30 records. Supply either exclusive `before_cursor`
or `after_cursor` to page older or newer; responses remain in chronological order and
provide both continuation cursors. The limit is a record count (1–200), with no body
truncation. Reads seek through the archive primary key and retain only the selected page.
Raw source/sequence identities do not depend on the current projection epoch. This read
requires the same app identity and is intended for explicit debug inspection.

Command evidence stays attached to the stable admission position when a later observation
settles it, including shared input confirmations and lifecycle effects.

### Ingestion batching and client memory

The runner feed commits batches of at most 128 events, flushing a partial batch after
25 ms or stream end. It keeps one pending transport read across flush deadlines and
at most one event of read-ahead while PostgreSQL is writing. A slow database applies
backpressure; a failed batch is retried from the committed cursor on reconnect.

Runner clients do not retain received history by default. Tests and explicit debug
consumers can opt into `capture_history`; the resume cursor is independent of that
capture. Explicit session Open cancels its observer after `Attached`, avoiding a
full-history drain. The leased ingestion connection remains responsible for copying
runner events into PostgreSQL.
