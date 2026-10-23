# Workload authentication

`WorkloadPrincipalResolver` authenticates an ordinary destination-side
`Authorization: Bearer <token>` with Kubernetes TokenReview. The result is the immutable
`WorkloadPrincipal`:

- Kubernetes namespace;
- ServiceAccount name and full subject; and
- Pod name and UID, as the API server bound the token to them.

`WorkloadPrincipal.account` is the `ServiceAccountRef` a binding names, and it is the subject every
destination authorizes against. Whatever else owns the Pod is not the caller.

Configure the accepted audience and a non-empty namespace allowlist when constructing the resolver.
The audience of a Sandbox's workload token, which the egress proxy, the LLM ingress and the Action
Service all verify, is `WORKLOAD_TOKEN_AUDIENCE` in `audience.py`; the cluster configuration passes it
to each, since none of them defaults it. It is the compatibility value `agentplane-egress`;
deployments may migrate it to `agentplane-workload` without changing this API.

Resolution requires an authenticated TokenReview for that audience, an allowed ServiceAccount
subject, and exactly one Pod name/UID claim pair. Nothing else is read: the API server validates the
object the token is bound to, so a deleted or replaced Pod fails the TokenReview. No caller-supplied
header, body field, source address, operator identity, role, permission, Agent, or Thread
participates, and resolution does not read the Pod object. The Runner Gateway's `resolve_peer` reads
it afterwards, to follow a runner control stream's Pod to its controlling Sandbox.

The Go resolver also exposes `ResolveServiceAccountToken`, returning a `ServiceAccountPrincipal`
with the account and its UID, without a Pod identity. Actions explicitly uses that method: it
requires the same TokenReview audience, namespace and ServiceAccount UID, and accepts either no
Pod binding claims or one complete name/UID pair. Partial or duplicate claims are rejected.
Its caller admission still requires the account's live label, apart from configured read-only
service readers. `ResolveToken`, `Resolve`, `ResolveAuthorization` and `RequireAccount` retain their
Pod-bound contract for the other destinations.

**Verdict reuse (Go).** The Go `Resolver` (`go/auth`) puts its TokenReview behind `k8s.io/apiserver`'s
cached token authenticator, the cache the API server itself uses for webhook and delegated
authentication (`authenticatorfactory.DelegatingAuthenticatorConfig`). It keeps the API server's
acceptance of a bearer for `DefaultVerdictTTL` (30 s): a revoked token, or one whose Pod is gone,
still resolves here for at most that long. The key is a keyed hash of the bearer, calls for one
bearer that arrive while its TokenReview is in flight share it, and a caller that gives up does not
fail the others. Only acceptances are kept: a rejection or a failed review is asked again by the
next call, so an API server blip is never extended. The namespace allowlist and claim checks run on
every call against the kept answer. Reviewing every request would make each one wait on the API
server and spend its client's rate limit, and Sandbox Pod tokens live 600 s, so the window is small
beside the token's own life.

**Deviation:** the Python resolver (`principal.py`) reviews every call.

Rejected: verifying the projected token locally against the cluster's issuer keys. A signature
and expiry check cannot see a deleted or replaced Pod, which is the property the TokenReview
supplies, so it would bound revocation by the token's lifetime, not by the TTL, unless every
service also watched Pods.

Kubernetes API failures log only the fixed operation (`create_token_review`) and numeric status,
never exception reason, body, headers or traceback. The original API exception still propagates.
These diagnostics distinguish the failed authentication hop, not the underlying outage's cause.

`WorkloadPrincipalAuthenticator` is the small FastAPI dependency shared by first-party destination
services; `egress.identity.WorkloadIdentityVerifier` is the same translation for a proxied
connection, answering with a `DenyReason` where the dependency answers 401. It accepts exactly one well-formed Bearer credential and returns 401 otherwise, and both
halves of that rule live in `bearer.py`: `sole_header`, because several credentials are not one, and
`parse_bearer` for the `token68` spelling a projected ServiceAccount token has. A door that also
admits credentials this service did not mint uses `sole_header` alone and lets the issuer's own
parser read the value. The bearer is sent only to TokenReview: it is absent from the principal,
exception text, and representations. Destination services pass the principal to their own
authorization layer.

No layer correlates a caller's address: destinations see the central proxy's source address rather
than the Pod's, and the proxy does not read the Pod at all. Operator/session/BFF authentication is a
separate mechanism and does not produce a `WorkloadPrincipal`.
