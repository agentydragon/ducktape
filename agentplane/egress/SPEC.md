# Agentplane egress proxy

The central proxy of the secure egress integration: sandbox tools reach the outside through it,
it decides each request from resources on the API server, and it alone holds the credentials it
substitutes. The design it implements is [the ADR](../docs/adr_sandbox_proxy_gateway.md).

## Configuration

The forward listener is configured by `forward.listen_address`, optional serving `forward.tls`,
and an explicit `forward.auth` list. `kubernetes_bearer` requires an audience; `spiffe` requires
TLS and trust roots. The sidecar names its central proxy as `proxy.address` (`host:port`) and
selects a typed `proxy.transport`: plaintext bearer, TLS bearer, or TLS with a SPIFFE client
certificate. Each method owns its credential settings. Unknown fields, unsupported or duplicate
modes, incomplete credentials and incompatible transport/auth combinations fail startup.

## Identity

- The single forward-proxy listener on port 8888 takes an explicit authentication allowlist. The
  staging default is token-only and plaintext: every CONNECT and request carries
  `Proxy-Authorization: Bearer <token>`, the Pod's projected ServiceAccount token for the configured
  bearer audience. A tunnel's inner requests inherit the tunnel's authenticated context. The proxy
  strips the hop header and retains its bearer only after successful verification; malformed or
  rejected replacement headers clear earlier connection context rather than falling back to it.
- The optional `spiffe` mode adds TLS to that same listener, with the bearer mode still allowed for
  clients without a certificate. It is off by default and enabled together with a server
  certificate, trust bundle, Sandbox RBAC and runner ingress rule. In this mixed configuration all
  clients use TLS. The proxy presents
  `spiffe://agentplane/egress-proxy`, issued by the environment's Runner Gateway issuer with the
  server-auth EKU; the trust-manager cluster-internal CA bundle verifies runner clients. The TLS
  verifier requires the client's explicit client-auth EKU and the canonical URI
  `spiffe://agentplane/environment-runtime/<namespace>/<sandbox-uid>/<credential-id>`.
- Exactly one enabled credential must be presented: no credential, a disabled mode, and a bearer
  together with an SVID are refused before policy evaluation. TLS by itself does not select SPIFFE.
- Each SVID-authenticated request, including requests inherited through a CONNECT tunnel, checks
  the leaf's validity and EKU again and reads the named Sandbox through an uncached API client.
  The live object must still have the URI's UID, a current runner credential whose `id` and
  `acceptedId` both match the URI, and a usable runner binding. A deletion timestamp or stop stage
  does not revoke an otherwise accepted credential: the runner may need to stay authenticated while
  it quiesces and seals final Session history. Revocation takes effect when the Sandbox is removed,
  the credential is cleared or no longer accepted, or its runner binding changes. For Pod and
  Firecracker runners, `runnerCredential.podUid` must match `status.incarnation.uid`; for KubeVirt,
  the current `status.incarnation.uid` is the projected-token Pod binding and the accepted VM
  credential must still carry its VMI UID. Replaced or stale credentials fail before policy
  evaluation. There is no indefinitely cached Sandbox identity.
- The resulting policy subject is exactly the Sandbox-owned ServiceAccount at
  `Sandbox.namespace/Sandbox.name`. Neither a certificate subject DN nor a caller-supplied header
  selects the subject, and the proxy does not read Pods or ServiceAccounts. The optional Sandbox
  reader permission in the proxy's own namespace grants only `get`, `list` and `watch` on Sandboxes.
  A Sandbox name no binding names reaches no rule.
- A request authenticated with SPIFFE does not need a hop bearer and must not carry one. A sidecar's
  separately projected workload token is forwarded under `X-Agentplane-Workload-Token` for a
  `projectedWorkloadToken` credential. The proxy keeps it only when TokenReview proves the same
  ServiceAccount and Pod UID as the live Sandbox binding and the credential names its configured
  audience; a mismatched token is dropped and a rule needing it refuses with
  `credential-unavailable`.
- A token on the forward listener is proven by TokenReview against its audience, which
  names the Pod the API server bound it to; a deleted or replaced Pod fails there once the proxy's
  reuse of an acceptance (30 s, <../workload_auth/README.md>) lapses. The subject is the
  ServiceAccount the Pod runs as, whatever else owns the Pod. Authenticating a workload is not
  admitting it: a subject no binding names reaches no rule.
- The connection's source address is not checked. A token copied out of its Pod and presented from
  elsewhere in the cluster is accepted for as long as it is valid, and is refused the same rules as
  the Pod it was taken from. An SVID copied from a runner is likewise only the transport proof for
  the live Sandbox and current runner binding named by that SVID.
