# Agentplane egress proxy

The central proxy of the secure egress integration: sandbox tools reach the outside through it,
it decides each request from resources on the API server, and it alone holds the credentials it
substitutes. The design it implements is [the ADR](../docs/adr_sandbox_proxy_gateway.md).

## Identity

- Every CONNECT and every request carries `Proxy-Authorization: Bearer <token>`, the Pod's
  projected ServiceAccount token with the proxy's audience, added by the sidecar. A tunnel's
  inner requests inherit the tunnel's authenticated context. The proxy strips the hop header and
  retains its bearer only after successful verification; malformed or rejected replacement headers
  clear earlier connection context rather than falling back to it.
- The token is proven by TokenReview against that audience. The Pod it is bound to is read live:
  its UID must equal the token's, its address must equal the connection's source, and its
  controller owner must be a Sandbox that the proxy's watch knows under the same UID. That
  Sandbox is the subject.
- A verdict is cached for at most the token's remaining life, bounded by a configured limit; the
  source-address check runs on every request regardless.

## Decision

- An `EgressBinding` grants by existing and unexpired: creating one is the whole act of allowing,
  and deleting it the whole act of taking that back. A binding names its subjects as Sandboxes by
  name and lists `EgressPolicy` names.
- A rule matches a request when its hosts, methods, and paths all admit it. One matching rule in
  any policy of any of the subject's bindings is enough to admit the request; nothing matching
  refuses with `no-rule`. A CONNECT is matched on host alone; each request inside the tunnel is
  decided on its own.
- A credential is an `EgressCredential`: where its real value comes from, and every exact location
  it may be presented in. Its placeholder is `agentplane-credential-<name>`, derived from the
  object's own name and written nowhere, so one placeholder means one credential by construction.
  A rule names a credential; the credential names the targets. Its source is exactly one of
  `secretRef`, preserving the central-held Secret behavior, or `authenticatedWorkloadToken`, the
  bearer retained from successful authentication of this request or CONNECT tunnel. The latter is
  resolved per request and must still be bound to the live Sandbox being decided; absent, stale, or
  mismatched context refuses with `credential-unavailable`.
- A **target** is a header and a parse of that header's value: `wholeValue` (the value entire),
  `schemeToken` (`<scheme> <credential>`, the scheme declared and compared case-insensitively),
  `basicUsername` and `basicPassword` (the halves of a `Basic base64(username:password)` payload),
  or `basicWhole` (a `Basic` payload that is the credential entire). A request **presents** a
  placeholder when some target's parse of some value of that header yields a component **equal** to
  it. A placeholder that is merely a substring of a component, or sits in a header or a shape no
  target declares, is not presented — it is neither detected nor substituted, and reaches the
  upstream inert.
- Which of the matching rules decides is directed by the placeholder the request presents. A
  placeholder is known when some `EgressCredential` in the namespace has it, whether or not the
  subject is bound to a policy naming that credential. A request presenting a known placeholder is
  decided by a matching rule naming exactly that credential; when none does — the matching rules
  name no credential, or another one, or the request presents two — it is refused with
  `placeholder-unresolved`. A request presenting no known placeholder is decided by the first
  matching rule and forwarded as it came. Granting a subject a broader binding therefore only
  widens what it may reach; it never takes a credential away from a call that asks for one.
- Where several matching rules would decide alike — two naming the same credential, two naming
  none — the first in walk order is the one recorded: bindings by name, their
  policies as listed, their rules in order. Which one that is changes neither the verdict nor what
  is forwarded, and shows only in the decision log.
- Hosts match exactly (case-insensitive) or by `*.` suffix, which never matches the apex. Path
  globs match the path without its query: `*` stays within one segment, `**` crosses segments.
- Substitution rebuilds each presented value around the real credential, through the same parse
  that found it, at every target the request presents it in and no others — so a placeholder the
  proxy recognised is never one it forwards. A value of that header the request sent alongside and
  did not present the placeholder in is forwarded untouched. A credential whose Secret or key is
  absent, or whose authenticated workload context is unavailable or unbound, denies with
  `credential-unavailable` rather than forwarding. `authenticatedWorkloadToken` never copies a raw
  `Proxy-Authorization` value and never appends `Authorization`; the declared target and exact
  placeholder presentation remain the only substitution authority.
- Nothing else is forwarded: no binding, no rule, an unproven token, a Pod that does not match,
  an unknown Sandbox, or any failure to reach the API server all refuse. A refusal is `403`
  (`502` when the proxy itself could not decide) with an empty body and
  `x-agentplane-egress: denied; reason=<reason>`, where reason is one of `token-missing`,
  `token-rejected`, `pod-mismatch`, `sandbox-unknown`, `no-binding`, `no-rule`,
  `placeholder-unresolved`, `credential-unavailable`, `address-forbidden`, `host-unresolved`,
  `unavailable`.

## Upstream address

