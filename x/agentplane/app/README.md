# Agentplane integration app

The browser and agent surface over Agentplane's sandboxes: a FastAPI service that stamps
Sandboxes from a `SandboxTemplate`, dials each runner Pod over the runner protocol,
streams sessions to the browser over SSE, keeps a watch over the objects its views read so a change
is pushed rather than polled for, and copies every event into the trajectory store as it arrives.
The staging instance lives in `cluster/k8s/agentplane-staging/`.

```sh
bbr test //x/agentplane/app/...
```

## Layout

- `main.py`: the entry point; `Settings` names every knob as a flag, an `AGENTPLANE_*`
  variable, and a key of the YAML file `AGENTPLANE_CONFIG_FILE` points at.
- `presets.py`: app-owned `SandboxPreset` and `ThreadPreset` configuration and concrete launch
  resolution. Preset names stop at the app boundary; Kubernetes and the runner receive resolved
  fields.
- `inventory.py`: the sandbox inventory read from and written to Kubernetes (create, suspend,
  resume, archive, delete), with the parsed subset of each CR it needs. It works in
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
- `bridge.py`: runner-first commands, leased ingestion per sandbox, and database-backed browser
  SSE; `api.py` is the REST surface and the OpenAPI schema `export_schema.py` emits
  for the frontend's generated client.
- `live.py`: one list-and-watch over Sandboxes, their Pods, and the egress objects
  (`../kubernetes_watch.py`), and the SSE streams that push a snapshot of it to every open tab.
- `identity.py`: whether a request proved itself, by whichever credential it carried; `oidc.py` and
  `auth_routes.py` are the browser's half of that (see below).
- `trajectory.py`: the PostgreSQL store of threads, events, feed state, and ingestion leases.
  `trajectory_updates.py` turns committed PostgreSQL notifications into replica-local wakeups.
- `action_federation.py`: request-bound operator federation into the canonical Action Service.
- `consent.py`: browser-session-bound enrollment BFF; the Action Service owns consent and grants.
- `operator_sessions.py`: PostgreSQL browser identity and pending OAuth state, shared across replicas.
- `frontend/`: the React SPA on the repo's `ts_library` and esbuild toolchain, with the visual
  scenarios under `frontend/visual/`.

## Live views

### Replica-safe runner delivery

Every app replica can send commands through an independent runner attachment. Inputs go to the
runner first, with stable `input_id` values: the runner serializes admission and deduplicates retries.
There is no database command queue. A successful HTTP command response is not a promise that the
trajectory copy is already committed; the runner's input settlement events describe its outcome.

One app replica leases each sandbox's ingestion in PostgreSQL. It observes every runner session,
copying events from the last committed sequence. Each write locks and validates the lease token
against database time, so an expired owner cannot write after takeover. Disconnects retry from the
committed cursor; event keys make replay idempotent. A graceful exit releases leases, while a crashed
owner is replaced after its lease expires. This coordinates ownership, not balanced placement.

Sandbox discovery and runner addresses use the existing Kubernetes list/watch cache, including
relist recovery; watch changes wake reconciliation. The timer renews leases and discovers sessions
via runner `ListSessions`, which currently has no watch RPC. Merely observing a stopped session
does not restart its harness. Explicit Open starts/resumes it and waits for ingestion to catch up
before returning, so a resumed browser does not read the previous harness's terminal state.

Browser SSE reads committed PostgreSQL events on whichever replica receives the request, never an
ingester's in-process queue. Transactional `NOTIFY` wakes readers for event, thread, and rename
changes. Notifications carry no data and are not a durable queue: the database sequence cursor is
authoritative, and listener reconnects and keepalives trigger catch-up reads. Stored history remains
readable while the runner is unreachable.

Rollout prerequisite: existing sandbox runners must support independent attachments before the new
app bridge is deployed. This change does not increase deployment replicas or change rollout strategy;
old runner processes are not upgraded merely by publishing the new image.

### Inventory snapshots

`/live/sandboxes` and `/live/sandboxes/{name}` are SSE, authenticated like every other route. Each
carries a whole `snapshot` of what it covers whenever that changes, and a `health` frame through
the quiet in between. What is pushed: the sandbox rows, one sandbox's egress bindings, and its
threads, the last of these from the store rather than a watch, since the app is the only writer of
a thread's name.

