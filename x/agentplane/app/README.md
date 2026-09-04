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
- `bridge.py`: one runner attachment per streaming session, fanned out to every browser tab, and
  the SSE framing; `api.py` is the REST surface and the OpenAPI schema `export_schema.py` emits
  for the frontend's generated client.
- `live.py`: one list-and-watch over Sandboxes, their Pods, and the egress objects
  (`../kubernetes_watch.py`), and the SSE streams that push a snapshot of it to every open tab.
- `identity.py`: whether a request proved itself, by whichever credential it carried; `oidc.py` and
  `auth_routes.py` are the browser's half of that (see below).
- `trajectory.py`: the PostgreSQL store of threads and their events.
- `frontend/`: the React SPA on the repo's `ts_library` and esbuild toolchain, with the visual
  scenarios under `frontend/visual/`.

## Live views

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
  `preferred_username` in a signed `__Host-` cookie. Unsafe methods additionally have to be
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
                    model traffic to LiteLLM with the cheap-experiments key from a Secret
```

Kubernetes is the sandbox inventory, including the archived flag; the runner holds the live
session; PostgreSQL holds the copy of every event that outlives the sandbox. The app stores no
product state beyond that until a feature needs it.

## Decisions

- **Connection direction:** the app dials the runner Pod's address directly, re-resolving on
  reconnect; Pod replacement changes the address and the session log makes the cursor valid across
  it. A Service per sandbox is not needed until something outside the cluster must reach a runner.
- **Staging first, on the cheap key:** the first instance exists for the agent to test against
  autonomously, so its runner Pods carry the `cheap-experiments` LiteLLM key as environment, the
  same operational convenience the `agent-workspaces` Codex lane uses. The credentialless egress
  design in [the ADR](../plans/adr_sandbox_proxy_gateway.md) governs external systems, not the
  model endpoint.
- **Transport security on the runner port:** Cilium policy between the app namespace and the
  sandbox Pods is the v0 control. Authentication on the port itself waits for the credentialed
  readiness gate.
- **No gRPC-Web or Connect proxy in front of the runners:** browsers cannot carry the
  bidirectional `Attach`, and a standard proxy would still need per-sandbox routing to Pod
  addresses that change on every resume. The app stays the one HTTP surface; the schema is shared
  through proto-JSON and generated types instead. Splitting `Attach` into a server-streaming
  `Open` plus unary commands waits for a second, non-browser client that wants it, since the
  stream is what identifies the controlling attachment today.
- **One replica:** the bridge holds live runner attachments in memory, and a second replica would
  supersede them. The live watch would not mind more -- each replica would hold its own copy of the
  same objects and push it to its own tabs -- so it is the bridge alone that keeps the count at one.
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
