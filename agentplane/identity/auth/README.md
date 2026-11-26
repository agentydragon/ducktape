# Workload and service authentication

First-party service calls authenticate with a role's service SVID over mutual TLS. Workload callers
present the SVID of their `WorkloadCredential`. Kubernetes bearers authenticate neither kind of
caller to Agentplane receivers. Operator OIDC and external MCP OAuth retain their separate edge
contracts; Kubernetes API credentials authenticate only to Kubernetes.

[`listener`](listener/config.go) and [`listener.py`](listener.py) define the shared listener
configuration. Set `auth` to a one-entry list with `mode: spiffe` and an absolute `ca_file`, and set
the listener's TLS certificate and key. The auth entry rejects unknown keys and values of the wrong
type. Native RPC listeners may admit unauthenticated public health methods. Listener authentication
does not grant method authorization.

## Workload Identity proof

`SVIDResolver.ResolveSubject` reads the verified TLS peer. `ResolveVerifiedLeaf` accepts a leaf
already verified by the shared workload TLS listener, rechecks its validity and explicit clientAuth
EKU, and resolves it through the live `WorkloadCredential` it names
([how a verifier resolves a peer](../../docs/workload_authentication.md#how-a-verifier-resolves-a-peer)).
Every record names an Identity in the verifier's policy namespace. The returned `IdentityPrincipal`
contains that Identity's live UID, the credential record and controller owner, and any accepted Pod
name/UID. A standing workload has no controller owner or Pod proof. Operations requiring either
must check that proof explicitly; authentication alone does not supply it.

A legacy record without an Identity is retained but refused; it is never reinterpreted as a
same-named ServiceAccount. Deleting the record or replacing its credential ID revokes the
certificate. A workload SVID cannot be combined with bearer or delegation metadata or used as a
service delegation actor. Stream revalidation repeats the live lookup and leaf validity checks.

History ingress, Notifications, Dispatcher, Actions, LLM ingress and the Rules API terminate their
workload TLS themselves. The proxy tunnels their encrypted traffic without asserting a caller.
Action command/build boxes and Haku's standing workloads present their own workload SVIDs.
Sandbox Runtime templates project no Kubernetes ServiceAccount tokens and do not forward a
caller-supplied Pod token for credential substitution. Kubernetes API credentials follow the
separately granted egress flow.

## Service proof and method grants

Backend clients verify the receiver's exact service URI, workload-CA chain and server-auth usage,
and present their role's service SVID. Certificate and trust-bundle files reload on replacement;
failed TLS never selects a bearer. The shared `grpc/healthprobe` binary checks the listener through
verified TLS without application credentials.

[`methods.go`](methods.go) admits calls in one interceptor. `Methods` maps each full RPC name to
its `Rule` and refuses any unlisted method before resolving credentials
([gRPC services](../../docs/grpc_services.md#authentication)). `Public` is reserved for the standard
health service through `HealthMethods`.

`AnySubject` with an `SVIDResolver` admits authenticated workload Identities as `IdentityAdmission`.
`Services` admits only verified client SVIDs in an exact service allowlist and returns
`ServiceAdmission`; an empty list grants no direct access. `AllowServiceCallers` adds exact service
peers to an existing authenticated rule. A service principal supplies no Identity, ServiceAccount
UID, Pod proof or `WorkloadCredential`. Stream revalidation rechecks certificate validity and the
exact method grant.

## Delegation

`AllowServiceDelegationFrom` grants exact service actors permission to assert live Identities on
service-only or Identity-admitting rules. `Methods.WithServiceDelegation` installs that grant on
explicit served methods. Direct service authority and delegation authority are independent; a
service-only rule with no direct callers can still admit its configured service delegators.

The actor proves its service identity with its client certificate and sends one typed delegation
envelope without Authorization metadata. `DelegatedServiceIdentityAdmission` retains the service
actor, effective Identity and provenance separately. The effective Identity's UID must match the
live Identity in the receiver's explicitly configured policy namespace. The actor's component
grants never become the effective Identity's grants. Connection provenance requires a separate
per-method grant. The envelope cannot assert a ServiceAccount as the effective caller.

Handlers consume `Admitted` and apply their ordinary resource and policy checks. Legacy `Caller`,
and `EffectiveCaller` accessors do not turn a workload Identity or service
actor into a ServiceAccount proof. Retained ServiceAccount attribution is historical data, not a
current credential or delegation grant.

`Rule.AdmitRequest` applies the same admission to HTTP requests, including the verified TLS peer
and the `agentplane-delegation-bin` header in standard base64 (`EncodeDelegationHeader`).
[`grpc`](grpc/client.go) provides `DialDelegated` and `WithDelegation`. First-party callers configure
their service certificate and an empty token path. The dialer sends only the typed binary envelope,
rejects duplicate authorization/envelope metadata and disables retries. A failed or absent
certificate never selects bearer proof.

The [delegated authorization contract](../../docs/delegated_authorization.md) records the shared
protocol. Actions' [Session delegation executor](../../action_service/session_delegation/README.md)
uses it for caller-scoped Environment, Dispatcher and History operations.
