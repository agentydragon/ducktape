# Agentplane egress proxy

The central proxy of the secure egress integration: sandbox tools reach the outside through it,
it decides each request from resources on the API server, and it alone holds the credentials it
substitutes. The design it implements is [the ADR](../docs/adr_sandbox_proxy_gateway.md).

## Configuration

The forward listener is configured by `forward.listen_address`, serving `forward.tls`, and an
explicit `forward.auth` list containing `spiffe` with trust roots and/or `kubernetes_bearer`
with its TokenReview audience. These structured settings
are file-only. The sidecar names its central proxy as `proxy.address` (`host:port`) and selects
TLS with an explicit SPIFFE certificate or projected bearer authentication mode. Unknown fields, unsupported or duplicate modes, incomplete
credentials and incompatible transport/auth combinations fail startup.

The sidecar uses Go's `http.Server` parser and `httputil.ReverseProxy` for ordinary HTTP requests.
Malformed request lines receive the standard `400` response without the sidecar refusal header;
headers over the configured limit receive `431` or the connection closes before a request reaches
the handler. These parser outcomes do not use a fallback parser. `CONNECT` remains an opaque tunnel
after a successful response, and HTTP upgrades are relayed as protocol switches.

## Identity

- The single forward listener requires TLS. Its authentication allowlist explicitly admits SPIFFE
  trusting the Agentplane workload CA alone, and Pod-bound Kubernetes bearers for existing
  non-Sandbox callers; a leaf of the cluster's internal CA naming a Runtime URI does not
  authenticate. It presents `spiffe://agentplane/egress-proxy`, issued by the workload CA
  with serverAuth. The sidecar's `proxy.transport.ca_file` holds the workload CA alone; it verifies
  this SVID and the service SVIDs behind workload and platform routes. Clients require clientAuth and
  the canonical URI `spiffe://agentplane/<namespace>/<name>/<credential-id>` of a `WorkloadCredential`.
  Plaintext is refused. Presenting both a certificate and bearer is refused; an invalid credential
  never falls back to another authentication method. A bearer is reviewed for the configured audience
  and allowed namespace, and its ServiceAccount is the unchanged policy subject.