Two things stay request-shaped, both because their source offers no stream. The proxy's recent
decisions live in its memory and the egress tab still asks for them on an interval; the runner
answers `ListSessions` per request, so the sandbox page re-reads the session table when the
sandbox's Pod comes or goes and when a session opens (which reaches it as a new thread).

A snapshot is not a delta, and there is no resumable id: a relist replaces a kind wholesale and a
`resourceVersion` expires, so a reconnecting tab is served a fresh snapshot instead. That leaves
one failure to handle honestly -- a watch that has wedged looks exactly like a cluster where
nothing is happening -- so every frame carries how long ago each kind last completed a cycle,
against the same three-cycle bound the egress proxy's `/healthz` uses, and a page whose data has
stopped moving says so rather than showing it as live.

## Authentication and authorization

Every route needs a caller; only `/healthz` and the `/auth/*` endpoints answer without one. There
are two credentials, and both are cryptographic:

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

## Shape

```text
browser (OIDC session)  /  agent (Kubernetes token)
   |  REST + SSE, straight from the cluster gateway
integration app (Deployment, namespace agentplane-staging)
   |  Kubernetes API              |  runner protocol (gRPC, in-cluster)
Sandbox -> Pod, PVC               |
                  runner container <-+
                    runner process, state dir on the PVC
                    claude / codex child per session
                    model traffic to LiteLLM through the egress proxy, which holds the key
```

Kubernetes is the sandbox inventory, including the archived flag and a compact annotation holding
its live preset association plus explicit thread-default edits; the runner holds the live session;
PostgreSQL holds the copy of every event that outlives the sandbox. Preset definitions remain app
configuration, and each launch sends only resolved concrete fields to the runtime.

## External-client consent

The Action Service's OAuth adapter sends a validated authorization request to
`/#/connection-enrollments/{handle}`. The hash route survives the app's existing operator
login. The page shows the client-supplied name, client ID, and validated redirect as text,
asks for a new Connection name or existing Connection and enabled configured Identity, and offers
Authorize/Deny. Existing selection shows its UUID, reviewed version and grant history, and requires
explicit authority-replacement confirmation. Changing Identity clears that confirmation. A fresh
authorization can reconnect to the same Identity or choose another; no policy editing is offered.

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
old grants, and old credentials and Action provenance never change Identity.
The v2 session interaction namespace requires in-flight pre-upgrade consent pages to restart OAuth;
ordinary operator login sessions remain valid.

Authorize resumes only the server-held continuation returned by the authenticated Actions
authority; no client or browser-provided URL is accepted. Deny grants nothing and leaves a
terminal page. Completing consent alone does not activate a grant: the OAuth adapter still
verifies the same upstream operator and completes issuance. Operator credentials never
reach the external client. New external-client Actions use human approval; clients sharing
an Identity share receipts while submitted Actions preserve exact connection provenance.

## Action review

`/#/actions` renders canonical Action Service receipts. The app's `GET /actions`,
`GET /actions/{request_id}`, `GET /actions/{request_id}/events?after_sequence=0`,
and `POST /actions/{request_id}/decision` are an
operator-only BFF over `OperatorActionServiceClient`. They use the service's models
unchanged, including expected versions, idempotency keys, and the human-authored `decision_note`.
The same note is visible to the requesting caller and operator; the existing UI displays it.
Provider-authored bounded `reason_code`/`reason_description` are separate outcome evidence, not
another human note. OpenAPI and frontend types are generated from the canonical models.
External receipts display the immutable authenticated Identity, issuer/client and Connection
from `external_grant`; the grant ID and revision are expandable audit detail. This is
submission-time evidence, not the Connection's current authorization status. Receipts without
that snapshot retain their caller-principal display.
Event reads return canonical `ActionEventView` entries in sequence order, strictly after the
non-negative cursor (default 0).
The service owns persistence, authorization, Decisions, dispatch, and recovery. Workload
submission and owner-scoped reads use the service's `/v1/action-requests` API, not the app.

**Production federation remains disabled** until explicit Authentik target, subject mappings,
allowlists, and network reachability are configured. The app now composes a request-bound
JWT-bearer exchanger; the service independently validates the exchanged operator JWT. No static BFF
bearer or workload-token promotion is used. Missing configuration is specifically
`503 detail.code=operator_federation_not_configured`; token callers get 403.

