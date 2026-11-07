# Workload authentication

[`listener`](listener/config.go) and [`listener.py`](listener.py) define the shared authoring
and Go decoding contract for workload listeners: a listen address and an explicit list of
supported authentication methods. `kubernetes_bearer` requires an audience; `spiffe` requires
serving TLS and trust roots. Unsupported modes,
empty lists, duplicates and unknown fields fail configuration loading; configuring a method does
not grant its caller authorization. The egress proxy and Runner Gateway consume this contract.

[`auth`](auth/auth.go) provides the TokenReview resolver used by Agentplane services. Each
service constructs a `Resolver` with its accepted audience and allowed namespaces. Its namespace
allowlist is checked for every request.

`ResolveAuthorization` accepts exactly one `Bearer <token>` value. `ResolveToken` verifies the
reviewed ServiceAccount identity and returns a Pod-bound `Principal` containing the account, the
ServiceAccount UID, and the Pod name and UID. The API server's TokenReview checks the bound Pod;
accepted review results are cached for up to 30 seconds by default, so revocation or Pod deletion
may take that long to affect a cached token. Rejections and review failures are not cached.

`SVIDResolver` accepts a leaf already verified by the shared workload TLS listener, requires its
validity and explicit clientAuth EKU, and resolves its namespace, Sandbox UID and credential
ID through live Sandbox status. It returns the same `Principal`: the Sandbox's own ServiceAccount
with its canonical UID and the status-bound Pod name/UID. There is no identity reference or caller
header. A finalizing Sandbox keeps using its accepted credential for History sealing; clearing
acceptance or removing the object revokes it. Services need Sandbox get/list and ServiceAccount get in their allowed namespaces.
`Resolver.WithSVID` selects this path for TLS peers while retaining backend TokenReview and delegated
callers on their existing listener. A TLS peer cannot replace its certificate with a bearer or a
delegation envelope. Stream revalidation repeats the live lookup and leaf validity checks.

[`facade`](facade/facade.go) runs a service's generated HandlerClient over an in-process gRPC
channel for its SVID HTTP listener. It carries the service-verified TLS peer in a typed context,
runs the same method interceptors as native gRPC, and preserves streaming revalidation. No caller
assertion crosses a socket or header. Backend HTTP gateways keep their existing token path.

History runner ingress, Notification, Dispatcher, Action Service, LLM ingress and the Rules API
terminate their workload TLS themselves. The proxy tunnels their encrypted traffic without
asserting a caller. The central sampler retains its delegated ServiceAccount path and holds no SVID.

The k3s `test_pod`, `mcp:test_actions`, `mcp:test_parent_delegation` and `test_real_model` suites
exercise these service boundaries together, including History after runner replacement, messaging,
native Dispatcher CLI access, Action authorization, Rules discovery and Claude/Codex model calls.
All four suites passed at implementation commit `b1e4e7460626af9a604ec76c66ec3b2ba6ba6a5d`
([BuildBuddy](https://app.buildbuddy.io/invocation/8faaf35b-bd60-4f31-9aa5-1d47a024c642)).
The resolver, facade and each service's focused integration tests cover forged/expired leaves and
live Sandbox revocation before handlers run
([focused tests](https://app.buildbuddy.io/invocation/61cc4155-9a41-46fd-b999-56bc8eea7701)).
This is implementation acceptance; the staging flip remains draft under
[`NON_SANDBOX_PROXY_AUTH`](../plans/task_dag.md#non_sandbox_proxy_auth--separate-caller-authentication-before-merging-the-flip).

`ResolveServiceAccountToken` returns a `ServiceAccountPrincipal` for destinations whose authority
belongs to the account alone. It requires a ServiceAccount UID and accepts either no Pod binding
claims or one complete Pod name and UID pair. Partial or repeated claims are rejected. The Action
Service uses this path. Other callers use Pod-bound `Principal` values.

[`methods.go`](auth/methods.go) admits a gRPC server's calls in one interceptor: `Methods` maps
each full method name to the `Rule` admitting its callers and refuses any method it does not list
([gRPC services](../docs/grpc_services.md#authentication)).

`AnyAccount` accepts a `PrincipalResolver` and puts its resolved `Principal` in `Caller`. A
TokenReview `Resolver` proves its established Pod-bound identity; another verified source supplies
only the identity fields it proves. `Accounts` and `ReviewedServiceAccount` remain TokenReview-only.

Delegated calls are a separate, opt-in mode on an authenticated direct `Rule`. `AllowDelegationFrom`
pins exact actor ServiceAccounts and per-actor caller namespace scopes to the Rule; attaching that
Rule to a full method name is the method trust grant. It preserves the Rule's ordinary direct path.
Delegation requires a concrete TokenReview `Resolver`; the actor token must be Pod-bound and is
reviewed for this service's audience. The asserted caller
must match the live ServiceAccount UID. A method with no delegation policy rejects the delegated
scheme and any delegation envelope; `Bearer` plus an envelope is rejected rather than treated as a
direct call.

Handlers can read the typed result with `Admitted` or use `EffectiveCaller` for ordinary account
checks. `DirectAdmission` keeps the direct caller and optional Pod proof together;
`DelegatedAdmission` keeps the actor's Pod-bound `Principal`, effective ServiceAccount identity and
provenance separate. A delegated actor is never installed in the legacy `Caller` context, which
continues to mean the direct principal. For TokenReview callers that principal is Pod-bound; other
`AnyAccount` sources supply only the fields they prove. The effective caller is account identity
only and cannot prove a Pod binding.

[`grpc`](grpc/client.go) provides `DialDelegated` and `WithDelegation`. The dialer reads the
destination-audience token file for each call, emits the distinct `Agentplane-Delegated` scheme and
binary envelope, rejects duplicate authorization/envelope metadata, and disables retries. It does
not install the ordinary Bearer interceptor. Existing direct clients continue using their current
Bearer helper.

[`audience.py`](audience.py) defines `WORKLOAD_TOKEN_AUDIENCE`, the `agentplane-egress` audience
retained by backend callers of LLM ingress and Action Service, including the sampler's delegated
path. Sandbox templates no longer project it, and the egress proxy does not accept it. Each backend
verifier receives its audience explicitly; the Go resolver has no default audience.

Operator and browser-session authentication use separate mechanisms and do not produce a workload
principal. The approved [delegated authorization design](../docs/delegated_authorization.md) records
the shared protocol. Action Service's [Session delegation executor](../action_service/session_delegation/README.md)
uses it for caller-scoped Environment, Dispatcher and History operations.

[`listener`](listener/config.go) and its [authoring model](listener.py) describe one address,
optional serving `tls` (`cert_file`, `key_file`), and a nonempty `auth` allowlist. `spiffe` requires
TLS and an absolute `ca_file`; `kubernetes_bearer` requires its TokenReview `audience`. Each mode
accepts only its own fields. Unknown, duplicate or incomplete modes fail startup. SPIFFE-only TLS
requires a client certificate;
a mixed listener permits no certificate but verifies any certificate presented with the same
chain, Runtime URI and explicit clientAuth checks. Supplying both a certificate and an
application authentication credential is ambiguous and fails. Selection never depends on list
order or retries another method after a failed credential. Each service retains its own
namespace admission and authorization after authentication.