- Each SVID-authenticated request, including requests inherited through a CONNECT tunnel, checks
  the leaf's validity and EKU again and resolves it through the shared resolver
  ([how a verifier resolves a peer](../docs/workload_authentication.md#how-a-verifier-resolves-a-peer)):
  one uncached GET of the named `WorkloadCredential`, whose current credential ID and URI must
  match, and for an owned record an accepted binding equal to its current one. A Sandbox's deletion
  timestamp or stop stage does not revoke an otherwise accepted credential: the runner may need to
  stay authenticated while it quiesces and seals final Session history. Revocation takes effect when
  the record is removed, with its Sandbox or on stop, or its binding is cleared or replaced.
  Replaced or stale credentials fail before policy evaluation. There is no cached identity.
- The resulting policy subject is exactly the record's subject ServiceAccount, for a Sandbox its own
  at `Sandbox.namespace/Sandbox.name`, read live for its UID. Neither a certificate subject DN nor a
  caller-supplied header selects the subject, and the proxy does not read Pods or Sandboxes for it. A
  forwarded projected token must be bound to the record's `spec.pod`. A ServiceAccount no binding
  names reaches no rule.
- A request authenticated with SPIFFE does not need a hop bearer and must not carry one. A sidecar's
  separately projected Kubernetes token is forwarded under `X-Agentplane-Workload-Token` for a
  `projectedWorkloadToken` credential. The proxy keeps it only when TokenReview proves the same
  ServiceAccount and Pod UID as the live Sandbox binding and the credential names its configured
  audience; a mismatched token is dropped and a rule needing it refuses with
  `credential-unavailable`.
- A copied SVID proves only the live Sandbox and current runner binding named by that SVID.
  Each request, including one inherited through a tunnel, repeats validity and live-state checks.

## Decision

- Exact configured first-party TLS authorities use opaque CONNECT after hop authentication.
  Each destination terminates TLS, resolves the workload SVID and applies its own authorization.
  Central cannot read these calls or assert their caller. Other destinations retain normal rules
  and credential substitution.

- A subject's egress rules come from `PolicyBinding`s. A `PolicyBinding` names one namespaced
  ServiceAccount and `PolicySet` names, and grants the sets' `egress` rules by existing: creating one
  is the whole act of allowing, deleting it the whole act of taking that back, and the Policy
  Controller deletes it at its `expiresAt` (`agentplane/policy_controller/SPEC.md`). A set that is
  missing takes nothing from the binding's other sets. The same name in two namespaces is two subjects. A
  subject is every Pod running as that account, so bind only an account dedicated to one workload;
  the integration app gives each sandbox an account of its own, owned by it, so deleting the sandbox
  collects the account and the bindings naming it alike.
- Hosts admit exact names, `*.` suffix wildcards (not their apex), or `*` for any public
  destination. A blanket `*` rule cannot set `credentialRef` or `clusterInternal`, enforced by
  both CRD admission and runtime parsing. It grants no credential substitution or private-address
  exemption; combine it with separate, destination-scoped credential/internal rules as needed.
  Authentication and a live binding are still required. No default or preset opts in.
- A rule matches a request when its hosts, methods, and paths all admit it. One matching rule in
  any egress policy of any of the subject's bindings is enough to admit the request; nothing matching
  refuses with `no-rule`. A CONNECT is matched on host alone; each request inside the tunnel is
  decided on its own.
- A credential is an `EgressCredential`: where its real value comes from, and every exact location
  it may be presented in. Its placeholder is `agentplane-credential-<name>`, derived from the
  object's own name and written nowhere, so one placeholder means one credential by construction.
  A rule names a credential; the credential names the targets. Its source is exactly one of
  `secretRef`, preserving the central-held Secret behavior; `projectedWorkloadToken`, naming the
  audience of a token the sidecar presented on the hop; or `kubernetesServiceAccount`, naming an
  account the proxy acts as on its own API server. Absent, stale or mismatched context refuses
  with `credential-unavailable`.
- **A Kubernetes account is acted as by impersonation.** A `kubernetesServiceAccount` credential
  names a namespace and a ServiceAccount on the API server the proxy's own credential reaches. A
  named API server is the intended extension for remote clusters, not modelled for lack of a use
  case and additive when one appears. Its only target is `schemeToken` `Bearer` on
  `Authorization`. Like a credential of any source the proxy cannot read, one
  that breaks these rules is dropped from its index, so its placeholder is unknown and reaches the
  API server inert. A request it decides gets the
  proxy's own bearer for that server in place of the placeholder, read from its token file once
  per request, and `Impersonate-User: system:serviceaccount:<namespace>:<name>` beside it, in one
  decision: the proxy's bearer is never forwarded without that header. The API server adds the
  ServiceAccount groups itself; the proxy never sends `Impersonate-Group`, `Impersonate-Uid` or
  `Impersonate-Extra-*`. A request decided by such a credential that carries any `Impersonate-*`
  header of its own is refused with `impersonation-forbidden`. The bearer goes only to the
  configured local API server host and port over TLS the proxy verifies; plaintext, another
  admitted host or another port refuses with `credential-unavailable`. No token is minted or cached
  for the account. What the proxy may impersonate is its RBAC: `impersonate` on `serviceaccounts`
  in one accounts namespace. Revocation is the use rule going, enforced from the next request,
  including the next one inside an established tunnel; the account's own roles are revoked only
  when their RoleBindings go. The decision records the impersonated account beside the subject,
  and the API server's audit log names the proxy as the original user.
- **Kubernetes keeps its projected workload credential.** The sidecar sends it as
  `X-Agentplane-Workload-Token: <audience> <token>`, and central consumes this header rather than
  forwarding it. TokenReview must resolve it to the Sandbox's own ServiceAccount and the Pod UID
  recorded in its live runner binding for SVID callers. For bearer callers it must match the
  authenticated hop's full TokenReview principal. Both require a configured audience. A failed review drops that token;
  a rule requiring it refuses with `credential-unavailable`. The sidecar replaces caller-supplied
  copies of the header, and the workload cannot read the projection. A tunnel's inner requests
  inherit the reviewed token from CONNECT. Other platform services verify SVIDs themselves.
- A **target** is a header and a parse of that header's value: `wholeValue` (the value entire),
  `schemeToken` (`<scheme> <credential>`, the scheme declared and compared case-insensitively),
  `basicUsername` and `basicPassword` (the halves of a `Basic base64(username:password)` payload),
  or `basicWhole` (a `Basic` payload that is the credential entire). A request **presents** a
  placeholder when some target's parse of some value of that header yields a component **equal** to
  it. A placeholder that is merely a substring of a component, or sits in a header or a shape no
  target declares, is not presented — it is neither detected nor substituted, and reaches the
  upstream inert.
- **`jsonField`** presents a placeholder as the entire string value of a declared top-level
  JSON object field. It is substituted only by the same credential-authorized host/method/path
  decision as header targets. Rebuilding uses JSON encoding, not text interpolation, and changes
  no other field's value. A body credential's route accepts only uncompressed UTF-8
  `application/json` objects up to 64 KiB; duplicate keys, malformed JSON, non-finite numbers,
  unsupported media/encoding and oversized bodies fail closed with `invalid-body` (unless the
  credential was presented through a declared header target instead). Nested paths, arrays,
  query parameters, substrings and arbitrary templates are not supported. Other requests keep
  their existing body handling. Body values and rewritten bytes are excluded from evidence and repr.
- Which of the matching rules decides is directed by the placeholder the request presents. A
  placeholder is known when some `EgressCredential` in the namespace has it, whether or not the
  subject is bound to an egress policy naming that credential. A request presenting a known placeholder is
  decided by a matching rule naming exactly that credential; when none does — the matching rules
  name no credential, or another one, or the request presents two — it is refused with
  `placeholder-unresolved`. A request presenting no known placeholder is decided by the first
  matching rule and forwarded as it came. Granting a subject a broader binding therefore only
  widens what it may reach; it never takes a credential away from a call that asks for one.
- Where several matching rules would decide alike — two naming the same credential, two naming
  none — the first in walk order is the one recorded: bindings by name, their
  egress policies as listed, their rules in order. Which one that is changes neither the verdict nor what
  is forwarded, and shows only in the decision log.
- Hosts match exactly (case-insensitive) or by `*.` suffix, which never matches the apex. Path
  globs match the path without its query: `*` stays within one segment, `**` crosses segments.
- Substitution rebuilds each presented value around the real credential, through the same parse
  that found it, at every target the request presents it in and no others — so a placeholder the
  proxy recognised is never one it forwards. A value of that header the request sent alongside and
  did not present the placeholder in is forwarded untouched. A credential whose Secret or key is
  absent, or whose authenticated workload context is unavailable or unbound, denies with
  `credential-unavailable` rather than forwarding. No token source copies a raw
  `Proxy-Authorization` value and never appends `Authorization`; the declared target and exact
  placeholder presentation remain the only substitution authority.
- Nothing else is forwarded: no binding, no rule, an unproven token, a Pod that does not match,
  or any failure to reach the API server all refuse. A refusal is `403`
  (`502` when the proxy itself could not decide) with an empty body and
  `x-agentplane-egress: denied; reason=<reason>`, where reason is one of `token-missing`,
  `token-rejected`, `pod-mismatch`, `no-binding`, `no-rule`,
  `placeholder-unresolved`, `credential-unavailable`, `impersonation-forbidden`, `invalid-body`,
  `address-forbidden`, `host-unresolved`, `unavailable`.

## Upstream address

- **A sandbox can read its effective egress rules** with `agentplane egress rules`. The CLI calls
  `EgressRules.GetRules` through the local sidecar gRPC route on shared platform port 3130 (the launcher relay for
  VM guests). The shared gRPC listener uses the forward listener's authentication policy,
  certificate and trust roots. The sidecar establishes destination
  TLS through an exact opaque CONNECT route; the destination verifies the actual peer and live
  Sandbox credential. No caller header selects the subject.
  The response contains only that ServiceAccount and its redacted rules: hosts, methods, paths,
  credential descriptions, placeholders and presentation targets, never Secret names, keys or
  values. Missing, ambiguous, invalid or stale credentials fail before reading rules; unavailable
  verification returns gRPC `Unavailable`. The response is an informational snapshot of the
  answering replica's enforcement index.
  Rules uses the shared native gRPC port 8081. With forward TLS configured, authenticated Rules
  calls require TLS on that same port. Probes and independently authorized operator methods use
  the same TLS gRPC server. Operator callers verify the proxy service SVID and retain their bearer
  audience/account policy; a Sandbox SVID never authorizes an operator method. Plaintext is refused.
- `agentplane egress guide` and `agentplane egress guide credentials` print the bundled Markdown
  workflow guides without requiring a service connection. Effective access comes from the live
  authenticated rules response.
- The admitted host is resolved by the proxy, never by the sandbox. A host with any address that
  is not globally reachable unicast — loopback, private, link-local, carrier-grade NAT, multicast,
  reserved, and the IPv4-mapped IPv6 forms of those — is refused whole with `address-forbidden`;
  a literal address in place of a host is held to the same rule.
- **A rule may declare its hosts cluster-internal**, which lifts that refusal for private unicast
  addresses — and only for the rules that say so, never for loopback or link-local, which are the
  sandbox's own interfaces rather than the cluster. It is how an in-cluster service is reached
  through the proxy at all; without it the guard refuses every Service address. A pin is keyed by
  host and port alone, so the check runs again on a cached address: one pinned for a rule that
  declared its host internal is not served to a rule that did not. A host that does not resolve is
  refused with `host-unresolved` (`502`).
- **The destination's own certificate is verified**, against a bundle the deployment supplies and
  not against the interception root, which the proxy issues rather than trusts. That bundle carries
  the public roots plus this cluster's CA, which is what lets a `clusterInternal` rule reach the API
  server; a destination it does not cover fails the connection rather than being carried unverified.
- The connection is made to the address that was checked: a name is resolved once per admission
  window (30 seconds) and every dial in it goes to that address, so a name cannot be re-pointed
  between the check and the connect. A dial for a target the gate did not admit is not made.
- The operator may exempt networks from the reachability rule (`--exempt-networks`); production
  exempts none.

## Resources and status

- The proxy depends on the API server and nothing else. The integration app is a viewer of the
  same resources, never a participant: no part of a decision passes through it, so an app that is
  down or broken changes nothing about what a sandbox may reach.
- The proxy watches egress policies, bindings, policy sets, policy bindings and credentials in its one configured rules namespace, and
  Secrets in the credentials namespace; it reads no Pods at all, in any namespace. The rules namespace may be one
  of the workload namespaces, and in both deployments it is the only one; the credentials namespace
  is separate, so a workload is never in a namespace holding the Secrets the proxy substitutes. The proxy's picture is kept equal to the API server's, and a
  rotated Secret is substituted from the next request on without a restart. An authenticated
  workload source is request context, not a Secret or another watched object.
- Each `PolicyBinding` carries an `EgressReady` condition, applied under the proxy's own field
  manager so other readers' conditions stay: `Enforced`, or `False` with reason
  `PolicySetUnavailable` naming the missing sets. Every replica writes it from its own picture, so
  it says what a replica resolved, never that every proxy has.
- Admission and readiness fail closed until all watched kinds are initially synced and remain
  within the configured freshness bound. Existing CONNECT/HTTP2 connections do not bypass this
  check. API/watch changes are independently observed; this is not linearizable revocation.
- Termination refuses new connections and requests/streams while already admitted responses drain
  for a bounded period. Unfinished streams can be interrupted; requests are never replayed and
  TCP connections are never migrated. Diagnostic database outages do not gate enforcement.

## Decisions

- Decisions are admissions, not upstream completions. CONNECT and HTTP requests are distinct
  events correlated by connection ID, with event time and stable producer/event IDs. The IDs
  support correlation, not global causal order.
- A record carries names, never values. It has no path, for denied requests too: query
  stripping cannot protect secrets embedded in arbitrary paths. Bodies, headers, query strings,
  raw exceptions, and Kubernetes object payloads are not recorded. An unauthenticated attempt has
  no claimed identity; a recorded Pod UID is a snapshot, not a reference to a live object.
- Recent history is shared PostgreSQL state, not a per-replica ring. Replacing a proxy does not
  remove committed history. `ListDecisions` selects one subject (`at` is decision time) or the
  requests that never authenticated. A lookup spans every workload that has run as that account.
- `WatchDecisions` sends a complete snapshot on subscribing, then added decisions and removed event
  IDs as the bounded window changes. Records are immutable and ordered by decision time and event ID;
  removals include expiry and capacity eviction. A quiet, healthy history feed sends heartbeats; a lost feed or failed history read ends the stream
  with `UNAVAILABLE`. A reconnect starts with a fresh snapshot, not a replay cursor.
- The `EgressAdmin` gRPC service, when enabled, serves `ListDecisions`, `WatchDecisions`,
  `WatchCredentials`, and `GetHealth`. Every call requires an allowed ServiceAccount bearer. Decision
  and credential watches revalidate authorization without a forced lease and end on revocation or
  proxy drain. Credential watches send an initial snapshot followed by upserts and UID-fenced removals,
  and end when informer health is unavailable. Their protobuf contains only public descriptions, placeholders and presentation targets;
  it never exposes sources or Secret values. The standard gRPC health service beside it reports
  readiness (the empty service) and liveness (`livez`) to any caller.
- Responses contain at most 200 recent records by default, ordered by decision time then event
  ID, oldest first within that window. This is not a global causal or insertion order.
- Logging is diagnostic and best effort, not a mandatory lossless audit. A bounded asynchronous
  queue, finite retry budget, and bounded shutdown flush keep database failures off the admission
  path. Queue overflow, expired events, exhausted retries and shutdown loss are counted explicitly.
  A retried batch reuses its event IDs and insertion is idempotent on event ID, so an
  acknowledgement lost after commit can count as a failure but never inserts a duplicate. The
  counters describe producer knowledge: an event counted lost may still have been committed.
- History is not tamper-evident auditing: the database owner can insert and delete records.
- `GetHealth` reports `decision_history`: accepted and acknowledged events (acknowledged counts
  producer events, not newly inserted rows), loss by overflow, unavailability, expiry and shutdown,
  write and cleanup failures, queue depth, whether the writer is running, and `available`, the
  outcome of the last write attempt (`null` before one). Counters are per process and reset on
  restart; none of it changes the enforcement verdict `ready` reports. A failed history
  read fails `UNAVAILABLE`, never an empty success or a local fallback. A successful read may lag queued
  admissions.
- Retention defaults to seven days and is a visibility bound: queries exclude expired records, and
  bounded indexed cleanup deletes them eventually. Database outages delay physical cleanup, not
  the visible time window.
- Schema changes are Goose-managed in the serving binary and run before it opens listeners. The
  embedded migrations preserve existing history when taking over the verified Alembic baseline;
  Goose serializes concurrent startup migrations. A startup probe gates readiness, while old
  replicas may continue enforcing, so every migration must support writer overlap. Request admission
  remains independent of diagnostic DB availability.

## Replica scope

Replicas enforce from independent bounded-fresh watch snapshots and drain independently.
Committed diagnostic history is shared; informational rule/binding observations are not a
cluster-wide acknowledgement. A source change is not deployed evidence: image publication,
GitOps gates and live rollout acceptance remain separate operational requirements.

## What the proxy does not decide

- **Only HTTP(S).** Substitution reaches declared header components and exact declared JSON
  fields only. A placeholder elsewhere — a query parameter, an undeclared body field, another
  envelope — is inert and reaches the upstream unsubstituted. In particular, a URL-borne
  credential would travel through logs and referrers and is never substituted.
- **Intercepted gRPC uses the same header target model.** A TLS gRPC client through the real
  sidecar presents its initial metadata as HTTP/2 request headers; a `wholeValue` target on
  `x-buildbuddy-api-key` substitutes the placeholder before the upstream sees it. The black-box
  test (`test_interactive_grpc_before_eof`) drives a bidirectional Build Event Service-shaped
  stream through the built proxy: several messages, upstream trailers, a client deadline and a
  client cancel, each reaching the upstream. The client trusts the interception CA, the proxy
  verifies the upstream TLS certificate, and neither leg uses mTLS.
- **Only a JSON request body is held for the decision.** A request whose headers declare anything
  but an uncompressed `application/json` body is decided from its headers and its body streams to
  the upstream as it arrives, so an interactive stream such as plaintext gRPC inside a tunnel works
  (`test_interactive_grpc_before_eof[h2c]`). A refusal of such a request
  is sent once its body has been read.
- This proves local Bazel, remote-cache/execution, BES, and BuildBuddy API clients whose credential
  is request metadata. It does **not** make `bb remote` credentialless: the CLI authenticates its
  local Remote Runner control RPC with the metadata, then also serialises the API key into the
  Bazel command run on a BuildBuddy-hosted runner. That runner is outside this proxy, so a
  placeholder arrives there unsubstituted. The remote-runner boundary and candidate rewrite are in <../docs/buildbuddy_remote_auth.md>.

## Sidecar platform routes

- The sidecar may also serve platform routes. A platform route is a loopback gRPC listener per
  configured Agentplane service, which forwards each call to that service as `authorization:
Bearer <token>`. The token is the Pod's projected token for that service's audience, re-read per
  call. The workload's own `authorization` is dropped; other metadata, the deadline and the status
  pass through.
- The central proxy never sees these calls and no egress rule admits them. What may reach the
  service is network policy, and each service authorizes the token itself. Without a token the
  sidecar refuses `UNAVAILABLE` and calls nothing.
- Unknown services fail with `UNIMPLEMENTED`. Each configured route keeps one independent upstream gRPC
  connection, so a failing route does not take down the listener or other routes. With no routes, the
  sidecar opens no platform listener. While an RPC is active, the sidecar sends an
  HTTP/2 keepalive ping after 60 seconds without transport activity and gives its acknowledgement
  10 seconds. It sends no pings without an active RPC. A silent transport failure ends the affected
  call; later calls reconnect through the same route connection and use the service's current
  endpoint. Runner commands are never replayed by the sidecar.
- Route receivers accept active-call keepalive pings no more often than every 30 seconds: Gateway
  control, History ingress, and Alloy OTLP gRPC. They do not permit pings without an active RPC.
- An unexpected exit of the shared platform listener, HTTP proxy, or readiness listener makes the
  sidecar unready and stops the process. Its other listeners close before the process exits.
