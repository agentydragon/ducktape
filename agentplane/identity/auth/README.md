# Workload authentication

[`listener`](listener/config.go) and [`listener.py`](listener.py) define the shared authoring
and Go decoding contract for workload listeners: a listen address and an explicit list of
supported authentication methods. `kubernetes_bearer` lists its allowed audiences; `spiffe` requires
TLS and an absolute trust-root path. Unsupported modes, empty lists, duplicates and unknown fields
fail configuration loading. For protected calls, exactly one enabled credential must select a mode;
both or neither are refused, and selection never falls back based on list order. Native RPC listeners
may still admit unauthenticated public health methods. Listener authentication does not grant method
authorization. The egress proxy and first-party service RPC listeners consume this contract.

[`auth`](auth/auth.go) provides the TokenReview resolver used by Agentplane services. Each
service constructs a `Resolver` with its accepted audience and allowed namespaces. Its namespace
allowlist is checked for every request.

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
method authorization, and delegated calls still require their explicit per-method grant. A TLS peer
cannot be combined with a bearer or delegation envelope. Stream revalidation repeats the live lookup
and leaf validity checks.

History runner ingress, Notification, Dispatcher, Action Service, LLM ingress and the Rules API
terminate their workload TLS themselves. The proxy tunnels their encrypted traffic without
asserting a caller. The central sampler authenticates to services as its own component ServiceAccount;
when it delegates Session work, the envelope names the Identity recorded for that Session.

Each service uses one TLS RPC listener for Sandbox and backend callers. Backend clients verify the
service's SPIFFE URI using `environment_runtime_transport.ClientTLS`, then authenticate with their
existing bearer or delegation credentials. They receive no SVID. The shared `grpc/healthprobe`
binary checks the same listener through verified TLS without application credentials.

Action command/build boxes and Haku's and `haku-ci`'s standing workloads present their own
`WorkloadCredential`'s SVID; the egress proxy's `forward` listener authenticates SVIDs alone. Sandbox
Runtime templates continue projecting Kubernetes destination-audience tokens where credential rules
require them; they do not project first-party-service audiences.

`ResolveServiceAccountToken` returns a `ServiceAccountPrincipal` for destinations whose authority
belongs to the account alone. It requires a ServiceAccount UID and accepts either no Pod binding
claims or one complete Pod name and UID pair. Partial or repeated claims are rejected. The Action
Service uses this path. Other callers use Pod-bound `Principal` values.

[`methods.go`](auth/methods.go) admits a gRPC server's calls in one interceptor: `Methods` maps
each full method name to the `Rule` admitting its callers and refuses any method it does not list
([gRPC services](../../docs/grpc_services.md#authentication)).

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

[`grpc`](grpc/client.go) provides `DialDelegated` and `WithDelegation`. The dialer reads the
destination-audience token file for each call, emits the distinct `Agentplane-Delegated` scheme and
binary envelope, rejects duplicate authorization/envelope metadata, and disables retries. It does
not install the ordinary Bearer interceptor. Existing direct clients continue using their current
Bearer helper.

[`audience.py`](audience.py) defines `WORKLOAD_TOKEN_AUDIENCE`, the `agentplane-egress` audience
used by backend callers of LLM ingress and Action Service: the sampler, Notification, and the MCP
edge's minted Connection tokens. The egress proxy's hop still accepts it, though no workload sends
it there. Sandbox Runtime templates do not project it. Each backend
verifier receives its audience explicitly; the Go resolver has no default audience.

Operator and browser-session authentication use separate mechanisms and do not produce a workload
principal. The approved [delegated authorization design](../../docs/delegated_authorization.md) records
the shared protocol. Action Service's [Session delegation executor](../../action_service/session_delegation/README.md)
uses it for caller-scoped Environment, Dispatcher and History operations.

Each listener mode accepts only its own fields, and each resolver verifies its own audience and
namespace policy after selection. A failed credential never triggers a retry with another mode.
Each service retains its own method authorization after authentication.
