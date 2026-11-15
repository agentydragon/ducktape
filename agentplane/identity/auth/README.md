# Workload authentication

[`listener`](listener/config.go) and [`listener.py`](listener.py) define the shared authoring
and Go decoding contract for workload listeners: a listen address and an explicit list of
supported authentication methods. `kubernetes_bearer` lists its allowed audiences; `spiffe` requires
TLS and an absolute trust-root path. Unsupported modes, empty lists, duplicates and unknown fields
fail configuration loading. For protected calls, exactly one enabled credential must select a mode;
both or neither are refused, and selection never falls back based on list order. Native RPC listeners
may still admit unauthenticated public health methods. Listener authentication does not grant method
authorization. The egress proxy and first-party service RPC listeners consume this contract.

[`auth`](auth.go) retains a legacy TokenReview resolver for receivers awaiting bearer retirement.
Its accepted audience and namespace allowlist are explicit. First-party service callers use service
SVIDs; these helpers do not describe their current caller authentication.

`ResolveAuthorization` accepts exactly one RFC 6750 Bearer value. `ResolveToken` verifies the
reviewed ServiceAccount identity and returns a Pod-bound `Principal` containing the account, the
ServiceAccount UID, and the Pod name and UID. The API server's TokenReview checks the bound Pod;
accepted review results are cached for up to 30 seconds by default, so revocation or Pod deletion
may take that long to affect a cached token. Rejections and review failures are not cached.

