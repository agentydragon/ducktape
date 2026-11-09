# Workload authentication and service routes

Sandbox-to-service traffic uses the Runtime SVID issued for the current Sandbox incarnation.
Configured non-Sandbox workloads retain Pod-bound Kubernetes bearer authentication over verified
TLS. This document describes both active routes and the remaining identity lifecycle boundary.

## Sandbox SVID route

The Sandbox Controller issues a Runtime certificate with URI SAN
`spiffe://agentplane/environment-runtime/<namespace>/<sandbox-uid>/<credential-id>`. It has server
and client authentication usages. The Environment Runtime and egress sidecar receive that
incarnation's certificate and the trust-manager CA bundle. The sidecar matches a configured
destination by exact origin, opens a CONNECT through the egress proxy, then presents the certificate
on the destination TLS connection. It strips caller `Authorization`, `Proxy-Authorization`, and
projected-token headers before sending the request.

The proxy verifies client TLS against the [workload CA](#the-workload-ca) and checks the leaf's
validity, client-auth usage, and exact URI identity. It resolves the URI's namespace, Sandbox UID, and credential ID
against the live Sandbox and current runner binding before evaluating egress policy for the
Sandbox's own ServiceAccount. Pod runners must match the Sandbox's current incarnation name and
Pod UID; VM runners must match its accepted VMI UID. No caller header supplies or overrides the
subject. The proxy does not read Pod or ServiceAccount objects.

The destination service independently verifies the client certificate against the workload CA and
resolves the same live Sandbox binding before it authorizes a request or stamps attribution metadata. Each service keeps
its own resource authorization. The SVID route does not depend on bearer substitution or a
per-service projected token audience. Finalization does not revoke a still-accepted credential:
Runtime may finish its History seal until the credential is cleared or the Sandbox is removed. See the [egress contract](../egress/SPEC.md) and
[LLM ingress](../llm_ingress/README.md) for their route-specific behavior.

## The workload CA

Every Agentplane SVID, the Runtime certificates and the services' own, comes from the
`agentplane-workload` ClusterIssuer (`cluster/cdk8s/cert_manager/cluster_ca.py`). Its CA is a
self-signed root whose key lives in cert-manager's namespace. Every listener that verifies client
SVIDs trusts the `agentplane-workload-ca-bundle` Bundle, which carries that CA alone: a
`cluster-internal-ca` leaf naming a live Sandbox's URI does not authenticate, so the live Sandbox
lookup is no longer the only check. `cluster-internal-ca-bundle` also carries the workload CA, so
clients verifying an Agentplane server certificate keep that bundle. Runtime certificates and their
Secrets carry the owning Sandbox's UID in the `agentplane.allegedly.works/sandbox-uid` label.

A `ValidatingAdmissionPolicy` (`cluster/cdk8s/agentplane/workload_certificate_admission.py`) on cert-manager `Certificate`s and `CertificateRequest`s that name the
issuer keeps it from being a cluster-wide minting service. The Sandbox Controller may request only
`spiffe://agentplane/environment-runtime/<its namespace>/<Sandbox UID>/<credential ID>` URIs in its
own namespace, and Flux's applier only one-segment service URIs such as `spiffe://agentplane/actions`.
No requester gets a CA certificate, only cert-manager creates the `CertificateRequest`s, and every
other Certificate against the issuer is refused. Certificates against other issuers are unaffected.

## TokenReview and projected credentials

The central egress proxy's `forward` listener requires TLS and allows either a Runtime SVID or a
Kubernetes bearer with the `agentplane-egress` audience. Action command/build boxes use a
Pod-bound bearer for this hop; the proxy verifies it with TokenReview and resolves the caller's
ServiceAccount and Pod binding. Sandbox sidecars select the SVID mode. Sandbox Runtime templates do
not project the proxy hop bearer.

`projectedWorkloadToken` is a separate destination credential. A sidecar offers its explicit
destination-audience token, and the proxy substitutes it only after TokenReview binds it to the same
caller Pod and a matching credential rule. Sandbox Runtime templates continue to project this
Kubernetes destination-audience token; they do not project the proxy-hop or first-party-service
audiences. The generic sidecar also retains its explicitly selected token transport for its separate
deployments.

The proxy's `forward` and services' `rpc` settings use the shared
[authentication allowlist](../workload_auth/README.md). Each first-party service uses one TLS RPC
listener for Sandbox SVIDs and existing backend bearers. Protected calls reject both a certificate
and an application credential presented together. Admission stays method-specific: enabling both
authentication modes on the listener does not grant access to backend-only methods, operator RPCs,
or delegated calls.

Backend clients verify the service's SPIFFE URI and server-auth usage against the cluster's
trust-manager roots, then present their existing TokenReview or delegation credentials. They hold no SVID.
Gateway backend and cross-replica methods keep their separate audiences and authorization;
History bearer writers remain limited to the sampler accounts. LLM inference and native ModelConfig
share one TLS HTTP/2 listener, retaining the sampler's bearer path and the service-held LiteLLM key.
Notification's signed external webhook endpoint remains a separate HTTP protocol.

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

## Non-Sandbox and future callers

Action command/build boxes and Haku UI/Anki retain Pod-bound bearer credentials over verified TLS;
the relevant listener audience and service method policy still decide whether each call is admitted.
These configured workload paths remain active while Sandbox traffic uses SVIDs. The independent
identity and SVID lifecycle for non-Sandbox and future callers remains deferred.
Do not infer that arbitrary callers are admitted, or treat an egress placeholder, source IP, or
caller-supplied identity header as authentication.

For service-mediated caller authorization, use the approved
[delegated authorization design](delegated_authorization.md). Resource-specific permissions and
selectors, service boundaries, trusted identity propagation, revocation consistency, and immutable
caller/client provenance remain part of that design.
