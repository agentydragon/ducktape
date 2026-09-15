# Sandbox workload authentication

`SandboxPrincipalResolver` authenticates an ordinary destination-side
`Authorization: Bearer <token>` with Kubernetes TokenReview and resolves the exact live managed
Sandbox that owns the token-bound Pod. The result is the immutable `SandboxPrincipal`:

- Kubernetes namespace;
- ServiceAccount name and full subject;
- Pod name and UID; and
- Sandbox name and UID.

Configure the accepted audience and a non-empty namespace allowlist when constructing the resolver.
The current compatibility audience is `agentplane-egress`; deployments may migrate it to
`agentplane-workload` without changing this API.

Resolution requires an authenticated TokenReview for that audience, an allowed ServiceAccount
subject, exactly one Pod name/UID claim pair, the same live Pod UID, and exactly one controller
Sandbox owner with a name and UID. Deleted or replaced Pods and incomplete or ambiguous ownership
fail closed. No caller-supplied Sandbox header, body field, source address, operator identity, role,
permission, Agent, or Thread participates.

Kubernetes API failures log only the fixed operation (`create_token_review` or
`read_namespaced_pod`) and numeric status, never exception reason, body, headers or traceback.
The original API exception still propagates; a live Pod read returning 404 remains a Pod mismatch.
These diagnostics distinguish the failed authentication hop, not the underlying outage's cause.

`SandboxPrincipalAuthenticator` is the small FastAPI dependency shared by first-party destination
services. It accepts exactly one well-formed Bearer credential and returns 401 otherwise. The bearer
is sent only to TokenReview: it is absent from the principal, exception text, and representations.
Destination services should pass the principal to their own authorization layer.

The central egress proxy separately correlates the live Pod address with its direct sidecar
connection. Destinations must not repeat that check: they see the central proxy's source address,
not the Sandbox Pod's. Operator/session/BFF authentication is a separate mechanism and does not
produce a `SandboxPrincipal`.
