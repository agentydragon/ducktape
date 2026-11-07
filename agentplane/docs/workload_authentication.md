# Workload authentication and service routes

Sandbox-to-service traffic uses the Runtime SVID issued for the current Sandbox incarnation. This
document describes that route and the remaining TokenReview compatibility paths; it does not define
authentication for external callers.

## Sandbox SVID route

The Sandbox Controller issues a Runtime certificate with URI SAN
`spiffe://agentplane/environment-runtime/<namespace>/<sandbox-uid>/<credential-id>`. It has server
and client authentication usages. The Environment Runtime and egress sidecar receive that
incarnation's certificate and the trust-manager CA bundle. The sidecar matches a configured
destination by exact origin, opens a CONNECT through the egress proxy, then presents the certificate
on the destination TLS connection. It strips caller `Authorization`, `Proxy-Authorization`, and
projected-token headers before sending the request.

The proxy verifies client TLS against the configured CA and checks the leaf's validity, client-auth
usage, and exact URI identity. It resolves the URI's namespace, Sandbox UID, and credential ID
against the live Sandbox and current runner binding before evaluating egress policy for the
Sandbox's own ServiceAccount. Pod runners must match the Sandbox's current incarnation name and
Pod UID; VM runners must match its accepted VMI UID. No caller header supplies or overrides the
subject. The proxy does not read Pod or ServiceAccount objects.

The destination service independently verifies the client certificate and resolves the same live
Sandbox binding before it authorizes a request or stamps attribution metadata. Each service keeps
its own resource authorization. The SVID route does not depend on bearer substitution or a
per-service projected token audience. Finalization does not revoke a still-accepted credential:
Runtime may finish its History seal until the credential is cleared or the Sandbox is removed. See the [egress contract](../egress/SPEC.md) and
[LLM ingress](../llm_ingress/README.md) for their route-specific behavior.

## TokenReview compatibility

The central proxy requires the Sandbox SVID; it has no plaintext token listener. The generic
sidecar still supports an explicitly selected token mode for separate deployments. Kubernetes
retains its destination-audience `projectedWorkloadToken`, checked against the Sandbox's current
Pod binding and substituted only for the matching credential rule. Sandbox templates project no
first-party service or proxy audience.

The proxy's `forward` and Gateway's `workload` listener settings use the shared
[authentication allowlist](../workload_auth/README.md). The deployed workload listeners select
SPIFFE only. Gateway workload ingress can explicitly enable `kubernetes_bearer` on the same TLS
listener; it then verifies that listener's configured audience and retains the same resource
authorization. Gateway backend and cross-replica APIs retain their separate audiences and method
policies. A presented certificate and an application credential together are rejected.

The LLM ingress also retains its HTTP TokenReview listener for direct requests from the central
sampler. Its Sandbox SVID listener is separate, accepts no bearer fallback, and uses its server-held
LiteLLM virtual key only on the internal LiteLLM hop. Caller headers and request bodies are never
identity evidence; verified workload metadata is stamped by the service.

## Service authorization and attribution

Authentication yields only the verified Sandbox ServiceAccount and, when proven, Pod identity.
Each destination service retains its own authorization checks, policy, and resource ownership.
Neither a Sandbox certificate nor a successful authentication grants Action, Thread, operator, or
cross-service permissions by itself. The egress proxy's allow decision and the destination's
authorization answer separate questions.

The LLM ingress strips caller identity-like headers and body claims, forwards provider-native
requests and responses, and stamps verified Sandbox and Pod metadata into LiteLLM's supported
metadata header. Its server-held LiteLLM key is never exposed to the runner. See
[`../llm_ingress/README.md`](../llm_ingress/README.md).

## External callers

The Sandbox SVID route does not authenticate command boxes, public-coder workloads, or other
non-Sandbox callers. Their service access remains unavailable until a separate caller-authentication
design defines the authority and authorization boundary. Do not treat an egress placeholder, source
IP, or caller-supplied identity header as that design.

For service-mediated caller authorization, use the approved
[delegated authorization design](delegated_authorization.md). Resource-specific permissions and
selectors, service boundaries, trusted identity propagation, revocation consistency, and immutable
caller/client provenance must be settled before a cross-service extension.