`SVIDResolver` accepts a leaf already verified by the shared workload TLS listener, requires its
validity and explicit clientAuth EKU, and resolves it through the live `WorkloadCredential` it names
([how a verifier resolves a peer](../../docs/workload_authentication.md#how-a-verifier-resolves-a-peer)).
Every record names an Identity in the verifier's policy namespace; the returned `IdentityPrincipal`
contains that Identity's live UID and, when the record's owner accepted one, the Pod name/UID. A
legacy record without an Identity is retained but refused; it is never reinterpreted as a
same-named ServiceAccount. ServiceAccount principals come only from TokenReview. Deleting the
record or replacing its credential ID revokes the certificate. `Resolver.WithListenerAuthentication`
selects this path only when the listener allows SPIFFE. Listener credential selection does not grant
method authorization, and delegated calls still require their explicit per-method grant. A workload
SVID cannot be combined with a bearer or used as a delegation actor; service actors have a separate
certificate rule. Stream revalidation repeats the live lookup
and leaf validity checks.

History runner ingress, Notification, Dispatcher, Action Service, LLM ingress and the Rules API
terminate their workload TLS themselves. The proxy tunnels their encrypted traffic without
asserting a caller. The central sampler authenticates to services with its own service SVID;
when it delegates Session work, the envelope names the Identity recorded for that Session.

Each service uses one TLS RPC listener for Sandbox and backend callers. Backend clients verify the
service's SPIFFE URI using `peertls.ClientTLS` and present their role's service SVID.
Certificate and trust-bundle files reload on replacement; failed TLS never selects a bearer. The shared `grpc/healthprobe`
binary checks the same listener through verified TLS without application credentials.

Action command/build boxes and Haku's and `haku-ci`'s standing workloads present their own
`WorkloadCredential`'s SVID; the egress proxy's `forward` listener authenticates SVIDs alone. Sandbox
Runtime templates project no Kubernetes ServiceAccount tokens and do not forward a caller-supplied Pod token
for credential substitution. Kubernetes API credentials follow the separately granted egress flow.

`ResolveServiceAccountToken` returns a `ServiceAccountPrincipal` for destinations whose authority
belongs to the account alone. It requires a ServiceAccount UID and accepts either no Pod binding
claims or one complete Pod name and UID pair. Partial or repeated claims are rejected. This is a
legacy account admission API, separate from service-SVID authentication.

[`methods.go`](methods.go) admits a gRPC server's calls in one interceptor: `Methods` maps
each full method name to the `Rule` admitting its callers and refuses any method it does not list
([gRPC services](../../docs/grpc_services.md#authentication)).

`Services` admits only verified client SVIDs in its exact service allowlist and returns a
`ServiceAdmission`; an empty list grants no direct access. It refuses bearer credentials, workload SVIDs
and ungranted delegation. PolicyService uses this rule for its separate reader and writer grants.
A service admission supplies no Identity, ServiceAccount UID or Pod proof. Stream revalidation
rechecks the certificate validity and exact grant.

`Accounts` admits only the listed ServiceAccounts. `SVIDOrAccounts` admits current Sandbox SVIDs
and applies the account allowlist only to bearer callers, preserving History's sampler writer policy;
an SVID naming an Identity is admitted as `AnySubject` admits it.
`ReviewedServiceAccount` retains the separate optional Pod proof. `AnySubject` accepts a `SubjectResolver`
and admits any ServiceAccount or Identity that source authenticates; Identity callers are returned
as an `IdentityAdmission`.
`ReviewedServiceAccountOrIdentity` admits a ServiceAccount as `ReviewedServiceAccount` does, or an Identity
as an `IdentityAdmission`.

Delegated calls are a separate, opt-in mode on an authenticated direct `Rule`. `AllowDelegationFrom`
pins exact actor ServiceAccounts to the Rule; attaching that Rule to a full method name is the method
trust grant. It preserves the Rule's ordinary direct path. Delegation requires a concrete TokenReview
`Resolver`; the actor token must be Pod-bound and is reviewed for this service's audience. An asserted
Identity needs the actor's separate `Delegator.IdentityCallers` grant, a Rule whose direct mode admits
Identity callers (`ReviewedServiceAccountOrIdentity`, `AnySubject`, `SVIDOrAccounts`), and the live
Identity's UID in the receiver's policy namespace (`KubernetesIdentityUIDs`, an uncached read);
otherwise it is `PERMISSION_DENIED`. Legacy `caller_namespaces` config fields never grant authority;
strict service settings reject them. Delegation never asserts a ServiceAccount as the effective caller. A method with no
delegation policy rejects the delegated scheme and any delegation envelope; `Bearer` plus an envelope
is rejected rather than treated as a direct call. The envelope's provenance is one typed profile,
an Action's records, a central Session, or a consented MCP Connection/grant; the handler consumes only
the profile its method serves. Connection provenance is a separate per-method grant, not actor
privilege.
`Rule.AdmitRequest` admits an HTTP request by the same Rule and middleware, reading the
`Authorization` header, the verified TLS peer and the `agentplane-delegation-bin` header in
standard base64 (`EncodeDelegationHeader`), the form gRPC itself transports `-bin` metadata in.

Handlers read the typed result with `Admitted`. `DirectAdmission` keeps a direct ServiceAccount caller
and optional Pod proof together; `DelegatedIdentityAdmission` keeps the actor's Pod-bound `Principal`,
effective `Identity`, and provenance separate. `EffectiveCaller` returns only a direct ServiceAccount
and refuses direct or delegated Identity admissions. A delegated actor is never installed in the
legacy `Caller` context, which continues to mean the direct principal. For TokenReview callers that
principal is Pod-bound; other `AnySubject` sources supply only the fields they prove. An Identity is
never converted to a ServiceAccount for resource checks and cannot prove a Pod binding.

[`grpc`](grpc/client.go) provides `DialDelegated` and `WithDelegation`. First-party callers configure
their service certificate and an empty token path. The dialer sends only the binary typed envelope,
rejects duplicate authorization/envelope metadata, and disables retries. A failed or absent
certificate never selects legacy bearer proof. The separately retained token mode is not selected
by first-party service deployments.

**Backend service admission.** `AllowServiceCallers` adds an exact service allowlist to an
existing authenticated rule; `Services` constructs a service-only rule with no workload or bearer
resolver. `AllowServiceDelegationFrom` separately grants service actors permission to assert live
Identities on service-only or Identity-admitting rules, retaining the service actor, effective Identity
and typed provenance. A service-only rule with no direct callers can admit only its configured
service delegators; it cannot gain legacy bearer delegation. Service principals never become `Account`, `Principal`, `IdentityPrincipal` or workload
`WorkloadCredential` values. Receiver-specific bearer paths remain until their own removal.

[`audience.py`](audience.py) retains the legacy `agentplane-egress` audience while its receiver
settings retire. First-party callers no longer mint or present it, and the proxy's `forward`
listener admits SVIDs alone. Kubernetes destination-audience tokens are a separate credential flow.

Operator and browser-session authentication use separate mechanisms and do not produce a workload
principal. The approved [delegated authorization design](../../docs/delegated_authorization.md) records
the shared protocol. Action Service's [Session delegation executor](../../action_service/session_delegation/README.md)
uses it for caller-scoped Environment, Dispatcher and History operations.

Each listener mode accepts only its own fields, and each resolver verifies its own audience and
namespace policy after selection. A failed credential never triggers a retry with another mode.
Each service retains its own method authorization after authentication.
