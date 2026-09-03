# Agentplane egress proxy

The central proxy of the secure egress integration: sandbox tools reach the outside through it,
it decides each request from resources on the API server, and it alone holds the credentials it
substitutes. The design it implements is [the ADR](../plans/adr_sandbox_proxy_gateway.md).

## Identity

- Every CONNECT and every request carries `Proxy-Authorization: Bearer <token>`, the Pod's
  projected ServiceAccount token with the proxy's audience, added by the sidecar. A tunnel's
  inner requests inherit the tunnel's token.
- The token is proven by TokenReview against that audience. The Pod it is bound to is read live:
  its UID must equal the token's, its address must equal the connection's source, and its
  controller owner must be a Sandbox that the proxy's watch knows under the same UID. That
  Sandbox is the subject.
- A verdict is cached for at most the token's remaining life, bounded by a configured limit; the
  source-address check runs on every request regardless.

## Decision

- Only `EgressBinding`s that are approved and unexpired grant anything; a binding names its
  subjects by Sandbox name or by label selector and lists `EgressPolicy` names.
- The subject's bindings are walked in name order, their policies in the listed order, and their
  rules in order; the first rule whose hosts, methods, and paths match decides. A CONNECT is
  matched on host alone; each request inside the tunnel is decided on its own.
- Hosts match exactly (case-insensitive) or by `*.` suffix, which never matches the apex. Path
  globs match the path without its query: `*` stays within one segment, `**` crosses segments.
- A matching rule with a credential replaces the placeholder in the named header with the value
  of the Secret named in the credentials namespace, also inside a `Basic` base64 payload. A known placeholder still present in its header
  after that — the rule had no credential, the header held another rule's placeholder — denies
  the request; a placeholder is never forwarded. A credential whose Secret or key is absent
  denies rather than forwards.
- Nothing else is forwarded: no binding, no rule, an unproven token, a Pod that does not match,
  an unknown Sandbox, or any failure to reach the API server all refuse. A refusal is `403`
  (`502` when the proxy itself could not decide) with an empty body and
  `x-agentplane-egress: denied; reason=<reason>`, where reason is one of `token-missing`,
  `token-rejected`, `pod-mismatch`, `sandbox-unknown`, `no-binding`, `no-rule`,
  `placeholder-unresolved`, `credential-unavailable`, `address-forbidden`, `host-unresolved`,
  `unavailable`.

## Upstream address

- The admitted host is resolved by the proxy, never by the sandbox. A host with any address that
  is not globally reachable unicast — loopback, private, link-local, carrier-grade NAT, multicast,
  reserved, and the IPv4-mapped IPv6 forms of those — is refused whole with `address-forbidden`;
  a literal address in place of a host is held to the same rule. A host that does not resolve is
  refused with `host-unresolved` (`502`).
- The connection is made to the address that was checked: a name is resolved once per admission
  window (30 seconds) and every dial in it goes to that address, so a name cannot be re-pointed
  between the check and the connect. A dial for a target the gate did not admit is not made.
- The operator may exempt networks from the reachability rule (`--exempt-networks`); production
  exempts none.

## Resources and status

- Policies, bindings, and Sandboxes of the sandbox namespace and Secrets of the credentials
  namespace are watched; the proxy's picture is kept equal to the API server's, and a rotated
  Secret is substituted from the next request on without a restart.
- Each binding's `status` is written by the proxy: `observedGeneration`, `resolvedPolicies`, and
  the `Active` condition — `True` with reason `Resolved` when the binding is approved, unexpired,
  and at least one policy resolved, otherwise `False` with reason `Expired`, `NotApproved`, or
  `MissingPolicy`. A status is written only when it differs from what the API server holds.

## Decisions

- Every decision is logged as one JSON line naming the subject, method, host, port, path,
  outcome, reason, and the binding (with its `agentplane.allegedly.works/granted-by` label),
  policy, and rule that decided it. Credential values and
  placeholders never appear in logs, decisions, or responses.
- The last decisions per subject (a configured ring, 200 by default) are served on the admin port
  at `GET /decisions?sandbox=<name>`; decisions with no proven subject at `GET /decisions`.
  `GET /healthz` is `200` while the index is both complete and moving: every kind listed at least
  once, and none more than three resync periods since it last completed a list-and-watch cycle. It
  reports how long ago each kind last completed one, so a wedged watch reads as an age rather than
  as a proxy quietly enforcing rules it stopped receiving updates to.