- Every bearer-authenticated request reviews its hop bearer with the API server, including
  requests inside an existing tunnel; an earlier successful review does not authorize a later
  request. The SPIFFE mode repeats the certificate validity and live Sandbox checks for each request,
  including those inside a tunnel.

## Decision

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
  `secretRef`, preserving the central-held Secret behavior; `authenticatedWorkloadToken`, using the
  bearer from a successfully authenticated bearer hop; or `projectedWorkloadToken`, naming the
  audience of a token the sidecar presented on the hop. A projected token is resolved per request
  and must still be bound to the caller being decided; absent, stale, or mismatched context refuses
  with `credential-unavailable`. An SVID hop supplies no bearer for `authenticatedWorkloadToken`,
  so a rule using that source refuses rather than forwarding an empty credential.
- **A projected token is the caller's own identity minted for somewhere else.** The hop bearer
  carries this proxy's audience, so a destination validating its own — the API server against
  `--api-audiences` — refuses it; the sidecar projects a second token per configured audience,
  presents each as `X-Agentplane-Workload-Token: <audience> <token>`, and the proxy consumes that
  header rather than forwarding it. A presented token is kept only when a TokenReview for that
  audience resolves it to the same Pod the hop authenticated, and only for an audience this
  deployment configured; one that fails either is dropped, so the rule naming it refuses with
  `credential-unavailable` rather than substituting something else. The sidecar drops a client's own
  copy of the header, and the workload can read neither token: both are projected into the sidecar
  alone. A tunnel's inner requests inherit what its CONNECT presented, as they inherit its identity.
  On SPIFFE-authenticated requests the reviewed account namespace/name and Pod UID are compared to the live
  Sandbox-owned account and runner binding; no ServiceAccount UID is inferred from the certificate.
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
  `credential-unavailable` rather than forwarding. Neither token source copies a raw
  `Proxy-Authorization` value and never appends `Authorization`; the declared target and exact
  placeholder presentation remain the only substitution authority.
- Nothing else is forwarded: no binding, no rule, an unproven token, a Pod that does not match,
  or any failure to reach the API server all refuse. A refusal is `403`
  (`502` when the proxy itself could not decide) with an empty body and
  `x-agentplane-egress: denied; reason=<reason>`, where reason is one of `token-missing`,
  `token-rejected`, `pod-mismatch`, `no-binding`, `no-rule`,
  `placeholder-unresolved`, `credential-unavailable`, `invalid-body`, `address-forbidden`, `host-unresolved`,
  `unavailable`.

## Upstream address

- The rules API publishes `/llms.txt` and linked Markdown guides under `/docs/agents/` for
  credential presentation and refusal recovery, and the OpenAPI document of `/v1/rules` at
  `/openapi.json`. They contain no credentials or caller-specific rules;
  effective access still comes from the authenticated `/v1/rules` view.
- **A sandbox can read the rules that apply to it**, at
  `http://egress.agentplane-staging.svc.cluster.local/v1/rules` through the sidecar's
  existing HTTP(S) proxy, the same path it uses for every other destination. The answer names the
  subject it is of and the egress policies an active binding grants it: each rule's
  hosts, methods, paths, and where a credential is substituted, its placeholder, its operator-written
  `description`, and every target — which is what a client needs to build the value and to know whose
  credential it is spending, since a header name and a placeholder leave open both whether the value
  reads `Bearer <placeholder>` or the placeholder bare, and what the token behind it can do. Never
  the Secret, its key,
  or its value — the projection is built from its own field list, so a field added to a resource
  does not appear here until someone writes it in.
  The API's existing listener shares the forward listener's explicit authentication modes,
  serving certificate, trust roots and bearer audience. It has no independent auth settings.
  In SPIFFE mode the sidecar originates destination TLS through an exact configured opaque
  CONNECT route; the API independently verifies the actual peer and accepted live Sandbox
  credential. In bearer mode the normal policy substitutes the projected destination token,
  and the API reviews it against the forward policy's audience. No forwarded identity header
  or source address is trusted. All API endpoints, including documentation, require exactly
  one admitted credential. The answer is always the authenticated ServiceAccount's own rules.
  Service port 80 reaches API port 8082; forward traffic uses 8888. TLS does not require another
  API port or a separate certificate. Operator admin endpoints stay separate.
  `GET /v1/rules` is the generated HTTP/JSON form of `EgressRules.GetRules`
  (<egress_rules.proto>). The facade preserves the API's verified context in process and opens
  no loopback listener. Authentication refusal returns `401` as `google.rpc.Status`; unavailable
  verification returns `503`.