See [`../docs/operator_federation.md`](../docs/operator_federation.md) for exact settings, PostgreSQL
startup schema creation, opaque session lifecycle/CSRF/invalidation, failure codes, and signed
multi-replica/two-operator test targets. Existing browser sessions must log in again after rollout.
Only operator Action arguments are unredacted; caller arguments and execution result/error
redaction are unchanged. There is no app-owned Action/Decision/Execution authority.

## Connection management

`/#/connections` lists the Action Service's runtime named Connections and immutable grant history.
The operator can rename or explicitly confirm unbind; unbind revokes active/pending authority,
without deleting history or stopping already claimed executions. Configured Identity availability
is displayed separately from each grant's lifecycle status. A missing Identity is not displayed as
enabled, and an active grant does not imply its Identity remains enabled.

The BFF proxies `GET /connections[/{id}]`, `PATCH /connections/{id}`,
`POST /connections/{id}/unbind`, and `GET /connection-identities` through the same request-bound
operator federation as Action review. Unsafe browser requests require exact Origin. Canonical
`ConnectionRename` and `ConnectionVersion` models carry the version the operator reviewed; a 409
refreshes the inventory and asks for review, never automatically retrying a destructive operation.
The app owns no Connection state. Reconnect/rebind begins with fresh authorization from the external
client and selecting this Connection on the consent page; management has no direct retarget action.
Policy editing and deployment are outside this surface.

## Launch presets

`GET /presets` publishes configured Sandbox presets and their inherited editable Thread defaults.
`POST /sandboxes` keeps its no-preset shape and additionally accepts an optional preset: omitted
fields inherit, while explicit policies and thread fields replace preset values. The Sandbox
annotation stores the preset name and only explicit thread edits, so later sessions resolve against
the current configured default instead of freezing a copied form.

Before opening a session on a bound Sandbox, the app sends the SandboxPreset's configured bootstrap
content to the runner under a stable preset identity. The runner executes it idempotently on the
persistent state volume; a failure refuses the session open. The existing full `SessionSpec` API is
available when no preset is selected. Every launch prepends the image's `agent_instructions.j2` to
the task or preset instructions, including direct `SessionSpec` API launches. The app renders its
service URLs from `agent_egress_rules_url` and `agent_actions_service_url` in deployment
configuration. A configured `agent_instructions` key replaces that image default, including an
explicitly empty value. The
shared block teaches agents the platform's egress and Actions Service protocol; a preset and the
per-turn task remain the place for workload-specific constraints and the requested outcome.

## Decisions

- **Connection direction:** the app dials the runner Pod's address directly, re-resolving on
  reconnect; Pod replacement changes the address and the session log makes the cursor valid across
  it. A Service per sandbox is not needed until something outside the cluster must reach a runner.
- **Staging first, on the cheap key:** the first instance exists for the agent to test against
  autonomously, so its sandboxes spend the `cheap-experiments` LiteLLM budget. The Pod holds no
  key or workload token: a harness sends the inert placeholder the `agentplane-workload`
  EgressCredential derives from its name, central substitutes the sidecar-only Pod-bound token,
  and the authenticated LLM ingress replaces it with its one server-held key after resolving the
  live SandboxPrincipal. The model endpoint remains governed by the credentialless egress design
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
- **The raw view keeps a frame on one wrapped line, rather than pretty-printing its JSON:** height
  is what the raw view trades on. Its whole point is reading the order of the session, and measured
  on the `session_raw` scenario the same window holds thirteen events compact against five
  pretty-printed — the turn header, the input and the reasoning block all fall off the page.
  Pretty-printing also does not help the payloads that are genuinely hard to read, since a long
  string value stays one long line either way; wrapping and highlighting are what make those
  legible.

## Action live updates and browser notifications

`/#/actions` follows the Action Service's operator SSE stream through `/actions/stream`, replacing
its old two-second timer poll. Streams provide fresh snapshots after reconnect, and an unavailable
stream is shown as disconnected rather than silently presenting stale state as live. The BFF
bounds stream lifetime to 30 seconds so each reconnect checks current browser-session state,
including logout in another replica. These reconnects are authentication checks, not state polling.

`/#/notifications` registers the current browser, lists registered browsers, identifies this one,
and forgets registrations. Forgetting the current browser also unsubscribes locally. The stable
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