- **A sandbox can read the rules that apply to it**, at
  `http://agentplane-egress.agentplane-staging.svc.cluster.local/v1/rules` through the sidecar's
  existing HTTP(S) proxy, the same path it uses for every other destination. The answer names the
  sandbox and the policies an active binding grants it: each rule's
  hosts, methods, paths, and where a credential is substituted, its placeholder, its operator-written
  `description`, and every target — which is what a client needs to build the value and to know whose
  credential it is spending, since a header name and a placeholder leave open both whether the value
  reads `Bearer <placeholder>` or the placeholder bare, and what the token behind it can do. Never
  the Secret, its key,
  or its value — the projection is built from its own field list, so a field added to a resource
  does not appear here until someone writes it in.
  The caller sends the published inert `agentplane-credential-agentplane-workload` placeholder
  in ordinary `Authorization: Bearer` through the configured proxy. An active ordinary
  `egress-rules` policy/binding authorizes exact substitution using `agentplane-workload`.
  Missing or forged destination auth and unbound placeholders fail closed.
  The destination independently authenticates its Authorization bearer with TokenReview and live
  Pod/Sandbox resolution, without relying on proxy-hop identity or the Sandbox's source address.
  `RulesProjection` checks the authenticated name and UID against the current enforcement index.
  Other request headers and bodies cannot select identity. Service port 80 reaches the distinct
  API listener, not proxy port 8888; there is no local proxy dispatch or recursive forwarding.
  Operator admin endpoints are not available through this route.
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
- The connection is made to the address that was checked: a name is resolved once per admission
  window (30 seconds) and every dial in it goes to that address, so a name cannot be re-pointed
  between the check and the connect. A dial for a target the gate did not admit is not made.
- The operator may exempt networks from the reachability rule (`--exempt-networks`); production
  exempts none.

## Resources and status

- The proxy depends on the API server and nothing else. The integration app is a viewer of the
  same resources, never a participant: no part of a decision passes through it, so an app that is
  down or broken changes nothing about what a sandbox may reach.
- Three namespaces are watched, and the separation is the point: policies and bindings in the
  proxy's own, Sandboxes in the one their Pods run in, Secrets in the credentials namespace. A
  sandbox is therefore never in a namespace holding the rules that govern it or the credentials
  they substitute. The proxy's picture is kept equal to the API server's, and a rotated Secret is
  substituted from the next request on without a restart. An authenticated workload source is
  request context, not a Secret or another watched object.
- Each binding's `status` is written by the proxy: `observedGeneration`, `resolvedPolicies`, and
  the `Active` condition — `True` with reason `Resolved` when the binding is unexpired and at
  least one policy resolved, otherwise `False` with reason `Expired` or `MissingPolicy`. A status is written only when it differs from what the API server holds.

## Decisions

- Every decision is logged as one JSON line naming the subject, method, host, port, path, outcome,
  reason, and the binding, policy, and rule that decided it. Credential values and placeholders
  never appear in logs, decisions, or responses.
- The last decisions per subject (a configured ring, 200 by default) are served on the admin port
  at `GET /decisions?sandbox=<name>`; decisions with no proven subject at `GET /decisions`.
  `GET /healthz` is `200` while the index is both complete and moving: every kind listed at least
  once, and none more than three resync periods since it last completed a list-and-watch cycle. It
  reports how long ago each kind last completed one, so a wedged watch reads as an age rather than
  as a proxy quietly enforcing rules it stopped receiving updates to.
- **One replica**, because the ring is per-process and the admin Service selects every Pod: a second
  would split the view the app and the acceptance suite read. Model traffic takes the proxy like
  every other request, so a proxy that restarts ends the turns in flight, and that cost was accepted
  rather than designed around.

## What the proxy does not decide

- **Only HTTP(S).** A decision is made from a request's method, host, port, path and headers, so
  substitution reaches header values and nothing else. A placeholder anywhere else — a query
  parameter, a body, another envelope — is inert and reaches the upstream unsubstituted, which is
  the property to keep: a URL-borne credential would travel through logs and referrers.
- **Intercepted gRPC uses the same header target model.** A TLS gRPC client through the real
  sidecar and hosted mitmproxy presents its initial metadata in `flow.request.headers`; a
  `wholeValue` target on `x-buildbuddy-api-key` substitutes the placeholder before the upstream
  sees it. The integration test covers both a unary Remote Execution-shaped call and a
  bidirectional Build Event Service-shaped stream, including multiple messages and upstream
  trailers (`test_buildbuddy_http_and_grpc_metadata_placeholder_is_substituted`). The client
  trusts the interception CA, the proxy verifies the upstream TLS certificate, and neither leg
  uses mTLS.
- This proves local Bazel, remote-cache/execution, BES, and BuildBuddy API clients whose credential
  is request metadata. It does **not** make `bb remote` credentialless: the CLI authenticates its
  local Remote Runner control RPC with the metadata, then also serialises the API key into the
  Bazel command run on a BuildBuddy-hosted runner. That runner is outside this proxy, so a
  placeholder arrives there unsubstituted
  (<../../../cluster/k8s/agents/public-coder-agent/app/deployment.yaml>).
