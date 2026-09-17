# Workload authentication

`SandboxPrincipalResolver` authenticates an ordinary destination-side
`Authorization: Bearer <token>` with Kubernetes TokenReview. The result is the immutable
`WorkloadPrincipal`:

- Kubernetes namespace;
- ServiceAccount name and full subject; and
- Pod name and UID, as the API server bound the token to them.

`WorkloadPrincipal.account` is the `ServiceAccountRef` a binding names, and it is the subject every
destination authorizes against. Whatever else owns the Pod is not the caller.

Configure the accepted audience and a non-empty namespace allowlist when constructing the resolver.
The current compatibility audience is `agentplane-egress`; deployments may migrate it to
`agentplane-workload` without changing this API.

Resolution requires an authenticated TokenReview for that audience, an allowed ServiceAccount
subject, and exactly one Pod name/UID claim pair. Nothing else is read: the API server validates the
object the token is bound to, so a deleted or replaced Pod fails the TokenReview. No caller-supplied
header, body field, source address, operator identity, role, permission, Agent, or Thread
participates, and no destination reads the Pod object.

Kubernetes API failures log only the fixed operation (`create_token_review`) and numeric status,
never exception reason, body, headers or traceback. The original API exception still propagates.
These diagnostics distinguish the failed authentication hop, not the underlying outage's cause.

`WorkloadPrincipalAuthenticator` is the small FastAPI dependency shared by first-party destination
services. It accepts exactly one well-formed Bearer credential and returns 401 otherwise, and both
halves of that rule live in `bearer.py`: `sole_header`, because several credentials are not one, and
`parse_bearer` for the `token68` spelling a projected ServiceAccount token has. A door that also
admits credentials this service did not mint uses `sole_header` alone and lets the issuer's own
parser read the value. The bearer is sent only to TokenReview: it is absent from the principal,
exception text, and representations. Destination services pass the principal to their own
authorization layer.

No layer correlates a caller's address: destinations see the central proxy's source address rather
than the Pod's, and the proxy does not read the Pod at all. Operator/session/BFF authentication is a
separate mechanism and does not produce a `WorkloadPrincipal`.
