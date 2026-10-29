# Agentplane integration app

The human-facing frontend over Agentplane's services: a FastAPI service that manages Sandboxes
through the Kubernetes API as the requesting operator, opens Sessions and submits commands through
the Session Dispatcher, and uses the Runner Gateway for UID-fenced live checks and read-only runner
status diagnostics. It reads archived Session history and Threads (each Session's conversation projection) from
the History Service and synchronizes selected rows and content to the browser through authenticated
Electric endpoints. It also presents Actions, operator decisions and Connection consent.
The staging instance lives in `cluster/k8s/agentplane-staging/`.

The API image contains no browser assets. The separate [static frontend](frontend/serving/README.md)
serves the SPA and service worker on the same public origin through Gateway path routing; API,
authentication and streaming requests reach this service directly.

Set `AGENTPLANE_ELECTRIC_URL` to enable the authenticated thread-sync API and Electric health probe.
Omitting it leaves REST and SSE available without Electric; backend lifecycle and model-smoke
fixtures use this composition. Electric configuration does not control static-file serving.

## Sandbox lifecycle ownership

The app uses the shared `agentplane/sandboxes` inventory to submit lifecycle requests directly to
Kubernetes as the requesting operator. The Sandbox Controller reconciles the requested `spec` and
owns `status`. Required `default_policy_sets` come from the app ConfigMap, including when the set is
empty. The former Sandbox Service Deployment, API and server source are retired.

## Dependency boundary

**The integration app is a user-facing client; other Agentplane services must not depend on it.**
This applies to APIs, private tables, implementation imports, browser/app-issued identity, background
attachments, and app-only provisioning or prompt/bootstrap state, including in v1. See the accepted
[service dependency rule](../docs/service_boundaries.md).

Backend responsibilities needed by another service move to an independent owner, not a temporary
app endpoint. The shared `agentplane/sandboxes` library owns Sandbox request construction; the app uses
it directly as the requesting operator. The [Session Dispatcher](../runner_gateway/dispatcher/README.md)
owns opening and durable command delivery; the [Runner Gateway](../runner_gateway/README.md) owns the
runner control stream and authorized live Runner proxy; the
[History Service](../history_service/README.md) owns the Session Event archive and its Thread fold.
The app is a client of each service for its distinct operation.
Notifications depends on that backend and Actions, not this app. The app's own state is browser
sessions and its `thread` table: each Thread, keyed by its Session's ID, with the name and archive
state an operator sets; like any state the app holds, these are candidates to move into a dedicated
service ([principles](../docs/principles.md#composable-services-not-a-platform-you-sign-up-for-whole)).

The current bridge's session-scoped runner attachment is implementation state, not
the desired product model. [Thread, runner, and harness layering](../docs/thread_layering.md) is
the authoritative design for Dispatcher submission receipts, runner admission, native effects,
multi-session Thread history, and folded/Raw projections.

```sh
bbr test //agentplane/app/...
```

### While the History Service is down or rolling

Thread lists, Thread views, the retained-event SSE API and Thread commands fail with the History
Service's error: each typed command route first looks for its admission in the archive and checks
the Thread's feed state (`api.py`, `threads/bridge.py`), so even a command the runner would accept is refused. Resume
reads the feed state the same way. Sandbox lifecycle and Action review keep working.
`HistoryWatch` reconnects on its own, and Sessions recorded meanwhile get their Threads once it does.

## Layout

- `main.py`: the entry point; `Settings` names every knob as a flag, an `AGENTPLANE_*`
  variable, and a key of the YAML file `AGENTPLANE_CONFIG_FILE` points at.
  `runner_gateway_target` is only for the health check; app live reads use the per-Sandbox
  `runner_gateway_control_target` and projected `runner_gateway_token_file`.
- `presets.py`: app-owned `SandboxPreset` and `ThreadPreset` configuration and concrete launch
  resolution. Preset names stop at the app boundary; Kubernetes and the runner receive resolved
  fields.
- Sandbox provisioning uses the shared `agentplane/sandboxes` inventory directly as the requesting
  operator. Session openings and commands use `SessionDispatcherClient`; the Sandbox control client
  reads current-incarnation checks and runner status through the per-Sandbox Runner Proxy, carrying
  the live Sandbox name, UID and owner. Runner status is informational and never wakes a Sandbox.
  The app reads Kubernetes projections for UI updates and has no Gateway Session API client.
- `egress_access.py`: PolicyBinding projections for the UI, and the operator's grants and
  revocations of `PolicyBinding`s, written through the Kubernetes API by the app itself. Bindings remain
  desired-state grants, with expiry/revocation per binding; Flux-owned and Sandbox-controlled bindings
  are refused ([composition](../docs/egress_composition.md)).
  The egress decision stream is in `live.py`; an unreachable proxy leaves the binding views readable.
- `action_policy.py`: Action Service's auto-approval list for a ServiceAccount, read over gRPC with the operator federation.
  App-level `default_policy_sets` are combined with the selected preset's policy sets by the shared
  Sandbox inventory; Policy Controller materializes the resulting grants.
- `threads/`: Threads over the History Service. The app's database holds each Thread's identity,
  the name and archive state an operator sets (`Thread`, `models.py`). Everything folded from a
  Session's events (summaries, entities, payload chunks, evidence) is the History Service's, read
  over its gRPC API (`summaries.py`, `view/`) or replicated by Electric from its database.
  `history_watch.py` follows the History Service's watch: `HistoryWatch` turns its fold commits
  into `Channel.THREADS` wake-ups, and `SessionOpenings` opens a Thread for each Session it records,
  whoever started it. `store.py` (`ThreadStore`) composes the app database and the History Service;
  runners push their events to the History Service.
- `threads/bridge.py`: browser-facing composition of session operations with archive synchronization.
  `threads/runner_directory.py` resolves a Thread's Sandbox through the Runner Gateway and creates
  UID-fenced clients for operations that need live runner state. The Sandbox page's retained Session
  inventory comes from History Thread summaries, not a runner inventory RPC.
- `api.py` is the REST surface and the OpenAPI schema `export_schema.py` emits for the frontend's
  generated client.
- `threads/events/`: a Thread's archived events, read through the History Service. `event_log.py`
  (`EventLogStore`: the Thread mapping, the archive's events and the feed state), `stream.py` (a
  thread's archived events as SSE, so any replica serves it without a runner) and `debug.py` (the
  typed, paginated observation and evidence reads).
- `client.py`: a Python client over the app's HTTP surface, speaking the app's own request and
  response models and the runner protocol's `Event` messages.
- `live.py`: one list-and-watch over Sandboxes, their Pods and the egress objects
  (`../kubernetes_watch.py`), the action policy kinds watched only as a trigger to re-ask the Action
  Service, and the SSE streams that push a snapshot of it to every open tab.
- `changes.py`: the payload-free wake-up a reader of the cluster index or the thread store waits
  on; a burst of changes coalesces into one re-read.
- `database_updates.py`: the replica's one PostgreSQL `LISTEN` connection, turning each channel's
  committed notifications (thread writes, deleted operator sessions) into that channel's
  `changes.py` wake-up, and waking every channel after a reconnect.
- `identity.py`: whether a request proved itself, by whichever credential it carried; `oidc.py` and
  `auth_routes.py` are the browser's half of that (see below).
- `threads/view/content.py`: `ContentStore`, reads of what the History Service's fold assembled
  (`ThreadEntityView`, `../history_service/fold/views.py`).
- `electric.py`: authenticated, scope-checked metadata, selected-command, and payload shape proxy.
  The private Electric service reads PostgreSQL logical replication; app replicas do not retain
  per-listener thread copies. The scope read is a long poll held until the thread's first fold.
- `system_status.py`: the System page's checks. `GET /system/status` probes, concurrently and each
  within a 5-second budget, the gRPC health service of the Runner Gateway, History Service, Action Service,
  Connection Service, Notification Service and egress proxy admin port,
  plus Electric's `/v1/health`; a probe that fails or times out is `down` with its
  reason, never an error response. The Kubernetes API is not probed either; the freshness of the live
  views shows it.
- `action_federation.py`: request-bound operator federation into the canonical Action Service.
- `grpc_stream.py`: shared gRPC deadlines and bearer handling for the generated operator-facing service
  stubs, and the bounded opening/cleanup lifecycle for browser-relayed watches.
- `frontend/connections/connection_service_client.ts`: generated ConnectionService ProtoJSON/NDJSON
  message adapters for the consent page and OAuth-client settings; the app's BFF owns the browser HTTP
  routes while the service owns Connections and consent.
- `operator_sessions.py`: PostgreSQL browser identity and pending OAuth state, shared across replicas.
- `database.py`: the declarative `Base` every table maps onto, and the app's one connection pool;
  `main.py` builds the pool and hands it to each store and to the database update listener.
- `database_migrate.py` and `migrations/`: the Alembic history covering the tables of
  `operator_sessions.py` and `threads/models.py`. Migrations run separately through
  `:migrate`, which fails when the migrated schema differs from the models; the server itself
  never creates or checks tables at startup. `:image` and `:migration_image` are separate OCI targets.
- `frontend/`: the React SPA on the repo's `ts_library` and esbuild toolchain, with the visual
  scenarios under `frontend/harness/`.

## Live views

### Replica-safe command delivery

Any app replica accepts typed `submit-input`, `change-model`, `change-reasoning-effort`, and
`interrupt-turn` requests under `/threads/{id}/`, each with a stable
command id. The required `session_dispatcher_target` calls the matching generated Session Dispatcher
RPC. Durable operations return their stored `Dispatch`; `InterruptTurn` returns its immediate
result without a queue row. A Dispatcher
failure is returned to the caller; the app has no direct Runner Gateway command path. An exact retry
already present in the History archive returns its `CommandAdmitted` entry without another service
call. Otherwise the browser keeps Retry and never Cancel after an unconfirmed result, because the
Dispatcher may have stored the command despite a lost reply.

The browser follows the Dispatcher's later state of the commands it still holds as queued or delivering
with `GET /upstream/dispatcher/v1/dispatches:watch?filter.sessionId=…&filter.commandIds=…`. The app's
authenticated BFF opens the Dispatcher's generated gRPC `WatchDispatches` stream, then sends each frame
to the browser as ProtoJSON newline-delimited JSON: those commands' Dispatches, a `CaughtUp` frame, then
a Dispatch whenever one changes. A command with no Dispatch by `CaughtUp` is shown as unknown, since the
Dispatcher deletes a Sandbox's rows with it. A connection that ends is replaced by a new one, which sends
the current Dispatches again. Queued and delivering are distinct from
runner admission, and neither admission nor `HARNESS_CONFIRMED` is presented as an effect. History
remains the authority for command outcomes. The browser retains the command locally until History
projects its admission or command row; the app adds no command queue. Deploy the Session Dispatcher
and authorize the app ServiceAccount before rolling out an app image that requires this setting.

The `/upstream/dispatcher/…` BFF routes expose Dispatcher reads to an operator session and call typed
gRPC methods with the app's rotating projected Dispatcher token. Unary routes return ProtoJSON; the
watch keeps a bounded initial read before starting its streaming response and then relays frames until
the operator session ends, the app drains or the browser goes. Reads carry no Thread check, so an
operator can read the Dispatches of any Session id. Commands stay on the typed Thread routes, whose
checks the Dispatcher does not make.

The Sandbox page lists retained Sessions from History Thread summaries, so suspension or removal of a
runner Pod does not clear the inventory. A row's status mark is derived from those last-observed History
values and the Sandbox's own state, and reads as unconfirmed whenever the stream, the watch or the
Thread-update listener (`updates_connected` in the frame) is not current. The Status tab shows the
Sandbox's state and lets an operator change its wake-on-message and idle-suspend settings, as the list
does; the Kubernetes conditions and containers below start open only where one is unmet. It reads
`GET /sandboxes/{name}/runner-status` for current runner process and Session diagnostics; it does not
wake a suspended Sandbox. A missing or unavailable runner is
shown as unavailable, while suspended Sandboxes show that diagnostics are unavailable until resumed.
Idle Sessions can be archived while their harness process remains live. The UI disables archive for
observable active turns, setup, or a pending local launch; the server checks whether Session work is
unfinished before accepting the archive request.
Every new Session opening is submitted to the Dispatcher with the complete Session spec. A `SessionOpening` reports queued,
delivering, runner-admitted, or failed state, and
`GET /sandboxes/{name}/sessions/{id}/opening` checks that durable result. Admission does not mean the
History Service has projected a Thread yet.

The optional `model_catalog.central_sampler` names a configured service and provider route;
its `HARNESS_CENTRAL_SAMPLER` model list adds **Central sampler** to the launch form alongside the
native harness offerings. Setup scripts run once on the bound Sandbox executor before sampling begins. For an existing
central Session, a suspended or unready execution Sandbox leaves conversation history and central
controls available while the bound Sandbox object remains in the current inventory.

Thread names are set, changed, or cleared explicitly by operators in the thread view.

The app ConfigMap groups browser-safe links under one optional `frontend_config` key.
`GET /frontend-config` returns that same typed object directly, with only the explicitly whitelisted
settings below; when the deployment does not supply the object, both fields are null.

`frontend_config.trace_search` is null unless the app configures a Grafana base URL, Tempo datasource
UID, and deployment environment. When configured, Sandbox, Session, and active Thread ellipsis menus
link to Grafana Explore with a Tempo TraceQL search scoped to
`resource.deployment.environment.name`. Sandbox links match `span.sandbox.name`; Session and Thread
links match `span.session.id`. Current spans do not carry a Sandbox UID, so a Sandbox name reused
after deletion can match traces from both Sandbox instances.

`frontend_config.langfuse_search` is null unless the app configures a Langfuse base URL, project ID,
and metadata keys for the Sandbox namespace and ServiceAccount. When configured, Sandbox and active
Thread ellipsis menus link to the project's last-24-hour traces. Native Thread and Sandbox links
filter by those two metadata values; they can include other Sessions and older same-name Sandbox
instances. Central Thread links instead filter by the public Session ID, using `session_metadata_key`
(default `attributes.litellm.metadata.spend_logs_metadata.agentplane.session_id`). That attribution
requires an explicitly trusted sampler, LLM ingress and LiteLLM metadata promotion. The configured
keys must match the producer's exported attributes; links alone do not create trace attribution.

The app copies no runner events and folds none. Each runner pushes its Sessions' events to the
History Service's ingress, and the History Service folds them under per-Session leases
([History Service](../history_service/README.md)); the app reads both through the History Service.

A Session gets its Thread from the History Service's watch, which names each Session it records
on the runner's first push; every stream resumes after the last one the replica opened, and a new
process starts from the first. Opening a Thread does not restart its harness. A composer may submit
input to an eligible dormant Session; the Dispatcher wakes and admits the Session as needed. A
suspended Sandbox still follows its `wake_on_send` policy. Exact-turn interruption remains available
while that turn is active.

After interruption/resume, runner `ConversationReconciled` events give each affected item a
`recovery` disposition and `recovery_reason`. The fold retains absent/unknown observations;
revised continuation content gets a new payload revision without changing the observed tool
execution result. Original observations remain in the archive. The app does not infer recovery
behavior from the harness type.

The thread UI badges revised and unknown continuation state; retained content, the ordinary
outcome, and a succeeded tool call carry no badge. Absent content is collapsed under "not retained
in model context" and remains expandable. Revised tool content is labeled "Continuation output";
the execution outcome stays separate (a failure is badged, a success is not). Tool runs summarize
their badges even while collapsed, and interrupted items do not become streaming again when
a later turn starts.

The retained-event SSE API reads archived events through the History Service on whichever replica
receives its request. The History Service's fold-commit notices (`HistoryWatch`) and the app's
transactional `NOTIFY` wake Thread and inventory readers; both are hints, and the History Service's
cursor remains authoritative.

`/#/threads/{id}` loads metadata and the thread's tail independently of runner discovery. The
browser follows one Electric shape over the thread's rows and one per payload field it shows, each
pinned by `/threads/{id}/sync/*` to the thread and its projection epoch, and loads its window as
subset snapshots of them: the latest 30 positions, the page before the oldest row it holds whenever
the top of what it holds is in view, the view state, pending commands, and the commands this browser sent,
by ID, so an outcome stays visible however far the thread has moved on. Every later change to a
held row arrives on the shape's live log, which the browser follows over SSE. Text and tool
arguments render as far as their references' chunk counts, while reasoning, tool output and
associated debug frames are read on demand. Local authored intent, unsent drafts and
viewport/disclosure state remain separate from the synchronized rows. The Thread history
coalesces consecutive setup observations into one disclosure: its summary shows the latest
bounded stdout/stderr progress lines (carriage-return updates replace the current line), then
the exit/interruption outcome. Expanding retains the original ordered, unmodified setup events;
reconnect rebuilds the same preview from archived events rather than a separate progress feed. See
[the sync design](../docs/thread_view_sync.md) for query, revision and memory contracts and the
remaining acceptance gates.

Sidebar entries remain navigable after Sandbox deletion. Availability comes from the separate
live inventory snapshot; suspended/deleted Sandboxes disable runner controls. Unfinished
retained items are labelled incomplete rather than actively streaming. The Runner Gateway
owns runner attachment; loading a thread never rebuilds its history.

`test_thread_browser.py` loads the built SPA in hermetic Chromium against real application
processes, PostgreSQL and Electric through a real HTTP/2 ingress. Only the runner source,
Kubernetes and authentication boundaries are controlled. Its cases are split across Bazel
shards. Traces and screenshots are test artifacts; inspect the images as well as results.

Coverage includes older-item streaming, selective bodies/debug, archived threads,
reload/offline/reconnect, admission responses ahead of projection, lost HTTP replies, exact
same-ID retries, and failed/noop outcomes after history eviction and explicit dismissal.
Fault injection holds actual Electric responses while ingestion continues. A suffix the fold
cannot apply preserves verified history, reports the verified cursor, and disables controls.
Scrolling checks cover live growth, reader anchors, desktop/phone layouts, and returning to the
bottom before a queued scroll event. The remaining server-memory, off-window traffic and Electric
availability gates are listed in [the acceptance record](../debug/conversation_acceptance.md).

Staging runs two app replicas on separate nodes with `RollingUpdate` (`maxUnavailable: 0`,
`maxSurge: 1`) and a PodDisruptionBudget of one available; testing stays at one replica.

### Inventory snapshots

`/live/sandboxes` and `/live/sandboxes/{name}` are SSE, authenticated like every other route. Each
carries a whole `snapshot` of what it covers whenever that changes, and a `health` frame through
the quiet in between. What is pushed: the sandbox rows, one sandbox's bindings with each reader's verdict,
its Action policy list, and its threads, the last of these from the store rather than a watch, since the app is the
only writer of a thread's name. The Sandbox page uses those History Thread summaries as its retained
Session inventory; its harness and turn fields are last observed History values. Suspending or
removing a runner Pod does not clear projected Threads, and the app does not claim their harnesses
are still running. Archived Threads stay hidden unless the operator chooses **Show archived**.

The Egress log tab's decisions are their own stream, `/live/sandboxes/{name}/egress/decisions`: the
app follows the proxy's authenticated `EgressAdmin.WatchDecisions` gRPC stream for the sandbox's
ServiceAccount. Each message replaces the retained decisions; the app renews the stream when its
authorization lease ends and reports outages in the browser stream. Its Egress system-status probe
also uses gRPC health on the same admin endpoint. Runner availability affects operations that need a live Session and
whether the page can open a new one; it does not determine whether a retained History Thread appears
in the table. Session creation displays progress and prevents duplicate clicks while the request
runs.

A snapshot is not a delta, and there is no resumable id: a relist replaces a kind wholesale and a
`resourceVersion` expires, so a reconnecting tab is served a fresh snapshot instead. That leaves
one failure to handle honestly -- a watch that has wedged looks exactly like a cluster where
nothing is happening -- so every frame carries how long ago each kind last completed a cycle,
against the same three-cycle bound the egress proxy's readiness uses, and a page whose data has
stopped moving says so rather than showing it as live: the browser's connection and that verdict
both feed the one connection indicator in the topbar, a spinner whose tooltip names each stream
that is not current and why. Every stream, and the server's watch behind it, retries by itself.

## Authentication and authorization

Every API route needs a caller; only `/healthz`, `/readyz` and the `/auth/*` endpoints answer without
one. The separate frontend serves public static files, including `/sw.js`. There are two credentials,
and both are cryptographic:

- **An operator's OIDC session.** `AGENTPLANE_OIDC_ISSUER` and its siblings register the app as an
  Authentik client; `/auth/login` runs an authorization-code flow with PKCE and stores the
  `iss`, stable `sub`, and display `preferred_username` in PostgreSQL behind an opaque signed `__Host-` cookie. Unsafe methods additionally have to be
  same-origin, which is the CSRF defence `SameSite=lax` leaves open. The deployed app requires these
  settings; an app the tests build without them has no login at all.
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
the gateway routes straight to the Service. A header set by an authenticating proxy, such as an
Authentik forward-auth outpost's `x-authentik-username`, cannot be trusted: the API server's service
proxy also reaches the Service and forwards caller-supplied headers, so anyone with `services/proxy`
on it could grant themselves egress. The outpost would also stall every SSE stream for 15 seconds,
since its response writer implements no `Flush()`.

## Sidebar inventory updates

The sidebar subscribes to `/live/threads`: whole operational snapshots of all Sandboxes and a
window of the Thread list, newest first. The window holds a page of Threads and grows a page at a
time on request; a search (name, Sandbox, or the start of the ID) and the archived switch narrow it
on the server, and the Thread a page has open rides along wherever it falls, so every Thread on
screen stays current without the browser holding the whole list. `GET /threads` pages the same list
under opaque tokens bound to their filters. Kubernetes watch invalidations and
the existing PostgreSQL thread notifications both refresh that snapshot. A listener reconnect
rereads PostgreSQL, including writes whose notifications were missed while disconnected. There
is no sidebar polling loop or new notification service.

The landing route (`/`) is the same Thread list as a whole page, with each Thread's harness, model
and last activity, so a phone opens on its Sandboxes and Threads rather than behind the drawer. The
sidebar and the landing page share the one `/live/threads` stream and its search.

The retained snapshot stays navigable during an outage, and the browser connection, stale
Kubernetes watch, and disconnected database listener all show in the topbar's one connection
indicator rather than as page banners. A Thread's live dot
requires fresh sources, a running Sandbox, and its last observed harness state; a stale persisted
RUNNING state alone does not make a suspended or deleted Sandbox look live. The snapshot includes
the feed's active turn, and how the Thread's last completed turn ended (`last_turn_status`, read
from the newest stored `turn_completed` entry through a partial index), so the sidebar and
open-thread composer can use the same status-dot component. These are operational snapshots, not
replacements for a Thread's runner Event prefix.

Every status indicator is drawn from one table, `frontend/status_mark.ts`, which gives each thread
and Sandbox status its icon component (a Tabler icon, or one of `custom_mark_icons.tsx`) and color,
and each thread status also its favicon shape and tab-title glyph. The in-page glyph (`mark_glyph.tsx`)
and the browser-tab favicon (`thread_favicon.ts`, which hand-draws its own SVG) only render what it
says. A thread shows two green chevrons moving left to right in the page while a turn runs, a steady
light blue dot when its harness is live and waiting, a red warning triangle instead of that dot while
the last turn to complete failed, was lost or ended without a status (an interrupt does not count; the
next turn to complete without an error brings the dot back), a gray power icon when its harness is
down, a red dot when its runner feed failed, and a gray dot when nothing else is live. A Sandbox shows a light blue
play icon when its Pod is ready, a yellow clock while it comes up, a gray pause when suspended, a red
cross when failed, and a gray cross once deleted or ended.

The favicon draws the same marks but never moves, and the tab title leads with a plain text glyph for
the thread's status (`»` running, `●` idle, `!` last turn errored, `⏻` harness down, `○` not live, `×` failed). A favicon is only seen in a
background tab, where browsers throttle timers, so both change on status events rather than on a clock.

The Thread composer shows an approximate age of this Thread's latest model-originated activity
beside its send controls on wider screens, or at the scrollable Thread history tail on narrow screens.
The fold records it on the Thread's summary in the same transaction as the events it folds, using
assistant/reasoning output and generated tool calls/arguments; it ignores tool
output, user input, harness bookkeeping and turn completion. A migration backfills existing
logs. The timestamp is a **proxy** for LLM-request recency: model output can arrive well after
an upstream request, and a request with no model output may not be represented. The UI does
not report a provider request time or a cache hit. The value remains meaningful during a
long-running tool or an active turn, and is unknown when no qualifying event was observed.
There is no ingress-side measurement: ingress can authenticate a Sandbox, but not distinguish its
co-resident Threads.

The composer also shows the Sandbox Controller's next autosuspend deadline, counting down on the
browser's clock. Runner busy reasons take precedence over a retained deadline. When a sibling
Session keeps the Sandbox busy, the hint links directly to that Thread. Opening the details lists
every reason and links to the responsible Thread for Session work, including a Session outside
the current Thread listing. The reasons describe active turns, pending commands,
setup, activation, bootstrap and command admission; terminals and background processes do not count.
During startup, including message-triggered wake, the hint identifies the Sandbox as starting;
the countdown waits for readiness. Stale inventory shows timing as unavailable. These facts use the
existing heartbeat, Lease and Sandbox watches; browser countdown ticks send no requests.

## Shutdown

SIGTERM begins a drain as Uvicorn's shutdown starts: `/readyz` answers 503 (`/healthz` stays a
liveness check), every other new request is refused, and every open SSE stream -- the live views,
a session's events, the Actions stream -- ends where it is waiting rather than at its next frame.
Uvicorn waits at most `--shutdown-timeout` (5 s) for what is still open, then cancels it. Only after
that do Session openings stop and the store close its connections, which is what
the rest of the Deployments' 60-second grace period is for. No preStop delay consumes that budget.

An unexpected Session openings worker failure starts the same drain, then exits the process with
the worker's error so Kubernetes restarts the replica. Connection, timeout, and database failures
handled by the worker's retry loop keep retrying. The restarted worker replays Session openings;
opening an existing Thread is idempotent.

## Shape

```text
browser (OIDC session)  /  agent (Kubernetes token)
   |  REST + Electric sync + inventory SSE, through the cluster gateway
integration app (Deployment, namespace agentplane-staging)
   |  Kubernetes API              |  gRPC Dispatcher / live Gateway checks
Sandbox -> Pod, PVC               v
                    Session Dispatcher
                         | runner proxy
                         Runner Gateway
                            |  mTLS Runner gRPC
                    runner process, state dir on the PVC
                    claude / codex child per session
                    model traffic to LiteLLM through the egress proxy, which holds the key
```

Kubernetes is the sandbox inventory, including a compact annotation holding its live preset
association plus explicit thread-default edits; the runner holds the live session;
PostgreSQL holds the raw event archive and materialized fold that outlives the sandbox. Preset definitions remain app
configuration, and each launch sends only resolved concrete fields to the runtime.

## External-client consent

The [MCP edge](../mcp_edge/README.md#authority)'s OAuth server sends a validated authorization request to
`/#/connection-enrollments/{handle}`. The hash route survives the app's existing operator
login. The page shows the client-supplied name, client ID, and validated redirect as text,
asks for a new Connection name or existing Connection and a labeled caller ServiceAccount, and
offers Authorize/Deny. Existing selection shows its UUID, reviewed version and grant history, and
requires explicit authority-replacement confirmation. Changing the ServiceAccount clears that
confirmation. A fresh authorization can reconnect to the same ServiceAccount or choose another; no
policy editing is offered.

The page calls the app's authenticated BFF at
`/upstream/connections/v1/operator/connection-enrollments/{handle}/preview` and `/decision`;
the app calls the Connection Service's generated gRPC API, which authorizes each request and keeps
consent state. The decision carries the
version the preview returned. The service pins the enrollment to the authenticated operator of the
first preview, in whichever browser session, owns expiry, and records one decision: it refuses
any decision after that, even a repeat after a lost response, so a lost answer means a fresh
OAuth authorization from the MCP client. An existing Connection's reviewed version is part of the
decision; a 409 never silently refreshes it. Consent does not revoke old authority: token
exchange's version-checked reservation does, before replacement activation. Failed issuance does
not restore old grants or rewrite Action provenance; deliberate operator Connection rebinding
(below) is separate from consent and can change the account an existing credential acts as.

The frontend parses generated protobuf JSON and adapts its field names and enum values to the
existing page model. Preview combines the enrollment metadata with the service-account and
Connection lists; the server-held continuation is not returned to the browser.

Authorize follows only the server-held continuation returned by the authenticated Connection
Service; no client or browser-provided URL is accepted. Deny grants nothing and leaves a
terminal page. Completing consent alone does not activate a grant: the MCP edge's OAuth server still
verifies the same upstream operator and completes issuance. Operator credentials never
reach the external client. An external-client Action is auto-approved only by an
PolicyBinding naming its ServiceAccount, otherwise by the operator; clients sharing a
ServiceAccount share receipts while submitted Actions preserve exact connection provenance.

## Action review

`/#/actions` renders Action Service receipts through the app's authenticated BFF. The browser
lists and reads receipts under `/upstream/actions/v1/action-requests`, decides through
`/upstream/actions/v1/operator/action-requests/{request_id}/decision`, and watches pending requests
at `/upstream/actions/v1/action-requests:watch`. The BFF validates each route and calls the generated
Action Service gRPC APIs with the current operator session's federated bearer; browser cookies and
credentials are not sent upstream. Responses use ProtoJSON's lower-camel field names and symbolic
enum names. The
frontend converts generated messages into local presentation types while preserving caller
arguments and execution results as their original JSON. A receipt names its caller ServiceAccount,
whether a Pod or a Connection acting as it submitted it.
The service owns persistence, authorization, Decisions, dispatch, and recovery. Workload
submission and owner-scoped reads use the service's `/v1/action-requests` API, not the app.

Staging configures federation: the Deployment reads `AGENTPLANE_ACTION_FEDERATION` from the
Git-owned `agentplane-action-federation` ConfigMap (built from the `action_federation`/
`operator_oidc` dicts in `cluster/cdk8s/generate_manifests.py`), and the app's network
policies admit Authentik by TLS SNI, the Action Service on 8080, and the MCP edge's Connection
Service on 8080. The app composes a request-bound
JWT-bearer exchanger; the service independently validates the exchanged operator JWT. No static BFF
bearer or workload-token promotion is used. Missing configuration is specifically
`503 detail.code=operator_federation_not_configured`; token callers get 403.

See [`../docs/operator_federation.md`](../docs/operator_federation.md) for exact settings, PostgreSQL
session storage, opaque session lifecycle/CSRF/invalidation, failure codes, and signed
multi-replica/two-operator test targets. Existing browser sessions must log in again after rollout.
There is no app-owned Action/Decision/Execution authority.

## Settings

The sidebar footer's gear button opens a modal with OAuth clients/MCP servers/Notifications tabs; it
has no dedicated route of its own. The one exception is `/#/mcp-servers`, the MCP-linkage OAuth
callback's redirect target (see below): landing there opens the modal pre-selected to that tab
instead of showing the Threads landing view as if the linkage completed silently.

The OAuth clients tab lists the MCP edge's Connections, each row showing its most recent grant's
OAuth client ID and its current bound ServiceAccount; superseded grants stay in the MCP edge's
history but are not listed here. The operator can select an eligible
ServiceAccount and explicitly confirm rebinding: the existing client token remains usable, but
future Actions act as the new account. The version-checked update never silently retries a stale
choice. Unlink instead revokes active/pending authority without deleting history or stopping
already claimed executions. An ineligible current account is flagged separately from the grant's
lifecycle status. A Connection's name is edited in place, like a Thread's name: Enter or moving
away commits, Escape restores, and a blank name keeps the stored one.

Each current account has a **View grants** disclosure, reusing the Sandbox bindings list and Action-policy
renderer. The operator-only `GET /caller-grants/{namespace}/{name}` composes existing account-scoped
CR views and the Action Service's effective-policy answer; no Sandbox, Connection record or active
caller-admission label is required for that read. The UI is a manually refreshable snapshot, not a
watch. Missing references, expiry and unsynced/unavailable Action policy stay visible; an unavailable
Action Service does not hide bindings. The disclosure is read-only and does not calculate
arbitrary Kubernetes RBAC. Existing Sandbox grant editing and live updates are unchanged.

The browser reads and updates Connections through the app's authenticated BFF under
`/upstream/connections/v1/operator/`: list, watch, rename, rebind, unbind, caller accounts, and consent.
The app calls the generated Connection Service gRPC API and serializes its responses as ProtoJSON;
`WatchConnections` streams NDJSON, replacing the snapshot after reconnect and applying
upserts by Connection ID. Protobuf int64 versions stay decimal strings in the UI adapter and are
sent back as `expectedVersion`; an `ABORTED` response refreshes the inventory and asks for review,
never automatically retrying a destructive operation. The app owns no Connection state. Reconnect
requires fresh OAuth authorization from the external client; the management rebind is a separate
operator-authorized retarget of an existing token. Policy editing and deployment are outside this
surface.

The app connects to the Connection Service through the separately configured `connection_service_url`
and to the Action Service gRPC APIs through `action_federation.service_url`. The public MCP-linkage
OAuth callback remains a narrow HTTP call to the Action Service. Staging points
`connection_service_url` at the MCP edge and `action_federation.service_url` at the Action Service.
Each service has its own gRPC health probe.

The MCP servers tab lists the Action Service's OAuth-linked MCP server groups and links or
disconnects each one. It follows independent gRPC `WatchMcpServers` and `WatchActionGroups` streams
through the app's NDJSON BFF routes at `/upstream/actions/v1/operator/mcp-servers:watch` and
`/upstream/actions/v1/action-groups:watch`. Both replay a bounded snapshot before upserts; the tab
joins their latest states by group key/server ID without coupling their change rates. Link and
disconnect use the app's `POST /upstream/actions/v1/operator/mcp-servers/{id}/linkage/start` and
`.../linkage/disconnect` routes, which call the Operator Action Service with the request-bound
operator bearer. The provider's
registered redirect remains the public `GET /mcp-linkage/callback`; the app forwards its state and
code to the Action Service's public callback and returns the browser to `/#/mcp-servers`. The Go callback's
ProtoJSON response is not part of the browser contract. What a link authorizes and when a linked
group becomes available is the Action Service's contract
([its README](../action_service/README.md#action-catalog)).

## Launch presets

`GET /presets` publishes configured Sandbox presets and their Thread launch defaults. `POST
/sandboxes` stores the selected preset label alongside the concrete Sandbox choices; it does not
store per-Sandbox Session defaults. On a new Thread open, the app resolves the defaults from its
configured preset catalog using that Sandbox label, then overlays fields explicitly supplied for
the Thread. `policy_sets` are the Sandbox's grants beside the deployment's defaults, and a set name
the namespace does not hold is refused with 422 before the Sandbox exists.
The form records the picked preset in the URL (`/#/?preset=<name>`) so a launch form can be linked
to.

A Sandbox takes `policy_sets` alone. The shared inventory validates the app's configured default sets
with the submitted policy sets; Action policy sets and Kubernetes grants are not create inputs.
`GET /sandboxes/form` lists the namespace's `PolicySet`s, which the form offers; a preset names
`PolicySet`s only. It also serves the app-level `new_sandbox_defaults`, the wake-on-message and
idle-suspend values the form starts with whatever the preset; `POST /sandboxes` stores only what the
caller sends, so an API caller that omits them gets neither. A preset's `inbox_tags` likewise prefill the form, and `POST /sandboxes` writes the submitted
ones as the Sandbox's initial `spec.inboxTags`: editing a preset changes only later launches.

Before opening a session on a bound Sandbox, the app sends the SandboxPreset's configured bootstrap
content to the runner under a stable preset identity. The runner executes it idempotently on the
persistent state volume; a failure refuses the session open. The selected Sandbox preset determines
the complete default `SessionSpec` even though the Sandbox stores only its preset label. An explicit
request can override those defaults, including with empty strings. The app composes the Session's preset and task instructions. The Runner composes those with the
deployment-wide platform instructions before starting the harness, including for direct
`SessionSpec` API launches ([Runner specification](../runner/SPEC.md)). The shared block teaches agents
the platform's egress and Actions Service protocol and how to query their effective Kubernetes
permissions; a preset and the per-turn task remain the place for workload-specific constraints and
the requested outcome.

Thread defaults may also include a one-time setup script. A new Thread sends its resolved cwd and
setup source on the runner's first `Open`; the runner records setup stdout, stderr, and completion in
the Thread event stream and starts the harness only after setup succeeds. A failure leaves a
readable Thread and workspace but cannot rerun setup under the same session id. The Sandbox page
uses the configured cwd template instead of replacing it with a hardcoded workspace path.

A new Thread's id is the public Session ID the browser mints and the opening API preserves;
`{session_id}` in a cwd resolves server-side. The browser sends a fresh Session ID with the original
overrides once and keeps only that ID in `sessionStorage`, never the spec or setup script, so it
cannot retry the create. After a lost reply or a reload it never re-sends Open. **Check Open status**
reads the Dispatcher opening and checks `GET /threads/{id}` for the projected Thread. A queued or
runner-admitted opening is not presented as an existing Thread until History records it; the page
keeps the status and reason visible while the projection catches up.

## Policy bindings

The Sandbox page's Policies tab, carried in the live snapshot frame, lists every `PolicyBinding` naming
the ServiceAccount the Sandbox runs as, from the objects the app's own watch holds (`egress_views.py`).
A row is the binding's provenance (git; the Sandbox's own, which its controller keeps equal to the
Sandbox's policy sets; or an operator's grant), its expiry, and the sets it names, a missing or refused
one in red. Its icon sums up the verdicts each reader wrote to the binding's status (`KubernetesReady`,
`ActionsReady`, `EgressReady`): ready when every reader that has judged it accepts its current
generation, pending while none has or one is behind, refused when one says false or a named set is
missing or refused. The reasons are the icon's hover text. Expanding a row shows each set's rules of
every type in the set's order: an egress rule as its hosts, methods, paths and credential, any other as
the set states it. A binding is the grant, so nothing here depends on the Action Service being asked.

Revoke deletes an operator's grant; a binding from git or the Sandbox's own refuses, since each would be
applied again. **Grant policy sets** adds a binding naming the Sandbox and is, with revocation and the
launch pick (drawn from `GET /sandboxes/form`, the namespace's PolicySet names), the only write the tab makes.

Below the bindings, **Auto-approve if** is the Action Service's own answer for that account, read through
the app's authenticated BFF (`/upstream/actions/v1/action-policy?subject.namespace=...&subject.name=...`, so an
operator session is required as for the Actions page): the `action_auto_approve` policies in the order the service
walks them, each entry naming the binding, set and index a Decision's evidence names, and `synced`, false
while the service's watch has not synced and nothing auto-decides. It is the resolution an admission would
use now, from the service that would use it, and says nothing about past Decisions; the Actions page holds
those. The app's own watch of the two kinds is a trigger: an event on either re-asks the service for the
next frame, and a binding lapsing while nothing changes leaves the page at the next frame. The two watches
are independent, so a frame can briefly precede the service's informer seeing the same event and show the
answer from just before it. Where the service cannot be asked -- no federation configured, a session to log
in again, a failed exchange or request -- the frame says so in place of the list rather than showing an empty
one. Which lists the service enforces is its contract
([SPEC § Action policies](../action_service/SPEC.md#action-policies)).

## Decisions

- **Connection direction:** the Runner Gateway dials the runner's private mTLS gRPC server
  ([runner control](../docs/runner_control.md)); the runner pushes journals to Event ingress.
  Callers use the per-Sandbox control Service across Pod replacements.
- **Model credentials:** staging uses a dedicated key for OpenAI/Claude subscription and local
  Ollama chat routes; testing uses the `cheap-experiments` LiteLLM key. The Pod holds no
  key or workload token: a harness sends the inert placeholder the `agentplane-workload`
  EgressCredential derives from its name, central substitutes the sidecar-only Pod-bound token,
  and the authenticated LLM ingress replaces it with its one server-held key after resolving the
  `WorkloadPrincipal` the token proves. The model endpoint remains governed by the credentialless egress design
  in [the ADR](../docs/adr_sandbox_proxy_gateway.md) rather than excepted from it.
- **No gRPC-Web or Connect surface for browsers:** the app stays the one HTTP surface; the schema is
  shared through proto-JSON and generated types instead.
- **Deletion takes only a suspended sandbox:** it removes the Pod and the volume with everything on
  it, and nothing brings that back. The rule lives in the API rather than in the browser, so it also
  binds the agent driving staging with a token; the two clicks it costs an operator are suspend and
  then delete. A running sandbox answers `DELETE` with 409 and a message saying to suspend it.
- **Resume during a suspend is retryable:** the Sandbox page's explicit resume requests Sandbox
  lifecycle state through `POST /sandboxes/{name}/resume`; while the controller seals an incarnation,
  the app returns HTTP 503. Retry after the Sandbox reaches Suspended. This remains available when
  `wake_on_send` is off; that setting controls whether queued work may wake the Sandbox.
- **Raw is additive:** both modes use the same chronological thread blocks and disclosure
  state. Compact Event summaries sit between those blocks; expanded details retain the full generated
  envelope, native payload, and causal references. JSON wraps instead of forcing horizontal scrolling.
  Cards are labelled as current aggregates, not historical snapshots at their first cursor. The
  [projection contract](../docs/thread_layering.md#timeline-pending-queue-and-operational-state) separates
  this evidence from the pending-command queue and operational snapshot.
- **Failed model turns:** normal and Raw views show the terminal failure and its plain-text
  diagnostic at the completion position, retaining any partial output. A confirmed input remains
  confirmed; a failed turn neither creates a delivery retry nor resends that input. Later input can
  be submitted to an eligible dormant Session, which the Dispatcher wakes as needed.
- **Interrupted or lost turns:** the fold keeps streamed text, tool arguments, and any tool output
  the harness reported. An item without a terminal native result stays incomplete; an unfinished
  tool result is unknown and is never retried automatically. Resume continues the same native
  session, whose history recovery can differ from the app's folded evidence.

## Action live updates and browser notifications

`/#/actions` follows the Action Service's gRPC `WatchActionRequests` through the app's NDJSON BFF at
`/upstream/actions/v1/action-requests:watch`, replacing its old two-second timer poll. Watches send a
bounded snapshot followed by upserts; reconnecting opens a new watch and replays a fresh snapshot.
An unavailable watch is shown as disconnected rather than silently presenting stale state as live.
Each stream is tied to the operator session and a fresh federated bearer. The browser reconnects
after an upstream stream ends, so the next request exchanges a fresh token. Logout or session
expiry closes the response, and the browser cannot supply an upstream bearer or cookie.

While the navigation sidebar is closed (phone width by default) and the route is not under
`/actions`, a strip under the top bar shows the first two pending Actions as the sidebar's compact
cards, decision buttons included, so a pending decision is visible over an open Thread. A longer
queue adds "Show N more", which expands the rest in place and stays expanded across a visit to an
Action's details page, such as approving there and being sent back (`ActionsAttentionBar`). The
sidebar's own Actions section is left out on `/actions`, whose page is the queue; details pages keep it.

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

## Notification diagnostics

An operator can open the read-only Notifications drawer from a Thread (filtered to its runner
session) or a Sandbox (all its sessions). The app's authenticated BFF resolves the live Sandbox
incarnation and follows the Notification Service's UID-pinned, bounded-summary `NotificationsOperator`
watch ([`notifications_operator.proto`](../notification_service/notifications_operator.proto)) over gRPC with its
own rotating audience-scoped service token, relaying each snapshot to the browser as an SSE `snapshot` event in
the proto3 JSON form. It shows the first 100 retained entries after
acknowledgement (provider, cursor, time, and curated lifecycle/event summary), with a truncation
indicator. It never sends that token or raw provider payloads to the browser, and requires a
browser operator login, not merely a workload bearer admitted elsewhere in the app. No inspection acknowledges an entry or changes a subscription. The drawer refreshes
only while open and labels an unavailable backend or retained snapshot explicitly.

Each listed thread whose live inboxes hold entries the agent has not acknowledged gets a badge
counting them, whether they still wait for a notice or a notice delivered them (the tooltip tells
which, with the debounce's estimated due time). The badge is muted gray, soft red while delivery
fails; an empty inbox shows none. `/threads/notices/stream` relays the Notification Service's `WatchSessionNotices` for the listed thread IDs
(a Thread's ID is its Session's) under the same operator-login and service-token rules; a disconnected stream dims the last badges. Following it
acknowledges nothing.
