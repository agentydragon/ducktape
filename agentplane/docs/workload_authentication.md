# Workload authentication and service routes

Sandbox-to-service traffic uses the Runtime SVID issued for the current Sandbox incarnation through
its [`WorkloadCredential`](#workloadcredentials).
Configured non-Sandbox workloads retain Pod-bound Kubernetes bearer authentication over verified
TLS. This document describes both active routes and the remaining identity lifecycle boundary.

## Sandbox SVID route

Each Sandbox's Runtime certificate is its `WorkloadCredential`'s, with URI SAN
`spiffe://agentplane/<namespace>/<sandbox-name>/<credential-id>`. It has server and client
authentication usages. The Environment Runtime and egress sidecar receive that
incarnation's certificate and the trust-manager CA bundle. The sidecar matches a configured
destination by exact origin, opens a CONNECT through the egress proxy, then presents the certificate
on the destination TLS connection. It strips caller `Authorization`, `Proxy-Authorization`, and
projected-token headers before sending the request.

The proxy verifies client TLS against the [workload CA](#the-workload-ca) and
[resolves the peer](#how-a-verifier-resolves-a-peer) through its record before evaluating egress
policy for the record's subject, the Sandbox's own ServiceAccount. No caller header supplies or
overrides the subject. The proxy reads no Sandbox or Pod for it.

The destination service independently verifies the client certificate against the workload CA and
resolves the same record before it authorizes a request or stamps attribution metadata. Each service keeps
its own resource authorization. The SVID route does not depend on bearer substitution or a
per-service projected token audience. Finalization does not revoke a still-accepted credential:
the record is owned by the Sandbox, so Runtime may finish its History seal until the binding is
cleared or the Sandbox, and with it the record, is removed. See the [egress contract](../egress/SPEC.md) and
[LLM ingress](../llm_ingress/README.md) for their route-specific behavior.

## WorkloadCredentials

A workload's SVID is requested by its owner as a `WorkloadCredential`
(`agentplane/workload_identity`), named after the owner and controlled by it. The owner writes
`spec`; only the issuer, hosted in the Policy Controller, writes `status` and creates the
Certificate. The Sandbox Controller requests one per Sandbox, and the Action Service one per command
or build box ([sandbox Actions](../action_service/sandbox/README.md)). A Sandbox's record:

```yaml
apiVersion: agentplane.allegedly.works/v1alpha1
kind: WorkloadCredential
metadata:
  name: agent-123 # the Sandbox's name
  namespace: agentplane-staging
  ownerReferences: [{ kind: Sandbox, name: agent-123, uid: <sandbox-uid>, controller: true }]
spec: # the Sandbox Controller's
  subject: { serviceAccount: agent-123 }
  binding: { podUID: <pod-uid> } # or { vmiUID: <vmi-uid> }
  pod: { name: agent-123, uid: <pod-uid> } # the caller's Pod: the bound Pod, or a VMI's virt-launcher
status: # the Policy Controller's
  observedGeneration: 2
  credentialID: 9f2c...
  uri: spiffe://agentplane/agentplane-staging/agent-123/9f2c...
  secretName: svid-agent-123-9f2c...
  acceptedBinding: { podUID: <pod-uid> } # the first binding seen for credentialID
  conditions: [{ type: Ready, status: "True" }]
```

The issuer reads the owner reference as data and never reads the owner. Its contract is in the
[Policy Controller SPEC](../policy_controller/SPEC.md#workload-credentials).

- **The URI** is `spiffe://agentplane/<namespace>/<name>/<credential-id>`. The credential ID is the
  revocation; namespace and name pick the record with one uncached GET. There is no kind segment.
- **Credential IDs** live exactly as long as the binding they were accepted for. The first binding
  after a fresh ID accepts it, since the incarnation was started with it; when `spec.binding` later
  leaves the accepted binding, cleared or replaced, the issuer mints a fresh ID, and nothing else
  rotates one. A new Pod or a VMI replacement is a new binding. Deleting a record revokes it at
  once.
- **Subjects** change only together with a new binding, and `spec.pod` only with a new binding
  (CRD transition rules), so a running incarnation is never re-pointed.
- **Who writes:** the Sandbox Controller and the Action Service may create, update and delete records
  in their namespace, and Flux applies standing ones; only the Policy Controller may write
  `workloadcredentials/status`.

### Standing workloads

A record with no owner and no binding is standing: a workload with replicas, not a controller-owned
incarnation. cdk8s renders Haku UI's (`haku-ui`, a StatefulSet), Anki's (`haku-anki`, a
Deployment) and Jupyter's (`haku-jupyter`, a Deployment) into `haku-sandbox`
(`cluster/cdk8s/haku/ui_egress.py`), all with subject `haku-ui`;
Haku's deployer Role cannot write a record. The issuer keeps its one credential ID and names its
Secret `svid-<name>`, which `haku-state`'s `k8s/` mounts into that workload's egress sidecar alone.
Any Pod Haku creates in `haku-sandbox` could mount it, the reach the `haku-ui` ServiceAccount
already gives. haku-ci's egress relay (`haku-ci-egress`, a Deployment in `haku-ci`;
`cluster/cdk8s/haku_ci/egress_gateway.py`) is one too: its subject is its own ServiceAccount, and
only the relay mounts `svid-haku-ci-egress`, so builds in the privileged runner Pods never hold it.

Every replica presents the same SVID and proves no Pod: the proxy drops any Pod token such a caller
forwards, and decisions record no source Pod ([egress SPEC](../egress/SPEC.md)). This is weaker
than a per-incarnation credential. Revocation is deleting the record, which Flux recreates with a
fresh ID, or a Flux prune.

**Gotcha:** a running sidecar refuses a reloaded SVID whose URI differs from the first it loaded
(`environment_runtime_transport` pins it), so after a record is recreated the workload regains
egress only on a rollout restart. Routine renewals keep the URI.

### How a verifier resolves a peer

`agentplane/workload_identity/resolver` makes the same checks for every verifier, on every request,
including one inherited through a tunnel:

1. The TLS listener verifies the chain against the workload CA bundle and the client-auth usage.
2. The leaf's URI SAN must parse as `spiffe://agentplane/<namespace>/<name>/<credential-id>`, and
   the namespace must be one the verifier serves.
3. One uncached GET of that record; a missing or deleting record is refused.
4. `status.credentialID` must equal the URI's last segment and `status.uri` the whole URI. A record
   with a controller owner or a binding also needs `spec.binding` to equal `status.acceptedBinding`,
   so clearing or replacing the binding revokes before the ID rotates. A stale certificate is
   refused after a name is reused, because the new record has a new ID.
5. `spec.subject` is the subject, read live: a ServiceAccount in the record's namespace, for its
   UID; or an Identity in the policy namespace, whose live UID must be the one the record names, so
   deleting an Identity revokes every credential naming it and a same-named successor inherits
   none. A verifier constructed without the policy namespace refuses Identity subjects; the egress
   proxy and the Action Service ([caller admission](../action_service/SPEC.md#caller-admission-and-connection-authority))
   pass their own, and until each other service authorizes an Identity `Principal`
   (`SERVICES_IDENTITY_PRINCIPAL`) every other verifier does.
6. A caller that also forwards a Pod-bound token (`projectedWorkloadToken`) must present one whose
   TokenReview Pod UID equals `spec.pod`. A standing record has no `spec.pod`, so no forwarded token
   binds to it.

An API server error fails closed. Headers, metadata and request bodies never select the subject.
Services see the record's subject and `spec.pod` as an `auth.Subject`: a ServiceAccount
`Principal`, or an `IdentityPrincipal` naming the Identity and its UID, each with the Pod as the
provenance Pod. An `IdentityPrincipal` also carries its record (`auth.Workload`: namespace, name and
its controller owner, or `StandingWorkload` for an ownerless record), so a service can pin state to
the Sandbox that owns the workload. Only a method whose rule is `AnySubject` admits an Identity caller, as an
`IdentityAdmission`; every other rule, `auth.Caller` and `EffectiveCaller` stay ServiceAccount-only
and refuse one. The Action Service's Gate calls `SVIDResolver.ResolveSubject` itself. A standing workload's `Principal`, like a bound
one whose owner has not named its Pod yet, is its subject with no Pod: an operation that needs Pod
proof refuses it. The egress proxy refuses a bound record with no Pod outright.

TLS proves possession of the key, nothing more. Three things defend it: the Secret mounts only into
Runtime and the sidecar (for a VM, the launcher relay), replacing an incarnation revokes its key
through a fresh ID, and on routes that carry one, the forwarded Pod token must pass TokenReview. The
cluster has no CRL or OCSP.

### Why one issuer

Owners only request; one component issues and every verifier links one resolver. The boundary is
the `WorkloadCredential` kind, the URI form, and the rule that only the issuer creates workload
Certificates and writes their status. Where the issuer runs is not: requesters see only the
object, so moving it out of the Policy Controller later changes no requester or verifier. It is
hosted there because that process already holds identity-scoped authority (binding expiry, grants,
RoleBindings with `bind` in the subject namespaces), so Certificate create adds no new class of
trust, and issuance is a reconcile with no serving path of its own.

The constraints that rejected the alternatives:

- **Each owner issuing for itself** gives every owner Certificate create against a CA that signs any
  URI, restates the per-owner URI admission for each new owner, and makes every verifier read each
  owner's objects.
- **An RPC issuing service** cannot be called by Flux, so a standing workload would enroll with a
  bearer or a pre-declared object, and it adds a serving dependency to every Sandbox start.
- **SPIRE** attests only Pods; KubeVirt and Firecracker guests get their SVID through a launcher,
  it has no per-incarnation acceptance, and it adds a second CA and a node agent.
- **Bearers for non-Sandbox callers** need a ServiceAccount per identity once Sandboxes stop
  creating them: a second subject model.
- **`csi-driver-spiffe`** makes the ServiceAccount the identity again, and needs cert-manager's
  default approver disabled cluster-wide; so does approver-policy in place of the Certificate
  admission policy.
- **An opaque `spiffe://agentplane/<credential-id>`** needs a per-request label list, a cache, or an
  object per credential to find its record.
- **A namespaced Issuer with External Secrets copying Secrets** adds a poll to every start, makes
  the operator hold every workload key, and adds a controller to renewal. Revisit if push-based
  cross-namespace delivery appears.

The URI never names where the subject lives: under every architecture direction the record,
Certificate, Secret and workload stay in the workload cluster, and only the subject reference
crosses to wherever `Identity` lives.

## The workload CA

Every Agentplane SVID, the Runtime certificates and the services' own, comes from the
`agentplane-workload` ClusterIssuer (`cluster/cdk8s/cert_manager/cluster_ca.py`). Its CA is an
intermediate under the cluster root whose key lives in cert-manager's namespace. Every verifier of an
Agentplane SVID trusts the `agentplane-workload-ca-bundle` Bundle, which carries that intermediate
alone, so a `cluster-internal-ca` leaf naming an Agentplane URI authenticates in neither direction:

- Listeners verifying client SVIDs: a leaf naming a live Sandbox's URI does not authenticate, so the
  live record lookup is not the only check. A runner's control listener refuses one naming the
  Runner Gateway ([runner control](runner_control.md#authentication-and-incarnation-binding)).
- Clients verifying server SVIDs: the Gateway dialing runners (`runner_security`), the egress sidecar
  and launcher relay dialing the proxy and services, and backend clients and probes through
  `cluster/cdk8s/agentplane/workload_tls.py`. A leaf at a runner's or service's address naming its URI
  is refused.

The Sandbox Controller projects the Bundle into runners as `workload-ca.crt`, beside the cluster
bundle, which verifies only the Environment Runtime's own chain. Clients of non-Agentplane servers
(LiteLLM, the API server, Postgres, external hosts) keep their own roots. A record's Certificate and
Secret carry the record's UID in the `agentplane.allegedly.works/workload-credential-uid` label, by
which the issuer deletes superseded generations' Secrets: cert-manager sets no owner on a Secret.

A `ValidatingAdmissionPolicy` (`cluster/cdk8s/agentplane/workload_certificate_admission.py`) on cert-manager `Certificate`s and `CertificateRequest`s that name the
issuer keeps it from being a cluster-wide minting service. The Policy Controller may request only
record URIs, `spiffe://agentplane/<the Certificate's namespace>/<name>/<32 hex digits>`, and Flux's
applier only one-segment service URIs such as `spiffe://agentplane/actions`.
No requester gets a CA certificate, only cert-manager creates the `CertificateRequest`s, and every
other Certificate against the issuer is refused. Certificates against other issuers are unaffected.

## TokenReview and projected credentials

The central egress proxy's `forward` listener requires TLS and allows either a Runtime SVID or a
Kubernetes bearer with the `agentplane-egress` audience, which it verifies with TokenReview. No
deployed workload uses the bearer: Sandbox, command/build box and standing-workload sidecars select
the SVID mode, and their templates do not project the proxy hop bearer.

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

No non-Sandbox workload holds a proxy-hop bearer: command/build boxes and standing workloads present
their `WorkloadCredential` SVIDs.
Do not infer that arbitrary callers are admitted, or treat an egress placeholder, source IP, or
caller-supplied identity header as authentication.

For service-mediated caller authorization, use the approved
[delegated authorization design](delegated_authorization.md). Resource-specific permissions and
selectors, service boundaries, trusted identity propagation, revocation consistency, and immutable
caller/client provenance remain part of that design.
